"""Hybrid retrieval over a department's chunks.

Two channels, fused by Reciprocal Rank Fusion:

- **dense** — pgvector cosine distance over `embedding`, HNSW-indexed.
- **lexical** — Postgres full text over the generated `tsv` column, GIN-indexed.

RRF is used rather than a weighted blend because a cosine distance and a
`ts_rank_cd` score share no scale and never will; ranks are the only thing the
two channels have in common. The trade is that the fused score carries **no
absolute meaning** — the top hit in a department with nothing relevant scores
exactly like a perfect match. That is why `rrf_score` must never be used as a
relevance threshold, and why abstention waits on a reranker (slice 3+).

`dense_distance` and `lexical_score` are carried through for diagnostics only.

Department scoping is not enforced here by convention: a chunk's `department_id`
is held to its document's by a composite FK, so `WHERE department_id = ?` is a
database invariant. The value comes from `current_department()` — never from a
tool argument, never from the request body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text

from ..db.session import SessionLocal

# One statement, both channels, fused in the database.
#
# Both candidate CTEs are MATERIALIZED deliberately. A `ROW_NUMBER()` sitting at
# the same query level as the `LIMIT` blocks Postgres' top-N heapsort (window
# functions are evaluated before ORDER BY/LIMIT), which measured 14.7 ms vs
# 8.4 ms on a 20k-chunk table. It is also pgvector's documented pattern for
# reordering under `hnsw.iterative_scan = relaxed_order`.
_SEARCH_SQL = """
WITH dense_candidates AS MATERIALIZED (
    SELECT id, embedding <=> CAST(:qvec AS vector) AS distance
      FROM document_chunks
     WHERE department_id = :dept
     ORDER BY embedding <=> CAST(:qvec AS vector)
     LIMIT :pool
),
dense AS (
    SELECT id, distance, ROW_NUMBER() OVER (ORDER BY distance) AS rank
      FROM dense_candidates
),
lexical_candidates AS MATERIALIZED (
    SELECT c.id, ts_rank_cd(c.tsv, q.query) AS lexical_score
      FROM document_chunks c
      CROSS JOIN LATERAL (
           SELECT websearch_to_tsquery('english', :qtext) AS query
      ) q
     WHERE c.department_id = :dept
       AND c.tsv @@ q.query
     ORDER BY lexical_score DESC
     LIMIT :pool
),
lexical AS (
    SELECT id, lexical_score, ROW_NUMBER() OVER (ORDER BY lexical_score DESC) AS rank
      FROM lexical_candidates
),
fused AS (
    SELECT COALESCE(d.id, l.id) AS id,
           d.distance      AS dense_distance,
           l.lexical_score AS lexical_score,
           d.rank          AS dense_rank,
           l.rank          AS lexical_rank,
           COALESCE(1.0 / (:rrf_k + d.rank), 0)
         + COALESCE(1.0 / (:rrf_k + l.rank), 0) AS rrf_score
      FROM dense d
      FULL OUTER JOIN lexical l USING (id)
)
SELECT c.id            AS chunk_id,
       c.document_id   AS document_id,
       doc.title       AS title,
       doc.file_name   AS file_name,
       doc.file_type   AS file_type,
       c.content       AS content,
       c.page_number   AS page_number,
       c.section       AS section,
       c.element_type  AS element_type,
       -- Opaque to retrieval: the chunk's own provenance and the document's,
       -- carried through verbatim so a caller can render a citation without
       -- retrieval knowing any origin's metadata schema. The NRB tool reads
       -- `route`/`authoritative` (chunk) and `page_url`/`published_at` (doc)
       -- out of these; a generic upload's are simply empty.
       c.metadata      AS chunk_metadata,
       doc.metadata    AS doc_metadata,
       fused.rrf_score      AS rrf_score,
       fused.dense_distance AS dense_distance,
       fused.lexical_score  AS lexical_score,
       fused.dense_rank     AS dense_rank,
       fused.lexical_rank   AS lexical_rank
  FROM fused
  JOIN document_chunks c ON c.id = fused.id
  JOIN documents doc     ON doc.id = c.document_id
 -- Belt and braces. Slice 2's invariant already guarantees this: chunks are
 -- written and `status='ready'` set in the same transaction, archiving deletes
 -- them, and a failed re-ingest of a ready document keeps both. So a chunk
 -- exists IFF its document is ready. We join `documents` for the citation title
 -- anyway, so the guard is free — it is not the mechanism.
 WHERE doc.status = 'ready'
 ORDER BY fused.rrf_score DESC
 LIMIT :limit
"""


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: str
    title: str
    content: str
    page_number: int | None
    section: str | None
    element_type: str | None
    rrf_score: float
    # Diagnostics only. Neither is a relevance threshold: cosine distance has no
    # corpus-independent meaning and ts_rank_cd is unnormalized.
    dense_distance: float | None
    lexical_score: float | None
    # Which rank each channel gave this chunk, or None if that channel did not
    # return it at all. Diagnostics only — never rendered into the tool result.
    # These make a bad retrieval attributable to a channel from stored data
    # instead of a hand-built reproduction.
    dense_rank: int | None
    lexical_rank: int | None
    # The chunk's `document_chunks.metadata` and its document's `documents.metadata`,
    # verbatim. Retrieval does not interpret them — an NRB chunk carries `route`
    # and (for OCR) `authoritative: false` here, and its document carries
    # `page_url`/`published_at`, which the citation renders as provenance and a
    # trust caveat. Empty for a generic upload.
    chunk_metadata: dict = field(default_factory=dict)
    doc_metadata: dict = field(default_factory=dict)
    # Carried for citations, not for retrieval. Defaulted so existing callers
    # that construct this by position keep working; the `documents` join is
    # already there for `title`, so these two columns are free.
    file_name: str | None = None
    file_type: str | None = None


def _as_dict(value: object) -> dict:
    """A JSONB column, however the driver handed it back, as a dict.

    SQLAlchemy's asyncpg dialect usually decodes JSONB to a Python object, but a
    raw `text()` SELECT carries no type for the column, so the value can arrive as
    a JSON string instead. Handle both, and treat anything unexpected as empty
    rather than raising inside retrieval.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _vector_literal(vector: list[float]) -> str:
    """pgvector's text input form. Built as a literal and CAST in SQL because a
    Python list is not an asyncpg-bindable vector."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def search_chunks(
    *,
    department_id: int,
    query_text: str,
    query_vector: list[float],
    limit: int,
    candidate_pool: int,
    rrf_k: int,
    ef_search: int,
) -> list[RetrievedChunk]:
    """Run the hybrid search for one department. Opens its own short-lived
    session, like the file sink — a tool has no request-scoped session.

    The two `SET LOCAL`s and the SELECT must share one transaction on one
    connection, or the settings apply to a connection the query never uses.
    `set_config(..., true)` rather than `SET LOCAL hnsw.ef_search = :ef`
    because SET LOCAL takes a literal and cannot bind a parameter — the
    alternative would be string interpolation into SQL.
    """
    params = {
        "qvec": _vector_literal(query_vector),
        "qtext": query_text,
        "dept": department_id,
        "pool": max(1, candidate_pool),
        "rrf_k": rrf_k,
        "limit": max(1, limit),
    }

    async with SessionLocal() as session:
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        await session.execute(
            text("SELECT set_config('hnsw.ef_search', :ef, true)"),
            # Bound as TEXT: set_config's second parameter is text, so asyncpg
            # types $1 as text and rejects a Python int outright. int() first so
            # a non-numeric value can never reach the statement.
            {"ef": str(int(ef_search))},
        )
        rows = (await session.execute(text(_SEARCH_SQL), params)).mappings().all()
        await session.rollback()  # read-only: release the connection promptly

    return [
        RetrievedChunk(
            chunk_id=r["chunk_id"],
            document_id=r["document_id"],
            title=r["title"],
            content=r["content"],
            page_number=r["page_number"],
            section=r["section"],
            element_type=r["element_type"],
            rrf_score=float(r["rrf_score"]),
            dense_distance=(
                None if r["dense_distance"] is None else float(r["dense_distance"])
            ),
            lexical_score=(
                None if r["lexical_score"] is None else float(r["lexical_score"])
            ),
            dense_rank=(None if r["dense_rank"] is None else int(r["dense_rank"])),
            lexical_rank=(
                None if r["lexical_rank"] is None else int(r["lexical_rank"])
            ),
            chunk_metadata=_as_dict(r["chunk_metadata"]),
            doc_metadata=_as_dict(r["doc_metadata"]),
            file_name=r["file_name"],
            file_type=r["file_type"],
        )
        for r in rows
    ]

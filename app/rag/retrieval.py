"""Hybrid retrieval over a department's chunks.

Three channels, fused by Reciprocal Rank Fusion:

- **dense** — pgvector cosine distance over `embedding`, HNSW-indexed.
- **lexical** — Postgres full text over the generated `tsv` column, GIN-indexed.
- **title** — documents ranked by how many of the question's terms their title
  contains, each entering through its one chunk nearest the query vector. It
  exists because a chunk does not carry its document's title: Circular No. 1
  and No. 2 differ in text by one digit, the vectors cannot tell them apart,
  and the lexical channel ANDs every word of a natural question and so matches
  almost nothing (8 of 9 real questions had 0 lexical hits — §9.10 of
  `docs/prod-incident-2026-09-20.md`). Query-time only: no migration.

RRF is used rather than a weighted blend because a cosine distance, a
`ts_rank_cd` score and a count of title terms share no scale and never will;
ranks are the only thing the channels have in common. The trade is that the
fused score carries **no absolute meaning** — the top hit in a department with
nothing relevant scores exactly like a perfect match. That is why `rrf_score`
must never be used as a relevance threshold, and why abstention waits on a
reranker (slice 3+).

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

# One statement, all three channels, fused in the database.
#
# The candidate CTEs are MATERIALIZED deliberately. A window function sitting at
# the same query level as the `LIMIT` blocks Postgres' top-N heapsort (window
# functions are evaluated before ORDER BY/LIMIT), which measured 14.7 ms vs
# 8.4 ms on a 20k-chunk table. It is also pgvector's documented pattern for
# reordering under `hnsw.iterative_scan = relaxed_order`.
#
# Every channel ranks with RANK(), not ROW_NUMBER(): tied scores get tied ranks.
# Two chunks with identical text have identical vectors, and ROW_NUMBER() would
# rank them 1 and 2 on nothing but row order — an invented lead that, under RRF,
# exactly cancels the one real lead the title channel gives the other.
_SEARCH_SQL = """
WITH dense_candidates AS MATERIALIZED (
    SELECT id, embedding <=> CAST(:qvec AS vector) AS distance
      FROM document_chunks
     WHERE department_id = :dept
     ORDER BY embedding <=> CAST(:qvec AS vector)
     LIMIT :pool
),
dense AS (
    SELECT id, distance, RANK() OVER (ORDER BY distance) AS rank
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
    SELECT id, lexical_score, RANK() OVER (ORDER BY lexical_score DESC) AS rank
      FROM lexical_candidates
),
-- The title channel ranks DOCUMENTS by how many of the question's distinct
-- terms their title contains (OR semantics), then enters each matching
-- document through its chunk nearest the query vector. Computed at query time
-- from `documents.title`: no column, no index, no migration.
--
-- A term is a lexeme of the text after ASCII digits are folded to Devanagari,
-- with a pure number's leading zeros dropped, so `2`, `२`, `02` and `०२` are one
-- term. Folding TO Devanagari, not from it: the parser splits `२०८३/८४` into
-- two words but keeps `2083/84` whole, and most titles are Nepali. The SAME
-- fold is written out twice below — question side and title side — and must
-- stay identical; tests cover each direction.
title_terms AS MATERIALIZED (
    SELECT array_agg(DISTINCT n.term) AS terms
      FROM unnest(tsvector_to_array(to_tsvector('english',
               translate(:qtext, '0123456789', '०१२३४५६७८९')))) lx
     CROSS JOIN LATERAL (
           SELECT CASE WHEN lx ~ '^[०-९]+$'
                       THEN COALESCE(NULLIF(ltrim(lx, '०'), ''), '०')
                       ELSE lx END AS term
     ) n
     WHERE n.term <> ALL (CAST(:ignored_terms AS text[]))
),
title_documents AS MATERIALIZED (
    SELECT d.id AS document_id, RANK() OVER (ORDER BY m.matched DESC) AS rank
      FROM documents d
      CROSS JOIN title_terms q
      CROSS JOIN LATERAL (
           SELECT count(DISTINCT n.term) AS matched
             FROM unnest(tsvector_to_array(to_tsvector('english',
                      translate(d.title, '0123456789', '०१२३४५६७८९')))) lx
            CROSS JOIN LATERAL (
                  SELECT CASE WHEN lx ~ '^[०-९]+$'
                              THEN COALESCE(NULLIF(ltrim(lx, '०'), ''), '०')
                              ELSE lx END AS term
            ) n
            WHERE n.term = ANY (q.terms)
      ) m
     WHERE d.department_id = :dept
       AND d.status = 'ready'
       AND m.matched > 0
),
title AS MATERIALIZED (
    -- `rank <= :pool`, not LIMIT: documents tied at the boundary all get in,
    -- rather than whichever the database emitted first (a LIMIT measured worse:
    -- it cut §9.10's UD2082 EN answer out of the pool). The price is that cost
    -- follows the ties — every chunk of every admitted document is scored:
    -- 4k-16k chunks, ~46 ms median on 338 documents / 26k chunks, 10x the
    -- two-channel statement.
    SELECT DISTINCT ON (c.document_id) c.id, td.rank
      FROM title_documents td
      JOIN document_chunks c ON c.document_id = td.document_id
     WHERE td.rank <= :pool
       AND c.department_id = :dept
     ORDER BY c.document_id, c.embedding <=> CAST(:qvec AS vector)
),
fused AS (
    SELECT id,
           d.distance      AS dense_distance,
           l.lexical_score AS lexical_score,
           d.rank          AS dense_rank,
           l.rank          AS lexical_rank,
           t.rank          AS title_rank,
           COALESCE(1.0 / (:rrf_k + d.rank), 0)
         + COALESCE(1.0 / (:rrf_k + l.rank), 0)
         + COALESCE(1.0 / (:rrf_k + t.rank), 0) AS rrf_score
      FROM dense d
      FULL OUTER JOIN lexical l USING (id)
      FULL OUTER JOIN title t USING (id)
)
SELECT c.id            AS chunk_id,
       c.document_id   AS document_id,
       doc.title       AS title,
       doc.file_name   AS file_name,
       doc.file_type   AS file_type,
       doc.source      AS doc_source,
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
       fused.lexical_rank   AS lexical_rank,
       fused.title_rank     AS title_rank
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


# Terms the title channel ignores in a QUESTION, because matching them says
# nothing about WHICH document is meant.
#
# Mostly Nepali grammar: a natural question is mostly these (`ब्याजदर करिडोर
# भनेको के हो र यसले कसरी काम गर्छ?`), Postgres' 'english' configuration has no
# Nepali stop-words, and a title that merely shares the grammar must not outrank
# the one that names the subject. English stop-words need no list:
# `to_tsvector('english', ...)` already drops them. Written from the §9.10
# questions, not reviewed by a Nepali reader.
TITLE_IGNORED_TERMS: tuple[str, ...] = (
    # postpositions and case markers written as separate words
    "मा", "ले", "को", "का", "की", "लाई", "बाट", "देखि", "सम्म", "अनुसार", "लागि",
    # conjunctions
    "र", "वा", "तथा", "पनि",
    # question words
    "के", "कति", "कसरी", "किन", "कुन", "कुनै", "कहाँ", "कहिले",
    # pronouns and determiners
    "यो", "त्यो", "यी", "ती", "यस", "यसले",
    # copulas, auxiliaries and light verbs
    "हो", "हुन्", "छ", "छन्", "छैन", "हुन्छ", "हुने", "भएको", "पर्छ", "पर्ने",
    "सक्छ", "सकिन्छ", "लिन", "गर्न", "गर्छ", "गर्ने", "गरेको", "गरिएको",
    "गर्नुपर्छ", "गर्नुपर्ने", "भनेको", "भन्नाले",
    # Not grammar: the corpus owner's abbreviation, in dozens of English titles.
    # Counted, it tied all of them at title rank 1 on one word and dropped an
    # English question's answer from rank 2 to 16 (§9.10's corridor question).
    # The spelled-out name stays: it is how "Nepal Rastra Bank Act" is found.
    "nrb",
)


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
    # The title channel's rank for this chunk's DOCUMENT. The channel enters one
    # chunk per document (its nearest to the query), so at most one chunk of a
    # document carries a title rank. Defaulted so callers that predate the
    # channel keep constructing this unchanged.
    title_rank: int | None = None
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
    # `documents.source` ("upload"/"manual"). Only a citation reads it — it is what
    # a source's `origin` falls back to when a document is not from the NRB catalog.
    doc_source: str | None = None


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
        "ignored_terms": list(TITLE_IGNORED_TERMS),
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
            title_rank=(None if r["title_rank"] is None else int(r["title_rank"])),
            chunk_metadata=_as_dict(r["chunk_metadata"]),
            doc_metadata=_as_dict(r["doc_metadata"]),
            file_name=r["file_name"],
            file_type=r["file_type"],
            doc_source=r["doc_source"],
        )
        for r in rows
    ]

"""Source citations for a RAG turn: what the answer was grounded in.

The retrieval tool hands the model a *string*, so there is no return channel for
structured provenance. This module is that channel — a per-turn collector on a
contextvar, installed by the chat router exactly like `rag_context` and the file
sink, and read back after the loop finishes.

Two levels of granularity, deliberately:

- **Internally** we keep chunk-level records (`SourceChunk`), because the model's
  ``[N]`` markers number *passages*, not documents. Resolving a citation means
  indexing into the passage list the model was actually shown.
- **Externally** we publish document-level sources, deduplicated, with the cited
  page numbers aggregated. A user wants one link per document, not one per
  passage.

`download_url` is NEVER stored. It is derived at serialization time from
`department_code` + `document_id`, so persisted rows survive a change to the
route. `departments.code` is immutable (PATCH updates only name/is_active),
which is what makes storing the code safe in the first place.

Streaming gotcha, inherited from the sibling contextvars: the collector must be
installed INSIDE the async generator Starlette iterates. The router additionally
constructs the collector *before* its `try`, so the `finally` that persists the
assistant row can still read it after the contextvar has been reset.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# The vocabulary of a machine-recovered citation, defined ONCE and read twice:
# `search_department_docs` renders it into the model's context, and the chat API
# publishes it as `verify_note`. Two copies of this sentence would drift, and a UI
# badge disagreeing with the answer text is worse than neither — the reader cannot
# tell which to believe. `test_the_caveat_is_one_constant_with_two_readers` locks it.
#
# Why the caveat exists at all: OCR output is explicitly `authoritative: false`
# (§16.6), a legacy-font conversion is still `awaiting_nepali_review` (§15), and
# even a native text layer can be codepoint-corrupt (§17.6). The route is how a
# reader knows which of those they are looking at.
NRB_ORIGIN = "nrb"
RECOVERED_ROUTES = frozenset({"ocr", "legacy_conversion"})
VERIFY_NOTE = "machine-recovered — VERIFY figures, dates and names against the source"

# ``[12]`` — the marker the retrieval tool tells the model to cite with. Bounded
# to three digits so a stray "[2024]" in document text is not read as a citation
# into a passage list that never has thousands of entries.
_CITATION = re.compile(r"\[(\d{1,3})\]")


@dataclass(frozen=True)
class SourceChunk:
    """One retrieved passage, as much of it as a citation needs.

    Deliberately NOT `retrieval.RetrievedChunk`: this module stays free of the
    database import so citation resolution can be unit-tested without Postgres.
    """

    document_id: str
    title: str
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    page_number: Optional[int] = None
    # Provenance, carried opaquely from the chunk's and the document's metadata.
    # `origin` is "nrb" for a catalog document, else the document's own source
    # ("upload"/"manual"). The rest are NRB-only and stay None elsewhere:
    # `route`/`authoritative` are the CHUNK's (NRB routes per page, §16) while
    # `source_url`/`published_at` are the DOCUMENT's.
    origin: Optional[str] = None
    route: Optional[str] = None
    authoritative: Optional[bool] = None
    source_url: Optional[str] = None
    published_at: Optional[str] = None


@dataclass
class SearchRecord:
    """One `search_department_docs` call and the passages it actually showed.

    `chunks` is the *presented* list — what survived the tool's character budget
    — not everything retrieval returned. The distinction matters: a passage that
    was trimmed away was never in the model's context, so citing it as a source
    would be a fabrication.
    """

    department_code: str
    chunks: list[SourceChunk]


@dataclass
class SourceCollector:
    """Accumulates the searches performed during one turn."""

    records: list[SearchRecord] = field(default_factory=list)

    def record(self, department_code: str, chunks: list[SourceChunk]) -> None:
        """Record one search, INCLUDING one that presented nothing.

        An empty record is not noise — it is the difference between "a corpus was
        searched and held nothing relevant" (`sources: []`) and "no corpus was
        searched at all" (`sources: null`). `resolve_sources` renders those
        differently, and an abstention is exactly the first case. Dropping empty
        records collapsed the two and made abstention look like a general chat.
        """
        self.records.append(
            SearchRecord(department_code=department_code, chunks=list(chunks))
        )


_current: ContextVar[Optional[SourceCollector]] = ContextVar(
    "source_collector", default=None
)


@contextmanager
def source_scope(collector: SourceCollector) -> Iterator[SourceCollector]:
    """Install `collector` for the enclosed block.

    Takes the collector rather than creating one so the caller keeps a reference
    that outlives the scope — see the module docstring on the streaming `finally`.
    """
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def record_search(department_code: str, chunks: list[SourceChunk]) -> None:
    """Record a search's presented passages, if a collector is installed.

    A no-op outside a turn (direct tool tests, future non-chat callers) so the
    retrieval tool never has to care whether anyone is listening.
    """
    collector = _current.get()
    if collector is not None:
        collector.record(department_code, chunks)


def _cited_indices(answer: str, count: int) -> list[int]:
    """1-based ``[N]`` markers in `answer` that address a real passage.

    Out-of-range markers are dropped rather than treated as an error: the model
    inventing "[9]" over a 5-passage result is a model bug we cannot fix here,
    and the remaining valid citations are still worth showing.
    """
    seen: list[int] = []
    for raw in _CITATION.findall(answer or ""):
        index = int(raw)
        if 1 <= index <= count and index not in seen:
            seen.append(index)
    return seen


def _document_sources(
    chunks: list[SourceChunk], *, department_code: str, cited: bool
) -> list[dict[str, Any]]:
    """Collapse passages to one entry per document, aggregating page numbers and
    (for NRB) the extraction routes behind them.

    First-seen order is preserved, so the most relevant document (retrieval is
    returned best-first) leads the list.

    The NRB keys are ABSENT rather than null on an ordinary upload, so a client can
    tell "not an NRB document" from "NRB, route unknown".
    """
    by_document: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        entry = by_document.get(chunk.document_id)
        if entry is None:
            entry = {
                "document_id": chunk.document_id,
                "title": chunk.title,
                "department_code": department_code,
                "file_name": chunk.file_name,
                "file_type": chunk.file_type,
                "pages": [],
                "cited": cited,
                "origin": chunk.origin,
            }
            if chunk.origin == NRB_ORIGIN:
                entry["source_url"] = chunk.source_url
                entry["published_at"] = chunk.published_at
                entry["routes"] = []
                entry["machine_recovered"] = False
                entry["verify_note"] = None
            by_document[chunk.document_id] = entry
        if chunk.page_number is not None and chunk.page_number not in entry["pages"]:
            entry["pages"].append(chunk.page_number)
        if chunk.origin == NRB_ORIGIN:
            # An NRB PDF is routed per PAGE (§16), so one document can mix native
            # text with a converted or an OCR'd page. Report the union: naming only
            # the first route would hide the recovered page, which is precisely the
            # page a reader has to verify.
            if chunk.route and chunk.route not in entry["routes"]:
                entry["routes"].append(chunk.route)
            if chunk.route in RECOVERED_ROUTES or chunk.authoritative is False:
                entry["machine_recovered"] = True

    for entry in by_document.values():
        entry["pages"].sort()
        if "routes" in entry:
            entry["routes"].sort()
        if entry.get("machine_recovered"):
            entry["verify_note"] = VERIFY_NOTE
    return list(by_document.values())


def resolve_sources(
    records: list[SearchRecord], answer: str
) -> Optional[list[dict[str, Any]]]:
    """Turn a turn's searches + final answer into publishable sources.

    - **No search ran** -> None. A turn that never touched the corpus has no
      sources, and `null` says that more clearly than `[]`.
    - **One search** -> map the model's ``[N]`` markers onto that call's passage
      list. Those documents are `cited: true`. If nothing parseable was cited,
      fall back to every presented document with `cited: false`, because the
      answer was still grounded in them even though the model failed to mark it.
    - **Several searches that PRESENTED passages** -> every presented document,
      `cited: false`. Each call restarts numbering at ``[1]``, so a marker is
      genuinely ambiguous across calls; guessing would attach confident-looking
      links to the wrong file.

    Records that presented NOTHING (an abstention, or a search with no hits) count
    only as the "a corpus was searched" signal. They are excluded from the
    one-vs-several decision: an abstaining search alongside one real hit still has
    exactly one passage list, so ``[N]`` is unambiguous and must still resolve.
    Counting the empty record would silently downgrade every document to
    `cited: false` — a citation-precision regression with no visible symptom.
    """
    if not records:
        return None

    presented = [r for r in records if r.chunks]
    if not presented:
        # Searched, and nothing survived: `[]`, which is not `None`.
        return []

    if len(presented) == 1:
        record = presented[0]
        indices = _cited_indices(answer, len(record.chunks))
        if indices:
            cited_chunks = [record.chunks[i - 1] for i in indices]
            return _document_sources(
                cited_chunks, department_code=record.department_code, cited=True
            )
        return _document_sources(
            record.chunks, department_code=record.department_code, cited=False
        )

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in presented:
        for entry in _document_sources(
            record.chunks, department_code=record.department_code, cited=False
        ):
            if entry["document_id"] not in seen:
                seen.add(entry["document_id"])
                out.append(entry)
    return out


def download_url_for(department_code: str, document_id: str) -> str:
    """The relative download path for a source.

    Relative on purpose: the frontend must fetch it with the Authorization
    header and build a blob URL (an `<a href>` cannot send a bearer token), so
    an absolute origin would buy nothing and hard-code the deployment host.
    """
    return f"/v1/departments/{department_code}/documents/{document_id}/download"


def with_download_urls(
    sources: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    """Add the derived `download_url` to stored sources on the way out.

    Called on every read path (live turn and history replay) because the field
    is computed, never persisted.
    """
    if sources is None:
        return None
    out = []
    for source in sources:
        code = source.get("department_code")
        document_id = source.get("document_id")
        enriched = dict(source)
        enriched["download_url"] = (
            download_url_for(code, document_id) if code and document_id else None
        )
        out.append(enriched)
    return out

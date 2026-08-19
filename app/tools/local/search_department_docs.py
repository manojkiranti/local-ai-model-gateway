"""Local tool: search_department_docs — hybrid search over the ACTIVE department.

**There is no `department` parameter, deliberately.** The department comes from
`rag_context`, installed by the chat router from the authenticated user and the
session's binding. The model has nowhere to put a department, so a prompt
injection has nothing to target — the same reasoning that keeps file ownership
out of the file tools' arguments.

Output is budgeted below the agent loop's `MAX_TOOL_RESULT_CHARS` (8000). That
cap slices a long result mid-line, which would sever a citation header and leave
the model quoting a page number that belongs to a different document. So the
tool trims its own passage bodies, keeps every header intact, and says what it
trimmed.
"""

from __future__ import annotations

from typing import Any

from ...config import get_settings
from ...ollama.client import OllamaClient, OllamaError
from ...rag.context import current_department
from ...rag.embedding import EmbeddingError, embed_texts
from ...rag.retrieval import RetrievedChunk, search_chunks
from ...rag.sources import SourceChunk, record_search
from .base import LocalToolSpec

NO_DEPARTMENT = (
    "ERROR: no department is active for this conversation. This is a general "
    "chat, so there is no department knowledge base to search. Answer from the "
    "conversation itself, or tell the user to start a new chat from a department "
    "tab (HR, IT, Finance, …) if they want documents searched."
)

MIN_BODY_CHARS = 200  # never trim a passage below this; drop the passage instead


def _clamp_top_k(raw: Any, default: int, ceiling: int) -> int:
    """JSON Schema bounds are advisory — the model can emit anything, including
    a string or 100000. Coerce and clamp here regardless of what it sent."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, ceiling))


# Routes whose text was reconstructed by a machine, not read from a trustworthy
# text layer. Their figures, dates and names must never be quoted as fact — OCR
# is explicitly `authoritative: false` (§16.6) and a legacy-font conversion is
# still unverified by a Nepali reader (§15). The caveat rides on the citation so
# the model sees it exactly where it would quote the passage.
_RECOVERED_ROUTES = {"ocr", "legacy_conversion"}
_VERIFY = "machine-recovered — VERIFY figures, dates and names against the source"


def _nrb_provenance(chunk: RetrievedChunk) -> str:
    """Extra citation lines for an NRB-origin chunk. Empty for anything else.

    Additive by design: a generic upload's citation is untouched. The route and
    trust caveat come from the CHUNK's metadata (per page), the source URL and
    published date from the DOCUMENT's — both carried opaquely through retrieval.
    """
    cm = chunk.chunk_metadata or {}
    if cm.get("origin") != "nrb":
        return ""
    lines: list[str] = []
    route = cm.get("route")
    if route:
        recovered = route in _RECOVERED_ROUTES or cm.get("authoritative") is False
        lines.append(f"route: {route}" + (f" — {_VERIFY}" if recovered else ""))
    dm = chunk.doc_metadata or {}
    source = dm.get("page_url") or dm.get("source_url")
    published = dm.get("published_at")
    if source:
        lines.append(f"source: {source}" + (f" (published {published})" if published else ""))
    elif published:
        lines.append(f"published {published}")
    return ("\n    " + "\n    ".join(lines)) if lines else ""


def _header(index: int, chunk: RetrievedChunk) -> str:
    bits = [f'[{index}] "{chunk.title}"']
    if chunk.page_number is not None:
        bits.append(f"page {chunk.page_number}")
    if chunk.section:
        bits.append(chunk.section)
    bits.append(f"doc={chunk.document_id}")
    # NRB provenance lines are part of the HEADER so the budget machinery reserves
    # them whole and can never sever them (the same reason the title/page live
    # here): a trimmed passage stays attributable, a trimmed caveat does not.
    return " — ".join(bits) + _nrb_provenance(chunk)


JOIN = "\n\n"
TRIM_SUFFIX = " …[passage trimmed]"
TRIM_NOTE = (
    "[Some passages were trimmed to fit. Ask a narrower question, or lower "
    "top_k, to see fuller text.]"
)


def _format(
    chunks: list[RetrievedChunk], *, department_code: str, budget: int
) -> tuple[str, list[RetrievedChunk]]:
    """Serialize results, keeping every citation header and trimming bodies.

    Headers are reserved before bodies because they are the citable part: a
    trimmed passage is still attributable, a trimmed header is not. The budget
    accounting is exact — intro, the trim note, every join separator and every
    trim suffix are all reserved up front — because the agent loop's own cut at
    MAX_TOOL_RESULT_CHARS makes no such distinction and would sever a header.

    Returns the text AND the passages that survived into it. Both drop paths
    below discard from the END, so the surviving prefix keeps its ``[1..k]``
    numbering. Citations are resolved against exactly this list: a passage the
    budget removed was never in the model's context, so listing its document as
    a source would invent provenance.
    """
    intro = (
        f"{len(chunks)} passage(s) from the {department_code} department's "
        f"documents, most relevant first. Answer ONLY from these passages and "
        f"cite the bracketed number and document title you used."
    )
    headers = [_header(i, c) for i, c in enumerate(chunks, start=1)]

    def _fixed_cost(hs: list[str]) -> int:
        # intro + note + one JOIN before each entry and before the note,
        # plus each header and the newline separating it from its body.
        return (
            len(intro)
            + len(TRIM_NOTE)
            + len(JOIN) * (len(hs) + 1)
            + sum(len(h) + 1 for h in hs)
        )

    # Drop whole entries rather than shredding all of them into fragments too
    # short to be useful.
    while len(chunks) > 1 and (
        budget - _fixed_cost(headers) < MIN_BODY_CHARS * len(chunks)
    ):
        chunks, headers = chunks[:-1], headers[:-1]

    share = max(
        MIN_BODY_CHARS,
        (budget - _fixed_cost(headers)) // max(1, len(chunks)),
    )

    trimmed_any = len(chunks) < len(headers) or False
    parts = [intro]
    for header, chunk in zip(headers, chunks):
        body = chunk.content.strip()
        if len(body) > share:
            body = body[: max(0, share - len(TRIM_SUFFIX))].rstrip() + TRIM_SUFFIX
            trimmed_any = True
        parts.append(f"{header}\n{body}")

    if trimmed_any:
        parts.append(TRIM_NOTE)
    out = JOIN.join(parts)

    # Backstop: if anything still overshoots, drop whole entries from the end.
    # Never a raw slice — that is precisely the header-severing failure this
    # function exists to prevent.
    while len(out) > budget and len(parts) > 2:
        parts = parts[:-2] + [TRIM_NOTE] if parts[-1] == TRIM_NOTE else parts[:-1]
        out = JOIN.join(parts)

    # parts = [intro, entry, entry, …] with an optional TRIM_NOTE last.
    entry_count = len(parts) - 1 - (1 if parts and parts[-1] == TRIM_NOTE else 0)
    return out, chunks[: max(0, entry_count)]


async def _search_department_docs(args: dict[str, Any]) -> str:
    department = current_department()
    if department is None:
        return NO_DEPARTMENT

    settings = get_settings()
    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: 'query' is required and must be a non-empty string."
    # Clamp before embedding: an enormous query is wasted GPU and a DoS vector.
    query = query[: settings.rag_max_query_chars]
    top_k = _clamp_top_k(args.get("top_k"), settings.rag_top_k, settings.rag_top_k)

    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
    try:
        vectors = await embed_texts(
            client,
            [query],
            mode="query",  # asymmetric: queries carry the instruction prefix
            model=settings.rag_embed_model,
            dim=settings.rag_embed_dim,
            batch_size=1,
        )
    except (EmbeddingError, OllamaError) as exc:
        return f"ERROR: could not embed the query ({exc})."
    finally:
        await client.aclose()

    chunks = await search_chunks(
        department_id=department.id,
        query_text=query,
        query_vector=vectors[0],
        limit=top_k,
        candidate_pool=settings.rag_candidate_pool,
        rrf_k=settings.rag_rrf_k,
        ef_search=settings.rag_hnsw_ef_search,
    )

    if not chunks:
        # Explicit, not an empty list: an empty result reads to the model as an
        # unremarkable outcome and invites an answer from its own parameters.
        return (
            f"No matching passages were found in the {department.code} "
            f"department's documents. Tell the user you could not find this in "
            f"the {department.code} documents. Do NOT answer from general "
            f"knowledge."
        )

    text, presented = _format(
        chunks,
        department_code=department.code,
        budget=settings.rag_tool_result_max_chars,
    )
    # Structured provenance for the turn's `sources`. The tool's own return value
    # is a string with nowhere to put it, so it goes out of band on a contextvar
    # — a no-op when nobody installed a collector.
    record_search(
        department.code,
        [
            SourceChunk(
                document_id=c.document_id,
                title=c.title,
                file_name=c.file_name,
                file_type=c.file_type,
                page_number=c.page_number,
            )
            for c in presented
        ],
    )
    return text


SPEC = LocalToolSpec(
    name="search_department_docs",
    description=(
        "Search the CURRENT department's official documents (policies, circulars, "
        "product sheets, spreadsheets) and return the most relevant passages with "
        "their document title and page number. Use this for any question about "
        "company policy, process, entitlements, products or internal rules — the "
        "answer must come from these documents, not from general knowledge. The "
        "department is fixed by the conversation; you cannot and need not choose "
        "it. For totals over a spreadsheet the USER attached to this chat, use "
        "aggregate_excel instead — corpus spreadsheets are searchable as text "
        "here but not aggregatable."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 1000,
                "description": "What to look for, in natural language.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "How many passages to return (default 12).",
            },
        },
        "required": ["query"],
    },
    func=_search_department_docs,
)

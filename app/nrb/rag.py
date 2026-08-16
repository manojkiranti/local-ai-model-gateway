"""The NRB → RAG ingestion boundary: one blob in, indexable chunks out.

**This is the only place where NRB recovery meets department RAG**, and it is
deliberately one function. `app/rag/parsing.py` keeps its generic contract
untouched — a non-NRB document parses exactly as it did — and the worker reaches
this through a single explicit branch on `documents.metadata.origin == "nrb"`.

WHAT IT DOES, AND WHY IN THIS ORDER
    classify (native-2) → route per page (`recovery`) → chunk per page

    The classification is re-run rather than read from `nrb_extractions`,
    because that table lives in the NRB catalog and a chunk must be a function
    of the BYTES on disk, not of a row that may have been written by an older
    extractor version. It is the same pypdf parse the corpus pass already
    measured, and it is cheap next to embedding.

TWO RULES THIS FILE ENFORCES
    **1. Only trustworthy text is indexed.** `PageText.indexable` is the whole
    filter: a page whose conversion was unresolved, whose OCR failed, or whose
    converter was missing contributes NOTHING. There is no "index it anyway with
    a warning" path — the failure mode that must not exist is a citation showing
    `g]kfn /fi6« a}+s` as the text of a directive.

    **2. A chunk never spans two pages.** Page identity is the citation, so
    chunking runs per page and `page_number` is set on every chunk. A chunk that
    merged page 3 and page 4 could not be cited at all.

WHAT IT DOES NOT DO
    No route-based ranking. The route travels as chunk metadata — provenance and
    a quality caveat, not a calibrated relevance penalty — and OCR'd and
    converted chunks enter retrieval on identical terms. Weighting them would be
    inventing a number this evidence cannot support.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

from ..rag.chunking import Chunk, chunk_text, renumber
from . import extraction, recovery, sniff

logger = logging.getLogger("app.nrb.rag")

__all__ = [
    "NRB_ORIGIN",
    "NrbParseError",
    "default_lexicon",
    "nrb_dependencies",
    "parse_nrb_to_chunks",
    "recover_blob",
    "reset_dependencies",
]

# The marker on `documents.metadata` that sends a document down this path.
# Explicit rather than inferred from a filename or a department: a department
# may hold both ordinary uploads and NRB corpus documents, and guessing would
# change how an ordinary PDF is parsed.
NRB_ORIGIN = "nrb"

# The frozen vocabulary the conversion guards read. Committed, fingerprinted,
# and the same one the Phase 6B evaluation used — a different lexicon would make
# `is_confidently_english` a different veto and the recovered text a different
# thing from what was measured.
LEXICON_PATH = "docs/nrb/phase6b-lexicon.json"

# The mapping Phase 6B validated on this corpus. Kantipur and Sagarmatha agree
# with it almost everywhere; FONTASY and PCS NEPALI are wrong on every document
# reviewed (§12). Named, never auto-selected: mapping identity is a claim about
# the document, and quietly trying a second one would make the record unreadable.
MAPPING = "Preeti"


class NrbParseError(Exception):
    """Nothing indexable came out of this blob.

    Deliberately distinct from `rag.parsing.ParseError` at the point of raise so
    the reason can name the routing outcome; the worker treats it the same way
    (a failed job, the document's previous chunks untouched).
    """


def default_lexicon():
    """The frozen lexicon, or None if it is not on disk.

    None degrades to "no conversion" rather than to "convert without guards".
    The guards are what stop an English table being rewritten as Devanagari
    (§12.2), so running the converter without them is worse than not running it.
    """
    from . import lexicon as lexicon_mod

    try:
        return lexicon_mod.load_lexicon(LEXICON_PATH)
    except Exception as exc:  # noqa: BLE001 - missing/unreadable is not fatal
        logger.warning("NRB rag: lexicon unavailable (%s)", type(exc).__name__)
        return None


@functools.lru_cache(maxsize=1)
def nrb_dependencies() -> tuple[Any, Any, Any]:
    """`(converter, lexicon, ocr_engine)`, built once per PROCESS.

    Cached because both ends are expensive and the worker parses documents in a
    loop: `FontMapper` re-reads a 34 KB rule file per construction, and the OCR
    converter loads three ONNX models. The worker handles one job at a time, so
    a single shared engine is the right shape; `reset_dependencies()` exists for
    tests, which must not inherit a previous test's stubs.

    Any of the three may be None. That is a supported state, not a failure:
    npttf2utf is GPL-3 and a deployment may legitimately omit it, and the OCR
    stack is worker-side only. Missing dependencies produce **recorded
    unresolved pages**, never untrusted text — see `recovery`'s fail-closed
    rules.
    """
    converter = None
    lexicon = default_lexicon()
    ocr_engine = None

    if lexicon is not None:
        try:
            from .legacy_font import converter_for

            converter = converter_for(MAPPING)
        except Exception as exc:  # noqa: BLE001 - absence is expected, not fatal
            logger.warning(
                "NRB rag: legacy converter unavailable (%s) — legacy pages will "
                "be recorded unresolved, not indexed", exc,
            )

    try:
        from .ocr import DoclingRapidOcrEngine

        engine = DoclingRapidOcrEngine()
        ok, evidence = engine.open()
        if ok:
            ocr_engine = engine
            logger.info("NRB rag: OCR ready — %s", evidence)
        else:
            logger.warning("NRB rag: OCR unavailable (%s)", evidence)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NRB rag: OCR unavailable (%s)", type(exc).__name__)

    return converter, lexicon, ocr_engine


def reset_dependencies() -> None:
    """Drop the cached converter/lexicon/OCR engine."""
    nrb_dependencies.cache_clear()


def recover_blob(
    path: Path, *, extractor_version: str = "native-2", **injected: Any
) -> tuple[extraction.ExtractionResult, recovery.RecoveredDocument]:
    """Classify and route one blob. `injected` overrides the cached dependencies.

    The family comes from our OWN magic-byte sniff, never from the filename —
    the same rule Phase 5 applies to a download, for the same reason: the
    claimed type is what we are checking.
    """
    path = Path(path)
    with path.open("rb") as handle:
        head = handle.read(4096)
    family = sniff.family_for(sniff.sniff(head)[0])
    result = extraction.extract_file(
        path,
        family=family,
        extension=path.suffix.lstrip("."),
        extractor_version=extractor_version,
    )
    if injected:
        converter = injected.get("converter")
        lexicon = injected.get("lexicon")
        ocr = injected.get("ocr")
    else:
        converter, lexicon, ocr = nrb_dependencies()
    recovered = recovery.recover(
        path, result, converter=converter, lexicon=lexicon, ocr=ocr
    )
    return result, recovered


def _chunk_meta(page: recovery.PageText, extractor_version: str) -> dict[str, Any]:
    """The provenance that rides with every chunk into `document_chunks.metadata`.

    Small on purpose — it is stored once per chunk. It answers "where did this
    text come from and how much should a reader trust it", which is what a
    citation needs; the full per-page record (dispositions, counts, errors) stays
    in the recovery result and in the ingest report.
    """
    meta: dict[str, Any] = {
        "origin": NRB_ORIGIN,
        "route": page.route,
        "route_reason": page.reason,
        "extractor_version": extractor_version,
    }
    detail = page.detail or {}
    if page.route == recovery.ROUTE_LEGACY:
        meta["converter"] = detail.get("converter")
        meta["mapping"] = detail.get("mapping")
        meta["converted_units"] = detail.get("converted_units")
        meta["unresolved_units"] = detail.get("unresolved_units")
    elif page.route == recovery.ROUTE_OCR:
        meta["ocr_engine"] = detail.get("engine")
        meta["ocr_model"] = detail.get("model")
        meta["ocr_version"] = detail.get("version")
        # Carried to the chunk, not just to the log: OCR text is retrieval
        # material and must never be quoted as an authoritative figure, date or
        # contact detail on a degraded scan (§16.6).
        meta["authoritative"] = False
    return {k: v for k, v in meta.items() if v is not None}


def chunks_from_recovery(
    recovered: recovery.RecoveredDocument,
    *,
    max_chars: int,
    overlap_chars: int,
    extractor_version: str = "native-2",
) -> list[Chunk]:
    """Indexable pages → chunks, one page at a time. Pure.

    Per page, never across pages: `page_number` is the citation and a chunk
    spanning two pages could not carry one. The generic `chunk_text` does the
    splitting — the paragraph/sentence/word boundary logic is shared with
    department RAG rather than reimplemented here.
    """
    chunks: list[Chunk] = []
    for page in recovered.indexable_pages:
        is_sheet = page.label is not None
        produced = chunk_text(
            page.text,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            section=page.label,
            page_number=page.page_number,
            element_type="table" if is_sheet else "text",
        )
        meta = _chunk_meta(page, extractor_version)
        chunks.extend(
            Chunk(
                content=chunk.content,
                chunk_index=chunk.chunk_index,
                page_number=page.page_number,
                section=chunk.section,
                element_type=chunk.element_type,
                token_count=chunk.token_count,
                meta=meta,
            )
            for chunk in produced
        )
    return renumber(chunks)


def parse_nrb_to_chunks(
    path: Path,
    *,
    max_chars: int,
    overlap_chars: int,
    extractor_version: str = "native-2",
    **injected: Any,
) -> list[Chunk]:
    """One NRB blob → chunks. THE function the worker calls.

    Raises `NrbParseError` when nothing indexable came out, with the routing
    outcome in the message: "0 of 4 pages indexable (legacy_conversion 4
    unresolved)" is an actionable report, "no indexable content" is not. A blob
    whose conversion could not run is a recorded gap — never a document indexed
    with its glyph-mapped original.
    """
    result, recovered = recover_blob(
        path, extractor_version=extractor_version, **injected
    )
    chunks = chunks_from_recovery(
        recovered,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        extractor_version=extractor_version,
    )
    if not chunks:
        failures = {
            page.reason for page in recovered.pages if not page.indexable
        }
        raise NrbParseError(
            f"no indexable text: {result.status}/{result.reason}, plan "
            f"{recovered.plan}, {len(recovered.pages)} pages, routes "
            f"{recovered.route_counts}, unindexable because "
            f"{sorted(failures) or ['empty']}"
        )
    return chunks

"""Parsing a corpus document into chunks.

Format split, and why:

- **pdf / docx -> Docling, walking `iterate_items()`.** Layout analysis and
  table-structure recognition are the difference between a usable and a useless
  PDF chunk. We iterate items rather than dumping `export_to_markdown()`,
  because the dump discards exactly what slice-3 citations need: the real
  `page_no` from `item.prov`, the heading path, and the element label.
- **xlsx / csv -> `app/files/readers.py`.** One spreadsheet normalizer is shared
  with `read_excel`/`aggregate_excel`; a second would diverge from the tools that
  already read spreadsheets here, and Docling buys nothing on a plain grid. Uses
  `open_sheet_rows` (uncapped streaming), NOT `load_table` (~200-row window).
- **text / md -> straight to the prose chunker.**

**Docling is imported lazily, inside the branch that needs it.** The API process
must never load torch; a stray module-scope import would drag ~90 packages into
the API image. `tests/test_rag_parsing.py` asserts this.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from ..files import readers
from .chunking import (
    Block,
    Chunk,
    chunk_table,
    chunk_text,
    drop_small_blocks,
    merge_blocks,
    renumber,
)


class ParseError(Exception):
    """The file could not be turned into at least one chunk."""


SUPPORTED_FILE_TYPES = frozenset({"pdf", "docx", "xlsx", "csv", "text"})

_EXT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
}

# Docling's label vocabulary -> our four element_type values.
_ELEMENT_TYPES = {
    "section_header": "heading",
    "title": "heading",
    "page_header": "heading",
    "table": "table",
    "list_item": "list",
}


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    file_type = _EXT_MAP.get(ext)
    if file_type is None:
        raise ParseError(
            f"unsupported file type {ext or '(none)'}; "
            f"supported: {', '.join(sorted(_EXT_MAP))}"
        )
    return file_type


def parse_text_to_chunks(
    text: str, *, max_chars: int, overlap_chars: int
) -> list[Chunk]:
    chunks = chunk_text(text, max_chars=max_chars, overlap_chars=overlap_chars)
    if not chunks:
        raise ParseError("no text content to index")
    return chunks


def _parse_spreadsheet(path: Path, *, max_chars: int) -> list[Chunk]:
    """Every sheet, every row. Header repeats into each chunk (see chunk_table)."""
    try:
        sheets = [s.sheet_name for s in readers.inspect_workbook(path)]
    except readers.ReadError as exc:
        raise ParseError(str(exc)) from exc

    collected: list[Chunk] = []
    for sheet in sheets or [None]:
        try:
            with readers.open_sheet_rows(path, sheet=sheet) as stream:
                rows = [row for row in stream.rows if any(str(c).strip() for c in row)]
                collected.extend(
                    chunk_table(
                        stream.headers,
                        rows,
                        sheet_name=stream.sheet_name,
                        max_chars=max_chars,
                    )
                )
        except readers.ReadError as exc:
            raise ParseError(str(exc)) from exc
    return renumber(collected)


def _heading_path(stack: list[tuple[int, str]]) -> str | None:
    return " > ".join(text for _level, text in stack) if stack else None


def _normalize_heading(text: str) -> str:
    """Casefold, collapse internal whitespace, drop trailing punctuation."""
    return re.sub(r"\s+", " ", text).strip().strip(".:;—-").strip().casefold()


def _is_skipped_section(section: str | None, skip: set[str]) -> bool:
    """True when a chunk's heading path starts with front matter.

    Matches the FIRST segment only, deliberately: that catches
    "Table of Contents" and "Table of Contents > 5.2.5 …" while leaving a
    legitimate "Chapter 3 > Index of Limits" indexed. Matching any segment
    would delete exactly the content most worth keeping in a policy document.

    Both the configured skip entries and the heading are normalized via
    _normalize_heading, so irregular formatting (trailing punctuation, doubled
    spaces) in the config list does not cause silent non-matches.
    """
    if not section or not skip:
        return False
    normalized_heading = _normalize_heading(section.split(" > ", 1)[0])
    normalized_skip = {_normalize_heading(s) for s in skip}
    return normalized_heading in normalized_skip


def _with_context(chunks: list[Chunk], section: str | None) -> list[Chunk]:
    """Prepend the heading path to each chunk's CONTENT, not just its metadata.

    `tsv` is generated from `content` alone, so a heading kept only in the
    `section` column would be invisible to the lexical channel — a query for
    "carry over" would miss the section actually titled "Carry Over". This is the
    same reasoning that repeats the header row into every table chunk.
    """
    if not section:
        return chunks
    return [replace(c, content=f"{section}\n\n{c.content}") for c in chunks]


def _pdf_pipeline_options():
    """PDF pipeline pinned to CPU with OCR off. Imports live HERE, never at
    module scope — torch must not load in the API process.

    device=CPU: ingestion must never touch the GPU. The GPU belongs to the LLM
    (Ollama); Docling's default AUTO grabs CUDA and, on a shared card, collides
    with the resident model — the CUDA OOM that failed every parse. The worker
    image already ships CPU-only torch for this reason; pinning the device makes
    it true regardless of which torch build a given venv happens to have.

    do_ocr=False: v1 does not OCR (see the "produced no text" ParseError below),
    and OCR is by far the heaviest, slowest stage. Digital PDFs extract from
    their embedded text layer without it; a scanned/image-only PDF still lands
    on the graceful no-text path rather than burning minutes on CPU OCR.
    """
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.settings import settings as docling_settings

    # No torch.compile. Docling compiles the layout model by default, and
    # TorchInductor shells out to a C++ compiler at RUNTIME to build the
    # generated kernels — `python:*-slim` has no g++ (build-essential lives only
    # in the builder stage, by design), so the layout stage died with
    # `InvalidCxxCompiler: No working C++ compiler found`. Eager mode needs no
    # toolchain, and for a background CPU ingest the compile is not worth
    # shipping a compiler into the runtime image for. MUST be set before the
    # options are built: `compile_model` resolves from this via a
    # default_factory when PdfPipelineOptions() is constructed.
    docling_settings.inference.compile_torch_models = False

    options = PdfPipelineOptions()

    options.do_ocr = False
    options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU)
    return options


def _docling_converter():
    """A DocumentConverter with the CPU/no-OCR PDF pipeline. Only the PDF format
    is overridden; DOCX uses Docling's default pipeline (no OCR, no GPU)."""
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=_pdf_pipeline_options())
        }
    )


def _parse_with_docling(
    path: Path,
    *,
    max_chars: int,
    overlap_chars: int,
    min_body_chars: int = 0,
    skip_sections: set[str] | None = None,
) -> list[Chunk]:
    """PDF/DOCX via Docling, PRESERVING provenance. Imported HERE, never at
    module scope.

    Walks `iterate_items()` rather than dumping `export_to_markdown()`, because
    the markdown dump throws away exactly what slice-3 citations need: the real
    `page_no` from `item.prov`, the heading path, and the element label.
    Verified against docling 2.118: `iterate_items()` yields `(item, level)`,
    `item.prov[0].page_no` is 1-based, and `item.label` is a `DocItemLabel`.

    Four phases, in this order:

    1. **Collect** — walk `iterate_items()` into `Block`s, tracking the heading
       stack for `section` and dropping anything under a `skip_sections` entry
       (e.g. a Table of Contents) before it ever becomes a `Block`.
    2. **Merge** (`merge_blocks`) — join consecutive blocks that share a
       section/page into passages, so `max_chars` is a target to fill, not a
       per-element ceiling.
    3. **Filter** (`drop_small_blocks`, tables exempt) — drop what merging
       revealed as genuine layout debris (`min_body_chars`), now that merging
       has had the chance to rescue anything that was only orphaned.
    4. **Chunk** (`chunk_text` per merged block) — split any still-oversized
       passage and re-attach the heading path to chunk content.

    Two distinct `ParseError`s, both preserved so an admin can tell the
    failure modes apart:

    - Zero blocks survive collection *and* none were skipped as front matter
      -> a scanned PDF with no extractable text at all (`"a scanned PDF needs
      OCR"`).
    - Zero blocks survive collection but at least one was skipped as front
      matter -> a document that is wholly front matter, e.g. a
      Table-of-Contents-only file (`"front matter or fragments"`). Without
      counting skips, this case collects zero blocks exactly like a scan does,
      and would otherwise be misdiagnosed as one.
    - Blocks survive collection but nothing survives merge+filter+chunk (all
      orphan fragments too small to keep) -> the same `"front matter or
      fragments"` message, for the same reason: nothing indexable came out.
    """
    try:
        converter = _docling_converter()
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ParseError(
            "Docling is not installed. PDF/DOCX ingestion runs in the WORKER "
            "environment only: pip install -r requirements-worker.txt"
        ) from exc

    try:
        document = converter.convert(str(path)).document
    except Exception as exc:  # noqa: BLE001 - Docling raises a wide range
        raise ParseError(f"could not parse document: {exc}") from exc

    headings: list[tuple[int, str]] = []
    blocks: list[Block] = []
    # Distinguishes "nothing survived collection because it's a scan" from
    # "nothing survived collection because it was all front matter" — both
    # leave `blocks` empty, and only this counter tells them apart.
    skipped_front_matter = 0

    for item, _tree_level in document.iterate_items():
        label = getattr(getattr(item, "label", None), "value", "") or ""
        prov = getattr(item, "prov", None) or []
        page = prov[0].page_no if prov else None

        if label in ("section_header", "title"):
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            level = getattr(item, "level", 1) or 1
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text))
            continue  # the heading itself is carried into following chunks

        section = _heading_path(headings)
        # Front matter never reaches the index: a Table of Contents lists every
        # heading in the document, so it matches almost any structural query,
        # and ts_rank_cd favours short text — it outranked real prose 7 slots
        # out of 12. Measured 2026-08-12; see the design spec.
        if _is_skipped_section(section, skip_sections or set()):
            skipped_front_matter += 1
            continue

        if label == "table":
            try:
                text = item.export_to_markdown(document).strip()
            except Exception:  # noqa: BLE001 - a malformed table is not fatal
                text = ""
        else:
            text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        blocks.append(
            Block(
                text=text,
                section=section,
                page_number=page,
                element_type=_ELEMENT_TYPES.get(label, "text"),
            )
        )

    if not blocks:
        # Both leave `blocks` empty, but they are not the same failure: a
        # wholly-front-matter document (e.g. a Table-of-Contents-only file)
        # skipped everything it saw, while a scanned PDF never had extractable
        # text to skip. Collapsing them would tell an admin to go find a
        # scanner for a file that never needed OCR.
        if skipped_front_matter:
            raise ParseError(
                "document contained only front matter or fragments — nothing to index"
            )
        raise ParseError(
            "document produced no text — a scanned PDF needs OCR, which v1 does not do"
        )

    # Merge BEFORE filtering: a short block is often real content orphaned from
    # its neighbours by Docling's element split, and only merging can tell the
    # difference. See drop_small_blocks.
    blocks = drop_small_blocks(
        merge_blocks(blocks, max_chars=max_chars), min_body_chars=min_body_chars
    )

    collected: list[Chunk] = []
    for block in blocks:
        pieces = chunk_text(
            block.text,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            section=block.section,
            page_number=block.page_number,
            element_type=block.element_type,
        )
        collected.extend(_with_context(pieces, block.section))

    if not collected:
        raise ParseError(
            "document contained only front matter or fragments — nothing to index"
        )
    return collected


def parse_to_chunks(
    path: Path,
    file_type: str,
    *,
    max_chars: int,
    overlap_chars: int,
    min_body_chars: int = 0,
    skip_sections: set[str] | None = None,
) -> list[Chunk]:
    """Dispatch on `file_type`, returning contiguously indexed chunks.

    `min_body_chars`/`skip_sections` apply to the Docling path ONLY — the text
    and spreadsheet branches already produce whole-body or row-buffered chunks,
    and a global filter would reduce a legitimately short .txt to zero chunks.
    """
    if file_type in ("xlsx", "csv"):
        chunks = _parse_spreadsheet(path, max_chars=max_chars)
    elif file_type in ("pdf", "docx"):
        chunks = _parse_with_docling(
            path,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            min_body_chars=min_body_chars,
            skip_sections=skip_sections,
        )
    elif file_type == "text":
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ParseError(f"could not read file: {exc}") from exc
        chunks = chunk_text(body, max_chars=max_chars, overlap_chars=overlap_chars)
    else:
        raise ParseError(f"unsupported file type {file_type!r}")

    if not chunks:
        raise ParseError("no indexable content found")
    return renumber(chunks)

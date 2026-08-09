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

from dataclasses import replace
from pathlib import Path

from ..files import readers
from .chunking import Chunk, chunk_table, chunk_text, renumber


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


def _parse_with_docling(
    path: Path, *, max_chars: int, overlap_chars: int
) -> list[Chunk]:
    """PDF/DOCX via Docling, PRESERVING provenance. Imported HERE, never at
    module scope.

    Walks `iterate_items()` rather than dumping `export_to_markdown()`, because
    the markdown dump throws away exactly what slice-3 citations need: the real
    `page_no` from `item.prov`, the heading path, and the element label.
    Verified against docling 2.118: `iterate_items()` yields `(item, level)`,
    `item.prov[0].page_no` is 1-based, and `item.label` is a `DocItemLabel`.
    """
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ParseError(
            "Docling is not installed. PDF/DOCX ingestion runs in the WORKER "
            "environment only: pip install -r requirements-worker.txt"
        ) from exc

    try:
        document = DocumentConverter().convert(str(path)).document
    except Exception as exc:  # noqa: BLE001 - Docling raises a wide range
        raise ParseError(f"could not parse document: {exc}") from exc

    collected: list[Chunk] = []
    headings: list[tuple[int, str]] = []

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

        if label == "table":
            try:
                text = item.export_to_markdown(document).strip()
            except Exception:  # noqa: BLE001 - a malformed table is not fatal
                text = ""
        else:
            text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        pieces = chunk_text(
            text,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            section=_heading_path(headings),
            page_number=page,
            element_type=_ELEMENT_TYPES.get(label, "text"),
        )
        collected.extend(_with_context(pieces, _heading_path(headings)))

    if not collected:
        raise ParseError(
            "document produced no text — a scanned PDF needs OCR, which v1 does not do"
        )
    return collected


def parse_to_chunks(
    path: Path, file_type: str, *, max_chars: int, overlap_chars: int
) -> list[Chunk]:
    """Dispatch on `file_type`, returning contiguously indexed chunks."""
    if file_type in ("xlsx", "csv"):
        chunks = _parse_spreadsheet(path, max_chars=max_chars)
    elif file_type in ("pdf", "docx"):
        chunks = _parse_with_docling(
            path, max_chars=max_chars, overlap_chars=overlap_chars
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

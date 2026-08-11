"""Normalize an uploaded document into a flat list of text LINES.

Pure module — no DB, no HTTP. The upload route (parse check + summary) and the
`read_document` tool both go through here, so every supported format behaves
identically downstream.

Design rule: this module reports FACTS and raises only when a file genuinely
cannot be parsed. It makes no policy decisions — notably a scanned PDF returns
normally with `text_pages == 0`, and the tool decides that means "no OCR".
Caps on how much reaches the model live in the tool, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .readers import ReadError

DOCUMENT_EXTS = {".pdf", ".docx", ".txt", ".md", ".json"}

# A hard bound on extraction work for one PDF. Beyond this we stop and SAY so
# (see DocumentText.pages_skipped) rather than refusing the file.
MAX_PDF_PAGES = 500


class EncryptedDocument(ReadError):
    """The file needs a password we don't have (empty password already tried)."""


@dataclass
class DocumentText:
    kind: str
    lines: list[str]
    # PDF-only; None for every other format.
    pages: Optional[int] = None
    text_pages: Optional[int] = None       # pages READ that produced text
    pages_skipped: Optional[int] = None    # pages beyond MAX_PDF_PAGES


def _decode(path: Path) -> str:
    """Bytes -> str, never raising. utf-8-sig strips a BOM when present and is
    plain utf-8 otherwise; errors='replace' means a binary file renamed .txt
    degrades to mojibake instead of crashing the reader."""
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _read_text(path: Path, ext: str) -> DocumentText:
    kind = "Markdown" if ext == ".md" else "Text file"
    return DocumentText(kind=kind, lines=_decode(path).splitlines())


def _read_json(path: Path) -> DocumentText:
    text = _decode(path)
    try:
        parsed = json.loads(text)
    except ValueError:
        # Deliberately NOT an error: near-valid JSON is still readable, and the
        # kind tells the model it is looking at raw text.
        return DocumentText(kind="JSON (unparsed)", lines=text.splitlines())
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return DocumentText(kind="JSON", lines=pretty.splitlines())


def read_lines(path: Path) -> DocumentText:
    """Any supported document -> its text as lines. Raises ReadError for an
    unsupported extension or an unparseable file."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".json":
        return _read_json(path)
    if ext in (".txt", ".md"):
        return _read_text(path, ext)
    raise ReadError(f"unsupported document type '{ext}'")

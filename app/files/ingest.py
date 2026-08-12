"""Which FAMILY is this uploaded file — spreadsheet or document?

One source of truth, so neither the upload route nor the turn-open path
branches on extension itself. Both summary types expose `.text()` and
`.as_dict()`, so callers never need to know which one they got back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import documents, readers
from .readers import ReadError
from .store import CSV_MEDIA_TYPE, DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE, XLSX_MEDIA_TYPE

SPREADSHEET_EXTS = {".xlsx", ".csv"}
DOCUMENT_EXTS = set(documents.DOCUMENT_EXTS)

# Upload allowlist: extension -> stored media type. `.xlsm` (macro-enabled) is
# deliberately absent.
UPLOAD_TYPES: dict[str, str] = {
    ".xlsx": XLSX_MEDIA_TYPE,
    ".csv": CSV_MEDIA_TYPE,
    ".pdf": PDF_MEDIA_TYPE,
    ".docx": DOCX_MEDIA_TYPE,
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
}

Summary = Union[readers.Summary, documents.DocumentSummary]


def summarize(path: Path) -> Summary:
    """Structure summary of any supported upload (raises ReadError otherwise)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in SPREADSHEET_EXTS:
        return readers.summarize(path)
    if ext in DOCUMENT_EXTS:
        return documents.summarize_document(path)
    raise ReadError(f"unsupported file type '{ext}'")

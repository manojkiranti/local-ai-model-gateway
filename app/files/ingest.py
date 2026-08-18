"""Which FAMILY is this uploaded file — spreadsheet, document or image?

One source of truth, so neither the upload route nor the turn-open path
branches on extension itself. Every summary type exposes `.text()` and
`.as_dict()`, so callers never need to know which one they got back.

Adding a family here is all it takes: `router.upload_file` and
`history/service._resolve_attachments` both go through `summarize`.
Note what a summary may NOT do — it is recomputed on every turn for every
attached file, so it stays a header read. Images are summarised by
dimensions; their TEXT comes from the `read_image` tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import documents, images, readers
from .readers import ReadError
from .store import (
    BMP_MEDIA_TYPE,
    CSV_MEDIA_TYPE,
    DOCX_MEDIA_TYPE,
    JPEG_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    PNG_MEDIA_TYPE,
    TIFF_MEDIA_TYPE,
    WEBP_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
)

SPREADSHEET_EXTS = {".xlsx", ".csv"}
DOCUMENT_EXTS = set(documents.DOCUMENT_EXTS)
IMAGE_EXTS = set(images.IMAGE_EXTS)

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
    ".png": PNG_MEDIA_TYPE,
    ".jpg": JPEG_MEDIA_TYPE,
    ".jpeg": JPEG_MEDIA_TYPE,
    ".webp": WEBP_MEDIA_TYPE,
    ".tif": TIFF_MEDIA_TYPE,
    ".tiff": TIFF_MEDIA_TYPE,
    ".bmp": BMP_MEDIA_TYPE,
}

Summary = Union[readers.Summary, documents.DocumentSummary, images.ImageSummary]


def summarize(path: Path) -> Summary:
    """Structure summary of any supported upload (raises ReadError otherwise)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in SPREADSHEET_EXTS:
        return readers.summarize(path)
    if ext in DOCUMENT_EXTS:
        return documents.summarize_document(path)
    if ext in IMAGE_EXTS:
        return images.summarize_image(path)
    raise ReadError(f"unsupported file type '{ext}'")

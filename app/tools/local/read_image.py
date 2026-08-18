"""Local tool: read_image — the text of ONE uploaded image, by OCR.

Owner-scoped by file_id (see files/source.py). Handles the raster formats the
upload route accepts (.png/.jpg/.jpeg/.webp/.tif/.tiff/.bmp); a PDF or DOCX is
routed to read_document, because those have a text layer worth reading and
handing page 1 to an OCR engine would silently discard it.

Paging is `_paging.window`, shared with read_document — metadata leads, and
truncation happens here on whole lines. Two things are specific to this tool:

  * THE CAVEAT IS PART OF THE RESULT. OCR output is retrieval text, not a
    transcription (docs/nrb-integration.md §16.6: PP-OCRv5 drops letterheads and
    subject lines, mangles latin runs, and renders २०६९।१।३१ as २०६९।९।३१). The
    model is the consumer, so the warning has to reach the model — a note in the
    docs would not. It sits in the HEADER for the same reason the totals do:
    agent/loop.py truncates from the END, so a trailing caveat is the first
    thing lost on exactly the long results that most need it.
  * CONFIDENCE IS REPORTED, NEVER ENFORCED. Per-line scores are information;
    §16.6 declines to invent a pass/fail threshold from an orthography
    measurement, so nothing here compares a score to a constant.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from ...files import image_ocr, images, ingest, readers
from ...files.store import resolve_file
from ._paging import MODEL_RESULT_CAP, window
from .base import LocalToolSpec

READ_IMAGE_MAX_LINES = 400

# Bigger header budget than read_document's: this one also carries the caveat
# and the engine/confidence line, and both must survive the loop's end-cut.
HEADER_BUDGET = 700
IMAGE_MAX_CHARS = MODEL_RESULT_CAP - HEADER_BUDGET  # 7300

CAVEAT = (
    "CAVEAT: this is machine-read text (OCR), not a transcription — words and "
    "whole lines can be dropped or misread. VERIFY every figure, date, account "
    "number and contact detail against the image itself before relying on it, "
    "and say so when you quote one."
)


def _header(
    summary: images.ImageSummary,
    result: Any,
    total: int,
    start: int,
    last: int,
    truncated: bool,
    hard_cut: Optional[tuple[int, int]],
) -> list[str]:
    line_word = "line" if total == 1 else "lines"
    out = [
        f"{summary.kind}, {summary.width}×{summary.height}, {total} {line_word} — "
        f"showing lines {start}–{last} of {total}.",
        CAVEAT,
    ]
    if summary.frames > 1:
        # Same honesty rule as read_document's "pages X-Y were not read": the
        # engine reads frame 1 only, and a multi-page scanned .tif is a normal
        # thing for a user to upload.
        out.append(
            f"PARTIAL: this image holds {summary.frames} frames and only the "
            f"FIRST was read — the other {summary.frames - 1} are not "
            f"retrievable by paging. Ask the user to send the remaining pages "
            f"as separate images or as a PDF."
        )
    if hard_cut is not None:
        line_no, length = hard_cut
        out.append(
            f"NOTE: line {line_no} is {length} characters, longer than the "
            f"{IMAGE_MAX_CHARS}-character read budget — it was hard-cut, and the "
            f"rest of that line is NOT retrievable by paging."
        )
    if truncated:
        out.append(
            f"TRUNCATED: call read_image again with start_line={last + 1} to continue."
        )
    engine = (
        f"Engine: {result.engine} {result.model}/{result.lang} via {result.backend}"
    )
    if result.scores:
        engine += (
            f"; confidence mean {result.mean_score:.2f}, lowest {result.min_score:.2f}"
            " (reported, not a pass mark)"
        )
    out.append(engine)
    return out


async def _read_image(args: dict[str, Any]) -> str:
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return "ERROR: 'file_id' is required (the id of an uploaded image)."
    record = await resolve_file(file_id.strip())
    if record is None:
        return "ERROR: no such file (unknown id, or you don't own it)."

    ext = Path(record.path).suffix.lower()
    if ext in ingest.SPREADSHEET_EXTS:
        return "ERROR: this is a spreadsheet — use inspect_excel / read_excel instead."
    if ext in ingest.DOCUMENT_EXTS:
        # Not an oversight: a PDF/DOCX has a text layer, and OCR'ing page 1 as a
        # picture would throw it away and return worse text with no warning.
        return "ERROR: this is a document — use read_document instead."
    if ext not in ingest.IMAGE_EXTS:
        return f"ERROR: '{ext}' is not an image this tool can read."

    lang = args.get("lang")
    if lang is not None:
        if not isinstance(lang, str) or lang.strip() not in image_ocr.SUPPORTED_LANGS:
            supported = ", ".join(sorted(image_ocr.SUPPORTED_LANGS))
            return f"ERROR: 'lang' must be one of {supported} (got '{lang}')."
        lang = lang.strip()

    try:
        start_line = int(args.get("start_line", 1) or 1)
    except (TypeError, ValueError):
        return "ERROR: 'start_line' must be an integer (1-based)."
    max_lines = args.get("max_lines")
    try:
        max_lines = int(max_lines) if max_lines is not None else None
    except (TypeError, ValueError):
        return "ERROR: 'max_lines' must be an integer."

    path = Path(record.path)
    # Dimensions + validation first: cheap, and it is what rejects a pixel bomb
    # or a file that is not really an image before an engine is handed it.
    try:
        summary = await asyncio.to_thread(images.summarize_image, path)
    except readers.ReadError as exc:
        return f"ERROR: could not read the image ({exc})."

    # OCR is sync and CPU-bound (~0.5-1 s for a screenshot) — off the loop.
    try:
        result = await asyncio.to_thread(image_ocr.ocr_image, path, lang=lang)
    except image_ocr.OcrUnavailable:
        # Fail CLOSED and say so. §18: every way this breaks otherwise looks
        # exactly like a clean success, so silence here would be a lie.
        return "ERROR: image OCR is not enabled on this deployment."
    except ValueError as exc:
        return f"ERROR: {exc}."

    if not result.lines:
        # Said explicitly, because an empty body reads to the model as "the
        # image is blank" — the same reasoning as read_document's scan message.
        return "ERROR: no text was detected in this image."

    total = len(result.lines)
    if start_line > total:
        return (
            f"ERROR: start_line={start_line} is past the end — this image "
            f"yielded {total} lines of text."
        )

    body, last, truncated, hard_cut = window(
        result.lines, start_line, max_lines,
        line_cap=READ_IMAGE_MAX_LINES, char_budget=IMAGE_MAX_CHARS,
    )
    header = _header(
        summary, result, total, max(1, start_line), last, truncated, hard_cut
    )
    return "\n".join(header + [""] + body)


SPEC = LocalToolSpec(
    name="read_image",
    description=(
        "Read the text in an IMAGE the USER attached to THIS chat (.png, .jpg, "
        ".jpeg, .webp, .tif, .tiff, .bmp) by its file_id — a screenshot, a photo "
        "of a document, or a scan. Uses OCR, so the result is machine-read text: "
        "it can drop or misread words, and the result says so. Never present a "
        "figure, date or account number from it as confirmed — tell the user to "
        "check it against the image. Page through long results with 'start_line' "
        "(1-based) and 'max_lines'; the FIRST lines of the result give the total "
        "line count and, if truncated, the exact start_line to continue from. "
        "Optional 'lang': 'devanagari' (the default, and it reads English too) or "
        "'en' for a Latin-only document. For a .pdf/.docx/.txt/.md/.json use "
        "read_document instead, for a spreadsheet use inspect_excel / read_excel, "
        "and for company policy or circulars the user did NOT attach, use "
        "search_department_docs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "Id of an uploaded/attached image.",
            },
            "start_line": {
                "type": "integer",
                "description": "1-based first line of text to return (default 1).",
            },
            "max_lines": {
                "type": "integer",
                "description": "Max lines to return this call (capped at 400).",
            },
            "lang": {
                "type": "string",
                "enum": ["devanagari", "en"],
                "description": (
                    "Recogniser language. Default 'devanagari', which also reads "
                    "English; use 'en' only for a document you know is Latin-only."
                ),
            },
        },
        "required": ["file_id"],
    },
    func=_read_image,
)

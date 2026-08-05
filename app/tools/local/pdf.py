"""Local tool: create_pdf (structured document -> .pdf download link).

The model supplies content only (title + sections, each with an optional
heading/body/table); the gateway renders a real PDF with fpdf2 and stores it,
mirroring create_excel/create_chart which take data and return a /v1/files/{id}
link. Uses fpdf2's built-in Helvetica core font (no embedded fonts, no system
libraries). Core fonts are latin-1 only, so text is sanitized to latin-1 (any
unsupported char — e.g. emoji, curly quotes — becomes '?') so a stray glyph
reports back instead of crashing the turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...files.store import PDF_MEDIA_TYPE, file_store
from .base import LocalToolSpec


def _latin1(text: str) -> str:
    """Coerce arbitrary text into what a core PDF font can draw (latin-1)."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def _validate(args: dict[str, Any]) -> tuple[str, list[dict], str] | str:
    """Return (title, sections, filename) on success, or an ERROR: string."""
    raw_sections = args.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        return "ERROR: 'sections' is required and must be a non-empty array of {heading?, body?, table?}."

    for idx, sec in enumerate(raw_sections):
        if not isinstance(sec, dict):
            return f"ERROR: sections[{idx}] must be an object with heading/body/table."
        has_heading = bool(sec.get("heading"))
        has_body = bool(sec.get("body"))
        table = sec.get("table")
        has_table = table is not None
        if not (has_heading or has_body or has_table):
            return f"ERROR: sections[{idx}] needs at least one of 'heading', 'body', or 'table'."
        if has_table:
            if not isinstance(table, dict):
                return f"ERROR: sections[{idx}].table must be an object with 'rows' (and optional 'headers')."
            rows = table.get("rows")
            if not isinstance(rows, list) or not rows:
                return f"ERROR: sections[{idx}].table.rows must be a non-empty array of rows."
            for ridx, row in enumerate(rows):
                if not isinstance(row, (list, tuple)):
                    return f"ERROR: sections[{idx}].table.rows[{ridx}] must be an array of cell values."
            headers = table.get("headers")
            if headers is not None and not isinstance(headers, list):
                return f"ERROR: sections[{idx}].table.headers must be an array of column names."

    title = str(args.get("title") or "")
    filename = str(args.get("filename") or "document.pdf")
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return title, raw_sections, filename


def _draw_table(pdf, table: dict) -> None:
    """Draw a simple bordered grid: optional bold header row, then data rows."""
    headers = table.get("headers")
    rows = table["rows"]
    ncols = max([len(headers or [])] + [len(r) for r in rows]) or 1
    avail = pdf.w - pdf.l_margin - pdf.r_margin
    col_w = avail / ncols
    line_h = 7.0

    if headers:
        pdf.set_font("Helvetica", "B", 11)
        for c in range(ncols):
            cell = _latin1(headers[c]) if c < len(headers) else ""
            pdf.cell(col_w, line_h, cell, border=1)
        pdf.ln(line_h)

    pdf.set_font("Helvetica", "", 11)
    for row in rows:
        for c in range(ncols):
            cell = _latin1(row[c]) if c < len(row) else ""
            pdf.cell(col_w, line_h, cell, border=1)
        pdf.ln(line_h)


def _build_pdf_bytes(title: str, sections: list[dict]) -> bytes:
    """Render the document with fpdf2. Sync — run in a thread."""
    from fpdf import FPDF

    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    if title:
        pdf.set_font("Helvetica", "B", 20)
        pdf.multi_cell(0, 10, _latin1(title))
        pdf.ln(2)

    for sec in sections:
        heading = sec.get("heading")
        if heading:
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(0, 8, _latin1(heading))
            pdf.ln(1)
        body = sec.get("body")
        if body:
            pdf.set_font("Helvetica", "", 11)
            pdf.multi_cell(0, 6, _latin1(body))
            pdf.ln(1)
        table = sec.get("table")
        if table is not None:
            _draw_table(pdf, table)
        pdf.ln(3)

    # fpdf2 v2 returns a bytearray; the store wants bytes.
    return bytes(pdf.output())


async def _create_pdf(args: dict[str, Any]) -> str:
    validated = _validate(args)
    if isinstance(validated, str):  # an ERROR: message
        return validated
    title, sections, filename = validated

    try:
        data = await asyncio.to_thread(_build_pdf_bytes, title, sections)
    except Exception as exc:  # noqa: BLE001 - report back, don't raise into the loop
        return f"ERROR: failed to build PDF: {exc}"

    record = await file_store.save(data, filename=filename, media_type=PDF_MEDIA_TYPE)
    # Same string shape as create_excel/create_chart so the frontend parses it identically.
    return (
        f"Created PDF '{record.filename}' "
        f"({record.size} bytes, {len(sections)} section(s)). "
        f"Download it at: GET /v1/files/{record.id}"
    )


SPEC = LocalToolSpec(
    name="create_pdf",
    description=(
        "Create a PDF document and return a download link. Provide 'sections' "
        "(array of {heading?, body?, table?}) and optionally a 'title' and "
        "'filename'. Each section may have a 'heading' (bold), a 'body' "
        "(wrapped paragraph text), and/or a 'table' ({headers?, rows[][]}). "
        "Use this for reports, summaries, or letters. Text uses a standard "
        "Latin font; emoji and other non-Latin characters are not rendered."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Optional document title (large, at the top)."},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string", "description": "Optional bold section heading."},
                        "body": {"type": "string", "description": "Optional paragraph text (wraps automatically)."},
                        "table": {
                            "type": "object",
                            "properties": {
                                "headers": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional bold column headers.",
                                },
                                "rows": {
                                    "type": "array",
                                    "items": {"type": "array"},
                                    "description": "Rows of cell values; each row is an array.",
                                },
                            },
                            "required": ["rows"],
                            "description": "Optional simple table.",
                        },
                    },
                },
                "description": "Document sections in order; each needs at least a heading, body, or table.",
            },
            "filename": {
                "type": "string",
                "description": "Output file name, e.g. 'report.pdf' (default 'document.pdf').",
            },
        },
        "required": ["sections"],
    },
    func=_create_pdf,
)

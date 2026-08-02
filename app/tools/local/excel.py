"""Local tool: create_excel (tabular data -> .xlsx download link)."""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, Optional

from ...files.store import XLSX_MEDIA_TYPE, file_store
from .base import LocalToolSpec


def _build_xlsx_bytes(sheet_name: str, headers: Optional[list], rows: list) -> bytes:
    """Build an .xlsx workbook in memory. Sync (openpyxl) — run in a thread."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_name or "Sheet1")[:31]  # Excel caps sheet names at 31 chars

    if headers:
        ws.append([str(h) for h in headers])
        for cell in ws[1]:
            cell.font = Font(bold=True)

    for row in rows:
        if isinstance(row, dict):
            values = [row.get(h, "") for h in headers] if headers else list(row.values())
            ws.append(values)
        elif isinstance(row, (list, tuple)):
            ws.append(list(row))
        else:
            ws.append([row])

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _create_excel(args: dict[str, Any]) -> str:
    """Create an .xlsx from headers + rows, store it, return its download link."""
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows:
        return "ERROR: 'rows' is required and must be a non-empty array of rows."

    headers = args.get("headers")
    if headers is not None and not isinstance(headers, list):
        return "ERROR: 'headers' must be an array of column-name strings."

    filename = str(args.get("filename") or "data.xlsx")
    if not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"
    sheet_name = str(args.get("sheet_name") or "Sheet1")

    try:
        data = await asyncio.to_thread(_build_xlsx_bytes, sheet_name, headers, rows)
    except Exception as exc:  # noqa: BLE001 - report back, don't raise into the loop
        return f"ERROR: failed to build spreadsheet: {exc}"

    record = file_store.save(data, filename=filename, media_type=XLSX_MEDIA_TYPE)
    return (
        f"Created Excel file '{record.filename}' "
        f"({record.size} bytes, {len(rows)} data row(s)). "
        f"Download it at: GET /v1/files/{record.id}"
    )


SPEC = LocalToolSpec(
    name="create_excel",
    description=(
        "Create an Excel (.xlsx) spreadsheet from tabular data and return a "
        "download link. Provide 'rows' (array of rows, each row an array of "
        "cell values), and optionally 'headers' (column names) and 'filename'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Output file name, e.g. 'report.xlsx'."},
            "sheet_name": {"type": "string", "description": "Worksheet name (default 'Sheet1')."},
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional column headers (bold first row).",
            },
            "rows": {
                "type": "array",
                "items": {"type": "array"},
                "description": "Rows of cell values; each row is an array aligned to headers.",
            },
        },
        "required": ["rows"],
    },
    func=_create_excel,
)

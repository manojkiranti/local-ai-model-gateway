"""Local tool: create_csv (tabular data -> .csv download link).

Mirrors create_excel but emits plain CSV via the stdlib `csv` module (which
correctly quotes fields containing the delimiter, quotes or newlines). The model
supplies rows (+ optional headers); the gateway writes the file and returns a
/v1/files/{id} link. A file tool, so it flows through the per-user file sink.
"""

from __future__ import annotations

import csv as _csv
import io
from typing import Any

from ...files.store import CSV_MEDIA_TYPE, file_store
from .base import LocalToolSpec


def _build_csv_text(headers: list | None, rows: list, delimiter: str) -> str:
    buffer = io.StringIO()
    writer = _csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    if headers:
        writer.writerow([str(h) for h in headers])
    for row in rows:
        if isinstance(row, dict):
            # Dict rows follow header order (missing keys -> empty cell).
            values = [row.get(h, "") for h in headers] if headers else list(row.values())
            writer.writerow(values)
        elif isinstance(row, (list, tuple)):
            writer.writerow(list(row))
        else:
            writer.writerow([row])
    return buffer.getvalue()


async def _create_csv(args: dict[str, Any]) -> str:
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows:
        return "ERROR: 'rows' is required and must be a non-empty array of rows."

    headers = args.get("headers")
    if headers is not None and not isinstance(headers, list):
        return "ERROR: 'headers' must be an array of column-name strings."

    delimiter = str(args.get("delimiter") or ",")
    if len(delimiter) != 1:
        return "ERROR: 'delimiter' must be a single character."

    filename = str(args.get("filename") or "data.csv")
    if not filename.lower().endswith(".csv"):
        filename += ".csv"

    try:
        text = _build_csv_text(headers, rows, delimiter)
    except Exception as exc:  # noqa: BLE001 - report back, don't raise into the loop
        return f"ERROR: failed to build CSV: {exc}"

    record = await file_store.save(
        text.encode("utf-8"), filename=filename, media_type=CSV_MEDIA_TYPE
    )
    # Same string shape as create_excel so the frontend parses it identically.
    return (
        f"Created CSV file '{record.filename}' "
        f"({record.size} bytes, {len(rows)} data row(s)). "
        f"Download it at: GET /v1/files/{record.id}"
    )


SPEC = LocalToolSpec(
    name="create_csv",
    description=(
        "Create a CSV file from tabular data and return a download link. Provide "
        "'rows' (array of rows; each row an array of cell values, or an object "
        "keyed by header name), and optionally 'headers' (column names), "
        "'delimiter' (default ','), and 'filename'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Output file name, e.g. 'report.csv'."},
            "delimiter": {
                "type": "string",
                "description": "Single-character field delimiter (default ',').",
            },
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional column headers (written as the first row).",
            },
            "rows": {
                "type": "array",
                "items": {"type": "array"},
                "description": "Rows of cell values; each row is an array aligned to headers "
                "(or an object keyed by header name).",
            },
        },
        "required": ["rows"],
    },
    func=_create_csv,
)

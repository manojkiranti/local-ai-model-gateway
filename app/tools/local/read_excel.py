"""Local tool: read_excel — rows of ONE sheet of an uploaded spreadsheet.

Owner-scoped by file_id (see files/source.py). Reads one sheet at a time,
capped (~200 rows / ~40k chars) so it can't blow the model's context. On a
multi-sheet workbook with no `sheet` given, it reads the FIRST sheet and names
the others in the response, so the model never silently answers from the wrong
tab. Cross-sheet questions = call this once per sheet. Formulas never evaluated.
"""

from __future__ import annotations

from typing import Any, Optional

from ...files import readers
from ...files.store import resolve_file
from .base import LocalToolSpec


def _as_str_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    return None


def _format(table: readers.Table, start_row: int) -> str:
    header = table.headers or []
    n = len(table.rows)
    first = start_row
    last = start_row + n - 1 if n else start_row - 1
    head = (
        f"Sheet '{table.sheet_name}' — showing rows {first}–{last} "
        f"of {table.total_rows} data row(s), {table.total_cols} column(s)."
    )
    lines = [head]

    others = [s for s in table.all_sheets if s != table.sheet_name]
    if others:
        lines.append(
            f"This workbook has {len(table.all_sheets)} sheets: "
            f"{', '.join(table.all_sheets)}. Pass sheet=\"…\" to read another."
        )
    lines.append("")
    if header:
        lines.append(" | ".join(header))
        lines.append("-+-".join("-" * len(h) for h in header))
    for row in table.rows:
        lines.append(" | ".join(row))
    if table.truncated:
        nxt = last + 1
        lines.append("")
        lines.append(
            f"…output truncated. To see more, call read_excel with start_row={nxt} "
            f"(and/or narrow with columns=[…] or a smaller max_rows)."
        )
    return "\n".join(lines).rstrip()


async def _read_excel(args: dict[str, Any]) -> str:
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return "ERROR: 'file_id' is required (the id of an uploaded spreadsheet)."
    record = await resolve_file(file_id.strip())
    if record is None:
        return "ERROR: no such file (unknown id, or you don't own it)."

    sheet = args.get("sheet")
    sheet = str(sheet) if sheet is not None else None
    columns = _as_str_list(args.get("columns"))
    try:
        start_row = int(args.get("start_row", 1) or 1)
    except (TypeError, ValueError):
        return "ERROR: 'start_row' must be an integer (1-based)."
    max_rows = args.get("max_rows")
    try:
        max_rows = int(max_rows) if max_rows is not None else None
    except (TypeError, ValueError):
        return "ERROR: 'max_rows' must be an integer."

    try:
        table = readers.load_table(
            record.path, sheet=sheet, columns=columns,
            start_row=start_row, max_rows=max_rows,
        )
    except readers.SheetNotFound as exc:
        return f"ERROR: {exc}."
    except readers.ReadError as exc:
        return f"ERROR: could not read the spreadsheet ({exc})."
    return _format(table, max(1, start_row))


SPEC = LocalToolSpec(
    name="read_excel",
    description=(
        "Read rows from ONE sheet of an uploaded spreadsheet (.xlsx/.csv) by its "
        "file_id. Optionally pick a 'sheet' (name or 1-based index), project "
        "'columns' (list of header names), and page with 'start_row' (1-based) + "
        "'max_rows'. Output is CAPPED at a few hundred rows; if truncated it tells "
        "you how to page. Because of that cap, do NOT sum, average or count from "
        "this output — use aggregate_excel for any total or breakdown, which reads "
        "every row. For a multi-sheet file, call once per sheet. Use inspect_excel "
        "first to see the sheets and headers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "Id of an uploaded/attached spreadsheet."},
            "sheet": {"type": "string", "description": "Sheet name or 1-based index (default: first sheet)."},
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset/order of column headers to return.",
            },
            "start_row": {"type": "integer", "description": "1-based first DATA row to return (default 1)."},
            "max_rows": {"type": "integer", "description": "Max rows to return this call (capped ~200)."},
        },
        "required": ["file_id"],
    },
    func=_read_excel,
)

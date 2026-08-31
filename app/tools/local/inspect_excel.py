"""Local tool: inspect_excel — the structure of an uploaded spreadsheet.

Given a `file_id` (from an upload / attachment), returns every sheet's name,
row/column counts, headers, and a small sample of rows, so the model can decide
which sheet + columns to actually read. Reads are owner-scoped: the id resolves
only if the caller owns the file (see files/source.py), else a friendly ERROR.
Formulas are never evaluated (readers opens data_only).
"""

from __future__ import annotations

from typing import Any

from ...files import readers
from ...files.store import resolve_file
from .base import LocalToolSpec

MAX_HEADERS_SHOWN = 30


def _format(infos: list[readers.SheetInfo], filename: str) -> str:
    lines = [f"Spreadsheet '{filename}' — {len(infos)} sheet(s):", ""]
    for s in infos:
        flag = " (hidden)" if s.hidden else ""
        lines.append(f"### Sheet '{s.sheet_name}'{flag} — {s.total_rows} rows × {s.total_cols} cols")
        if s.headers:
            shown = s.headers[:MAX_HEADERS_SHOWN]
            more = "" if len(s.headers) <= MAX_HEADERS_SHOWN else f" …(+{len(s.headers) - MAX_HEADERS_SHOWN} more)"
            lines.append("Columns: " + ", ".join(shown) + more)
        else:
            lines.append("Columns: (none — empty or chart-only sheet)")
        if s.sample_rows:
            lines.append("Sample rows:")
            for row in s.sample_rows:
                lines.append("  " + " | ".join(row))
        lines.append("")
    lines.append("Use read_excel(file_id, sheet=…) to read a sheet's rows.")
    return "\n".join(lines).rstrip()


async def _inspect_excel(args: dict[str, Any]) -> str:
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return "ERROR: 'file_id' is required (the id of an uploaded spreadsheet)."
    record = await resolve_file(file_id.strip())
    if record is None:
        return "ERROR: no such file (unknown id, or you don't own it)."
    try:
        infos = readers.inspect_workbook(record.path)
    except readers.ReadError as exc:
        return f"ERROR: could not read the spreadsheet ({exc})."
    return _format(infos, record.filename)


SPEC = LocalToolSpec(
    name="inspect_excel",
    description=(
        "Inspect an uploaded spreadsheet (.xlsx/.csv) by its file_id and return "
        "the structure of EVERY sheet: name, row/column counts, column headers, "
        "and a few sample rows. Call this first to see what a file contains, then "
        "pick by question: for a total, average, min/max, count or a breakdown by "
        "category use aggregate_excel (it reads every row); to look at or quote "
        "actual rows use read_excel (capped, so never add up its output); to "
        "MODIFY the workbook and get an edited copy back use edit_excel. The "
        "sample rows here are a preview only — never answer from them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "Id of an uploaded/attached spreadsheet file.",
            },
        },
        "required": ["file_id"],
    },
    func=_inspect_excel,
)

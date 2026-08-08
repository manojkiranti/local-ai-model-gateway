"""Local tool: aggregate_excel — totals and group-bys over an uploaded sheet.

Why this exists: `read_excel` is capped (~200 rows / ~40k chars), so on a large
sheet the model only ever sees a slice and any total it computes itself is
quietly wrong. This tool does the arithmetic in the gateway over EVERY row and
returns a bounded result, so the answer no longer depends on context size.

Owner-scoped by file_id like the other read tools. Every result states how many
rows it counted, how many cells it had to skip, and whether the scan was cut
short — a silently-partial number is the failure this tool removes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from ...files import readers
from ...files.aggregate import (
    MAX_GROUPS,
    MAX_SCAN_ROWS,
    VALID_FILTER_OPS,
    VALID_METRIC_OPS,
    AggregateResult,
    Filter,
    Metric,
    MetricValue,
    UnknownColumn,
    aggregate,
)
from ...files.store import resolve_file
from .base import LocalToolSpec


def _parse_metrics(raw: Any) -> list[Metric]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("'metrics' must be a list of {column, op} objects.")
    out: list[Metric] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(
                'each metric must be an object like {"column": "Amount", "op": "sum"}.'
            )
        column = str(item.get("column", "")).strip()
        op = str(item.get("op", "")).strip().lower()
        if not column:
            raise ValueError("each metric needs a 'column'.")
        if op not in VALID_METRIC_OPS:
            raise ValueError(f"unknown metric op '{op}' (use: {', '.join(VALID_METRIC_OPS)}).")
        out.append(Metric(column=column, op=op))
    return out


def _parse_filters(raw: Any) -> list[Filter]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("'filters' must be a list of {column, op, value} objects.")
    out: list[Filter] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(
                'each filter must be an object like '
                '{"column": "Region", "op": "eq", "value": "NSW"}.'
            )
        column = str(item.get("column", "")).strip()
        op = str(item.get("op", "")).strip().lower()
        if not column:
            raise ValueError("each filter needs a 'column'.")
        if op not in VALID_FILTER_OPS:
            raise ValueError(f"unknown filter op '{op}' (use: {', '.join(VALID_FILTER_OPS)}).")
        out.append(Filter(column=column, op=op, value=item.get("value")))
    return out


def _fmt_number(value: MetricValue) -> str:
    """Thousands separators; at most 2 decimals; integers stay integers."""
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value.quantize(Decimal('0.01')):,.2f}"


def _describe_filters(filters: list[Filter]) -> str:
    return "; ".join(f'{f.column} {f.op} "{f.value}"' for f in filters)


def _format(
    result: AggregateResult,
    stream: readers.RowStream,
    *,
    filters: list[Filter],
) -> str:
    lines: list[str] = []

    head = f"Sheet '{stream.sheet_name}' — {result.rows_matched:,} matching row(s)"
    if filters and result.rows_scanned != result.rows_matched:
        head += f" of {result.rows_scanned:,} scanned"
    lines.append(head + ".")

    if filters:
        lines.append(f"Filters: {_describe_filters(filters)}")

    others = [s for s in stream.all_sheets if s != stream.sheet_name]
    if others:
        lines.append(
            f"This workbook has {len(stream.all_sheets)} sheets: "
            f"{', '.join(stream.all_sheets)}. Pass sheet=\"…\" to aggregate another."
        )
    lines.append("")

    grouped = bool(result.groups) and result.groups[0].key != "(all)"
    headers = (["group"] if grouped else []) + ["rows"] + result.metric_labels
    rows = [
        (([g.key] if grouped else []) + [f"{g.row_count:,}"] + [_fmt_number(v) for v in g.values])
        for g in result.groups
    ]

    widths = [
        max([len(headers[i])] + [len(r[i]) for r in rows]) for i in range(len(headers))
    ]
    lines.append(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    lines.append("-+-".join("-" * w for w in widths))
    for row in rows:
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))

    lines.append("")
    if result.total_groups > len(result.groups):
        order = result.metric_labels[0] if result.metric_labels else "row count"
        lines.append(
            f"Showing the top {len(result.groups)} of {result.total_groups:,} groups by {order}."
        )

    # Provenance — one line per column whose cells could not all be read.
    for column, count in result.skipped.items():
        counted = result.parsed.get(column, 0)
        note = (
            f"Counted {counted:,} of {result.rows_matched:,} matching rows for "
            f"'{column}'; {count:,} skipped as non-numeric"
        )
        examples = ", ".join(f'"{e}"' for e in result.skipped_examples.get(column, []))
        if examples:
            note += f" (e.g. {examples})"
        blanks = result.blank.get(column, 0)
        if blanks:
            note += f"; {blanks:,} blank"
        lines.append(note + ".")

    if result.scan_truncated:
        if stream.total_rows_hint:
            lines.append(
                f"STOPPED at {result.rows_scanned:,} of {stream.total_rows_hint:,} rows — "
                f"this result is PARTIAL."
            )
        else:
            lines.append(
                f"STOPPED at {result.rows_scanned:,} rows — the sheet has more; "
                f"this result is PARTIAL."
            )

    return "\n".join(lines).rstrip()


async def _aggregate_excel(args: dict[str, Any]) -> str:
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return "ERROR: 'file_id' is required (the id of an uploaded spreadsheet)."
    record = await resolve_file(file_id.strip())
    if record is None:
        return "ERROR: no such file (unknown id, or you don't own it)."

    try:
        metrics = _parse_metrics(args.get("metrics"))
        filters = _parse_filters(args.get("filters"))
    except ValueError as exc:
        return f"ERROR: {exc}"

    sheet = args.get("sheet")
    sheet = str(sheet) if sheet is not None else None
    group_by = args.get("group_by")
    group_by = str(group_by).strip() if group_by else None

    try:
        with readers.open_sheet_rows(record.path, sheet=sheet) as stream:
            result = aggregate(
                stream.headers,
                stream.rows,
                filters=filters,
                group_by=group_by,
                metrics=metrics,
                max_scan_rows=MAX_SCAN_ROWS,
                max_groups=MAX_GROUPS,
            )
            return _format(result, stream, filters=filters)
    except UnknownColumn as exc:
        return f"ERROR: no column '{exc.column}' (have: {', '.join(exc.headers)})."
    except readers.SheetNotFound as exc:
        return f"ERROR: {exc}."
    except ValueError as exc:
        return f"ERROR: {exc}"
    except (readers.ReadError, UnicodeError) as exc:
        return f"ERROR: could not read the spreadsheet ({exc})."


SPEC = LocalToolSpec(
    name="aggregate_excel",
    description=(
        "Compute totals over an uploaded spreadsheet (.xlsx/.csv) by file_id — "
        "sum/avg/min/max/count of a column, optionally grouped by another column "
        "and filtered. USE THIS INSTEAD OF read_excel whenever the question is "
        "about a total, an average, a count or a breakdown: read_excel is capped "
        "at ~200 rows, so adding up its output is WRONG on a larger sheet, while "
        "this reads every row. Returns a small table plus a note saying how many "
        "rows were counted and how many cells were skipped as non-numeric."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "Id of an uploaded/attached spreadsheet."},
            "sheet": {
                "type": "string",
                "description": "Sheet name or 1-based index (default: first sheet).",
            },
            "metrics": {
                "type": "array",
                "description": "What to compute. Omit for a plain row count.",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string", "description": "Header name of the column."},
                        "op": {"type": "string", "enum": list(VALID_METRIC_OPS)},
                    },
                    "required": ["column", "op"],
                },
            },
            "group_by": {
                "type": "string",
                "description": "Optional header name to break the totals down by (one column).",
            },
            "filters": {
                "type": "array",
                "description": "Optional row filters, ALL of which must match (AND).",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string"},
                        "op": {"type": "string", "enum": list(VALID_FILTER_OPS)},
                        "value": {"description": "Text or number to compare against."},
                    },
                    "required": ["column", "op", "value"],
                },
            },
        },
        "required": ["file_id"],
    },
    func=_aggregate_excel,
)

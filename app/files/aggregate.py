"""Filter → group → accumulate over a stream of spreadsheet rows.

Pure: takes headers plus any iterable of string rows and returns numbers. No
file I/O, no DB, no contextvars — `open_sheet_rows` feeds it and the tool
adapter formats it.

THE THREE-OUTCOME RULE (why the output can be trusted). For a numeric metric,
each cell is exactly one of:

  blank        -> absent. No contribution; excluded from avg's denominator.
                  Not an error, counted separately in `blank`.
  parses       -> counted in `parsed`.
  unparseable  -> excluded, counted in `skipped`, and the first few offending
                  values are kept in `skipped_examples` so the caller can name
                  them. NEVER silently dropped.

`count` is the exception: it counts non-blank cells without parsing, so a text
column is a legitimate target and nothing is 'skipped'.

A column where nothing parsed yields None, not 0 — zero would be a lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Optional, Union

from .numeric import parse_number

MAX_SCAN_ROWS = 200_000
MAX_GROUPS = 50
MAX_SKIPPED_EXAMPLES = 3

VALID_METRIC_OPS = ("sum", "avg", "min", "max", "count")
VALID_FILTER_OPS = ("eq", "ne", "contains", "gt", "gte", "lt", "lte")
_NUMERIC_FILTER_OPS = ("gt", "gte", "lt", "lte")

MetricValue = Optional[Union[Decimal, int]]


class UnknownColumn(Exception):
    """A referenced column is not in the sheet's header row."""

    def __init__(self, column: str, headers: list[str]) -> None:
        super().__init__(f"no column '{column}'")
        self.column = column
        self.headers = headers


@dataclass
class Metric:
    column: str
    op: str  # sum|avg|min|max|count

    @property
    def label(self) -> str:
        return f"{self.op}({self.column})"


@dataclass
class Filter:
    column: str
    op: str  # eq|ne|contains|gt|gte|lt|lte
    value: Any


@dataclass
class GroupResult:
    key: str
    row_count: int
    values: list[MetricValue]


@dataclass
class AggregateResult:
    groups: list[GroupResult]
    total_groups: int                    # before the max_groups cap
    rows_scanned: int
    rows_matched: int
    metric_labels: list[str]
    parsed: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    skipped_examples: dict[str, list[str]] = field(default_factory=dict)
    blank: dict[str, int] = field(default_factory=dict)
    scan_truncated: bool = False


def _norm(text: Any) -> str:
    """Header/value normalization. Defined here rather than imported from
    readers so this module stays independent of the reader."""
    return str(text).strip().lower()


class _Acc:
    """Running accumulator for one metric within one group."""

    def __init__(self, op: str) -> None:
        self.op = op
        self.total = Decimal(0)
        self.n = 0
        self.min: Optional[Decimal] = None
        self.max: Optional[Decimal] = None

    def add_number(self, value: Decimal) -> None:
        self.total += value
        self.n += 1
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value

    def add_present(self) -> None:
        self.n += 1

    def value(self) -> MetricValue:
        if self.op == "count":
            return self.n
        if self.n == 0:
            return None          # nothing parsed — None, never 0
        if self.op == "sum":
            return self.total
        if self.op == "avg":
            return self.total / self.n
        if self.op == "min":
            return self.min
        return self.max


def _resolve(column: str, index: dict[str, int], headers: list[str]) -> int:
    i = index.get(_norm(column))
    if i is None:
        raise UnknownColumn(str(column), headers)
    return i


def _cell_at(row: list[str], i: int) -> str:
    return row[i] if i < len(row) else ""


def _matches(cell: str, flt: Filter) -> tuple[bool, bool]:
    """(matched, cell_was_unparseable_for_a_numeric_op)."""
    if flt.op in _NUMERIC_FILTER_OPS:
        left = parse_number(cell)
        right = parse_number(str(flt.value))
        if left is None or right is None:
            return False, left is None and cell.strip() != ""
        if flt.op == "gt":
            return left > right, False
        if flt.op == "gte":
            return left >= right, False
        if flt.op == "lt":
            return left < right, False
        return left <= right, False

    if flt.op == "contains":
        return _norm(flt.value) in _norm(cell), False

    # eq/ne: compare numerically when BOTH sides are numbers, so "1,000" == 1000.
    left_num = parse_number(cell)
    right_num = parse_number(str(flt.value))
    if left_num is not None and right_num is not None:
        equal = left_num == right_num
    else:
        equal = _norm(cell) == _norm(flt.value)
    return (equal if flt.op == "eq" else not equal), False


def aggregate(
    headers: list[str],
    rows: Iterable[list[str]],
    *,
    filters: Optional[list[Filter]] = None,
    group_by: Optional[str] = None,
    metrics: Optional[list[Metric]] = None,
    max_scan_rows: int = MAX_SCAN_ROWS,
    max_groups: int = MAX_GROUPS,
) -> AggregateResult:
    filters = list(filters or [])
    metrics = list(metrics or [])

    for m in metrics:
        if m.op not in VALID_METRIC_OPS:
            raise ValueError(f"unknown metric op '{m.op}' (use: {', '.join(VALID_METRIC_OPS)})")
    for f in filters:
        if f.op not in VALID_FILTER_OPS:
            raise ValueError(f"unknown filter op '{f.op}' (use: {', '.join(VALID_FILTER_OPS)})")

    index = {_norm(h): i for i, h in enumerate(headers)}
    metric_idx = [_resolve(m.column, index, headers) for m in metrics]
    filter_idx = [_resolve(f.column, index, headers) for f in filters]
    group_idx = _resolve(group_by, index, headers) if group_by else None

    # Distinct columns needing numeric parsing, keyed by index so two spellings
    # of the same column ("Amount"/"amount") collapse to one. The first
    # spelling seen becomes the display name used in the accounting dicts.
    numeric_cols: dict[int, str] = {}
    for m, i in zip(metrics, metric_idx):
        if m.op != "count":
            numeric_cols.setdefault(i, m.column)

    parsed: dict[str, int] = {}
    skipped: dict[str, int] = {}
    skipped_examples: dict[str, list[str]] = {}
    blank: dict[str, int] = {}

    def _note_skip(column: str, raw: str) -> None:
        skipped[column] = skipped.get(column, 0) + 1
        ex = skipped_examples.setdefault(column, [])
        if len(ex) < MAX_SKIPPED_EXAMPLES and raw not in ex:
            ex.append(raw)

    # normalized group key -> (display key, [accumulators], row_count)
    groups: dict[str, tuple[str, list[_Acc], int]] = {}
    rows_scanned = 0
    rows_matched = 0
    scan_truncated = False

    for row in rows:
        if rows_scanned >= max_scan_rows:
            scan_truncated = True
            break
        # A stream cannot trim trailing empties the way _xlsx_sheet_grid does,
        # so drop fully blank rows here instead.
        if not any(c.strip() for c in row):
            continue
        rows_scanned += 1

        keep = True
        for f, i in zip(filters, filter_idx):
            raw = _cell_at(row, i)
            matched, unparseable = _matches(raw, f)
            if unparseable:
                _note_skip(f.column, raw.strip())
            if not matched:
                keep = False
                break
        if not keep:
            continue
        rows_matched += 1

        key_display = "(all)"   # NOT "(blank)" — the formatter keys off this
        key_norm = ""
        if group_idx is not None:
            raw_key = _cell_at(row, group_idx).strip()
            key_display = raw_key or "(blank)"
            key_norm = _norm(raw_key)

        entry = groups.get(key_norm)
        if entry is None:
            entry = (key_display, [_Acc(m.op) for m in metrics], 0)
        display, accs, count = entry
        groups[key_norm] = (display, accs, count + 1)

        # Classify each numeric column's cell ONCE per row. Keyed by column
        # index, so `sum(Amount)` and `avg(Amount)` in the same call neither
        # double-count the accounting nor parse the cell twice.
        cell_values: dict[int, Optional[Decimal]] = {}
        for i, name in numeric_cols.items():
            raw = _cell_at(row, i)
            if not raw.strip():
                blank[name] = blank.get(name, 0) + 1
                cell_values[i] = None
                continue
            value = parse_number(raw)
            if value is None:
                _note_skip(name, raw.strip())
            else:
                parsed[name] = parsed.get(name, 0) + 1
            cell_values[i] = value

        for m, i, acc in zip(metrics, metric_idx, accs):
            if m.op == "count":
                # Counts non-blank cells without parsing — a text column is a
                # legitimate target, so nothing here is ever 'skipped'.
                if _cell_at(row, i).strip():
                    acc.add_present()
                continue
            value = cell_values.get(i)
            if value is not None:
                acc.add_number(value)

    # An ungrouped call still reports one implicit group, even over no rows.
    # A GROUPED call with no matches correctly returns no groups at all — an
    # empty breakdown, not a phantom "(all)" row.
    if not groups and group_idx is None:
        groups[""] = ("(all)", [_Acc(m.op) for m in metrics], 0)

    results = [
        GroupResult(key=display, row_count=count, values=[a.value() for a in accs])
        for display, accs, count in groups.values()
    ]
    return AggregateResult(
        groups=results,
        total_groups=len(results),
        rows_scanned=rows_scanned,
        rows_matched=rows_matched,
        metric_labels=[m.label for m in metrics],
        parsed=parsed,
        skipped=skipped,
        skipped_examples=skipped_examples,
        blank=blank,
        scan_truncated=scan_truncated,
    )

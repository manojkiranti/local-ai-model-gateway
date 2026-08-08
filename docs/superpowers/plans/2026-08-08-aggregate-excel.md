# aggregate_excel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local tool that computes totals and one-level group-bys over an uploaded spreadsheet inside the gateway, so answers stop depending on how much of the sheet fits in the model's context window.

**Architecture:** Three new pure modules plus one thin tool adapter, mirroring how `read_excel.py` (adapter) sits on `readers.py` (pure). `numeric.parse_number` turns display text back into numbers; `readers.open_sheet_rows` streams a sheet uncapped; `aggregate.aggregate` filters/groups/accumulates over that stream; `tools/local/aggregate_excel.py` validates args, resolves the owner-scoped file, and formats text.

**Tech Stack:** Python 3.10, stdlib `decimal`/`csv`, openpyxl (already a dependency), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-08-aggregate-excel-design.md`

## Global Constraints

- Use **this** project's venv: `.venv/bin/pytest`, `.venv/bin/python`. Never a sibling's.
- **No new dependencies.** stdlib + openpyxl only.
- **Never `eval`.** Number parsing is pure string manipulation, consistent with `app/tools/local/calculator.py`.
- **`Decimal`, never `float`,** for any accumulated figure. Float drift is unacceptable on financial totals.
- Formulas are never evaluated — inherited from `_open_xlsx(read_only=True, data_only=True)`.
- Pure modules under `app/files/` must not import DB, HTTP, or contextvars. Only the tool adapter touches `resolve_file`.
- Every result states its own provenance: how many rows were counted, how many were skipped, and whether the scan was cut short. A silently-partial answer is the bug this feature exists to remove.
- Tool error strings start with `ERROR: ` and never leak another user's data (match `read_excel.py`).

## File Structure

| File | Responsibility |
|---|---|
| `app/files/numeric.py` (new) | `parse_number(text) -> Decimal \| None`. Coercion policy, nothing else. |
| `app/files/readers.py` (modify) | Add `RowStream` + `open_sheet_rows()` — uncapped streaming row access. Existing capped `load_table`/`inspect_workbook` untouched. |
| `app/files/aggregate.py` (new) | Filter → group → accumulate over a row stream. Pure; no file I/O. |
| `app/tools/local/aggregate_excel.py` (new) | Arg validation, `resolve_file`, formatting, `SPEC`. |
| `app/tools/local/__init__.py` (modify) | One import + one `LOCAL_TOOLS` entry. |
| `tests/test_numeric_parse.py` (new) | Coercion table. |
| `tests/test_row_stream.py` (new) | Streaming reader, xlsx + csv. |
| `tests/test_aggregate.py` (new) | Engine: metrics, filters, grouping, accounting, caps. |
| `tests/test_aggregate_excel_tool.py` (new) | End-to-end through the tool fn. |
| `tests/test_aggregate_eval.py` (new) | The 8 labelled eval cases from the spec. |

### Deviations from the spec, deliberate

**1. `open_sheet_rows` (context manager), not `iter_sheet_rows` (plain function).** The spec named it `iter_sheet_rows` returning a tuple. An openpyxl workbook must stay open for the life of the iterator and be closed after; a bare generator leaks the handle when the consumer stops early (which the scan ceiling does, by design). A context manager makes that impossible to get wrong.

**2. The over-size message.** The spec's version reads `stopped at 200,000 of 340,000 rows`. A streamed CSV cannot know its total row count without scanning it. `RowStream` therefore carries `total_rows_hint: int | None` — populated for xlsx from `ws.max_row`, `None` for csv — and the message degrades to `stopped at 200,000 rows — the sheet has more; this result is partial` when the total is unknown. Same honesty, no false precision.

---

### Task 1: `parse_number`

**Files:**
- Create: `app/files/numeric.py`
- Test: `tests/test_numeric_parse.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_number(text: str) -> Decimal | None` — used by Task 3.

- [ ] **Step 1: Write the failing test**

Create `tests/test_numeric_parse.py`:

```python
"""The numeric coercion table — the trust-critical piece of aggregate_excel.

Every spreadsheet cell reaches us as display text, so a wrong answer here is a
wrong total downstream. Table-driven so adding a real-world format found in
production is a one-line change.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.files.numeric import parse_number

PARSES = [
    ("1234", Decimal("1234")),
    ("1,234.50", Decimal("1234.50")),
    ("$1,234.50", Decimal("1234.50")),
    ("£99", Decimal("99")),
    ("€1,000", Decimal("1000")),
    ("-$5", Decimal("-5")),
    ("$-5", Decimal("-5")),
    ("(500)", Decimal("-500")),
    ("($1,200.25)", Decimal("-1200.25")),
    ("12%", Decimal("0.12")),
    ("  42  ", Decimal("42")),
    ("1 234", Decimal("1234")),             # plain-space thousands separator
    ("1\u00a0234", Decimal("1234")),      # non-breaking space
    ("1\u202f234", Decimal("1234")),      # narrow no-break space
    ("0", Decimal("0")),
    ("1e3", Decimal("1000")),
    ("3.5", Decimal("3.5")),
]

REJECTS = [
    "", "   ", "N/A", "n/a", "see note 3", "TBC", "-", "1.2.3", "$", "%",
    "nan", "NaN", "Infinity", "-inf",     # Decimal() accepts these — must NOT
]


@pytest.mark.parametrize("text,expected", PARSES)
def test_parses(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", REJECTS)
def test_rejects(text):
    assert parse_number(text) is None


def test_percent_of_negative():
    assert parse_number("(12%)") == Decimal("-0.12")


def test_returns_decimal_not_float():
    # 0.1 + 0.2 must be exact when accumulated downstream.
    assert parse_number("0.1") + parse_number("0.2") == Decimal("0.3")


def test_comma_is_always_a_thousands_separator():
    # European decimal-comma is explicitly NOT supported; "1,5" is 15, not 1.5.
    # Documented so a future reader knows this is a decision, not an oversight.
    assert parse_number("1,5") == Decimal("15")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_numeric_parse.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.files.numeric'`

- [ ] **Step 3: Write minimal implementation**

Create `app/files/numeric.py`:

```python
"""Coerce a spreadsheet cell's display text back into a number.

Every cell reaches the aggregator as a string (`readers._cell` stringifies
everything), so summing a column means parsing numbers back out of human
formatting: "$1,234.50", "(500)", "12%", "1 234".

Pure string manipulation into `Decimal` — **never `eval`**, matching
`calculator.py`. `Decimal` rather than `float` because these are money figures
and float accumulation drifts (1204299.9999998).

Blank is NOT special-cased here: "" returns None like any other non-number. The
caller distinguishes 'absent' from 'unparseable' — see aggregate.py.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_CURRENCY = "$€£₹¥"
# plain, non-breaking and narrow-no-break spaces all show up as thousands
# separators in exported sheets.
_SPACES = ("\u00a0", "\u202f", " ")  # nbsp, narrow-nbsp, plain space


def parse_number(text: str) -> Decimal | None:
    """The cell's numeric value, or None if it isn't a number."""
    s = str(text).strip()
    if not s:
        return None

    negative = False
    # Accounting negatives: (500) -> -500
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    percent = s.endswith("%")
    if percent:
        s = s[:-1].strip()

    # Sign may sit either side of the currency symbol: "-$5" and "$-5".
    if s[:1] in "+-":
        negative = negative or s[0] == "-"
        s = s[1:].strip()
    while s[:1] in _CURRENCY:
        s = s[1:].strip()
    if s[:1] in "+-":
        negative = negative or s[0] == "-"
        s = s[1:].strip()

    for ch in _SPACES:
        s = s.replace(ch, "")
    s = s.replace(",", "")
    if not s:
        return None

    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    # Decimal() happily accepts "nan"/"Infinity"; either would poison a sum.
    if not value.is_finite():
        return None

    if percent:
        value = value / Decimal(100)
    return -value if negative else value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_numeric_parse.py -q`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add app/files/numeric.py tests/test_numeric_parse.py
git commit -m "feat(files): parse_number — spreadsheet text to Decimal, never eval"
```

---

### Task 2: Streaming sheet reader

**Files:**
- Modify: `app/files/readers.py` (add after `load_table`, around line 233)
- Test: `tests/test_row_stream.py`

**Interfaces:**
- Consumes: existing `_open_xlsx`, `_resolve_sheet`, `_cell`, `ReadError`, `SheetNotFound`.
- Produces:
  - `RowStream` dataclass: `sheet_name: str`, `headers: list[str]`, `rows: Iterator[list[str]]`, `all_sheets: list[str]`, `total_rows_hint: int | None`
  - `open_sheet_rows(path, *, sheet=None) -> ContextManager[RowStream]` — used by Task 5.

**Why a context manager:** the openpyxl workbook must stay open for the whole iteration and be closed after. A bare generator leaks the handle if the consumer stops early.

- [ ] **Step 1: Write the failing test**

Create `tests/test_row_stream.py`:

```python
"""open_sheet_rows — uncapped, streaming access to one sheet.

Distinct from load_table, which materializes the whole grid then windows it to
~200 rows. Aggregation must see EVERY row, so this path has no caps and never
builds the full grid.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import Workbook

from app.files import readers


def _write_xlsx(tmp_path, sheets: dict[str, list[list]], name="book.xlsx"):
    wb = Workbook()
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    path = tmp_path / name
    wb.save(path)
    return path


def test_yields_every_row_past_the_read_cap(tmp_path):
    rows = [["n"]] + [[i] for i in range(1, 1001)]
    path = _write_xlsx(tmp_path, {"Data": rows})
    with readers.open_sheet_rows(path) as stream:
        collected = list(stream.rows)
    assert stream.headers == ["n"]
    assert len(collected) == 1000            # not capped at READ_MAX_ROWS
    assert collected[0] == ["1"] and collected[-1] == ["1000"]


def test_headers_are_consumed_off_the_front(tmp_path):
    path = _write_xlsx(tmp_path, {"S": [["a", "b"], ["1", "2"]]})
    with readers.open_sheet_rows(path) as stream:
        assert stream.headers == ["a", "b"]
        assert list(stream.rows) == [["1", "2"]]


def test_resolves_named_sheet_and_reports_the_others(tmp_path):
    path = _write_xlsx(tmp_path, {"Q1": [["a"], ["1"]], "Q2": [["a"], ["2"]]})
    with readers.open_sheet_rows(path, sheet="Q2") as stream:
        assert stream.sheet_name == "Q2"
        assert stream.all_sheets == ["Q1", "Q2"]
        assert list(stream.rows) == [["2"]]


def test_unknown_sheet_raises(tmp_path):
    path = _write_xlsx(tmp_path, {"Q1": [["a"], ["1"]]})
    with pytest.raises(readers.SheetNotFound):
        with readers.open_sheet_rows(path, sheet="nope"):
            pass


def test_xlsx_reports_a_total_row_hint(tmp_path):
    path = _write_xlsx(tmp_path, {"S": [["a"], ["1"], ["2"], ["3"]]})
    with readers.open_sheet_rows(path) as stream:
        assert stream.total_rows_hint == 3      # data rows, header excluded


def test_csv_streams_as_one_pseudo_sheet_with_no_hint(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    with readers.open_sheet_rows(path) as stream:
        assert stream.sheet_name == "sales"
        assert stream.all_sheets == ["sales"]
        assert stream.headers == ["a", "b"]
        assert list(stream.rows) == [["1", "2"], ["3", "4"]]
        assert stream.total_rows_hint is None   # unknowable without scanning


def test_csv_delimiter_is_sniffed(tmp_path):
    path = tmp_path / "semi.csv"
    path.write_text("a;b\n1;2\n", encoding="utf-8")
    with readers.open_sheet_rows(path) as stream:
        assert stream.headers == ["a", "b"]


def test_empty_sheet_yields_no_headers_and_no_rows(tmp_path):
    path = _write_xlsx(tmp_path, {"Empty": []})
    with readers.open_sheet_rows(path) as stream:
        assert stream.headers == []
        assert list(stream.rows) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_row_stream.py -q`
Expected: FAIL — `AttributeError: module 'app.files.readers' has no attribute 'open_sheet_rows'`

- [ ] **Step 3: Write minimal implementation**

Add to the imports at the top of `app/files/readers.py`:

```python
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional
```

(`Iterable`/`Optional` are already imported — add `Iterator` and the `contextmanager` import.)

Append this section to `app/files/readers.py`, after `load_table` and its helpers:

```python
# --------------------------------------------------------------------------- #
# Public: stream one sheet's rows UNCAPPED (for aggregation)
#
# load_table above materializes the whole grid then windows it — fine for the
# ~200 rows a model can read, wrong for a 200k-row sum. This path yields row by
# row so memory stays flat regardless of file size, and applies NO caps: the
# scan ceiling lives in aggregate.py where the result is bounded instead.
# --------------------------------------------------------------------------- #
@dataclass
class RowStream:
    sheet_name: str
    headers: list[str]
    rows: Iterator[list[str]]
    all_sheets: list[str]
    total_rows_hint: Optional[int] = None  # data rows; None when unknowable (csv)


@contextmanager
def _csv_stream(path: Path):
    """Open a CSV and yield a csv.reader, sniffing the delimiter as _csv_grid does."""
    try:
        fh = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ReadError(f"could not read CSV: {exc}") from exc
    try:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except _csv.Error:
            dialect = _csv.excel
        yield _csv.reader(fh, dialect)
    finally:
        fh.close()


@contextmanager
def open_sheet_rows(path: Path, *, sheet: Optional[str] = None) -> Iterator[RowStream]:
    """Stream ONE sheet: headers plus an iterator over the remaining rows.

    Must be used as a context manager — the workbook/file handle stays open for
    the life of the iterator and is closed on exit, even if the consumer stops
    early. Trailing all-empty rows are NOT trimmed here (a stream cannot look
    ahead); the aggregator skips fully blank rows instead.
    """
    path = Path(path)

    if not _is_xlsx(path):
        name = path.stem
        with _csv_stream(path) as reader:
            rows = ([_cell(v) for v in row] for row in reader)
            headers = next(rows, [])
            yield RowStream(
                sheet_name=name,
                headers=headers,
                rows=rows,
                all_sheets=[name],
                total_rows_hint=None,
            )
        return

    wb = _open_xlsx(path)
    try:
        all_sheets = list(wb.sheetnames)
        sheet_name = _resolve_sheet(all_sheets, sheet)
        ws = wb[sheet_name]
        hint = max((ws.max_row or 0) - 1, 0)  # minus the header row
        rows = ([_cell(v) for v in row] for row in ws.iter_rows(values_only=True))
        headers = next(rows, [])
        yield RowStream(
            sheet_name=sheet_name,
            headers=headers,
            rows=rows,
            all_sheets=all_sheets,
            total_rows_hint=hint,
        )
    finally:
        wb.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_row_stream.py tests/test_readers.py tests/test_excel_read_tools.py -q`
Expected: PASS. The existing reader tests must stay green — `load_table` is untouched.

- [ ] **Step 5: Commit**

```bash
git add app/files/readers.py tests/test_row_stream.py
git commit -m "feat(files): open_sheet_rows — uncapped streaming access to one sheet"
```

---

### Task 3: Aggregate engine — metrics and filters

**Files:**
- Create: `app/files/aggregate.py`
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Consumes: `parse_number` (Task 1).
- Produces (all used by Tasks 4 and 5):
  - `Metric(column: str, op: str)`, ops `sum|avg|min|max|count`
  - `Filter(column: str, op: str, value: Any)`, ops `eq|ne|contains|gt|gte|lt|lte`
  - `GroupResult(key: str, row_count: int, values: list[Decimal | int | None])`
  - `AggregateResult(groups, total_groups, rows_scanned, rows_matched, parsed, skipped, skipped_examples, blank, scan_truncated, metric_labels)`
  - `UnknownColumn(Exception)` with `.column` and `.headers`
  - `aggregate(headers, rows, *, filters=None, group_by=None, metrics=None, max_scan_rows=MAX_SCAN_ROWS, max_groups=MAX_GROUPS) -> AggregateResult`
  - Constants `MAX_SCAN_ROWS = 200_000`, `MAX_GROUPS = 50`, `VALID_METRIC_OPS`, `VALID_FILTER_OPS`

This task implements everything except grouping; `group_by` is accepted but Task 4 makes it work. Until then all rows fall into one group keyed `""`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_aggregate.py`:

```python
"""The aggregation engine, driven directly over in-memory rows.

Pure — no files, no DB. Every test states the arithmetic it expects, because
the whole point of this tool is that the number is right.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.files.aggregate import (
    Filter,
    Metric,
    UnknownColumn,
    aggregate,
)

HEADERS = ["Region", "Product", "Amount"]
ROWS = [
    ["NSW", "Fixed", "100"],
    ["NSW", "Variable", "$250.50"],
    ["VIC", "Fixed", "(50)"],
    ["VIC", "Variable", "1,000"],
]


def _agg(rows=None, **kw):
    return aggregate(HEADERS, iter(rows if rows is not None else ROWS), **kw)


def test_sum_of_a_column():
    r = _agg(metrics=[Metric("Amount", "sum")])
    assert r.groups[0].values[0] == Decimal("1300.50")
    assert r.rows_matched == 4


def test_avg_min_max_count():
    r = _agg(metrics=[
        Metric("Amount", "avg"),
        Metric("Amount", "min"),
        Metric("Amount", "max"),
        Metric("Amount", "count"),
    ])
    avg, mn, mx, cnt = r.groups[0].values
    assert avg == Decimal("1300.50") / 4
    assert mn == Decimal("-50")
    assert mx == Decimal("1000")
    assert cnt == 4


def test_no_metrics_returns_the_row_count():
    r = _agg()
    assert r.groups[0].row_count == 4
    assert r.groups[0].values == []


def test_metric_labels_describe_the_columns():
    r = _agg(metrics=[Metric("Amount", "sum"), Metric("Amount", "avg")])
    assert r.metric_labels == ["sum(Amount)", "avg(Amount)"]


def test_column_names_are_case_insensitive():
    r = _agg(metrics=[Metric("amount", "sum")])
    assert r.groups[0].values[0] == Decimal("1300.50")


def test_unknown_metric_column_raises_with_the_real_headers():
    with pytest.raises(UnknownColumn) as exc:
        _agg(metrics=[Metric("Total", "sum")])
    assert exc.value.column == "Total"
    assert exc.value.headers == HEADERS


def test_unknown_filter_column_raises():
    with pytest.raises(UnknownColumn):
        _agg(filters=[Filter("Zone", "eq", "NSW")])


def test_invalid_op_raises_value_error():
    with pytest.raises(ValueError):
        _agg(metrics=[Metric("Amount", "median")])


# --- the three-outcome rule ------------------------------------------------- #
def test_blank_cells_are_absent_not_skipped():
    rows = [["NSW", "Fixed", "100"], ["NSW", "Fixed", ""], ["NSW", "Fixed", "200"]]
    r = _agg(rows, metrics=[Metric("Amount", "sum"), Metric("Amount", "avg")])
    assert r.groups[0].values[0] == Decimal("300")
    assert r.groups[0].values[1] == Decimal("150")   # denominator 2, not 3
    assert r.skipped.get("Amount", 0) == 0
    assert r.blank["Amount"] == 1


def test_unparseable_cells_are_counted_and_named():
    rows = [
        ["NSW", "Fixed", "100"],
        ["NSW", "Fixed", "N/A"],
        ["NSW", "Fixed", "see note 3"],
        ["NSW", "Fixed", "200"],
    ]
    r = _agg(rows, metrics=[Metric("Amount", "sum")])
    assert r.groups[0].values[0] == Decimal("300")
    assert r.skipped["Amount"] == 2
    assert r.parsed["Amount"] == 2
    assert r.skipped_examples["Amount"] == ["N/A", "see note 3"]


def test_skipped_examples_are_capped_at_three():
    rows = [["NSW", "Fixed", f"bad{i}"] for i in range(10)]
    r = _agg(rows, metrics=[Metric("Amount", "sum")])
    assert len(r.skipped_examples["Amount"]) == 3


def test_count_does_not_mark_text_as_skipped():
    # count(column) counts non-blank cells; a text column is a legitimate target.
    rows = [["NSW", "Fixed", "x"], ["NSW", "Fixed", ""], ["NSW", "Fixed", "y"]]
    r = _agg(rows, metrics=[Metric("Amount", "count")])
    assert r.groups[0].values[0] == 2
    assert r.skipped.get("Amount", 0) == 0


def test_a_column_with_nothing_parseable_yields_none_not_zero():
    rows = [["NSW", "Fixed", "N/A"], ["NSW", "Fixed", "TBC"]]
    r = _agg(rows, metrics=[Metric("Amount", "sum")])
    assert r.groups[0].values[0] is None   # zero would be a lie


def test_fully_blank_rows_are_skipped_entirely():
    rows = [["NSW", "Fixed", "100"], ["", "", ""], ["NSW", "Fixed", "200"]]
    r = _agg(rows, metrics=[Metric("Amount", "sum")])
    assert r.rows_matched == 2
    assert r.groups[0].values[0] == Decimal("300")


# --- filters ---------------------------------------------------------------- #
def test_text_filter_eq_is_case_insensitive():
    r = _agg(filters=[Filter("Region", "eq", "nsw")], metrics=[Metric("Amount", "sum")])
    assert r.rows_matched == 2
    assert r.groups[0].values[0] == Decimal("350.50")


def test_filter_ne_and_contains():
    assert _agg(filters=[Filter("Region", "ne", "NSW")]).rows_matched == 2
    assert _agg(filters=[Filter("Product", "contains", "var")]).rows_matched == 2


def test_numeric_filters_compare_numerically():
    r = _agg(filters=[Filter("Amount", "gte", 250)], metrics=[Metric("Amount", "sum")])
    assert r.rows_matched == 2                       # 250.50 and 1,000
    assert r.groups[0].values[0] == Decimal("1250.50")
    assert _agg(filters=[Filter("Amount", "lt", 0)]).rows_matched == 1


def test_filters_are_anded():
    r = _agg(filters=[Filter("Region", "eq", "NSW"), Filter("Amount", "gt", 200)])
    assert r.rows_matched == 1


def test_eq_compares_numerically_when_both_sides_are_numbers():
    # "1,000" == 1000 must match; a plain string compare would silently miss it.
    r = _agg(filters=[Filter("Amount", "eq", 1000)])
    assert r.rows_matched == 1


def test_row_excluded_when_its_filter_cell_will_not_parse():
    rows = [["NSW", "Fixed", "100"], ["NSW", "Fixed", "N/A"]]
    r = _agg(rows, filters=[Filter("Amount", "gt", 0)])
    assert r.rows_matched == 1
    assert r.skipped["Amount"] == 1


# --- scan ceiling ----------------------------------------------------------- #
def test_scan_ceiling_stops_and_says_so():
    rows = [["NSW", "Fixed", "1"] for _ in range(50)]
    r = _agg(rows, metrics=[Metric("Amount", "sum")], max_scan_rows=10)
    assert r.scan_truncated is True
    assert r.rows_scanned == 10
    assert r.groups[0].values[0] == Decimal("10")


def test_scan_not_truncated_when_under_the_ceiling():
    r = _agg(metrics=[Metric("Amount", "sum")], max_scan_rows=10)
    assert r.scan_truncated is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_aggregate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.files.aggregate'`

- [ ] **Step 3: Write minimal implementation**

Create `app/files/aggregate.py`:

```python
"""Filter → group → accumulate over a stream of spreadsheet rows.

Pure: takes headers plus any iterable of string rows, returns numbers. No file
I/O, no DB, no contextvars — `open_sheet_rows` feeds it and the tool adapter
formats it.

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
from typing import Any, Iterable, Optional

from .numeric import parse_number

MAX_SCAN_ROWS = 200_000
MAX_GROUPS = 50
MAX_SKIPPED_EXAMPLES = 3

VALID_METRIC_OPS = ("sum", "avg", "min", "max", "count")
VALID_FILTER_OPS = ("eq", "ne", "contains", "gt", "gte", "lt", "lte")
_NUMERIC_FILTER_OPS = ("gt", "gte", "lt", "lte")


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
    values: list[Optional[Decimal | int]]


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


def _norm(text: str) -> str:
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

    def value(self) -> Optional[Decimal | int]:
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

    parsed: dict[str, int] = {}
    skipped: dict[str, int] = {}
    skipped_examples: dict[str, list[str]] = {}
    blank: dict[str, int] = {}

    def _note_skip(column: str, raw: str) -> None:
        skipped[column] = skipped.get(column, 0) + 1
        ex = skipped_examples.setdefault(column, [])
        if len(ex) < MAX_SKIPPED_EXAMPLES and raw not in ex:
            ex.append(raw)

    # group key -> (display key, [accumulators], row_count)
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
            groups[key_norm] = entry
        display, accs, count = entry
        groups[key_norm] = (display, accs, count + 1)

        for m, i, acc in zip(metrics, metric_idx, accs):
            raw = _cell_at(row, i)
            if not raw.strip():
                blank[m.column] = blank.get(m.column, 0) + 1
                continue
            if m.op == "count":
                acc.add_present()
                continue
            value = parse_number(raw)
            if value is None:
                _note_skip(m.column, raw.strip())
                continue
            parsed[m.column] = parsed.get(m.column, 0) + 1
            acc.add_number(value)

    # A metrics-only call still reports one implicit group.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_aggregate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/files/aggregate.py tests/test_aggregate.py
git commit -m "feat(files): aggregate engine — metrics, AND filters, honest cell accounting"
```

---

### Task 4: Group-by, ordering and the group cap

**Files:**
- Modify: `app/files/aggregate.py` (the `aggregate` return path)
- Test: `tests/test_aggregate.py` (append)

**Interfaces:**
- Consumes: everything from Task 3.
- Produces: `AggregateResult.groups` ordered by the first metric descending and truncated to `max_groups`, with `total_groups` reporting the pre-cap count.

Grouping itself already works from Task 3 (`group_idx`). This task adds deterministic ordering and the cap.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_aggregate.py`:

```python
# --- grouping --------------------------------------------------------------- #
def test_group_by_splits_and_totals_each_key():
    r = _agg(group_by="Region", metrics=[Metric("Amount", "sum")])
    assert {g.key: g.values[0] for g in r.groups} == {
        "VIC": Decimal("950"),
        "NSW": Decimal("350.50"),
    }


def test_groups_are_ordered_by_the_first_metric_descending():
    r = _agg(group_by="Region", metrics=[Metric("Amount", "sum")])
    assert [g.key for g in r.groups] == ["VIC", "NSW"]


def test_groups_order_by_row_count_when_there_are_no_metrics():
    rows = ROWS + [["NSW", "Fixed", "1"]]
    r = _agg(rows, group_by="Region")
    assert [g.key for g in r.groups] == ["NSW", "VIC"]


def test_group_keys_are_matched_case_insensitively_but_displayed_as_first_seen():
    rows = [["NSW", "Fixed", "1"], ["nsw", "Fixed", "2"]]
    r = _agg(rows, group_by="Region", metrics=[Metric("Amount", "sum")])
    assert len(r.groups) == 1
    assert r.groups[0].key == "NSW"          # first spelling seen
    assert r.groups[0].values[0] == Decimal("3")


def test_blank_group_key_renders_as_blank_label():
    rows = [["", "Fixed", "5"]]
    r = _agg(rows, group_by="Region", metrics=[Metric("Amount", "sum")])
    assert r.groups[0].key == "(blank)"


def test_group_cap_truncates_but_reports_the_true_total():
    rows = [[f"R{i}", "Fixed", str(i)] for i in range(1, 61)]
    r = _agg(rows, group_by="Region", metrics=[Metric("Amount", "sum")], max_groups=50)
    assert len(r.groups) == 50
    assert r.total_groups == 60
    assert r.groups[0].key == "R60"          # largest kept, not an arbitrary 50


def test_groups_with_no_parseable_values_sort_last_deterministically():
    rows = [
        ["NSW", "Fixed", "N/A"],
        ["VIC", "Fixed", "10"],
        ["QLD", "Fixed", "TBC"],
    ]
    r = _agg(rows, group_by="Region", metrics=[Metric("Amount", "sum")])
    assert r.groups[0].key == "VIC"
    assert [g.key for g in r.groups[1:]] == ["NSW", "QLD"]   # None -> key order
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_aggregate.py -q`
Expected: FAIL on the ordering and cap tests (grouping itself passes; order is insertion order and nothing truncates).

- [ ] **Step 3: Write minimal implementation**

In `app/files/aggregate.py`, replace the `results = [...]` / `return AggregateResult(...)` block at the end of `aggregate` with:

```python
    results = [
        GroupResult(key=display, row_count=count, values=[a.value() for a in accs])
        for display, accs, count in groups.values()
    ]

    # Deterministic order: biggest first by the first metric (row count when
    # there are no metrics). Groups whose metric is None (nothing parseable)
    # sort last, then by key, so the output never depends on dict insertion
    # order — tests and users both see a stable list.
    def _sort_key(g: GroupResult):
        primary = g.values[0] if g.values else None
        if primary is None:
            primary = g.row_count if not g.values else None
        if primary is None:
            return (1, Decimal(0), g.key)
        return (0, -Decimal(primary), g.key)

    results.sort(key=_sort_key)
    total_groups = len(results)
    if len(results) > max_groups:
        results = results[:max_groups]

    return AggregateResult(
        groups=results,
        total_groups=total_groups,
        rows_scanned=rows_scanned,
        rows_matched=rows_matched,
        metric_labels=[m.label for m in metrics],
        parsed=parsed,
        skipped=skipped,
        skipped_examples=skipped_examples,
        blank=blank,
        scan_truncated=scan_truncated,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_aggregate.py -q`
Expected: PASS, all cases including Task 3's.

- [ ] **Step 5: Commit**

```bash
git add app/files/aggregate.py tests/test_aggregate.py
git commit -m "feat(files): group-by ordering + 50-group cap that reports the true total"
```

---

### Task 5: The `aggregate_excel` tool

**Files:**
- Create: `app/tools/local/aggregate_excel.py`
- Modify: `app/tools/local/__init__.py`
- Modify: `CLAUDE.md` (tool list + the layout bullet)
- Test: `tests/test_aggregate_excel_tool.py`

**Interfaces:**
- Consumes: `readers.open_sheet_rows` (Task 2), `aggregate`/`Metric`/`Filter`/`UnknownColumn` (Tasks 3–4), existing `resolve_file` from `app.files.store`.
- Produces: `SPEC` (a `LocalToolSpec` named `aggregate_excel`) registered in `LOCAL_TOOLS`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_aggregate_excel_tool.py`:

```python
"""End-to-end tests for aggregate_excel through the tool fn.

No DB: uses the in-memory fallback file store as the file source, exactly like
tests/test_excel_read_tools.py.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.files.store import CSV_MEDIA_TYPE, XLSX_MEDIA_TYPE, file_store
from app.tools.local import aggregate_excel


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _make_xlsx(sheets: dict[str, list[list]], filename="book.xlsx") -> str:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    rec = asyncio.run(
        file_store.save(buf.getvalue(), filename=filename, media_type=XLSX_MEDIA_TYPE)
    )
    return rec.id


def _make_csv(text: str, filename="data.csv") -> str:
    rec = asyncio.run(
        file_store.save(text.encode("utf-8"), filename=filename, media_type=CSV_MEDIA_TYPE)
    )
    return rec.id


def _run(args):
    return asyncio.run(aggregate_excel.SPEC.func(args))


SHEET = [
    ["Region", "Product", "Amount"],
    ["NSW", "Fixed", 100],
    ["NSW", "Variable", 250],
    ["VIC", "Fixed", 400],
]


# --- guards ----------------------------------------------------------------- #
def test_missing_file_id_errors():
    assert _run({}).startswith("ERROR")


def test_unknown_file_id_errors_without_leaking():
    assert _run({"file_id": "deadbeef"}).startswith("ERROR: no such file")


def test_unknown_column_names_the_real_headers():
    fid = _make_xlsx({"S": SHEET})
    out = _run({"file_id": fid, "metrics": [{"column": "Total", "op": "sum"}]})
    assert out.startswith("ERROR")
    assert "Total" in out and "Region, Product, Amount" in out


def test_bad_metric_op_errors():
    fid = _make_xlsx({"S": SHEET})
    out = _run({"file_id": fid, "metrics": [{"column": "Amount", "op": "median"}]})
    assert out.startswith("ERROR") and "median" in out


def test_unknown_sheet_errors():
    fid = _make_xlsx({"S": SHEET})
    assert _run({"file_id": fid, "sheet": "nope"}).startswith("ERROR")


# --- results ---------------------------------------------------------------- #
def test_sum_over_a_sheet():
    fid = _make_xlsx({"S": SHEET})
    out = _run({"file_id": fid, "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "750" in out
    assert "3 matching row" in out


def test_group_by_renders_a_row_per_group():
    fid = _make_xlsx({"S": SHEET})
    out = _run({
        "file_id": fid,
        "group_by": "Region",
        "metrics": [{"column": "Amount", "op": "sum"}],
    })
    assert "sum(Amount)" in out
    assert "VIC" in out and "NSW" in out
    assert "400" in out and "350" in out


def test_filters_apply():
    fid = _make_xlsx({"S": SHEET})
    out = _run({
        "file_id": fid,
        "filters": [{"column": "Region", "op": "eq", "value": "NSW"}],
        "metrics": [{"column": "Amount", "op": "sum"}],
    })
    assert "350" in out


def test_csv_works_the_same_way():
    fid = _make_csv("Region,Amount\nNSW,100\nVIC,250\n")
    out = _run({"file_id": fid, "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "350" in out


def test_multi_sheet_names_the_other_sheets():
    fid = _make_xlsx({"Q1": SHEET, "Q2": SHEET})
    out = _run({"file_id": fid, "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "Q1" in out and "Q2" in out


def test_skipped_cells_are_reported_in_the_footer():
    sheet = [["Region", "Amount"], ["NSW", 100], ["NSW", "N/A"], ["VIC", 200]]
    fid = _make_xlsx({"S": sheet})
    out = _run({"file_id": fid, "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "300" in out
    assert "1 skipped" in out and "N/A" in out


def test_beats_the_read_window_on_a_large_sheet():
    # THE regression this tool exists for: 500 rows > READ_MAX_ROWS (200), so
    # read_excel could only ever see a slice. sum(1..500) = 125250.
    sheet = [["n"]] + [[i] for i in range(1, 501)]
    fid = _make_xlsx({"S": sheet})
    out = _run({"file_id": fid, "metrics": [{"column": "n", "op": "sum"}]})
    assert "125,250" in out
    assert "500 matching row" in out


def test_no_metrics_reports_a_row_count():
    fid = _make_xlsx({"S": SHEET})
    out = _run({"file_id": fid, "filters": [{"column": "Region", "op": "eq", "value": "NSW"}]})
    assert "2 matching row" in out


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "aggregate_excel" for spec in LOCAL_TOOLS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_aggregate_excel_tool.py -q`
Expected: FAIL — `ImportError: cannot import name 'aggregate_excel' from 'app.tools.local'`

- [ ] **Step 3: Write minimal implementation**

Create `app/tools/local/aggregate_excel.py`:

```python
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
            raise ValueError("each metric must be an object like {\"column\": \"Amount\", \"op\": \"sum\"}.")
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
            raise ValueError("each filter must be an object like {\"column\": \"Region\", \"op\": \"eq\", \"value\": \"NSW\"}.")
        column = str(item.get("column", "")).strip()
        op = str(item.get("op", "")).strip().lower()
        if not column:
            raise ValueError("each filter needs a 'column'.")
        if op not in VALID_FILTER_OPS:
            raise ValueError(f"unknown filter op '{op}' (use: {', '.join(VALID_FILTER_OPS)}).")
        out.append(Filter(column=column, op=op, value=item.get("value")))
    return out


def _fmt_number(value: Optional[Decimal | int]) -> str:
    """Thousands separators; at most 2 decimals; integers stay integers."""
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    quantized = value.quantize(Decimal("0.01")) if value != value.to_integral_value() else value
    if quantized == quantized.to_integral_value():
        return f"{int(quantized):,}"
    return f"{quantized:,.2f}"


def _format(result: AggregateResult, stream: readers.RowStream, *, has_filters: bool) -> str:
    lines: list[str] = []

    head = f"Sheet '{stream.sheet_name}' — {result.rows_matched:,} matching row(s)"
    if has_filters and result.rows_scanned != result.rows_matched:
        head += f" of {result.rows_scanned:,} scanned"
    lines.append(head + ".")

    others = [s for s in stream.all_sheets if s != stream.sheet_name]
    if others:
        lines.append(
            f"This workbook has {len(stream.all_sheets)} sheets: "
            f"{', '.join(stream.all_sheets)}. Pass sheet=\"…\" to aggregate another."
        )
    lines.append("")

    grouped = result.groups and result.groups[0].key != "(all)"
    headers = (["group"] if grouped else []) + ["rows"] + result.metric_labels
    rows = [
        (([g.key] if grouped else []) + [f"{g.row_count:,}"] + [_fmt_number(v) for v in g.values])
        for g in result.groups
    ]

    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
              for i in range(len(headers))]
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

    # Provenance — always present, per column touched by a numeric metric.
    for column, count in result.skipped.items():
        examples = ", ".join(f'"{e}"' for e in result.skipped_examples.get(column, []))
        counted = result.parsed.get(column, 0)
        note = (
            f"Counted {counted:,} of {result.rows_matched:,} matching rows for "
            f"'{column}'; {count:,} skipped as non-numeric"
        )
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
            return _format(result, stream, has_filters=bool(filters))
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
            "sheet": {"type": "string", "description": "Sheet name or 1-based index (default: first sheet)."},
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
```

- [ ] **Step 2b: Register the tool**

In `app/tools/local/__init__.py`, add `aggregate_excel` to the import block (alphabetically first) and `aggregate_excel.SPEC` to `LOCAL_TOOLS`:

```python
from . import (
    aggregate_excel,
    calculator,
    chart,
    ...
)

LOCAL_TOOLS: list[LocalToolSpec] = [
    time.SPEC,
    excel.SPEC,
    ...
    inspect_excel.SPEC,
    read_excel.SPEC,
    aggregate_excel.SPEC,
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_aggregate_excel_tool.py -q`
Expected: PASS.

Then the whole suite, since a new tool changes the tool list that `test_tool_filter.py` and `test_agent_loop.py` may assert on:

Run: `.venv/bin/pytest -q`
Expected: PASS. If a test asserts an exact tool count or list, update it to include `aggregate_excel`.

- [ ] **Step 5: Update CLAUDE.md**

In the `## Conventions / gotchas` section, extend the Excel bullet with a sentence:

```markdown
  Tools `inspect_excel` (every sheet's structure), `read_excel` (one sheet,
  paged/projected) + **`aggregate_excel`** (sum/avg/min/max/count, optional
  one-level `group_by`, AND-only filters) — aggregate reads EVERY row and is the
  correct tool for any total, because `read_excel`'s ~200-row cap makes
  model-side arithmetic silently wrong on bigger sheets. Numbers are parsed by
  `app/files/numeric.py` (currency/commas/percent/accounting negatives → Decimal,
  never eval) and every result reports rows counted vs cells skipped.
```

- [ ] **Step 6: Commit**

```bash
git add app/tools/local/aggregate_excel.py app/tools/local/__init__.py tests/test_aggregate_excel_tool.py CLAUDE.md
git commit -m "feat(tools): aggregate_excel — correct totals over the whole sheet"
```

---

### Task 6: The eval set

**Files:**
- Create: `tests/test_aggregate_eval.py`
- Modify: `docs/superpowers/specs/2026-08-08-aggregate-excel-design.md` (record the baseline)

**Interfaces:**
- Consumes: the tool fn from Task 5.
- Produces: nothing importable — this is the spec's Evaluation section made executable.

Fixtures are generated in-test rather than checked in as binaries, so a reviewer can read the expected answer next to the data that produces it.

- [ ] **Step 1: Write the eval**

Create `tests/test_aggregate_eval.py`:

```python
"""The aggregate_excel eval set (see the design doc's Evaluation section).

8 labelled cases: a sheet plus the answer we expect. These are deterministic —
the engine is not a model — so the target is 8/8 and any failure is a bug, not
variance. When a real-world sheet breaks the parser, add it here first.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.files.store import XLSX_MEDIA_TYPE, file_store
from app.tools.local import aggregate_excel


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _sheet(rows: list[list]) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    rec = asyncio.run(
        file_store.save(buf.getvalue(), filename="eval.xlsx", media_type=XLSX_MEDIA_TYPE)
    )
    return rec.id


def _run(args):
    return asyncio.run(aggregate_excel.SPEC.func(args))


def test_eval_1_small_sheet_fits_the_read_window():
    rows = [["Amount"]] + [[i] for i in range(1, 51)]        # sum(1..50) = 1275
    out = _run({"file_id": _sheet(rows), "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "1,275" in out


def test_eval_2_large_sheet_exceeds_the_read_window():
    rows = [["Amount"]] + [[i] for i in range(1, 1201)]      # sum(1..1200) = 720600
    out = _run({"file_id": _sheet(rows), "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "720,600" in out
    assert "1,200 matching row" in out


def test_eval_3_group_by_category():
    rows = [["Region", "Amount"]] + [
        ["NSW" if i % 2 else "VIC", 10] for i in range(1, 401)
    ]                                                        # 200 each -> 2000 each
    out = _run({
        "file_id": _sheet(rows),
        "group_by": "Region",
        "metrics": [{"column": "Amount", "op": "sum"}],
    })
    assert out.count("2,000") == 2


def test_eval_4_currency_formatted_column():
    rows = [["Amount"], ["$1,234.50"], ["$765.50"]]          # 2000.00
    out = _run({"file_id": _sheet(rows), "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "2,000" in out


def test_eval_5_na_and_blanks_are_accounted_for():
    rows = [["Amount"], [100], ["N/A"], [None], [300]]       # sum 400, 1 skipped, 1 blank
    out = _run({"file_id": _sheet(rows), "metrics": [
        {"column": "Amount", "op": "sum"}, {"column": "Amount", "op": "avg"},
    ]})
    assert "400" in out
    assert "200" in out                                       # avg over 2, not 4
    assert "1 skipped" in out and "N/A" in out
    assert "1 blank" in out


def test_eval_6_accounting_negatives():
    rows = [["Amount"], [1000], ["(250)"], ["(750)"], ["(500)"]]   # 1000-1500 = -500
    out = _run({"file_id": _sheet(rows), "metrics": [{"column": "Amount", "op": "sum"}]})
    assert "-500" in out


def test_eval_7_filtered_subset():
    rows = [["Region", "Amount"]] + [["NSW", 100]] * 3 + [["VIC", 100]] * 7
    out = _run({
        "file_id": _sheet(rows),
        "filters": [{"column": "Region", "op": "eq", "value": "NSW"}],
        "metrics": [{"column": "Amount", "op": "sum"}],
    })
    assert "300" in out
    assert "3 matching row" in out


def test_eval_8_more_groups_than_the_cap():
    rows = [["Region", "Amount"]] + [[f"R{i}", i] for i in range(1, 61)]
    out = _run({
        "file_id": _sheet(rows),
        "group_by": "Region",
        "metrics": [{"column": "Amount", "op": "sum"}],
    })
    assert "top 50 of 60 groups" in out
    assert "R60" in out            # largest kept
```

- [ ] **Step 2: Run the eval**

Run: `.venv/bin/pytest tests/test_aggregate_eval.py -q`
Expected: 8 passed. If a case fails, fix the engine — the expected answers are arithmetic, not opinions. Case 6's assertion is loose about formatting; tighten it to the actual rendered cell once you see the real output.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 4: Record the baseline in the spec**

In `docs/superpowers/specs/2026-08-08-aggregate-excel-design.md`, under **Eval**, replace "Baseline to record on first run" with the actual result, e.g. `Baseline 2026-08-08: 8/8 passing (tests/test_aggregate_eval.py).`

- [ ] **Step 5: Commit**

```bash
git add tests/test_aggregate_eval.py docs/superpowers/specs/2026-08-08-aggregate-excel-design.md
git commit -m "test: aggregate_excel eval set — 8 labelled cases, baseline recorded"
```

---

## Manual verification

After Task 6, confirm it works through the real stack rather than only in tests:

1. Start the gateway: `.venv/bin/uvicorn app.main:app --reload --port 8000`
2. Log in as `admin@example.com` / `supersecret123`, upload a spreadsheet with more than 200 rows via `POST /v1/files`.
3. `POST /v1/chat` with `{"message": "what's the total of the Amount column?", "file_ids": ["<id>"], "stream": false}`.
4. Check the returned `trace`: the model should call `aggregate_excel`, not `read_excel`. If it reaches for `read_excel` instead, the fix is the tool description (Task 5) or the one-line `SYSTEM_PROMPT` in `app/agent/loop.py:35` — not the engine.
5. Confirm the total matches the file.

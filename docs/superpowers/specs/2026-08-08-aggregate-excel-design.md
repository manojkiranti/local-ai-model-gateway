# aggregate_excel — Design

**Date:** 2026-08-08
**Status:** approved, implementing

## Goal
Answer "what's the total?" and "total by category?" over an uploaded spreadsheet
**correctly, at any file size**.

Today the model can only see what `read_excel` returns, and that is capped at
`READ_MAX_ROWS=200` / `READ_MAX_CHARS=40_000`. On a 5,000-row sheet the model
sees a 200-row slice and any total it reports is **quietly wrong** — it has no
signal that it is extrapolating. This tool moves the arithmetic into the gateway
and returns a bounded result, so the answer no longer depends on how much of the
sheet fits in context.

Scope is deliberately narrow: totals + one-level group-by + AND filters. Not a
query engine.

## Approach (chosen forks)
- **Interface: structured JSON params**, not model-authored SQL. A fixed schema
  has no parser and no injection surface, adds no dependency, and small local
  models drive typed fields far more reliably than they write correct SQL.
  Rejected: in-memory `sqlite3` + `SELECT` — more expressive, but every column
  lands as TEXT so correctness moves into fragile `CAST` logic (a bad cast is
  silently `0`), and wrong-but-valid SQL reproduces the exact silent-wrong
  failure this tool exists to remove.
- **Unparseable cells: parse leniently, always report.** Never refuse a whole
  call over one stray `N/A`; never skip cells silently.
- **Streaming read.** `load_table` materializes the entire grid then windows it —
  fine at 200 rows, not at 200,000. Aggregation gets its own row iterator.
- **One sheet per call**, matching `read_excel`. Cross-sheet = call twice.

## Components

### `app/files/numeric.py` (new, pure)
`parse_number(text) -> Decimal | None`

Coercion policy, isolated in its own module because it is the trust-critical
piece and deserves its own test table:
- strip whitespace, leading currency symbols (`$ € £ ₹`), thousands separators.
- trailing `%` → divide by 100 (`"12%"` → `0.12`).
- `(500)` → `-500` (accounting negatives).
- pure string manipulation into `Decimal` — **never `eval`**, consistent with
  `calculator.py`.

Returns `None` for anything it cannot read. Blank is handled by the caller, not
here — see the three-outcome rule below.

**`Decimal`, not `float`:** float accumulation drifts on money
(`1204299.9999998`). Slower over a 200k-row scan; correct, which wins for
financial figures.

### `app/files/readers.py` (extend)
`iter_sheet_rows(path, *, sheet=None) -> tuple[list[str], Iterator[list[str]]]`

Headers plus a **streaming** row iterator — yields off `ws.iter_rows()` /
the `csv` reader without materializing the grid, so memory stays flat regardless
of file size. Reuses the existing `_open_xlsx`, `_csv_grid` dialect sniffing,
`_resolve_sheet`, and `_cell` coercion. Existing caps do **not** apply here; the
scan ceiling below does.

### `app/files/aggregate.py` (new, pure — no DB, no contextvars)
The engine: filter → group → aggregate over a row stream.

- **metrics:** `[{column, op}]`, op ∈ `sum|avg|min|max|count`. `count` on a
  column counts non-blank cells. Omitted metrics → row count, which answers
  "how many rows match?".
- **filters:** `[{column, op, value}]`, op ∈ `eq|ne|contains|gt|gte|lt|lte`.
  **AND only** — no OR, no nesting. Text comparison is case-insensitive, using
  the existing `_norm()` convention. Numeric ops parse both sides via
  `parse_number`; a row whose filter cell will not parse is excluded and counted.
- **group_by:** ONE column. Not a list — multi-key grouping is where a small
  model starts producing confidently wrong pivots. Add later if genuinely missed.
- **Result:** groups sorted by the first metric descending, **capped at 50**
  with the remainder reported as a count. Blank keys render `(blank)`.

#### The three-outcome rule (why the output can be trusted)
| Cell | Treatment |
|---|---|
| Blank | **Absent.** Excluded from sums and from `avg`'s denominator. Not an error, not reported. |
| Parses | Counted. |
| Unparseable | Excluded, **counted, and named.** |

Every result carries its provenance footer:

```
summed 4,812 of 4,900 rows; 88 skipped in 'Amount' (e.g. "N/A", "see note 3", "TBC")
```

Up to 3 offending values are shown so the user can fix the sheet.

#### Scan ceiling
`MAX_SCAN_ROWS = 200_000`. Above it the tool aggregates what it scanned and says
so plainly:

```
stopped at 200,000 of 340,000 rows — this result is partial
```

A refusal would be safer but useless; a *stated* partial preserves the
never-silently-wrong property while still answering.

### `app/tools/local/aggregate_excel.py` (new)
Thin adapter, same shape as `read_excel.py`: validate args → `resolve_file`
(owner-scoped) → call the engine → format a pipe-separated text table in
`read_excel`'s house style. Registered by adding `aggregate_excel.SPEC` to
`LOCAL_TOOLS`; `registry.py` does not change.

## Errors
- Unknown / unowned `file_id`, `SheetNotFound`, `ReadError` → reuse
  `read_excel`'s existing messages verbatim.
- **Unknown column → error naming the real headers:**
  `ERROR: no column 'Total' (have: Date, Product, Region, Amount)`. Header
  guessing is the failure the model will hit most; this makes it self-correcting
  in one turn.

## Security
Owner-scoped via the existing `resolve_file` contextvar — an unowned id returns
the standard error, never data. No `eval` anywhere. Formulas still never
evaluated (`data_only=True` inherited from `_open_xlsx`). Output is bounded by
the group cap regardless of input size; memory is bounded by streaming.

## Testing
- `tests/test_numeric_parse.py` — ~20 cases covering currency, separators,
  percent, accounting negatives, blanks, and junk.
- `tests/test_aggregate.py` — engine over a fixture grid: every op, filters,
  grouping, blank-vs-unparseable accounting, group cap, scan ceiling.
- `tests/test_aggregate_excel_tool.py` — end-to-end on `.xlsx` and `.csv`
  fixtures: bad `file_id`, unknown column, and **a >200-row sheet with a known
  correct total** — the regression that proves this beats the capped
  `read_excel` window.

## Evaluation & Improvement

**Success metric.** Aggregation accuracy on sheets larger than the read window:
the share of totals/group-bys the model reports that match the ground truth
computed directly from the file. The failure this replaces is a wrong number
delivered confidently, so a wrong answer counts strictly worse than a refusal or
a stated-partial.

**Eval.** 8 labelled cases in `tests/fixtures/aggregate_eval/`, each a sheet plus
the expected answer, run as a normal pytest:
1. clean 50-row sheet, single sum (fits the read window — must still be right)
2. 1,200-row sheet, single sum (exceeds the window — the core case)
3. same sheet grouped by a category column
4. currency-formatted column (`$1,234.50`)
5. column containing `N/A` and blanks — checks the skipped count and denominator
6. accounting negatives `(500)`
7. filtered subset (`Region eq NSW`)
8. group-by producing >50 groups — checks the cap and remainder line

Scored as exact match on the numbers plus presence of the correct provenance
footer. Baseline to record on first run; target is 8/8 on the engine, since these
are deterministic — any failure is a bug, not variance.

**Feedback capture.** Every call already lands in the turn `trace` JSONB on
`chat_messages` with its args and result. The skipped-cell counts and any
partial-scan notice are in that stored result text, so recurring real-world
coercion gaps (a currency symbol or date format the parser misses) are
greppable from Postgres without new instrumentation.

**Review loop.** Monthly, or sooner if a user reports a wrong number: grep the
stored traces for non-zero skipped counts, look at what actually failed to parse,
and extend `parse_number` plus the eval set with those real cases.

## Not in scope
Multi-column group-by, OR/nested filters, joins across sheets, computed columns,
date bucketing (group by month), writing results to a file, and charting the
result. Each is additive later; none block the core fix.

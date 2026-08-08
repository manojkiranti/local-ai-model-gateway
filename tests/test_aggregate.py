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


def test_two_metrics_on_one_column_count_each_cell_once():
    # Regression: the accounting is per CELL, not per metric. Keying it per
    # metric made sum+avg on the same column report double the real counts,
    # which would have made the provenance footer lie.
    rows = [
        ["NSW", "Fixed", "100"],
        ["NSW", "Fixed", "N/A"],
        ["NSW", "Fixed", ""],
    ]
    r = _agg(rows, metrics=[Metric("Amount", "sum"), Metric("Amount", "avg")])
    assert r.parsed["Amount"] == 1
    assert r.skipped["Amount"] == 1
    assert r.blank["Amount"] == 1


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

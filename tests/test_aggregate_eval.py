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
    # Two columns on purpose: a blank Amount beside a filled Region is a blank
    # CELL (counted, excluded from avg's denominator). In a single-column sheet
    # the same cell would make the whole ROW blank, and blank rows are dropped
    # before the accounting ever sees them — a different, also-correct path.
    rows = [["Region", "Amount"], ["NSW", 100], ["NSW", "N/A"], ["NSW", None], ["NSW", 300]]
    out = _run({"file_id": _sheet(rows), "metrics": [
        {"column": "Amount", "op": "sum"}, {"column": "Amount", "op": "avg"},
    ]})
    assert "4 matching row" in out
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

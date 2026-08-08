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


def test_malformed_metrics_shape_errors():
    fid = _make_xlsx({"S": SHEET})
    assert _run({"file_id": fid, "metrics": "sum"}).startswith("ERROR")
    assert _run({"file_id": fid, "metrics": [{"op": "sum"}]}).startswith("ERROR")


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

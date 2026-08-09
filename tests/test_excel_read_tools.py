"""Offline tests for inspect_excel / read_excel.

No DB: uses the in-memory fallback file store as the file source (resolve_file
falls back to file_store.get when no PostgresFileSource is installed). Builds a
real .xlsx on disk via the store, then drives the tool fns directly.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.files.store import XLSX_MEDIA_TYPE, file_store
from app.tools.local import inspect_excel, read_excel


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


def _inspect(args):
    return asyncio.run(inspect_excel.SPEC.func(args))


def _read(args):
    return asyncio.run(read_excel.SPEC.func(args))


# --------------------------------------------------------------------------- #
# inspect_excel
# --------------------------------------------------------------------------- #
def test_inspect_lists_every_sheet():
    fid = _make_xlsx(
        {
            "Q1": [["name", "amount"], ["a", 10]],
            "Q2": [["name", "amount"], ["b", 20], ["c", 30]],
        }
    )
    out = _inspect({"file_id": fid})
    assert "2 sheet(s)" in out
    assert "Sheet 'Q1'" in out and "Sheet 'Q2'" in out
    assert "name, amount" in out


def test_inspect_unknown_id_errors():
    assert _inspect({"file_id": "deadbeef"}).startswith("ERROR: no such file")


def test_inspect_missing_id_errors():
    assert _inspect({}).startswith("ERROR")


# --------------------------------------------------------------------------- #
# read_excel
# --------------------------------------------------------------------------- #
def test_read_default_first_sheet_lists_others():
    fid = _make_xlsx({"Alpha": [["h"], ["1"]], "Beta": [["h"], ["2"]]})
    out = _read({"file_id": fid})
    assert "Sheet 'Alpha'" in out
    # must name the other sheet so the model can't silently miss it
    assert "Beta" in out


def test_read_select_sheet_by_name():
    fid = _make_xlsx({"Alpha": [["h"], ["1"]], "Beta": [["h"], ["2"]]})
    out = _read({"file_id": fid, "sheet": "beta"})
    assert "Sheet 'Beta'" in out
    assert "2" in out


def test_read_unknown_sheet_errors():
    fid = _make_xlsx({"Alpha": [["h"], ["1"]]})
    assert _read({"file_id": fid, "sheet": "Nope"}).startswith("ERROR")


def test_read_unknown_id_errors():
    assert _read({"file_id": "nope"}).startswith("ERROR: no such file")


def test_read_truncation_message():
    rows = [["v"]] + [[i] for i in range(300)]
    fid = _make_xlsx({"S": rows})
    out = _read({"file_id": fid})
    assert "truncated" in out.lower()
    assert "start_row=" in out


def test_read_column_projection():
    fid = _make_xlsx({"S": [["a", "b", "c"], [1, 2, 3]]})
    out = _read({"file_id": fid, "columns": ["a", "c"]})
    assert "a | c" in out
    assert "1 | 3" in out


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    names = {s.name for s in LOCAL_TOOLS}
    assert {"inspect_excel", "read_excel"} <= names


def test_descriptions_route_totals_to_aggregate_excel():
    """The description is the routing prompt. inspect_excel is read FIRST, so if
    it only points at read_excel the model commits to the ~200-row capped path
    before it ever weighs aggregate_excel — which is how totals came out wrong.
    Both readers must name aggregate_excel as the answer for totals."""
    assert "aggregate_excel" in inspect_excel.SPEC.description
    # read_excel's cap warning has to name the uncapped alternative, not just
    # say "capped".
    assert "aggregate_excel" in read_excel.SPEC.description

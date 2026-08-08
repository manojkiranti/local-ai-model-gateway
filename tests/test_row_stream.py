"""open_sheet_rows — uncapped, streaming access to one sheet.

Distinct from load_table, which materializes the whole grid then windows it to
~200 rows. Aggregation must see EVERY row, so this path has no caps and never
builds the full grid.
"""

from __future__ import annotations

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


def test_file_handle_is_released_when_the_consumer_stops_early(tmp_path):
    # The scan ceiling stops iterating mid-sheet by design; the workbook must
    # still be closed. A leaked handle only shows up under load, so assert it here.
    rows = [["n"]] + [[i] for i in range(1, 101)]
    path = _write_xlsx(tmp_path, {"S": rows})
    with readers.open_sheet_rows(path) as stream:
        first = next(stream.rows)
    assert first == ["1"]
    path.unlink()   # fails on Windows-style locks / open handles

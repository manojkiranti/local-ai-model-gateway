"""Offline tests for the create_csv local tool.

No network: calls the tool fn directly against a temp-configured fallback file
store and asserts (a) a valid CSV is produced and stored as text/csv, (b) headers
+ rows (list and dict forms) serialize correctly, and (c) validation returns
friendly ERROR strings (never raises).
"""

import asyncio
import csv
import io

import pytest

from app.files.store import CSV_MEDIA_TYPE, file_store
from app.tools.local import csv as csv_tool


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _run(args):
    return asyncio.run(csv_tool.SPEC.func(args))


def _stored_text(result: str) -> str:
    assert "Download it at: GET /v1/files/" in result, result
    fid = result.split("/v1/files/")[1].strip().split()[0]
    record = file_store.get(fid)
    assert record is not None
    assert record.media_type == CSV_MEDIA_TYPE
    return open(record.path, encoding="utf-8").read()


def test_list_rows_with_headers():
    result = _run(
        {
            "headers": ["name", "qty"],
            "rows": [["apple", 3], ["pear", 5]],
            "filename": "fruit",
        }
    )
    text = _stored_text(result)
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["name", "qty"]
    assert parsed[1] == ["apple", "3"]
    assert parsed[2] == ["pear", "5"]


def test_dict_rows_use_header_order():
    result = _run(
        {
            "headers": ["name", "qty"],
            "rows": [{"qty": 9, "name": "kiwi"}],
        }
    )
    text = _stored_text(result)
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["name", "qty"]
    assert parsed[1] == ["kiwi", "9"]


def test_filename_forced_to_csv():
    result = _run({"rows": [["a"]], "filename": "data"})
    fid = result.split("/v1/files/")[1].strip().split()[0]
    assert file_store.get(fid).filename == "data.csv"


def test_values_with_commas_are_quoted():
    result = _run({"rows": [["hello, world", "x"]]})
    text = _stored_text(result)
    # The csv module quotes fields containing the delimiter.
    assert '"hello, world"' in text


def test_missing_rows_errors():
    assert _run({}).startswith("ERROR")


def test_empty_rows_errors():
    assert _run({"rows": []}).startswith("ERROR")


def test_bad_headers_errors():
    assert _run({"headers": "nope", "rows": [["a"]]}).startswith("ERROR")


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "create_csv" for spec in LOCAL_TOOLS)

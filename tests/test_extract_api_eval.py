"""A deterministic eval for POST /v1/extract.

Unlike tests/test_ocr_api_eval.py this asserts EXACT output, because native
extraction is deterministic — the same DOCX yields the same lines every run.
Only the image case is nondeterministic, and it is deliberately excluded: that
engine is already evaluated in tests/test_ocr_api_eval.py and re-scoring it
here would just import its nondeterminism.

Every case is built in-process, so this needs no fixture files and no network.
"""

import os

import pytest

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")

from tests.test_extract_api_integration import _client, _mint, _post  # noqa: E402


def _docx_bytes(paragraphs):
    import io

    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _xlsx_bytes(headers, rows):
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CASES = [
    ("txt-plain", "a.txt", b"alpha\nbeta\n", "text/plain",
     lambda b: b["text"] == "alpha\nbeta" and b["source"]["route"] == "native"),
    ("md-headings", "a.md", b"# Title\n\nBody text\n", "text/markdown",
     lambda b: "# Title" in b["text"] and "Body text" in b["text"]),
    ("json-object", "a.json", b'{"amount": 87500, "name": "Ramesh"}', "application/json",
     lambda b: "87500" in b["text"] and "Ramesh" in b["text"]),
    ("docx-paragraphs", "a.docx",
     _docx_bytes(["Employee: Ramesh Shrestha", "Gross Pay: 87,500.00"]),
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
     lambda b: b["text"].splitlines() == ["Employee: Ramesh Shrestha", "Gross Pay: 87,500.00"]),
    ("xlsx-sheet", "a.xlsx",
     _xlsx_bytes(["name", "amount"], [["alice", 10], ["bob", 20]]),
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     lambda b: b["sheets"][0]["headers"] == ["name", "amount"]
               and b["sheets"][0]["total_rows"] == 2 and b["text"] == ""),
    ("csv-sheet", "a.csv", b"name,amount\nalice,10\n", "text/csv",
     lambda b: b["sheets"][0]["rows"] == [["alice", "10"]]),
    ("native-carries-no-caveat", "a.txt", b"anything\n", "text/plain",
     lambda b: "caveat" not in b["source"] and b["source"]["authoritative"] is True),
]


@pytest.mark.parametrize("name,filename,data,ctype,check", CASES,
                         ids=[c[0] for c in CASES])
def test_case(name, filename, data, ctype, check):
    with _client() as client:
        key = _mint(client, f"eval-{name}", ["document:read"])
        resp = _post(client, key, filename=filename, data=data, ctype=ctype)
        assert resp.status_code == 200, resp.text
        assert check(resp.json()), f"{name}: {resp.json()}"


def test_the_whole_eval_set_passes():
    """One aggregate so a partial regression cannot hide in a green module."""
    failures = []
    with _client() as client:
        for name, filename, data, ctype, check in CASES:
            key = _mint(client, f"agg-{name}", ["document:read"])
            resp = _post(client, key, filename=filename, data=data, ctype=ctype)
            if resp.status_code != 200 or not check(resp.json()):
                failures.append(name)
    assert not failures, f"{len(failures)}/{len(CASES)} failed: {failures}"

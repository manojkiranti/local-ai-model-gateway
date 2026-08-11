"""Offline tests for the pure document reader (no DB, no HTTP)."""

from __future__ import annotations

import json

import pytest

from app.files import documents
from app.files.readers import ReadError


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_txt_splits_into_lines(tmp_path):
    p = _write(tmp_path, "a.txt", "first\nsecond\nthird")
    doc = documents.read_lines(p)
    assert doc.kind == "Text file"
    assert doc.lines == ["first", "second", "third"]
    assert doc.pages is None


def test_md_passes_through_verbatim(tmp_path):
    p = _write(tmp_path, "a.md", "# Title\n\n- one\n- two\n\n```py\nx = 1\n```")
    doc = documents.read_lines(p)
    assert doc.kind == "Markdown"
    assert "# Title" in doc.lines
    assert "x = 1" in doc.lines


def test_txt_with_undecodable_byte_does_not_crash(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_bytes(b"ok\n\xff\xfe bad bytes\n")
    doc = documents.read_lines(p)
    assert doc.lines[0] == "ok"
    assert len(doc.lines) == 2  # replacement chars, no exception


def test_json_is_pretty_printed(tmp_path):
    p = _write(tmp_path, "a.json", json.dumps({"a": {"b": [1, 2]}}))
    doc = documents.read_lines(p)
    assert doc.kind == "JSON"
    assert doc.lines[0] == "{"
    assert any(line.startswith('  "a"') for line in doc.lines)


def test_invalid_json_falls_back_to_raw_text(tmp_path):
    p = _write(tmp_path, "bad.json", '{"a": 1,,,}')
    doc = documents.read_lines(p)
    assert doc.kind == "JSON (unparsed)"
    assert doc.lines == ['{"a": 1,,,}']


def test_unsupported_extension_raises(tmp_path):
    p = _write(tmp_path, "a.rtf", "hi")
    with pytest.raises(ReadError):
        documents.read_lines(p)


def _make_docx(tmp_path, name="a.docx"):
    from docx import Document

    doc = Document()
    doc.add_heading("Eligibility", level=1)
    doc.add_paragraph("Applicants must be resident.")
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "name"
    table.cell(0, 1).text = "min"
    table.cell(0, 2).text = "max"
    table.cell(1, 0).text = "term"
    table.cell(1, 1).text = "1"
    table.cell(1, 2).text = "30"
    doc.add_paragraph("End matter.")
    p = tmp_path / name
    doc.save(str(p))
    return p


def test_docx_heading_body_and_table(tmp_path):
    doc = documents.read_lines(_make_docx(tmp_path))
    assert doc.kind == "Word document"
    assert "# Eligibility" in doc.lines
    assert "Applicants must be resident." in doc.lines
    assert "name | min | max" in doc.lines
    assert "term | 1 | 30" in doc.lines


def test_docx_preserves_document_order(tmp_path):
    doc = documents.read_lines(_make_docx(tmp_path))
    heading = doc.lines.index("# Eligibility")
    table_row = doc.lines.index("name | min | max")
    tail = doc.lines.index("End matter.")
    assert heading < table_row < tail


def test_corrupt_docx_raises_read_error(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"not a zip at all")
    with pytest.raises(ReadError):
        documents.read_lines(p)

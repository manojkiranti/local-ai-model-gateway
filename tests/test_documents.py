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

"""`read_pdf_pages` — the single pypdf call site in the repository.

NRB Phase 6A needs per-page text to compute page coverage; `read_document` needs
a flat line stream. Both come from here, so a change to encryption handling, the
page cap or per-page failure isolation cannot apply to one and not the other.
"""

import pytest
from fpdf import FPDF

from app.files import documents
from app.files.readers import ReadError


def _pdf(tmp_path, pages, name="doc.pdf"):
    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        if body:
            pdf.set_font("helvetica", size=12)
            pdf.cell(0, 10, body)
    path = tmp_path / name
    pdf.output(str(path))
    return path


def test_returns_one_entry_per_page_in_order(tmp_path):
    result = documents.read_pdf_pages(_pdf(tmp_path, ["alpha", "beta", "gamma"]))
    assert result.total == 3
    assert len(result.pages) == 3
    assert "alpha" in result.pages[0]
    assert "gamma" in result.pages[2]


def test_a_page_with_no_text_is_an_empty_string_not_a_missing_entry(tmp_path):
    # The scanned-page signal. Dropping the entry would make coverage read 100%.
    result = documents.read_pdf_pages(_pdf(tmp_path, ["alpha", "", "gamma"]))
    assert len(result.pages) == 3
    assert result.pages[1].strip() == ""


def test_the_page_cap_is_reported_rather_than_silently_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    result = documents.read_pdf_pages(_pdf(tmp_path, ["a", "b", "c", "d"]))
    assert result.total == 4
    assert len(result.pages) == 2
    assert result.skipped == 2


def test_a_corrupt_pdf_raises_readerror_without_leaking_the_path(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot really a pdf at all")
    with pytest.raises(ReadError) as exc:
        documents.read_pdf_pages(path)
    assert str(tmp_path) not in str(exc.value)


def test_a_missing_file_raises_readerror(tmp_path):
    with pytest.raises(ReadError):
        documents.read_pdf_pages(tmp_path / "nope.pdf")


def test_read_lines_still_produces_page_markers_from_the_shared_reader(tmp_path):
    doc = documents.read_lines(_pdf(tmp_path, ["alpha", "", "gamma"]))
    assert doc.kind == "PDF"
    assert doc.pages == 3
    assert doc.text_pages == 2
    assert "[page 1]" in doc.lines
    assert any("no extractable text" in line for line in doc.lines)


def test_read_lines_and_read_pdf_pages_agree_on_the_page_count(tmp_path):
    path = _pdf(tmp_path, ["alpha", "", "gamma", "delta"])
    assert documents.read_lines(path).pages == documents.read_pdf_pages(path).total

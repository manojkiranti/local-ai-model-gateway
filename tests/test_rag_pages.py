"""app/rag/pages.py — PDF pages rendered as PNGs for the view-only viewer.
Pure: a file on disk in, bytes out. No database."""

import io

import pytest
from pypdf import PdfWriter

from app.rag import pages


def _pdf(tmp_path, n=3):
    writer = PdfWriter()
    for _ in range(n):
        writer.add_blank_page(width=300, height=400)
    path = tmp_path / "doc.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def test_page_count(tmp_path):
    assert pages.page_count(_pdf(tmp_path, 3)) == 3


def test_a_page_renders_as_a_png_at_the_configured_width(tmp_path):
    from PIL import Image

    png = pages.render_page_png(_pdf(tmp_path), 2)
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    assert abs(image.width - pages.PAGE_WIDTH_PX) <= 1
    assert abs(image.height - pages.PAGE_WIDTH_PX * 400 / 300) <= 2


@pytest.mark.parametrize("page", [0, 4, -1])
def test_an_out_of_range_page_is_an_index_error(tmp_path, page):
    with pytest.raises(IndexError):
        pages.render_page_png(_pdf(tmp_path, 3), page)


def test_a_non_pdf_is_a_page_error(tmp_path):
    path = tmp_path / "x.pdf"
    path.write_bytes(b"Employee,Days\nAlice,10\n")
    with pytest.raises(pages.PageError):
        pages.page_count(path)

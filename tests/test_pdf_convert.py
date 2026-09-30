"""app/files/pdf_convert.py — Office file -> PDF for the preview panel.

`soffice` is replaced by a stub, so this runs without LibreOffice; the real
conversion is exercised by the live check in the image, not here."""

import os
import subprocess

import pytest

from app.files import pdf_convert


@pytest.fixture()
def fake_soffice(monkeypatch):
    """Record each soffice call and write a PDF where LibreOffice would."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        outdir = cmd[cmd.index("--outdir") + 1]
        source = cmd[-1]
        stem = os.path.splitext(os.path.basename(source))[0]
        with open(os.path.join(outdir, f"{stem}.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.7 fake")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pdf_convert.subprocess, "run", run)
    return calls


def test_converts_next_to_the_source_and_caches(tmp_path, fake_soffice):
    source = tmp_path / "abc.docx"
    source.write_bytes(b"docx")

    pdf = pdf_convert.convert_to_pdf(source)
    assert pdf == tmp_path / "abc.preview.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")

    pdf_convert.convert_to_pdf(source)
    assert len(fake_soffice) == 1, "a fresh cached PDF must not be rebuilt"


def test_a_newer_source_is_reconverted(tmp_path, fake_soffice):
    source = tmp_path / "abc.docx"
    source.write_bytes(b"docx")
    pdf = pdf_convert.convert_to_pdf(source)
    old = pdf.stat().st_mtime
    os.utime(source, (old + 10, old + 10))

    pdf_convert.convert_to_pdf(source)
    assert len(fake_soffice) == 2


def test_each_conversion_gets_its_own_profile(tmp_path, fake_soffice):
    """Two soffice processes sharing a profile lock each other out."""
    for name in ("a.docx", "b.docx"):
        (tmp_path / name).write_bytes(b"x")
        pdf_convert.convert_to_pdf(tmp_path / name)
    profiles = [next(a for a in cmd if a.startswith("-env:UserInstallation=")) for cmd in fake_soffice]
    assert profiles[0] != profiles[1]
    assert all("--headless" in cmd for cmd in fake_soffice)


def test_no_output_is_a_convert_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pdf_convert.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    source = tmp_path / "abc.docx"
    source.write_bytes(b"docx")
    with pytest.raises(pdf_convert.ConvertError):
        pdf_convert.convert_to_pdf(source)
    assert not pdf_convert.cached_pdf_path(source).exists()


def test_a_timeout_is_a_convert_error(tmp_path, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(pdf_convert.subprocess, "run", slow)
    source = tmp_path / "abc.docx"
    source.write_bytes(b"docx")
    with pytest.raises(pdf_convert.ConvertError, match="timed out"):
        pdf_convert.convert_to_pdf(source)


def test_only_office_formats_are_convertible():
    assert pdf_convert.CONVERTIBLE_EXTS == {".docx", ".pptx", ".xlsx"}

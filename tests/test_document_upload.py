"""Upload-route integration for documents (.pdf/.docx/.txt/.md/.json), against
real Postgres. Skips cleanly if the DB is unreachable.

Mirrors the setup in test_excel_upload_integration.py: a TestClient per test and
a local _auth() that registers, logs in, and skips when Postgres is down.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from starlette.testclient import TestClient

from app.main import app

OWNER = "docup-owner@example.com"
PASSWORD = "supersecret123"

PDF_CT = "application/pdf"
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _auth(client, email):
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upload(client, headers, name, data, ctype):
    return client.post(
        "/v1/files", files={"file": (name, data, ctype)}, headers=headers
    )


def _text_pdf_bytes(pages):
    from fpdf import FPDF

    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, body)
    return bytes(pdf.output())


def _image_only_pdf_bytes(tmp_path, n_pages):
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "block.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    for _ in range(n_pages):
        pdf.add_page()
        pdf.image(str(img), x=10, y=10, w=50)
    return bytes(pdf.output())


def test_pdf_upload_is_accepted():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "doc.pdf", _text_pdf_bytes(["hello", "world"]), PDF_CT)
        assert up.status_code == 201, up.text
        body = up.json()
        assert body["media_type"] == "application/pdf"
        assert body["summary"]["pages"] == 2
        assert body["source"] == "uploaded"


def test_scanned_pdf_uploads_successfully(tmp_path):
    """The OCR seam: a scan is a VALID file, so it must not be rejected here.
    read_document is where the user learns it has no text layer."""
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "scan.pdf", _image_only_pdf_bytes(tmp_path, 3), PDF_CT)
        assert up.status_code == 201, up.text
        summary = up.json()["summary"]
        assert summary["pages"] == 3
        assert summary["text_pages"] == 0


def test_txt_and_md_uploads_are_accepted():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        assert _upload(client, owner, "notes.txt", b"one\ntwo\n", "text/plain").status_code == 201
        assert _upload(client, owner, "notes.md", b"# hi\n", "text/markdown").status_code == 201


def test_json_upload_is_accepted():
    """The module docstring names .json among the supported extensions, but
    (until this fix) nothing here actually uploaded one."""
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "a.json", b'{"loan": {"term": 30}}', "application/json")
        assert up.status_code == 201, up.text
        body = up.json()
        assert body["media_type"] == "application/json"
        assert body["summary"]["kind"] == "JSON"


def test_deeply_nested_json_is_accepted_not_500(tmp_path):
    """Finding 2's exact repro: ~400 KB of nothing but brackets, well under the
    10 MB cap, is deep enough to blow json's recursive parser (RecursionError,
    not ValueError). Unparseable JSON is explicitly NOT an error in this
    design (documents.py's own contract + the spec's error table) — it must be
    accepted and served as raw text, not crash the upload route with a 500 and
    leave the file orphaned on disk with no `generated_files` row."""
    raw = b"[" * 200_000 + b"]" * 200_000
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "deep.json", raw, "application/json")
        assert up.status_code == 201, up.text
        assert up.json()["summary"]["kind"] == "JSON (unparsed)"


def test_docx_upload_is_accepted(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("hello")
    p = tmp_path / "a.docx"
    doc.save(str(p))
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "a.docx", p.read_bytes(), DOCX_CT)
        assert up.status_code == 201, up.text


def test_corrupt_pdf_is_rejected():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "broken.pdf", b"%PDF-1.4\ngarbage", PDF_CT)
        assert up.status_code == 400
        assert "could not read" in up.json()["detail"]


def test_password_protected_pdf_is_rejected():
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(_text_pdf_bytes(["secret"])))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buf = BytesIO()
    writer.write(buf)
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "locked.pdf", buf.getvalue(), PDF_CT)
        assert up.status_code == 400


def test_docx_zip_bomb_is_refused():
    """The .xlsx guard now covers .docx, which is also a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # +1: the cap check is strict `>`, so exactly-at-cap would not trip it.
        zf.writestr("word/document.xml", b"\0" * (200 * 1024 * 1024 + 1))
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "bomb.docx", buf.getvalue(), DOCX_CT)
        assert up.status_code == 400
        assert "too large" in up.json()["detail"]


def test_xlsm_is_still_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        assert _upload(client, owner, "macro.xlsm", b"anything", "application/octet-stream").status_code == 400


def test_rtf_is_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        assert _upload(client, owner, "a.rtf", b"{\\rtf1}", "application/rtf").status_code == 400

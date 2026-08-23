"""Integration tests for POST /v1/extract.

None of these need the OCR stack: every case uses a native format, because the
image branch is `/v1/ocr`'s engine and is already covered there. The test
client mechanism mirrors tests/test_ocr_api_integration.py — read that file's
docstring for why `_client()` must be entered as a context manager.
"""

import contextlib
import os

import pytest

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")

PASSWORD = "supersecret123"
ADMIN_EMAIL = "admin@example.com"


@contextlib.contextmanager
def _client():
    from fastapi.testclient import TestClient

    from app.config import get_settings

    previous = os.environ.get("EXTERNAL_API_ENABLED")
    os.environ["EXTERNAL_API_ENABLED"] = "true"
    get_settings.cache_clear()
    import importlib

    import app.main

    importlib.reload(app.main)
    try:
        with TestClient(app.main.app) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop("EXTERNAL_API_ENABLED", None)
        else:
            os.environ["EXTERNAL_API_ENABLED"] = previous
        get_settings.cache_clear()


def _admin_headers(client):
    resp = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD})
    if resp.status_code != 200:
        pytest.skip(f"cannot log in as {ADMIN_EMAIL} ({resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _mint(client, name, scopes):
    resp = client.post(
        "/v1/api-keys", json={"name": name, "scopes": scopes},
        headers=_admin_headers(client),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


def _post(client, key, *, filename="a.txt", data=b"hello\nworld\n", ctype="text/plain"):
    return client.post(
        "/v1/extract",
        files={"file": (filename, data, ctype)},
        headers={"X-API-Key": key},
    )


def _pdf_bytes(text="Gross Pay 87500"):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(60, 10, text)
    return bytes(pdf.output())


def test_a_txt_extract_is_native_and_carries_no_caveat():
    with _client() as client:
        key = _mint(client, "e1", ["document:read"])
        resp = _post(client, key)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == "hello\nworld"
        assert body["source"]["route"] == "native"
        assert body["source"]["authoritative"] is True
        assert "caveat" not in body["source"]
        assert body["request_id"]


def test_the_request_id_is_on_the_200_header_too():
    with _client() as client:
        key = _mint(client, "e2", ["document:read"])
        resp = _post(client, key)
        assert resp.headers["X-Request-Id"] == resp.json()["request_id"]


def test_a_pdf_reports_its_page_counts():
    with _client() as client:
        key = _mint(client, "e3", ["document:read"])
        resp = _post(client, key, filename="a.pdf", data=_pdf_bytes(),
                     ctype="application/pdf")
        assert resp.status_code == 200, resp.text
        src = resp.json()["source"]
        assert src["pages"] == 1 and src["text_pages"] == 1


def test_a_csv_comes_back_as_sheets_with_an_empty_text():
    with _client() as client:
        key = _mint(client, "e4", ["document:read"])
        resp = _post(client, key, filename="a.csv",
                     data=b"name,amount\nalice,10\n", ctype="text/csv")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == ""
        assert body["sheets"][0]["headers"] == ["name", "amount"]


def test_an_ocr_only_key_is_403_not_401():
    with _client() as client:
        key = _mint(client, "e5", ["ocr:read"])
        resp = _post(client, key)
        assert resp.status_code == 403
        assert "document:read" in resp.json()["detail"]


def test_every_bad_credential_gets_the_same_401_body():
    with _client() as client:
        for key in ("", "garbage", "lgw_live_00000000_nosuchsecret"):
            resp = _post(client, key)
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Invalid API key"


def test_an_unsupported_extension_is_400():
    with _client() as client:
        key = _mint(client, "e6", ["document:read"])
        resp = _post(client, key, filename="a.exe", data=b"MZ", ctype="application/x-msdownload")
        assert resp.status_code == 400
        assert "not a supported" in resp.json()["detail"].lower()


def test_an_empty_upload_is_400():
    with _client() as client:
        key = _mint(client, "e7", ["document:read"])
        resp = _post(client, key, data=b"")
        assert resp.status_code == 400


def test_a_document_read_key_cannot_reach_the_ocr_route():
    """The reverse of the test below. Scope separation is only a boundary if
    it holds in BOTH directions — a key minted for text extraction must not
    quietly acquire the model-adjacent OCR route, and vice versa."""
    with _client() as client:
        key = _mint(client, "e10", ["document:read"])
        resp = client.post(
            "/v1/ocr",
            files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 403
        assert "ocr:read" in resp.json()["detail"]


def test_a_jwt_cannot_be_used_on_the_extract_route():
    with _client() as client:
        resp = client.post(
            "/v1/extract",
            files={"file": ("a.txt", b"hi", "text/plain")},
            headers=_admin_headers(client),
        )
        assert resp.status_code == 401


def test_a_scanned_pdf_is_422_and_says_so():
    # A PDF whose pages exist but yield no text. fpdf2 makes one by drawing
    # only a rectangle — no text operators, so pypdf finds no text layer
    # (verified directly against pypdf before relying on it here).
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.rect(10, 10, 50, 50)
    with _client() as client:
        key = _mint(client, "e8", ["document:read"])
        resp = _post(client, key, filename="scan.pdf", data=bytes(pdf.output()),
                     ctype="application/pdf")
        assert resp.status_code == 422, resp.text
        assert "no text layer" in resp.json()["detail"].lower()


def test_a_usage_row_is_written_for_a_success_and_for_a_403():
    import asyncio

    from sqlalchemy import text as sql_text

    with _client() as client:
        good = _mint(client, "e9-good", ["document:read"])
        bad = _mint(client, "e9-bad", ["ocr:read"])
        assert _post(client, good).status_code == 200
        assert _post(client, bad).status_code == 403

    async def count():
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(DB_URL, poolclass=None)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    sql_text(
                        "SELECT status_code, count(*) FROM api_key_usage "
                        "WHERE route = 'POST /v1/extract' GROUP BY 1"
                    )
                )
                return dict(rows.all())
        finally:
            await engine.dispose()

    by_status = asyncio.run(count())
    assert by_status.get(200, 0) >= 1
    assert by_status.get(403, 0) >= 1

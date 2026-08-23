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
    """Scoped by THIS test's OWN key ids, not a floor against the whole
    table. The original version of this test asserted `>= 1` across every row
    ever written for this route, with no run scoping at all — after one
    green run, table residue satisfies `>= 1` even if `record_usage` were
    deleted outright. Minting fresh keys and counting exactly their own rows
    is the only way this test can fail when it should.
    """
    import asyncio

    from sqlalchemy import text as sql_text

    with _client() as client:
        good_resp = client.post(
            "/v1/api-keys",
            json={"name": "e9-good", "scopes": ["document:read"]},
            headers=_admin_headers(client),
        )
        assert good_resp.status_code == 201, good_resp.text
        good_id, good_key = good_resp.json()["id"], good_resp.json()["key"]

        bad_resp = client.post(
            "/v1/api-keys",
            json={"name": "e9-bad", "scopes": ["ocr:read"]},
            headers=_admin_headers(client),
        )
        assert bad_resp.status_code == 201, bad_resp.text
        bad_id, bad_key = bad_resp.json()["id"], bad_resp.json()["key"]

        assert _post(client, good_key).status_code == 200
        assert _post(client, bad_key).status_code == 403

    async def count(key_id, status_code):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(DB_URL, poolclass=None)
        try:
            async with engine.connect() as conn:
                row = await conn.execute(
                    sql_text(
                        "SELECT count(*) FROM api_key_usage "
                        "WHERE route = 'POST /v1/extract' AND api_key_id = :kid "
                        "AND status_code = :code"
                    ),
                    {"kid": key_id, "code": status_code},
                )
                return row.scalar_one()
        finally:
            await engine.dispose()

    assert asyncio.run(count(good_id, 200)) == 1
    assert asyncio.run(count(bad_id, 403)) == 1


# --- regressions found by the Task 6 review --------------------------------


def test_a_response_build_failure_is_500_with_exactly_one_row_no_false_200():
    """`build_extract_response` pairs lines to confidences with
    `zip(..., strict=True)`, so a genuine length mismatch raises `ValueError`.
    The route builds the body BEFORE writing the success row for exactly this
    reason: writing a `200` usage row first and building the body second
    would leave that row on record for a request that actually failed — a
    false success in the one place `api_key_usage` exists to be trustworthy
    evidence. Forces the raise by monkeypatching `build_extract_response`
    (the same technique `test_an_unexpected_ocr_failure_is_500_with_a_usage_
    row_not_a_crash` in tests/test_ocr_api_integration.py uses for the
    sibling route) rather than crafting real mismatched data, because the
    mismatch is not reachable through any real input — proving the guard is
    correct in the failure case it exists for still needs some way to force
    it.
    """
    import asyncio
    from unittest.mock import patch

    from sqlalchemy import text as sql_text

    from app.publicapi import extract_router

    with _client() as client:
        key_resp = client.post(
            "/v1/api-keys",
            json={"name": "build-fail", "scopes": ["document:read"]},
            headers=_admin_headers(client),
        )
        assert key_resp.status_code == 201, key_resp.text
        key_id, key = key_resp.json()["id"], key_resp.json()["key"]

        def _boom(*args, **kwargs):
            raise ValueError("lines/confidences length mismatch")

        with patch.object(extract_router, "build_extract_response", _boom):
            resp = _post(client, key)

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"] == "extraction failed unexpectedly"
        # The internal exception must never reach the caller.
        assert "ValueError" not in resp.text
        assert "length mismatch" not in resp.text

    async def counts():
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(DB_URL, poolclass=None)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    sql_text(
                        "SELECT status_code, count(*) FROM api_key_usage "
                        "WHERE route = 'POST /v1/extract' AND api_key_id = :kid "
                        "GROUP BY 1"
                    ),
                    {"kid": key_id},
                )
                return dict(rows.all())
        finally:
            await engine.dispose()

    by_status = asyncio.run(counts())
    # Exactly one row for this request, and it must be the 500 — never a
    # false 200 alongside or instead of it.
    assert by_status == {500: 1}, by_status


def test_413_from_the_routes_own_streamed_count_not_the_middleware():
    """`UploadContentLengthGuard` only ever sees a DECLARED `Content-Length`;
    a request that omits it (chunked, or any client that streams without one)
    sails straight through the middleware unconditionally — exactly as
    `test_a_chunked_request_with_no_content_length_is_not_refused_by_the_
    guard` proves for `/v1/ocr`. The route's OWN cap is `_route.stream_to_
    temp`, which counts bytes as they arrive off the wire and stops once
    `max_bytes` is passed. Sending the multipart body as a generator (rather
    than a plain `bytes`/`files=` payload) makes httpx negotiate chunked
    transfer encoding with no `Content-Length` header at all — verified
    directly against `httpx.Request` before relying on it here — so the ONLY
    guard that can be answering this 413 is the route's own streamed count.
    """
    with _client() as client:
        key = _mint(client, "stream-413", ["document:read"])
        os.environ["EXTRACT_MAX_UPLOAD_BYTES"] = "2048"  # the config minimum
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            boundary = "----extracttestboundary"
            payload = b"x" * 5000  # over the 2048-byte cap above

            def gen():
                body = (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="file"; '
                    'filename="big.txt"\r\n'
                    "Content-Type: text/plain\r\n\r\n"
                ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
                for i in range(0, len(body), 1024):
                    yield body[i : i + 1024]

            resp = client.post(
                "/v1/extract",
                content=gen(),
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "X-API-Key": key,
                },
            )
            assert "content-length" not in {k.lower() for k in resp.request.headers}
            assert resp.status_code == 413, resp.text
            assert "limit" in resp.json()["detail"].lower()
        finally:
            os.environ.pop("EXTRACT_MAX_UPLOAD_BYTES", None)
            get_settings.cache_clear()


def test_no_capacity_is_503_with_retry_after():
    """The bounded semaphore wait expiring. Driven deterministically by
    swapping the module-global semaphore for an already-exhausted one and
    dropping the queue-wait to 1s, the same technique `test_no_capacity_is_
    503_and_is_not_the_same_answer_as_429` uses for `/v1/ocr` — no real load,
    no sleeping on a real extraction.
    """
    import asyncio

    from app.publicapi import extract_router

    with _client() as client:
        key = _mint(client, "no-capacity", ["document:read"])
        os.environ["EXTRACT_QUEUE_WAIT_SECONDS"] = "1"
        from app.config import get_settings

        get_settings.cache_clear()
        saved = extract_router._slots
        extract_router._slots = asyncio.Semaphore(0)  # every slot already taken
        try:
            resp = _post(client, key)
            assert resp.status_code == 503, resp.text
            assert resp.headers["Retry-After"] == "5"
            assert "capacity" in resp.json()["detail"].lower()
        finally:
            extract_router._slots = saved
            os.environ.pop("EXTRACT_QUEUE_WAIT_SECONDS", None)
            get_settings.cache_clear()


def test_no_temp_file_survives_a_success_or_a_rejected_request():
    """The endpoint promises it does not retain caller uploads. Checked on
    BOTH a 200 (the ordinary path through `finally: dest.unlink(...)`) and a
    400 (an empty upload, which still calls `stream_to_temp` before the
    empty-body check runs) — the same pairing
    `test_no_temp_file_survives_a_rejected_request` uses for `/v1/ocr`, with
    a real success added since a leak on the happy path is just as real a
    leak as one on a rejection.
    """
    import tempfile
    from pathlib import Path

    with _client() as client:
        key = _mint(client, "temp-cleanup", ["document:read"])
        before = set(Path(tempfile.gettempdir()).glob("extract-*"))
        ok = _post(client, key)
        assert ok.status_code == 200, ok.text
        empty = _post(client, key, data=b"")
        assert empty.status_code == 400, empty.text
        after = set(Path(tempfile.gettempdir()).glob("extract-*"))
        assert after == before, after - before


def _fake_xlsx_bomb() -> bytes:
    """A tiny, well-formed .xlsx whose CENTRAL DIRECTORY claims one member
    inflates past `upload_xlsx_max_uncompressed` (200 MB) — the exact shape
    `app/files/router.py`'s own guard was written to catch, mirrored here so
    the /v1/extract guard is exercised the same way rather than by actually
    writing 200+ MB of real bytes into the request body. All-zero content
    compresses to a few bytes under DEFLATE, so the .xlsx on disk (and the
    request body) stays tiny while `zipfile.ZipFile(...).infolist()` still
    reports the true (huge) uncompressed size — that field is what the guard
    reads, and it is set from `len(data)` regardless of how compressible
    `data` is."""
    import io as _io
    import zipfile as _zipfile

    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        # +1: the cap check is strict `>`, so exactly-at-cap would not trip it.
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (200 * 1024 * 1024 + 1))
    return buf.getvalue()


def test_a_zip_bomb_xlsx_is_400_not_processed():
    """Would FAIL if the guard were removed: the bomb's actual bytes are a
    tiny, well-formed, all-zero .xlsx that `openpyxl` can open without
    complaint, so absent this guard the upload would sail through to a 200
    (or, on a real payload, run `readers.load_table` against gigabytes of
    inflated XML in this process). The assertion is specifically the
    "expands too large" wording the guard raises, not just any 400 — an
    unrelated 400 (bad extension, empty body) would not prove this."""
    with _client() as client:
        key = _mint(client, "zip-bomb", ["document:read"])
        resp = _post(
            client, key, filename="bomb.xlsx", data=_fake_xlsx_bomb(),
            ctype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        assert resp.status_code == 400, resp.text
        assert "too large" in resp.json()["detail"]


def test_a_corrupt_xlsx_is_400_not_500():
    """Bytes that pass the extension allowlist and the empty-body check but
    are not a zip at all. Without `zipfile.BadZipFile` caught explicitly,
    `zipfile.ZipFile(dest)` raises straight out of the route and FastAPI
    turns an unhandled exception into a 500 — this proves the guard's own
    `except zipfile.BadZipFile` branch runs, not merely that SOME 400 exists
    for bad input (an unsupported-extension 400 would pass this filename
    with no guard involved at all, which is why the extension stays
    `.xlsx`)."""
    with _client() as client:
        key = _mint(client, "bad-zip", ["document:read"])
        resp = _post(
            client, key, filename="broken.xlsx", data=b"not a zip file at all",
            ctype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        assert resp.status_code == 400, resp.text
        assert "not a valid" in resp.json()["detail"]

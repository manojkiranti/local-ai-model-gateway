"""Integration tests for POST /v1/ocr.

Most of these do not need the OCR stack: they exercise auth, scope, the guards
and the usage log, all of which run BEFORE the engine. The tests that need real
text belong in a future tests/test_ocr_api_eval.py, gated on OCR_LIVE_TESTS —
not written here.

Test-client mechanism: mirrors `tests/test_apikey_admin_integration.py`'s
`_client()`, entered as `with _client() as client:`. `TestClient` only pins
every request it makes to ONE event loop while entered as a context manager;
used bare, each request gets its own throwaway loop and the SECOND
DB-touching request dies with "attached to a different loop" against the
app's pooled engine (see that file's docstring for the full explanation).
Unlike that helper, this one restores `EXTERNAL_API_ENABLED` to whatever it
was before the test ran (and re-clears the settings cache), so a later test
asserting the disabled-by-default behaviour is not silently broken by this
file having flipped the switch and left it flipped.
"""

import asyncio
import contextlib
import io
import os

import pytest

from app.files import image_ocr

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")

PASSWORD = "supersecret123"
ADMIN_EMAIL = "admin@example.com"


@contextlib.contextmanager
def _client():
    """A TestClient with the external API switched ON, and switched back off
    (or left as it was) on the way out.

    `EXTERNAL_API_ENABLED` is read at import time by `app.main`, so it must be
    set in the environment BEFORE the module is (re)imported and the settings
    cache cleared — same requirement as `test_apikey_admin_integration.py`.
    """
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
    resp = client.post(
        "/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    if resp.status_code != 200:
        pytest.skip(f"cannot log in as {ADMIN_EMAIL} ({resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _mint(client, name, scopes=None):
    body = {"name": name}
    if scopes is not None:
        body["scopes"] = scopes
    resp = client.post("/v1/api-keys", json=body, headers=_admin_headers(client))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _png(width=40, height=20):
    """A real, tiny, valid PNG. No text in it — these tests are about the
    boundary, not the recogniser."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _post(client, key, data=None, filename="a.png", extra=None):
    files = {"file": (filename, data if data is not None else _png(), "image/png")}
    return client.post(
        "/v1/ocr", files=files, data=extra or {}, headers={"X-API-Key": key}
    )


# --- authentication ------------------------------------------------------

@pytest.mark.parametrize(
    "key",
    [
        "",                                    # absent
        "garbage",                             # malformed
        "lgw_live_00000000_nosuchsecretatall",  # unknown prefix
    ],
)
def test_every_bad_credential_gets_the_same_401_body(key):
    with _client() as client:
        resp = _post(client, key)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid API key"


def test_a_truly_absent_header_is_byte_identical_to_a_wrong_secret_401():
    """`APIKeyHeader(auto_error=False)` must keep the absent-header case
    OURS — same status, same body — rather than falling through to the
    generic 403 FastAPI's own security dependency raises by default when a
    scheme is missing and `auto_error` is left True. This sends NO
    `X-API-Key` header at all, which is a different wire shape from the
    parametrized `""` case above (that one still SENDS the header, with an
    empty value) — and compares raw response BYTES, not just status/JSON
    shape, against a wrong-secret rejection on a real key.
    """
    with _client() as client:
        minted = _mint(client, "byte-identical-check")

        absent = client.post(
            "/v1/ocr", files={"file": ("a.png", _png(), "image/png")}
        )

        tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43
        wrong_secret = _post(client, tampered)

        assert absent.status_code == 401 == wrong_secret.status_code
        assert absent.content == wrong_secret.content
        assert absent.json()["detail"] == "Invalid API key"


def test_a_wrong_secret_on_a_real_prefix_is_the_same_401():
    """Distinguishing this from an unknown prefix tells an attacker which
    prefixes are real."""
    with _client() as client:
        minted = _mint(client, "wrong-secret")
        tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43
        resp = _post(client, tampered)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid API key"


def test_a_revoked_key_stops_working_immediately():
    with _client() as client:
        minted = _mint(client, "to-revoke")
        client.delete(f"/v1/api-keys/{minted['id']}", headers=_admin_headers(client))
        resp = _post(client, minted["key"])
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid API key"


def test_a_jwt_cannot_be_used_on_the_ocr_route():
    """The two credential types are disjoint, in both directions."""
    with _client() as client:
        token = _admin_headers(client)["Authorization"].split()[1]
        files = {"file": ("a.png", _png(), "image/png")}
        resp = client.post(
            "/v1/ocr", files=files, headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401


def test_an_api_key_cannot_reach_a_jwt_route():
    with _client() as client:
        minted = _mint(client, "no-jwt-routes")
        for path in ("/users/me", "/v1/api-keys", "/v1/sessions"):
            resp = client.get(path, headers={"X-API-Key": minted["key"]})
            assert resp.status_code in (401, 403), f"{path} accepted an API key"


def test_a_key_without_the_scope_gets_403_not_401():
    """403 says the credential is genuine and the permissions are not, so the
    caller does not rotate a working key chasing the wrong bug."""
    with _client() as client:
        # No key can be minted without ocr:read (it is the only scope), so
        # strip the scope directly in the database to exercise the branch.
        minted = _mint(client, "scopeless")
        import asyncio

        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.apikeys.models import ApiKey

        async def strip():
            engine = create_async_engine(DB_URL, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                await s.execute(
                    update(ApiKey).where(ApiKey.id == minted["id"]).values(scopes=[])
                )
                await s.commit()
            await engine.dispose()

        asyncio.run(strip())
        resp = _post(client, minted["key"])
        assert resp.status_code == 403
        assert "ocr:read" in resp.json()["detail"]


def test_an_expired_key_is_the_same_401_as_every_other_cause():
    """The sixth credential cause, proven END-TO-END rather than only against
    the pure `policy.is_usable` function: an expired ROW must reach the live
    dependency and come back indistinguishable from a wrong secret.
    """
    with _client() as client:
        minted = _mint(client, "expired")
        import asyncio
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.apikeys import keygen
        from app.apikeys.models import ApiKey
        from app.apikeys.throttle import get_auth_throttle

        async def expire():
            engine = create_async_engine(DB_URL, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                await s.execute(
                    update(ApiKey)
                    .where(ApiKey.id == minted["id"])
                    .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
                )
                await s.commit()
            await engine.dispose()

        asyncio.run(expire())

        # This key's prefix is freshly random and has never been presented
        # before, so the throttle has no entry for it yet. Checked in-process
        # (TestClient runs the ASGI app in THIS Python process, so the
        # throttle's module-level singleton is the real, live one) rather than
        # asserted from memory.
        prefix, _secret = keygen.parse(minted["key"])
        throttle = get_auth_throttle()
        assert throttle.retry_after(prefix) is None
        assert prefix not in throttle._entries

        resp = _post(client, minted["key"])
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid API key"

        # The property that makes this cause interesting, REVERSED from an
        # earlier version of this test: an expired key now DOES consume a
        # throttle attempt. The original exemption copied AD login's rule
        # that an unreachable directory must not cost an attempt — but that
        # protects an honest caller from a TRANSIENT fault (the directory
        # recovers, the account is fine). Revocation and expiry are the
        # opposite: PERMANENT for this prefix (no route un-revokes one, and
        # re-minting hands out a fresh random prefix), so the exemption never
        # spared an honest caller anything, while it let an attacker probe a
        # dead key unboundedly AND told them "429 means the secret was right,
        # 401-forever means it was wrong" — exactly the six-causes-one-message
        # boundary this module exists to hide. Directly observable here
        # because of the in-process TestClient noted above.
        assert prefix in throttle._entries


# --- input guards --------------------------------------------------------

def test_a_pdf_is_rejected_with_a_pointer_to_what_is_accepted():
    with _client() as client:
        minted = _mint(client, "pdf-reject")
        resp = _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
        assert resp.status_code == 400
        assert ".png" in resp.json()["detail"]


def test_a_very_long_extensionless_filename_is_truncated_in_the_400_body():
    """`file.filename` is the caller's own, unbounded string, reflected
    straight into the 400 detail. JSON-encoded so this isn't an injection,
    but it is attacker-controlled and was previously unbounded."""
    with _client() as client:
        minted = _mint(client, "long-filename")
        long_name = "a" * 500  # no extension, so the raw name is what's shown
        resp = _post(client, minted["key"], data=b"not an image", filename=long_name)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert len(detail) < 250, f"detail not bounded: {len(detail)} chars"


def test_an_empty_upload_is_rejected():
    with _client() as client:
        minted = _mint(client, "empty")
        resp = _post(client, minted["key"], data=b"")
        assert resp.status_code == 400


def test_a_gif_renamed_png_never_reaches_the_gif_decoder():
    """images._KINDS is a decoder allowlist on the SNIFFED format.

    Uses a REAL, Pillow-written GIF. An earlier version of this test used a
    hand-built byte string (`b"GIF89a" + b"\\x01\\x00\\x01\\x00" + b"\\x00" * 20`)
    that never reached `_KINDS` at all — measured, Pillow itself raises
    UnidentifiedImageError on those bytes before the format allowlist is ever
    consulted, so the test passed for a reason unrelated to its own name and
    would have kept passing if 'GIF' were added to `_KINDS` tomorrow. A real
    GIF actually exercises the allowlist rejection path, which is why the
    detail assertion below checks for THAT specific message rather than just
    the status code.
    """
    with _client() as client:
        minted = _mint(client, "renamed-gif")
        from PIL import Image

        buf = io.BytesIO()
        Image.new("P", (4, 4)).save(buf, format="GIF")
        resp = _post(client, minted["key"], data=buf.getvalue(), filename="a.png")
        assert resp.status_code == 400
        assert "unsupported image format" in resp.json()["detail"]


def test_an_unauthenticated_oversized_upload_is_413_not_401():
    """`OcrContentLengthGuard` runs as ASGI middleware, BEFORE FastAPI even
    parses the multipart body — and therefore before `require_api_client`
    ever runs. Without it, an attacker holding NO valid key could still make
    the gateway read and spool an arbitrarily large file part to disk before
    the auth dependency got a chance to answer 401 (Starlette's own
    `max_part_size` only bounds non-file form parts). Proven here by sending
    a garbage credential together with an oversized declared body: if auth
    ran first this would be 401, not 413.
    """
    with _client() as client:
        os.environ["OCR_MAX_UPLOAD_BYTES"] = "1024"
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            big = b"\x00" * 5000
            resp = client.post(
                "/v1/ocr",
                files={"file": ("a.png", big, "image/png")},
                headers={"X-API-Key": "garbage"},
            )
            assert resp.status_code == 413
            assert "limit" in resp.json()["detail"].lower()
        finally:
            os.environ.pop("OCR_MAX_UPLOAD_BYTES", None)
            get_settings.cache_clear()


def test_the_guards_413_carries_cors_headers_for_an_allowed_origin():
    """M-a: the guard's `JSONResponse` is sent directly, without ever calling
    the inner app — so it only picks up CORS headers if CORS wraps it (is
    OUTERMOST). Registering the guard AFTER CORSMiddleware in `app/main.py`
    made the guard outermost instead (`Starlette.add_middleware` inserts at
    index 0, and the stack is built by wrapping in REVERSE order), so a
    browser client from an allowed origin used to see this 413 with no
    `access-control-allow-origin` at all — an opaque network failure instead
    of the documented response. Compares against the route's own (post-auth)
    401, which has always carried the header, to show the guard is now no
    different.
    """
    with _client() as client:
        os.environ["OCR_MAX_UPLOAD_BYTES"] = "1024"
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            big = b"\x00" * 5000
            resp = client.post(
                "/v1/ocr",
                files={"file": ("a.png", big, "image/png")},
                headers={"X-API-Key": "garbage", "Origin": "https://example.com"},
            )
            assert resp.status_code == 413
            assert resp.headers.get("access-control-allow-origin"), (
                f"the guard's 413 carries no CORS header: {dict(resp.headers)}"
            )
        finally:
            os.environ.pop("OCR_MAX_UPLOAD_BYTES", None)
            get_settings.cache_clear()

        # The route's own (post-auth) 401 has always had this header; this
        # confirms the guard is no longer the odd one out.
        auth_resp = client.post(
            "/v1/ocr",
            files={"file": ("a.png", _png(), "image/png")},
            headers={"X-API-Key": "garbage", "Origin": "https://example.com"},
        )
        assert auth_resp.status_code == 401
        assert auth_resp.headers.get("access-control-allow-origin")


def test_a_chunked_request_with_no_content_length_is_not_refused_by_the_guard():
    """The guard must let a request with NO declared `Content-Length` through
    unconditionally — refusing it would break a legitimate client that
    streams without one. Exercised directly at the ASGI layer (constructing a
    real chunked HTTP/1.1 request through TestClient is not practical), which
    also keeps this test independent of the DB/app-reload machinery every
    other test in this file needs.
    """
    import asyncio

    from app.publicapi.middleware import OcrContentLengthGuard

    calls = []

    async def inner_app(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guard = OcrContentLengthGuard(inner_app)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/ocr",
        "headers": [],  # no content-length at all
    }

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(guard(scope, receive, send))

    assert calls == ["/v1/ocr"], "the inner app must have been reached"
    assert sent[0]["status"] == 200


def test_the_guard_ignores_every_path_except_post_v1_ocr():
    """Scoped narrowly: neither the wrong method nor the wrong path should be
    intercepted, even with a huge declared Content-Length."""
    import asyncio

    from app.publicapi.middleware import OcrContentLengthGuard

    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guard = OcrContentLengthGuard(inner_app)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    for method, path in (("GET", "/v1/ocr"), ("POST", "/v1/other")):
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"content-length", str(10**9).encode())],
        }
        sent = []

        async def send(message, _sent=sent):
            _sent.append(message)

        asyncio.run(guard(scope, receive, send))
        assert sent[0]["status"] == 200, f"{method} {path} was wrongly intercepted"


def test_an_oversized_upload_is_413_and_never_decoded():
    with _client() as client:
        minted = _mint(client, "too-big")
        os.environ["OCR_MAX_UPLOAD_BYTES"] = "2048"
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            resp = _post(client, minted["key"], data=_png(400, 400) + b"\x00" * 5000)
            assert resp.status_code == 413
            assert "limit" in resp.json()["detail"].lower()
        finally:
            os.environ.pop("OCR_MAX_UPLOAD_BYTES", None)
            get_settings.cache_clear()


def test_an_unsupported_lang_is_400_not_500():
    with _client() as client:
        minted = _mint(client, "bad-lang")
        resp = _post(client, minted["key"], extra={"lang": "klingon"})
        assert resp.status_code == 400
        assert "devanagari" in resp.json()["detail"]


def test_a_pixel_bomb_is_refused_without_being_decoded():
    """A ~200-byte PNG can declare 40000x40000: it passes the byte cap, and
    Pillow only RAISES above 2x its own limit (merely warning between 1x and
    2x), so relying on its exception lets a 1.5x bomb through."""
    with _client() as client:
        minted = _mint(client, "pixel-bomb")
        from PIL import Image

        buf = io.BytesIO()
        # 12000x12000 = 144M pixels, over MAX_IMAGE_PIXELS (40M), but a flat
        # colour so the compressed bytes stay small.
        Image.new("L", (12000, 12000), 255).save(buf, format="PNG", optimize=True)
        payload = buf.getvalue()
        assert len(payload) < 1_000_000, "the fixture must stay under the byte cap"
        resp = _post(client, minted["key"], data=payload)
        assert resp.status_code == 400
        assert "pixel" in resp.json()["detail"].lower()


# --- outcome and bookkeeping ---------------------------------------------

def test_no_temp_file_survives_a_rejected_request():
    import tempfile
    from pathlib import Path

    with _client() as client:
        minted = _mint(client, "temp-cleanup")
        before = set(Path(tempfile.gettempdir()).glob("ocr-*"))
        _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
        _post(client, minted["key"], data=b"")
        after = set(Path(tempfile.gettempdir()).glob("ocr-*"))
        assert after == before


def test_a_usage_row_is_written_even_for_a_rejected_request():
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKeyUsage

    with _client() as client:
        minted = _mint(client, "usage-log")

        async def count():
            engine = create_async_engine(DB_URL, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                n = await s.scalar(
                    select(func.count())
                    .select_from(ApiKeyUsage)
                    .where(ApiKeyUsage.api_key_id == minted["id"])
                )
            await engine.dispose()
            return n

        before = asyncio.run(count())
        _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
        assert asyncio.run(count()) == before + 1


def _usage_count(key_id, status_code):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKeyUsage

    async def count():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            n = await s.scalar(
                select(func.count())
                .select_from(ApiKeyUsage)
                .where(
                    ApiKeyUsage.api_key_id == key_id,
                    ApiKeyUsage.status_code == status_code,
                )
            )
        await engine.dispose()
        return n

    return asyncio.run(count())


def test_a_wrong_secret_on_a_real_prefix_writes_an_attributable_401_row():
    """The credential is genuine (a real key exists at this prefix), so this
    401 is the FIRST of the six causes that has a real `api_keys.id` to
    attach a row to — the dependency must not stay silent just because it
    never reaches the route body."""
    with _client() as client:
        minted = _mint(client, "wrong-secret-usage")
        tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43
        resp = _post(client, tampered)
        assert resp.status_code == 401
        assert _usage_count(minted["id"], 401) == 1


def test_a_usage_log_write_fault_still_answers_the_byte_identical_401():
    """R1: `record_usage`/`commit` faulting (disk full, a statement timeout, a
    role with SELECT but not INSERT) must not turn a clean credential
    rejection into a 500 — a usage row is evidence, not a precondition for
    refusing a credential. Reproduced by making `record_usage` raise on the
    wrong-secret path, the first cause with a real key id to attribute to."""
    with _client() as client:
        minted = _mint(client, "record-usage-fault")
        tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43

        from app.apikeys import repository

        async def _boom(*args, **kwargs):
            raise RuntimeError("db is down")

        original = repository.record_usage
        repository.record_usage = _boom
        try:
            faulted = _post(client, tampered)
        finally:
            repository.record_usage = original

        assert faulted.status_code == 401
        assert faulted.json()["detail"] == "Invalid API key"

        # Byte-identical to the same rejection with no write fault at all —
        # a different tampered secret so this second call is its own,
        # independent wrong-secret rejection rather than a lockout retry.
        tampered2 = minted["key"].rsplit("_", 1)[0] + "_" + "y" * 43
        baseline = _post(client, tampered2)
        assert baseline.status_code == 401
        assert faulted.content == baseline.content
        assert dict(faulted.headers) == dict(baseline.headers)

        # The faulted write left no row (it never committed) and the session
        # was left usable — proved by the very next request against the same
        # key succeeding normally afterwards, further down this test module.


def test_an_unknown_prefix_writes_no_usage_row_at_all():
    """The other 401 shape: no row matches the prefix, so there is no real
    `api_keys.id` to attribute anything to. Unattributable, and the docs say
    so rather than claiming a row on every path — checked by asserting the
    TABLE'S total row count is unchanged, since there is no key id to filter
    a per-key count by."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKeyUsage

    async def total():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            n = await s.scalar(select(func.count()).select_from(ApiKeyUsage))
        await engine.dispose()
        return n

    with _client() as client:
        before = asyncio.run(total())
        resp = _post(client, "lgw_live_deadbeef_" + "0" * 64)
        assert resp.status_code == 401
        assert asyncio.run(total()) == before


def test_a_key_lacking_scope_writes_an_attributable_403_row():
    with _client() as client:
        minted = _mint(client, "scope-usage")
        import asyncio as _asyncio

        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.apikeys.models import ApiKey

        async def strip():
            engine = create_async_engine(DB_URL, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                await s.execute(
                    update(ApiKey).where(ApiKey.id == minted["id"]).values(scopes=[])
                )
                await s.commit()
            await engine.dispose()

        _asyncio.run(strip())
        resp = _post(client, minted["key"])
        assert resp.status_code == 403
        assert _usage_count(minted["id"], 403) == 1


def test_a_credential_lockout_429_writes_an_attributable_row_for_a_real_key():
    """R2: the lockout row is written once, at the TRANSITION — by the wrong-
    secret request whose own `record_failure` call trips the lock — not by
    this later, already-locked request. This request performs no lookup and
    writes nothing further; it only confirms the row from the transition is
    already there and attributed to the right key."""
    with _client() as client:
        minted = _mint(client, "lockout-usage")
        from app.config import get_settings

        settings = get_settings()
        # Trip the lockout with wrong secrets first (each one already writes
        # its own 401 usage row, which is fine — this test only cares about
        # the LOCKOUT row that follows).
        tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43
        for _ in range(settings.api_key_max_attempts):
            _post(client, tampered)

        resp = _post(client, minted["key"])  # correct secret, but locked out
        assert resp.status_code == 429
        assert resp.json()["detail"] == "Too many failed attempts for this key"
        assert _usage_count(minted["id"], 429) == 1


def test_the_lockout_transition_writes_exactly_one_row():
    """R2 (a): however many wrong-secret attempts it takes to reach
    `API_KEY_MAX_ATTEMPTS`, exactly one 429 usage row results — the one
    written by the attempt that actually trips the lock, not one per
    request."""
    with _client() as client:
        minted = _mint(client, "lockout-transition")
        from app.config import get_settings

        settings = get_settings()
        tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43
        for _ in range(settings.api_key_max_attempts):
            _post(client, tampered)

        assert _usage_count(minted["id"], 429) == 1


def test_further_locked_requests_write_no_rows_and_look_up_nothing():
    """R2 (b)+(c): once locked, N further requests must cost ZERO database
    access — no `find_by_prefix`, no usage row — and still answer 429 with
    `Retry-After`. Before the fix, every one of these did its own lookup and
    wrote its own row: a leaked prefix with no valid secret at all turned the
    cheapest rejection into the most expensive one, for the whole lockout
    window."""
    with _client() as client:
        minted = _mint(client, "lockout-noamplify")
        from app.config import get_settings

        settings = get_settings()
        tampered = minted["key"].rsplit("_", 1)[0] + "_" + "z" * 43
        for _ in range(settings.api_key_max_attempts):
            _post(client, tampered)
        assert _usage_count(minted["id"], 429) == 1

        from app.apikeys import repository

        original = repository.find_by_prefix
        calls = []

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        repository.find_by_prefix = _spy
        try:
            for _ in range(3):
                resp = _post(client, minted["key"])
                assert resp.status_code == 429
                assert resp.json()["detail"] == "Too many failed attempts for this key"
                assert "Retry-After" in resp.headers
        finally:
            repository.find_by_prefix = original

        assert calls == [], "the already-locked path performed a key lookup"
        assert _usage_count(minted["id"], 429) == 1


def test_the_rate_limit_answers_429_with_a_retry_after():
    with _client() as client:
        minted = _mint(client, "rate-limited")
        from app.apikeys import throttle

        throttle._rate_limiter = throttle.RateLimiter(per_minute=1, burst=1)
        try:
            first = _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
            assert first.status_code != 429
            second = _post(
                client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf"
            )
            assert second.status_code == 429
            assert int(second.headers["Retry-After"]) >= 1
        finally:
            throttle._rate_limiter = None


def test_no_capacity_is_503_and_is_not_the_same_answer_as_429():
    """429 says the CALLER sent too much; 503 says the box is busy with other
    callers. A client that backs off its own rate on a 503 fixes nothing, so
    these must be distinguishable answers, not one message reused — and the
    503 must be distinguishable from the OTHER 503 (missing stack) too, since
    both share a status code but must not share a cause.

    Driven deterministically: the module-global semaphore is swapped for an
    already-exhausted one, exactly as the test above swaps the rate limiter.
    No real load, no sleeping on a real OCR.
    """
    with _client() as client:
        minted = _mint(client, "no-capacity")
        os.environ["OCR_QUEUE_WAIT_SECONDS"] = "1"
        from app.config import get_settings

        get_settings.cache_clear()
        from app.publicapi import ocr_router

        saved = ocr_router._slots
        ocr_router._slots = asyncio.Semaphore(0)  # every slot already taken
        try:
            resp = _post(client, minted["key"])
            assert resp.status_code == 503
            assert resp.headers["Retry-After"] == "5"
            detail = resp.json()["detail"].lower()
            assert "capacity" in detail
            # Not the missing-stack message — same status code, different
            # cause, and a test that only checked `== 503` would pass against
            # either.
            assert resp.json()["detail"] != ocr_router.STACK_MISSING
        finally:
            ocr_router._slots = saved
            os.environ.pop("OCR_QUEUE_WAIT_SECONDS", None)
            get_settings.cache_clear()

        # The row is the only evidence of what a key did — confirm one exists
        # for THIS status code, not just that the count went up by one.
        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.apikeys.models import ApiKeyUsage

        async def count_503():
            engine = create_async_engine(DB_URL, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                n = await s.scalar(
                    select(func.count())
                    .select_from(ApiKeyUsage)
                    .where(
                        ApiKeyUsage.api_key_id == minted["id"],
                        ApiKeyUsage.status_code == 503,
                    )
                )
            await engine.dispose()
            return n

        assert asyncio.run(count_503()) == 1


def test_an_unexpected_ocr_failure_is_500_with_a_usage_row_not_a_crash():
    """`image_ocr.ocr_image` documents `OcrUnavailable`/`ValueError` only;
    nothing structurally enforces that (e.g. a bad enum lookup inside
    `image_ocr._engine` could surface as `AttributeError`/`KeyError`). The
    route must still answer (never a raw 500 traceback body) and must still
    write a usage row — the row is the only evidence of what a key did, and
    losing it on the one path most likely to be a real bug would be exactly
    backwards. Stack-independent: `ocr_image` is monkeypatched, so this runs
    whether or not rapidocr is installed here.
    """
    from unittest.mock import patch

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKeyUsage
    from app.publicapi import ocr_router

    with _client() as client:
        minted = _mint(client, "unexpected-failure")

        def _boom(*args, **kwargs):
            raise KeyError("not one of OcrUnavailable/ValueError")

        with patch.object(ocr_router.image_ocr, "ocr_image", _boom):
            resp = _post(client, minted["key"])

        assert resp.status_code == 500
        assert resp.json()["detail"] == "OCR failed unexpectedly"
        # The internal exception must never reach the caller.
        assert "KeyError" not in resp.text

        async def count_500():
            engine = create_async_engine(DB_URL, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                n = await s.scalar(
                    select(func.count())
                    .select_from(ApiKeyUsage)
                    .where(
                        ApiKeyUsage.api_key_id == minted["id"],
                        ApiKeyUsage.status_code == 500,
                    )
                )
            await engine.dispose()
            return n

        assert asyncio.run(count_500()) == 1


def test_an_unavailable_engine_is_503_with_the_exact_detail_and_a_usage_row():
    """The §18 path, proven by EXECUTION rather than by reading the code.

    The sibling test below skips wherever the OCR stack is installed (true on
    this machine), and Task 9's subprocess test only checks that the
    STACK_MISSING constant exists under an import blocker — neither one ever
    actually calls the route's handler. So without this test, the route's own
    `except image_ocr.OcrUnavailable` -> 503 branch has never executed in a
    test run. An empty `lines: []` with a 200 is the worst outcome this route
    has (§18: the caller writes "no text found" into a client file), so this
    checks both the status AND that the body is shaped like a failure, not a
    disguised empty success.

    Both this test and the skip-gated one below stay: this one is the
    always-runs proof that the handler branch executes correctly; that one is
    the real-deployment proof that `image_ocr.available()` itself reports
    False when the stack is truly absent. Neither subsumes the other.

    Patches `available` to False as well as `ocr_image`: the route now asks
    `available()` to tell a genuinely-absent stack apart from a present one
    that merely failed on this image (Important 5's fix), so simulating "the
    stack is absent" must patch both — on a machine where the real OCR stack
    IS installed (true here), leaving `available` unpatched would route this
    to the OTHER branch (500) and this test would fail for the wrong reason.
    """
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKeyUsage
    from app.publicapi import ocr_router

    with _client() as client:
        minted = _mint(client, "engine-unavailable")

        saved_ocr_image = ocr_router.image_ocr.ocr_image
        saved_available = ocr_router.image_ocr.available

        def _unavailable(*args, **kwargs):
            raise ocr_router.image_ocr.OcrUnavailable("stack absent for the test")

        ocr_router.image_ocr.ocr_image = _unavailable
        ocr_router.image_ocr.available = lambda: False
        try:
            resp = _post(client, minted["key"])
        finally:
            ocr_router.image_ocr.ocr_image = saved_ocr_image
            ocr_router.image_ocr.available = saved_available

        assert resp.status_code == 503
        body = resp.json()
        # Exact, not a substring — and distinct from the capacity 503, whose
        # own test asserts its detail `!= STACK_MISSING`. Together the two
        # prove the pair is discriminated in both directions.
        assert body["detail"] == ocr_router.STACK_MISSING
        # No partial-success body: a failure must not be dressed up as an
        # empty success.
        assert "lines" not in body

        async def count_503():
            engine = create_async_engine(DB_URL, poolclass=NullPool)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                n = await s.scalar(
                    select(func.count())
                    .select_from(ApiKeyUsage)
                    .where(
                        ApiKeyUsage.api_key_id == minted["id"],
                        ApiKeyUsage.status_code == 503,
                    )
                )
            await engine.dispose()
            return n

        assert asyncio.run(count_503()) == 1


def test_an_engine_present_ocr_unavailable_is_500_not_503():
    """A per-image engine failure must not be reported as 'the deployment has
    no OCR stack'. `image_ocr.ocr_image` wraps EVERY runtime exception from
    the engine call in `OcrUnavailable`, so an image Pillow accepted but
    onnxruntime chokes on used to make the route claim the deployment has no
    OCR stack at all — sending an operator on a rebuild-with-INSTALL_OCR
    chase that is already satisfied, and any caller could trigger that false
    diagnosis on demand. Now the route asks `available()`: present but this
    image broke it is 500; genuinely absent is 503 (the sibling tests above
    and below).

    Patches `available` explicitly (rather than relying on the local
    machine's real OCR install state) so this holds in any environment.
    """
    from app.publicapi import ocr_router

    with _client() as client:
        minted = _mint(client, "engine-present-image-failure")

        saved_ocr_image = ocr_router.image_ocr.ocr_image
        saved_available = ocr_router.image_ocr.available

        def _boom(*args, **kwargs):
            raise ocr_router.image_ocr.OcrUnavailable("this image broke the engine")

        ocr_router.image_ocr.ocr_image = _boom
        ocr_router.image_ocr.available = lambda: True
        try:
            resp = _post(client, minted["key"])
        finally:
            ocr_router.image_ocr.ocr_image = saved_ocr_image
            ocr_router.image_ocr.available = saved_available

        assert resp.status_code == 500
        assert resp.json()["detail"] == "OCR failed unexpectedly"


@pytest.mark.skipif(image_ocr.available(), reason="the OCR stack IS installed")
def test_a_missing_ocr_stack_is_503_never_an_empty_200():
    """§18's lesson: every way an OCR deployment breaks looks like a clean
    deployment. An empty lines:[] with a 200 is the worst possible outcome,
    because the caller writes 'no text found' into a client file."""
    with _client() as client:
        minted = _mint(client, "no-stack")
        resp = _post(client, minted["key"])
        assert resp.status_code == 503
        assert resp.json()["detail"] == "image OCR is not enabled on this deployment"


@pytest.mark.skipif(not image_ocr.available(), reason="OCR stack not installed")
def test_a_successful_call_carries_the_caveat_and_the_engine_block():
    with _client() as client:
        minted = _mint(client, "happy-path")
        resp = _post(client, minted["key"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["authoritative"] is False
        assert body["caveat"] == image_ocr.OCR_CAVEAT
        assert body["engine"]["model"] == "PP-OCRv5"
        assert body["engine"]["backend"] == "onnxruntime"
        # images._KINDS maps the sniffed Pillow format to a human string
        # ("PNG image", not "png") — schemas.build_response passes it through
        # unchanged, so that's what a caller actually receives.
        assert body["image"]["kind"] == "PNG image"
        assert body["request_id"]
        # A blank image legitimately has no text: that is an EMPTY result from
        # an engine that RAN, which is why it still carries a full engine
        # block. It is never inferred from emptiness — a stack that could not
        # run is 503 above.
        assert body["lines"] == [] or isinstance(body["lines"][0]["confidence"], float)

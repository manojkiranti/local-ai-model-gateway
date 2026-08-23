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

        # The property that makes this cause interesting: an expired key is
        # NOT a guess, so presenting one honestly must not consume a throttle
        # attempt (require_api_client's "not a guess" branch) — otherwise an
        # honest caller with a key that quietly expired would get locked out
        # of retrying with a NEW key from the same prefix on top of the
        # expiry itself. Directly observable here because of the in-process
        # TestClient noted above.
        assert prefix not in throttle._entries


# --- input guards --------------------------------------------------------

def test_a_pdf_is_rejected_with_a_pointer_to_what_is_accepted():
    with _client() as client:
        minted = _mint(client, "pdf-reject")
        resp = _post(client, minted["key"], data=b"%PDF-1.4\n", filename="a.pdf")
        assert resp.status_code == 400
        assert ".png" in resp.json()["detail"]


def test_an_empty_upload_is_rejected():
    with _client() as client:
        minted = _mint(client, "empty")
        resp = _post(client, minted["key"], data=b"")
        assert resp.status_code == 400


def test_a_gif_renamed_png_never_reaches_the_gif_decoder():
    """images._KINDS is a decoder allowlist on the SNIFFED format."""
    with _client() as client:
        minted = _mint(client, "renamed-gif")
        gif = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 20
        resp = _post(client, minted["key"], data=gif, filename="a.png")
        assert resp.status_code == 400


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
    """
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.apikeys.models import ApiKeyUsage
    from app.publicapi import ocr_router

    with _client() as client:
        minted = _mint(client, "engine-unavailable")

        saved = ocr_router.image_ocr.ocr_image

        def _unavailable(*args, **kwargs):
            raise ocr_router.image_ocr.OcrUnavailable("stack absent for the test")

        ocr_router.image_ocr.ocr_image = _unavailable
        try:
            resp = _post(client, minted["key"])
        finally:
            ocr_router.image_ocr.ocr_image = saved

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

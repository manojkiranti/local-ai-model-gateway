"""The declared-Content-Length guard, now covering more than one path.

It is a narrow, provably-correct PRE-AUTH gate: FastAPI spools a multipart
file part to disk before any dependency runs, so without this an attacker with
no key can make the gateway write an arbitrarily large body before it answers
401. It is NOT a substitute for a reverse-proxy cap — a client that lies about
its Content-Length, or omits it, sails past any declared-length check.
"""

import asyncio

from app.publicapi.middleware import UPLOAD_CAPS, UploadContentLengthGuard


def _call(path, method="POST", content_length=None):
    """Drive the ASGI guard directly and report (status, inner_was_called)."""
    seen = {"inner": False}
    sent = []

    async def inner(scope, receive, send):
        seen["inner"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}

    async def send(message):
        if message["type"] == "http.response.start":
            sent.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(UploadContentLengthGuard(inner)(scope, receive, send))
    return sent[0], seen["inner"]


def test_both_upload_paths_are_covered():
    assert set(UPLOAD_CAPS) == {"/v1/ocr", "/v1/extract"}


def test_an_oversized_ocr_body_is_413ed_before_the_app_is_called():
    status, inner_called = _call("/v1/ocr", content_length=999_000_000)
    assert status == 413
    assert inner_called is False, "the guard must answer without calling inward"


def test_an_oversized_extract_body_is_413ed_too():
    status, inner_called = _call("/v1/extract", content_length=999_000_000)
    assert status == 413
    assert inner_called is False


def test_a_small_body_passes_through():
    status, inner_called = _call("/v1/ocr", content_length=100)
    assert status == 200 and inner_called is True


def test_an_unguarded_path_passes_through_whatever_it_declares():
    status, inner_called = _call("/v1/chat", content_length=999_000_000)
    assert status == 200 and inner_called is True


def test_a_GET_is_never_guarded():
    status, inner_called = _call("/v1/ocr", method="GET", content_length=999_000_000)
    assert status == 200 and inner_called is True


def test_a_chunked_request_with_no_content_length_is_let_through():
    # Refusing it would break a legitimate streaming client; the route's own
    # counted cap still applies once the body is actually read.
    status, inner_called = _call("/v1/extract", content_length=None)
    assert status == 200 and inner_called is True


def test_the_two_paths_can_carry_DIFFERENT_caps():
    from app.config import get_settings

    settings = get_settings()
    assert settings.extract_max_upload_bytes != settings.ocr_max_upload_bytes, (
        "a 10 MB image cap is the wrong cap for a PDF — if these are ever "
        "equal by intent, delete this test and say why"
    )

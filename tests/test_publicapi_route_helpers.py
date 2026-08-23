"""The per-route policies every external endpoint shares.

These lived inline in a 322-line ocr_router.py. Endpoint three is where
hand-copying them starts silently going wrong, so they moved here first.
"""

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.publicapi import _route


@dataclass
class _FakeClient:
    key_id: str = "key-1"
    name: str = "test"
    scopes: tuple = ("ocr:read",)


class _FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeUpload:
    """Mimics the two methods of UploadFile that stream_to_temp uses."""

    def __init__(self, payload: bytes, chunk: int = 7):
        self._data = payload
        self._pos = 0
        self._chunk = chunk

    async def read(self, n):
        out = self._data[self._pos : self._pos + min(n, self._chunk)]
        self._pos += len(out)
        return out


def test_finish_writes_exactly_one_usage_row_and_commits():
    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="POST /x")
        await rec.finish(200, None, bytes_in=99)
        return session

    session = asyncio.run(go())
    assert len(session.added) == 1
    assert session.commits == 1
    row = session.added[0]
    assert row.status_code == 200 and row.bytes_in == 99
    assert row.route == "POST /x" and row.api_key_id == "key-1"


def test_finish_raises_when_given_a_detail_and_still_records_first():
    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="POST /x")
        with pytest.raises(HTTPException) as exc:
            await rec.finish(413, "too big", bytes_in=5)
        return session, exc.value

    session, exc = asyncio.run(go())
    assert exc.status_code == 413 and exc.detail == "too big"
    assert len(session.added) == 1, "the row must be written BEFORE the raise"
    # `add` before `raise` is not enough on its own: `add -> raise -> commit`
    # would satisfy the assertion above while never persisting the row.
    assert session.commits == 1, "the row must be COMMITTED before the raise"


def test_finish_passes_extra_columns_through():
    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="POST /x")
        await rec.finish(200, None, bytes_in=1, width=40, height=20, lines_out=3)
        return session.added[0]

    row = asyncio.run(go())
    assert (row.width, row.height, row.lines_out) == (40, 20, 3)


def test_a_request_id_is_minted_per_recorder():
    a = _route.UsageRecorder(_FakeSession(), client=_FakeClient(), route="r")
    b = _route.UsageRecorder(_FakeSession(), client=_FakeClient(), route="r")
    assert a.request_id != b.request_id
    assert len(a.request_id) == 32


def test_stream_to_temp_writes_the_bytes_and_reports_the_size():
    async def go():
        up = _FakeUpload(b"hello world, this is a body")
        return await _route.stream_to_temp(
            up, prefix="t-", suffix=".txt", max_bytes=1000
        )

    streamed = asyncio.run(go())
    try:
        assert streamed.exceeded is False
        assert streamed.size == 27
        assert streamed.path.read_bytes() == b"hello world, this is a body"
    finally:
        streamed.path.unlink(missing_ok=True)


def test_stream_to_temp_stops_at_the_cap_and_still_returns_a_path_to_unlink():
    async def go():
        up = _FakeUpload(b"x" * 500)
        return await _route.stream_to_temp(
            up, prefix="t-", suffix=".bin", max_bytes=100
        )

    streamed = asyncio.run(go())
    try:
        assert streamed.exceeded is True
        assert streamed.size > 100
        # The path exists even on the over-cap path: the caller unlinks in
        # `finally`, and a None here would leak the partial file.
        assert streamed.path.exists()
    finally:
        streamed.path.unlink(missing_ok=True)


class _RaisingUpload:
    """Yields one chunk, then fails — a client disconnecting mid-upload."""

    def __init__(self, payload: bytes, error: BaseException):
        self._data = payload
        self._error = error
        self._calls = 0

    async def read(self, n):
        self._calls += 1
        if self._calls == 1:
            return self._data
        raise self._error


def _leftovers(prefix: str) -> list[Path]:
    """Temp files stream_to_temp created under this run's unique prefix."""
    return sorted(Path(tempfile.gettempdir()).glob(prefix + "*"))


def test_stream_to_temp_removes_the_partial_file_when_the_read_raises():
    """No StreamedUpload is returned on this path, so the caller has no path to
    unlink in its own `finally` — the leak the over-cap branch avoids by
    returning one. A read genuinely can raise here (a client disconnecting
    mid-upload is the ordinary case) and the endpoint promises it does not keep
    caller uploads, so the partial file must not survive the exception."""
    prefix = f"leak-{uuid4().hex}-"
    boom = ValueError("connection dropped")

    async def go():
        up = _RaisingUpload(b"x" * 64, boom)
        with pytest.raises(ValueError) as exc:
            await _route.stream_to_temp(
                up, prefix=prefix, suffix=".bin", max_bytes=10_000
            )
        return exc.value

    raised = asyncio.run(go())
    assert raised is boom, "the exception must propagate unchanged, not be wrapped"
    assert _leftovers(prefix) == [], "the partial upload was left on disk"


def test_stream_to_temp_removes_the_partial_file_when_the_request_is_cancelled():
    """The case that discriminates: `asyncio.CancelledError` is a
    `BaseException`, NOT an `Exception`. A future edit narrowing
    `except BaseException` to `except Exception` — which reads like a tidy-up —
    silently reintroduces the leak for every cancelled upload, and a test that
    only raised `ValueError` would stay green through it."""
    prefix = f"leak-{uuid4().hex}-"
    cancelled = asyncio.CancelledError()
    assert not isinstance(cancelled, Exception), (
        "if this ever becomes an Exception the test below stops discriminating"
    )

    async def go():
        up = _RaisingUpload(b"x" * 64, cancelled)
        with pytest.raises(asyncio.CancelledError) as exc:
            await _route.stream_to_temp(
                up, prefix=prefix, suffix=".bin", max_bytes=10_000
            )
        return exc.value

    raised = asyncio.run(go())
    assert raised is cancelled, "cancellation must propagate unchanged"
    assert _leftovers(prefix) == [], "a cancelled upload was left on disk"


def test_enforce_rate_limit_is_a_no_op_when_the_bucket_allows():
    from app.apikeys.throttle import RateLimiter

    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="r")
        await _route.enforce_rate_limit(RateLimiter(per_minute=60, burst=5), rec)
        return session

    session = asyncio.run(go())
    assert session.added == []


def test_enforce_rate_limit_429s_with_retry_after_and_records_a_row():
    from app.apikeys.throttle import RateLimiter

    async def go():
        session = _FakeSession()
        rec = _route.UsageRecorder(session, client=_FakeClient(), route="r")
        limiter = RateLimiter(per_minute=60, burst=1)
        await _route.enforce_rate_limit(limiter, rec)      # consumes the token
        with pytest.raises(HTTPException) as exc:
            await _route.enforce_rate_limit(limiter, rec)
        return session, exc.value

    session, exc = asyncio.run(go())
    assert exc.status_code == 429
    assert exc.detail == "Rate limit exceeded for this API key"
    assert int(exc.headers["Retry-After"]) >= 1
    assert len(session.added) == 1

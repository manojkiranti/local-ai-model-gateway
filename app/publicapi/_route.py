"""The per-request policies every external API route shares.

These were invented inline in `ocr_router.py` and each of them exists because
getting it wrong is silent:

  * **A usage row is written before the response is raised, on every
    attributable path.** It is the only evidence of what a key did. Writing it
    after the raise means never writing it.
  * **`X-Request-Id` is minted here and belongs on the 200 only.** Every error
    path raises an `HTTPException`, and FastAPI builds that response from the
    exception — a header set on the success `Response` never reaches the
    client. Do not tell a caller to quote an id on a failure.
  * **An upload is streamed and counted, never `await file.read()` whole.** The
    cap has to bite before the bytes are all in memory.
  * **The temp path is returned even when the cap was exceeded**, so the
    caller's `finally` can unlink it. Returning None there leaks the partial
    file, and we told the caller we do not keep their uploads.
  * **The rate limit is checked before touching disk.**

Kept deliberately small. This is a policy toolbox, not a framework: a route
still reads top to bottom.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..apikeys.dependencies import ApiClient
from ..apikeys.repository import record_usage
from ..apikeys.throttle import RateLimiter

__all__ = [
    "UsageRecorder",
    "StreamedUpload",
    "stream_to_temp",
    "enforce_rate_limit",
    "CHUNK_BYTES",
]

CHUNK_BYTES = 1024 * 1024


class UsageRecorder:
    """One per request: mints the request id, times the call, writes the row."""

    def __init__(
        self, session: AsyncSession, *, client: ApiClient, route: str
    ) -> None:
        self._session = session
        self._client = client
        self._route = route
        self._started = time.monotonic()
        self.request_id = uuid4().hex

    @property
    def key_id(self) -> str:
        return self._client.key_id

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    async def finish(
        self,
        status_code: int,
        detail: str | None = None,
        *,
        bytes_in: int = 0,
        headers: dict[str, str] | None = None,
        **columns,
    ) -> None:
        """Record the row, commit, then raise if `detail` was given.

        `**columns` passes route-specific measurements straight through to
        `record_usage` (`width`/`height`/`lines_out` for OCR; nothing for a
        text extract, whose columns stay NULL).
        """
        await record_usage(
            self._session,
            api_key_id=self._client.key_id,
            route=self._route,
            status_code=status_code,
            bytes_in=bytes_in,
            duration_ms=self.elapsed_ms,
            **columns,
        )
        await self._session.commit()
        if detail is not None:
            raise HTTPException(
                status_code=status_code, detail=detail, headers=headers
            )


@dataclass(frozen=True)
class StreamedUpload:
    path: Path
    size: int
    exceeded: bool


async def stream_to_temp(
    upload, *, prefix: str, suffix: str, max_bytes: int
) -> StreamedUpload:
    """Stream an UploadFile to a temp file, stopping once `max_bytes` is passed.

    Returns the path in EVERY case, including the over-cap one, so the caller
    can unlink it. `size` is the count at the moment streaming stopped, which
    is what the usage row should record.
    """
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    path = Path(name)
    size = 0
    exceeded = False
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    exceeded = True
                    break
                out.write(chunk)
    except BaseException:
        # No StreamedUpload is returned on this path, so the caller has no
        # path to unlink in its own `finally` — the same leak the over-cap
        # branch avoids by returning one. A read can genuinely raise here
        # (a client disconnecting mid-upload is the ordinary case), so clean
        # up before the exception leaves. `BaseException` deliberately:
        # a cancelled request must not leave the caller's bytes on disk
        # either, and the exception is re-raised untouched.
        path.unlink(missing_ok=True)
        raise
    return StreamedUpload(path=path, size=size, exceeded=exceeded)


async def enforce_rate_limit(limiter: RateLimiter, recorder: UsageRecorder) -> None:
    """429 with a `Retry-After` if this key has spent its bucket."""
    wait = limiter.check(recorder.key_id)
    if wait is not None:
        await recorder.finish(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded for this API key",
            bytes_in=0,
            headers={"Retry-After": str(wait)},
        )

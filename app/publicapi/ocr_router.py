"""POST /v1/ocr — the text of one image, for an external API-key caller.

Three things here are not incidental:

  * **`asyncio.to_thread` is mandatory, not an optimisation.**
    `image_ocr.ocr_image` is synchronous and CPU-bound, so calling it directly
    in an `async def` route stops the whole event loop — a single 4-second OCR
    freezes every in-flight chat stream in this worker. Not a slowdown, a
    stall. Same pattern and same reason as `app/rag/worker.py` running Docling
    through `to_thread`.
  * **The semaphore is separate from the thread offload**, because
    `to_thread`'s default executor is much larger and would happily run many
    concurrent OCRs, each spawning onnxruntime's own intra-op threads,
    oversubscribing the box into swap.
  * **A missing OCR stack is 503, never an empty 200.** `docs/nrb-integration.md`
    §18 found five real deployment defects that all produced *successful*
    operations with no text. The route returns 200 with empty `lines` ONLY when
    the engine actually ran and genuinely found nothing — that case carries a
    full `engine` block — and "could not run" is never inferred from emptiness.

The temp file is unlinked in `finally` on EVERY path, 400s included: we told
the caller we do not store their images, and a rejected upload leaving bytes on
disk is the defect `app/rag/router.py` already compensates for.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..apikeys.dependencies import ApiClient, require_api_client
from ..apikeys.policy import SCOPE_OCR_READ
from ..apikeys.repository import record_usage
from ..apikeys.throttle import get_rate_limiter
from ..config import get_settings
from ..db.session import get_session
from ..files import image_ocr, images, ingest
from .schemas import OcrResponse, build_response

logger = logging.getLogger("app.publicapi.ocr")

router = APIRouter(prefix="/v1", tags=["ocr"])

_CHUNK = 1024 * 1024
_ROUTE = "POST /v1/ocr"

STACK_MISSING = "image OCR is not enabled on this deployment"

# Per PROCESS, like the rate limiter. Built lazily so importing this module does
# not need settings.
_slots: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(get_settings().ocr_max_concurrent)
    return _slots


@router.post(
    "/ocr",
    response_model=OcrResponse,
    summary="Read the text of one image (API key, scope ocr:read)",
    responses={
        400: {"description": "Not an image, corrupt, too many pixels, or a bad lang."},
        401: {"description": "Missing/invalid API key."},
        403: {"description": "The key lacks the ocr:read scope."},
        413: {"description": "Image exceeds the size limit."},
        429: {"description": "Rate limited. See Retry-After."},
        503: {"description": "OCR unavailable, or no capacity right now."},
        500: {"description": "Unexpected OCR failure (logged; should not happen)."},
    },
)
async def ocr(
    response: Response,
    file: UploadFile,
    lang: str | None = Form(default=None),
    client: ApiClient = Depends(require_api_client(SCOPE_OCR_READ)),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    request_id = uuid4().hex
    response.headers["X-Request-Id"] = request_id
    started = time.monotonic()
    dest: Path | None = None
    size = 0
    summary = None

    async def finish(status_code: int, detail: str | None = None, lines: int | None = None):
        """Record the usage row, then raise or return. Called on EVERY path."""
        await record_usage(
            session,
            api_key_id=client.key_id,
            route=_ROUTE,
            status_code=status_code,
            bytes_in=size,
            duration_ms=int((time.monotonic() - started) * 1000),
            width=summary.width if summary else None,
            height=summary.height if summary else None,
            lines_out=lines,
        )
        await session.commit()
        if detail is not None:
            raise HTTPException(status_code=status_code, detail=detail)

    try:
        # 1) rate limit, before touching disk
        wait = get_rate_limiter().check(client.key_id)
        if wait is not None:
            await record_usage(
                session,
                api_key_id=client.key_id,
                route=_ROUTE,
                status_code=429,
                bytes_in=0,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded for this API key",
                headers={"Retry-After": str(wait)},
            )

        # 2) language, before any IO — a bad value must not cost an upload
        chosen = (lang or image_ocr.DEFAULT_LANG).strip()
        if chosen not in image_ocr.SUPPORTED_LANGS:
            await finish(
                400,
                f"unsupported lang '{chosen}' (supported: "
                f"{', '.join(sorted(image_ocr.SUPPORTED_LANGS))})",
            )

        # 3) extension allowlist: images only. A PDF has a text layer worth
        #    reading, and handing page 1 to an OCR engine would discard it.
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ingest.IMAGE_EXTS:
            await finish(
                400,
                f"'{ext or file.filename}' is not an image — /v1/ocr accepts "
                f"{', '.join(sorted(ingest.IMAGE_EXTS))}",
            )

        # 4) stream to a temp file, counting bytes (413 before any decode)
        fd, temp_name = tempfile.mkstemp(prefix="ocr-", suffix=ext)
        dest = Path(temp_name)
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.ocr_max_upload_bytes:
                    await finish(
                        413,
                        f"image exceeds the "
                        f"{settings.ocr_max_upload_bytes // (1024 * 1024)} MB limit",
                    )
                out.write(chunk)
        if size == 0:
            await finish(400, "uploaded file is empty")

        # 5) header read: format allowlist on the SNIFFED format + the decoded
        #    PIXEL cap, both BEFORE any full decode. summarize_image owns both.
        try:
            summary = await asyncio.to_thread(images.summarize_image, dest)
        except Exception as exc:
            await finish(400, f"could not read the image ({exc})")

        # 6) a slot, or 503 — bounded, because an unbounded queue turns a load
        #    spike into an outage. Distinct from 429: that means YOU sent too
        #    much, this means the box is busy with other callers.
        try:
            await asyncio.wait_for(
                _semaphore().acquire(), timeout=settings.ocr_queue_wait_seconds
            )
        except asyncio.TimeoutError:
            await record_usage(
                session,
                api_key_id=client.key_id,
                route=_ROUTE,
                status_code=503,
                bytes_in=size,
                duration_ms=int((time.monotonic() - started) * 1000),
                width=summary.width,
                height=summary.height,
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OCR is at capacity; retry shortly",
                headers={"Retry-After": "5"},
            )

        try:
            result = await asyncio.to_thread(image_ocr.ocr_image, dest, lang=chosen)
        except image_ocr.OcrUnavailable as exc:
            # The stack is absent or the engine could not run. 503 and a clear
            # reason — NEVER an empty 200. See the module docstring.
            logger.warning("ocr unavailable (request %s): %s", request_id, exc)
            await finish(503, STACK_MISSING)
        except ValueError as exc:
            await finish(400, str(exc))
        except Exception:
            # ocr_image documents OcrUnavailable/ValueError only, but nothing
            # enforces that contract (e.g. a bad enum lookup inside
            # image_ocr._engine could surface as AttributeError/KeyError). A
            # usage row is the only evidence of what a key did, so an
            # unexpected failure must still leave one — and the caller gets a
            # generic message, never an internal exception string. Deliberately
            # `Exception`, not `BaseException`: asyncio.CancelledError must stay
            # uncaught, because a cancelled request is not a server error and
            # swallowing it breaks cancellation.
            logger.exception("ocr failed unexpectedly (request %s)", request_id)
            await finish(500, "OCR failed unexpectedly")
        finally:
            _semaphore().release()

        await finish(200, None, lines=len(result.lines))
        logger.info(
            "ocr ok request=%s key=%s lines=%d %dx%d frames=%d %dms",
            request_id, client.key_id, len(result.lines),
            summary.width, summary.height, summary.frames,
            int((time.monotonic() - started) * 1000),
        )
        return build_response(result, summary, request_id)
    finally:
        # Every path, success and failure. We told the caller we do not keep
        # their images; leaving one in /tmp makes that untrue.
        if dest is not None:
            dest.unlink(missing_ok=True)
        await file.close()

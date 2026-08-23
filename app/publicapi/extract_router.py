"""POST /v1/extract — the text and structure of one document.

The engines are all pre-existing and pure; this file is the HTTP boundary and
nothing else. Two things it does NOT do, deliberately:

  * **It never calls a language model.** Named-field extraction is
    `/v1/extract/fields` (Phase B) behind its own scope, so a key provisioned
    for text cannot silently buy model access by adding a form field.
  * **It never infers "scanned" from empty text.** `extraction.read_any`
    reports `text_pages == 0` as a FACT and this route turns it into a 422.
    docs/nrb-integration.md §18 found five deployment defects that all produced
    successful operations with no text; a 200 with empty text is the worst
    outcome available, because the caller writes "no text found" into a file.

`asyncio.to_thread` is mandatory, not an optimisation: parsing a 500-page PDF
is synchronous and CPU-bound, and doing it in an `async def` stalls the whole
event loop for every in-flight chat stream in this worker. The semaphore is
separate because `to_thread`'s default executor is much larger.

**The response is built BEFORE the success usage row is written, not after.**
`build_extract_response` pairs lines to confidences with `zip(..., strict=True)`
and can raise `ValueError` on a genuine length mismatch. Writing the 200 row
first and building the body second would leave a `200` usage row on record for
a request that actually failed — the same "a usage row is evidence of what
happened" rule `_route.UsageRecorder` exists for. So the body is built first;
a failure there is logged with the request id and answered with a generic 500,
and that is the ONE usage row this request gets.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..apikeys.dependencies import ApiClient, require_api_client
from ..apikeys.policy import SCOPE_DOCUMENT_READ
from ..apikeys.throttle import get_extract_rate_limiter
from ..config import get_settings
from ..db.session import get_session
from ..files import image_ocr, readers
from . import _route, extraction
from .extract_schemas import ExtractResponse, build_extract_response

logger = logging.getLogger("app.publicapi.extract")

router = APIRouter(prefix="/v1", tags=["extract"])

_ROUTE = "POST /v1/extract"

# Per PROCESS, like the rate limiter. Built lazily so importing this module
# does not need settings.
_slots: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(get_settings().extract_max_concurrent)
    return _slots


@router.post(
    "/extract",
    response_model=ExtractResponse,
    summary="Extract text and structure from one document (API key, scope document:read)",
    responses={
        400: {
            "description": (
                "Unsupported extension, empty upload, a corrupt/unreadable "
                "file, or a bad `lang`."
            )
        },
        401: {
            "description": (
                "The API key is missing, malformed, unknown, wrong, revoked "
                "or expired — one message for all six causes."
            )
        },
        403: {
            "description": (
                "The key is genuine but lacks `document:read`. Ask an admin "
                "to re-mint it; do not rotate the key."
            )
        },
        413: {"description": "Over `EXTRACT_MAX_UPLOAD_BYTES` (default 25 MB)."},
        422: {
            "description": (
                "A PDF whose pages carry no text layer. Its text needs OCR, "
                "which this endpoint does not do for PDFs."
            )
        },
        429: {
            "description": (
                "This key's own rate limit, or its prefix is credential-"
                "locked. `Retry-After` and the detail text distinguish which."
            )
        },
        503: {
            "description": (
                "The box is at capacity right now "
                "(`EXTRACT_MAX_CONCURRENT`/`EXTRACT_QUEUE_WAIT_SECONDS`), or "
                "— for an IMAGE upload only — the OCR stack is not installed "
                "on this deployment."
            )
        },
        500: {
            "description": (
                "An unexpected failure. Logged server-side, never echoed; "
                "report it rather than retrying."
            )
        },
    },
)
async def extract(
    response: Response,
    file: UploadFile,
    lang: str | None = Form(default=None),
    client: ApiClient = Depends(require_api_client(SCOPE_DOCUMENT_READ)),
    session: AsyncSession = Depends(get_session),
):
    """Return the text of one uploaded document, plus what kind of text it is.

    Accepts `.pdf .docx .txt .md .json` (read from the document's own text
    layer), `.xlsx .csv` (returned as `sheets`, not flat lines), and
    `.png .jpg .jpeg .webp .tif .tiff .bmp` (read by OCR).

    The `source` block is the part to read first. `route: "native"` means the
    text came from the document's own text layer and is exact —
    `authoritative` is true and there is no caveat. `route: "ocr"` means it was
    machine-read: `authoritative` is false, a `caveat` is present, and no
    figure, date, account number or contact detail from it should be treated
    as correct without being checked against the original.

    A PDF whose pages carry no text layer is a **422**, not an empty 200 — its
    text needs OCR, and silently returning nothing would read as "this
    document is blank". `lang` applies to image uploads only.
    """
    settings = get_settings()
    recorder = _route.UsageRecorder(session, client=client, route=_ROUTE)
    response.headers["X-Request-Id"] = recorder.request_id
    dest: Path | None = None
    size = 0

    async def finish(status_code: int, detail: str | None = None, lines: int | None = None):
        await recorder.finish(status_code, detail, bytes_in=size, lines_out=lines)

    try:
        await _route.enforce_rate_limit(get_extract_rate_limiter(), recorder)

        chosen = (lang or image_ocr.DEFAULT_LANG).strip()
        if chosen not in image_ocr.SUPPORTED_LANGS:
            await finish(
                400,
                f"unsupported lang '{chosen}' (supported: "
                f"{', '.join(sorted(image_ocr.SUPPORTED_LANGS))})",
            )

        ext = Path(file.filename or "").suffix.lower()
        if ext not in extraction.EXTRACT_EXTS:
            # The caller's own unbounded filename, reflected back. JSON-encoded
            # so it is not an injection, but truncate it anyway.
            shown = (ext or (file.filename or ""))[:100]
            await finish(
                400,
                f"'{shown}' is not a supported document — /v1/extract accepts "
                f"{', '.join(sorted(extraction.EXTRACT_EXTS))}",
            )

        streamed = await _route.stream_to_temp(
            file, prefix="extract-", suffix=ext,
            max_bytes=settings.extract_max_upload_bytes,
        )
        dest, size = streamed.path, streamed.size
        if streamed.exceeded:
            # Same wording as `UploadContentLengthGuard`'s declared-length
            # 413: both are the same cap on the same upload, so a caller
            # should see one phrase for it regardless of which check caught
            # it first, not a third variant coined for the streamed-count
            # path alone.
            await finish(
                413,
                f"upload exceeds the "
                f"{settings.extract_max_upload_bytes // (1024 * 1024)} MB limit",
            )
        if size == 0:
            await finish(400, "uploaded file is empty")

        # zip-bomb guard for the OOXML formats: refuse absurd expansion. Same
        # cap, same wording as `app/files/router.py`'s upload path — one
        # physical risk (an OOXML member inflating past
        # `upload_xlsx_max_uncompressed` in-process) should read as one
        # message to a caller, not a variant coined because this endpoint
        # happens to be reached with an API key instead of a JWT. Checked
        # before the semaphore is acquired: a crafted file should not spend a
        # concurrency slot to be rejected.
        if ext in (".xlsx", ".docx"):
            try:
                with zipfile.ZipFile(dest) as zf:
                    uncompressed = sum(i.file_size for i in zf.infolist())
            except zipfile.BadZipFile:
                await finish(400, f"file is not a valid {ext} document")
            if uncompressed > settings.upload_xlsx_max_uncompressed:
                await finish(400, "file expands too large to process safely")

        try:
            await asyncio.wait_for(
                _semaphore().acquire(), timeout=settings.extract_queue_wait_seconds
            )
        except asyncio.TimeoutError:
            await recorder.finish(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "extraction is at capacity; retry shortly",
                bytes_in=size,
                headers={"Retry-After": "5"},
            )

        try:
            extracted = await asyncio.to_thread(
                extraction.read_any, dest, lang=chosen
            )
        except image_ocr.OcrUnavailable as exc:
            # Only reachable for an IMAGE upload. Same split as /v1/ocr: the
            # package importing but the engine failing to build is a 500,
            # genuinely absent is a 503 — collapsing them sends an operator to
            # rebuild with a flag that is already set.
            logger.warning(
                "extract ocr unavailable (request %s): %s", recorder.request_id, exc
            )
            if image_ocr.available():
                await finish(500, "extraction failed unexpectedly")
            await finish(503, "image OCR is not enabled on this deployment")
        except readers.ReadError as exc:
            await finish(400, f"could not read the document ({exc})")
        except Exception:
            logger.exception(
                "extract failed unexpectedly (request %s)", recorder.request_id
            )
            await finish(500, "extraction failed unexpectedly")
        finally:
            _semaphore().release()

        if extracted.is_scanned_pdf:
            await finish(
                422,
                f"this PDF has {extracted.pages} page(s) but no text layer — "
                "its text would need OCR, which /v1/extract does not do for PDFs",
            )

        # Build the body BEFORE recording success: `build_extract_response`
        # can raise `ValueError` (a genuinely mismatched lines/confidences
        # length, via `zip(..., strict=True)`), and a usage row already
        # written as 200 must not survive that — see the module docstring.
        try:
            result = build_extract_response(extracted, recorder.request_id)
        except ValueError:
            logger.exception(
                "extract response could not be built (request %s)",
                recorder.request_id,
            )
            await finish(500, "extraction failed unexpectedly")
            raise AssertionError("unreachable")  # finish() above always raises

        await finish(200, None, lines=len(extracted.lines))
        logger.info(
            "extract ok request=%s key=%s kind=%s route=%s lines=%d %dms",
            recorder.request_id, client.key_id, extracted.kind,
            extracted.route, len(extracted.lines), recorder.elapsed_ms,
        )
        return result
    finally:
        if dest is not None:
            dest.unlink(missing_ok=True)
        await file.close()

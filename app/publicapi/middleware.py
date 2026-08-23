"""ASGI guard: reject a declared-oversized `POST /v1/ocr` before the body is
spooled to disk.

FastAPI resolves `await request.form()` (which is how it reads a multipart
upload) BEFORE it solves any dependency, so the uploaded file part is written
to a `SpooledTemporaryFile` before `require_api_client` ever runs — and
Starlette's own `max_part_size` only bounds NON-file form parts; a part with a
`filename` gets no size check at all. Without this, an attacker with no key at
all can send an arbitrarily large file part and make the gateway read and
spool the whole thing to the container's temp filesystem before it ever
answers 401. Concurrent repeats fill the disk.

This checks only `Content-Length`, and only for this one path+method, and it
is registered beside the routers inside the SAME `external_api_enabled` guard
in `app/main.py` — a deployment with the feature off gains no middleware. A
CHUNKED request (no `Content-Length` header at all) is let through
unconditionally: refusing it would break a legitimate client that streams
without declaring a length, and the streamed byte-counting cap already in
`ocr_router.py` (`OCR_MAX_UPLOAD_BYTES`, enforced chunk by chunk as the body is
read) still applies once such a request is actually processed.

This is a cheap, early rejection for the declared-length case ONLY — it is NOT
a substitute for a reverse-proxy body cap (e.g. nginx's
`client_max_body_size`). See docs/external-api.md's "Turning it on" section:
only the proxy stops the bytes from arriving on the wire in the first place: a
client that lies about its own Content-Length, or omits it, sails past this
middleware exactly as it would past any other declared-length check.

**M-e caveat:** the path check below is an EXACT match on `scope["path"]`,
which is the path AFTER Starlette strips any mounted `root_path` — but a
reverse proxy that forwards under a prefix and sets `--root-path` (e.g.
`--root-path /api`) makes `scope["path"]` `/api/v1/ocr`, not `/v1/ocr`. The
`!=` comparison then fails for every request, and this guard quietly stops
existing while every test here still passes (none of them run behind a
`root_path`). Chose a comment over a suffix match (`path.endswith(OCR_PATH)`)
deliberately: a suffix match widens the check to any path ending in
`/v1/ocr`, including one this route was never meant to guard behind a proxy
that rewrites paths in less predictable ways, and this module's whole job is
to be a narrow, provably-correct pre-auth gate — trading that for a guess at
every possible proxy prefix is the wrong direction for a bank's first
externally-reachable upload endpoint. If this gateway is ever deployed behind
a path-prefixing proxy, that prerequisite belongs in the runbook (it does —
see docs/external-api.md's "Turning it on" section) and/or `OCR_PATH` needs to
become configurable, not this comparison guessed at.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import get_settings

# Kept as a constant rather than imported from ocr_router: importing that
# module here would be harmless today, but this file's whole job is to run
# BEFORE any dependency in that router does, so it stays decoupled from it.
OCR_PATH = "/v1/ocr"


class OcrContentLengthGuard:
    """413s a `POST /v1/ocr` whose declared `Content-Length` exceeds
    `OCR_MAX_UPLOAD_BYTES`, before Starlette/FastAPI parse the body at all."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            # M-e: EXACT match on the post-root_path path. Behind a
            # path-prefixing reverse proxy running with `--root-path /api`,
            # `scope["path"]` is `/api/v1/ocr` and this never matches — see
            # the module docstring for why that stays a documented
            # prerequisite rather than a suffix match here.
            or scope.get("path") != OCR_PATH
        ):
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = None
            if length is not None:
                # Read fresh every request (not cached at construction) so a
                # settings-cache-clearing test, or a live config reload, is
                # honoured the same way the route's own check already is.
                max_bytes = get_settings().ocr_max_upload_bytes
                if length > max_bytes:
                    response = JSONResponse(
                        {
                            "detail": (
                                f"image exceeds the {max_bytes // (1024 * 1024)} "
                                "MB limit"
                            )
                        },
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)

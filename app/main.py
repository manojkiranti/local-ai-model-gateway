"""FastAPI application assembly: lifespan, CORS, routers, health."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .apikeys.router import router as api_keys_router
from .auth.router import router as auth_router
from .chat.router import router as chat_router
from .config import Settings, get_settings
from .db.session import engine
from .files.router import router as files_router
from .files.store import file_store
from .history.router import router as sessions_router
from .mcp.client import MCPClient
from .mcp.grants_router import router as mcp_grants_router
from .mcp.router import router as mcp_router
from .nrb.router import router as nrb_router
from .ollama.client import OllamaClient, OllamaError
from .publicapi.extract_router import router as extract_router
from .publicapi.middleware import UploadContentLengthGuard
from .publicapi.ocr_router import router as ocr_router
from .rag.jobs_router import router as ingest_jobs_router
from .rag.router import router as departments_router
from .tools.router import router as tools_router
from .users.router import router as users_router

logger = logging.getLogger("app.main")


def _build_mcp_client(settings: Settings) -> MCPClient:
    return MCPClient(
        server_url=settings.mcp_server_url,
        auth_token=settings.mcp_auth_token,
        tool_mode=settings.mcp_tool_mode,
        allowlist=settings.tool_allowlist,
        read_prefixes=settings.read_prefixes,
        write_keywords=settings.write_keywords,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Under `uvicorn app.main:app` (the documented run command and the
    # Dockerfile's CMD) uvicorn only configures ITS OWN loggers — the root
    # logger stays at WARNING with no handler attached, so an INFO record from
    # "app.main" is discarded before it reaches any output (verified: zero
    # occurrences of "chat backend:" with plain `uvicorn app.main:app`, with or
    # without `--log-level info`, since that flag only tunes uvicorn's loggers
    # too). The "chat backend: ..." line below is the operator's only defence
    # against a misspelled AGENT_BASE_URL being silently dropped
    # (`extra="ignore"`), so it must actually reach stdout. Guarded so this
    # never clobbers a root logger some other entry point (a test runner, a
    # future ASGI server config) already configured.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    app.state.settings = settings
    # One shared chat client (connection pool) for the whole process. This is
    # the CHAT backend, which may be a different server than the embeddings one
    # (vLLM for chat, Ollama for embeddings) — see Settings.chat_base_url.
    # Logged because AGENT_BASE_URL is silently dropped when misspelled
    # (`extra="ignore"`), and the fallback to Ollama would otherwise look
    # exactly like a successful cutover.
    app.state.ollama = OllamaClient(settings.chat_base_url, settings.ollama_timeout)
    logger.info(
        "chat backend: %s (model %s)", settings.chat_base_url, settings.agent_model
    )
    # MCP client (the gateway is the MCP client) + file store for generated files.
    app.state.mcp = _build_mcp_client(settings)
    file_store.configure(settings.files_dir)
    # Optional: pay the OCR model load at startup instead of charging it to the
    # first caller. `prewarm()` actually builds the engine (available() alone
    # only imports the package — see its docstring); failure is logged and
    # ignored either way, because a deployment without the OCR stack must
    # still boot, and /v1/ocr answers 503 on its own.
    if settings.external_api_enabled and settings.ocr_prewarm:
        from .files import image_ocr as _image_ocr

        try:
            loaded = await asyncio.to_thread(_image_ocr.prewarm)
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("OCR pre-warm failed: %s", exc)
        else:
            if loaded:
                logger.info(
                    "OCR pre-warm: engine loaded (lang=%s)", _image_ocr.DEFAULT_LANG
                )
            else:
                logger.warning(
                    "OCR pre-warm: OCR stack unavailable; first call will 503"
                )
    try:
        yield
    finally:
        await app.state.ollama.aclose()
        # Dispose the DB engine so pooled connections don't outlive the loop
        # (correct on real shutdown; also keeps test event loops isolated).
        await engine.dispose()


app = FastAPI(
    title="Local LLM Gateway",
    version="0.3.0",
    description="Single authenticated front door: auth + users + chat + agent/tools/files over Ollama.",
    lifespan=lifespan,
    # Only the two tags this residual-fix wave added get a description here.
    # Every other tag in the app renders with a bare name and no description
    # too, and inventing text for a feature this wave did not review would be
    # a guess dressed as documentation — see docs/external-api.md for the
    # full runbook either tag's routes point back to.
    openapi_tags=[
        {
            "name": "ocr",
            "description": (
                "External, API-key-authenticated OCR. `POST /v1/ocr` reads "
                "an image and returns machine-read text — never a "
                "transcription, and never authoritative. Gated behind "
                "`EXTERNAL_API_ENABLED`; absent when the switch is off. "
                "See docs/external-api.md."
            ),
        },
        {
            "name": "api-keys",
            "description": (
                "Admin-only (JWT) management of the API keys that authenticate "
                "the external endpoints (`ocr`, `extract`) via `X-API-Key`. "
                "A key carries explicit scopes — `ocr:read` and `document:read` "
                "are separate grants, and one does not imply the other. "
                "Minting, listing and "
                "revoking a key is a human, privileged act — a key can never "
                "manage keys. Gated behind `EXTERNAL_API_ENABLED`; absent when "
                "the switch is off. See docs/external-api.md."
            ),
        },
        {
            "name": "extract",
            "description": (
                "External, API-key-authenticated document extraction. "
                "`POST /v1/extract` returns the text and structure of one "
                "uploaded PDF, DOCX, TXT, MD, JSON, XLSX, CSV or image. "
                "**Read the `source` block first:** `route: \"native\"` means "
                "the text came from the document's own text layer and is exact "
                "(`authoritative: true`, no caveat); `route: \"ocr\"` means it "
                "was machine-read, and no figure, date or account number from "
                "it should be trusted without checking the original. Requires "
                "scope `document:read` — an `ocr:read` key is refused with 403, "
                "not 401. Gated behind `EXTERNAL_API_ENABLED`; absent when the "
                "switch is off. See docs/external-api.md."
            ),
        },
    ],
)

_settings = get_settings()

# M-a: `UploadContentLengthGuard` must be added to the middleware stack BEFORE
# `CORSMiddleware`. `Starlette.add_middleware` inserts each new middleware at
# position 0 of `app.user_middleware`, and the ASGI app is built by wrapping
# in REVERSE of that list — so the LAST middleware added ends up OUTERMOST,
# the opposite of how this reads. Adding the guard here, before CORS, means
# CORS is added second, lands at index 0, and ends up outermost — wrapping
# the guard. That is what makes the guard's pre-auth 413 (a response it sends
# directly, without ever calling the inner app) pass back OUT through CORS's
# response handling and pick up `access-control-allow-origin` — verified with
# an `Origin:` header (see docs/external-api.md and
# tests/test_ocr_api_boundaries.py). Registering it AFTER CORS (the previous
# order) made the guard outermost instead, so its 413 never reached CORS at
# all and a browser client saw an opaque network failure.
if _settings.external_api_enabled:
    app.add_middleware(UploadContentLengthGuard)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(OllamaError)
async def _ollama_error_handler(request: Request, exc: OllamaError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/health", tags=["health"])
async def health(request: Request) -> JSONResponse:
    """Liveness + CHAT and EMBEDDINGS backend reachability.

    Before the chat/embeddings split, one URL meant one probe covered both
    roles. It no longer does: `AGENT_BASE_URL` can point chat at vLLM while
    embeddings stay on Ollama, and a dead embeddings server (RAG search/ingest
    broken) behind a healthy vLLM used to be completely invisible here — 200
    "ok", green Docker healthcheck, silent RAG outage.

    The `ollama` key is UNCHANGED for client compatibility — it is still the
    CHAT backend's reachability, still named `ollama` on purpose. A second
    `embeddings` key reports `ollama_base_url`'s own reachability additively.

    HTTP status code and the top-level `status` string stay driven by the CHAT
    backend alone, exactly as before this field existed: 200/"ok" when chat is
    reachable, 503/"degraded" when it is not. That is deliberate, not an
    oversight — restarting the gateway container can plausibly fix a wedged
    chat client, so that is what Docker's `HEALTHCHECK` (which acts on the
    status code) should trigger on; restarting the gateway does nothing for a
    dead EXTERNAL embeddings server, so a dead embeddings backend does not flip
    the code or bounce the container. It is still not invisible: `status`
    reports **"embeddings_degraded"** and `embeddings.reachable` is `false`
    whenever chat is fine but embeddings is not, so any monitor parsing the
    JSON body — not just the HTTP status — has a signal to alert on.

    When `ollama_base_url == chat_base_url` (the default, unset
    `AGENT_BASE_URL`), there is exactly one server to ask, so only one probe
    runs and both fields report its result.
    """
    settings = request.app.state.settings
    chat_reachable = await request.app.state.ollama.is_healthy()

    if settings.ollama_base_url == settings.chat_base_url:
        embeddings_reachable = chat_reachable
    else:
        embed_client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        try:
            embeddings_reachable = await embed_client.is_healthy()
        finally:
            await embed_client.aclose()

    if not chat_reachable:
        status = "degraded"
    elif not embeddings_reachable:
        status = "embeddings_degraded"
    else:
        status = "ok"

    return JSONResponse(
        status_code=200 if chat_reachable else 503,
        content={
            "status": status,
            "ollama": {
                "base_url": settings.chat_base_url,
                "reachable": chat_reachable,
            },
            "embeddings": {
                "base_url": settings.ollama_base_url,
                "reachable": embeddings_reachable,
            },
        },
    )


# Public
app.include_router(auth_router)
# Authenticated
app.include_router(users_router)
app.include_router(chat_router)
app.include_router(tools_router)
app.include_router(mcp_router)
app.include_router(mcp_grants_router)
app.include_router(files_router)
app.include_router(sessions_router)
app.include_router(departments_router)
app.include_router(ingest_jobs_router)
# NRB operations (admin): pipeline trigger + run/status. Thin — every handler
# calls one `app.nrb.pipeline` service and shapes the answer.
app.include_router(nrb_router)
# The external API is opt-in. When disabled the routes do not exist at all —
# see the comment on `Settings.external_api_enabled` for why 404 is right here
# and 503 is right for a missing OCR stack.
if get_settings().external_api_enabled:
    app.include_router(api_keys_router)
    app.include_router(ocr_router)
    app.include_router(extract_router)
    # `UploadContentLengthGuard` itself is registered further up, alongside
    # CORSMiddleware — see the comment there for why the ordering matters. It
    # rejects a declared-oversized upload body before FastAPI spools it to
    # disk, and before authentication runs. Still gated on the same
    # `external_api_enabled` flag: a deployment with the feature off gains no
    # middleware.

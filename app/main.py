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
from .mcp.router import router as mcp_router
from .nrb.router import router as nrb_router
from .ollama.client import OllamaClient, OllamaError
from .publicapi.middleware import OcrContentLengthGuard
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
    settings = get_settings()
    app.state.settings = settings
    # One shared Ollama client (connection pool) for the whole process.
    app.state.ollama = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
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
)

_settings = get_settings()
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
    """Liveness + Ollama reachability. 200 when reachable, 503 when degraded."""
    settings = request.app.state.settings
    reachable = await request.app.state.ollama.is_healthy()
    return JSONResponse(
        status_code=200 if reachable else 503,
        content={
            "status": "ok" if reachable else "degraded",
            "ollama": {"base_url": settings.ollama_base_url, "reachable": reachable},
        },
    )


# Public
app.include_router(auth_router)
# Authenticated
app.include_router(users_router)
app.include_router(chat_router)
app.include_router(tools_router)
app.include_router(mcp_router)
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
    # Rejects a declared-oversized /v1/ocr body before FastAPI spools it to
    # disk, and before authentication runs — see the module docstring for why
    # that ordering matters. Added only in this branch, so a deployment with
    # the feature off gains no middleware.
    app.add_middleware(OcrContentLengthGuard)

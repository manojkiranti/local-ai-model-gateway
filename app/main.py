"""FastAPI application assembly: lifespan, CORS, routers, health."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth.router import router as auth_router
from .chat.router import router as chat_router
from .config import Settings, get_settings
from .db.session import engine
from .files.router import router as files_router
from .files.store import file_store
from .history.router import router as sessions_router
from .mcp.client import MCPClient
from .mcp.router import router as mcp_router
from .ollama.client import OllamaClient, OllamaError
from .tools.router import router as tools_router
from .users.router import router as users_router


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

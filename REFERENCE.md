# Reference: sibling project `local-ai-model`

**Path:** `/home/manoj/newlaptop/projects/python/local-ai-model`

The **original / reference implementation** that this gateway was ported from.
Code was first built and proven there, then absorbed into this gateway
(Pattern A — one authenticated FastAPI front door).

> ⚠️ **Do not edit the sibling as part of gateway work.** It is a read-only
> reference. If gateway code needs a proven pattern, copy it *here* and adapt.

## What it is
"Ollama Gateway" — a small, config-driven FastAPI service in front of a local
Ollama server. Talks to Ollama's REST API directly with `httpx` (no `ollama`
SDK), fully async with a shared `httpx.AsyncClient` via FastAPI lifespan.
Streaming + non-streaming chat, embeddings, clear upstream error handling
(502 when Ollama down, 404 when model not pulled).

## Port convention (never clash)
- Sibling `local-ai-model` → **8001** (reference service)
- This gateway `local-ai-model-gateway` → **8000** (product front door)
- Never run both on the same port.

## Layout (sibling)
- `app/main.py`, `app/config.py`, `app/schemas.py`
- `app/ollama_client.py` — httpx REST client (the pattern this gateway reuses)
- `app/agent.py` — hand-rolled agent loop
- `app/mcp_client.py`, `app/tool_registry.py` — MCP client + tool registry
- `app/file_store.py` — generated-file store
- `app/routers/` — `chat`, `agent`, `tools`, `files`, `models`, `embeddings`
- No `CLAUDE.md`; project doc lives in `README.md`. Uses `requirements.txt`
  (not this gateway's `.venv`/alembic setup) and its own `.venv`.

## Key differences vs this gateway
| Aspect | sibling `local-ai-model` | this gateway |
|---|---|---|
| Role | reference/original | authenticated product front door |
| Auth | none | JWT (PyJWT HS256) + bcrypt |
| Users/DB | none | Postgres (role `gateway`, db `local_ai_gateway`), alembic |
| Deps | `requirements.txt` | own `.venv` + alembic |
| Port | 8001 | 8000 |
| Extra endpoints | `/models`, `/embeddings` | `/auth/*`, `/users/*` |

## Shared conventions (carried over)
- Never use the `ollama` SDK — httpx REST only.
- Keep the agent loop hand-rolled / glass-box readable.
- MCP client uses streamable HTTP.

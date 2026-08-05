# CLAUDE.md — Local LLM Gateway

## System (Local LLM product)
```
Frontend  →  THIS GATEWAY (:8000)  →  Ollama LLM (:11434)
                    |                    (inference only; NOT an MCP client)
                    ├─ Postgres (users, later chat history)
                    └─ remote MCP server (business tools)
```
This gateway is the **single authenticated front door**. Auth + ALL tool
execution live here (Pattern A). Ollama only runs the model and says which tool
to call; the gateway (the MCP client) actually calls it. The frontend talks ONLY
to this gateway, with a JWT bearer token.

Sibling project `../local-ai-model` is the original where this code was first
built/proven; code is being ported here. Don't edit it as part of gateway work.

## Environment / commands
- **Use THIS project's `.venv`** (`.venv/bin/python`, `.venv/bin/pip`, `.venv/bin/uvicorn`,
  `.venv/bin/alembic`, `.venv/bin/pytest`). Never install into a sibling's venv. Python 3.10.
- Run: `.venv/bin/uvicorn app.main:app --reload --port 8000`  (Swagger at `/docs`).
  **Port convention: this gateway = 8000** (front door the frontend targets);
  sibling `local-ai-model` = 8001. Never run both on the same port.
- Migrations: `.venv/bin/alembic revision --autogenerate -m "msg"` then `.venv/bin/alembic upgrade head`
- Tests: `.venv/bin/pytest`
- Config via `.env` (see `.env.example`). `DATABASE_URL` and `JWT_SECRET` are required.

## Postgres (local dev)
Local PG17 via TCP. Superuser: `postgres`/`postgres` on 127.0.0.1:5432 (peer auth
fails — no `manoj` role). App uses role `gateway` / db `local_ai_gateway` (creds
in `.env` only, never in code). Create with:
`psql -h 127.0.0.1 -U postgres -c "CREATE ROLE gateway LOGIN PASSWORD '...'; CREATE DATABASE local_ai_gateway OWNER gateway;"`

## Layout
`app/{config,main}`, `db/`, `auth/`, `users/`, `ollama/` (client), `chat/`,
`mcp/` (client), `tools/` (`registry.py` = engine; `local/` package = one module
per in-process tool, each exporting a `SPEC`, aggregated in `local/__init__.py`'s
`LOCAL_TOOLS`), `agent/` (hand-rolled loop; `loop.stream_turn` = async event
generator, `loop.run_turn` = collect for non-stream, `schemas` = trace types —
**no router**, it's driven by `/v1/chat`), `files/` (per-user generated AND
uploaded files: `models`=`generated_files` table (with `source`
`generated|uploaded`), `repository`=data access, `store`=`file_sink`+`file_source`
contextvars + async `save`/`resolve_file` + in-memory fallback, `sink`=
`PostgresFileSink` (owns its own commit), `source`=`PostgresFileSource`
(owner-scoped id→path resolver) + `turn_files` (installs sink+source together),
`readers`=xlsx/csv→`Table` normalizer (pure, no formula eval), `router`=upload
`POST /v1/files` + `GET /v1/files` list + owner-scoped `/v1/files/{id}`; feeds
create_excel/html/chart/pdf/csv/docx and inspect_excel/read_excel),
`history/` (chat-history: `models` = `chat_sessions`
+ `chat_messages`, `repository` = data access, `service.open_turn` = shared
turn-open used by chat, `router` = `/v1/sessions`). `alembic/` for migrations.
- **Adding a local tool:** new `app/tools/local/<name>.py` with `_fn` + `SPEC`,
  then add `<name>.SPEC` to `LOCAL_TOOLS`. The engine (`registry.py`) never changes.

## Endpoints
Public: `/health`, `POST /auth/register`, `POST /auth/login`.
Authed (JWT): `GET /users/me`, `GET /users` (admin), `POST /v1/chat`,
`GET /v1/tools`, `GET /v1/mcp/status`, `POST /v1/files` (upload .xlsx/.csv →
`generated_files` row `source=uploaded`; 400 bad ext/corrupt/zip-bomb, 413 over
size cap), `GET /v1/files` (caller's files, newest first; `?source=` filters),
`GET /v1/files/{id}` (owner-scoped download; 404 if not yours),
`DELETE /v1/files/{id}` (owner-scoped; 204, removes row + on-disk file),
`GET /v1/sessions`, `GET /v1/sessions/{id}`, `DELETE /v1/sessions/{id}`.
`GET /v1/mcp/status` is the UI's MCP-connection badge — **always 200**, health
is in the body (`configured/reachable/tools/error`), never a 502.
`POST /v1/chat` is the **single, unified** turn endpoint — **stateful**
(`{session_id?, message, model?, stream?, options?, file_ids?}`, server rebuilds
context + persists both rows) and **tool-capable** (runs the agent loop every
turn; the
model calls local/MCP tools when useful). `stream:false` → JSON
`{session_id, message, model, stop_reason, trace?}`; `stream:true` → NDJSON typed
events (`token`/`tool_call`/`tool_result`/`done`) + the new id in the
`X-Session-Id` header. **There is no `/v1/agent`** — it was folded in.

## Conventions / gotchas
- Auth: JWT (PyJWT HS256) + bcrypt. Provider-agnostic User (email, auth_provider,
  nullable password_hash, role admin|member). **First registered user → admin.**
- Agent loop is **hand-rolled, no framework** — keep it readable/commented.
- **Never** use the `ollama` SDK — call Ollama's REST API with httpx.
- **fetch_url SSRF rule:** the `fetch_url` tool (outbound HTTP GET) must keep its
  guards — http/https only, resolve the host and refuse if ANY IP is
  non-public (blocks localhost/private/link-local incl. 169.254.169.254 metadata),
  re-check every redirect hop, GET-only, timeout + byte cap. Never relax these to
  "make it work"; internal services (Ollama/PG/MCP on localhost) are reachable
  otherwise. Config: `FETCH_URL_ENABLED`, `FETCH_URL_ALLOWLIST`.
- MCP: gateway is the MCP client (streamable HTTP). Set `MCP_SERVER_URL` to enable;
  blank = agent runs with local tools only. `mcp` SDK v2: fn is `streamable_http_client`,
  tool field is `input_schema`.
- File downloads are behind JWT — the frontend must fetch with the Bearer header
  and make a blob URL (an `<a href>` can't send the header). Files are **per-user**
  now: every generated file gets a `generated_files` row owned by the caller;
  `GET /v1/files/{id}` 404s unless you own it, `GET /v1/files` lists your files.
- **File-sink/source contextvar gotcha:** tools call `await file_store.save(...)`
  (write) / `await resolve_file(id)` (read) but never see the user; the chat
  router installs BOTH via `turn_files(user_id, session_id)` (= `file_sink(
  PostgresFileSink)` + `file_source(PostgresFileSource)`) for the turn. For
  streaming they MUST be set *inside* the async generator Starlette iterates
  (done in `chat/router.py`), else they're invisible while the loop runs (writes
  fall back to the unowned in-memory store; reads can't find owned files). A
  new file-producing tool needs nothing here — just `await file_store.save(...)`;
  a new file-reading tool just `await resolve_file(id)` (None ⇒ not owned/unknown).
- **Excel/CSV upload + read:** `POST /v1/files` ingests .xlsx/.csv (uuid on-disk
  name under the user folder, `source=uploaded`; guards: size cap→413, ext
  allowlist + xlsx zip-bomb + parse-check→400). `app/files/readers.py` normalizes
  both formats to a capped `Table` (**opens xlsx `data_only=True` — formulas are
  NEVER evaluated**; row/char caps bound context). Tools `inspect_excel` (every
  sheet's structure) + `read_excel` (one sheet, paged/projected, truncation tells
  the model how to page; multi-sheet-with-no-`sheet` reads the first AND names
  the others). Attach with `file_ids` on `/v1/chat`: `open_turn` verifies
  ownership (404 on foreign id), persists `{id,filename,summary}` on the user
  message (`chat_messages.attachments` JSONB), and `build_context_messages`
  re-emits the attachment note on later turns so ids survive without resending.
- **Starlette 1.x gotcha:** `include_router` mounts as a lazy `_IncludedRouter`,
  so `app.routes` won't list child routes as `APIRoute`. Verify routes via
  TestClient or `/openapi.json`, not `isinstance` checks.
- Test login: `admin@example.com` / `supersecret123` (persisted in Postgres).

## Not done yet
Frontend (unblocked now). History follow-ups (title rename, context-window
truncation). File follow-ups (pagination, orphan cleanup of root-level
pre-scoping files). Client-side stream cancellation/abort.
Deployment hardening (firewall internal deps to the gateway IP) is deferred by
the user for now.

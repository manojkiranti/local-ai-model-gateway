# Local LLM Chat Product — Status & Architecture

_Last updated: 2026-08-02_

## What we're building
A self-hosted local-LLM chat product with a 3-tier architecture:

```
Frontend  →  Gateway API (:8000)  →  Ollama LLM (:11434) + Postgres + remote MCP server
```

- **Ollama** = inference only (runs the model; executes nothing; NOT an MCP client).
- **Gateway** = the single authenticated front door. Auth + ALL tool execution live here.
  It is the MCP client and owns the agent loop.
- **Frontend** = talks ONLY to the gateway (JWT bearer). Never calls the LLM/DB/tools directly.

Design decision: **Pattern A** — everything is absorbed into ONE FastAPI gateway
app (no separate inner service).

## Projects & their relationship
1. **local-ai-model-gateway** (MAIN — the product). Port **8000**. Own `.venv`.
2. **local-ai-model** (ORIGINAL / reference). Port **8001**. Where code was first
   built & proven, then ported into the gateway. Now reference-only.
3. **frontend** (separate repo). In progress via Claude CLI; contract defined, UI not built yet.

## Gateway — what's BUILT (28 tests passing, proven live)
Stack: FastAPI (async), SQLAlchemy 2.0 async + asyncpg, Alembic, Postgres 17,
PyJWT (HS256), bcrypt, httpx (no ollama SDK), mcp SDK v2, openpyxl. Python 3.10.

- **Auth**: login → JWT. User (email, auth_provider `local`|`ad`, nullable
  password_hash, role admin|member, is_active, timestamps). `POST /auth/login`
  dispatches on auth_provider and consults exactly ONE credential store — never a
  fallback chain; `ck_users_credential` makes "both" unrepresentable. Active
  Directory via `app/auth/directory.py` (`AD_AUTH_ENABLED`, off by default);
  unknown email + AD success auto-provisions a `member` with no departments. A
  directory outage is **503, not 401**. Login is throttled
  (`LOGIN_MAX_ATTEMPTS`, per process) because the endpoint can otherwise trip AD
  lockout domain-wide. `POST /auth/register` is **admin-only** except on an empty
  users table, where it mints the first admin (or ADMIN_EMAILS).
- **Endpoints**:
  - Public: `GET /health`, `POST /auth/login`
  - Admin: `POST /auth/register` (unauthenticated only on an empty users table)
  - Authed: `GET /users/me`, `GET /users` (admin, paginated, `?q=` email search)
  - Admin: `PATCH /users/{id}` `{is_active}` — immediate access cut-off
    (guards: no self-deactivation, never the last active admin; `role` not patchable)
  - Authed: `POST /v1/chat` — the **single unified turn endpoint**: persisted
    (`{session_id?, message, model?, stream?, options?}`, server owns context) AND
    tool-capable (runs the agent loop every turn; model calls tools when useful).
    `stream:false` → JSON `{session_id, message, model, stop_reason, trace?}`;
    `stream:true` → NDJSON typed events (token/tool_call/tool_result/done) + the
    new id in `X-Session-Id`. **`/v1/agent` was removed** (folded in).
  - Authed: `GET /v1/tools` — merged/filtered tool list
  - Authed: `GET /v1/mcp/status` — MCP connection status for the UI badge.
    **Always 200**; body carries `{configured, reachable, server_url, tool_mode,
    tools[], error}`. Use this (not `/v1/tools`'s 502) to render 🟢/🔴/⚪.
  - Authed: `GET /v1/files` — the caller's generated files, newest first
    (`{files:[{id, filename, media_type, size, created_at}]}`) for a "my files" UI
  - Authed: `GET /v1/files/{id}` — **owner-scoped** download (404 unless yours)
  - Authed: `DELETE /v1/files/{id}` — **owner-scoped** delete (204; drops the row
    + unlinks the on-disk file; 404 if not yours; idempotent)
  - Authed: `GET /v1/sessions`, `GET /v1/sessions/{id}`, `DELETE /v1/sessions/{id}`
    — list/read/delete chat threads (all scoped to the caller; not-owned → 404)
- **Chat history**: `chat_sessions` + `chat_messages` (Postgres). A turn = one
  user row + one assistant row (clean thread); agent-loop internals live only in
  the assistant row's `trace` JSONB. UUID-hex PKs, per-session `seq` ordering.
  User message persists immediately; assistant row on model success. Streaming
  accumulates deltas server-side and returns the new id via `X-Session-Id`.
  **Per-user scoping lands here** (sessions/messages tied to `user_id`).
- **Agent loop**: hand-rolled (no framework), an **async event generator**
  (`stream_turn` yields token/tool_call/tool_result/done; `run_turn` collects it
  for non-stream — one engine, both paths). Streams Ollama internally. Glass-box
  trace, robust to unknown tool / bad args / repeat / tool errors / max-iterations.
- **Tools**: local (`get_current_time`, `create_excel` via openpyxl,
  `create_html`, `create_chart`, `create_pdf` via fpdf2, `create_docx` via
  python-docx, `create_csv`, `calculator`, `date_math`, `fetch_url`,
  `inspect_excel`, `read_excel`) always on;
  MCP tools filtered by read_only|allowlist|all. `inspect_excel`/`read_excel`
  read an UPLOADED spreadsheet (`POST /v1/files`, .xlsx/.csv, `source=uploaded`)
  owner-scoped by `file_id`; `readers.py` normalizes both formats to a capped
  `Table` and **never evaluates formulas** (`data_only=True`). Attach via
  `file_ids` on `/v1/chat` (ownership-verified, note persisted on the user
  message so ids survive later turns). `fetch_url` is an
  **SSRF-guarded** outbound HTTP GET (scheme allowlist; every resolved IP must be
  public, so localhost/private/link-local incl. cloud metadata are blocked;
  redirects re-checked per hop; GET-only; 10s timeout, ~2 MB cap, truncated; HTML
  reduced to readable text; `FETCH_URL_ENABLED`/`FETCH_URL_ALLOWLIST` config).
  `create_docx` shares `create_pdf`'s
  content model (title + sections{heading?/body?/table?}) but emits a real .docx
  and keeps full Unicode (no latin-1 clamping). `calculator` is a safe math evaluator (stdlib `ast`
  allowlist, **never `eval`**; arithmetic + common math fns/constants;
  DoS-guarded). `date_math` does calendar arithmetic (add/subtract with
  end-of-month clamping, and diff) on supplied dates. Both return a text result,
  not a file. `create_csv` (stdlib `csv`) writes `text/csv` and returns a
  download link like the other file tools. `create_excel`, `create_html`,
  `create_chart`, and `create_pdf` return a `/v1/files/{id}` download link (same
  string shape). `create_pdf` takes a document model (`title?` + `sections[]` of
  `{heading?, body?, table?}`) and renders a real PDF with fpdf2 (Helvetica core
  font, no embedded fonts/system libs; text sanitized to latin-1, so emoji/
  non-Latin glyphs become `?`). They
  run through `POST /v1/chat` (which is now tool-capable). `create_chart` takes
  structured data (chart_type bar|hbar|line|area|pie|donut + labels + series) and
  the gateway renders a static, script-free SVG (`image/svg+xml`) — no JS, no new
  deps; palette from the dataviz skill (validated). `create_html`
  writes a model-generated HTML document (no server-side sanitizing); safe
  rendering is the frontend's job — preview only inside a sandboxed `<iframe
  srcdoc>` (no allow-scripts). `/v1/files/{id}` sends HTML as an attachment with
  `X-Content-Type-Options: nosniff`, so it's never rendered inline in our origin.
- **MCP client** lives in the gateway (streamable HTTP). **Now connected** to
  `../../node/local-llm-mcp` (FastMCP, `httpStream` at `/mcp`, Bearer auth) —
  exposes 3 read-only demo tools (`get_server_time`, `get_echo`, `list_examples`),
  all merged into `/v1/tools` and callable from the agent. Blank `MCP_SERVER_URL`
  still falls back to local-tools-only. A **pre-flight reachability probe** turns
  "server down" into a clean `MCPUnavailableError` (→ 502 / caught mid-run)
  instead of a leaked `CancelledError` from the transport's cancel scope.
- **Files**: **per-user**, Postgres-backed (`generated_files` table). Every file
  a tool produces gets a row owned by the caller (+ the originating session),
  written under `generated_files/{user_id}/{uuid}.ext`. Tools call `await
  file_store.save(...)`; the caller is threaded in via a `file_sink()` contextvar
  the chat router installs per turn (`PostgresFileSink`, which commits the row in
  its own transaction so the file is durable even if the turn later fails). No
  sink installed (offline tool tests) → in-memory fallback, unchanged. Downloads
  are owner-scoped (404 for non-owners) and listable via `GET /v1/files`.
- CORS enabled (dev). Bearer tokens (no cookies).

## Infra / environment
- Ollama local `:11434` (has qwen2.5:latest etc.). Prod target: qwen2.5:72b on
  2×A40, kept warm via OLLAMA_KEEP_ALIVE=-1 (Ollama-side, not the app's concern).
- Postgres 17 local: role `gateway` / db `local_ai_gateway` (creds in `.env` only).
- Ports: gateway **8000**, local-ai-model **8001**. Each project has its own `.venv`.

## Key conventions
- Frontend calls ONLY the gateway; JWT bearer on every request; 401 → re-login.
- All turns (incl. tool/file requests) go to the one endpoint `/v1/chat`.
- Read the file download link from the turn `trace` (not final_answer); fetch
  `/v1/files/{id}` WITH the bearer header → blob URL (an `<a href>` can't send it).
- **Branch on the file's `media_type`** (from the response `Content-Type`, or
  tracked when you parse the tool result):
  - `text/html` → **sandboxed iframe preview + download**. Fetch WITH the bearer
    header, get the text, then preview ONLY via `<iframe sandbox srcdoc={htmlText}>`
    (no `allow-scripts` — disables scripts, isolates model HTML from your app).
    **NEVER** inject into your DOM / `dangerouslySetInnerHTML`. Offer a Download
    button alongside.
  - `image/svg+xml` (chart) → **render via `<img src={blobURL}>` + download**.
    An `<img>`-loaded SVG never executes scripts, so it's safe inline. Do NOT
    inline the SVG markup into the DOM.
  - `application/pdf` → **download** (blob URL); optionally preview via the
    browser-native PDF viewer (`<iframe src={blobURL}>` / `<embed>`). Our PDFs
    carry no JavaScript.
  - xlsx / docx / csv → **download only** (blob URL). (docx = Word, csv = text.)
- Never use the ollama SDK (httpx REST). Agent loop stays hand-rolled/readable.

## NOT done yet (open items for planning)
- **History follow-ups** — title rename/auto-title, context-window truncation
  (full history is sent to the model now), message edit/delete, pagination of
  `GET /v1/sessions/{id}` messages, session-scoped file ownership.
- **Real MCP server** — demo server (`../../node/local-llm-mcp`) connected &
  validated with 3 read-only tools; no *business* tools yet, and the write-tool
  filter (read_only/allowlist) is still unexercised against real write tools.
- **Frontend** — auth UI + chat UI not built yet (contract handed off).
- **Per-user scoping** — chats AND files now tied to `user_id` (owner-scoped
  list/download/delete). File follow-ups: pagination, orphan cleanup of
  pre-scoping root-level files.
- **Deployment** — `Dockerfile` + `.dockerignore` exist (image builds & boots,
  non-root, `/health` check); NOT run for real yet. See `DOCKER.md` for the
  localhost→`host.docker.internal` env changes and the deferred `docker-compose`.
  Firewalling internal deps to the gateway IP is deferred; secrets/migrations-in-prod TBD.
- **Active Directory** — implemented (`app/auth/directory.py`), but the shim at
  `AD_AUTH_BASE_URL` has NOT been reached from this environment: the host is
  unroutable here, so UPN-vs-sAMAccountName and the exact response envelope are
  unverified against the real service. OIDC/SAML remain unimplemented.
- **Rate limiting / observability** — login is throttled per identifier
  (in-process only, so N workers means N x the limit); nothing else is. (Integration tests now exist for
  chat history + MCP; they skip when their backing service is unreachable.)
- **Prompt-injection surface (fetch_url + uploaded spreadsheets + write tools)** —
  `fetch_url` pulls arbitrary external text into the loop, and `inspect_excel`/
  `read_excel` now feed UPLOADED cell contents (untrusted user data) into the
  loop too — either could carry instructions. Fine while all MCP tools are
  read-only; **before wiring any write-capable MCP tool**, revisit so
  fetched/uploaded/tool content can't chain into a state-changing action (e.g.
  confirmation gates, keep external text as data, don't let it authorize writes).
  SSRF is handled; this is the content-trust angle.
- **Uploaded-file data sensitivity** — uploaded spreadsheets will realistically
  carry client PII/financials. Stays owner-scoped (`generated_files.user_id`,
  404-for-non-owner) and out of logs; row/cell caps bound how much enters the
  model context. No formula/macro execution (`data_only=True`, `.xlsm` rejected).

## Candidate next steps (to plan)
1. Build the frontend (auth + chat threads + streaming + file download) — now
   unblocked: sidebar from `GET /v1/sessions`, thread from `GET /v1/sessions/{id}`,
   send via `POST /v1/chat` with `{session_id?, message, stream?}`, render the
   glass-box event stream (token/tool_call/tool_result/done).
2. Connect a real (business-tool) MCP server and validate the write-tool filter.
3. Deployment: Docker, network isolation, prod secrets, migration workflow.

## Frontend contract — unified chat (updated 2026-08-02)
- **One endpoint: `POST /v1/chat`.** A conversation is a `session_id`: omit it to
  start a new one; the server mints it. Track it and send it on every next turn.
  Send only the NEW `message` — the server rebuilds context from history.
- Body: `{session_id?, message, model?, stream?, options?}`. Tools are always
  available; the model uses them when useful (no mode/endpoint choice).
- **Non-stream** (`stream:false`) → `{session_id, message:{role,content}, model,
  stop_reason, trace?}`. `trace` is present only when tools were used.
- **Stream** (`stream:true`) → `Content-Type: application/x-ndjson`, one JSON
  event per line; the new id is in the **`X-Session-Id`** header. Event types:
  `{"type":"token","content":…}` (append to the answer bubble),
  `{"type":"tool_call","name":…,"arguments":…,"iteration":…}`,
  `{"type":"tool_result","name":…,"status":…,"result":…,"iteration":…}`,
  and a terminal `{"type":"done","session_id":…,"stop_reason":…,"trace":…}`.
  Render tool_call/tool_result as a live "working…" timeline.
- Render threads from `GET /v1/sessions` (sidebar, newest first, `message_count`)
  and `GET /v1/sessions/{id}` (ordered `messages`). Assistant rows may carry a
  `trace` array (tool turns) — show it as an expandable "how it worked" panel.
  Note: a reloaded thread shows the clean final answer (+trace), not the live
  token/narration stream.
- 404 on any session route = not yours / doesn't exist; treat identically.

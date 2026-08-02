# Chat-History Persistence — Design

_2026-08-02 · Local LLM Gateway_

## Goal & rationale
Turn the stateless `/v1/chat` proxy into a real multi-turn chat product by
persisting conversations in Postgres, and land per-user scoping while we're here.
This is the foundational slice: the schema is the contract the frontend renders
against, so it must exist before the chat UI is built (otherwise the UI is built
twice — once stateless, once with sessions).

Design keystone: the per-iteration `trace` already returned by `/v1/agent` is
exactly the JSONB payload stored on an assistant message. Two histories —
**visible conversation** (message rows) vs **execution trace** (JSONB on the
assistant row) — map directly onto what already exists.

## Decisions (locked)
- **Interaction model:** server owns truth. Client sends `session_id` + a single
  new `message`; server rebuilds context from history, calls the model, persists.
  No `session_id` → server mints a new session.
- **Scope:** unified — BOTH `/v1/chat` and `/v1/agent` persist into one schema.
- **PKs:** UUID-hex for `chat_sessions` / `chat_messages` (consistent with the
  file store, unguessable, stable frontend render keys). Users stay `int`.
- **Clean-turn rule:** one turn = one user row + one assistant row. Agent loop
  internals (assistant-with-tool-calls, `role:"tool"`, repeats) are NOT rows —
  they live in the assistant row's `trace` JSONB. The visible thread is always
  `user, assistant, user, assistant, …`.
- **User-message lifecycle:** persist the user row **immediately** on request
  (both endpoints). Assistant row commits on model success. A failed model call
  leaves a reply-less user turn (frontend can retry).

## Data model
```
chat_sessions
  id           varchar(32)  PK      -- uuid4().hex
  user_id      int          FK users.id ON DELETE CASCADE, indexed
  title        varchar(200) NULL    -- truncated first user message
  created_at   timestamptz  default now()
  updated_at   timestamptz  default now(), onupdate now()   -- sort threads by recency

chat_messages
  id           varchar(32)  PK      -- uuid4().hex
  session_id   varchar(32)  FK chat_sessions.id ON DELETE CASCADE, indexed
  seq          int          NOT NULL -- per-session monotonic (1,2,3…)
  role         varchar(16)  NOT NULL -- user | assistant
  content      text         NOT NULL
  trace        JSONB        NULL     -- agent turns: TraceEntry[]; chat turns: NULL
  model        varchar(128) NULL     -- model that produced an assistant row
  created_at   timestamptz  default now()
  UNIQUE(session_id, seq)
```
- `seq` gives deterministic ordering (UUID PKs don't sort chronologically).
- `trace` reuses the existing `TraceEntry` shape verbatim.
- Full session history is sent to the model for now; token/`num_ctx` truncation
  is a flagged later refinement (YAGNI now).

## API surface
New package `app/history/` (`models.py`, `repository.py`, `schemas.py`,
`router.py`). Chat/agent routers call the repository so they stay thin.

**Modified — turn endpoints (contract changed; frontend not built yet):**
- `POST /v1/chat` — `{session_id?, message, model?, stream?, options?}`
  → resolve/create session, persist user row, rebuild context, call Ollama,
  persist assistant row (`trace=null`). Non-stream returns
  `{session_id, message:{role,content}, model}`. Streaming returns the new
  session id in an **`X-Session-Id`** response header, proxies Ollama NDJSON
  while accumulating deltas, and persists the assistant row when the stream ends
  (incl. partial-on-disconnect).
- `POST /v1/agent` — `{session_id?, message, model?}`
  → same session lifecycle; run the existing loop with clean history as base;
  persist one assistant row (`content=final_answer`, `trace=result.trace`).
  Response = existing `AgentResponse` + `session_id`.

Both take a single new `message` (agent's old `prompt`/`messages` dropped).
Authed calls always persist.

**New — read/manage endpoints (all scoped to `user_id`):**
- `GET /v1/sessions` → `[{id, title, created_at, updated_at, message_count}]`,
  newest-updated first.
- `GET /v1/sessions/{id}` → `{id, title, created_at, updated_at,
  messages:[{id, seq, role, content, trace, model, created_at}]}`.
- `DELETE /v1/sessions/{id}` → 204, cascade-deletes messages.
- Rename (`PATCH` title) deferred (YAGNI).

## Persistence flow
**Context rebuild (both):** load session messages ordered by `seq` →
`[{role, content}]`. Agent turns contribute only `final_answer` as the assistant
message (never replayed tool calls).

**`/v1/chat` non-stream:** resolve/create session → persist user row → Ollama
`/api/chat` → persist assistant row + bump `updated_at` → return.

**`/v1/chat` streaming:** resolve/create session → persist user row → open Ollama
stream, set `X-Session-Id` → proxy NDJSON while accumulating `content` → in
`finally`, persist assistant row if any content accumulated.

**`/v1/agent`:** resolve/create session → persist user row → build base_messages
from clean history + new message → `run_agent(...)` → persist assistant row
(`final_answer` + `trace`) → return `AgentResponse` + `session_id`.

**`seq` allocation:** `SELECT … FOR UPDATE` the session row, then `max(seq)+1`,
inside the turn transaction; `UNIQUE(session_id, seq)` is the safety net.

## Ownership & errors
- Unknown/not-owned `session_id` → **404** (never leak existence).
- Ollama down → **502** (existing handler); user row already saved, no assistant.
- Agent MCP unavailable → **502** (existing handler).
- Empty `message` → **422**.

## Testing
- **No-DB unit tests** (fit the offline suite): clean-turn extraction,
  history→context mapping, title truncation, schema mapping. Ollama mocked via
  the existing `FakeOllama`.
- **Postgres integration tests** (JSONB is PG-specific — no SQLite): a fixture
  creates the `chat_*` tables on a **test database**, overrides `get_session` +
  `get_current_user`, and **skips cleanly if the DB is unreachable** (same
  pattern as the MCP integration test). Covers: new-session create, ownership
  404, multi-turn context rebuild, agent-turn `trace` persisted, streaming
  persists accumulated content + `X-Session-Id`, cascade delete.

## Migration & files
- New autogenerated Alembic migration: `chat_sessions`, `chat_messages`
  (FKs, `UNIQUE(session_id, seq)`, index on `session_id`). `alembic/env.py`
  imports `history.models` so autogen sees them.
- New: `app/history/{__init__,models,repository,schemas,router}.py`; router
  registered in `main.py`.
- Modified: `chat/router.py`, `chat/schemas.py`, `agent/router.py`,
  `agent/schemas.py`.

## Out of scope (later slices)
Title rename/auto-LLM-title, context-window truncation, message edit/delete,
search, pagination of `GET /v1/sessions/{id}` messages.
```

# AGENTS.md — Local LLM Gateway

Orientation file for AI coding agents (Codex, Claude Code, etc.). Read this
first. `CLAUDE.md` is the detailed engineering record for this repository and
is required reading before changing agent/tool calling, files, chat history,
RAG, ingestion, or deployment behavior.

## System

```text
Frontend -> THIS GATEWAY (:8000) -> Ollama-compatible model server
                  |               (inference and embeddings only)
                  +-> Postgres (users, chat, files, RAG, ingest queue)
                  +-> remote MCP server (business tools)

Separate ingest worker -> Postgres + model embeddings + Docling parsing
```

This FastAPI service is the product's single authenticated front door. The
frontend talks only to this gateway. Ollama is not an MCP client and never
executes tools; local and MCP tool execution happens in the gateway.

The API and ingest worker do not call each other. Department-document uploads
write a queued Postgres job and return HTTP 202. The worker claims jobs with
`FOR UPDATE SKIP LOCKED`, parses and embeds outside a transaction, then replaces
chunks atomically.

Do not edit the sibling `../local-ai-model` project as part of gateway work.

## Stack

- Python 3.10 and FastAPI/Starlette
- Pydantic v2 plus `pydantic-settings`
- SQLAlchemy 2 async plus `asyncpg`
- PostgreSQL plus pgvector, with Alembic migrations
- JWT authentication with PyJWT and bcrypt
- `httpx` for the model server and external HTTP clients
- MCP Python SDK v2 using Streamable HTTP
- Pytest
- Separate worker dependency set for Docling and its heavy ML dependencies

## Commands

Always use this checkout's `.venv`; never install into or run from a sibling
project's environment.

```bash
# Install API dependencies
.venv/bin/python -m pip install -r requirements.txt

# Install worker dependencies (includes the API set and Docling)
.venv/bin/python -m pip install -r requirements-worker.txt

# Run the API; Swagger is at http://localhost:8000/docs
.venv/bin/uvicorn app.main:app --reload --port 8000

# Run the separate RAG ingest worker
.venv/bin/python -m app.rag.worker

# Tests
.venv/bin/pytest
.venv/bin/pytest tests/test_name.py -q

# Migrations
.venv/bin/alembic revision --autogenerate -m "describe change"
.venv/bin/alembic upgrade head
```

Port convention is fixed: this gateway uses `8000`; the sibling
`local-ai-model` uses `8001`.

Configuration comes from `.env` through `app/config.py`. `DATABASE_URL` and
`JWT_SECRET` are required. Add every new setting to `Settings` and the relevant
environment template. Never hardcode or log credentials/tokens, and never
commit `.env`.

Do not apply, revert, or otherwise mutate database migrations automatically
unless the user explicitly asks. Schema changes must include an Alembic
migration; do not rely on runtime schema creation.

## Folder map

```text
app/
├── main.py             FastAPI assembly, lifespan, CORS, routers, health
├── config.py           canonical typed environment surface
├── localtime.py        Nepal-time source used by prompts and date tools
├── db/                 async engine/session/base
├── auth/               registration, login, JWT dependencies/security
├── users/              user models, schemas, routes
├── ollama/             sole model-server wire-format adapter
├── chat/               unified stateful /v1/chat endpoint
├── agent/              hand-written streaming tool-call loop and trace types
├── tools/              registry plus in-process tools under tools/local/
├── mcp/                remote Streamable HTTP MCP client and status route
├── files/              owner-scoped uploads, generated files, readers/store
├── history/            persisted sessions/messages and context reconstruction
├── rag/                 department auth, corpus, hybrid retrieval, ingest jobs
└── nrb/                 constrained Nepal Rastra Bank API client

alembic/                schema migrations
tests/                  unit, contract, integration, live, and evaluation tests
docs/                   environment, transport, reasoning, and frontend notes
Dockerfile              lightweight API image
Dockerfile.worker       heavy Docling ingest-worker image
requirements.txt        API and test dependencies
requirements-worker.txt API dependencies plus Docling
```

## Core contracts

1. **One front door.** Auth, authorization, state, tool execution, and file
   ownership live in the gateway. Do not move tool execution into the frontend
   or model server.
2. **One chat route.** `POST /v1/chat` is stateful, persisted, streaming or
   non-streaming, and tool-capable. There is no separate `/v1/agent` route.
3. **One model wire adapter.** OpenAI-compatible HTTP parsing belongs in
   `app/ollama/client.py`. Do not add the `ollama` or `openai` SDK; the code must
   preserve streamed tool-call fragment accumulation for Ollama, vLLM, and
   compatible backends.
4. **Typed configuration.** Read settings through `Settings`/`get_settings()`
   or the `request.app.state.settings` instance. Avoid scattered `os.environ`
   reads in feature code.
5. **JWT boundaries.** Files and sessions are owner-scoped. Department access
   is checked against Postgres on the server; it is not trusted from JWT claims
   or request-provided identifiers.
6. **Department context is server-owned.** The request's department is resolved
   and installed through `rag_context`. It is never exposed as a retrieval-tool
   argument, and existing general sessions cannot be relabelled later.
7. **Async ingestion.** Upload returns 202 after storage plus queueing. Only the
   separate worker parses and embeds. Keep Docling imports lazy and worker-only.
8. **Embedding/schema alignment.** `RAG_EMBED_DIM` must match pgvector's
   `vector(1536)`. Queries and documents use different Qwen embedding modes;
   preserve result reordering by returned embedding index.
9. **File context lives inside streaming generators.** Install file sink/source
   and RAG contextvars inside the async generator Starlette actually iterates,
   or streaming turns lose ownership/context.
10. **HTTP 202 is not ingestion success.** Clients must poll
    `/v1/ingest-jobs/{id}` until `succeeded` or `failed` and surface progress or
    errors.

## Tool conventions and security

- Add an in-process tool as `app/tools/local/<name>.py` with `_fn` and `SPEC`,
  then register its `SPEC` in `LOCAL_TOOLS`. Do not special-case it in the
  registry engine.
- Keep the hand-written agent loop readable. Tool results correlate by
  `tool_call_id`, not tool name. Preserve announced truncation and fragmented
  streamed-argument merging.
- Tool descriptions are routing instructions. Keep cross-references between
  `inspect_excel`, `read_excel`, and `aggregate_excel`; totals must use the
  uncapped aggregation path.
- `read_document` is for attached documents; spreadsheet reads use
  `inspect_excel`/`read_excel`. Attachment notes remain `user` messages because
  that role materially affects tool use.
- MCP is optional. Blank `MCP_SERVER_URL` means local tools only. Apply the
  configured read-only/allowlist/all policy before tools reach the model.
- `GET /v1/mcp/status` is a UI status endpoint and always returns 200; connection
  health is represented in its body.
- Never weaken `fetch_url` SSRF protections: public HTTP(S) only, DNS/IP checks
  on every redirect, GET only, timeout, and response-size limits.
- `get_nrb_forex` uses a configured official host and fixed `/rates` path. Do
  not add a model-controlled URL. Preserve string-valued official rates and
  always display the currency unit.
- Use `app/localtime.py` for "today" in Nepal. Do not derive Nepal's date from
  UTC or ask the model to supply current dates/rates from memory.

## Database and RAG invariants

- Preserve the composite department/document foreign key. It is the database
  enforcement boundary preventing cross-department chunks.
- Departments are retired with `is_active = false`, not deleted.
- Document-content and active-ingest-job uniqueness indexes are deliberately
  partial. Their status CHECK constraints are part of correctness.
- `documents.storage_key` is relative to `RAG_DOCS_DIR`; do not store an
  absolute path there.
- SQLAlchemy declarative reserves `metadata`; the Python attribute is `meta`
  while the database column remains `metadata`.
- HNSW and GIN indexes are model-declared but migration-managed and excluded
  from Alembic comparison. Do not remove that exclusion as apparent cleanup.
- Worker parsing/embedding must not hold a database transaction. Keep heartbeat,
  stale-job recovery, row locks, and rollback behavior for failed re-ingests.
- Uploads write storage before the DB transaction completes, so failure paths
  must compensate by deleting the newly written file.

## Testing and verification

- Every production-code behavior change must include new or updated automated
  tests in the same task. For bug fixes, add a regression test that fails
  without the fix.
- Do not consider an implementation complete until the narrowest relevant test
  suite passes. Run the full suite for broad or cross-cutting changes.
- Documentation-only and comment-only changes do not require tests. If a test
  cannot reasonably be added or executed, explain why in the final response and
  state what verification was performed instead.
- Integration/live tests may skip when Postgres, Ollama, Docling models, or the
  embedding model are unavailable. Report skips honestly; they are not proof of
  live integration.
- Do not claim Docker, database, model, MCP, or external-API success unless that
  path was actually exercised.
- Starlette 1.x mounts included routers lazily. Verify routes through TestClient
  or `/openapi.json`, not by expecting every child route in `app.routes`.
- Preserve unrelated user changes in a dirty worktree. Never rewrite generated
  files or migration history unless the task requires it.

## Deployment

The API and worker have separate images and dependency sets. In Compose,
Postgres and Ollama are external services; both containers reach them through
configured hostnames and share the `rag_documents` volume. Generated user files
use a separate persistent volume. Read `DOCKER.md` before changing container
behavior and `docs/server-and-models.md` before assuming hardware, ports, model
availability, or live deployment state.

## Checklist for a change

1. Read the relevant module, tests, `CLAUDE.md` section, and environment/docs
   contract before editing.
2. Trace the boundary end to end: request schema, auth/context, service or agent
   loop, persistence, response/stream event, and tests.
3. If configuration changes, update `app/config.py` and environment templates.
4. If schema changes, add a migration and verify model/migration agreement.
5. Keep API-only dependencies out of the worker distinction and Docling out of
   the API image/import path.
6. Run focused tests and state exactly which live dependencies were or were not
   exercised.

## Further reading

- `CLAUDE.md` — detailed, hard-won implementation invariants and known seams
- `README.md` — setup, architecture, endpoints, and basic verification flow
- `docs/server-and-models.md` — current environment and model facts
- `docs/llm-transport-and-deployment.md` — context and backend transport details
- `DOCKER.md` — API/worker container setup
- `.env.example`, `.env.docker.example` — supported configuration

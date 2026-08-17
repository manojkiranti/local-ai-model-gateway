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

# NRB corpus pipeline — offline, never model-facing, SCRATCH database only.
# Every one of these needs DATABASE_URL=.../local_ai_gateway_p4 (see the NRB
# section below); several refuse to run without an explicit scope.
.venv/bin/python scripts/nrb_sync.py --dry-run      # catalog reconciliation
.venv/bin/python scripts/nrb_fetch.py --core        # download + verify bytes
.venv/bin/python scripts/nrb_extract.py --extractor-version native-2
.venv/bin/python scripts/nrb_recover.py <blob> --plan-only   # page routing, no DB
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
└── nrb/                 forex tool (model-facing) + the NRB document pipeline
                        (catalog, fetch, extract, classify — NOT model-facing)

alembic/                schema migrations
tests/                  unit, contract, integration, live, and evaluation tests
docs/                   environment, transport, reasoning, and frontend notes
Dockerfile              lightweight API image
Dockerfile.worker       heavy Docling ingest-worker image
docker-compose.yml      migrate -> gateway + worker
docker-compose.p4.yml   NRB overlay: scratch DB + the GPL-3 build flag
requirements.txt        API and test dependencies
requirements-worker.txt API dependencies plus Docling
requirements-nrb.txt    npttf2utf (GPL-3, opt-in build ARG only)
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

## NRB document pipeline

`app/nrb/` is two unrelated things. `client.py` backs the model-facing
`get_nrb_forex` tool. **Everything else is an offline corpus pipeline that no
model can call** — it is driven by `scripts/nrb_*.py` and exists to turn Nepal
Rastra Bank's public site into a searchable corpus. Read
`docs/nrb-integration.md` before touching any of it; that document is the status
record, and re-deriving state from code or chat history has gone wrong before.

- **Use the scratch database `local_ai_gateway_p4`**, never `local_ai_gateway`.
  `alembic current` failing against the dev DB on this branch is by design; do
  not "fix" it with `alembic stamp` or by recreating the database.
- Where it stands: the catalog (18,577 sources / 18,266 files) syncs
  idempotently, files download with magic-byte verification into
  content-addressed storage, every fetched blob is parsed and classified, and
  each PDF page is routable to native text, the guarded converter or OCR
  (`app/nrb/recovery.py`). Eight named blobs have been chunked, embedded and
  retrieved end-to-end (250 chunks, scratch DB, §17/§18.7). **The CORPUS is not
  ingested** — Phase 7 needs a scoped, resumable ingest driver that does not yet
  exist, and the `search_nrb_documents` tool (Phase 8) is not started.
- **Each pipeline stage has its own answer to "is it idempotent", and stage 4 is
  the odd one.** Sync is all-zero on a second run; fetch selects only `pending`
  (excluded by the status column, not a `WHERE`); extract selects blobs with no
  row at this `extractor_version`. But **recovery re-runs on every ingest** —
  `rag.parse_nrb_to_chunks` calls `extraction.extract_file` fresh and never reads
  `nrb_extractions`, so conversion and OCR are recomputed each time and that
  table is *evidence, not an input to ingestion*. Nothing is scheduled anywhere:
  stages 1–3 are manual CLI passes and the only daemon is `app.rag.worker`.
  Details and the three Phase 7 gaps are `docs/nrb-integration.md` §19.
- Extraction identity is `(content_sha256, extractor_version)`. `native-1` and
  `native-2` rows coexist deliberately, so an old measurement stays reproducible.
  A classifier change means a NEW version, never an edit in place.
- Legacy Nepali fonts (Preeti and friends) are a detection-and-recovery problem
  with a counter-intuitive rule: **producing Devanagari is not succeeding.** An
  English table run through a Preeti converter comes out 91% Devanagari and
  scores well on every after-the-fact check, so the guards run on the INPUT.
- `npttf2utf` is GPL-3.0, imported lazily, and reachable only through
  `app/nrb/legacy_font.py`. It is not installed by `Dockerfile`. The distribution
  question is unresolved on purpose; do not vendor its tables and do not import
  it anywhere else.
- Conversion and OCR are wired into ONE path only: `app/nrb/recovery.py`, the
  offline extraction router. Nothing model-facing, nothing persisted, no corpus
  pass. Font provenance (`app/nrb/provenance.py`, pypdf — no subprocess) chooses
  between the converter and OCR *inside* an already-eligible document; it never
  makes a document eligible. The `unit_legacy_ratio >= 0.80` gate is unchanged.
- NRB text reaches department RAG through ONE branch:
  `documents.metadata.origin == "nrb"` in `worker._load_chunks_sync` →
  `app/nrb/rag.parse_nrb_to_chunks`. Everything else parses generically and
  unchanged. Chunks are per PAGE (page identity is the citation) and carry the
  route in `document_chunks.metadata`; no migration was needed and none should be
  added for this. Route is provenance, never a ranking weight.
- Recovery fails CLOSED in both directions. A page routed to OCR is never handed
  to the converter, and a conversion that does not succeed — npttf2utf absent, a
  broken backend, a rejected unit — withholds its glyph-mapped INPUT instead of
  publishing it. Only `PageText.indexable` text may be chunked, embedded or
  cited. Omitting npttf2utf in a deployment must produce an explicit unresolved
  extraction, never silent garbage in the index.
- OCR is the narrow fallback for pages with no embedded font: PP-OCRv5
  Devanagari via docling/RapidOCR on the **onnxruntime** backend (docling reaches
  the rejected PP-OCRv4 through torch). Worker-side only —
  `requirements-worker.txt`, never `requirements.txt`. Its output is retrieval
  text, **not** authoritative for figures, dates or contact details.
- **An NRB worker image is verified by its route split, not by job success.**
  The five ways a deployment loses NRB text (missing lexicon, CWD-relative
  lexicon path, missing `npttf2utf`, RapidOCR's root-owned model dir, docling's
  `torch.compile` without a C++ compiler) all end in withheld input and a job
  that reports **succeeded** — the fail-closed rule doing its job, and the reason
  none of them show up in a health check. Run a known blob and read the routes.
  `INSTALL_LEGACY_FONT=true` is required for conversion and is off by default
  because npttf2utf is GPL-3; `docker-compose.p4.yml` is the only supported way
  to point the stack at the scratch database, because `migrate` will otherwise
  upgrade the real one.
- **Frozen evidence is frozen.** The Phase 6A benchmark and the Phase 6B routing
  holdout are committed manifests with self-verifying fingerprints, and the
  holdout was committed before any network access precisely so it could validate
  a classifier it never influenced. Once a finding from a holdout changes the
  classifier, that holdout becomes development evidence: the change needs a new
  extractor version and a NEW cohort. Never tune against it, never redraw it,
  never rewrite its artifacts.

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
- `docs/nrb-integration.md` — NRB status and roadmap; the record of what each
  phase measured and why. Required before any NRB change.
- `docs/server-and-models.md` — current environment and model facts
- `docs/llm-transport-and-deployment.md` — context and backend transport details
- `DOCKER.md` — API/worker container setup
- `.env.example`, `.env.docker.example` — supported configuration

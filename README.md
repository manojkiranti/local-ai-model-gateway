# Local LLM Gateway

The single front door for a local-LLM chat product. The frontend talks only to
this gateway; the gateway talks to Ollama (inference), Postgres (data), and a
remote MCP server (tools). A **separate ingest worker** process shares the same
database to turn uploaded documents into searchable chunks.

```
Frontend ->  API GATEWAY (:8000)  ->  Ollama (inference + embeddings)
                    |
                    +-> Postgres (data, incl. the ingest job queue)
                    +-> remote MCP server (tools)
                          ^
                          |  shared DB only — no direct link
                          |
             INGEST WORKER (separate process)  ->  Ollama (embeddings)
                          |                          Docling (parse PDF/DOCX)
                          +-> Postgres (claims jobs, writes chunks)
```

Ollama only runs the model and returns output — it is **not** an MCP client and
executes nothing. **All** tool execution lives in this gateway.

**The API and the worker never talk to each other directly.** Upload writes a
`queued` row to `ingest_jobs` and returns `202`; the worker polls that table with
`FOR UPDATE SKIP LOCKED`, parses + embeds outside any transaction, and commits
the chunks in one atomic replacement. Postgres is the entire channel — no Redis,
no RPC. This is why the heavy parsing stack (Docling, torch, CUDA) lives only in
the worker and never enters the API image.

## Status

Implemented: **auth + users + health**, the **agent loop + chat** (`/v1/chat`,
streaming and not), **local + MCP tools**, **per-user files** (upload + tool
output), and **department-scoped RAG** — schema + access control (slice 1),
ingestion via the worker (slice 2), and hybrid retrieval wired into chat
(slice 3). Retrieval eval set and a reranker model are deferred follow-ups.

Also in progress: the **NRB corpus pipeline** (`app/nrb/`, driven by
`scripts/nrb_*.py`). Nepal Rastra Bank's 18,266-file public corpus is
catalogued, downloaded with magic-byte verification, parsed and classified —
including detection of legacy Nepali fonts, which render as Devanagari but store
glyph-mapped ASCII. Each PDF page is then routed on its own provenance: text that
is already fine is kept, a page that embeds a legacy font goes through a guarded
converter, and a scanned page goes to OCR. It is **not chunked, embedded or
searchable yet**, and none
of it is reachable by the model; the only model-facing NRB surface is the
`get_nrb_forex` tool. Status and roadmap live in `docs/nrb-integration.md`, and
the pipeline runs against a scratch database, not the dev one.

## Stack

- FastAPI (async), SQLAlchemy 2.0 async + asyncpg, Alembic migrations
- Postgres, JWT auth (PyJWT), bcrypt password hashing
- pydantic-settings for config

## Setup

```bash
# from the project root, using THIS project's venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env    # then set DATABASE_URL and JWT_SECRET
```

Postgres must be running with a database and role matching `DATABASE_URL`. For
local dev, e.g.:

```bash
psql -h 127.0.0.1 -U postgres -c "CREATE ROLE gateway LOGIN PASSWORD 'gateway_dev_pw';"
psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE local_ai_gateway OWNER gateway;"
```

## Migrations

```bash
# generate a migration after changing models
.venv/bin/alembic revision --autogenerate -m "describe change"
# apply migrations
.venv/bin/alembic upgrade head
```

## Run

> **Port convention — this gateway runs on `8000`.** It's the product's single
> front door, so it owns port **8000** and that's the URL the frontend targets
> (`http://localhost:8000`). The sibling `local-ai-model` app runs on **8001** to
> avoid a clash — never run both on the same port.

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```
  python -m app.rag.worker
Serves on `http://localhost:8000`; Swagger UI at `/docs`.

## Ingestion worker (department RAG)

Uploading a document to a department (`POST /v1/departments/{code}/documents`)
does **not** parse or embed inside the request — it stores the file, writes a
`queued` ingest job, and returns `202`. A **separate worker process** does the
slow work. Run it alongside the API:

```bash
.venv/bin/python -m app.rag.worker
```

**In Docker**, the worker runs as its own `worker` compose service (its own
`Dockerfile.worker`, so Docling + torch never enter the API image), sharing
the `rag_documents` volume with the `gateway` service so both see the same
uploaded files — see `DOCKER.md`. The `python -m app.rag.worker` command above
is the non-Docker way to run it.

Two things make this a separate process rather than a background task in the API:

1. **Dependencies.** Parsing PDFs/DOCX uses Docling, which pulls torch + the CUDA
   stack (~90 packages, several GB). That must never enter the API image, so the
   worker has its own dependency file:

   ```bash
   .venv/bin/python -m pip install -r requirements-worker.txt   # API deps + Docling
   ```

2. **Connection.** The worker and the API do not connect directly. The worker
   polls `ingest_jobs` in Postgres (`FOR UPDATE SKIP LOCKED`, so you can run more
   than one), embeds via Ollama, and writes chunks. If the worker is not running,
   uploads still succeed and simply sit `queued` until it starts. Poll a job's
   progress at `GET /v1/ingest-jobs/{id}`.

**Model prerequisite:** the worker refuses to start unless the embedding model is
present and returns the expected dimension. Pull it first:

```bash
ollama pull qwen3-embedding:4b-q8_0
```

`RAG_DOCS_DIR` (default `rag_documents/`, gitignored) is where uploaded corpus
files are stored — separate from `FILES_DIR`. Set the same value for the API and
the worker; they read the same tree.

## Auth model

Provider-agnostic so SSO/OIDC drops in later without a schema rewrite: users are
identified by `email`, with `auth_provider` ("local" now), a nullable
`password_hash` (SSO users have none), and a `role` (`admin` | `member`).

**Admin bootstrap:** the first user to register becomes `admin` (so there's
always a way in); everyone after is `member`. You can also force specific
emails to admin via `ADMIN_EMAILS`.

## Endpoints

Full, current list is in Swagger at `/docs`. The main groups:

| Method | Path | Auth | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/health` | none | Liveness + Ollama reachability. |
| POST | `/auth/register` · `/auth/login` | none | Create a local user · get a JWT. |
| GET | `/users/me` · `/users` | bearer · +admin | Current user · list users. |
| POST | `/v1/chat` | bearer | The unified chat turn — streaming or not, tool-capable, persisted. Pass `department` to scope it to a department's documents. |
| GET | `/v1/tools` · `/v1/mcp/status` | bearer | Available tools · MCP connection badge. |
| POST/GET | `/v1/files` · `/v1/files/{id}` | bearer | Upload/list/download per-user files (owner-scoped). |
| GET | `/v1/sessions` · `/v1/sessions/{id}` | bearer | Chat history. |
| — | `/v1/departments` (+`/members`, `/documents`) | admin / member | Manage departments, grants, and the document corpus. |
| GET | `/v1/ingest-jobs/{id}` | admin | Ingest progress for an uploaded document. |

## Prove it works (register → login → authenticated /users/me)

```bash
# 0) health (200 if Ollama is up, 503 degraded otherwise)
curl -s http://localhost:8000/health | jq

# 1) register (first user -> admin)
curl -s -X POST http://localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"supersecret123"}' | jq

# 2) login -> capture the JWT
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"supersecret123"}' | jq -r .access_token)

# 3) authenticated call
curl -s http://localhost:8000/users/me -H "Authorization: Bearer $TOKEN" | jq

# 4) admin-only listing
curl -s "http://localhost:8000/users?limit=50&offset=0" -H "Authorization: Bearer $TOKEN" | jq
```

## Tests

```bash
.venv/bin/pytest
```

Integration tests **skip cleanly** when Postgres, Ollama, or the embedding model
is unavailable, so the offline suite stays green. The RAG retrieval/ingestion and
live-embedding tests need Postgres (and, for the end-to-end ones,
`qwen3-embedding:4b-q8_0` pulled).

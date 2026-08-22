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
(slice 3). A **retrieval eval harness** and cross-encoder **abstention** are now
built but **inert**: ranking can refuse to answer when nothing relevant is
retrieved, yet `RAG_RERANK_ENABLED=false` and the relevance threshold is an
unfitted placeholder, so the serving path is unchanged. Enabling it needs a
human-authored frozen question cohort and `ollama pull qwen3-reranker:4b`, then
`scripts/rag_eval_sweep.py` to fit the threshold from data — a threshold guessed
rather than measured would refuse questions the corpus can answer, which is worse
than the over-confidence it replaces.

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
python -m app.nrb.runner
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

Users are identified by `email`, with `auth_provider` (`local` | `ad`), a
nullable `password_hash`, and a `role` (`admin` | `member`).

**Two credential stores, one login endpoint, and never a fallback between them.**
`POST /auth/login` reads the user's `auth_provider` and consults exactly one:

| provider | checked against | `password_hash` |
| -------- | --------------- | --------------- |
| `local`  | bcrypt hash in Postgres | required |
| `ad`     | the Active Directory shim (`app/auth/directory.py`) | always NULL |

That asymmetry is the point. "Try AD, then fall back to local" would let an
offboarded employee keep signing in on a stale hash after their AD account was
disabled; "try local first" would let a locally-set password shadow an AD
identity. `ck_users_credential` enforces the same rule in the database, so one
identity can never hold two ways in.

An email with no user row is the one case that asks AD without knowing the
provider in advance; a `Success` there creates the row (`auth_provider='ad'`,
`role='member'`). The new user can chat immediately but sees **no department
corpus** until an admin grants one.

Set `AD_AUTH_ENABLED=true` + `AD_AUTH_BASE_URL` to switch it on; with it off,
login behaves exactly as it did before AD existed.

### Two kinds of role

`users.role` (`admin` | `member`) gates **global** routes: the user table, the NRB
pipeline, creating and retiring departments. A department **grant** carries its own
level, so curating one department never requires power over the others.

| Capability | viewer | editor | owner | global admin |
| --- | :-: | :-: | :-: | :-: |
| Chat + retrieval in the department | Yes | Yes | Yes | Yes (bypasses the grant) |
| Download `ready` documents | Yes | Yes | Yes | Yes |
| List non-`ready` and archived documents | — | Yes | Yes | Yes |
| Upload / add text / archive documents | — | Yes | Yes | Yes |
| Poll ingest jobs for this department | — | Yes | Yes | any department |
| List members, grant/revoke viewer & editor | — | — | Yes | Yes |
| Grant or revoke **owner** | — | — | — | Yes |
| Create, rename or retire a department | — | — | — | Yes |

An owner runs their department day to day but cannot mint another owner, which
bounds the escalation chain at one level. **A global admin is the backstop for
every department; only a global admin can create a department owner.** That is why
the guard takes "is this caller a global admin" as a separate question from "what
is their level here" — collapse the two and nobody can ever create an owner.

Levels are set through `POST /v1/departments/{code}/members`, which is also the
promote/demote route. Omitting `role` grants `viewer`, so every grant that existed
before levels did means exactly what it always meant.

**Admin bootstrap:** on an empty `users` table the first registration is allowed
unauthenticated and becomes `admin`. After that, `POST /auth/register` is
**admin-only** — it creates local service and break-glass accounts. `ADMIN_EMAILS`
forces specific addresses to admin, including directory users, which is how a
staff admin is designated without any local account at all.

Registration is not public because it was a real hole: anyone could pre-register
a colleague's address as a `local` account and permanently shadow their AD
identity, and on an empty database the first-user rule would make them admin.

**Keep at least one local admin.** The AD shim is a single host; if it is
unreachable, directory sign-in returns 503 and a local account is the only way
back in.

## Endpoints

Full, current list is in Swagger at `/docs`. The main groups:

| Method | Path | Auth | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/health` | none | Liveness + Ollama reachability. |
| POST | `/auth/login` | none | Get a JWT. Local password or Active Directory, decided by the user's record. 429 when throttled, **503 when the directory is unreachable — not a bad password**. |
| POST | `/auth/register` | admin | Create a local account (service / break-glass). Unauthenticated only on an empty database, where it mints the first admin. |
| GET | `/users/me` · `/users` | bearer · +admin | Current user · list/search users (`?q=` email substring). |
| PATCH | `/users/{id}` | admin | `{is_active}` — the offboarding switch, effective on the user's next request. 409 for self-deactivation or the last active admin. |
| POST | `/v1/chat` | bearer | The unified chat turn — streaming or not, tool-capable, persisted. Pass `department` to scope it to a department's documents. Returns `sources`: the documents the answer was grounded in (on the `done` event when streaming). |
| GET | `/v1/tools` · `/v1/mcp/status` | bearer | Available tools · MCP connection badge. |
| POST/GET | `/v1/files` · `/v1/files/{id}` | bearer | Upload/list/download per-user files (owner-scoped). |
| GET | `/v1/sessions` · `/v1/sessions/{id}` | bearer | Chat history; assistant messages replay their `sources`. |
| GET | `/v1/departments` | bearer | Your departments (all of them for an admin). Each row's `role` is your effective level here — the one field a UI needs to decide what to draw. |
| — | `/v1/departments/{code}/members` | owner (dept) / admin | List, grant, revoke. `POST` takes `user_id` XOR `email`, plus optional `role`, and doubles as promote/demote. Only an admin may set `owner`. |
| — | `/v1/departments/{code}/documents` | editor (dept) / admin | Upload, add typed text, archive. A viewer gets 403 naming the level required. |
| GET | `/v1/departments/{code}/documents/{id}/download` | viewer (dept) | The original bytes behind a chat citation. Behind JWT, so fetch with the bearer header and make a blob URL. |
| GET | `/v1/ingest-jobs/{id}` | editor (dept) / admin | Ingest progress for an uploaded document. 404 rather than 403 when you may not see it. |

## Prove it works (register → login → authenticated /users/me)

```bash
# 0) health (200 if Ollama is up, 503 degraded otherwise)
curl -s http://localhost:8000/health | jq

# 1) register. Unauthenticated ONLY while the users table is empty, where it
#    creates the first admin. After that this needs an admin bearer token
#    (add -H "Authorization: Bearer $TOKEN"), and AD users never register at
#    all -- their row is created by their first successful sign-in.
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

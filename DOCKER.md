# Docker — notes & TODO before running

_Status: `Dockerfile` + `.dockerignore` exist and the image **builds + boots**
(verified). NOT yet run for real. Read this before you `docker run` in anger._

## What's done
- `Dockerfile` — two-stage, slim, non-root (`appuser` uid 10001), `HEALTHCHECK`
  on `/health`, `CMD` runs uvicorn on `:8000`. No secrets baked in.
- `.dockerignore` — keeps `.env*`, `.venv`, tests, docs, `generated_files/`,
  `.git` out of the build context.

## ⚠️ MUST change before it works (host vs container `localhost`)
Inside a container, `localhost`/`127.0.0.1` is the CONTAINER, not your host.
Today's `.env` points every dependency at localhost — all will fail in Docker.
Override these (via `--env-file`, a prod `.env`, or compose) to reachable hosts:

| Var | `.env` now (host-only) | In Docker use |
|---|---|---|
| `DATABASE_URL` | `...@127.0.0.1:5432/...` | `...@host.docker.internal:5432/...` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | `http://host.docker.internal:11434` |
| `MCP_SERVER_URL` | `http://localhost:3333/mcp` | `http://host.docker.internal:3333/mcp` |

On **Linux**, `host.docker.internal` needs:
`docker run --add-host=host.docker.internal:host-gateway ...`
(compose does this for both services via `extra_hosts`).

### Host Postgres must accept the docker bridge
Only the gateway is containerized — **Postgres stays external**. A default local
PG only listens on `127.0.0.1`, which containers can't reach. On the host:
- `postgresql.conf`: `listen_addresses = '*'` (or add the `docker0` address)
- `pg_hba.conf`: `host local_ai_gateway gateway 172.16.0.0/12 scram-sha-256`
- reload: `sudo systemctl reload postgresql`

Verify from inside the stack:
`docker compose run --rm gateway python -c "import socket;socket.create_connection(('host.docker.internal',5432),3)"`

## Run recipe (when ready)
```bash
docker build -t local-ai-gateway .

# migrations are NOT auto-run on boot (avoids races) — do this one-off first:
docker run --rm --env-file .env local-ai-gateway alembic upgrade head

docker run -d -p 8000:8000 --env-file .env \
  --add-host=host.docker.internal:host-gateway \
  local-ai-gateway
```
Health check: `curl http://localhost:8000/health` → 200 (503 = DB unreachable).

## compose recipe
`docker-compose.yml` runs `migrate` (one-off `alembic upgrade head`) first,
then `gateway` and `worker` together. Postgres, Ollama and the MCP server are
all external host services — nothing is containerized for them by design.
```bash
cp .env.docker.example .env.docker   # set JWT_SECRET + real DB password
docker compose up --build
```

## Ingestion worker (department RAG)
`docker-compose.yml` also runs a `worker` service — the department-RAG ingest
poller (`python -m app.rag.worker`) — built from its own `Dockerfile.worker`
rather than the API's `Dockerfile`. That's a separate image because Docling
(PDF/DOCX parsing) pulls in torch + the CPU-only PyTorch wheel, several GB of
deps that must never enter the slim API image. `docker compose up --build`
starts `migrate` → then `gateway` and `worker` together.

**Shared volume, not a direct link.** The gateway and worker never call each
other directly. An upload writes the file bytes to disk and a `queued` row to
Postgres on the gateway side; the worker polls that row and then needs the
same bytes to parse. Both services mount the same named volume at the same
path to make that possible:
```yaml
volumes:
  - rag_documents:/app/rag_documents   # gateway writes, worker reads
```
Postgres carries the job; the shared volume carries the file. Without it, the
worker can see the queued job but not the file, and ingestion fails with a
"file not found" error.

**`worker_cache`** is a second named volume, mounted at
`/home/appuser/.cache` in the worker only. Docling downloads its
layout/table-detection models on first parse; without a persistent cache,
every worker restart (or redeploy) re-downloads them from scratch.

**Model prerequisite the stack can't provision for you:** the worker's
startup preflight checks Ollama for the embedding model
(`qwen3-embedding:4b-q8_0`) and exits immediately if it's missing or returns
the wrong dimension. Pull it on the Ollama host before bringing the stack up:
```bash
ollama pull qwen3-embedding:4b-q8_0
```

## NRB corpus deployment (the scratch database)

NRB work runs against the **scratch** database `local_ai_gateway_p4`, never
`local_ai_gateway` — see `CLAUDE.md`. Two things about this stack make that
worth stating out loud rather than assuming:

**1. `migrate` runs `alembic upgrade head` against whatever `DATABASE_URL`
names.** All three services read the same `.env.docker`, so a stack brought up
with the default file migrates and writes the REAL database. There is no
per-service override and there should not be — gateway and worker must agree on
the schema. Point the one variable at the scratch DB:
```
DATABASE_URL=postgresql+asyncpg://gateway:<pw>@host.docker.internal:5432/local_ai_gateway_p4
```
`local_ai_gateway_p4` is at `b1bea6ac36c5` = this branch's head, so that upgrade
is a no-op today. Verify before trusting that: `alembic current` against p4.

**2. The worker needs npttf2utf, and it is OFF by default.**
```bash
INSTALL_LEGACY_FONT=true docker compose build worker
```
The default build omits it because it is **GPL-3.0** and the obligations attach
to distribution (`requirements-nrb.txt`). The omission is safe but silent: every
legacy-font page is recorded `conversion_unavailable` and withheld rather than
indexed — on the Phase 6B sample that is 239 of 250 chunks, and four of eight
documents ingest to nothing at all. Check which build you have:
```bash
docker compose run --rm worker python -c "import npttf2utf; print(npttf2utf.__name__)"
```

Also set, for the GPU box:
```
AGENT_MODEL=qwen3.5:35b-a3b          # .env.docker.example still ships the laptop's qwen2.5
RAG_EMBED_MODEL=qwen3-embedding:4b-q8_0
RAG_EMBED_DIM=1536                    # must match the schema's vector(1536)
```
and set `OLLAMA_CONTEXT_LENGTH: 32768` on the **Ollama service**, not here —
different process, different config home (`docs/server-and-models.md` §3).

Nothing in this stack ingests a corpus on boot. The worker polls `ingest_jobs`
and does nothing until a row appears; `scripts/nrb_rag_ingest.py` is what
enqueues the Phase 6B sample, and it refuses to run unless `DATABASE_URL` names
`local_ai_gateway_p4`.

## Still TODO (deferred)
- **`generated_files/`** — with `docker run` it's a dir inside the container
  (lost on restart); compose already mounts the `gateway_files` volume.
  Mount a named volume if generated files must survive: `-v gw_files:/app/generated_files`.
- **Ollama** — stays a host/external service (GPU); not containerized here.
- **Secrets** — `--env-file .env` is fine for local; use a real secrets manager
  (or Docker/K8s secrets) in prod. Never bake `.env` into the image.

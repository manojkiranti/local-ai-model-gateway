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
`docker-compose.yml` runs **only** `migrate` (one-off `alembic upgrade head`) and
`gateway`. Postgres, Ollama and the MCP server are all external host services —
nothing is containerized for them by design.
```bash
cp .env.docker.example .env.docker   # set JWT_SECRET + real DB password
docker compose up --build
```

## Still TODO (deferred)
- **`generated_files/`** — with `docker run` it's a dir inside the container
  (lost on restart); compose already mounts the `gateway_files` volume.
  Mount a named volume if generated files must survive: `-v gw_files:/app/generated_files`.
- **Ollama** — stays a host/external service (GPU); not containerized here.
- **Secrets** — `--env-file .env` is fine for local; use a real secrets manager
  (or Docker/K8s secrets) in prod. Never bake `.env` into the image.

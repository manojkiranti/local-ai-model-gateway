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
| `DATABASE_URL` | `...@127.0.0.1:5432/...` | `host.docker.internal` or the DB service name |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | `http://host.docker.internal:11434` |
| `MCP_SERVER_URL` | `http://localhost:3333/mcp` | `http://host.docker.internal:3333/mcp` |

On **Linux**, `host.docker.internal` needs:
`docker run --add-host=host.docker.internal:host-gateway ...`

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

## Still TODO (deferred)
- **`docker-compose.yml`** — wire gateway + Postgres + (optionally) the node MCP
  server together so the localhost/networking mess above is solved by service
  names. This is the clean next step.
- **`generated_files/`** — currently a dir inside the container (lost on restart).
  Mount a named volume if generated files must survive: `-v gw_files:/app/generated_files`.
- **Ollama** — stays a host/external service (GPU); not containerized here.
- **Secrets** — `--env-file .env` is fine for local; use a real secrets manager
  (or Docker/K8s secrets) in prod. Never bake `.env` into the image.

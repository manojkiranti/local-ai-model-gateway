# Local LLM Gateway

The single front door for a local-LLM chat product. The frontend talks only to
this gateway; the gateway talks to Ollama (inference), Postgres (data), and a
remote MCP server (tools).

```
Frontend  ->  THIS GATEWAY  ->  Ollama (inference)
                    |
                    +-> Postgres (data)
                    +-> remote MCP server (tools)
```

Ollama only runs the model and returns output — it is **not** an MCP client and
executes nothing. **All** tool execution lives in this gateway.

## Status

This slice implements **auth + users + db + health**. The `ollama/`, `mcp/`,
`tools/`, `agent/`, `chat/`, and `files/` packages are seams for later slices
(ported from the existing `local-ai-model` project).

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

Serves on `http://localhost:8000`; Swagger UI at `/docs`.

## Auth model

Provider-agnostic so SSO/OIDC drops in later without a schema rewrite: users are
identified by `email`, with `auth_provider` ("local" now), a nullable
`password_hash` (SSO users have none), and a `role` (`admin` | `member`).

**Admin bootstrap:** the first user to register becomes `admin` (so there's
always a way in); everyone after is `member`. You can also force specific
emails to admin via `ADMIN_EMAILS`.

## Endpoints

| Method | Path             | Auth        | Description                          |
| ------ | ---------------- | ----------- | ------------------------------------ |
| GET    | `/health`        | none        | Liveness + Ollama reachability.      |
| POST   | `/auth/register` | none        | Create a local user.                 |
| POST   | `/auth/login`    | none        | Get a JWT.                           |
| GET    | `/users/me`      | bearer      | The current user.                    |
| GET    | `/users`         | bearer+admin| List users (paginated).              |

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

Covers the auth primitives (password hashing, JWT issue/verify/expiry). The full
HTTP flow is proven with the curl commands above.

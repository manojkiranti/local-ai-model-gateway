# Server, models & database — one-page reference

**Purpose:** the single place to look up *what runs where* for this gateway —
server hardware, model names, database, and the config knobs that go with them.

> **Pointing Claude at this file:** start a prompt with
> "read `docs/server-and-models.md` for our server/model/DB setup" and you don't
> have to re-explain the environment. Keep it updated when any of it changes —
> a stale line here becomes a wrong assumption in every future session.

Last verified: **2026-08-10**.

Related docs: `docs/llm-transport-and-deployment.md` (why the transport is
OpenAI-compatible, deployment recipes, context-length trap), `DOCKER.md`
(container specifics), `CLAUDE.md` (code-level gotchas).

---

## 1. Topology

```
Frontend  →  GATEWAY (:8000)  →  Ollama (:11434, OpenAI-compatible /v1)
                  |                inference + embeddings only
                  ├─ Postgres (+ vector data for RAG)
                  ├─ remote MCP server (:3333/mcp, business tools)
                  └─ ingest worker (separate process, department RAG)
```

The gateway is the only authenticated front door. It is the MCP client and it
executes every tool; Ollama only runs the model and names the tool to call.

---

## 2. Live server (GPU box, referred to as `chatbot`)

Address/SSH user deliberately not recorded here — use `<SERVER_HOST>`.

### Hardware

| Resource | Detail |
|---|---|
| CPU | 2× Intel Xeon Gold 5415+ — 16 cores / 32 threads total (2 sockets, NUMA) |
| GPU | 2× NVIDIA A40, **46 GB VRAM each (~92 GB total)**, driver 580.173.02, CUDA 13.0 |
| RAM | 125 GB (≈119 GB available) |
| Swap | none |

### What actually runs there today

Ollama is a **container, not systemd** — it belongs to an unrelated compose
stack at `/home/localllm/backend-local/docker-compose.yml`:

| Container | Role for us |
|---|---|
| `nic_ollama` (`ollama/ollama:latest`, `runtime: nvidia`, volume `ollama_data:/root/.ollama`) | the model server; publishes `11434:11434` |
| `nic_postgres` | the Postgres we piggyback on; publishes `5432` |
| `nic_qdrant` | belongs to that other app — **we do not use it** (our vectors live in Postgres) |

We only consume the published ports via `host.docker.internal`. We don't own
that stack, so treat edits to it as touching someone else's app.

### Deployment status (important)

**The gateway itself is NOT deployed on the server yet.** It runs on the laptop
and points `OLLAMA_BASE_URL` at the server's `:11434`. The container images
build and boot but have not been run in anger — see `DOCKER.md`.
So "live server" today means: **live model server + live database**, laptop app.

---

## 3. Models

All served by Ollama over the OpenAI-compatible surface
(`/v1/chat/completions`, `/v1/models`, `/v1/embeddings`).

| Purpose | Model | Where | Config key |
|---|---|---|---|
| Chat / agent (server) | **`qwen3.5:35b-a3b`** — MoE, ~3B active params | GPU box | `AGENT_MODEL` |
| Chat / agent (laptop dev) | `qwen2.5:latest` (7.6B) | local Ollama | `AGENT_MODEL` |
| Department-RAG embeddings | **`qwen3-embedding:4b-q8_0`** | GPU box | `RAG_EMBED_MODEL` |
| Reranker | `qwen3-reranker:4b` — **pulled but OFF** | GPU box | `RAG_RERANK_MODEL`, `RAG_RERANK_ENABLED=false` |
| Legacy/fallback embed | `nomic-embed-text:latest` — not used by department RAG | both | `DEFAULT_EMBED_MODEL` |

All four are pulled on the server's Ollama (verified 2026-08-10).

Model facts that bite:

- **Qwen3-Embedding is asymmetric.** Queries get an `Instruct:`/`Query:` prefix,
  documents do not. `embed_texts` requires an explicit `mode` — getting it wrong
  silently degrades retrieval rather than erroring.
- **Native embedding output is 2560 dims, MRL-truncated to 1536** because
  pgvector's HNSW index caps at 2000. `RAG_EMBED_DIM=1536` must match the
  schema's `vector(1536)`; the worker's preflight refuses to start on a mismatch.
- **Reranking is off**, so there is no calibrated relevance score and therefore
  no abstention threshold. `RAG_RELEVANCE_THRESHOLD` is only consulted when
  reranking is turned on.
- **Tool-calling behaviour is model- and shim-dependent.** The 35B MoE has not
  been fully re-verified for multi-tool turns; the local 7B was the reference.

### Context length — the recurring trap

`num_ctx` does **not** exist on the `/v1` surface, and Ollama's shim ignores a
passthrough `options.num_ctx`. Without configuration Ollama defaults to
**4096 tokens**, which is too small. **Measured 2026-08-11** (`usage.prompt_tokens`
against qwen2.5, 15 local tools): the tool schemas alone are **3,475 tokens** and
a bare turn's prompt floor is **3,778** — about **300 tokens** of a 4096 window
left for the conversation, the tool result and the answer. Every added tool
raises that floor, so re-measure after adding one. Set it **on the Ollama
service**:

| Host | Where it goes |
|---|---|
| GPU box | `nic_ollama`'s compose `environment:` → `OLLAMA_CONTEXT_LENGTH: 32768`, then `docker compose up -d ollama` to **recreate** (editing compose alone does nothing) |
| Laptop | `sudo systemctl edit ollama` → `Environment="OLLAMA_CONTEXT_LENGTH=32768"`, restart |

Never in the gateway `.env` — different process, different config home. Verify:
`docker exec nic_ollama printenv | grep CONTEXT`, then
`docker exec nic_ollama ollama ps` must show `PROCESSOR` = **100% GPU** (a bigger
KV cache can spill the model to CPU; step down to 16384 if so).

---

## 4. Database

**Postgres, with the vector data held in Postgres too** (pgvector) — there is no
separate vector database in our stack. `nic_qdrant` on the server belongs to a
different app.

| | Live | Laptop dev |
|---|---|---|
| Server | `nic_postgres` container on the GPU box, published `:5432` | local Postgres on `127.0.0.1:5432` |
| Database | `local_ai_gateway` (its own DB inside that container) | `local_ai_gateway` |
| App role | `gateway` | `gateway` |
| Reached as | `host.docker.internal:5432` from a container, host/IP otherwise | `127.0.0.1:5432` |

Credentials live in `.env` / `.env.docker` only — never in code, never in this
file.

- `pgvector` must be enabled **once per database by a superuser**
  (`CREATE EXTENSION vector`); the Alembic migration does it, but the migration
  user needs the privilege.
- Schema is owned by Alembic: `.venv/bin/alembic upgrade head`. In Docker the
  one-off `migrate` service runs it before `gateway` and `worker` start.
- Tables by area: `users`; `chat_sessions` + `chat_messages`; `generated_files`;
  RAG = `departments`, `user_departments`, `documents`, `document_chunks`,
  `ingest_jobs`.
- Vector storage: `document_chunks.embedding` is `vector(1536)` with an **HNSW**
  index, plus a **STORED generated `tsv`** column (`'english'` config) with a GIN
  index for the keyword channel. Both indexes are hand-written in the migration
  and excluded from autogenerate comparison — Alembic cannot reflect an HNSW
  opclass.
- The host's Postgres must accept the docker bridge if the gateway is
  containerized: `listen_addresses` reachable + a `pg_hba.conf` line for
  `172.16.0.0/12`.

---

## 5. RAG pipeline settings (current values)

| Setting | Value | Note |
|---|---|---|
| `RAG_DOCS_DIR` | `rag_documents` | `documents.storage_key` is **relative** to this |
| `RAG_CHUNK_MAX_CHARS` / `_OVERLAP_CHARS` | 2000 / 200 | |
| `RAG_EMBED_BATCH` | 32 | texts per `/v1/embeddings` call |
| `RAG_TOP_K` | 12 | chunks handed to the model |
| `RAG_CANDIDATE_POOL` | 50 | per channel, before fusion |
| `RAG_RRF_K` | 60 | reciprocal-rank fusion constant |
| `RAG_HNSW_EF_SEARCH` | 100 | must be ≥ per-channel pool |
| `RAG_TOOL_RESULT_MAX_CHARS` | 7000 | under the loop's 8000 cap, so citations aren't severed |
| `RAG_INGEST_POLL_SECONDS` | 2.0 | worker poll |
| `RAG_INGEST_STALE_MINUTES` / `_HEARTBEAT_SECONDS` | 10 / 30 | heartbeat keeps long parses from being swept |

Retrieval is **hybrid**: pgvector cosine (dense) + `ts_rank_cd` (keyword), fused
by RRF. The department is **never a tool argument** — it comes from the session
via a contextvar, so prompt injection has nothing to target.

**Ingestion is a separate process:** `.venv/bin/python -m app.rag.worker` (its
own image, `Dockerfile.worker`, `requirements-worker.txt`). Docling drags in
torch + CPU-only wheels, which must never enter the slim API image. The API
never parses or embeds — upload returns **202** and queues a job.

---

## 6. Ports & processes

| Thing | Port | Notes |
|---|---|---|
| This gateway | **8000** | `.venv/bin/uvicorn app.main:app --reload --port 8000`, Swagger at `/docs` |
| Sibling `local-ai-model` | 8001 | the original port-from project; never share 8000 |
| Ollama | 11434 | OpenAI-compatible base is `<host>:11434` |
| Postgres | 5432 | |
| MCP server (`node/local-llm-mcp`, FastMCP) | 3333 | `MCP_SERVER_URL=http://<host>:3333/mcp`; blank = local tools only |

Ingest worker has no port — it polls Postgres.

### Outbound internet dependencies

Everything above is ours. Two tools reach the public internet from the gateway
process, so a deployment behind an egress firewall must allow them or they fail
at call time (readably, as a tool `ERROR:`, not a 500):

| Host | Used by | Note |
|---|---|---|
| `www.nrb.org.np` | `get_nrb_forex` → `app/nrb/client.py` | official NRB Forex API, `NRB_API_BASE_URL`; HTTPS, GET only, no credentials, redirects NOT followed |
| any public host | `fetch_url` | SSRF-guarded (every resolved IP must be public); `FETCH_URL_ENABLED=false` disables it, `FETCH_URL_ALLOWLIST` narrows it |

Neither takes a host from the model: NRB's is config plus a hardcoded `/rates`
path, and `fetch_url` is the only tool that accepts a URL at all.

---

## 7. Where each variable is read

| Variable | Read by | Lives in |
|---|---|---|
| `DATABASE_URL`, `JWT_SECRET`, `AGENT_MODEL`, `OLLAMA_BASE_URL`, `RAG_*`, … | the gateway (and the worker, for `RAG_*` + `DATABASE_URL`) | gateway `.env` / `.env.docker` |
| `OLLAMA_CONTEXT_LENGTH` | **Ollama** | the Ollama service's own env (compose `environment:` or systemd unit) |
| `NRB_API_BASE_URL` | the gateway (`get_nrb_forex`) | gateway `.env`; default `https://www.nrb.org.np/api/forex/v1` |

There is **no timezone variable**. "Today" comes from `app/localtime.py`, which
hardcodes Nepal's fixed UTC+05:45 — `zoneinfo` would need system tzdata the slim
images don't install, and a UTC-derived date is already the next day in Kathmandu
after 18:15 UTC. Both the system prompt's date line and `get_nrb_forex`'s default
date read it, so they cannot disagree.

Pydantic settings are `extra="ignore"`, so a misspelled or obsolete key (the old
`AGENT_NUM_CTX` is the classic) is **silently dropped** — it looks configured and
isn't.

Branding: `ASSISTANT_NAME=NIC AI`, `ASSISTANT_ORG=NIC Bank` (deployment config,
not a security boundary — the real model id is still in the `/v1/chat` body).
`EXPOSE_TRACE` gates whether the execution trace leaves the gateway; it is
persisted to `chat_messages.trace` either way.

---

## 8. Backend swap checklist

Moving local Ollama → server Ollama → vLLM is config only:

1. `OLLAMA_BASE_URL` → the new server's OpenAI base URL.
2. `AGENT_MODEL` → whatever name that server serves the model under.
3. Context: `OLLAMA_CONTEXT_LENGTH` (Ollama) or `--max-model-len` (vLLM, plus
   `--tensor-parallel-size 2` for the two A40s).
4. Restart the gateway. No code edits — the wire format lives only in
   `app/ollama/client.py`.

---

## 9. Known gaps / not yet live

- Gateway not deployed on the server (containers unproven in real use).
- Live-server tool-calling with `qwen3.5:35b-a3b` not fully re-verified —
  especially **multi-tool** turns (`tool_call_id` correlation).
- Reranking disabled; no abstention threshold in effect.
- Frontend not built.
- Deploy hardening deferred by choice: firewall Ollama/Postgres/MCP to the
  gateway's IP so only the gateway is reachable.

---

## 10. Quick commands

```bash
# gateway (laptop)
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/alembic upgrade head
.venv/bin/pytest
.venv/bin/python -m app.rag.worker        # ingest worker, separate process

# containers
docker compose up --build                  # migrate -> gateway + worker

# on the GPU box, against the Ollama container
docker exec nic_ollama ollama list
docker exec nic_ollama ollama ps                       # PROCESSOR must be 100% GPU
docker exec nic_ollama printenv | grep CONTEXT
```

Test login (dev): `admin@example.com` / `supersecret123`.

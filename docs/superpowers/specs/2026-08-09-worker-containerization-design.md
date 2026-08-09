# Ingest Worker Containerization — Design

**Date:** 2026-08-09
**Status:** Approved (design), pending implementation plan
**Scope:** Run the department-RAG ingest worker as a separate Docker image and
service, sharing the corpus directory with the API. Fix the config/ignore gaps
that currently prevent Dockerized RAG from working at all.

## Problem

RAG ingestion runs in a separate process (`python -m app.rag.worker`) with its
own dependency set (`requirements-worker.txt` = `requirements.txt` + Docling).
Docling pulls torch + torchvision + transformers + OpenCV + the NVIDIA CUDA
stack — several GB — which must never enter the slim API image.

The current Docker setup (`Dockerfile`, `docker-compose.yml`) ships only the
API (`migrate` + `gateway`). It has **no worker**, and more fundamentally it
cannot run RAG end-to-end even if the worker existed, because:

1. The API writes each upload to `RAG_DOCS_DIR` (`rag_documents/`) and the
   worker reads that same file to parse it. Postgres is the *job* channel, but
   the file bytes pass through a shared directory. Compose has no volume for
   `rag_documents/`, so API and worker would each get a private copy and every
   job would fail "file not found".
2. `.env.docker.example` never sets `RAG_DOCS_DIR`.
3. `.dockerignore` excludes `generated_files/` but not `rag_documents/`, so the
   corpus would be pulled into the build context.

## Non-goals

- GPU-accelerated Docling. We ship CPU-only. (A GPU worker is a later option:
  `deploy.resources` devices + CUDA torch + nvidia-container-toolkit.)
- Baking Docling models into the image. We persist them via a cache volume.
- Any change to the API `Dockerfile`, the app code, or the RAG schema. This is
  packaging only.

## Design

### 1. `Dockerfile.worker` (new)

Two-stage build mirroring the API `Dockerfile` (builder compiles the venv;
runtime stage carries only the prebuilt venv + app code, runs as non-root
`appuser`). Differences:

- **CPU-only torch.** Install CPU torch from the PyTorch CPU index
  (`--index-url https://download.pytorch.org/whl/cpu`) *before* installing
  `requirements-worker.txt`, so pip resolves the already-present CPU torch
  instead of pulling the multi-GB CUDA build. Saves gigabytes.
- Installs `requirements-worker.txt` (adds Docling).
- `CMD ["python", "-m", "app.rag.worker"]`. No `EXPOSE` — it is a background
  poller, not an HTTP server. No `HEALTHCHECK` (nothing to probe over HTTP).
- Copies `app/` only. No `alembic/` — the worker never migrates; the `migrate`
  service owns schema.
- Creates and `chown`s `/app/rag_documents` and the cache dir so the non-root
  user can write.

### 2. `docker-compose.yml` changes

Add a `worker` service and two named volumes; mount the corpus volume into the
**gateway** as well.

```yaml
services:
  gateway:
    # ...existing...
    volumes:
      - gateway_files:/app/generated_files
      - rag_documents:/app/rag_documents        # NEW: shared with worker

  worker:                                        # NEW
    build:
      context: .
      dockerfile: Dockerfile.worker
    env_file: .env.docker
    depends_on:
      migrate:
        condition: service_completed_successfully
    extra_hosts:
      - "host.docker.internal:host-gateway"      # reach host Postgres + Ollama
    restart: unless-stopped
    volumes:
      - rag_documents:/app/rag_documents         # SHARED with gateway
      - worker_cache:/home/appuser/.cache        # persist Docling models

volumes:
  gateway_files:
  rag_documents:      # NEW
  worker_cache:       # NEW
```

- **`rag_documents` shared volume** is the crux: the same directory is mounted
  at `/app/rag_documents` in both containers, so a file the gateway writes on
  upload is the exact file the worker parses.
- **`worker_cache`** persists the layout/table models Docling downloads on the
  first parse (~hundreds of MB), so container restarts don't re-download.
- `depends_on: migrate` — the worker needs the schema present, not the gateway
  running. It does not depend on `gateway`.
- `restart: unless-stopped` — a transient Ollama outage recovers; a genuine
  embedding-dimension mismatch crash-loops visibly (correct: preflight refuses
  to run against the wrong model).

### 3. Config + ignore fixes

- `.env.docker.example`: add `RAG_DOCS_DIR=rag_documents` (both services read
  the same `.env.docker`, so the path agrees). Add a comment that the embedding
  model (`qwen3-embedding:4b-q8_0`) must be pulled on the Ollama host, or the
  worker's `preflight` exits on startup.
- `.dockerignore`: add `rag_documents/`.

### 4. Documentation

Update `DOCKER.md` (and the README's worker section) to describe the new
`worker` service, the shared volume, and the embedding-model prerequisite.

## Constraints the design must respect

- **Dependency isolation:** Docling/torch/CUDA live only in the worker image;
  the API image stays on `requirements.txt`.
- **Preflight prerequisite:** compose cannot `ollama pull`. The embedding model
  being present on the Ollama host is a documented precondition, not something
  the stack provisions.
- **Shared-path invariant:** `RAG_DOCS_DIR` must resolve to the same mounted
  volume in both services. If they diverge, ingestion silently fails.

## Testing / acceptance

1. `docker compose build worker` succeeds; image is materially smaller than a
   CUDA-torch build would be (sanity check on the CPU-torch choice).
2. `docker compose up --build` brings up `migrate` → `gateway` + `worker`.
3. End-to-end: register/login → `POST /v1/departments/{code}/documents` (202)
   → the **worker** container logs claim the job → `GET /v1/ingest-jobs/{id}`
   reaches `succeeded` → a department chat retrieves and cites the document.
4. Shared-volume proof: a file written under `/app/rag_documents` in the
   gateway container is visible at the same path in the worker container.

## Evaluation & Improvement

- **Success metric:** a document uploaded via the Dockerized gateway reaches
  `succeeded` and becomes retrievable — i.e. the containerized worker completes
  the same end-to-end path already proven on the host. Binary per release.
- **Eval:** the 4 acceptance checks above, run against `docker compose up` on a
  clean volume set. Pass = all 4 green. (No labelled dataset applies; this is
  packaging, and retrieval quality is covered by the RAG feature's own evals.)
- **Feedback capture:** worker container logs (job claim → parse → embed →
  commit, plus preflight result) are the primary signal; `ingest_jobs.status`
  and `.error` in Postgres are the durable record of failures.
- **Review loop:** revisit when the embedding model, Docling major version, or
  base image changes — any of which can break preflight or the CPU-torch
  resolution. Re-run the 4 acceptance checks on those bumps.

# Ingest Worker Containerization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the department-RAG ingest worker as its own Docker image + compose service, sharing the corpus directory with the gateway, so `docker compose up` runs the full RAG stack.

**Architecture:** A new `Dockerfile.worker` mirrors the slim API image but adds Docling (CPU-only torch). A `worker` compose service runs `python -m app.rag.worker`. A named `rag_documents` volume is mounted into BOTH the gateway (writes uploads) and the worker (reads them to parse) — Postgres carries the job, the shared volume carries the bytes. A `worker_cache` volume persists Docling's downloaded models.

**Tech Stack:** Docker multi-stage build, docker compose, Python 3.10, Docling, PyTorch (CPU wheel), FastAPI gateway (unchanged deps).

## Global Constraints

- **Dependency isolation:** Docling/torch/CUDA live ONLY in the worker image. The API image stays on `requirements.txt` — no worker deps added to it.
- **CPU-only torch:** install torch from `https://download.pytorch.org/whl/cpu`. The final worker image must contain NO `nvidia-*`/CUDA packages.
- **Shared-path invariant:** `RAG_DOCS_DIR` must resolve to the same mounted volume in gateway and worker (`/app/rag_documents`). If they diverge, ingestion silently fails.
- **Non-root runtime:** both images run as `appuser` (uid 10001). Any volume mount point must be created and `chown`ed to `appuser` in the image, or the non-root process can't write to it.
- **Preflight prerequisite (documented, not provisioned):** `qwen3-embedding:4b-q8_0` must be present on the Ollama host or the worker exits on startup. Compose cannot `ollama pull`.
- **External services:** Postgres, Ollama, MCP run on the host; containers reach them via `host.docker.internal` (`extra_hosts`).

---

## File Structure

- **Create** `Dockerfile.worker` — worker image (venv + Docling + app code, CPU torch).
- **Modify** `Dockerfile` — one line: also `mkdir -p rag_documents` so the gateway's shared-volume mount point is appuser-owned.
- **Modify** `docker-compose.yml` — add `worker` service; add `rag_documents` + `worker_cache` volumes; mount `rag_documents` into gateway and worker.
- **Modify** `.dockerignore` — exclude `rag_documents/`.
- **Modify** `.env.docker.example` — add `RAG_DOCS_DIR=rag_documents` + preflight note.
- **Modify** `DOCKER.md` and **`README.md`** — document the worker service, shared volume, and model prerequisite.

---

### Task 1: Config + ignore fixes

Small, no build required. These make the path agree across services and keep the corpus out of the build context.

**Files:**
- Modify: `.dockerignore`
- Modify: `.env.docker.example`

- [ ] **Step 1: Exclude the corpus from the build context**

In `.dockerignore`, under the `generated_files/` line, add:

```
# Runtime-generated files (mount a volume instead)
generated_files/
# RAG corpus uploads (mount a volume instead)
rag_documents/
```

- [ ] **Step 2: Set RAG_DOCS_DIR in the Docker env example**

In `.env.docker.example`, near the `FILES_DIR=generated_files` line, add:

```
FILES_DIR=generated_files
# Corpus documents (department RAG). MUST match between gateway and worker —
# both mount the same rag_documents volume at /app/rag_documents.
RAG_DOCS_DIR=rag_documents
# The ingest worker refuses to start unless this embedding model is present on
# the Ollama host (docker compose cannot pull it): ollama pull qwen3-embedding:4b-q8_0
```

- [ ] **Step 3: Verify the build context no longer includes the corpus**

Run:
```bash
mkdir -p rag_documents && touch rag_documents/_probe.pdf
docker build -q -t ctx-check . >/dev/null && \
  docker run --rm ctx-check sh -c 'ls /app/rag_documents 2>/dev/null || echo "not-in-image"'
```
Expected: prints `not-in-image` (the corpus dir exists as a volume mount point later, but its *contents* are never baked in). Then `rm rag_documents/_probe.pdf`.

- [ ] **Step 4: Commit**

```bash
git add .dockerignore .env.docker.example
git commit -m "chore(docker): set RAG_DOCS_DIR + ignore rag_documents in build context"
```

---

### Task 2: `Dockerfile.worker`

**Files:**
- Create: `Dockerfile.worker`

**Interfaces:**
- Produces: an image whose `CMD` is `python -m app.rag.worker`, running as `appuser`, with Docling installed and CPU-only torch. Compose (Task 3) builds it via `dockerfile: Dockerfile.worker`.

- [ ] **Step 1: Write `Dockerfile.worker`**

```dockerfile
# syntax=docker/dockerfile:1

# ============================================================================
# Ingest worker for department RAG. Separate image because Docling pulls torch
# + the CUDA stack (several GB) that must never enter the slim API image.
# CPU-only torch keeps this image as small as that allows.
# ============================================================================

# ---- Stage 1: build the virtualenv --------------------------------------
FROM python:3.10-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# CPU torch FIRST, from the PyTorch CPU index, so the subsequent Docling install
# finds torch already satisfied and never resolves the multi-GB CUDA build.
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

COPY requirements-worker.txt requirements.txt ./
RUN pip install -r requirements-worker.txt

# ---- Stage 2: runtime ----------------------------------------------------
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# App code only — the worker never migrates (the `migrate` service owns schema).
COPY app ./app

# Mount points must exist and be appuser-owned BEFORE the named volumes mount,
# or Docker initializes the volumes root-owned and the non-root worker can't
# write. rag_documents = shared corpus; ~/.cache = Docling model downloads.
RUN mkdir -p /app/rag_documents /home/appuser/.cache \
    && chown -R appuser:appuser /app /home/appuser/.cache

USER appuser

# Background poller: no port, no HTTP healthcheck.
CMD ["python", "-m", "app.rag.worker"]
```

- [ ] **Step 2: Build the worker image**

Run: `docker build -f Dockerfile.worker -t gateway-worker .`
Expected: build succeeds through both stages.

- [ ] **Step 3: Verify CPU-only torch (no CUDA baked in)**

Run:
```bash
docker run --rm gateway-worker sh -c \
  "python -c 'import torch; print(torch.__version__)'; pip list | grep -iE 'nvidia|cuda' || echo NO-CUDA"
```
Expected: torch version ends in `+cpu`, and the last line is `NO-CUDA`.
If CUDA packages appear, pin torch/torchvision to the version Docling requires from the CPU index and rebuild.

- [ ] **Step 4: Verify Docling imports and the worker entrypoint is runnable**

Run:
```bash
docker run --rm gateway-worker python -c "import docling, app.rag.worker; print('worker-image-ok')"
```
Expected: prints `worker-image-ok` (imports resolve; no DB/Ollama contacted by import alone).

- [ ] **Step 5: Commit**

```bash
git add Dockerfile.worker
git commit -m "feat(docker): worker image with Docling and CPU-only torch"
```

---

### Task 3: Compose — worker service + shared volume + gateway mount point

**Files:**
- Modify: `docker-compose.yml`
- Modify: `Dockerfile` (one line — gateway mount point)

**Interfaces:**
- Consumes: `Dockerfile.worker` (Task 2), `RAG_DOCS_DIR=rag_documents` (Task 1).

- [ ] **Step 1: Ensure the gateway image owns its rag_documents mount point**

In `Dockerfile`, change the mkdir line (currently `RUN mkdir -p generated_files && chown -R appuser:appuser /app`) to:

```dockerfile
# generated_files (per-user output) and rag_documents (RAG corpus, shared with
# the worker via a named volume) must exist + be appuser-owned before their
# volumes mount, or Docker initializes them root-owned and the non-root gateway
# can't write.
RUN mkdir -p generated_files rag_documents && chown -R appuser:appuser /app
```

- [ ] **Step 2: Add the shared volume mount to the gateway service and the new worker service**

In `docker-compose.yml`, add `rag_documents` to the gateway's volumes and append the `worker` service. Final `services`/`volumes` shape:

```yaml
  gateway:
    build: .
    env_file: .env.docker
    ports:
      - "8000:8000"
    depends_on:
      migrate:
        condition: service_completed_successfully
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - gateway_files:/app/generated_files
      - rag_documents:/app/rag_documents          # shared with worker

  worker:
    build:
      context: .
      dockerfile: Dockerfile.worker
    env_file: .env.docker
    depends_on:
      migrate:
        condition: service_completed_successfully
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
    volumes:
      - rag_documents:/app/rag_documents          # SAME volume the gateway writes
      - worker_cache:/home/appuser/.cache         # persist Docling models

volumes:
  gateway_files:
  rag_documents:
  worker_cache:
```

- [ ] **Step 3: Validate the compose file parses and resolves both build contexts**

Run: `docker compose config >/dev/null && echo COMPOSE-OK`
Expected: prints `COMPOSE-OK` (no YAML/merge errors; both `build` targets resolve).

- [ ] **Step 4: Build both images through compose**

Run: `docker compose build`
Expected: `gateway`, `migrate` (same image), and `worker` all build successfully.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml Dockerfile
git commit -m "feat(docker): worker service + shared rag_documents volume"
```

---

### Task 4: End-to-end verification (real stack)

Not a code change — the acceptance gate. Requires host Postgres + Ollama running and `qwen3-embedding:4b-q8_0` pulled, and a real `.env.docker` (copied from the example with `JWT_SECRET` + DB creds set).

**Files:** none (verification only).

- [ ] **Step 1: Bring up the stack**

Run: `docker compose up --build -d`
Then: `docker compose ps`
Expected: `migrate` exited 0; `gateway` and `worker` are `running`.

- [ ] **Step 2: Confirm the worker passed preflight (didn't crash-loop on the model)**

Run: `docker compose logs worker | tail -20`
Expected: startup lines indicating the worker is polling; NO repeated preflight/dimension-mismatch exits. (If it crash-loops, the embedding model isn't on the Ollama host — pull it and `docker compose up -d worker`.)

- [ ] **Step 3: Prove the shared volume (a gateway-written file is visible to the worker)**

Run:
```bash
docker compose exec gateway sh -c 'echo probe > /app/rag_documents/_shared_probe.txt'
docker compose exec worker sh -c 'cat /app/rag_documents/_shared_probe.txt'
docker compose exec gateway sh -c 'rm /app/rag_documents/_shared_probe.txt'
```
Expected: the middle command prints `probe`. (If it errors "No such file", the two services are not on the same volume — the mount is misconfigured.)

- [ ] **Step 4: Full ingest path through the containers**

```bash
BASE=http://localhost:8000
# register (first user -> admin) + login
curl -s -X POST $BASE/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"supersecret123"}' >/dev/null
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"supersecret123"}' | jq -r .access_token)
# create a department
curl -s -X POST $BASE/v1/departments -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"code":"hr","name":"HR"}' >/dev/null
# upload a document (use any small PDF/DOCX at ./sample.pdf)
RESP=$(curl -s -X POST $BASE/v1/departments/hr/documents -H "Authorization: Bearer $TOKEN" \
  -F 'title=Leave Policy' -F 'file=@sample.pdf')
echo "$RESP"
JOB=$(echo "$RESP" | jq -r .job_id)
# poll the job
for i in $(seq 1 30); do
  S=$(curl -s $BASE/v1/ingest-jobs/$JOB -H "Authorization: Bearer $TOKEN")
  echo "$S" | jq -c '{status, chunks_done, chunks_total, error}'
  echo "$S" | jq -e '.status=="succeeded"' >/dev/null && break
  echo "$S" | jq -e '.status=="failed"' >/dev/null && { echo FAILED; break; }
  sleep 2
done
```
Expected: job transitions `queued`/`running` → `succeeded` with `chunks_total > 0`.

- [ ] **Step 5: Confirm retrieval cites the ingested document**

```bash
curl -s -X POST $BASE/v1/chat -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"department":"hr","message":"How does annual leave accrue?","stream":false}' \
  | jq -r '.message.content'
```
Expected: an answer grounded in the uploaded document (mentions its content / a citation), not a generic refusal.

- [ ] **Step 6: Tear down**

Run: `docker compose down`
(Keep volumes; add `-v` only to wipe the corpus + cache.)

---

### Task 5: Documentation

**Files:**
- Modify: `DOCKER.md`
- Modify: `README.md` (worker section, ~lines 83–119)

- [ ] **Step 1: Document the worker service in `DOCKER.md`**

Add a section describing: the `worker` service and `Dockerfile.worker`; that Docling/torch live only in the worker image; the shared `rag_documents` volume (both services mount `/app/rag_documents`); the `worker_cache` volume for Docling models; and the `ollama pull qwen3-embedding:4b-q8_0` prerequisite. Note `docker compose up --build` now starts `migrate` → `gateway` + `worker`.

- [ ] **Step 2: Cross-reference from the README worker section**

In `README.md`, under "Ingestion worker (department RAG)", add a short note that in Docker the worker runs as the `worker` compose service (see `DOCKER.md`), sharing the `rag_documents` volume with the gateway; the host command `python -m app.rag.worker` remains for non-Docker runs.

- [ ] **Step 3: Commit**

```bash
git add DOCKER.md README.md
git commit -m "docs(docker): document the worker service, shared volume, model prereq"
```

---

## Self-Review

**Spec coverage:**
- Dockerfile.worker (CPU torch, Docling, non-root, CMD) → Task 2. ✔
- Worker compose service + `rag_documents` shared volume + `worker_cache` → Task 3. ✔
- Gateway mounts `rag_documents` → Task 3 Step 2. ✔
- `.env.docker.example` RAG_DOCS_DIR + preflight note → Task 1. ✔
- `.dockerignore` rag_documents → Task 1. ✔
- Docs (DOCKER.md + README) → Task 5. ✔
- Acceptance tests (build size/CPU-torch, up, e2e ingest, shared-volume proof) → Tasks 2–4. ✔
- **Spec deviation:** spec non-goal "no change to the API Dockerfile" is corrected — a one-line `mkdir -p rag_documents` is required (Task 3 Step 1) for the non-root gateway to write to the shared volume. Spec updated to match.

**Placeholder scan:** none — every step has concrete commands/content. `sample.pdf` in Task 4 is a caller-supplied test fixture, not a plan placeholder.

**Type consistency:** paths and names consistent — `Dockerfile.worker`, volume names `rag_documents`/`worker_cache`/`gateway_files`, mount path `/app/rag_documents`, `RAG_DOCS_DIR=rag_documents`, service name `worker`, CMD `python -m app.rag.worker` all match across tasks.

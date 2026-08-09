# syntax=docker/dockerfile:1

# ============================================================================
# Local LLM Gateway — the single authenticated front door (FastAPI, :8000).
# Two-stage build: deps compile in `builder`, the final image only carries the
# prebuilt virtualenv + app code, runs as a non-root user, and never bakes in
# secrets (config is injected via runtime env — .env is .dockerignore'd).
# ============================================================================

# ---- Stage 1: build the virtualenv --------------------------------------
FROM python:3.10-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build toolchain lives ONLY in this stage (discarded from the final image),
# so wheels that lack a prebuilt manylinux artifact can still compile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- Stage 2: runtime ----------------------------------------------------
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root runtime user (least privilege).
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Prebuilt virtualenv from the builder stage — no compilers in the final image.
COPY --from=builder /opt/venv /opt/venv

# Application code + migrations. (No .env — see .dockerignore.)
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# FILES_DIR default; must be writable by the runtime user. Mount a volume here
# in prod if generated files need to survive container restarts.
# generated_files (per-user output) and rag_documents (RAG corpus, shared with
# the worker via a named volume) must exist + be appuser-owned before their
# volumes mount, or Docker initializes them root-owned and the non-root gateway
# can't write.
RUN mkdir -p generated_files rag_documents && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Liveness against the public /health endpoint (slim has no curl, so use stdlib).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

# Migrations are NOT run here on purpose (avoids races across replicas). Run
# them as a one-off before rollout:
#   docker run --rm --env-file .env <image> alembic upgrade head
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

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

COPY requirements.txt requirements-ocr.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- Optional: image OCR (the read_image tool) --------------------------
# A build ARG rather than a line in requirements.txt because it is ~270 MB
# (rapidocr + onnxruntime + opencv) that a deployment which never OCRs an image
# should not carry. It needs NO torch — see requirements-ocr.txt.
#
#   docker compose build --build-arg INSTALL_OCR=true gateway
#
# Omitted (the default), `read_image` reports "image OCR is not enabled on this
# deployment" and every other route behaves identically. Uploading an image
# still works either way: Pillow is in requirements.txt because the pixel-bomb
# guard is a security control, not part of the optional feature.
ARG INSTALL_OCR=false
RUN if [ "$INSTALL_OCR" = "true" ]; then \
        pip install -r requirements-ocr.txt; \
    else \
        echo "image OCR OMITTED (INSTALL_OCR=false) — read_image will report it is not enabled"; \
    fi

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

# ---- Optional: make the installed OCR stack actually USABLE --------------
# Each of these three is a §18-class defect: omit it and the stack is present,
# the job "succeeds", and no text ever comes out.
#
#  1. opencv's cv2.abi3.so links X11/XCB/GL. python:*-slim ships none of them,
#     so the first OCR call dies with "libxcb.so.1: cannot open shared object
#     file" — at request time, not build time.
#  2. RapidOCR downloads its ONNX weights on FIRST USE, into its own package
#     directory. Pre-warming here means no request depends on network access and
#     the first upload is not seconds slower than the rest.
#  3. That directory is root-owned after COPY --from, and this container runs as
#     uid 10001. The write then fails SILENTLY AND EXPENSIVELY: rapidocr
#     re-downloads on every call, never persists, and returns no text — correct
#     (fail-closed) but indistinguishable from an image with no text in it.
#     Measured in the worker image 2026-08-16 (see Dockerfile.worker).
#
# The warm step imports app.files.image_ocr rather than repeating the model
# configuration here, so there is exactly one place that names PP-OCRv5. It is
# deliberately NOT `|| true`: a build that cannot fetch the weights must fail
# loudly instead of shipping an image that looks fine and reads nothing.
ARG INSTALL_OCR=false
RUN if [ "$INSTALL_OCR" = "true" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends \
            libgl1 libglib2.0-0 libxcb1 libx11-6 libxext6 libxrender1 libsm6 \
        && rm -rf /var/lib/apt/lists/* \
        && python -c "from app.files.image_ocr import _engine, SUPPORTED_LANGS; [_engine(l) for l in sorted(SUPPORTED_LANGS)]" \
        && chown -R appuser:appuser /opt/venv/lib/python3.10/site-packages/rapidocr; \
    fi

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

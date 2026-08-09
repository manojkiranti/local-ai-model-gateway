"""The ingest worker: a separate process, deliberately.

Run it with:

    .venv/bin/python -m app.rag.worker

It shares this repository and its database, but NOT the API's dependency set —
Docling drags in torch, transformers, opencv and the CUDA stack, which must
never enter the API image. Ingestion is also slow and memory-hungry, so it does
not belong in a process serving requests.

The loop is deliberately dull: sweep stale jobs, claim one with SKIP LOCKED, do
all the slow work with NO transaction open, then commit one short atomic
replacement. Postgres is the queue; there is no Redis and no Celery.

Shape of `process_job` is the point: no transaction is held while parsing or
embedding, the synchronous Docling parse runs off the event loop via
`asyncio.to_thread`, and a background task heartbeats for the whole duration so
a long job is never swept as stale.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from ..config import Settings, get_settings
from ..ollama.client import OllamaClient
from . import documents as docs_repo
from . import ingest
from . import jobs as jobs_repo
from .embedding import EmbeddingError, embed_texts, truncate_normalize
from .models import JOB_FAILED, JOB_SUCCEEDED, STATUS_ARCHIVED, STATUS_FAILED, STATUS_READY
from .parsing import ParseError, parse_to_chunks
from .storage import resolve_storage_path

log = logging.getLogger("rag.worker")


class WorkerPreflightError(Exception):
    """The embedding backend is unusable; refuse to start."""


@dataclass(frozen=True)
class DocSnapshot:
    """The fields the pipeline needs, read once so no transaction stays open.

    `get_document` runs inside a session, and an SQLAlchemy session holds a
    transaction (and a pooled connection) open from its first query until commit
    or rollback. Parsing a 200-page PDF with that transaction open would pin a
    connection for minutes and hold row locks for no reason. So we snapshot,
    close, and only then do the slow work.
    """

    id: str
    department_id: int
    file_type: str
    storage_key: str | None
    status: str


async def preflight(client, settings: Settings) -> None:
    """Prove the embedding backend works and returns the expected width BEFORE
    touching any job.

    Discovering a dimension mismatch after inserting half a corpus is far worse
    than refusing to boot: `vector(1536)` would start rejecting inserts partway
    through, leaving documents half-indexed.
    """
    try:
        response = await client.embeddings(
            {"model": settings.rag_embed_model, "input": ["preflight"]}
        )
        vector = response["data"][0]["embedding"]
    except Exception as exc:  # noqa: BLE001 - any failure is fatal here
        raise WorkerPreflightError(
            f"embedding backend {settings.ollama_base_url} "
            f"({settings.rag_embed_model}) is unusable: {exc}"
        ) from exc

    try:
        truncated = truncate_normalize(vector, settings.rag_embed_dim)
    except EmbeddingError as exc:
        raise WorkerPreflightError(
            f"{settings.rag_embed_model} returned {len(vector)} dimensions; "
            f"RAG_EMBED_DIM is {settings.rag_embed_dim}. Pull the right model "
            f"(ollama pull {settings.rag_embed_model}) or fix the config."
        ) from exc

    if len(truncated) != settings.rag_embed_dim:  # pragma: no cover - defensive
        raise WorkerPreflightError("truncation did not produce the configured width")

    log.info(
        "preflight ok: %s -> %d native dims, storing %d",
        settings.rag_embed_model, len(vector), settings.rag_embed_dim,
    )


async def _snapshot_document(Session, document_id: str) -> DocSnapshot | None:
    async with Session() as session:
        doc = await docs_repo.get_document(session, document_id)
        snap = (
            None
            if doc is None
            else DocSnapshot(
                id=doc.id,
                department_id=doc.department_id,
                file_type=doc.file_type,
                storage_key=doc.storage_key,
                status=doc.status,
            )
        )
        await session.rollback()  # read-only: end the transaction immediately
        return snap


def _load_chunks_sync(snap: DocSnapshot, settings: Settings):
    """Parse the stored bytes. SYNCHRONOUS and CPU-bound — Docling is not async.

    Called via `asyncio.to_thread` so it cannot block the event loop, which
    would starve the heartbeat and let the stale sweep kill a healthy job.
    """
    if not snap.storage_key:
        raise ParseError(f"document {snap.id} has no storage_key")
    path: Path = resolve_storage_path(snap.storage_key, settings.rag_docs_dir)
    if not path.exists():
        raise ParseError(f"stored file is missing: {snap.storage_key}")
    return parse_to_chunks(
        path,
        snap.file_type,
        max_chars=settings.rag_chunk_max_chars,
        overlap_chars=settings.rag_chunk_overlap_chars,
    )


async def _heartbeat_loop(Session, job_id: str, interval: float) -> None:
    """Keep saying the job is alive until cancelled.

    A single heartbeat after embedding is not enough: a large PDF can spend far
    longer than `rag_ingest_stale_minutes` in parse+embed, and the sweep would
    fail a job that is working perfectly well. Uses its own short-lived session
    per beat so it never contends with the pipeline's transactions.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with Session() as session:
                await jobs_repo.heartbeat(session, job_id)
                await session.commit()
        except Exception:  # noqa: BLE001 - a missed beat must not kill the job
            log.warning("heartbeat failed for job %s", job_id, exc_info=True)


async def _record_failure(Session, job, exc: Exception) -> None:
    """Fail the JOB; demote the DOCUMENT only if it was not already serving.

    A re-ingest that fails must leave a `ready` document exactly as it was —
    its previous chunks are still there and still correct (the replacement
    transaction rolled back), so marking it `failed` would libel a healthy
    document. Only a document that never had a good version becomes `failed`.
    An `archived` document is left alone entirely.
    """
    async with Session() as session:
        doc = await docs_repo.lock_document(session, job.document_id)
        if doc is not None and doc.status not in (STATUS_READY, STATUS_ARCHIVED):
            doc.status = STATUS_FAILED
        await jobs_repo.finish(
            session, job.id, status=JOB_FAILED, error=str(exc)[:2000]
        )
        await session.commit()
    log.warning("ingest failed for %s: %s", job.document_id, exc)


async def process_job(Session, client, settings: Settings, job) -> None:
    """Run one job to completion, recording the outcome on both rows."""
    snap = await _snapshot_document(Session, job.document_id)
    if snap is None:
        await _record_failure(Session, job, RuntimeError("document no longer exists"))
        return

    heart = asyncio.create_task(
        _heartbeat_loop(Session, job.id, settings.rag_ingest_heartbeat_seconds)
    )
    failure: Exception | None = None
    written = total = 0

    try:
        # --- slow work: NO transaction open, off the event loop ---
        chunks = await asyncio.to_thread(_load_chunks_sync, snap, settings)
        total = len(chunks)

        async with Session() as session:
            await jobs_repo.set_chunks_total(session, job.id, total)
            await session.commit()

        vectors = await embed_texts(
            client,
            [c.content for c in chunks],
            mode="document",                      # documents are embedded raw
            model=settings.rag_embed_model,
            dim=settings.rag_embed_dim,
            batch_size=settings.rag_embed_batch,
        )

        # --- short atomic replacement, its own transaction ---
        async with Session() as session:
            written = await ingest.replace_chunks(
                session,
                document_id=snap.id,
                department_id=snap.department_id,
                chunks=chunks,
                embeddings=vectors,
                embed_model=settings.rag_embed_model,
                embed_dim=settings.rag_embed_dim,
            )
            await session.commit()

    except Exception as exc:  # noqa: BLE001 - one job must never kill the loop
        # ParseError / StorageError / EmbeddingError / DocumentGone / ValueError
        # all land here, as does anything Docling raises.
        failure = exc
    finally:
        # Stop the heartbeat BEFORE writing the terminal state, so a beat cannot
        # land after the job is finished.
        heart.cancel()
        with suppress(asyncio.CancelledError):
            await heart

    if failure is not None:
        await _record_failure(Session, job, failure)
        return

    async with Session() as session:
        await jobs_repo.finish(
            session, job.id, status=JOB_SUCCEEDED,
            chunks_total=total, chunks_done=written,
        )
        await session.commit()
    log.info("ingested %s (%d chunks)", snap.id, written)


async def run_once(engine: AsyncEngine, client, settings: Settings) -> bool:
    """Sweep, claim one job, process it. True if a job was handled."""
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        await jobs_repo.sweep_stale(
            session, stale_minutes=settings.rag_ingest_stale_minutes
        )
        await session.commit()
        job = await jobs_repo.claim_next(session)
        await session.commit()

    if job is None:
        return False

    await process_job(Session, client, settings, job)
    return True


async def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    try:
        await preflight(client, settings)
        log.info("ingest worker started; polling every %.1fs",
                 settings.rag_ingest_poll_seconds)
        while not stopping.is_set():
            try:
                did_work = await run_once(engine, client, settings)
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("worker iteration failed; continuing")
                did_work = False
            if not did_work:
                try:
                    await asyncio.wait_for(
                        stopping.wait(), timeout=settings.rag_ingest_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
    finally:
        await client.aclose()
        await engine.dispose()
        log.info("ingest worker stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())

"""The ingest queue — Postgres as the broker, no Redis, no Celery.

Two guarantees that are easy to conflate:

- `SELECT ... FOR UPDATE SKIP LOCKED` stops two workers claiming the same job
  ROW. That is what makes `claim_next` safe to run in N processes.
- `ux_ingest_jobs_active_document` (slice 1) stops two active JOBS existing for
  one document — SKIP LOCKED says nothing about that. `enqueue` translates the
  violation into `JobConflict`, which the router turns into 409 rather than 500.

`heartbeat_at` exists because a worker can die holding a `running` job. The
sweep fails anything whose heartbeat has gone stale, which releases the partial
unique index and lets the document be re-queued.
"""

from __future__ import annotations

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import JOB_FAILED, JOB_QUEUED, JOB_RUNNING, IngestJob


class JobConflict(Exception):
    """An active (queued|running) job already exists for this document."""


async def enqueue(session: AsyncSession, *, document_id: str) -> IngestJob:
    """Queue an ingest. Raises JobConflict if one is already active."""
    job = IngestJob(document_id=document_id, status=JOB_QUEUED)
    session.add(job)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise JobConflict(
            f"an ingest is already queued or running for document {document_id}"
        ) from exc
    return job


async def claim_next(session: AsyncSession) -> IngestJob | None:
    """Atomically take the oldest queued job. None when the queue is empty.

    SKIP LOCKED lets N workers poll the same table without blocking each other:
    a row another transaction holds is passed over rather than waited on.
    """
    claimed_id = (
        await session.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET status       = :running,
                       started_at   = now(),
                       heartbeat_at = now(),
                       attempts     = attempts + 1
                 WHERE id = (
                       SELECT id FROM ingest_jobs
                        WHERE status = :queued
                        ORDER BY created_at
                          FOR UPDATE SKIP LOCKED
                        LIMIT 1)
             RETURNING id
                """
            ),
            {"running": JOB_RUNNING, "queued": JOB_QUEUED},
        )
    ).scalar_one_or_none()

    if claimed_id is None:
        return None
    return await get_job(session, claimed_id)


async def get_job(session: AsyncSession, job_id: str) -> IngestJob | None:
    """Always re-read from the database, never the identity map.

    `claim_next` and `sweep_stale` update via raw SQL, which the ORM cannot
    synchronize, and sessions here run with `expire_on_commit=False`. Without
    `populate_existing` a caller that already loaded this job would keep seeing
    the pre-sweep `running` status forever — the row changes out from under you
    by design in a queue, so a cached read is always the wrong answer.
    """
    return (
        await session.execute(
            select(IngestJob)
            .where(IngestJob.id == job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def heartbeat(session: AsyncSession, job_id: str) -> None:
    """Say the worker is still alive, so the sweep leaves this job alone."""
    await session.execute(
        update(IngestJob).where(IngestJob.id == job_id).values(heartbeat_at=func.now())
    )


async def set_chunks_total(session: AsyncSession, job_id: str, total: int) -> None:
    """Record how many chunks this job will embed, so `chunks_done` has a
    meaningful denominator while the worker is mid-flight."""
    await session.execute(
        update(IngestJob).where(IngestJob.id == job_id).values(chunks_total=total)
    )


async def finish(
    session: AsyncSession,
    job_id: str,
    *,
    status: str,
    error: str | None = None,
    chunks_total: int | None = None,
    chunks_done: int | None = None,
) -> None:
    values: dict = {"status": status, "finished_at": func.now(), "error": error}
    if chunks_total is not None:
        values["chunks_total"] = chunks_total
    if chunks_done is not None:
        values["chunks_done"] = chunks_done
    await session.execute(
        update(IngestJob).where(IngestJob.id == job_id).values(**values)
    )


async def sweep_stale(session: AsyncSession, *, stale_minutes: int) -> int:
    """Fail `running` jobs whose worker stopped heartbeating. Returns the count.

    This is what makes a killed worker recoverable: failing the job releases
    `ux_ingest_jobs_active_document`, so the document can be queued again.
    """
    result = await session.execute(
        text(
            """
            UPDATE ingest_jobs
               SET status      = :failed,
                   finished_at = now(),
                   error       = COALESCE(error,
                                 'worker stopped heartbeating; swept as stale')
             WHERE status = :running
               AND heartbeat_at < now() - make_interval(mins => :mins)
            """
        ),
        {"failed": JOB_FAILED, "running": JOB_RUNNING, "mins": stale_minutes},
    )
    return result.rowcount or 0

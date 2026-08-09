"""Ingest job queue against real Postgres. Skips if the DB is unreachable.

Throwaway NullPool engine per call — the app's module-level engine pools
connections bound to the first event loop (see CLAUDE.md).
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import jobs
from app.rag.models import JOB_FAILED, JOB_RUNNING, JOB_SUCCEEDED


def _run(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _sql(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def docs():
    """A department with two documents; everything removed afterwards."""
    _skip_if_no_db()
    tag = uuid.uuid4().hex[:8]
    ids = [uuid.uuid4().hex, uuid.uuid4().hex]

    async def setup(conn):
        dept = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'J') RETURNING id"),
            {"c": f"jobs{tag}"})).scalar_one()
        for n, doc_id in enumerate(ids):
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'pending')"),
                {"i": doc_id, "d": dept, "h": f"{n}" * 64})
        return dept

    dept = _sql(setup)
    yield {"dept": dept, "a": ids[0], "b": ids[1]}

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id = :d"),
                           {"d": dept})
        await conn.execute(text("DELETE FROM departments WHERE id = :d"), {"d": dept})
    _sql(teardown)


def test_enqueue_creates_a_queued_job(docs):
    async def go(s):
        job = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        return job.status, job.document_id

    status, doc_id = _run(go)
    assert status == "queued" and doc_id == docs["a"]


def test_a_second_active_job_for_one_document_is_a_conflict(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        with pytest.raises(jobs.JobConflict):
            await jobs.enqueue(s, document_id=docs["a"])
            await s.commit()
        return True

    assert _run(go) is True


def test_a_finished_job_does_not_block_a_new_one(docs):
    async def go(s):
        first = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        await jobs.finish(s, first.id, status=JOB_SUCCEEDED)
        await s.commit()
        second = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        return second.status

    assert _run(go) == "queued"


def test_claim_marks_running_and_increments_attempts(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        claimed = await jobs.claim_next(s)
        await s.commit()
        return claimed.status, claimed.attempts, claimed.started_at is not None

    status, attempts, started = _run(go)
    assert status == JOB_RUNNING and attempts == 1 and started


def test_claim_returns_none_when_the_queue_is_empty(docs):
    async def go(s):
        # Drain anything an earlier test left queued, then confirm empty.
        while await jobs.claim_next(s):
            await s.commit()
        await s.commit()
        return await jobs.claim_next(s)

    assert _run(go) is None


def test_set_chunks_total_records_the_progress_denominator(docs):
    async def go(s):
        job = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        await jobs.set_chunks_total(s, job.id, 42)
        await s.commit()
        return (await jobs.get_job(s, job.id)).chunks_total

    assert _run(go) == 42


def test_claim_is_fifo(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        await jobs.enqueue(s, document_id=docs["b"])
        await s.commit()
        first = await jobs.claim_next(s)
        await s.commit()
        return first.document_id

    assert _run(go) == docs["a"]


def test_two_concurrent_workers_never_claim_the_same_job(docs):
    """SKIP LOCKED: one gets the job, the other gets the next one or None."""
    async def go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await jobs.enqueue(s, document_id=docs["a"])
                await s.commit()

            async def worker():
                async with Session() as s:
                    claimed = await jobs.claim_next(s)
                    await s.commit()
                    return claimed.id if claimed else None

            return await asyncio.gather(worker(), worker())
        finally:
            await engine.dispose()

    a, b = asyncio.run(go())
    assert {a, b} != {None}                    # somebody got it
    assert a is None or b is None or a != b    # never the same row twice


def test_heartbeat_advances(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        before = job.heartbeat_at
        await jobs.heartbeat(s, job.id)
        await s.commit()
        after = (await jobs.get_job(s, job.id)).heartbeat_at
        return before, after

    before, after = _run(go)
    assert after is not None and (before is None or after >= before)


def test_finish_records_failure_and_the_error_text(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await jobs.finish(s, job.id, status=JOB_FAILED, error="parse blew up")
        await s.commit()
        done = await jobs.get_job(s, job.id)
        return done.status, done.error, done.finished_at is not None

    status, error, finished = _run(go)
    assert status == JOB_FAILED and "blew up" in error and finished


def test_sweep_fails_a_stale_running_job(docs):
    """A worker that died mid-job must not hold the document forever."""
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await s.execute(text(
            "UPDATE ingest_jobs SET heartbeat_at = now() - interval '1 hour'"
            " WHERE id = :i"), {"i": job.id})
        await s.commit()
        swept = await jobs.sweep_stale(s, stale_minutes=10)
        await s.commit()
        return swept, (await jobs.get_job(s, job.id)).status

    swept, status = _run(go)
    assert swept == 1 and status == JOB_FAILED


def test_sweep_leaves_a_live_job_alone(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await jobs.heartbeat(s, job.id)
        await s.commit()
        swept = await jobs.sweep_stale(s, stale_minutes=10)
        await s.commit()
        return swept, (await jobs.get_job(s, job.id)).status

    swept, status = _run(go)
    assert swept == 0 and status == JOB_RUNNING


def test_a_swept_job_frees_the_document_for_a_retry(docs):
    """The whole point of the sweep — the partial unique index must let go."""
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await s.execute(text(
            "UPDATE ingest_jobs SET heartbeat_at = now() - interval '1 hour'"
            " WHERE id = :i"), {"i": job.id})
        await s.commit()
        await jobs.sweep_stale(s, stale_minutes=10)
        await s.commit()
        retry = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        return retry.status

    assert _run(go) == "queued"

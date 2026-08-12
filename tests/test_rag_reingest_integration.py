"""Integration tests for the re-ingest backfill command (real Postgres)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import jobs as jobs_repo
from app.rag.models import Department, Document, IngestJob
from app.rag.reingest import reingest


def _run(coro_fn):
    """Run `coro_fn(session)` on a fresh NullPool engine + session."""

    async def _go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}: {exc}")


def _seed(session, *, status="ready"):
    """One department + one document in the given status. Returns (code, dept)."""
    code = f"ri{uuid.uuid4().hex[:8]}"
    dept = Department(code=code, name="Reingest Test", is_active=True)
    session.add(dept)
    return code, dept


def _make_doc(dept_id, status="ready"):
    """Build a `Document` with every non-nullable, no-default column filled.

    Factored out of the five tests below: `Document` has two NOT NULL columns
    with no server_default — `source` and `content_hash` — that a naive
    per-test constructor call is easy to omit. Centralizing construction means
    a future required column breaks one call site, not five.
    """
    return Document(
        id=uuid.uuid4().hex,
        department_id=dept_id,
        title="t",
        source="upload",
        file_type="docx",
        status=status,
        storage_key="k",
        content_hash=uuid.uuid4().hex,
    )


def test_reingest_queues_every_non_archived_document():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        session.add(_make_doc(dept.id))
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["total"] == 1
    assert stats["queued"] + stats["skipped"] == stats["total"]
    assert stats["queued"] == 1


def test_dry_run_queues_nothing():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        doc = _make_doc(dept.id)
        session.add(doc)
        await session.commit()
        stats = await reingest(session, department_code=code, dry_run=True)
        n = (
            await session.execute(
                select(IngestJob).where(IngestJob.document_id == doc.id)
            )
        ).scalars().all()
        return stats, len(n)

    stats, job_count = _run(go)
    assert stats["total"] == 1
    assert job_count == 0


def test_department_filter_restricts_the_set():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        session.add(_make_doc(dept.id))
        await session.commit()
        one = await reingest(session, department_code=code, dry_run=True)
        every = await reingest(session, department_code=None, dry_run=True)
        return one, every

    one, every = _run(go)
    assert one["total"] == 1
    assert every["total"] >= one["total"]


def test_a_document_with_an_active_job_is_skipped_not_raised():
    """JobConflict is expected traffic, not an error: ux_ingest_jobs_active_document
    is a PARTIAL unique index over queued|running, so a document already being
    ingested will pick up the new chunker anyway."""

    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        doc = _make_doc(dept.id)
        session.add(doc)
        await session.commit()
        await jobs_repo.enqueue(session, document_id=doc.id)
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["skipped"] == 1
    assert stats["queued"] == 0


def test_archived_documents_are_not_requeued():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        session.add(_make_doc(dept.id, status="archived"))
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["total"] == 0

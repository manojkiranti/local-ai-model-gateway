"""Integration tests for the re-ingest backfill command (real Postgres)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
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


def _seed(session):
    """One department with a random code. Returns (code, dept)."""
    code = f"ri{uuid.uuid4().hex[:8]}"
    dept = Department(code=code, name="Reingest Test", is_active=True)
    session.add(dept)
    return code, dept


def _make_doc(dept_id, status="ready", doc_id=None):
    """Build a `Document` with every non-nullable, no-default column filled.

    Factored out of the tests below: `Document` has two NOT NULL columns
    with no server_default — `source` and `content_hash` — that a naive
    per-test constructor call is easy to omit. Centralizing construction means
    a future required column breaks one call site, not several.

    `doc_id` is settable so a test can control scan order: `reingest` orders
    its query by `Document.id`, so a test that needs a specific document to be
    scanned before another passes explicit, pre-sorted ids.
    """
    return Document(
        id=doc_id or uuid.uuid4().hex,
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
    """A department-scoped call must exclude a document that belongs to a
    DIFFERENT department, not merely return a total that happens to be no
    larger than the unscoped one (`>=` holds even for a no-op filter)."""

    async def go(session):
        code_a, dept_a = _seed(session)
        _, dept_b = _seed(session)
        await session.flush()
        doc_a = _make_doc(dept_a.id)
        doc_b = _make_doc(dept_b.id)
        session.add_all([doc_a, doc_b])
        await session.commit()

        scoped = await reingest(session, department_code=code_a, dry_run=False)
        jobs_for_b = (
            await session.execute(
                select(IngestJob).where(IngestJob.document_id == doc_b.id)
            )
        ).scalars().all()
        every = await reingest(session, department_code=None, dry_run=True)
        return scoped, len(jobs_for_b), every

    scoped, jobs_for_b_count, every = _run(go)
    assert scoped["total"] == 1
    assert scoped["queued"] == 1
    # Dept B's document was never enqueued by the dept-A-scoped call.
    assert jobs_for_b_count == 0
    # The unscoped call sees both departments' documents.
    assert every["total"] == 2
    assert every["total"] > scoped["total"]


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


def test_a_conflict_does_not_poison_the_rest_of_the_scan():
    """Regression: `jobs_repo.enqueue` calls `session.rollback()` on an
    IntegrityError, and `Session.rollback()` expires every persistent object
    in the session (independent of `expire_on_commit`, which only governs
    `commit()`). If `reingest` held onto ORM `Document` instances across that
    rollback instead of plain values, the NEXT iteration's attribute access
    (`doc.id`) would trigger an implicit synchronous refresh, which raises
    `MissingGreenlet` under an `AsyncSession` — the whole run would die
    without returning stats.

    Two documents, ids chosen so the conflicted one is scanned FIRST (the
    query orders by `Document.id`): that guarantees there is a "next"
    document whose attribute access would have poisoned the old code. Seeding
    only one document (as in the sibling conflict test above) can't exercise
    this — there is no next document to poison."""

    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        first_id, second_id = sorted([uuid.uuid4().hex, uuid.uuid4().hex])
        conflicted = _make_doc(dept.id, doc_id=first_id)
        clean = _make_doc(dept.id, doc_id=second_id)
        session.add_all([conflicted, clean])
        await session.commit()
        await jobs_repo.enqueue(session, document_id=conflicted.id)
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["total"] == 2
    assert stats["skipped"] == 1
    assert stats["queued"] == 1


def test_archived_documents_are_not_requeued():
    async def go(session):
        code, dept = _seed(session)
        await session.flush()
        session.add(_make_doc(dept.id, status="archived"))
        await session.commit()
        return await reingest(session, department_code=code, dry_run=False)

    stats = _run(go)
    assert stats["total"] == 0

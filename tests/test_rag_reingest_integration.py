"""Integration tests for the re-ingest backfill command (real Postgres)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError, OperationalError
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
    except (OperationalError, InterfaceError, OSError) as exc:
        # ONLY a genuine connection failure skips. A blanket `except Exception`
        # here used to relabel every error "Postgres unreachable", which meant
        # `test_a_conflict_does_not_poison_the_rest_of_the_scan` — whose entire
        # job is to catch a MissingGreenlet in `reingest` — could not fail. It
        # skipped instead, and the bug it guards shipped for exactly that reason.
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}: {exc}")


# Every department `_seed` creates, so `_cleanup` can remove it again. Without
# this the file leaked a department + 2 documents + their ingest_jobs per test,
# per run: 125 stale `running` jobs had accumulated in one day, and because
# `worker.run_once` claims one job per call, that backlog starved the 50-call
# drain loop in tests/test_rag_ingest_e2e.py and failed it in full-suite runs
# only. A test that leaves rows behind eventually breaks a different test.
_SEEDED_CODES: list[str] = []


def _seed(session):
    """One department with a random code. Returns (code, dept)."""
    code = f"ri{uuid.uuid4().hex[:8]}"
    _SEEDED_CODES.append(code)
    dept = Department(code=code, name="Reingest Test", is_active=True)
    session.add(dept)
    return code, dept


@pytest.fixture(autouse=True)
def _cleanup():
    """Delete this test's departments afterwards, and their documents/jobs with
    them. `ingest_jobs.document_id` and `document_chunks` both cascade from
    `documents`, but `documents.department_id` is ON DELETE RESTRICT, so the
    documents must go first and the department cannot simply be dropped."""
    _SEEDED_CODES.clear()
    yield
    codes = list(_SEEDED_CODES)
    _SEEDED_CODES.clear()
    if not codes:
        return

    async def _go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM documents WHERE department_id IN"
                        " (SELECT id FROM departments WHERE code = ANY(:c))"
                    ),
                    {"c": codes},
                )
                await conn.execute(
                    text("DELETE FROM departments WHERE code = ANY(:c)"),
                    {"c": codes},
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(_go())
    except (OperationalError, InterfaceError, OSError):
        # The test already skipped for the same reason; nothing to clean.
        pass


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
    larger than the unscoped one (`>=` holds even for a no-op filter).

    The unscoped total is asserted as a DELTA around the seeding, never as an
    absolute. `reingest(department_code=None)` counts the whole table, so
    `== 2` only ever held on an empty database and failed on any developer
    database with real corpus data in it.

    Scoping to B as well as A is what actually proves the filter discriminates:
    each department sees exactly its own document, so the predicate can be
    neither a no-op nor accidentally pinned to one department.
    """

    async def go(session):
        # Before seeding, because this database is not empty.
        baseline = await reingest(session, department_code=None, dry_run=True)

        code_a, dept_a = _seed(session)
        code_b, dept_b = _seed(session)
        await session.flush()
        doc_a = _make_doc(dept_a.id)
        doc_b = _make_doc(dept_b.id)
        session.add_all([doc_a, doc_b])
        await session.commit()

        scoped_a = await reingest(session, department_code=code_a, dry_run=False)
        jobs_for_b = (
            await session.execute(
                select(IngestJob).where(IngestJob.document_id == doc_b.id)
            )
        ).scalars().all()
        # Dry run: proves B's document is selectable without enqueuing it, so the
        # assertion above about B having no job stays true.
        scoped_b = await reingest(session, department_code=code_b, dry_run=True)
        every = await reingest(session, department_code=None, dry_run=True)
        return scoped_a, len(jobs_for_b), scoped_b, every, baseline

    scoped_a, jobs_for_b_count, scoped_b, every, baseline = _run(go)
    assert scoped_a["total"] == 1
    assert scoped_a["queued"] == 1
    # Dept B's document was never enqueued by the dept-A-scoped call.
    assert jobs_for_b_count == 0
    # ...and B's own scope sees exactly B's document, so the filter is neither a
    # no-op nor pinned to a single department.
    assert scoped_b["total"] == 1
    # The unscoped call sees BOTH new documents, whatever else the database holds.
    assert every["total"] - baseline["total"] == 2
    assert every["total"] > scoped_a["total"]


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

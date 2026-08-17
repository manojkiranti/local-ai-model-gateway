"""Phase 7 step 4 — the shared NRB pipeline runner, against real Postgres.

WHAT IS STUBBED AND WHAT IS NOT
    The three upstream stages are stubbed: `run_sync` talks to a central bank's
    website, `run_fetch` downloads gigabytes and `run_extract` parses hundreds of
    documents. Their own suites already cover them, and the question here is
    ORCHESTRATION — order, durability, locking, job association and status.

    Everything below the stub is real: the run rows, the RAG stage
    (`corpus.run_rag_enqueue` against a real catalog fixture), the job
    association table and the status arithmetic. Recovery and embedding never
    run because nothing drains the queue — which is exactly the production
    shape, since the worker is a separate process.

ISOLATION
    One connection, one outer transaction always rolled back, sessions joined
    with `create_savepoint` so the runner's own commits become savepoint
    releases. The advisory lock is taken on the ENGINE, i.e. a different
    connection, which is what makes the locking tests real rather than a
    simulation. Rows are scoped to a test-only department so the scratch
    database's unrelated `ri*` debris (§20.7 item 4) cannot affect an assertion —
    a property one test asserts directly.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.nrb import corpus, pipeline
from app.nrb.locks import PIPELINE_LOCK_KEY, advisory_lock
from app.nrb.models import (
    PIPELINE_AWAITING,
    PIPELINE_FAILED,
    PIPELINE_PARTIAL,
    PIPELINE_RUNNING,
    PIPELINE_SUCCEEDED,
)
from app.rag import repository as dept_repo

DEPT_CODE = "test-nrb-pipeline"
NRB_TABLES = ("nrb_source_files", "nrb_sources", "nrb_files")


def _engine():
    return create_async_engine(get_settings().database_url, poolclass=NullPool)


def _skip_if_no_db() -> None:
    async def probe():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


def _run(fn):
    """`fn(session, Session, engine)` on a clean slate, then roll everything back.

    The engine is handed over as well because the advisory lock deliberately
    lives on its own connection (`locks.py`) — a lock taken on the test's
    savepoint session would be released at the first commit and prove nothing.
    """
    _skip_if_no_db()

    async def main():
        engine = _engine()
        try:
            async with engine.connect() as connection:
                outer = await connection.begin()
                Session = async_sessionmaker(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                )
                session = AsyncSession(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                )
                try:
                    for table in NRB_TABLES:
                        await session.execute(text(f"DELETE FROM {table}"))
                    # `nrb_pipeline_runs` is NOT cleared: the scratch database
                    # holds real runs from live exercises, and every assertion
                    # below names the ids it created rather than assuming the
                    # table is empty. (`sweep_abandoned` is unqualified by
                    # design; inside this rolled-back transaction that is
                    # harmless.)
                    for statement in (
                        "DELETE FROM document_chunks WHERE department_id IN "
                        "(SELECT id FROM departments WHERE code = :c)",
                        "DELETE FROM ingest_jobs WHERE document_id IN "
                        "(SELECT d.id FROM documents d JOIN departments dp "
                        " ON dp.id = d.department_id WHERE dp.code = :c)",
                        "DELETE FROM documents WHERE department_id IN "
                        "(SELECT id FROM departments WHERE code = :c)",
                        "DELETE FROM departments WHERE code = :c",
                    ):
                        await session.execute(text(statement), {"c": DEPT_CODE})
                    await session.commit()
                    return await fn(session, Session, engine)
                finally:
                    await session.close()
                    await outer.rollback()
        finally:
            await engine.dispose()

    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# Stubs for the three upstream stages.
# --------------------------------------------------------------------------- #
@dataclass
class _StageResult:
    counters: dict[str, Any] = field(default_factory=dict)


def _stub_stages(monkeypatch, calls: list[str], *, fail: str | None = None,
                 counters: dict[str, dict] | None = None):
    """Replace sync/fetch/extract, recording the order they are called in."""
    counters = counters or {}

    def make(name):
        async def stage(**kwargs):
            calls.append(name)
            if fail == name:
                raise RuntimeError(f"{name} exploded")
            return _StageResult(counters.get(name, {}))
        return stage

    from app.nrb import extract as extract_mod
    from app.nrb import fetch as fetch_mod
    from app.nrb import sync as sync_mod

    monkeypatch.setattr(sync_mod, "run_sync", make("sync"))
    monkeypatch.setattr(fetch_mod, "run_fetch", make("fetch"))
    monkeypatch.setattr(extract_mod, "run_extract", make("extract"))


def _patch_store(monkeypatch, tmp):
    monkeypatch.setattr(
        corpus.filestore, "resolve_path", lambda key, base=None: tmp / key
    )


async def _department(session):
    dept = await dept_repo.create_department(
        session, code=DEPT_CODE, name="Phase 7 pipeline test"
    )
    await session.flush()
    return dept


async def _blob(session, tmp, body: bytes, *, key: str) -> str:
    sha = hashlib.sha256(body).hexdigest()
    storage_key = f"{sha[:2]}/{sha}.pdf"
    path = tmp / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    await session.execute(
        text(
            """
            INSERT INTO nrb_files (comparison_key, source_url, filename, extension,
                resource_type, type_source, host, fetch_status, content_sha256,
                content_length, storage_key)
            VALUES (:k, :k, 'f.pdf', 'pdf', 'document', 'extension',
                    'www.nrb.org.np', 'fetched', :sha, :len, :store)
            """
        ),
        {"k": key, "sha": sha, "len": len(body), "store": storage_key},
    )
    await session.flush()
    return sha


async def _set_jobs(session, run_id: int, status: str, *, limit: int | None = None):
    """Move this run's jobs to a terminal status, as the worker would."""
    await session.execute(
        text(
            "UPDATE ingest_jobs SET status = :s, finished_at = now() "
            " WHERE id IN (SELECT job_id FROM nrb_pipeline_run_jobs "
            "               WHERE run_id = :r"
            + (" LIMIT :n" if limit else "") + ")"
        ),
        {"s": status, "r": run_id, **({"n": limit} if limit else {})},
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# 4-5. Orchestration order, and the second run being free.
# --------------------------------------------------------------------------- #
def test_one_run_orchestrates_the_stages_in_order(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls, counters={"fetch": {"downloaded": 2}})
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()

        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        return run, calls

    run, calls = _run(body)
    assert calls == ["sync", "fetch", "extract"]     # rag is not stubbed
    assert run.status == PIPELINE_AWAITING           # it queued a document
    assert run.stage == "waiting"
    assert run.counters["fetch"] == {"downloaded": 2}
    assert run.counters["rag"]["created"] == 1
    assert run.counters["rag"]["new_source"] == 1
    assert run.jobs == {"queued": 1}


def test_a_second_unchanged_run_does_no_upstream_work_and_queues_nothing(
    tmp_path, monkeypatch
):
    """The idempotence the whole design rests on, at the orchestration level.

    The upstream stages are still CALLED — they are the thing that discovers
    change, and each is internally idempotent — but the RAG stage classifies
    every blob `already_current` and enqueues nothing, so the run settles
    immediately instead of waiting on a worker.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        scope = pipeline.PipelineScope(department=DEPT_CODE, limit=10)

        first = await pipeline.start(scope, engine=engine, session_factory=Session)
        # The worker drains the first pass before the second is triggered. That
        # ordering is not incidental: `awaiting_jobs` is an active update and a
        # second trigger during it gets `PipelineBusy` (asserted separately).
        await _set_jobs(session, first.id, "succeeded")
        await session.commit()
        second = await pipeline.start(scope, engine=engine, session_factory=Session)
        return first, second

    first, second = _run(body)
    assert first.counters["rag"]["created"] == 1
    assert second.counters["rag"] == {
        **second.counters["rag"], "created": 0, "already_current": 1, "queued": 0
    }
    assert second.status == PIPELINE_SUCCEEDED   # nothing to wait for
    assert second.jobs == {}


# --------------------------------------------------------------------------- #
# 6, 13. Exclusion, and recovering from a crash.
# --------------------------------------------------------------------------- #
def test_a_second_orchestrator_does_not_start_duplicate_work(tmp_path, monkeypatch):
    """A real advisory lock on a real second connection, not a simulated one.

    The second trigger gets the run in progress rather than an error page: that
    is what an admin endpoint wants to return, and it is why `PipelineBusy`
    carries the run.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await session.execute(
            text(
                "INSERT INTO nrb_pipeline_runs (trigger, status, stage, department, "
                " scope, counters, started_at) "
                "VALUES ('cli', 'running', 'fetch', :d, '{}'::jsonb, '{}'::jsonb, now())"
            ),
            {"d": DEPT_CODE},
        )
        await session.commit()

        async with advisory_lock(engine, PIPELINE_LOCK_KEY, what="test"):
            with pytest.raises(pipeline.PipelineBusy) as excinfo:
                await pipeline.start(
                    pipeline.PipelineScope(department=DEPT_CODE, limit=10),
                    engine=engine, session_factory=Session,
                )
        return excinfo.value, calls

    busy, calls = _run(body)
    assert calls == []                       # no stage ran at all
    assert busy.run is not None
    assert busy.run.status == PIPELINE_RUNNING
    assert busy.run.stage == "fetch"


def test_a_run_abandoned_by_a_dead_orchestrator_is_swept_by_the_next_one(
    tmp_path, monkeypatch
):
    """Holding the lock is proof no orchestrator is alive, so no timeout is needed.

    A run left `running` by a killed process would otherwise sit there forever,
    make a status view lie and — now that `awaiting_jobs` and `running` both
    block a new trigger — wedge the pipeline permanently. The sweep happens
    AFTER the lock is obtained and before the active-run gate, which is what
    makes a crashed orchestrator recoverable rather than fatal.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        planted = (
            await session.execute(
                text(
                    "INSERT INTO nrb_pipeline_runs (trigger, status, stage, "
                    " scope, counters, started_at) VALUES "
                    "('cli', 'running', 'fetch', '{}'::jsonb, '{}'::jsonb, now()) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await session.commit()

        fresh = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10, stages=("sync",)),
            engine=engine, session_factory=Session,
        )
        # Asserted by id, never by row order: the scratch database carries real
        # pipeline runs from live exercises, and `ORDER BY id LIMIT` would be an
        # assertion about those instead.
        rows = dict(
            (
                await session.execute(
                    text("SELECT id, status FROM nrb_pipeline_runs WHERE id = ANY(:i)"),
                    {"i": [planted, fresh.id]},
                )
            ).all()
        )
        error = (
            await session.execute(
                text("SELECT error FROM nrb_pipeline_runs WHERE id = :i"),
                {"i": planted},
            )
        ).scalar_one()
        return planted, fresh, rows, error

    planted, fresh, rows, error = _run(body)
    assert rows[planted] == PIPELINE_FAILED
    assert "did not finish" in error
    assert rows[fresh.id] == PIPELINE_SUCCEEDED   # the crash did not wedge us


# --------------------------------------------------------------------------- #
# 7-11. Durable status, job association, and the terminal verdict.
# --------------------------------------------------------------------------- #
def test_the_run_row_persists_its_scope_stage_and_counters(tmp_path, monkeypatch):
    _stub_stages(monkeypatch, [], counters={"sync": {"sources_created": 3}})
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        run = await pipeline.start(
            pipeline.PipelineScope(
                department=DEPT_CODE, sections=("circulars",), limit=5,
                retry_failed=True,
            ),
            trigger="api", requested_by="admin@example.com",
            engine=engine, session_factory=Session,
        )
        async with Session() as s:
            reread = await pipeline.get_run(s, run.id)
            await s.rollback()
        return reread

    run = _run(body)
    assert run.trigger == "api" and run.requested_by == "admin@example.com"
    assert run.scope["sections"] == ["circulars"]
    assert run.scope["limit"] == 5 and run.scope["retry_failed"] is True
    assert run.counters["sync"] == {"sources_created": 3}
    assert run.started_at is not None


def test_a_run_counts_only_its_own_jobs(tmp_path, monkeypatch):
    """Explicitly, by id — never by a time window.

    An unrelated job created in the same second (the scratch database is full of
    them) must not be adopted. This is the reason `nrb_pipeline_run_jobs` exists
    rather than a `created_at BETWEEN` query.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        dept = await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()

        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )

        # A stranger's document and job, created right now in the same department.
        from app.rag import documents as docs_repo
        from app.rag import jobs as jobs_repo

        other = await docs_repo.create_document(
            session, department_id=dept.id, title="not ours", source="upload",
            file_type="pdf", content_hash=hashlib.sha256(b"other").hexdigest(),
            storage_key="x/y.pdf",
        )
        await jobs_repo.enqueue(session, document_id=other.id)
        await session.commit()

        await _set_jobs(session, run.id, "failed")
        await session.commit()
        async with Session() as s:
            view = await pipeline.reconcile(s, run.id)
            await s.commit()
        return view

    view = _run(body)
    assert view.jobs == {"failed": 1}       # one job, not two
    assert view.status == PIPELINE_FAILED


def test_a_run_stays_waiting_while_any_of_its_jobs_is_unfinished(
    tmp_path, monkeypatch
):
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        for i in range(2):
            await _blob(session, tmp_path, f"b{i}".encode(),
                        key=f"https://www.nrb.org.np/{i}.pdf")
        await session.commit()

        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        await _set_jobs(session, run.id, "succeeded", limit=1)
        await session.commit()
        async with Session() as s:
            waiting = await pipeline.reconcile(s, run.id)
            await s.commit()

        await _set_jobs(session, run.id, "succeeded")
        await session.commit()
        async with Session() as s:
            done = await pipeline.reconcile(s, run.id)
            await s.commit()
        return waiting, done

    waiting, done = _run(body)
    assert waiting.status == PIPELINE_AWAITING
    assert waiting.jobs == {"queued": 1, "succeeded": 1}
    assert done.status == PIPELINE_SUCCEEDED
    assert done.finished_at is not None


def test_a_mix_of_succeeded_and_failed_jobs_makes_the_run_partial(
    tmp_path, monkeypatch
):
    """`partial` is a real outcome, not a hedge.

    One unparseable OLE2 file among four good circulars is a successful update
    with a recorded gap — reporting the whole run `failed` would hide that the
    other three are indexed and searchable.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        for i in range(3):
            await _blob(session, tmp_path, f"b{i}".encode(),
                        key=f"https://www.nrb.org.np/{i}.pdf")
        await session.commit()
        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        await _set_jobs(session, run.id, "failed", limit=1)
        await _set_jobs(session, run.id, "succeeded")
        await session.execute(
            text(
                "UPDATE ingest_jobs SET status = 'failed' WHERE id = "
                "(SELECT job_id FROM nrb_pipeline_run_jobs WHERE run_id = :r LIMIT 1)"
            ),
            {"r": run.id},
        )
        await session.commit()
        async with Session() as s:
            view = await pipeline.reconcile(s, run.id)
            await s.commit()
        return view

    view = _run(body)
    assert view.status == PIPELINE_PARTIAL
    assert view.jobs == {"succeeded": 2, "failed": 1}


def test_reconciling_a_terminal_run_is_a_no_op():
    """Idempotent, and it never rewrites `finished_at`.

    A status endpoint will call this on every poll; a run that keeps moving its
    own completion time would make "how long did the update take" unanswerable.
    """
    async def body(session, Session, engine):
        await session.execute(
            text(
                "INSERT INTO nrb_pipeline_runs (trigger, status, stage, scope, "
                " counters, started_at, finished_at) VALUES "
                "('cli', 'succeeded', 'done', '{}'::jsonb, '{}'::jsonb, now(), now())"
            )
        )
        await session.commit()
        run_id = (
            await session.execute(
                text("SELECT id FROM nrb_pipeline_runs ORDER BY id DESC LIMIT 1")
            )
        ).scalar_one()
        async with Session() as s:
            first = await pipeline.reconcile(s, run_id)
            await s.commit()
        async with Session() as s:
            second = await pipeline.reconcile(s, run_id)
            await s.commit()
        return first, second

    first, second = _run(body)
    assert first.status == second.status == PIPELINE_SUCCEEDED
    assert first.finished_at == second.finished_at


# --------------------------------------------------------------------------- #
# 12. A stage that raises.
# --------------------------------------------------------------------------- #
def test_a_stage_failure_records_the_run_failed_and_stops(tmp_path, monkeypatch):
    """NRB being unreachable must not touch the searchable corpus.

    The run records what happened and stops; later stages would be acting on a
    corpus state the failed stage was supposed to establish. Nothing is
    archived, nothing is deleted, no chunk is written — this whole module only
    ever creates and enqueues.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls, fail="sync")
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        docs = (
            await session.execute(
                text("SELECT count(*) FROM documents d JOIN departments dp "
                     " ON dp.id = d.department_id WHERE dp.code = :c"),
                {"c": DEPT_CODE},
            )
        ).scalar_one()
        return run, calls, docs

    run, calls, docs = _run(body)
    assert calls == ["sync"]                     # fetch and extract never ran
    assert run.status == PIPELINE_FAILED
    assert "sync exploded" in run.error
    assert run.finished_at is not None
    assert docs == 0                             # nothing was staged


def test_the_rag_stage_needs_a_department_and_says_so(tmp_path, monkeypatch):
    _stub_stages(monkeypatch, [])

    async def body(session, Session, engine):
        return await pipeline.start(
            pipeline.PipelineScope(department=None, limit=10),
            engine=engine, session_factory=Session,
        )

    run = _run(body)
    assert run.status == PIPELINE_FAILED
    assert "needs a department" in run.error


# --------------------------------------------------------------------------- #
# 14-15. Retry is opt-in, and is not a recovery refresh.
# --------------------------------------------------------------------------- #
def test_a_normal_run_does_not_retry_a_failed_document(tmp_path, monkeypatch):
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        await session.execute(
            text("UPDATE documents SET status = 'failed' WHERE department_id IN "
                 "(SELECT id FROM departments WHERE code = :c)"),
            {"c": DEPT_CODE},
        )
        await _set_jobs(session, run.id, "failed")
        await session.commit()

        again = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        return again

    again = _run(body)
    assert again.counters["rag"]["retry_failed"] == 0
    assert again.counters["rag"]["queued"] == 0
    assert again.status == PIPELINE_SUCCEEDED


def test_retry_failed_true_requeues_the_failed_document(tmp_path, monkeypatch):
    """The same Phase 7 step 1.1 behaviour, reached through the runner.

    And it is NOT a recovery refresh: an unresolved recovery stays cached, so a
    retry that re-runs the ingest does not re-run OCR on a page the pipeline has
    already decided it cannot read. Purging that is a separate, explicit command.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        await session.execute(
            text("UPDATE documents SET status = 'failed' WHERE department_id IN "
                 "(SELECT id FROM departments WHERE code = :c)"),
            {"c": DEPT_CODE},
        )
        await _set_jobs(session, run.id, "failed")
        await session.commit()

        again = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10, retry_failed=True),
            engine=engine, session_factory=Session,
        )
        rows = (
            await session.execute(
                text("SELECT reason, count(*) FROM nrb_pipeline_run_jobs "
                     " WHERE run_id = :r GROUP BY 1"),
                {"r": again.id},
            )
        ).all()
        return again, rows

    again, rows = _run(body)
    assert again.counters["rag"]["retry_failed"] == 1
    assert again.status == PIPELINE_AWAITING
    assert rows == [("retried", 1)]          # a retried job, not a created one


# --------------------------------------------------------------------------- #
# 16. Everything else is untouched.
# --------------------------------------------------------------------------- #
def test_the_generic_rag_flow_is_untouched_by_the_runner():
    """The runner adds a table and a service; it changes no generic semantics.

    `ingest_jobs` gained no column, `documents` gained no column, and the
    association lives in an NRB-owned table — so an ordinary department upload
    behaves exactly as it did and is invisible to every run.
    """
    from app.rag.models import Document, IngestJob

    assert not hasattr(IngestJob, "pipeline_run_id")
    assert not hasattr(Document, "pipeline_run_id")
    assert "nrb" not in {c.name for c in IngestJob.__table__.columns}

    async def body(session, Session, engine):
        dept = await _department(session)
        from app.rag import documents as docs_repo
        from app.rag import jobs as jobs_repo

        doc = await docs_repo.create_document(
            session, department_id=dept.id, title="ordinary upload",
            source="upload", file_type="pdf",
            content_hash=hashlib.sha256(b"plain").hexdigest(),
            storage_key="x/y.pdf",
        )
        job = await jobs_repo.enqueue(session, document_id=doc.id)
        await session.commit()
        linked = (
            await session.execute(
                text("SELECT count(*) FROM nrb_pipeline_run_jobs WHERE job_id = :j"),
                {"j": job.id},
            )
        ).scalar_one()
        return linked

    assert _run(body) == 0


# --------------------------------------------------------------------------- #
# The status arithmetic, without a database.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "jobs,counters,expected",
    [
        ({}, {}, PIPELINE_SUCCEEDED),                               # nothing to do
        ({"queued": 1}, {}, PIPELINE_AWAITING),
        ({"running": 1, "succeeded": 9}, {}, PIPELINE_AWAITING),
        ({"succeeded": 3}, {}, PIPELINE_SUCCEEDED),
        ({"failed": 3}, {}, PIPELINE_FAILED),
        ({"succeeded": 2, "failed": 1}, {}, PIPELINE_PARTIAL),
        # An item-level stage failure counts even when every job succeeded: a
        # fetch that lost one file did not fully update the corpus.
        ({"succeeded": 2}, {"fetch": {"failed": 1}}, PIPELINE_PARTIAL),
        ({}, {"extract": {"failed": 2}}, PIPELINE_FAILED),
    ],
)
def test_the_status_arithmetic(jobs, counters, expected):
    assert pipeline.resolve_status(counters, jobs) == expected


def test_waiting_beats_every_other_signal():
    """Order matters: one queued job means the run has not finished, whatever
    else went wrong. Reporting `partial` while work is still in flight would let
    a UI call an update done and then change its mind."""
    assert pipeline.resolve_status(
        {"fetch": {"failed": 5}}, {"queued": 1, "failed": 3}
    ) == PIPELINE_AWAITING


# --------------------------------------------------------------------------- #
# Lifecycle review (follow-up to §23): the two invariants a status API will
# depend on and that neither the lock nor the job table enforces on its own.
# --------------------------------------------------------------------------- #
def test_a_terminal_run_reports_what_happened_during_it_not_what_happened_after(
    tmp_path, monkeypatch
):
    """A finished run is HISTORY. Later work on the same documents cannot edit it.

    Run A queues a job, the job fails, Run A settles `failed`. Later a retry
    ingests the same document successfully. Run A must still say `failed` with
    one failed job — otherwise a status view would rewrite the past, and "which
    update broke the corpus" becomes unanswerable.

    Note what makes the naive version of this safe today and why it is not
    relied on: `jobs.enqueue` always INSERTs, so a retry creates a NEW job row
    rather than reviving the old one, and `claim_next`/`sweep_stale` only touch
    `queued`/`running` rows. The freeze holds the invariant HERE instead of
    borrowing it from `app/rag/jobs.py`.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        scope = pipeline.PipelineScope(department=DEPT_CODE, limit=10)

        run_a = await pipeline.start(scope, engine=engine, session_factory=Session)
        job_a = (
            await session.execute(
                text("SELECT job_id FROM nrb_pipeline_run_jobs WHERE run_id = :r"),
                {"r": run_a.id},
            )
        ).scalar_one()
        await _set_jobs(session, run_a.id, "failed")
        await session.execute(
            text("UPDATE documents SET status = 'failed' WHERE department_id IN "
                 "(SELECT id FROM departments WHERE code = :c)"),
            {"c": DEPT_CODE},
        )
        await session.commit()
        async with Session() as s:
            settled = await pipeline.reconcile(s, run_a.id)
            await s.commit()

        # Run B retries the same DOCUMENT and succeeds.
        run_b = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10, retry_failed=True),
            engine=engine, session_factory=Session,
        )
        await _set_jobs(session, run_b.id, "succeeded")
        await session.commit()
        async with Session() as s:
            b_view = await pipeline.reconcile(s, run_b.id)
            await s.commit()

        # ...and, belt and braces, the old job row is mutated directly, which is
        # the thing an in-place retry would do.
        await session.execute(
            text("UPDATE ingest_jobs SET status = 'succeeded' WHERE id = :i"),
            {"i": job_a},
        )
        await session.commit()

        async with Session() as s:
            after = await pipeline.get_run(s, run_a.id)
            again = await pipeline.reconcile(s, run_a.id)
            await s.commit()
        return settled, b_view, after, again

    settled, b_view, after, again = _run(body)
    assert settled.status == PIPELINE_FAILED and settled.jobs == {"failed": 1}
    assert b_view.status == PIPELINE_SUCCEEDED and b_view.jobs == {"succeeded": 1}
    # Run A, read twice, after the underlying job row was flipped to succeeded.
    assert after.status == PIPELINE_FAILED
    assert after.jobs == {"failed": 1}
    assert again.status == PIPELINE_FAILED
    assert again.jobs == {"failed": 1}
    assert after.finished_at == again.finished_at        # never rewritten


def test_a_run_that_goes_terminal_with_jobs_in_flight_freezes_them_as_they_were(
    tmp_path, monkeypatch
):
    """The one path that reaches the defect without an in-place retry.

    If anything raises AFTER `_record_jobs` has associated the jobs, the run is
    recorded `failed` while they are still `queued`. They then drain — and
    without the freeze the run would keep its `failed` status while its job
    counts drifted to `succeeded`, which is a self-contradictory row for a UI to
    render.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    real_mark = pipeline._mark_stage

    async def explode_after_recording(Session, run_id, stage, counters):
        if stage == "rag":
            raise RuntimeError("database blip after the jobs were recorded")
        return await real_mark(Session, run_id, stage, counters)

    monkeypatch.setattr(pipeline, "_mark_stage", explode_after_recording)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()

        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            engine=engine, session_factory=Session,
        )
        assert run.status == PIPELINE_FAILED
        assert run.jobs == {"queued": 1}          # frozen mid-flight, honestly

        # The worker drains it anyway — the job is real and was queued.
        await _set_jobs(session, run.id, "succeeded")
        await session.commit()
        async with Session() as s:
            after = await pipeline.get_run(s, run.id)
            await s.commit()
        return run, after

    run, after = _run(body)
    assert after.status == PIPELINE_FAILED
    assert after.jobs == {"queued": 1}            # NOT {"succeeded": 1}
    assert after.counters["jobs"] == {"queued": 1}


def test_a_waiting_run_blocks_a_second_trigger_and_is_not_swept(
    tmp_path, monkeypatch
):
    """`awaiting_jobs` is still an active NRB update.

    The advisory lock cannot express this: it is released the moment
    orchestration returns, while the jobs it queued outlive it by design. So the
    durable row is what refuses the second trigger, and the second trigger gets
    the WAITING run back — which is what an admin endpoint needs to return. It
    must also not be swept as abandoned; only `running` rows are.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        scope = pipeline.PipelineScope(department=DEPT_CODE, limit=10)

        first = await pipeline.start(scope, engine=engine, session_factory=Session)
        assert first.status == PIPELINE_AWAITING
        calls.clear()

        # No lock is held now — orchestration returned — and the trigger must
        # still be refused.
        with pytest.raises(pipeline.PipelineBusy) as excinfo:
            await pipeline.start(scope, engine=engine, session_factory=Session)

        still = (
            await session.execute(
                text("SELECT status FROM nrb_pipeline_runs WHERE id = :i"),
                {"i": first.id},
            )
        ).scalar_one()
        return first, excinfo.value, still, calls

    first, busy, still, calls = _run(body)
    assert calls == []                       # not one stage ran
    assert busy.run is not None
    assert busy.run.id == first.id
    assert busy.run.status == PIPELINE_AWAITING
    assert still == PIPELINE_AWAITING        # not swept, not failed


def test_a_waiting_run_whose_jobs_all_finished_does_not_wedge_the_pipeline(
    tmp_path, monkeypatch
):
    """The trap the blocking rule would otherwise set.

    `reconcile` only advances a run when somebody reads it. So a run whose jobs
    have all finished but which nobody polled would block every future trigger
    forever. `settle_waiting` runs inside the lock, immediately before the
    active-run gate, so a stale wait costs one query rather than an operator.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await _blob(session, tmp_path, b"two", key="https://www.nrb.org.np/b.pdf")
        await session.commit()

        first = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, keys=(
                "https://www.nrb.org.np/a.pdf",
            )),
            engine=engine, session_factory=Session,
        )
        assert first.status == PIPELINE_AWAITING
        # The worker finished, and NOBODY asked about the run.
        await _set_jobs(session, first.id, "succeeded")
        await session.commit()

        second = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, keys=(
                "https://www.nrb.org.np/b.pdf",
            )),
            engine=engine, session_factory=Session,
        )
        async with Session() as s:
            settled = await pipeline.get_run(s, first.id)
            await s.rollback()
        return second, settled

    second, settled = _run(body)
    assert settled.status == PIPELINE_SUCCEEDED     # settled by the next start
    assert settled.jobs == {"succeeded": 1}
    assert second.status == PIPELINE_AWAITING       # the new run really ran
    assert second.counters["rag"]["created"] == 1


def test_the_active_run_gate_reads_both_active_statuses():
    """Stated as a property, so a future edit to one status list is caught.

    `running` and `awaiting_jobs` are exactly the non-terminal statuses, and both
    mean "an NRB update is in progress". If a status were added to
    `PIPELINE_STATUSES` without deciding which side of this line it falls on, the
    gate would silently let a second orchestrator through.
    """
    from app.nrb.models import PIPELINE_ACTIVE_STATUSES, PIPELINE_QUEUED, PIPELINE_STATUSES

    active = {PIPELINE_QUEUED, PIPELINE_RUNNING, PIPELINE_AWAITING}
    terminal = {PIPELINE_SUCCEEDED, PIPELINE_PARTIAL, PIPELINE_FAILED}
    assert active | terminal == set(PIPELINE_STATUSES)
    assert not active & terminal
    # And the shared constant the gate, the sweep boundary and the singleton
    # index all read is exactly that active set — three places that must not
    # drift from each other.
    assert set(PIPELINE_ACTIVE_STATUSES) == active
    # And the run row's own CHECK agrees about which are unfinished.
    assert all(pipeline.resolve_status({}, {s: 1}) for s in ("queued", "running"))


# --------------------------------------------------------------------------- #
# Phase 7 step 6: admission is durable and orchestration left the request.
#
# `request_run` inserts a `queued` row and executes nothing; `execute_run`
# claims it and stages it; `app/nrb/runner.py` is the loop that pairs them.
# --------------------------------------------------------------------------- #
def test_requesting_a_run_executes_nothing_and_leaves_it_queued(tmp_path, monkeypatch):
    """The whole point of the split: acceptance costs one INSERT.

    Before this, `POST /v1/nrb/runs` ran the stages inline, so a request that
    included `sync` held an HTTP connection open for minutes while it read ~190
    pages of a central bank's REST API. Admission must touch none of that.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()

        run = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            trigger="api", requested_by="admin@example.com",
            session_factory=Session,
        )
        docs = (
            await session.execute(
                text("SELECT count(*) FROM documents d JOIN departments dp "
                     "  ON dp.id = d.department_id WHERE dp.code = :c"),
                {"c": DEPT_CODE},
            )
        ).scalar_one()
        return run, calls, docs

    run, calls, docs = _run(body)
    assert run.status == "queued" and run.stage == "queued"
    assert run.trigger == "api" and run.requested_by == "admin@example.com"
    assert run.started_at is None and run.finished_at is None
    assert calls == []          # no sync, no fetch, no extract
    assert docs == 0            # and no rag enqueue either


def test_a_queued_run_survives_the_requesting_session(tmp_path, monkeypatch):
    """It is a committed row, not a task in a process's memory.

    This is what makes a 202 honest: the gateway can die immediately afterwards
    and the accepted run is still there for a runner to pick up. Read back
    through a session the requester never touched.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await session.commit()
        run = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, limit=3, sections=("circulars",)),
            session_factory=Session,
        )
        # A brand-new session — the "API restarted" simulation, as far as a
        # rolled-back test transaction can go.
        async with Session() as fresh:
            reread = await pipeline.get_run(fresh, run.id)
            queued_id = await pipeline.claim_next(Session)
            await fresh.rollback()
        return run, reread, queued_id

    run, reread, queued_id = _run(body)
    assert reread is not None
    assert reread.status == "queued"
    assert reread.scope["sections"] == ["circulars"]
    assert reread.scope["limit"] == 3
    assert queued_id == run.id          # a runner would find exactly this one


def test_the_runner_claims_a_queued_run_and_transitions_it(tmp_path, monkeypatch):
    """queued -> running -> awaiting_jobs, driven by `runner.run_once`.

    The runner is exercised rather than `execute_run` directly, because the loop
    is where "poll, then hand the id to the service" has to be right.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        from app.nrb import runner

        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await session.commit()
        requested = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, limit=10),
            session_factory=Session,
        )
        monkeypatch.setattr(
            runner, "async_sessionmaker", lambda *a, **kw: Session
        )
        did = await runner.run_once(engine, get_settings())
        idle = await runner.run_once(engine, get_settings())
        async with Session() as s:
            after = await pipeline.get_run(s, requested.id)
            await s.rollback()
        return did, idle, after, calls

    did, idle, after, calls = _run(body)
    assert did is True and idle is False        # one run, then an empty queue
    assert calls == ["sync", "fetch", "extract"]
    assert after.status == PIPELINE_AWAITING
    assert after.stage == "waiting"
    assert after.started_at is not None
    assert after.jobs == {"queued": 1}


def test_a_second_runner_cannot_claim_the_same_run(tmp_path, monkeypatch):
    """`FOR UPDATE SKIP LOCKED` on the one row: the loser gets None and moves on.

    Asserted on `_claim` directly, because that is the transition. The advisory
    lock already stops two runners ORCHESTRATING at once; this is what stops two
    of them believing they own the same row.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await session.commit()
        run = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, limit=1),
            session_factory=Session,
        )
        first = await pipeline._claim(Session, run.id)
        second = await pipeline._claim(Session, run.id)
        async with Session() as s:
            after = await pipeline.get_run(s, run.id)
            await s.rollback()
        return first, second, after

    first, second, after = _run(body)
    assert first is not None          # the claimer gets the scope back
    assert first.department == DEPT_CODE
    assert second is None             # no longer `queued`
    assert after.status == PIPELINE_RUNNING


def test_executing_a_run_that_is_no_longer_queued_is_a_no_op(tmp_path, monkeypatch):
    """`_claim` returning None means "not my work", and nothing is re-run.

    Reached whenever the row moved on between the poll and the claim: another
    runner took it, or the sweep failed it. Asserted against an already-terminal
    run, which is the case that cannot be confused with anything else — a
    `running` row would be swept first, because holding the lock proves no
    orchestrator is alive.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await session.commit()
        run = await pipeline.start(
            pipeline.PipelineScope(department=DEPT_CODE, keys=("nothing",)),
            engine=engine, session_factory=Session,
        )
        assert run.status == PIPELINE_SUCCEEDED
        calls.clear()
        view = await pipeline.execute_run(
            run.id, engine=engine, session_factory=Session
        )
        return view, calls

    view, calls = _run(body)
    assert calls == []                         # no stage ran twice
    assert view.status == PIPELINE_SUCCEEDED   # returned untouched


def test_a_queued_run_refuses_a_second_request(tmp_path, monkeypatch):
    """`queued` is an active update, so admission refuses — and the index agrees.

    Two guards, and the test proves both: the SELECT gate (which produces the
    useful `PipelineBusy` body) and `ux_nrb_pipeline_runs_one_active`, which is
    what makes the gate not have to win a race.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await session.commit()
        first = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, limit=1),
            session_factory=Session,
        )
        with pytest.raises(pipeline.PipelineBusy) as excinfo:
            await pipeline.request_run(
                pipeline.PipelineScope(department=DEPT_CODE, limit=1),
                session_factory=Session,
            )
        # And the database refuses the state directly, gate or no gate.
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO nrb_pipeline_runs (trigger, status, stage, scope, "
                    " counters) VALUES ('api', 'queued', 'queued', '{}'::jsonb, "
                    " '{}'::jsonb)"
                )
            )
            await session.flush()
        await session.rollback()
        return first, excinfo.value

    first, busy = _run(body)
    assert busy.run is not None and busy.run.id == first.id
    assert busy.run.status == "queued"


def test_two_admissions_that_both_pass_the_gate_are_arbitrated_by_the_index(
    tmp_path, monkeypatch
):
    """The lost race itself: the index refuses, and the loser gets `PipelineBusy`.

    The test above proves the two guards SEPARATELY — the SELECT gate turning a
    second request away, and the database refusing the state when handed it
    directly. Neither reaches `request_run`'s `except IntegrityError`, which is
    the code that turns a lost race into the same answer the gate would have
    given a moment earlier.

    THE INTERLEAVING IS FORCED, DELIBERATELY. Both callers have to observe an
    empty gate before either inserts, and hoping two `asyncio.gather`'d calls
    happen to interleave that way gives a test that quietly goes through the GATE
    whenever they do not — asserting nothing about the handler while looking like
    it does. So the gate is blinded for exactly its two observations, which is
    precisely what a real race makes true (both SELECTed before either
    INSERTed), and the handler's own winner lookup — the third call — runs for
    real. Blinding that one too would answer with `run: None` and every
    assertion below would still pass.

    Two independent connections are NOT the way to write this: the loser's
    INSERT blocks on the winner's uncommitted row, and this harness's outer
    transaction is never committed, so it would hang rather than fail.
    """
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    _patch_store(monkeypatch, tmp_path)

    real_active_run = pipeline.active_run
    gate_observations: list[str] = []

    async def blind_gate(session, **kwargs):
        # Calls 1 and 2 are the two gates. Call 3 is the handler resolving the
        # winner after its rollback, and that one must see the truth.
        if len(gate_observations) < 2:
            gate_observations.append("blind")
            return None
        return await real_active_run(session, **kwargs)

    monkeypatch.setattr(pipeline, "active_run", blind_gate)
    scope = pipeline.PipelineScope(department=DEPT_CODE, limit=1)

    async def body(session, Session, engine):
        from sqlalchemy import select

        from app.nrb.models import PIPELINE_ACTIVE_STATUSES, NRBPipelineRun

        await _department(session)
        await session.commit()
        winner = await pipeline.request_run(scope, session_factory=Session)
        with pytest.raises(pipeline.PipelineBusy) as excinfo:
            await pipeline.request_run(scope, session_factory=Session)
        active_ids = set(
            (
                await session.execute(
                    select(NRBPipelineRun.id).where(
                        NRBPipelineRun.status.in_(PIPELINE_ACTIVE_STATUSES)
                    )
                )
            ).scalars().all()
        )
        return winner, excinfo.value, active_ids

    winner, busy, active_ids = _run(body)

    # Both gates were blind, so the only thing that can have refused the second
    # request is `ux_nrb_pipeline_runs_one_active`.
    assert gate_observations == ["blind", "blind"]
    # And the loser was ANSWERED, not crashed: an unhandled `IntegrityError`
    # would not have been caught by `pytest.raises(PipelineBusy)` above.
    assert busy.run is not None and busy.run.id == winner.id
    assert busy.run.status == "queued"
    # Exactly one active run exists — the winner. Nothing was admitted twice.
    assert active_ids == {winner.id}
    assert calls == []


def test_a_runner_crash_mid_run_is_recovered_by_the_next_one(tmp_path, monkeypatch):
    """The existing sweep, now also unblocking ADMISSION.

    A run left `running` by a killed runner used to only make a status view lie.
    Now it also occupies the singleton index, so nothing could ever be accepted
    again — which is why `execute_run` sweeps BEFORE it claims. Holding the lock
    is still what makes the sweep sound with no timeout.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await session.commit()
        run = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, limit=1),
            session_factory=Session,
        )
        await pipeline._claim(Session, run.id)      # ...and then the runner dies

        # Nothing can be accepted while the corpse holds the active slot.
        with pytest.raises(pipeline.PipelineBusy):
            await pipeline.request_run(
                pipeline.PipelineScope(department=DEPT_CODE, limit=1),
                session_factory=Session,
            )

        # A new runner starts. Its sweep clears the corpse...
        async with Session() as s:
            swept = await pipeline.sweep_abandoned(s)
            await s.commit()
        # ...and admission works again.
        fresh = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, limit=1),
            session_factory=Session,
        )
        async with Session() as s:
            corpse = await pipeline.get_run(s, run.id)
            await s.rollback()
        return swept, corpse, fresh

    swept, corpse, fresh = _run(body)
    assert swept == 1
    assert corpse.status == PIPELINE_FAILED
    assert "did not finish" in corpse.error
    assert fresh.status == "queued"


def test_a_run_that_stages_nothing_becomes_terminal_without_a_runner_wait(
    tmp_path, monkeypatch
):
    """Queued nothing, so nothing to wait for: terminal on the spot."""
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await session.commit()
        run = await pipeline.request_run(
            pipeline.PipelineScope(department=DEPT_CODE, keys=("no-such-key",)),
            session_factory=Session,
        )
        view = await pipeline.execute_run(
            run.id, engine=engine, session_factory=Session
        )
        return view

    view = _run(body)
    assert view.status == PIPELINE_SUCCEEDED
    assert view.stage == "done"
    assert view.jobs == {}
    assert view.counters["jobs"] == {}      # frozen, per §24.2
    assert view.finished_at is not None


def test_the_scope_survives_the_process_boundary(tmp_path, monkeypatch):
    """The runner is a different PROCESS, so the scope has to travel as data.

    `as_dict` stores `keys` as a COUNT — right for a status view, useless for
    execution — so the verbatim list rides in `scope['key_list']`. If that broke,
    a keyed run would silently widen to "everything matching the other bounds",
    which is exactly the kind of quiet scope creep the CLI's `--all` guard exists
    to prevent.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/a.pdf")
        await _blob(session, tmp_path, b"two", key="https://www.nrb.org.np/b.pdf")
        await session.commit()
        run = await pipeline.request_run(
            pipeline.PipelineScope(
                department=DEPT_CODE, keys=("https://www.nrb.org.np/a.pdf",),
                retry_failed=True, extensions=("pdf",),
            ),
            session_factory=Session,
        )
        assert run.scope["keys"] == 1        # a count on the row
        view = await pipeline.execute_run(
            run.id, engine=engine, session_factory=Session
        )
        return view

    view = _run(body)
    # One key in scope, so exactly one blob was staged — not both.
    assert view.counters["rag"]["scope_blobs"] == 1
    assert view.counters["rag"]["created"] == 1
    assert view.scope["retry_failed"] is True
    assert view.scope["extensions"] == ["pdf"]

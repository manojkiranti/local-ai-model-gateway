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

    A run left `running` by a killed process would otherwise sit there forever
    and make a status view lie. `awaiting_jobs` runs are deliberately NOT swept:
    they hold no lock, they are legitimately unfinished, and their jobs are
    still being drained by the worker.
    """
    _stub_stages(monkeypatch, [])
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session, engine):
        await _department(session)
        planted = {}
        for status, stage in (("running", "fetch"), ("awaiting_jobs", "waiting")):
            planted[status] = (
                await session.execute(
                    text(
                        "INSERT INTO nrb_pipeline_runs (trigger, status, stage, "
                        " scope, counters, started_at) VALUES "
                        "('cli', :s, :g, '{}'::jsonb, '{}'::jsonb, now()) "
                        "RETURNING id"
                    ),
                    {"s": status, "g": stage},
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
                    {"i": [planted["running"], planted["awaiting_jobs"], fresh.id]},
                )
            ).all()
        )
        error = (
            await session.execute(
                text("SELECT error FROM nrb_pipeline_runs WHERE id = :i"),
                {"i": planted["running"]},
            )
        ).scalar_one()
        return planted, fresh, rows, error

    planted, fresh, rows, error = _run(body)
    assert rows[planted["running"]] == PIPELINE_FAILED
    assert "did not finish" in error
    assert rows[planted["awaiting_jobs"]] == PIPELINE_AWAITING   # untouched
    assert rows[fresh.id] == PIPELINE_SUCCEEDED                  # the run we made


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

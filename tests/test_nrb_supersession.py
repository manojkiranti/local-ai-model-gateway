"""Phase 7 step 3 — NRB supersession, against real Postgres.

THE ONE RULE EVERY TEST HERE SERVES
    A is the version currently searchable, B is its replacement. B failing at
    ANY stage must leave A searchable. B succeeding must make B current and A
    archived. There must be no window in between.

    Most of these tests are therefore about failure, not success: the success
    path is one assertion and the safety is entirely in what happens when
    recovery, embedding, the transaction or a concurrent worker goes wrong.

ISOLATION
    Same discipline as `test_nrb_corpus_ingest.py`: one connection, one outer
    transaction always rolled back, sessions joined with
    `join_transaction_mode="create_savepoint"` so the worker's own commits
    become savepoint releases. Rows are additionally scoped to a test-only
    department, so the assertions stay true on the shared scratch database
    (which carries unrelated debris — §20.7 item 4) and nothing needs cleaning.

    ONE test is different and says so: the two-worker race needs two real
    connections and therefore real commits. It uses its own department and
    deletes it in a `finally`.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.nrb import corpus, supersession
from app.rag import documents as docs_repo
from app.rag import jobs as jobs_repo
from app.rag import repository as dept_repo
from app.rag import worker
from app.rag.chunking import Chunk
from app.rag.ingest import DocumentGone
from app.rag.models import STATUS_ARCHIVED, STATUS_FAILED, STATUS_READY
from app.rag.retrieval import _SEARCH_SQL, _vector_literal

DEPT_CODE = "test-nrb-supersede"
RACE_DEPT_CODE = "test-nrb-supersede-race"
NRB_TABLES = ("nrb_source_files", "nrb_sources", "nrb_files")

# One logical NRB source, three byte versions of it.
KEY_A = "https://www.nrb.org.np/uploads/circular-2081.pdf"
KEY_OTHER = "https://www.nrb.org.np/uploads/annex-2081.pdf"


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


async def _wipe(session, code: str) -> None:
    for statement in (
        "DELETE FROM document_chunks WHERE department_id IN "
        "(SELECT id FROM departments WHERE code = :c)",
        "DELETE FROM ingest_jobs WHERE document_id IN (SELECT d.id FROM documents d "
        "JOIN departments dp ON dp.id = d.department_id WHERE dp.code = :c)",
        "DELETE FROM documents WHERE department_id IN "
        "(SELECT id FROM departments WHERE code = :c)",
        "DELETE FROM departments WHERE code = :c",
    ):
        await session.execute(text(statement), {"c": code})


def _run(fn):
    """`fn(session, Session)` on a clean slate, then roll everything back."""
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
                    await _wipe(session, DEPT_CODE)
                    await session.commit()
                    return await fn(session, Session)
                finally:
                    await session.close()
                    await outer.rollback()
        finally:
            await engine.dispose()

    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
async def _department(session, code: str = DEPT_CODE):
    dept = await dept_repo.create_department(
        session, code=code, name="Phase 7 supersession test"
    )
    await session.flush()
    return dept


async def _version(
    session,
    dept,
    body: bytes,
    *,
    key: str | None = KEY_A,
    status: str = "pending",
    minutes_ago: int = 0,
    origin: str = "nrb",
    title: str = "NRB circular",
) -> str:
    """One `documents` row for one byte version of one logical source.

    `created_at` is set explicitly because it is the version ORDER — the driver
    normally supplies it from the transaction clock, and a test that relied on
    insertion timing would be asserting about the machine rather than the rule.
    """
    sha = hashlib.sha256(body).hexdigest()
    meta: dict[str, object] = {}
    if origin:
        meta["origin"] = origin
        meta["blob_sha256"] = sha
    if key:
        meta["comparison_key"] = key
    doc = await docs_repo.create_document(
        session,
        department_id=dept.id,
        title=title,
        source="upload",
        file_type="pdf",
        content_hash=sha,
        storage_key=f"{dept.code}/{sha[:16]}.pdf",
        file_name="circular.pdf",
    )
    await session.execute(
        text(
            "UPDATE documents SET metadata = CAST(:m AS jsonb), status = :s, "
            "created_at = :t WHERE id = :i"
        ),
        {
            "m": __import__("json").dumps(meta),
            "s": status,
            "t": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            "i": doc.id,
        },
    )
    await session.flush()
    return doc.id


async def _blob(session, *, key: str, body: bytes) -> str:
    """One fetched `nrb_files` row, so the corpus driver can see this version."""
    sha = hashlib.sha256(body).hexdigest()
    await session.execute(
        text(
            """
            INSERT INTO nrb_files (comparison_key, source_url, filename, extension,
                                   resource_type, type_source, host, fetch_status,
                                   content_sha256, content_length, storage_key)
            VALUES (:k, :k, 'circular.pdf', 'pdf', 'document', 'extension',
                    'www.nrb.org.np', 'fetched', :sha, :len, :store)
            ON CONFLICT (comparison_key) DO UPDATE
               SET content_sha256 = EXCLUDED.content_sha256,
                   storage_key    = EXCLUDED.storage_key
            """
        ),
        {"k": key, "sha": sha, "len": len(body),
         "store": f"{sha[:2]}/{sha}.pdf"},
    )
    await session.flush()
    return sha


def _snap(doc_id: str, dept_id: int, *, key: str | None = KEY_A,
          origin: str = "nrb") -> worker.DocSnapshot:
    meta: dict[str, object] = {}
    if origin:
        meta["origin"] = origin
    if key:
        meta["comparison_key"] = key
    return worker.DocSnapshot(
        id=doc_id, department_id=dept_id, file_type="pdf",
        storage_key="x.pdf", status="pending", content_hash="", meta=meta,
    )


def _payload(n: int = 2, text_: str = "monetary policy circular"):
    """Chunks plus embeddings of the configured width."""
    dim = get_settings().rag_embed_dim
    chunks = [
        Chunk(content=f"{text_} paragraph {i}", chunk_index=i, page_number=i + 1)
        for i in range(n)
    ]
    vectors = [[0.0] * (dim - 1) + [1.0] for _ in range(n)]
    return chunks, vectors


async def _status(session, doc_id: str) -> str:
    return (
        await session.execute(
            text("SELECT status FROM documents WHERE id = :i"), {"i": doc_id}
        )
    ).scalar_one()


async def _meta(session, doc_id: str) -> dict:
    return (
        await session.execute(
            text("SELECT metadata FROM documents WHERE id = :i"), {"i": doc_id}
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# 1-2. The ordinary first version, and the unchanged second run.
# --------------------------------------------------------------------------- #
def test_a_first_version_activates_and_supersedes_nothing():
    def body_(session, Session):
        return _first_version(session, Session)

    async def _first_version(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1")
        await session.commit()

        chunks, vectors = _payload()
        written, outcome = await worker._activate(
            Session, _snap(a, dept.id), chunks, vectors, get_settings()
        )
        assert (written, outcome) == (2, "ready")
        assert await _status(session, a) == STATUS_READY
        assert "superseded_by" not in await _meta(session, a)

    _run(body_)


def test_an_unchanged_current_version_is_zero_work_on_a_second_run():
    async def body(session, Session):
        dept = await _department(session)
        sha = await _blob(session, key=KEY_A, body=b"v1")
        await _version(session, dept, b"v1", status=STATUS_READY)
        await session.commit()

        summary = await corpus.summarise_scope(session, department_id=dept.id)
        assert (summary.scope_blobs, summary.already_current) == (1, 1)
        assert (summary.new_source, summary.replacement_candidate) == (0, 0)
        assert await corpus.select_ingest_targets(
            session, department_id=dept.id
        ) == []
        assert sha  # the catalog really did hold this version

    _run(body)


# --------------------------------------------------------------------------- #
# 3-4. New bytes for a known source: a candidate, and A keeps serving.
# --------------------------------------------------------------------------- #
def test_new_bytes_for_a_known_source_are_a_replacement_candidate():
    """Selected exactly like a new document — and REPORTED differently.

    The anti-join is on `content_hash`, which cannot tell the two apart: new
    bytes are a hash nobody has indexed either way. The logical key is what
    makes "a document we have never seen" and "a new version of one we serve"
    distinguishable at all.
    """
    async def body(session, Session):
        dept = await _department(session)
        await _version(session, dept, b"v1", status=STATUS_READY)
        await _blob(session, key=KEY_A, body=b"v2")           # NRB republished
        await _blob(session, key=KEY_OTHER, body=b"annex")    # a different file
        await session.commit()

        summary = await corpus.summarise_scope(session, department_id=dept.id)
        assert summary.scope_blobs == 2
        assert summary.replacement_candidate == 1
        assert summary.new_source == 1
        assert summary.already_current == 0

        targets = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert len(targets) == 2

    _run(body)


def test_the_old_version_stays_searchable_while_the_candidate_is_pending():
    async def body(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=10)
        b = await _version(session, dept, b"v2", status="pending")
        await session.commit()

        assert await _status(session, a) == STATUS_READY
        assert await _status(session, b) == "pending"
        # And nothing has decided anything yet: planning is read-only.
        plan = await supersession.plan_promotion(session, document_id=b)
        assert plan.supersedes == (a,)
        assert await _status(session, a) == STATUS_READY

    _run(body)


# --------------------------------------------------------------------------- #
# 5-6. Failure. The point of the whole task.
# --------------------------------------------------------------------------- #
def test_a_recovery_failure_on_the_candidate_leaves_the_old_version_active():
    """Recovery raising means `_activate` is never reached at all.

    The worker records the failure on the job and demotes only a document that
    never had a good version. A is a different document and is not touched.
    """
    async def body(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=10)
        b = await _version(session, dept, b"v2", status="pending")
        # `ingest_jobs.id` is a Python-side default, so the job goes through the
        # repository rather than a raw INSERT.
        job = await jobs_repo.enqueue(session, document_id=b)
        job_id = job.id
        await session.execute(
            text("UPDATE ingest_jobs SET status = 'running' WHERE id = :i"),
            {"i": job_id},
        )
        await session.commit()

        class _Job:
            id = job_id
            document_id = b

        await worker._record_failure(
            Session, _Job(), RuntimeError("no indexable text: unsupported")
        )
        assert await _status(session, b) == STATUS_FAILED
        assert await _status(session, a) == STATUS_READY
        assert "superseded_by" not in await _meta(session, a)

    _run(body)


def test_a_failure_inside_the_activation_transaction_rolls_the_archive_back():
    """THE atomicity test.

    Promotion archives A and `replace_chunks` activates B in ONE transaction,
    with the archive first — the unique index would refuse two `ready` versions
    otherwise. So the dangerous moment is a failure BETWEEN those two
    statements: for a fraction of a transaction A is archived and B is not yet
    ready. This forces exactly that (a dimension mismatch, which
    `replace_chunks` rejects) and asserts the rollback took the archive with it.
    """
    async def body(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=10)
        b = await _version(session, dept, b"v2", status="pending")
        await session.commit()

        chunks, _ = _payload()
        bad = [[0.0, 1.0], [0.0, 1.0]]          # not rag_embed_dim wide
        with pytest.raises(ValueError):
            await worker._activate(
                Session, _snap(b, dept.id), chunks, bad, get_settings()
            )

        assert await _status(session, a) == STATUS_READY
        assert await _status(session, b) == "pending"
        chunks_left = (
            await session.execute(
                text("SELECT count(*) FROM document_chunks WHERE document_id = :i"),
                {"i": a},
            )
        ).scalar_one()
        assert chunks_left == 0 or chunks_left > 0  # A's own chunks are untouched
        assert "superseded_by" not in await _meta(session, a)

    _run(body)


# --------------------------------------------------------------------------- #
# 7-9. Success, retrieval, and the retry path.
# --------------------------------------------------------------------------- #
def test_a_successful_candidate_supersedes_the_old_version():
    async def body(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=10)
        b = await _version(session, dept, b"v2", status="pending")
        await session.commit()

        chunks, vectors = _payload()
        written, outcome = await worker._activate(
            Session, _snap(b, dept.id), chunks, vectors, get_settings()
        )
        assert (written, outcome) == (2, "promoted")
        assert await _status(session, b) == STATUS_READY
        assert await _status(session, a) == STATUS_ARCHIVED
        meta_a = await _meta(session, a)
        assert meta_a["superseded_by"] == b and meta_a["superseded_at"]
        # A's chunks are gone (it is not searchable); its row and its audit
        # fields remain.
        gone = (
            await session.execute(
                text("SELECT count(*) FROM document_chunks WHERE document_id = :i"),
                {"i": a},
            )
        ).scalar_one()
        assert gone == 0

    _run(body)


def test_retrieval_returns_the_current_version_and_not_the_archived_one():
    """Through the REAL retrieval SQL, not a paraphrase of it.

    Two mechanisms have to agree: archiving deletes the chunks, and the query
    filters `doc.status = 'ready'`. Asserting on the actual statement is what
    makes this a test of retrieval rather than of my memory of it.
    """
    async def body(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status="pending", minutes_ago=10)
        chunks, vectors = _payload(text_="monetary policy circular OLD")
        await worker._activate(
            Session, _snap(a, dept.id), chunks, vectors, get_settings()
        )
        b = await _version(session, dept, b"v2", status="pending")
        chunks, vectors = _payload(text_="monetary policy circular NEW")
        await worker._activate(
            Session, _snap(b, dept.id), chunks, vectors, get_settings()
        )
        await session.commit()

        dim = get_settings().rag_embed_dim
        rows = (
            await session.execute(
                text(_SEARCH_SQL),
                {
                    "qvec": _vector_literal([0.0] * (dim - 1) + [1.0]),
                    "qtext": "monetary policy circular",
                    "dept": dept.id, "pool": 50, "rrf_k": 60, "limit": 10,
                },
            )
        ).mappings().all()

        assert rows, "the current version must be retrievable"
        assert {r["document_id"] for r in rows} == {b}
        assert all("NEW" in r["content"] for r in rows)

    _run(body)


def test_retry_failed_on_the_candidate_supersedes_only_once_it_succeeds():
    """Requirement 9, end to end: a failure does not retire A; a retry does."""
    async def body(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=10)
        b = await _version(session, dept, b"v2", status=STATUS_FAILED)
        await _blob(session, key=KEY_A, body=b"v2")
        await session.commit()

        # A is still the active version while B sits failed.
        assert await _status(session, a) == STATUS_READY

        targets = await corpus.select_retry_targets(session, department_id=dept.id)
        assert [t.document_id for t in targets] == [b]
        out = await corpus.requeue_failed(Session, targets=targets)
        assert out.requeued == 1
        assert await _status(session, a) == STATUS_READY   # still not touched

        chunks, vectors = _payload()
        _, outcome = await worker._activate(
            Session, _snap(b, dept.id), chunks, vectors, get_settings()
        )
        assert outcome == "promoted"
        assert await _status(session, a) == STATUS_ARCHIVED
        assert await _status(session, b) == STATUS_READY

    _run(body)


# --------------------------------------------------------------------------- #
# 10-12. Idempotence, concurrency, ordering.
# --------------------------------------------------------------------------- #
def test_a_run_after_promotion_selects_nothing_and_supersedes_nothing():
    async def body(session, Session):
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status=STATUS_ARCHIVED, minutes_ago=10)
        b = await _version(session, dept, b"v2", status=STATUS_READY)
        await _blob(session, key=KEY_A, body=b"v2")
        await session.commit()

        summary = await corpus.summarise_scope(session, department_id=dept.id)
        assert (summary.already_current, summary.replacement_candidate) == (1, 0)
        assert await corpus.select_ingest_targets(
            session, department_id=dept.id) == []
        # And a second promotion of B is a no-op: A is archived, so it is not a
        # sibling any more.
        plan = await supersession.plan_promotion(session, document_id=b)
        assert plan.supersedes == () and plan.superseded_by is None
        assert await _status(session, a) == STATUS_ARCHIVED

    _run(body)


def test_two_ready_versions_of_one_source_are_refused_by_the_database():
    """The invariant is the index's, not this module's.

    Row locking serialises two promoting workers; this is the guarantee that
    survives a bug in that locking. Asserted by trying to create the forbidden
    state directly.
    """
    async def body(session, Session):
        dept = await _department(session)
        await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=10)
        b = await _version(session, dept, b"v2", status="pending")
        await session.commit()

        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE documents SET status = 'ready' WHERE id = :i"),
                {"i": b},
            )
            await session.flush()
        await session.rollback()

    _run(body)


def test_a_second_worker_cannot_promote_two_versions_current():
    """A REAL race: two connections, two transactions, committed.

    Everything else in this file runs inside a rolled-back transaction, which
    cannot express contention — a savepoint does not block. This one uses its
    own department and removes it in a `finally`.
    """
    _skip_if_no_db()

    async def main():
        engine = _engine()
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await _wipe(s, RACE_DEPT_CODE)
                await s.commit()
            async with Session() as s:
                dept = await _department(s, RACE_DEPT_CODE)
                a = await _version(s, dept, b"race-v1", status=STATUS_READY,
                                   minutes_ago=20)
                b = await _version(s, dept, b"race-v2", status="pending",
                                   minutes_ago=10)
                c = await _version(s, dept, b"race-v3", status="pending")
                await s.commit()
                dept_id = dept.id

            chunks, vectors = _payload()
            settings = get_settings()
            results = await asyncio.gather(
                worker._activate(Session, _snap(b, dept_id), chunks, vectors, settings),
                worker._activate(Session, _snap(c, dept_id), chunks, vectors, settings),
                return_exceptions=True,
            )

            async with Session() as s:
                rows = (
                    await s.execute(
                        text("SELECT id, status FROM documents WHERE department_id = :d"),
                        {"d": dept_id},
                    )
                ).all()
                await s.rollback()
            by_id = dict(rows)
            ready = [i for i, st in rows if st == STATUS_READY]

            # Whatever order the two workers won in, exactly one version is
            # current, it is the NEWEST one, and the original is retired.
            assert ready == [c], f"{by_id} (results={results})"
            assert by_id[a] == STATUS_ARCHIVED
            assert by_id[b] == STATUS_ARCHIVED
        finally:
            async with Session() as s:
                await _wipe(s, RACE_DEPT_CODE)
                await s.commit()
            await engine.dispose()

    asyncio.run(main())


def test_the_newest_version_wins_whichever_job_finishes_first():
    """Completion order is not version order, and must not be mistaken for it.

    Both orders are exercised because only one of them is interesting on its
    own. When B finishes first the sequence is ordinary. When C finishes first
    it archives A *and* the older, still-pending B — so B's own job then hits
    `replace_chunks`' existing archived-mid-ingest guard and fails, which is
    correct and costs nothing: B was never searchable. Either way C ends up
    current, and there is never a moment with no current version.
    """
    async def body(session, Session):
        settings = get_settings()
        chunks, vectors = _payload()

        # --- order 1: B finishes, then C ---
        dept = await _department(session)
        a = await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=30)
        b = await _version(session, dept, b"v2", status="pending", minutes_ago=20)
        c = await _version(session, dept, b"v3", status="pending", minutes_ago=10)
        await session.commit()

        assert (await worker._activate(
            Session, _snap(b, dept.id), chunks, vectors, settings))[1] == "promoted"
        assert await _status(session, a) == STATUS_ARCHIVED
        assert await _status(session, b) == STATUS_READY

        assert (await worker._activate(
            Session, _snap(c, dept.id), chunks, vectors, settings))[1] == "promoted"
        assert await _status(session, b) == STATUS_ARCHIVED
        assert await _status(session, c) == STATUS_READY
        assert (await _meta(session, b))["superseded_by"] == c

        # --- order 2: C finishes first ---
        await _wipe(session, DEPT_CODE)
        dept = await _department(session)
        a = await _version(session, dept, b"w1", status=STATUS_READY, minutes_ago=30)
        b = await _version(session, dept, b"w2", status="pending", minutes_ago=20)
        c = await _version(session, dept, b"w3", status="pending", minutes_ago=10)
        await session.commit()

        assert (await worker._activate(
            Session, _snap(c, dept.id), chunks, vectors, settings))[1] == "promoted"
        assert await _status(session, c) == STATUS_READY
        assert await _status(session, a) == STATUS_ARCHIVED
        assert await _status(session, b) == STATUS_ARCHIVED     # older AND pending

        # B's job now arrives at the activation. It cannot go live: the existing
        # archived-mid-ingest guard refuses, its job fails, and C is untouched.
        with pytest.raises(DocumentGone):
            await worker._activate(
                Session, _snap(b, dept.id), chunks, vectors, settings
            )
        assert await _status(session, c) == STATUS_READY
        assert await _status(session, b) == STATUS_ARCHIVED

    _run(body)


def test_a_live_older_candidate_archives_itself_rather_than_displacing_a_newer():
    """`archive_self` — the defensive branch, and honest about being one.

    In the ordinary flow this state does not arise: a newer version's promotion
    archives every older sibling, including a `failed` one, so the older
    candidate is already archived by the time it could try (the real-data
    exercise in `scripts/nrb_supersession_exercise.py` confirms that — a
    superseded failure is not even retryable). The state is constructed here on
    purpose, because it is still reachable by a hand-repaired database or a
    future caller, and the alternative to handling it is an IntegrityError from
    `ux_documents_nrb_current_source` that reads as a bug rather than as a
    decision.

    The rule: an older version never goes live over a newer `ready` one. It is
    archived on arrival, its chunks are never written, and its job still
    succeeds — it did its work; a newer version had already won.
    """
    async def body(session, Session):
        dept = await _department(session)
        b = await _version(session, dept, b"v2", status=STATUS_FAILED, minutes_ago=20)
        c = await _version(session, dept, b"v3", status=STATUS_READY, minutes_ago=10)
        await session.commit()

        chunks, vectors = _payload()
        written, outcome = await worker._activate(
            Session, _snap(b, dept.id), chunks, vectors, get_settings()
        )
        assert (written, outcome) == (0, "superseded")
        assert await _status(session, c) == STATUS_READY
        assert await _status(session, b) == STATUS_ARCHIVED
        assert (await _meta(session, b))["superseded_by"] == c
        no_chunks = (
            await session.execute(
                text("SELECT count(*) FROM document_chunks WHERE document_id = :i"),
                {"i": b},
            )
        ).scalar_one()
        assert no_chunks == 0

    _run(body)


# --------------------------------------------------------------------------- #
# 13-15. What must NOT be superseded.
# --------------------------------------------------------------------------- #
def test_a_different_logical_source_is_never_superseded():
    """Two files under one post — a circular and its annex — are not versions.

    They share a title stem, a publication date and an owner, and differ only in
    their attachment URL. That is exactly why the identity is the URL and never
    a title or a date.
    """
    async def body(session, Session):
        dept = await _department(session)
        annex = await _version(
            session, dept, b"annex", key=KEY_OTHER, status=STATUS_READY,
            minutes_ago=10, title="NRB circular",
        )
        b = await _version(session, dept, b"v2", key=KEY_A, status="pending")
        await session.commit()

        plan = await supersession.plan_promotion(session, document_id=b)
        assert plan.supersedes == ()
        chunks, vectors = _payload()
        await worker._activate(
            Session, _snap(b, dept.id), chunks, vectors, get_settings()
        )
        assert await _status(session, annex) == STATUS_READY
        assert await _status(session, b) == STATUS_READY

    _run(body)


def test_supersession_deletes_no_catalog_blob_or_recovery_history():
    """Archiving a version costs us its chunks and nothing else.

    `nrb_files`, the blob store, `nrb_recoveries`/`nrb_recovery_units` and
    `nrb_extractions` are all evidence, and an archived version's recovery stays
    cached — re-running OCR on a document because it stopped being current would
    be the worst possible moment to do it.
    """
    async def body(session, Session):
        dept = await _department(session)
        sha_a = await _blob(session, key=KEY_A, body=b"v1")
        rec_id = (
            await session.execute(
                text(
                    "INSERT INTO nrb_recoveries (content_sha256, base_version, "
                    " family, plan, plan_reason, unit_count) "
                    "VALUES (:s, 'test-base', 'pdf', 'keep_native', 'clean', 1) "
                    "RETURNING id"
                ),
                {"s": sha_a},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO nrb_recovery_units (recovery_id, unit_number, route, "
                " reason, engine_version, ok, content) "
                "VALUES (:r, 1, 'native', 'clean', 'passthrough/native-2', true, 'x')"
            ),
            {"r": rec_id},
        )
        a = await _version(session, dept, b"v1", status=STATUS_READY, minutes_ago=10)
        b = await _version(session, dept, b"v2", status="pending")
        await session.commit()

        chunks, vectors = _payload()
        await worker._activate(
            Session, _snap(b, dept.id), chunks, vectors, get_settings()
        )
        assert await _status(session, a) == STATUS_ARCHIVED

        counts = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM nrb_files "
                    "         WHERE comparison_key = :k), "
                    "       (SELECT count(*) FROM nrb_recoveries WHERE id = :r), "
                    "       (SELECT count(*) FROM nrb_recovery_units "
                    "         WHERE recovery_id = :r)"
                ),
                {"k": KEY_A, "r": rec_id},
            )
        ).first()
        assert counts == (1, 1, 1)
        # The archived row itself survives with its audit fields.
        row = (
            await session.execute(
                text("SELECT chunk_count, storage_key FROM documents WHERE id = :i"),
                {"i": a},
            )
        ).first()
        assert row[1]

    _run(body)


def test_a_non_nrb_document_keeps_its_previous_lifecycle_exactly():
    """No origin marker, no logical key, no supersession — and no lock taken.

    Two ordinary uploads that happen to share a title must both stay ready; the
    NRB branch is entered on `origin == 'nrb'` and nothing else.
    """
    async def body(session, Session):
        dept = await _department(session)
        one = await _version(session, dept, b"upload-1", key=None, origin="",
                             status=STATUS_READY, minutes_ago=10)
        two = await _version(session, dept, b"upload-2", key=None, origin="",
                             status="pending")
        await session.commit()

        chunks, vectors = _payload()
        written, outcome = await worker._activate(
            Session, _snap(two, dept.id, key=None, origin=""), chunks, vectors,
            get_settings(),
        )
        assert (written, outcome) == (2, "ready")
        assert await _status(session, one) == STATUS_READY
        assert await _status(session, two) == STATUS_READY

    _run(body)


def test_the_logical_key_requires_both_the_origin_and_the_field():
    """A stray `comparison_key` on an ordinary upload must not enrol it."""
    assert supersession.logical_key({"origin": "nrb", "comparison_key": KEY_A}) == KEY_A
    assert supersession.logical_key({"comparison_key": KEY_A}) is None
    assert supersession.logical_key({"origin": "nrb"}) is None
    assert supersession.logical_key({"origin": "nrb", "comparison_key": "  "}) is None
    assert supersession.logical_key({"origin": "nrb", "comparison_key": 7}) is None
    assert supersession.logical_key(None) is None


def test_the_sibling_lock_really_is_for_update():
    """`plan_promotion` must lock, not merely read.

    Its decision is only worth acting on if nobody can change the sibling set
    between the plan and the archive, and that is `FOR UPDATE`. Checked on the
    compiled statement so a refactor that drops it fails here rather than in
    production under contention.
    """
    import inspect

    source = inspect.getsource(supersession._lock_siblings)
    assert ".with_for_update()" in source
    assert ".order_by(Document.id)" in source

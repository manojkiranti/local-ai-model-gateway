"""Worker loop against real Postgres. Embedding is faked so this runs without
Ollama; tests/test_rag_embedding_live.py covers the real backend.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import jobs, worker
from app.rag.models import JOB_FAILED, JOB_SUCCEEDED, STATUS_FAILED, STATUS_READY

DIM = 1536


class FakeEmbedClient:
    """Returns native-width vectors, like the real backend before truncation."""

    def __init__(self, native_dim=2560, fail=False):
        self.native_dim = native_dim
        self.fail = fail

    async def embeddings(self, payload):
        if self.fail:
            raise RuntimeError("embedding backend is down")
        return {
            "data": [
                {"index": i, "embedding": [0.01 * (i + 1)] * self.native_dim}
                for i, _ in enumerate(payload["input"])
            ]
        }


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
def queued_csv(tmp_path):
    """A department + a queued CSV document, file written to a temp docs dir."""
    _skip_if_no_db()
    from app.rag.storage import mint_storage_key, write_document

    tag = uuid.uuid4().hex[:8]
    doc_id = uuid.uuid4().hex
    key = mint_storage_key("wk", "leave.csv")
    write_document(
        b"Employee,Department,Days\nAlice,HR,10\nBob,HR,12\n", key, str(tmp_path)
    )

    async def setup(conn):
        dept = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'W') RETURNING id"),
            {"c": f"wk{tag}"})).scalar_one()
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status, storage_key) VALUES"
            " (:i, :d, 'T', 'upload', 'csv', :h, 'pending', :k)"),
            {"i": doc_id, "d": dept, "h": "c" * 64, "k": key})
        job_id = uuid.uuid4().hex
        await conn.execute(text(
            "INSERT INTO ingest_jobs (id, document_id, status)"
            " VALUES (:j, :i, 'queued')"), {"j": job_id, "i": doc_id})
        return dept, job_id

    dept, job_id = _sql(setup)
    yield {"dept": dept, "doc": doc_id, "job": job_id, "docs_dir": str(tmp_path)}

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id = :d"),
                           {"d": dept})
        await conn.execute(text("DELETE FROM departments WHERE id = :d"), {"d": dept})
    _sql(teardown)


def _settings_with(docs_dir):
    return get_settings().model_copy(update={"rag_docs_dir": docs_dir})


def _run_once(queued, client):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            return await worker.run_once(
                engine, client, _settings_with(queued["docs_dir"])
            )
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _read(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def test_run_once_returns_false_on_an_empty_queue(tmp_path):
    _skip_if_no_db()

    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            while await worker.run_once(engine, FakeEmbedClient(),
                                        _settings_with(str(tmp_path))):
                pass
            return await worker.run_once(engine, FakeEmbedClient(),
                                         _settings_with(str(tmp_path)))
        finally:
            await engine.dispose()

    assert asyncio.run(main()) is False


def test_a_queued_document_is_parsed_embedded_and_stored(queued_csv):
    assert _run_once(queued_csv, FakeEmbedClient()) is True

    async def check(s):
        doc = (await s.execute(text(
            "SELECT status, chunk_count, embed_dim, embed_model FROM documents"
            " WHERE id = :i"), {"i": queued_csv["doc"]})).one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        job = await jobs.get_job(s, queued_csv["job"])
        return doc, chunks, job.status

    doc, chunks, job_status = _read(check)
    assert doc.status == STATUS_READY
    assert doc.chunk_count > 0 and chunks == doc.chunk_count
    assert doc.embed_dim == DIM
    assert job_status == JOB_SUCCEEDED


def test_stored_chunks_are_exactly_1536_wide(queued_csv):
    _run_once(queued_csv, FakeEmbedClient())

    async def check(s):
        return (await s.execute(text(
            "SELECT DISTINCT vector_dims(embedding) FROM document_chunks"
            " WHERE document_id = :i"), {"i": queued_csv["doc"]})).scalars().all()

    assert _read(check) == [DIM]


def test_an_embedding_failure_fails_the_job_and_stores_nothing(queued_csv):
    """The atomic transaction's whole purpose: a failure leaves zero chunks."""
    assert _run_once(queued_csv, FakeEmbedClient(fail=True)) is True

    async def check(s):
        doc = (await s.execute(text(
            "SELECT status, chunk_count FROM documents WHERE id = :i"),
            {"i": queued_csv["doc"]})).one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        job = await jobs.get_job(s, queued_csv["job"])
        return doc.status, doc.chunk_count, chunks, job.status, job.error

    status, chunk_count, chunks, job_status, error = _read(check)
    assert chunks == 0 and chunk_count == 0
    assert status == STATUS_FAILED
    assert job_status == JOB_FAILED and error


def test_a_failed_job_frees_the_document_to_be_requeued(queued_csv):
    _run_once(queued_csv, FakeEmbedClient(fail=True))

    async def requeue(s):
        job = await jobs.enqueue(s, document_id=queued_csv["doc"])
        await s.commit()
        return job.status

    assert _read(requeue) == "queued"


def test_a_failed_re_ingest_leaves_a_ready_document_untouched(queued_csv):
    """A document already serving good chunks must not be libelled `failed` by
    a later ingest that blew up — the replacement rolled back, so its previous
    chunks are still there and still correct."""
    assert _run_once(queued_csv, FakeEmbedClient()) is True   # first ingest: ready

    async def before(s):
        return (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()

    first_count = _read(before)
    assert first_count > 0

    async def requeue(s):
        await jobs.enqueue(s, document_id=queued_csv["doc"])
        await s.commit()

    _read(requeue)
    _run_once(queued_csv, FakeEmbedClient(fail=True))         # re-ingest fails

    async def after(s):
        doc = (await s.execute(text(
            "SELECT status, chunk_count FROM documents WHERE id = :i"),
            {"i": queued_csv["doc"]})).one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        return doc.status, doc.chunk_count, chunks

    status, chunk_count, chunks = _read(after)
    assert status == STATUS_READY          # NOT failed
    assert chunks == first_count           # previous version intact
    assert chunk_count == first_count


def test_an_archived_document_is_not_resurrected_by_an_in_flight_ingest(queued_csv):
    """The race: worker parses+embeds, admin archives, worker commits. Without
    the FOR UPDATE check the chunks come back and status flips to ready."""
    async def archive(s):
        from app.rag import documents as docs_repo
        await docs_repo.archive_document(s, queued_csv["doc"])
        await s.commit()

    _read(archive)                                  # archived BEFORE the worker runs
    assert _run_once(queued_csv, FakeEmbedClient()) is True

    async def after(s):
        doc = (await s.execute(text(
            "SELECT status FROM documents WHERE id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        job = await jobs.get_job(s, queued_csv["job"])
        return doc, chunks, job.status, job.error

    status, chunks, job_status, error = _read(after)
    assert status == "archived"      # stayed archived
    assert chunks == 0               # NOT resurrected
    assert job_status == JOB_FAILED
    assert "archived" in (error or "").lower()


def test_the_heartbeat_advances_during_a_slow_job(queued_csv):
    """A job longer than the stale window must not be swept. The periodic
    heartbeat is what prevents that; one beat after embedding would not."""
    class SlowClient(FakeEmbedClient):
        async def embeddings(self, payload):
            await asyncio.sleep(0.35)
            return await super().embeddings(payload)

    settings = _settings_with(queued_csv["docs_dir"]).model_copy(
        update={"rag_ingest_heartbeat_seconds": 0.05}
    )

    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            return await worker.run_once(engine, SlowClient(), settings)
        finally:
            await engine.dispose()

    assert asyncio.run(main()) is True

    async def beats(s):
        job = await jobs.get_job(s, queued_csv["job"])
        return job.status, job.heartbeat_at, job.started_at

    status, heartbeat_at, started_at = _read(beats)
    assert status == JOB_SUCCEEDED
    assert heartbeat_at is not None and started_at is not None
    assert heartbeat_at > started_at   # at least one beat landed mid-flight


def test_parsing_runs_off_the_event_loop(queued_csv, monkeypatch):
    """Docling is synchronous and CPU-bound; running it inline would block the
    heartbeat. Assert it goes through asyncio.to_thread."""
    seen = {}
    real = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        seen["fn"] = getattr(fn, "__name__", str(fn))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", spy)
    assert _run_once(queued_csv, FakeEmbedClient()) is True
    assert seen.get("fn") == "_load_chunks_sync"


def test_preflight_rejects_a_backend_returning_too_few_dimensions(tmp_path):
    async def go():
        await worker.preflight(
            FakeEmbedClient(native_dim=768), _settings_with(str(tmp_path))
        )

    with pytest.raises(worker.WorkerPreflightError):
        asyncio.run(go())


def test_preflight_accepts_a_native_width_backend(tmp_path):
    async def go():
        await worker.preflight(
            FakeEmbedClient(native_dim=2560), _settings_with(str(tmp_path))
        )
        return True

    assert asyncio.run(go()) is True


def test_preflight_rejects_an_unreachable_backend(tmp_path):
    async def go():
        await worker.preflight(
            FakeEmbedClient(fail=True), _settings_with(str(tmp_path))
        )

    with pytest.raises(worker.WorkerPreflightError):
        asyncio.run(go())

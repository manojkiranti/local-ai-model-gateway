"""Schema-invariant tests against real Postgres. Skips if the DB is unreachable.

These assert the things only the database can enforce — the composite FK, the
two partial unique indexes, the status CHECK constraints those predicates depend
on, and RESTRICT on department deletion. Getting any of them wrong is silent
until it matters.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

VEC = "[" + ",".join(["0.1"] * 1536) + "]"


def _tx(fn):
    """Run `fn(conn)` inside one transaction on a THROWAWAY NullPool engine.

    Not the app's module-level engine: it pools connections bound to whichever
    event loop touched it first, and every `asyncio.run` here creates a new one,
    so the second test would hit "Event loop is closed". A fresh engine disposed
    inside the same loop sidesteps that entirely.
    """
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
        _tx(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001 - DB down -> skip
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def dept_pair():
    """Two departments plus one document in the first. Cleaned up after."""
    _skip_if_no_db()
    suffix = uuid.uuid4().hex[:8]
    doc_id = uuid.uuid4().hex
    state = {}

    async def setup(conn):
        a = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'A') RETURNING id"),
            {"c": f"a-{suffix}"})).scalar_one()
        b = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'B') RETURNING id"),
            {"c": f"b-{suffix}"})).scalar_one()
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'ready')"),
            {"i": doc_id, "d": a, "h": "h" * 64})
        return a, b

    state["a"], state["b"] = _tx(setup)
    state["doc"] = doc_id
    yield state

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id IN (:a,:b)"),
                           {"a": state["a"], "b": state["b"]})
        await conn.execute(text("DELETE FROM departments WHERE id IN (:a,:b)"),
                           {"a": state["a"], "b": state["b"]})
    _tx(teardown)


def test_vector_extension_is_installed():
    _skip_if_no_db()
    rows = _tx(lambda c: c.execute(
        text("SELECT extname FROM pg_extension WHERE extname = 'vector'")))
    rows = rows.all()
    assert rows, "pgvector extension is not installed in this database"


def test_chunk_cannot_claim_a_foreign_department(dept_pair):
    """The composite FK: a chunk of an A-document may not be labelled B."""
    async def forge(conn):
        await conn.execute(text(
            "INSERT INTO document_chunks (document_id, department_id, chunk_index,"
            " content, embedding) VALUES (:d, :dept, 0, 'forged', CAST(:v AS vector))"),
            {"d": dept_pair["doc"], "dept": dept_pair["b"], "v": VEC})

    with pytest.raises(Exception) as exc:
        _tx(forge)
    assert "foreign key" in str(exc.value).lower()


def test_chunk_with_its_own_department_is_accepted(dept_pair):
    async def insert_ok(conn):
        await conn.execute(text(
            "INSERT INTO document_chunks (document_id, department_id, chunk_index,"
            " content, embedding) VALUES (:d, :dept, 0, 'fine', CAST(:v AS vector))"),
            {"d": dept_pair["doc"], "dept": dept_pair["a"], "v": VEC})
        return (await conn.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": dept_pair["doc"]})).scalar_one()

    assert _tx(insert_ok) == 1


def test_same_content_hash_twice_in_one_department_is_rejected(dept_pair):
    async def dupe(conn):
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status) VALUES (:i, :d, 'T2', 'upload', 'pdf', :h, 'ready')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["a"], "h": "h" * 64})

    with pytest.raises(Exception) as exc:
        _tx(dupe)
    assert "ux_documents_active_content" in str(exc.value)


def test_archived_document_frees_its_content_hash_for_re_upload(dept_pair):
    """The reason the dedup index is partial rather than a plain UNIQUE."""
    new_id = uuid.uuid4().hex

    async def archive_then_readd(conn):
        await conn.execute(text(
            "UPDATE documents SET status='archived' WHERE id = :i"),
            {"i": dept_pair["doc"]})
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status) VALUES (:i, :d, 'again', 'upload', 'pdf', :h, 'ready')"),
            {"i": new_id, "d": dept_pair["a"], "h": "h" * 64})
        return (await conn.execute(text(
            "SELECT count(*) FROM documents WHERE department_id = :d"),
            {"d": dept_pair["a"]})).scalar_one()

    assert _tx(archive_then_readd) == 2  # archived original + fresh copy


def test_only_one_active_ingest_job_per_document(dept_pair):
    async def two_jobs(conn):
        await conn.execute(text(
            "INSERT INTO ingest_jobs (id, document_id, status)"
            " VALUES (:i, :d, 'queued')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})
        await conn.execute(text(
            "INSERT INTO ingest_jobs (id, document_id, status)"
            " VALUES (:i, :d, 'running')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})

    with pytest.raises(Exception) as exc:
        _tx(two_jobs)
    assert "ux_ingest_jobs_active_document" in str(exc.value)


def test_finished_jobs_do_not_block_a_new_one(dept_pair):
    async def finished_then_new(conn):
        await conn.execute(text(
            "INSERT INTO ingest_jobs (id, document_id, status)"
            " VALUES (:i, :d, 'succeeded')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})
        await conn.execute(text(
            "INSERT INTO ingest_jobs (id, document_id, status)"
            " VALUES (:i, :d, 'queued')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})
        return (await conn.execute(text(
            "SELECT count(*) FROM ingest_jobs WHERE document_id = :d"),
            {"d": dept_pair["doc"]})).scalar_one()

    assert _tx(finished_then_new) == 2


def test_department_with_documents_cannot_be_deleted(dept_pair):
    async def drop(conn):
        await conn.execute(text("DELETE FROM departments WHERE id = :a"),
                           {"a": dept_pair["a"]})

    with pytest.raises(Exception) as exc:
        _tx(drop)
    assert "foreign key" in str(exc.value).lower()


def test_chunk_tsv_is_populated_and_stems_english(dept_pair):
    async def insert_and_read(conn):
        await conn.execute(text(
            "INSERT INTO document_chunks (document_id, department_id, chunk_index,"
            " content, embedding) VALUES (:d, :dept, 1, 'annual leave loans',"
            " CAST(:v AS vector))"),
            {"d": dept_pair["doc"], "dept": dept_pair["a"], "v": VEC})
        return (await conn.execute(text(
            "SELECT tsv::text FROM document_chunks WHERE document_id = :d"
            " AND chunk_index = 1"), {"d": dept_pair["doc"]})).scalar_one()

    tsv = _tx(insert_and_read)
    assert "loan" in tsv and "loans" not in tsv  # 'english' stemmed it


def test_bad_document_status_is_rejected(dept_pair):
    """Without this CHECK, a typo'd status matches no partial-index predicate
    and silently escapes the dedup guarantee."""
    async def bad(conn):
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'archived_')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["a"], "h": "z" * 64})

    with pytest.raises(Exception) as exc:
        _tx(bad)
    assert "ck_documents_status" in str(exc.value)


def test_bad_ingest_status_cannot_bypass_the_active_job_index(dept_pair):
    """'runnning' would match neither the predicate nor the claim query."""
    async def bad(conn):
        await conn.execute(text(
            "INSERT INTO ingest_jobs (id, document_id, status)"
            " VALUES (:i, :d, 'runnning')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})

    with pytest.raises(Exception) as exc:
        _tx(bad)
    assert "ck_ingest_jobs_status" in str(exc.value)


def test_bad_document_source_is_rejected(dept_pair):
    async def bad(conn):
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status) VALUES (:i, :d, 'T', 'ftp', 'pdf', :h, 'ready')"),
            {"i": uuid.uuid4().hex, "d": dept_pair["a"], "h": "y" * 64})

    with pytest.raises(Exception) as exc:
        _tx(bad)
    assert "ck_documents_source" in str(exc.value)

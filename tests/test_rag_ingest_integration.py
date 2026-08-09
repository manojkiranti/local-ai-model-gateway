"""Atomic chunk replacement against real Postgres. Skips if the DB is down."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import documents as docs_repo
from app.rag import ingest
from app.rag.chunking import Chunk
from app.rag.models import STATUS_ARCHIVED, STATUS_READY

DIM = 1536


def _vec(seed: float):
    return [seed] * DIM


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
def doc():
    _skip_if_no_db()
    tag = uuid.uuid4().hex[:8]
    doc_id = uuid.uuid4().hex

    async def setup(conn):
        dept = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'I') RETURNING id"),
            {"c": f"ing{tag}"})).scalar_one()
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'pending')"),
            {"i": doc_id, "d": dept, "h": "a" * 64})
        return dept

    dept = _sql(setup)
    yield {"id": doc_id, "dept": dept}

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id = :d"),
                           {"d": dept})
        await conn.execute(text("DELETE FROM departments WHERE id = :d"), {"d": dept})
    _sql(teardown)


def _chunks(n, prefix="chunk"):
    return [Chunk(content=f"{prefix} {i}", chunk_index=i) for i in range(n)]


def test_replace_inserts_chunks_and_marks_the_document_ready(doc):
    async def go(s):
        n = await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(3), embeddings=[_vec(0.1)] * 3,
            embed_model="m", embed_dim=DIM)
        await s.commit()
        row = await docs_repo.get_document(s, doc["id"])
        count = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()
        return n, row.status, row.chunk_count, row.embed_model, row.embed_dim, count

    n, status, chunk_count, model, dim, count = _run(go)
    assert n == 3 and count == 3
    assert status == STATUS_READY and chunk_count == 3
    assert model == "m" and dim == DIM


def test_re_ingest_replaces_rather_than_appends(doc):
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(5, "old"), embeddings=[_vec(0.1)] * 5,
            embed_model="m", embed_dim=DIM)
        await s.commit()
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(2, "new"), embeddings=[_vec(0.2)] * 2,
            embed_model="m", embed_dim=DIM)
        await s.commit()
        return (await s.execute(text(
            "SELECT content FROM document_chunks WHERE document_id = :d"
            " ORDER BY chunk_index"), {"d": doc["id"]})).scalars().all()

    rows = _run(go)
    assert len(rows) == 2
    assert all(r.startswith("new") for r in rows)


def test_a_failed_replacement_leaves_the_previous_version_intact(doc):
    """The reason parse/embed happen OUTSIDE the transaction: a re-ingest that
    blows up must keep serving the last complete version."""
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(4, "good"), embeddings=[_vec(0.1)] * 4,
            embed_model="m", embed_dim=DIM)
        await s.commit()

        # Wrong-width vector -> refused before the INSERT.
        with pytest.raises(ValueError):
            await ingest.replace_chunks(
                s, document_id=doc["id"], department_id=doc["dept"],
                chunks=_chunks(3, "bad"), embeddings=[[0.1] * 999] * 3,
                embed_model="m", embed_dim=DIM)
        await s.rollback()

        return (await s.execute(text(
            "SELECT content FROM document_chunks WHERE document_id = :d"
            " ORDER BY chunk_index"), {"d": doc["id"]})).scalars().all()

    rows = _run(go)
    assert len(rows) == 4
    assert all(r.startswith("good") for r in rows)


def test_mismatched_chunk_and_embedding_counts_are_rejected(doc):
    async def go(s):
        with pytest.raises(ValueError):
            await ingest.replace_chunks(
                s, document_id=doc["id"], department_id=doc["dept"],
                chunks=_chunks(3), embeddings=[_vec(0.1)] * 2,
                embed_model="m", embed_dim=DIM)
        return True

    assert _run(go) is True


def test_a_wrong_width_embedding_is_refused_before_the_insert(doc):
    async def go(s):
        with pytest.raises(ValueError):
            await ingest.replace_chunks(
                s, document_id=doc["id"], department_id=doc["dept"],
                chunks=_chunks(1), embeddings=[[0.1] * 768],
                embed_model="m", embed_dim=DIM)
        return True

    assert _run(go) is True


def test_batching_handles_more_than_one_statement_worth(doc):
    """Exercises the >500-row path in one transaction."""
    n = ingest.CHUNK_INSERT_BATCH + 25

    async def go(s):
        written = await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(n), embeddings=[_vec(0.05)] * n,
            embed_model="m", embed_dim=DIM)
        await s.commit()
        count = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()
        return written, count

    written, count = _run(go)
    assert written == n == count


def test_stored_chunks_carry_the_documents_department(doc):
    """The composite FK would reject anything else — this proves we pass it."""
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(2), embeddings=[_vec(0.1)] * 2,
            embed_model="m", embed_dim=DIM)
        await s.commit()
        return (await s.execute(text(
            "SELECT DISTINCT department_id FROM document_chunks"
            " WHERE document_id = :d"), {"d": doc["id"]})).scalars().all()

    assert _run(go) == [doc["dept"]]


def test_tsv_is_generated_for_stored_chunks(doc):
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=[Chunk(content="annual leave loans", chunk_index=0)],
            embeddings=[_vec(0.1)], embed_model="m", embed_dim=DIM)
        await s.commit()
        return (await s.execute(text(
            "SELECT tsv::text FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()

    tsv = _run(go)
    assert "loan" in tsv and "loans" not in tsv


def test_replacing_an_archived_document_is_refused(doc):
    """The resurrection guard: an archive that landed while we were embedding
    must not be undone by the replacement that follows."""
    async def go(s):
        await docs_repo.archive_document(s, doc["id"])
        await s.commit()
        with pytest.raises(ingest.DocumentGone):
            await ingest.replace_chunks(
                s, document_id=doc["id"], department_id=doc["dept"],
                chunks=_chunks(2), embeddings=[_vec(0.1)] * 2,
                embed_model="m", embed_dim=DIM)
        await s.rollback()
        status = (await s.execute(text(
            "SELECT status FROM documents WHERE id = :d"),
            {"d": doc["id"]})).scalar_one()
        count = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()
        return status, count

    status, count = _run(go)
    assert status == STATUS_ARCHIVED
    assert count == 0


def test_archiving_removes_chunks_but_keeps_the_row(doc):
    """Archived documents must stop being retrievable — chunks carry no status
    and HNSW filters before a join would be reachable."""
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(3), embeddings=[_vec(0.1)] * 3,
            embed_model="m", embed_dim=DIM)
        await s.commit()
        archived = await docs_repo.archive_document(s, doc["id"])
        await s.commit()
        row = await docs_repo.get_document(s, doc["id"])
        count = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()
        return archived, row.status, row.chunk_count, count

    archived, status, chunk_count, count = _run(go)
    assert archived is True
    assert status == STATUS_ARCHIVED
    assert count == 0
    assert chunk_count == 3   # retained for audit


def test_archiving_frees_the_content_hash_for_re_upload(doc):
    async def go(s):
        await docs_repo.archive_document(s, doc["id"])
        await s.commit()
        fresh = await docs_repo.create_document(
            s, department_id=doc["dept"], title="again", source="upload",
            file_type="pdf", content_hash="a" * 64)
        await s.commit()
        return fresh.id != doc["id"]

    assert _run(go) is True


def test_duplicate_content_hash_in_one_department_is_a_conflict(doc):
    async def go(s):
        with pytest.raises(docs_repo.DocumentConflict):
            await docs_repo.create_document(
                s, department_id=doc["dept"], title="dupe", source="upload",
                file_type="pdf", content_hash="a" * 64)
            await s.commit()
        return True

    assert _run(go) is True


def test_content_hash_is_stable_and_content_addressed():
    assert docs_repo.content_hash_of(b"abc") == docs_repo.content_hash_of(b"abc")
    assert docs_repo.content_hash_of(b"abc") != docs_repo.content_hash_of(b"abd")
    assert len(docs_repo.content_hash_of(b"abc")) == 64


def test_list_documents_hides_archived_by_default(doc):
    async def go(s):
        await docs_repo.archive_document(s, doc["id"])
        await s.commit()
        visible = await docs_repo.list_documents(s, doc["dept"])
        everything = await docs_repo.list_documents(
            s, doc["dept"], include_archived=True)
        return len(visible), len(everything)

    visible, everything = _run(go)
    assert visible == 0 and everything == 1


def test_ready_only_hides_pending_documents(doc):
    """The member view: a pending document is not part of the corpus their
    answers can cite."""
    async def go(s):
        admin_view = await docs_repo.list_documents(s, doc["dept"])
        member_view = await docs_repo.list_documents(s, doc["dept"], ready_only=True)
        return len(admin_view), len(member_view)

    admin_view, member_view = _run(go)
    assert admin_view == 1     # pending, visible to admins
    assert member_view == 0    # not ready, hidden from members

"""Phase 7 corpus ingest driver against real Postgres. Skips if the DB is down.

ISOLATION — read this before adding a test
    Same discipline as `test_nrb_sync_integration.py`: one connection, one outer
    transaction that is ALWAYS rolled back, and every session joined to it with
    `join_transaction_mode="create_savepoint"` so the driver's own per-document
    commits become savepoint releases. That matters here because
    `create_ingest_targets` takes a session FACTORY and opens a session per
    document — the factory the tests hand it is bound to the same connection, so
    a test that inserts 3 documents and a department leaves nothing behind.

    The nrb_* tables are cleared inside that transaction (a global catalog has no
    department to scope a fixture to), and the RAG rows are scoped to a test-only
    department code so the intent is visible even though the rollback is what
    actually protects the developer's data.

No blob store: `filestore.resolve_path` is monkeypatched at the point
`app.nrb.corpus` calls it, so the "bytes on disk" are tmp_path files. Nothing
touches NRB_FILES_DIR and nothing is downloaded.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.nrb import corpus
from app.rag import documents as docs_repo
from app.rag import repository as dept_repo
from app.rag.models import STATUS_ARCHIVED, STATUS_READY

DEPT_CODE = "test-nrb-p7"
NRB_TABLES = ("nrb_source_files", "nrb_sources", "nrb_extractions", "nrb_files")


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
    """Run `fn(session, Session)` on a clean catalog, then roll everything back.

    `fn` gets both a session (for fixtures and assertions) and the factory the
    driver needs, bound to the SAME connection — otherwise the driver's commits
    would land outside the transaction under test and survive the rollback.
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
                    await session.execute(
                        text(
                            "DELETE FROM document_chunks WHERE department_id IN "
                            "(SELECT id FROM departments WHERE code = :c)"
                        ),
                        {"c": DEPT_CODE},
                    )
                    await session.execute(
                        text(
                            "DELETE FROM ingest_jobs WHERE document_id IN "
                            "(SELECT d.id FROM documents d JOIN departments dp "
                            " ON dp.id = d.department_id WHERE dp.code = :c)"
                        ),
                        {"c": DEPT_CODE},
                    )
                    await session.execute(
                        text(
                            "DELETE FROM documents WHERE department_id IN "
                            "(SELECT id FROM departments WHERE code = :c)"
                        ),
                        {"c": DEPT_CODE},
                    )
                    await session.execute(
                        text("DELETE FROM departments WHERE code = :c"),
                        {"c": DEPT_CODE},
                    )
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
async def _department(session):
    dept = await dept_repo.create_department(
        session, code=DEPT_CODE, name="Phase 7 corpus ingest test"
    )
    await session.flush()
    return dept


async def _blob(session, tmp: Path, body: bytes, *, key: str, extension: str = "pdf",
                status: str = "fetched", title: str | None = None) -> str:
    """One fetched `nrb_files` row whose bytes really exist under `tmp`."""
    sha = hashlib.sha256(body).hexdigest()
    storage_key = f"{sha[:2]}/{sha}.{extension}"
    path = tmp / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    await session.execute(
        text(
            """
            INSERT INTO nrb_files (comparison_key, source_url, filename, extension,
                                   resource_type, type_source, host, fetch_status,
                                   content_sha256, content_length, storage_key)
            VALUES (:k, :k, :fn, :ext, 'document', 'extension', 'www.nrb.org.np',
                    :st, :sha, :len, :key)
            """
        ),
        {
            "k": key, "fn": f"{key.rsplit('/', 1)[-1]}", "ext": extension,
            "st": status,
            "sha": sha if status == "fetched" else None,
            "len": len(body) if status == "fetched" else None,
            "key": storage_key if status == "fetched" else None,
        },
    )
    if title:
        await session.execute(
            text(
                """
                INSERT INTO nrb_sources (url_key, page_url, title, owner,
                                         page_kind, document_type, metadata_status,
                                         metadata_hash, is_active)
                VALUES (:u, :u, :t, 'bfr', 'document', 'circulars',
                        'rest', 'h', true)
                """
            ),
            {"u": f"{key}-page", "t": title},
        )
        await session.execute(
            text(
                """
                INSERT INTO nrb_source_files (source_id, file_id, ordinal,
                                              relationship_type)
                SELECT s.id, f.id, 0, 'primary'
                  FROM nrb_sources s, nrb_files f
                 WHERE s.url_key = :u AND f.comparison_key = :k
                """
            ),
            {"u": f"{key}-page", "k": key},
        )
    await session.flush()
    return sha


def _code_only(path: Path) -> str:
    """The module's CODE, with comments and docstrings removed.

    Via `ast`, not a text scan: this file's own prose explains at length why
    `nrb_extractions` is off the ingestion path, and a naive `"x" not in
    source` would fail on the explanation of the rule it is checking. Ordinary
    string literals are KEPT, because a raw SQL string naming the table is
    exactly the thing worth catching.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    return ast.unparse(tree)


def _patch_store(monkeypatch, tmp: Path) -> None:
    monkeypatch.setattr(
        corpus.filestore, "resolve_path", lambda key, base=None: tmp / key
    )


# --------------------------------------------------------------------------- #
# The headline property
# --------------------------------------------------------------------------- #
def test_a_second_pass_selects_nothing_creates_nothing_and_raises_nothing(
    tmp_path, monkeypatch
):
    """Running the driver twice over one scope is a no-op the second time.

    This is what makes an interrupted pass resumable rather than restartable,
    and it is the property the 8-blob smoke test does NOT have: that script
    calls `create_document` without catching `DocumentConflict`, so its second
    run aborts on the first already-ingested blob.
    """
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        for i in range(3):
            await _blob(session, tmp_path, f"pdf-body-{i}".encode(),
                        key=f"https://www.nrb.org.np/a/{i}.pdf", title=f"Doc {i}")
        await session.commit()

        first = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert len(first) == 3
        out1 = await corpus.create_ingest_targets(
            Session, department_id=dept.id, department_code=DEPT_CODE,
            targets=first, rag_docs_dir=str(tmp_path / "rag"),
        )
        assert (out1.created, out1.conflict_document, out1.errors) == (3, 0, [])

        # Second pass — same scope, no --reset, nothing raised.
        second = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert second == []
        out2 = await corpus.create_ingest_targets(
            Session, department_id=dept.id, department_code=DEPT_CODE,
            targets=second, rag_docs_dir=str(tmp_path / "rag"),
        )
        assert out2.created == 0
        assert out2.conflict_document == 0   # skipped by the anti-join, not caught
        assert out2.errors == []

        docs = (
            await session.execute(
                text("SELECT count(*) FROM documents WHERE department_id = :d"),
                {"d": dept.id},
            )
        ).scalar_one()
        jobs = (
            await session.execute(
                text(
                    "SELECT count(*) FROM ingest_jobs j JOIN documents d "
                    "ON d.id = j.document_id WHERE d.department_id = :d"
                ),
                {"d": dept.id},
            )
        ).scalar_one()
        assert (docs, jobs) == (3, 3)

    _run(body)


def test_every_created_document_gets_exactly_one_queued_job(tmp_path, monkeypatch):
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        await _blob(session, tmp_path, b"one", key="https://www.nrb.org.np/x.pdf",
                    title="X")
        await session.commit()
        targets = await corpus.select_ingest_targets(session, department_id=dept.id)
        out = await corpus.create_ingest_targets(
            Session, department_id=dept.id, department_code=DEPT_CODE,
            targets=targets, rag_docs_dir=str(tmp_path / "rag"),
        )
        assert out.created == 1
        row = (
            await session.execute(
                text(
                    "SELECT d.metadata->>'origin' AS origin, "
                    "       d.metadata->>'blob_sha256' AS sha, d.title, j.status "
                    "  FROM documents d JOIN ingest_jobs j ON j.document_id = d.id "
                    " WHERE d.department_id = :d"
                ),
                {"d": dept.id},
            )
        ).mappings().one()
        # The marker the worker branches on. Without it the blob would be parsed
        # generically and none of the NRB routing would run.
        assert row["origin"] == "nrb"
        assert row["sha"] == hashlib.sha256(b"one").hexdigest()
        assert row["title"] == "X"          # NRB's title, not the filename
        assert row["status"] == "queued"

    _run(body)


# --------------------------------------------------------------------------- #
# Selection rules
# --------------------------------------------------------------------------- #
def test_two_catalog_keys_sharing_bytes_select_as_one_document(tmp_path, monkeypatch):
    """One blob republished under two URLs is ONE document, not a conflict.

    Selecting both would inflate `conflict_document` with something that is not
    a race — and the unique index would reject the second insert anyway.
    """
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        await _blob(session, tmp_path, b"same", key="https://www.nrb.org.np/a.pdf",
                    title="A")
        await _blob(session, tmp_path, b"same", key="https://www.nrb.org.np/b.pdf",
                    title="B")
        await session.commit()
        targets = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert len(targets) == 1
        out = await corpus.create_ingest_targets(
            Session, department_id=dept.id, department_code=DEPT_CODE,
            targets=targets, rag_docs_dir=str(tmp_path / "rag"),
        )
        assert (out.created, out.conflict_document) == (1, 0)

    _run(body)


def test_only_fetched_blobs_are_selectable(tmp_path, monkeypatch):
    """`pending` and `failed` rows are excluded by the STATUS COLUMN.

    Same rule as Phases 5 and 6: not by a WHERE clause someone could forget, but
    because the driver only ever asks for `fetched`. A pending row has no bytes
    on disk, so selecting one would be a guaranteed job failure.
    """
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        await _blob(session, tmp_path, b"ok", key="https://www.nrb.org.np/ok.pdf",
                    title="ok")
        await _blob(session, tmp_path, b"no1", key="https://www.nrb.org.np/p.pdf",
                    status="pending", title="pending")
        await _blob(session, tmp_path, b"no2", key="https://www.nrb.org.np/f.pdf",
                    status="failed", title="failed")
        await session.commit()
        targets = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert [t.title for t in targets] == ["ok"]

    _run(body)


def test_an_archived_document_is_selected_again(tmp_path, monkeypatch):
    """Archiving deletes the chunks but keeps the row; re-ingest must be possible.

    The anti-join repeats `ux_documents_active_content`'s own `status <>
    'archived'` predicate. Skipping archived rows here would make archiving
    permanent — the document could never be re-added.
    """
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        await _blob(session, tmp_path, b"arch", key="https://www.nrb.org.np/z.pdf",
                    title="Z")
        await session.commit()
        targets = await corpus.select_ingest_targets(session, department_id=dept.id)
        out = await corpus.create_ingest_targets(
            Session, department_id=dept.id, department_code=DEPT_CODE,
            targets=targets, rag_docs_dir=str(tmp_path / "rag"),
        )
        assert out.created == 1
        assert await corpus.select_ingest_targets(session, department_id=dept.id) == []

        await session.execute(
            text("UPDATE documents SET status = :s WHERE department_id = :d"),
            {"s": STATUS_ARCHIVED, "d": dept.id},
        )
        await session.commit()
        again = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert len(again) == 1

    _run(body)


def test_a_document_created_between_select_and_insert_is_a_conflict(
    tmp_path, monkeypatch
):
    """`DocumentConflict` means RACED, not "already done".

    The two are counted separately because a nonzero conflict count is evidence
    of concurrency — a second driver, or a manual upload of the same bytes —
    and reporting it as a skip would hide that.
    """
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        sha = await _blob(session, tmp_path, b"race",
                          key="https://www.nrb.org.np/r.pdf", title="R")
        await session.commit()
        targets = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert len(targets) == 1

        # Someone else gets there first, after the select.
        await docs_repo.create_document(
            session, department_id=dept.id, title="R (theirs)", source="upload",
            file_type="pdf", content_hash=sha, storage_key=f"{DEPT_CODE}/other.pdf",
            file_name="other.pdf",
        )
        await session.commit()

        out = await corpus.create_ingest_targets(
            Session, department_id=dept.id, department_code=DEPT_CODE,
            targets=targets, rag_docs_dir=str(tmp_path / "rag"),
        )
        assert (out.created, out.conflict_document) == (0, 1)
        assert out.errors == []
        # The file written before the insert failed must not be left behind.
        written = list((tmp_path / "rag" / DEPT_CODE).glob("*")) if (
            tmp_path / "rag" / DEPT_CODE
        ).exists() else []
        assert written == []

    _run(body)


def test_a_missing_blob_is_counted_and_the_batch_continues(tmp_path, monkeypatch):
    """One bad file must not take the pass down — the OLE2 case in miniature."""
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        await _blob(session, tmp_path, b"good", key="https://www.nrb.org.np/g.pdf",
                    title="good")
        sha = await _blob(session, tmp_path, b"gone", key="https://www.nrb.org.np/n.pdf",
                          title="gone")
        (tmp_path / f"{sha[:2]}/{sha}.pdf").unlink()
        await session.commit()

        targets = await corpus.select_ingest_targets(session, department_id=dept.id)
        assert len(targets) == 2
        out = await corpus.create_ingest_targets(
            Session, department_id=dept.id, department_code=DEPT_CODE,
            targets=targets, rag_docs_dir=str(tmp_path / "rag"),
        )
        assert (out.created, out.missing_blob) == (1, 1)
        assert len(out.errors) == 1

    _run(body)


def test_the_scope_is_the_comparison_key_and_a_limit_bounds_it(tmp_path, monkeypatch):
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        for i in range(4):
            await _blob(session, tmp_path, f"body-{i}".encode(),
                        key=f"https://www.nrb.org.np/k{i}.pdf", title=f"K{i}")
        await session.commit()
        scoped = await corpus.select_ingest_targets(
            session, department_id=dept.id,
            keys=["https://www.nrb.org.np/k1.pdf", "https://www.nrb.org.np/k3.pdf"],
        )
        assert sorted(t.title for t in scoped) == ["K1", "K3"]
        assert len(await corpus.select_ingest_targets(
            session, department_id=dept.id, limit=2)) == 2

    _run(body)


def test_a_ready_document_in_ANOTHER_department_does_not_block_this_one(
    tmp_path, monkeypatch
):
    """Dedup is per department, because the unique index is."""
    _patch_store(monkeypatch, tmp_path)

    async def body(session, Session):
        dept = await _department(session)
        other = await dept_repo.create_department(
            session, code=f"{DEPT_CODE}-other", name="other"
        )
        await session.flush()
        sha = await _blob(session, tmp_path, b"shared",
                          key="https://www.nrb.org.np/s.pdf", title="S")
        doc = await docs_repo.create_document(
            session, department_id=other.id, title="S", source="upload",
            file_type="pdf", content_hash=sha, storage_key="other/s.pdf",
            file_name="s.pdf",
        )
        await session.execute(
            text("UPDATE documents SET status = :s WHERE id = :i"),
            {"s": STATUS_READY, "i": doc.id},
        )
        await session.commit()
        assert len(await corpus.select_ingest_targets(
            session, department_id=dept.id)) == 1

    _run(body)


# --------------------------------------------------------------------------- #
# The decision this module is not allowed to walk back
# --------------------------------------------------------------------------- #
def test_the_driver_never_consults_the_extraction_evidence_table():
    """`nrb_extractions` must stay off the ingestion path (§19, 2026-08-17).

    A source-level check because the failure it guards against is someone adding
    a "skip blobs we know are unparseable" join in good faith. That would make
    every future ingest depend on a measurement pass having been run first, and
    it would silently narrow the corpus to whatever Phase 6 happened to profile.
    Recovery reuse belongs in a versioned recovery cache, not here.
    """
    code = _code_only(Path("app/nrb/corpus.py"))
    assert "nrb_extractions" not in code
    assert "NRBExtraction" not in code
    assert "extractor_version" not in code

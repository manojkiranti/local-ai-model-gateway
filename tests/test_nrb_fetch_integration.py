"""Phase 5 against real Postgres: selection, recording, and a whole fetch pass.

Two harnesses, deliberately different:

  * **Selection and recording** run in the rolled-back-transaction harness from
    `test_nrb_sync_integration` (savepoint-joined session, nrb_* cleared inside the
    transaction). They are pure queries against a known-empty catalog.

  * **`run_fetch` really commits**, because it manages its own sessions — so those
    tests scope themselves to a **unique owner code** and delete only their own rows
    afterwards. That isolation is not cosmetic: a test that asked for
    `--section circular` against a developer's populated catalog would select the
    1,295 real circulars and march through them with a mocked transport, writing
    nonsense hashes over real rows.

HTTP is always a `MockTransport` and the blob directory is always `tmp_path`; no
test here touches the network or `NRB_FILES_DIR`.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.nrb import catalog, fetch as fetch_mod, filestore
from app.nrb.models import (
    FETCH_BLOCKED_HOST,
    FETCH_FAILED,
    FETCH_FETCHED,
    FETCH_PENDING,
    FETCH_RUN_COMPLETED,
    FETCH_RUN_PARTIAL,
)
from tests.test_nrb_fetch import PDF_BODY, SOFT_404, XLSX_BODY, _init_with
from tests.test_nrb_sync_integration import _engine, _run, _skip_if_no_db

UPLOADS = "https://www.nrb.org.np/contents/uploads/2026/08"


# --------------------------------------------------------------------------- #
# Fixtures: rows straight into the catalog
# --------------------------------------------------------------------------- #
async def _seed(session, *, owner: str, files, section: str = "circular", active=True):
    """One source owning `files`; returns `{name: file_id}`.

    Raw SQL rather than the sync: these tests are about the fetch, and a
    hand-built row makes the fetch-relevant columns (reported MIME, extension,
    status) obvious at the point of use.
    """
    source_id = (
        await session.execute(
            text(
                "INSERT INTO nrb_sources (page_url, url_key, metadata_status,"
                " metadata_hash, owner, document_type, is_active) VALUES"
                " (:u, :u, 'rest', :h, :o, :s, :a) RETURNING id"
            ),
            {"u": f"https://www.nrb.org.np/{owner}/post/", "h": "0" * 64,
             "o": owner, "s": section, "a": active},
        )
    ).scalar_one()
    ids: dict[str, int] = {}
    for ordinal, spec in enumerate(files):
        file_id = (
            await session.execute(
                text(
                    "INSERT INTO nrb_files (comparison_key, source_url, filename,"
                    " reported_mime_type, extension, resource_type, type_source,"
                    " host, fetch_status, blocked_reason) VALUES"
                    " (:k, :k, :f, :m, :e, :r, 'mime', :h, :st, :br) RETURNING id"
                ),
                {
                    "k": spec["url"],
                    "f": spec.get("filename", "x.pdf"),
                    "m": spec.get("mime", "application/pdf"),
                    "e": spec.get("extension", "pdf"),
                    "r": spec.get("resource_type", "pdf"),
                    "h": httpx.URL(spec["url"]).host,
                    "st": spec.get("status", FETCH_PENDING),
                    "br": spec.get("blocked_reason"),
                },
            )
        ).scalar_one()
        ids[spec["name"]] = file_id
        await session.execute(
            text(
                "INSERT INTO nrb_source_files (source_id, file_id, ordinal,"
                " relationship_type) VALUES (:s, :f, :o, 'primary')"
            ),
            {"s": source_id, "f": file_id, "o": ordinal},
        )
    await session.flush()
    return ids


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_pending_files_are_selected_in_id_order():
    async def go(session):
        await _seed(session, owner="bfr", files=[
            {"name": "a", "url": f"{UPLOADS}/a.pdf"},
            {"name": "b", "url": f"{UPLOADS}/b.pdf"},
        ])
        targets = await catalog.select_fetch_targets(session)
        return [t.source_url for t in targets]

    urls = _run(go)
    assert urls == [f"{UPLOADS}/a.pdf", f"{UPLOADS}/b.pdf"]


def test_a_blocked_file_can_never_be_selected():
    """Not filtered out — excluded by construction, because the status list only
    ever holds `pending` (and `failed` on request). The three uat.nrb.org.np links
    are unreachable from every code path, not just from a WHERE clause."""
    async def go(session):
        await _seed(session, owner="bfr", files=[
            {"name": "ok", "url": f"{UPLOADS}/ok.pdf"},
            {"name": "uat", "url": "http://uat.nrb.org.np/x.pdf",
             "status": FETCH_BLOCKED_HOST, "blocked_reason": "refusing plain http"},
        ])
        plain = await catalog.select_fetch_targets(session)
        retried = await catalog.select_fetch_targets(session, retry_failed=True)
        return [t.source_url for t in plain], [t.source_url for t in retried]

    plain, retried = _run(go)
    assert plain == [f"{UPLOADS}/ok.pdf"]
    assert retried == [f"{UPLOADS}/ok.pdf"]


def test_a_fetched_file_is_not_selected_again():
    async def go(session):
        await _seed(session, owner="bfr", files=[
            {"name": "done", "url": f"{UPLOADS}/done.pdf"}])
        # Status and content columns must move together — the CHECK refuses a
        # `fetched` row that cannot name its bytes, which is why this is one
        # statement rather than two.
        await session.execute(text(
            "UPDATE nrb_files SET fetch_status = 'fetched', content_sha256 = :h,"
            " content_length = 1, storage_key = 'aa/x.pdf'"), {"h": "a" * 64})
        await session.flush()
        return await catalog.select_fetch_targets(session)

    assert _run(go) == []


def test_a_failed_file_is_selected_only_when_a_retry_is_asked_for():
    async def go(session):
        await _seed(session, owner="bfr", files=[
            {"name": "bad", "url": f"{UPLOADS}/bad.pdf", "status": FETCH_FAILED},
        ])
        return (
            await catalog.select_fetch_targets(session),
            await catalog.select_fetch_targets(session, retry_failed=True),
        )

    plain, retried = _run(go)
    assert plain == []
    assert len(retried) == 1


def test_selection_can_be_scoped_by_section_owner_and_type():
    async def go(session):
        await _seed(session, owner="bfr", section="circular", files=[
            {"name": "c", "url": f"{UPLOADS}/c.pdf"}])
        await _seed(session, owner="red", section="statistics", files=[
            {"name": "s", "url": f"{UPLOADS}/s.xlsx", "extension": "xlsx",
             "resource_type": "spreadsheet",
             "mime": "application/vnd.ms-excel"}])
        by_section = await catalog.select_fetch_targets(session, sections=["circular"])
        by_owner = await catalog.select_fetch_targets(session, owners=["red"])
        by_type = await catalog.select_fetch_targets(session, resource_types=["spreadsheet"])
        return (
            [t.source_url for t in by_section],
            [t.source_url for t in by_owner],
            [t.source_url for t in by_type],
        )

    by_section, by_owner, by_type = _run(go)
    assert by_section == [f"{UPLOADS}/c.pdf"]
    assert by_owner == [f"{UPLOADS}/s.xlsx"]
    assert by_type == [f"{UPLOADS}/s.xlsx"]


def test_a_file_referenced_by_several_sources_is_selected_once():
    async def go(session):
        ids = await _seed(session, owner="bfr", files=[
            {"name": "shared", "url": f"{UPLOADS}/shared.pdf"}])
        other = (
            await session.execute(
                text(
                    "INSERT INTO nrb_sources (page_url, url_key, metadata_status,"
                    " metadata_hash, owner, document_type) VALUES"
                    " ('https://www.nrb.org.np/psd/p/', 'https://www.nrb.org.np/psd/p',"
                    " 'rest', :h, 'psd', 'circular') RETURNING id"
                ),
                {"h": "1" * 64},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO nrb_source_files (source_id, file_id, ordinal,"
                " relationship_type) VALUES (:s, :f, 0, 'primary')"
            ),
            {"s": other, "f": ids["shared"]},
        )
        return await catalog.select_fetch_targets(session, sections=["circular"])

    assert len(_run(go)) == 1


def test_a_file_only_reachable_from_a_deactivated_source_is_not_fetched():
    """NRB withdrew the post; downloading its attachment now would be work done to
    add something upstream no longer publishes."""
    async def go(session):
        await _seed(session, owner="bfr", active=False, files=[
            {"name": "gone", "url": f"{UPLOADS}/gone.pdf"}])
        default = await catalog.select_fetch_targets(session)
        explicit = await catalog.select_fetch_targets(session, include_inactive=True)
        return default, explicit

    default, explicit = _run(go)
    assert default == []
    assert len(explicit) == 1


def test_the_limit_takes_the_oldest_rows_so_a_pass_resumes():
    async def go(session):
        await _seed(session, owner="bfr", files=[
            {"name": str(n), "url": f"{UPLOADS}/{n}.pdf"} for n in range(5)])
        first = await catalog.select_fetch_targets(session, limit=2)
        return [t.id for t in first]

    ids = _run(go)
    assert len(ids) == 2 and ids == sorted(ids)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_a_mixed_batch_of_outcomes_is_recorded_correctly():
    """Success rows carry the content columns, failures omit them, blocked rows add
    a reason — three different key sets in one call, which is what
    `record_fetch_outcomes` groups for. Passing them as one batch would otherwise
    raise or silently drop keys."""
    async def go(session):
        ids = await _seed(session, owner="bfr", files=[
            {"name": "ok", "url": f"{UPLOADS}/ok.pdf"},
            {"name": "bad", "url": f"{UPLOADS}/bad.pdf"},
            {"name": "off", "url": f"{UPLOADS}/off.pdf"},
        ])
        run_id, now = await catalog.create_fetch_run(session, scope={})
        rows = [
            {"_id": ids["ok"], "fetch_status": FETCH_FETCHED, "fetch_attempts": 1,
             "http_status": 200, "last_fetch_run_id": run_id,
             "sniffed_mime": "application/pdf", "fetch_error": None,
             "content_sha256": "e" * 64, "content_length": 42,
             "storage_key": "ee/x.pdf", "downloaded_at": now},
            {"_id": ids["bad"], "fetch_status": FETCH_FAILED, "fetch_attempts": 1,
             "http_status": 500, "last_fetch_run_id": run_id,
             "sniffed_mime": None, "fetch_error": "HTTP 500"},
            {"_id": ids["off"], "fetch_status": FETCH_BLOCKED_HOST, "fetch_attempts": 1,
             "http_status": None, "last_fetch_run_id": run_id, "sniffed_mime": None,
             "fetch_error": "off host", "blocked_reason": "off host"},
        ]
        await catalog.record_fetch_outcomes(session, rows)
        await session.flush()
        got = (await session.execute(text(
            "SELECT fetch_status, content_sha256, content_length, storage_key,"
            " downloaded_at IS NOT NULL, fetch_error, blocked_reason, fetch_attempts"
            " FROM nrb_files ORDER BY id"))).all()
        return got

    ok, bad, off = _run(go)
    assert ok[0] == FETCH_FETCHED and ok[1] == "e" * 64 and ok[2] == 42
    assert ok[3] == "ee/x.pdf" and ok[4] is True and ok[7] == 1
    assert bad[0] == FETCH_FAILED and bad[1] is None and bad[5] == "HTTP 500"
    assert off[0] == FETCH_BLOCKED_HOST and off[6] == "off host"


def test_a_fetched_row_without_its_bytes_is_rejected_by_the_check():
    """`ck_nrb_files_fetched_is_complete`: a row claiming to be fetched that cannot
    say WHICH bytes it has would read to Phase 6 as available and resolve to
    nothing."""
    async def go(session):
        await _seed(session, owner="bfr", files=[{"name": "x", "url": f"{UPLOADS}/x.pdf"}])
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text("UPDATE nrb_files SET fetch_status = 'fetched'")
            )
            await session.flush()
        await session.rollback()
        return True

    assert _run(go) is True


def test_an_unknown_fetch_status_is_still_rejected_after_the_vocabulary_widened():
    async def go(session):
        await _seed(session, owner="bfr", files=[{"name": "x", "url": f"{UPLOADS}/x.pdf"}])
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text("UPDATE nrb_files SET fetch_status = 'downloading'")
            )
            await session.flush()
        await session.rollback()
        return True

    assert _run(go) is True


def test_fetch_counts_reports_the_disk_footprint_over_distinct_blobs():
    """Two rows sharing a sha256 share one file on disk; summing rows would
    overstate it."""
    async def go(session):
        await _seed(session, owner="bfr", files=[
            {"name": "a", "url": f"{UPLOADS}/a.pdf"},
            {"name": "b", "url": f"{UPLOADS}/b.pdf"},
        ])
        await session.execute(text(
            "UPDATE nrb_files SET fetch_status='fetched', content_sha256 = :h,"
            " content_length = 100, storage_key = 'aa/a.pdf'"), {"h": "a" * 64})
        await session.flush()
        return await catalog.fetch_counts(session)

    counts = _run(go)
    assert counts["fetched"] == 2
    assert counts["distinct_blobs"] == 1
    assert counts["bytes_on_disk"] == 100        # not 200


# --------------------------------------------------------------------------- #
# A whole pass (real commits, owner-scoped, cleaned up)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def corpus():
    """A source with three files under a unique owner code, removed afterwards.

    The unique owner is the isolation: every `run_fetch` below scopes to it, so a
    developer's real catalog is never selected. See the module docstring.
    """
    _skip_if_no_db()
    owner = f"zz{uuid.uuid4().hex[:8]}"
    tag = uuid.uuid4().hex[:8]
    files = [
        {"name": "pdf", "url": f"{UPLOADS}/{tag}-a.pdf"},
        {"name": "twin", "url": f"{UPLOADS}/{tag}-b.pdf"},
        {"name": "missing", "url": f"{UPLOADS}/{tag}-gone.pdf"},
    ]

    async def setup():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                ids = await _seed(session, owner=owner, files=files)
                await session.commit()
                return ids
        finally:
            await engine.dispose()

    ids = asyncio.run(setup())
    yield {"owner": owner, "ids": ids, "urls": {f["name"]: f["url"] for f in files}}

    async def teardown():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "DELETE FROM nrb_source_files WHERE file_id = ANY(:ids)"),
                    {"ids": list(ids.values())})
                await conn.execute(text(
                    "DELETE FROM nrb_files WHERE id = ANY(:ids)"),
                    {"ids": list(ids.values())})
                await conn.execute(text(
                    "DELETE FROM nrb_sources WHERE owner = :o"), {"o": owner})
                # Runs this test created: identified by their recorded scope.
                await conn.execute(text(
                    "DELETE FROM nrb_fetch_runs WHERE scope->'owners' ? :o"),
                    {"o": owner})
        finally:
            await engine.dispose()

    asyncio.run(teardown())


def _pass(monkeypatch, tmp_path, corpus, **kwargs):
    """Run a real `run_fetch` scoped to the fixture's owner, with mocked HTTP."""
    urls = corpus["urls"]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == urls["missing"]:
            # WordPress's soft 404: 200 OK with a themed HTML page.
            return httpx.Response(200, content=SOFT_404)
        # The other two serve IDENTICAL bytes under different URLs.
        return httpx.Response(200, content=PDF_BODY)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))
    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path)

    async def go():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            return await fetch_mod.run_fetch(
                owners=[corpus["owner"]], engine=engine, session_factory=factory,
                **kwargs,
            )
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _files(corpus):
    async def go():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                rows = (await conn.execute(text(
                    "SELECT id, fetch_status, content_sha256, content_length,"
                    " storage_key, sniffed_mime, fetch_error, fetch_attempts,"
                    " http_status, downloaded_at IS NOT NULL"
                    " FROM nrb_files WHERE id = ANY(:ids) ORDER BY id"),
                    {"ids": list(corpus["ids"].values())})).all()
            return {row[0]: row for row in rows}
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_a_pass_downloads_records_and_deduplicates(monkeypatch, tmp_path, corpus):
    result = _pass(monkeypatch, tmp_path, corpus)
    rows = _files(corpus)
    ids = corpus["ids"]

    assert result.status == FETCH_RUN_PARTIAL          # the soft-404 failed
    assert result.counters["files_selected"] == 3
    assert result.counters["files_fetched"] == 2
    assert result.counters["files_failed"] == 1
    # Identical bytes under two URLs: downloaded twice, stored once.
    assert result.counters["files_deduplicated"] == 1
    assert result.counters["bytes_downloaded"] == 2 * len(PDF_BODY)
    assert result.counters["bytes_stored"] == len(PDF_BODY)

    good = rows[ids["pdf"]]
    twin = rows[ids["twin"]]
    assert good[1] == FETCH_FETCHED and twin[1] == FETCH_FETCHED
    assert good[2] == twin[2]                          # same sha256
    assert good[4] == twin[4]                          # same storage key
    assert good[5] == "application/pdf" and good[9] is True
    # Exactly one blob on disk for the two rows.
    blobs = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".part"]
    assert len(blobs) == 1

    missing = rows[ids["missing"]]
    assert missing[1] == FETCH_FAILED
    assert "soft 404" in missing[6]
    assert missing[2] is None and missing[4] is None
    assert missing[7] == 1                             # attempt counted


def test_a_second_pass_has_nothing_left_to_do(monkeypatch, tmp_path, corpus):
    """Idempotency: fetched rows are not re-selected, so a repeat pass only sees
    what genuinely still needs fetching (here, nothing — the failure is excluded
    until a retry is asked for)."""
    _pass(monkeypatch, tmp_path, corpus)
    second = _pass(monkeypatch, tmp_path, corpus)
    assert second.counters["files_selected"] == 0
    assert second.counters["files_fetched"] == 0
    assert second.status == FETCH_RUN_COMPLETED


def test_retry_failed_re_attempts_only_the_failure(monkeypatch, tmp_path, corpus):
    _pass(monkeypatch, tmp_path, corpus)
    retry = _pass(monkeypatch, tmp_path, corpus, retry_failed=True)
    assert retry.counters["files_selected"] == 1
    assert retry.counters["files_failed"] == 1
    rows = _files(corpus)
    assert rows[corpus["ids"]["missing"]][7] == 2       # attempts accumulate


def test_a_byte_budget_stops_the_pass_and_reports_what_was_skipped(
    monkeypatch, tmp_path, corpus
):
    result = _pass(monkeypatch, tmp_path, corpus, max_bytes=1)
    assert result.counters["files_fetched"] == 1        # the budget is checked between files
    assert result.counters["files_skipped"] == 2
    assert result.status == FETCH_RUN_PARTIAL
    assert "byte budget reached" in result.notes["stopped"]


def test_the_run_row_records_the_scope_and_the_counters(monkeypatch, tmp_path, corpus):
    result = _pass(monkeypatch, tmp_path, corpus)

    async def go():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                return (await conn.execute(text(
                    "SELECT status, dry_run, scope, files_selected, files_fetched,"
                    " files_failed, files_deduplicated, bytes_downloaded,"
                    " bytes_stored, error_count, notes, completed_at IS NOT NULL"
                    " FROM nrb_fetch_runs WHERE id = :i"), {"i": result.run_id})).one()
        finally:
            await engine.dispose()

    row = asyncio.run(go())
    assert row[0] == FETCH_RUN_PARTIAL and row[1] is False
    assert row[2]["owners"] == [corpus["owner"]]
    assert (row[3], row[4], row[5], row[6]) == (3, 2, 1, 1)
    assert row[7] == 2 * len(PDF_BODY) and row[8] == len(PDF_BODY)
    assert row[9] == 1 and row[11] is True
    assert "soft 404" in row[10]["errors"][0]


def test_a_dry_run_makes_no_requests_and_writes_nothing(monkeypatch, tmp_path, corpus):
    """Unlike the sync's dry run, this one does not do the work and roll back: a
    rolled-back download would still have pulled the bytes, which is the cost being
    previewed."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=PDF_BODY)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))
    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path)

    async def go():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            return await fetch_mod.run_fetch(
                owners=[corpus["owner"]], dry_run=True,
                engine=engine, session_factory=factory,
            )
        finally:
            await engine.dispose()

    result = asyncio.run(go())
    assert requested == []
    assert result.run_id is None
    assert result.counters["files_selected"] == 3
    assert result.notes["reported_bytes_selected"] == 0   # the fixture reports no sizes
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    assert all(row[1] == FETCH_PENDING for row in _files(corpus).values())


def test_a_second_fetch_refuses_while_the_lock_is_held(monkeypatch, tmp_path, corpus):
    """Two passes would double the load on NRB and race on the same rows."""
    _skip_if_no_db()

    def landmine(*a, **kw):
        raise AssertionError("selection ran while another fetch held the lock")

    monkeypatch.setattr(catalog, "select_fetch_targets", landmine)

    async def go():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.connect() as holder:
                await holder.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": fetch_mod.FETCH_LOCK_KEY},
                )
                await holder.commit()
                try:
                    with pytest.raises(fetch_mod.FetchBusy):
                        await fetch_mod.run_fetch(
                            owners=[corpus["owner"]], dry_run=True,
                            engine=engine, session_factory=factory,
                        )
                finally:
                    await holder.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": fetch_mod.FETCH_LOCK_KEY},
                    )
                    await holder.commit()
            return True
        finally:
            await engine.dispose()

    assert asyncio.run(go()) is True


def test_the_sync_and_fetch_locks_are_different_so_they_do_not_block_each_other():
    from app.nrb import locks

    assert locks.SYNC_LOCK_KEY != locks.FETCH_LOCK_KEY

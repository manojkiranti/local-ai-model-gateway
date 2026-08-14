"""NRB catalog reconciliation against real Postgres. Skips if the DB is down.

ISOLATION — read this before adding a test
    Every test runs inside a transaction that is **always rolled back**, with the
    session joined to it via `join_transaction_mode="create_savepoint"` so the
    service's own phase commits become savepoint releases. That matters more here
    than in the other integration suites for one specific reason: deactivation is
    a single unqualified `UPDATE nrb_sources`, and the catalog is global (there is
    no department to scope a fixture to). A test that really committed would
    deactivate a developer's entire live catalog.

    For the same reason each test starts by DELETEing the nrb_* tables — inside
    that rolled-back transaction. It gives every test a deterministic, empty
    catalog (the shrink-floor guard is a ratio against what is already stored, so
    leftover rows would change the outcome), and the developer's real data is
    untouched because the transaction never commits.

    Throwaway NullPool engine per test, as in the RAG suites: the app's
    module-level engine pools connections bound to the first event loop, and each
    `asyncio.run` makes a new one.

No network: discovery is always a fixture built from REST-shaped post dicts and
sitemap-shaped entries, run through the real `build_document` so the tests
exercise Phase 3's extraction too.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.nrb import catalog, sync
from app.nrb.classify import classify_url
from app.nrb.discovery import Discovery
from app.nrb.documents import build_document
from app.nrb.http import normalize_url
from app.nrb.models import (
    FETCH_BLOCKED_HOST,
    FETCH_PENDING,
    METADATA_STATUS_REST,
    METADATA_STATUS_SITEMAP_ONLY,
    REL_PRIMARY,
    REL_SECONDARY,
    RUN_COMPLETED,
    RUN_PARTIAL,
    NRBFile,
    NRBSource,
    NRBSourceFile,
    NRBSyncRun,
)
from app.nrb.records import page_key
from tests.test_nrb_catalog import (
    DEVANAGARI_FILE,
    ENCODED_FILE,
    TAXONOMY,
    wp_file,
    wp_post,
)

NRB_TABLES = ("nrb_source_files", "nrb_sources", "nrb_files", "nrb_sync_runs")


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
    """Run `fn(session)` against a clean catalog, then roll everything back."""
    _skip_if_no_db()

    async def main():
        engine = _engine()
        try:
            async with engine.connect() as connection:
                outer = await connection.begin()
                session = AsyncSession(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                )
                try:
                    for table in NRB_TABLES:
                        await session.execute(text(f"DELETE FROM {table}"))
                    await session.commit()
                    return await fn(session)
                finally:
                    await session.close()
                    await outer.rollback()
        finally:
            await engine.dispose()

    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def entry(url: str, lastmod: str | None = "2026-08-13T09:12:34+00:00"):
    return classify_url(
        url=url, normalized_url=normalize_url(url),
        source_sitemap="https://www.nrb.org.np/bfr-sitemap1.xml",
        last_modified=lastmod,
    )


def discovery_for(
    posts,
    *,
    sitemap=(),
    complete: bool = True,
    errors=(),
    truncated=(),
    rest_errors=(),
    rest_truncated=(),
) -> Discovery:
    """A Discovery as `discover_corpus` would have produced it.

    `errors`/`truncated` are the sitemap-side kind (they block deactivation);
    `rest_errors`/`rest_truncated` are the REST-side kind, which additionally
    suppress sitemap-only source creation. `discover_corpus` merges the latter
    into the former, so these fixtures do too.
    """
    documents = sorted(
        (build_document(post, taxonomy=TAXONOMY) for post in posts),
        key=lambda document: (document.url, document.post_id or 0),
    )
    found = Discovery(
        documents=documents,
        sitemaps_seen=60 if complete else 0,
        errors=list(errors) + list(rest_errors),
        truncated=list(truncated) + list(rest_truncated),
        rest_errors=list(rest_errors),
        rest_truncated=list(rest_truncated),
    )
    found.sitemap_documents = {page_key(e.normalized_url): e for e in sitemap}
    return found


async def apply(session, discovery, *, dry_run: bool = False):
    """Open a run, reconcile, close the run. What `run_sync` does around it."""
    run_id, seen_at = await catalog.create_run(session, dry_run=dry_run)
    if not dry_run:
        await session.commit()
    result = await sync.reconcile(
        session, discovery, run_id=run_id, seen_at=seen_at, dry_run=dry_run
    )
    if dry_run:
        await session.rollback()
        result.run_id = None
    else:
        await catalog.finish_run(
            session, run_id,
            status=result.status, counters=result.counters, notes=result.notes,
            discovery_complete=result.discovery_complete,
            deactivation_applied=result.deactivation_applied,
        )
        await session.commit()
    return result


async def sources(session) -> list[NRBSource]:
    return list(
        (
            await session.execute(
                select(NRBSource)
                .order_by(NRBSource.url_key)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def files(session) -> list[NRBFile]:
    return list(
        (
            await session.execute(
                select(NRBFile)
                .order_by(NRBFile.comparison_key)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def links(session) -> list[NRBSourceFile]:
    return list(
        (
            await session.execute(
                select(NRBSourceFile)
                .order_by(NRBSourceFile.source_id, NRBSourceFile.ordinal)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def count(session, model) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
    )


# --------------------------------------------------------------------------- #
# Source persistence
# --------------------------------------------------------------------------- #
def test_a_new_rest_source_is_persisted_with_nrbs_own_metadata():
    async def go(session):
        result = await apply(session, discovery_for([wp_post()]))
        rows = await sources(session)
        return result, rows[0]

    result, row = _run(go)
    assert result.counters["sources_created"] == 1
    assert row.wp_post_id == 9001 and row.wp_post_type == "bfr"
    assert row.page_url == "https://www.nrb.org.np/bfr/circular-15/"
    assert row.url_key == "https://www.nrb.org.np/bfr/circular-15"
    assert row.title == "एकीकृत निर्देशन – 2082"      # entity decoded, not transliterated
    assert row.owner == "bfr" and row.page_kind == "document_post"
    assert row.document_type == "circular" and row.sections == ["circular"]
    assert row.metadata_status == METADATA_STATUS_REST
    assert row.is_active is True
    assert row.published_at is not None and row.modified_at is not None
    assert row.metadata_hash and len(row.metadata_hash) == 64


def test_raw_taxonomy_and_acf_extras_are_queryable_after_the_sync():
    async def go(session):
        await apply(session, discovery_for([wp_post(categories=[11, 30])]))
        return (await sources(session))[0]

    row = _run(go)
    assert row.raw_taxonomy["category_ids"] == [11, 30]
    assert row.raw_taxonomy["category_slugs"] == ["2082-83", "notices"]
    assert row.meta["extras"]["circular_number"] == "15/082/83"


def test_an_unknown_document_type_is_stored_as_null():
    """The 2019 `upload-files` backlog. NULL, not a guess."""
    async def go(session):
        await apply(session, discovery_for([wp_post(categories=[20])]))
        return (await sources(session))[0]

    row = _run(go)
    assert row.document_type is None
    assert row.sections == []


def test_a_second_source_with_the_same_wordpress_id_is_rejected_by_the_index():
    """Identity is enforced by Postgres, not only by the sync's diff."""
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO nrb_sources (wp_post_id, wp_post_type, page_url,"
                    " url_key, metadata_status, metadata_hash) VALUES"
                    " (9001, 'bfr', 'https://www.nrb.org.np/bfr/dup/',"
                    " 'https://www.nrb.org.np/bfr/dup', 'rest', 'x')"
                )
            )
        await session.rollback()
        return True

    assert _run(go) is True


def test_two_sitemap_only_sources_can_coexist_because_the_index_is_partial():
    """A plain UNIQUE(wp_post_type, wp_post_id) would allow exactly one NULL id."""
    async def go(session):
        await apply(
            session,
            discovery_for(
                [],
                sitemap=[
                    entry("https://www.nrb.org.np/economic-review/vol-35/"),
                    entry("https://www.nrb.org.np/er-article/an-article/"),
                ],
            ),
        )
        rows = await sources(session)
        return [(r.wp_post_id, r.metadata_status) for r in rows]

    got = _run(go)
    assert got == [(None, METADATA_STATUS_SITEMAP_ONLY)] * 2


def test_a_sitemap_only_source_persists_the_gap_without_inventing_metadata():
    async def go(session):
        result = await apply(
            session,
            discovery_for(
                [], sitemap=[entry("https://www.nrb.org.np/economic-review/vol-35/")]
            ),
        )
        return result, (await sources(session))[0]

    result, row = _run(go)
    assert result.counters["sitemap_only_sources"] == 1
    assert row.metadata_status == METADATA_STATUS_SITEMAP_ONLY
    assert row.title is None and row.published_at is None
    assert row.wp_post_type == "economic-review"      # inferable from the path
    assert row.sitemap_lastmod is not None


def test_sitemap_urls_that_rest_already_returned_do_not_become_extra_rows():
    """The 18,370-phantom-row trap: the sitemap spells the URL differently."""
    async def go(session):
        post = wp_post(link="https://www.nrb.org.np/bfr/आगलागी/", slug="आगलागी")
        encoded = (
            "https://www.nrb.org.np/bfr/"
            "%E0%A4%86%E0%A4%97%E0%A4%B2%E0%A4%BE%E0%A4%97%E0%A5%80/"
        )
        result = await apply(
            session, discovery_for([post], sitemap=[entry(encoded)])
        )
        return result, await sources(session)

    result, rows = _run(go)
    assert len(rows) == 1
    assert rows[0].metadata_status == METADATA_STATUS_REST
    assert result.counters["sitemap_only_sources"] == 0


def test_a_truncated_rest_pass_does_not_invent_thousands_of_sitemap_only_rows():
    """A bounded run sees 300 REST documents against 18,567 sitemap document URLs.
    Treating the difference as a corpus gap would fill the catalog with stubs."""
    async def go(session):
        post = wp_post()
        result = await apply(
            session,
            discovery_for(
                [post],
                sitemap=[
                    entry(post["link"]),
                    entry("https://www.nrb.org.np/bfr/not-yet-fetched/"),
                    entry("https://www.nrb.org.np/psd/also-not-fetched/"),
                ],
                rest_truncated=["limit=1"],
            ),
        )
        return result, await sources(session)

    result, rows = _run(go)
    assert len(rows) == 1                                   # only the REST document
    assert result.counters["sitemap_only_sources"] == 0
    assert any("not recording sitemap-only" in w for w in result.notes["warnings"])


def test_a_sitemap_only_row_is_upgraded_in_place_when_rest_starts_serving_it():
    """Same row id, post id filled in — not a duplicate."""
    async def go(session):
        url = "https://www.nrb.org.np/economic-review/vol-35/"
        await apply(session, discovery_for([], sitemap=[entry(url)]))
        before = (await sources(session))[0]
        post = wp_post(id=4242, type="economic-review", link=url, slug="vol-35")
        result = await apply(
            session, discovery_for([post], sitemap=[entry(url)])
        )
        rows = await sources(session)
        return before.id, before.first_seen_at, result, rows

    old_id, first_seen, result, rows = _run(go)
    assert len(rows) == 1
    assert rows[0].id == old_id                       # upgraded, not replaced
    assert rows[0].wp_post_id == 4242
    assert rows[0].metadata_status == METADATA_STATUS_REST
    assert rows[0].first_seen_at == first_seen        # the day we first saw it
    assert result.counters["sources_updated"] == 1
    assert result.counters["sources_created"] == 0


def test_a_known_rest_source_is_never_downgraded_to_sitemap_only():
    """If a post type drops out of REST for one run, its 5,400 sources must not
    lose their metadata and their attachments while the run calls itself clean."""
    async def go(session):
        post = wp_post()
        await apply(session, discovery_for([post], sitemap=[entry(post["link"])]))
        # REST returns nothing this time; the sitemap still lists the URL.
        result = await apply(
            session, discovery_for([], sitemap=[entry(post["link"])])
        )
        return result, (await sources(session))[0], await links(session)

    result, row, relations = _run(go)
    assert row.metadata_status == METADATA_STATUS_REST
    assert row.title is not None
    assert len(relations) == 1                        # attachment survived
    assert row.is_active is True                      # and it was stamped as seen
    assert any("REST did not return" in w for w in result.notes["warnings"])


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def test_an_attachment_becomes_a_file_row_with_its_reported_mime():
    async def go(session):
        result = await apply(session, discovery_for([wp_post()]))
        return result, (await files(session))[0]

    result, row = _run(go)
    assert result.counters["files_created"] == 1
    assert row.reported_mime_type == "application/pdf"
    assert row.resource_type == "pdf" and row.type_source == "mime"
    assert row.reported_bytes == 123456
    assert row.host == "www.nrb.org.np"
    assert row.fetch_status == FETCH_PENDING
    assert row.source_url == DEVANAGARI_FILE           # NRB's own spelling


def test_a_duplicate_comparison_key_is_rejected_by_the_index():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        key = (await files(session))[0].comparison_key
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO nrb_files (comparison_key, source_url,"
                    " resource_type, type_source, host) VALUES (:k, :k, 'pdf',"
                    " 'mime', 'www.nrb.org.np')"
                ),
                {"k": key},
            )
        await session.rollback()
        return True

    assert _run(go) is True


def test_an_encoding_only_url_change_does_not_create_a_second_file():
    """The single most important file-identity case."""
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        before = (await files(session))[0].id
        base = wp_post()
        respelled = wp_post(
            acf={**base["acf"], "document_file": wp_file(ENCODED_FILE)}
        )
        result = await apply(session, discovery_for([respelled]))
        rows = await files(session)
        return before, result, rows

    before, result, rows = _run(go)
    assert len(rows) == 1
    assert rows[0].id == before
    assert result.counters["files_created"] == 0
    assert result.counters["sources_updated"] == 0     # nor did the source change


def test_a_genuinely_different_file_path_creates_a_second_file():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        base = wp_post()
        moved = wp_post(
            acf={
                **base["acf"],
                "document_file": wp_file("https://www.nrb.org.np/uploads/other.pdf"),
            }
        )
        result = await apply(session, discovery_for([moved]))
        return result, await files(session), await links(session)

    result, rows, relations = _run(go)
    assert result.counters["files_created"] == 1
    assert len(rows) == 2                              # the old row is retained
    assert len(relations) == 1                         # but no longer referenced
    assert result.counters["relationships_removed"] == 1


def test_a_reported_mime_change_updates_the_file_and_not_the_source():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        base = wp_post()
        retyped = wp_post(
            acf={
                **base["acf"],
                "document_file": wp_file(
                    DEVANAGARI_FILE, mime_type="application/vnd.ms-excel"
                ),
            }
        )
        result = await apply(session, discovery_for([retyped]))
        return result, (await files(session))[0]

    result, row = _run(go)
    assert result.counters["files_updated"] == 1
    assert result.counters["sources_updated"] == 0
    assert row.reported_mime_type == "application/vnd.ms-excel"
    assert row.resource_type == "spreadsheet"


def test_the_uat_attachment_is_recorded_as_blocked_and_never_pending():
    async def go(session):
        post = wp_post(
            acf={
                "document_file": wp_file(
                    "http://uat.nrb.org.np/wp-content/uploads/2019/12/r.pdf"
                )
            }
        )
        result = await apply(session, discovery_for([post]))
        blocked = (await files(session))[0]
        fetchable = (
            await session.execute(
                select(func.count()).select_from(NRBFile).where(
                    NRBFile.fetch_status == FETCH_PENDING
                )
            )
        ).scalar_one()
        return result, blocked, fetchable

    result, row, fetchable = _run(go)
    assert result.counters["blocked_files"] == 1
    assert row.fetch_status == FETCH_BLOCKED_HOST
    assert row.blocked_reason
    assert row.host == "uat.nrb.org.np"
    # The whole point: it is in the catalog, and it is not in the work queue.
    assert fetchable == 0


def test_a_blocked_file_with_no_reason_is_rejected_by_the_check():
    async def go(session):
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO nrb_files (comparison_key, source_url,"
                    " resource_type, type_source, host, fetch_status) VALUES"
                    " ('k', 'k', 'pdf', 'mime', 'x', 'blocked_host')"
                )
            )
        await session.rollback()
        return True

    assert _run(go) is True


def test_an_unknown_fetch_status_is_rejected_by_the_check():
    async def go(session):
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO nrb_files (comparison_key, source_url,"
                    " resource_type, type_source, host, fetch_status) VALUES"
                    " ('k2', 'k2', 'pdf', 'mime', 'x', 'downloaded')"
                )
            )
        await session.rollback()
        return True

    assert _run(go) is True


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #
def test_a_source_with_no_attachment_has_no_relationship_rows():
    async def go(session):
        result = await apply(session, discovery_for([wp_post(acf=[])]))
        return result, await sources(session), await links(session)

    result, rows, relations = _run(go)
    assert len(rows) == 1 and relations == []
    assert result.counters["files_seen"] == 0


def test_a_two_file_source_keeps_both_in_nrbs_order():
    """One document in two parts — a circular and its annex — not two documents."""
    async def go(session):
        base = wp_post()
        post = wp_post(
            acf={
                **base["acf"],
                "secondary_file": wp_file("https://www.nrb.org.np/uploads/annex.pdf"),
            }
        )
        result = await apply(session, discovery_for([post]))
        return result, await sources(session), await links(session)

    result, rows, relations = _run(go)
    assert len(rows) == 1
    assert len(relations) == 2
    assert [r.ordinal for r in relations] == [0, 1]
    assert [r.relationship_type for r in relations] == [REL_PRIMARY, REL_SECONDARY]
    assert result.counters["relationships_created"] == 2


def test_a_duplicate_relationship_cannot_be_inserted():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        relation = (await links(session))[0]
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO nrb_source_files (source_id, file_id, ordinal,"
                    " relationship_type) VALUES (:s, :f, 9, 'primary')"
                ),
                {"s": relation.source_id, "f": relation.file_id},
            )
        await session.rollback()
        return True

    assert _run(go) is True


def test_the_same_file_may_belong_to_several_sources():
    """42 duplicate attachment references were measured across the live corpus."""
    async def go(session):
        first = wp_post()
        second = wp_post(
            id=9002, link="https://www.nrb.org.np/bfr/circular-16/", slug="circular-16"
        )
        result = await apply(session, discovery_for([first, second]))
        return result, await files(session), await links(session)

    result, rows, relations = _run(go)
    assert len(rows) == 1                       # one physical file
    assert len(relations) == 2                  # two sources referencing it
    assert result.counters["files_created"] == 1


def test_removing_one_of_two_attachments_removes_only_the_relationship():
    async def go(session):
        base = wp_post()
        two = wp_post(
            acf={
                **base["acf"],
                "secondary_file": wp_file("https://www.nrb.org.np/uploads/annex.pdf"),
            }
        )
        await apply(session, discovery_for([two]))
        result = await apply(session, discovery_for([wp_post()]))
        return result, await files(session), await links(session)

    result, rows, relations = _run(go)
    assert result.counters["relationships_removed"] == 1
    assert len(relations) == 1
    # The annex row is KEPT: it may be referenced elsewhere, it matters
    # historically, and Phase 5 may already have downloaded it.
    assert len(rows) == 2
    assert {r.filename for r in rows} == {DEVANAGARI_FILE.rsplit("/", 1)[-1], "annex.pdf"}


def test_a_shared_file_survives_one_source_dropping_it():
    async def go(session):
        first = wp_post()
        second = wp_post(
            id=9002, link="https://www.nrb.org.np/bfr/circular-16/", slug="circular-16"
        )
        await apply(session, discovery_for([first, second]))
        # The second post drops the shared file for a different one.
        base = wp_post()
        second_changed = wp_post(
            id=9002, link="https://www.nrb.org.np/bfr/circular-16/", slug="circular-16",
            acf={
                **base["acf"],
                "document_file": wp_file("https://www.nrb.org.np/uploads/new.pdf"),
            },
        )
        await apply(session, discovery_for([first, second_changed]))
        return await files(session), await links(session)

    rows, relations = _run(go)
    assert len(rows) == 2
    assert len(relations) == 2
    shared = [r for r in rows if r.source_url == DEVANAGARI_FILE][0]
    assert any(r.file_id == shared.id for r in relations)


def test_a_file_moving_from_annex_to_primary_is_an_update_not_a_rewrite():
    async def go(session):
        annex = "https://www.nrb.org.np/uploads/annex.pdf"
        base = wp_post()
        before = wp_post(
            acf={**base["acf"], "secondary_file": wp_file(annex)}
        )
        await apply(session, discovery_for([before]))
        created = {r.file_id: r.created_at for r in await links(session)}
        after = wp_post(
            acf={**base["acf"], "document_file": wp_file(annex), "secondary_file": False}
        )
        result = await apply(session, discovery_for([after]))
        return result, created, await links(session)

    result, created, relations = _run(go)
    promoted = [r for r in relations if r.relationship_type == REL_PRIMARY][0]
    assert result.counters["relationships_updated"] == 1
    assert promoted.ordinal == 0
    assert promoted.created_at == created[promoted.file_id]   # not re-created


def test_an_unknown_relationship_type_is_rejected_by_the_check():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        relation = (await links(session))[0]
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    "UPDATE nrb_source_files SET relationship_type = 'annexure'"
                    " WHERE source_id = :s"
                ),
                {"s": relation.source_id},
            )
        await session.rollback()
        return True

    assert _run(go) is True


def test_a_referenced_file_cannot_be_deleted():
    """`ON DELETE RESTRICT`: the physical catalog is the conservative half."""
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        file_id = (await files(session))[0].id
        with pytest.raises(IntegrityError):
            await session.execute(
                text("DELETE FROM nrb_files WHERE id = :i"), {"i": file_id}
            )
        await session.rollback()
        return True

    assert _run(go) is True


# --------------------------------------------------------------------------- #
# Idempotency — the acceptance test
# --------------------------------------------------------------------------- #
def test_an_identical_second_sync_changes_nothing_meaningful():
    async def go(session):
        posts = [
            wp_post(),
            wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16"),
        ]
        found = discovery_for(posts, sitemap=[entry(p["link"]) for p in posts])
        first = await apply(session, found)
        second = await apply(session, discovery_for(posts, sitemap=[entry(p["link"]) for p in posts]))
        return first, second, await sources(session), await files(session), await links(session)

    first, second, rows, file_rows, relations = _run(go)
    assert first.counters["sources_created"] == 2
    assert second.counters["sources_created"] == 0
    assert second.counters["sources_updated"] == 0
    assert second.counters["sources_unchanged"] == 2
    assert second.counters["files_created"] == 0
    assert second.counters["files_updated"] == 0
    assert second.counters["files_unchanged"] == 1
    assert second.counters["relationships_created"] == 0
    assert second.counters["relationships_removed"] == 0
    assert second.counters["sources_deactivated"] == 0
    # No duplicates crept in.
    assert len(rows) == 2 and len(file_rows) == 1 and len(relations) == 2


def test_the_second_sync_advances_last_seen_without_reporting_an_update():
    async def go(session):
        found = discovery_for([wp_post()])
        await apply(session, found)
        before = (await sources(session))[0]
        first_seen, last_seen = before.first_seen_at, before.last_seen_at
        result = await apply(session, discovery_for([wp_post()]))
        after = (await sources(session))[0]
        return result, first_seen, last_seen, after

    result, first_seen, last_seen, after = _run(go)
    assert result.counters["sources_updated"] == 0        # bookkeeping is not a change
    assert after.last_seen_at >= last_seen
    assert after.first_seen_at == first_seen              # never rewritten


def test_every_source_is_stamped_with_the_run_that_saw_it():
    async def go(session):
        result = await apply(session, discovery_for([wp_post()]))
        return result.run_id, (await sources(session))[0].last_sync_run_id

    run_id, stamped = _run(go)
    assert stamped == run_id


# --------------------------------------------------------------------------- #
# Update detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "change,column,expected",
    [
        ({"title": {"rendered": "New title"}}, "title", "New title"),
        ({"categories": [30]}, "document_type", "notice"),
    ],
)
def test_an_upstream_change_updates_the_stored_source(change, column, expected):
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        result = await apply(session, discovery_for([wp_post(**change)]))
        return result, (await sources(session))[0]

    result, row = _run(go)
    assert result.counters["sources_updated"] == 1
    assert result.counters["sources_created"] == 0
    assert getattr(row, column) == expected


def test_a_modified_date_change_is_an_update():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        before = (await sources(session))[0].modified_at
        result = await apply(
            session, discovery_for([wp_post(modified="2026-03-09T09:00:00")])
        )
        return result, before, (await sources(session))[0].modified_at

    result, before, after = _run(go)
    assert result.counters["sources_updated"] == 1
    assert after != before


def test_a_moved_url_follows_the_wordpress_id_rather_than_duplicating():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        moved = wp_post(link="https://www.nrb.org.np/bfr/circular-15-final/",
                        slug="circular-15-final")
        result = await apply(session, discovery_for([moved]))
        return result, await sources(session)

    result, rows = _run(go)
    assert len(rows) == 1
    assert rows[0].url_key.endswith("/circular-15-final")
    assert result.counters["sources_created"] == 0
    assert result.counters["sources_updated"] == 1


def test_a_url_collision_is_reported_and_leaves_both_rows_intact():
    """NRB withdrawing post A and moving post B onto A's URL.

    Updating B would violate `ux_nrb_sources_url_key` and abort a 1,000-row batch,
    failing an otherwise good sync. The row is left as it was and a human is told.
    """
    async def go(session):
        first = wp_post()
        second = wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16")
        await apply(session, discovery_for([first, second]))
        # A is gone; B now claims A's URL.
        collided = wp_post(id=9002, link=first["link"], slug="circular-15")
        result = await apply(session, discovery_for([collided]))
        return result, await sources(session)

    result, rows = _run(go)
    assert len(rows) == 2
    assert any("URL collision" in w for w in result.notes["warnings"])
    # Neither row was rewritten, and B is still recorded as seen (so the missing
    # A is the only thing deactivated).
    assert {r.url_key for r in rows} == {
        "https://www.nrb.org.np/bfr/circular-15",
        "https://www.nrb.org.np/bfr/c-16",
    }
    moved = [r for r in rows if r.wp_post_id == 9002][0]
    assert moved.is_active is True


def test_a_duplicate_url_inside_one_discovery_is_collapsed_not_fatal():
    async def go(session):
        first = wp_post()
        clash = wp_post(id=9002, link=first["link"], slug="circular-15")
        result = await apply(session, discovery_for([first, clash]))
        return result, await sources(session)

    result, rows = _run(go)
    assert len(rows) == 1
    assert any("duplicate source URL" in w for w in result.notes["warnings"])


# --------------------------------------------------------------------------- #
# Deactivation
# --------------------------------------------------------------------------- #
def test_a_source_that_disappears_is_deactivated_after_a_complete_sync():
    async def go(session):
        first = wp_post()
        second = wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16")
        await apply(session, discovery_for([first, second]))
        result = await apply(session, discovery_for([first]))
        return result, await sources(session)

    result, rows = _run(go)
    assert result.deactivation_applied is True
    assert result.counters["sources_deactivated"] == 1
    gone = [r for r in rows if r.url_key.endswith("c-16")][0]
    assert gone.is_active is False and gone.deactivated_at is not None
    assert len(rows) == 2                       # soft state: never hard-deleted


@pytest.mark.parametrize(
    "kwargs",
    [
        {"complete": False},
        {"errors": ["timeout on /api/wp/v2/bfr"]},
        {"truncated": ["limit=300"]},
    ],
)
def test_an_incomplete_sync_never_deactivates_anything(kwargs):
    """The safety rule that matters most: a network blip must not read as NRB
    deleting thousands of documents."""
    async def go(session):
        first = wp_post()
        second = wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16")
        await apply(session, discovery_for([first, second]))
        result = await apply(session, discovery_for([first], **kwargs))
        return result, await sources(session)

    result, rows = _run(go)
    assert result.deactivation_applied is False
    assert result.status == RUN_PARTIAL
    assert result.counters["sources_deactivated"] == 0
    assert all(r.is_active for r in rows)
    assert result.notes["deactivation_skipped"]


def test_a_sudden_corpus_shrink_is_refused_even_on_a_complete_run():
    """120 known sources, 100 discovered: below the 90% floor, so nothing is
    deactivated and the run says why."""
    async def go(session):
        many = [
            wp_post(id=n, link=f"https://www.nrb.org.np/bfr/doc-{n}/", slug=f"doc-{n}")
            for n in range(120)
        ]
        await apply(session, discovery_for(many))
        result = await apply(session, discovery_for(many[:100]))
        return result, await sources(session)

    result, rows = _run(go)
    assert result.deactivation_applied is False
    assert result.counters["sources_deactivated"] == 0
    assert "below the 90% floor" in result.notes["deactivation_skipped"]
    assert sum(1 for r in rows if r.is_active) == 120


def test_a_normal_shrink_above_the_floor_still_deactivates():
    async def go(session):
        many = [
            wp_post(id=n, link=f"https://www.nrb.org.np/bfr/doc-{n}/", slug=f"doc-{n}")
            for n in range(120)
        ]
        await apply(session, discovery_for(many))
        result = await apply(session, discovery_for(many[:115]))
        return result

    result = _run(go)
    assert result.deactivation_applied is True
    assert result.counters["sources_deactivated"] == 5


def test_a_reappearing_source_is_reactivated_and_keeps_its_first_seen():
    async def go(session):
        first = wp_post()
        second = wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16")
        await apply(session, discovery_for([first, second]))
        original = {r.url_key: r.first_seen_at for r in await sources(session)}
        await apply(session, discovery_for([first]))
        result = await apply(session, discovery_for([first, second]))
        rows = await sources(session)
        return result, original, rows

    result, original, rows = _run(go)
    assert result.counters["sources_reactivated"] == 1
    back = [r for r in rows if r.url_key.endswith("c-16")][0]
    assert back.is_active is True and back.deactivated_at is None
    assert back.first_seen_at == original[back.url_key]


def test_a_reappearing_source_with_changed_metadata_is_also_reactivated():
    async def go(session):
        first = wp_post()
        second = wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16")
        await apply(session, discovery_for([first, second]))
        await apply(session, discovery_for([first]))
        changed = wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/",
                          slug="c-16", title={"rendered": "Reissued"})
        result = await apply(session, discovery_for([first, changed]))
        rows = await sources(session)
        return result, [r for r in rows if r.url_key.endswith("c-16")][0]

    result, row = _run(go)
    assert result.counters["sources_reactivated"] == 1
    assert result.counters["sources_updated"] == 1
    assert row.is_active is True and row.title == "Reissued"


# --------------------------------------------------------------------------- #
# Sync runs
# --------------------------------------------------------------------------- #
def test_a_completed_run_records_its_counters_and_finish_time():
    async def go(session):
        posts = [
            wp_post(),
            wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16"),
        ]
        await apply(
            session,
            discovery_for(posts, sitemap=[
                entry(p["link"]) for p in posts
            ] + [entry("https://www.nrb.org.np/economic-review/vol-35/")]),
        )
        return (
            await session.execute(
                select(NRBSyncRun).order_by(NRBSyncRun.id.desc()).limit(1)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    run = _run(go)
    assert run.status == RUN_COMPLETED
    assert run.completed_at is not None
    assert run.discovery_complete is True and run.deactivation_applied is True
    assert run.sources_seen == 3 and run.sources_created == 3
    assert run.sitemap_only_sources == 1
    assert run.files_created == 1 and run.relationships_created == 2
    assert run.sitemaps_seen == 60
    assert run.notes["sitemap_document_urls"] == 3


def test_a_partial_run_is_recorded_as_partial_with_its_errors():
    async def go(session):
        await apply(
            session,
            discovery_for([wp_post()], errors=["timeout on /api/wp/v2/bfr"]),
        )
        return (
            await session.execute(
                select(NRBSyncRun).order_by(NRBSyncRun.id.desc()).limit(1)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    run = _run(go)
    assert run.status == RUN_PARTIAL
    assert run.error_count == 1
    assert "timeout" in run.notes["errors"][0]
    assert run.deactivation_applied is False


def test_a_blocked_file_is_counted_on_the_run():
    async def go(session):
        post = wp_post(
            acf={"document_file": wp_file("http://uat.nrb.org.np/uploads/r.pdf")}
        )
        await apply(session, discovery_for([post]))
        return (
            await session.execute(
                select(NRBSyncRun.blocked_files).order_by(NRBSyncRun.id.desc()).limit(1)
            )
        ).scalar_one()

    assert _run(go) == 1


def test_a_run_cannot_record_deactivating_on_an_incomplete_discovery():
    """The CHECK, not just the service, forbids the dangerous combination."""
    async def go(session):
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO nrb_sync_runs (status, discovery_complete,"
                    " deactivation_applied) VALUES ('completed', false, true)"
                )
            )
        await session.rollback()
        return True

    assert _run(go) is True


def test_an_unknown_run_status_is_rejected_by_the_check():
    async def go(session):
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text("INSERT INTO nrb_sync_runs (status) VALUES ('finished')")
            )
        await session.rollback()
        return True

    assert _run(go) is True


def test_catalog_counts_reports_zero_duplicates_and_the_real_totals():
    async def go(session):
        posts = [
            wp_post(),
            wp_post(id=9002, link="https://www.nrb.org.np/bfr/c-16/", slug="c-16",
                    acf={"document_file": wp_file("http://uat.nrb.org.np/x.pdf")}),
        ]
        await apply(
            session,
            discovery_for(posts, sitemap=[entry("https://www.nrb.org.np/er-article/a/")]),
        )
        return await catalog.catalog_counts(session)

    counts = _run(go)
    assert counts["sources"] == 3 and counts["active_sources"] == 3
    assert counts["rest_sources"] == 2 and counts["sitemap_only_sources"] == 1
    assert counts["files"] == 2 and counts["blocked_files"] == 1
    assert counts["relationships"] == 2
    assert counts["duplicate_source_identities"] == 0
    assert counts["duplicate_comparison_keys"] == 0


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #
def test_a_dry_run_reports_what_would_change_and_writes_nothing():
    async def go(session):
        result = await apply(session, discovery_for([wp_post()]), dry_run=True)
        return result, await count(session, NRBSource), await count(session, NRBFile), \
            await count(session, NRBSyncRun)

    result, source_rows, file_rows, runs = _run(go)
    assert result.counters["sources_created"] == 1     # it computed the change...
    assert result.counters["files_created"] == 1
    assert source_rows == 0 and file_rows == 0        # ...and kept none of it
    assert runs == 0                                  # not even the run row
    assert result.run_id is None


def test_a_dry_run_after_a_real_sync_reports_no_change():
    async def go(session):
        await apply(session, discovery_for([wp_post()]))
        result = await apply(session, discovery_for([wp_post()]), dry_run=True)
        return result, await count(session, NRBSource)

    result, rows = _run(go)
    assert result.counters["sources_created"] == 0
    assert result.counters["sources_updated"] == 0
    assert rows == 1


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_a_second_sync_refuses_while_the_advisory_lock_is_held():
    """Two syncs would interleave counters and race on the same rows."""
    _skip_if_no_db()

    async def go():
        engine = _engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.connect() as holder:
                await holder.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": sync.ADVISORY_LOCK_KEY},
                )
                await holder.commit()
                try:
                    with pytest.raises(sync.SyncBusy):
                        await sync.run_sync(
                            discovery=discovery_for([wp_post()], complete=False),
                            dry_run=True,
                            engine=engine,
                            session_factory=factory,
                        )
                finally:
                    await holder.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": sync.ADVISORY_LOCK_KEY},
                    )
                    await holder.commit()
            return True
        finally:
            await engine.dispose()

    assert asyncio.run(go()) is True


def test_a_blocked_sync_does_not_crawl_nrb_first(monkeypatch):
    """The lock is taken BEFORE discovery.

    Ordering it the other way still refuses correctly — but only after spending
    ~190 requests and several minutes on a central bank's website to build a result
    it throws away. `discover_corpus` is replaced with a landmine: if it is reached,
    the test fails with that error instead of SyncBusy.
    """
    _skip_if_no_db()

    async def landmine(**kwargs):
        raise AssertionError("discovery ran while another sync held the lock")

    monkeypatch.setattr(sync, "discover_corpus", landmine)

    async def go():
        engine = _engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.connect() as holder:
                await holder.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": sync.ADVISORY_LOCK_KEY},
                )
                await holder.commit()
                try:
                    with pytest.raises(sync.SyncBusy):
                        await sync.run_sync(
                            dry_run=True, engine=engine, session_factory=factory
                        )
                finally:
                    await holder.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": sync.ADVISORY_LOCK_KEY},
                    )
                    await holder.commit()
            return True
        finally:
            await engine.dispose()

    assert asyncio.run(go()) is True


def test_the_lock_is_released_after_a_run_so_the_next_one_can_start():
    """A dry run against an intentionally incomplete discovery: it must leave the
    lock free (and, being a dry run, the catalog untouched)."""
    _skip_if_no_db()

    async def go():
        engine = _engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            found = discovery_for([wp_post()], complete=False)
            first = await sync.run_sync(
                discovery=found, dry_run=True, engine=engine, session_factory=factory
            )
            second = await sync.run_sync(
                discovery=found, dry_run=True, engine=engine, session_factory=factory
            )
            async with engine.connect() as conn:
                held = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype ="
                            " 'advisory' AND ((classid::bigint << 32) |"
                            " objid::bigint) = :key"
                        ),
                        {"key": sync.ADVISORY_LOCK_KEY},
                    )
                ).scalar_one()
            return first, second, held
        finally:
            await engine.dispose()

    first, second, held = asyncio.run(go())
    assert first.status == RUN_PARTIAL and second.status == RUN_PARTIAL
    assert held == 0

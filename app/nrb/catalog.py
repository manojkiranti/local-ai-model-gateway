"""Data access for the NRB catalog. Set-based, and it never commits.

Convention, same as `history/repository.py`, `files/repository.py` and
`rag/repository.py`: every function takes an `AsyncSession` and leaves the
transaction boundary to its caller — here, `sync.py`, which owns the batching.

The shape of this module is dictated by one number: **18,370 sources and ~18,256
files, on every sync**. So there is no "get source by url, then decide" helper —
that is the N+1 pattern this would degenerate into. Instead the current state is
loaded once into an index (a few MB), the whole diff is computed in Python, and
each kind of change is applied as a set operation:

    load_source_index / load_file_index / load_relationships   3 SELECTs
    insert_*                                                   executemany + RETURNING
    update_*                                                   executemany by primary key
    touch_*                                                    ONE statement per batch
    delete_relationships                                       ONE statement per batch

`touch_*` is separated from `update_*` deliberately. Advancing `last_seen_at` on
18k unchanged rows is bookkeeping, and doing it with per-row parameters would be
18k parameter sets for two identical values; a single
`UPDATE … WHERE id = ANY(...)` is the same work in one statement. It also keeps
the counters honest — a touched row is not an updated row, which is exactly the
distinction the idempotency requirement turns on.

`RETURNING` includes the natural key (`url_key` / `comparison_key`) rather than
relying on insertmanyvalues preserving parameter order, so the id mapping is
correct by construction instead of by assumption.

**Every statement here is Core (`Table.insert()` / `Table.update()`), never the
ORM's `update(Model)` form, and that is not a style choice.** `nrb_sources` maps
the attribute `meta` onto the column named `metadata` (SQLAlchemy reserves
`metadata` on a declarative class). The two statement forms disagree about which
name a parameter dict should use — Core wants the column key `metadata`, the ORM
wants the attribute `meta` — and the failure mode is silent: an ORM bulk update
handed `{"metadata": ...}` **drops that key with no error and leaves the column
unchanged**. Measured, not theorised. Using Core throughout means one vocabulary
(column keys) for inserts and updates alike, so a mismatch cannot happen.
`Table.update().where(id == bindparam("_id"))` is the executemany-by-primary-key
form; the `_id` prefix keeps the WHERE parameter from colliding with a SET column.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import bindparam, delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    FETCH_BLOCKED_HOST,
    FETCH_FAILED,
    FETCH_FETCHED,
    FETCH_PENDING,
    FETCH_RUN_RUNNING,
    METADATA_STATUS_REST,
    NRBExtraction,
    NRBFetchRun,
    NRBFile,
    NRBSource,
    NRBSourceFile,
    NRBSyncRun,
    RUN_RUNNING,
)
from .records import FileRecord, SourceRecord

__all__ = [
    "ExtractTarget",
    "FetchTarget",
    "FileState",
    "ManifestResolution",
    "SourceIndex",
    "SourceState",
    "catalog_counts",
    "count_unfetched",
    "create_fetch_run",
    "create_run",
    "deactivate_unseen",
    "delete_relationships",
    "extraction_counts",
    "fetch_counts",
    "finish_fetch_run",
    "finish_run",
    "insert_files",
    "insert_relationships",
    "insert_sources",
    "load_file_index",
    "load_relationships",
    "load_sample_rows",
    "load_source_index",
    "record_extractions",
    "record_fetch_outcomes",
    "resolve_manifest_keys",
    "select_extract_targets",
    "select_fetch_targets",
    "touch_files",
    "touch_sources",
    "update_files",
    "update_relationships",
    "update_sources",
]

# Rows per statement. Bounds statement size and parameter count; atomicity comes
# from the surrounding transaction, exactly as in `rag/ingest.CHUNK_INSERT_BATCH`.
BATCH = 1000

# A manifest is a benchmark cohort, not a back door around Phase 5's
# scope-is-required rule. 5,000 keys is ~12x the planned 400-file sample and far
# under the 18,263-file corpus. Mirrors `manifest.MANIFEST_MAX_KEYS`, which
# refuses an oversized file before it is ever loaded; this is the same bound at
# the query, so a key list assembled some other way is bounded too.
MANIFEST_MAX_KEYS = 5000


# --------------------------------------------------------------------------- #
# Current state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceState:
    """The parts of an existing `nrb_sources` row a sync has to reason about."""

    id: int
    wp_post_id: int | None
    wp_post_type: str | None
    page_url: str
    url_key: str
    metadata_hash: str
    metadata_status: str
    is_active: bool


@dataclass
class SourceIndex:
    """Existing sources, addressable by both identities.

    `match` implements the resolution order that lets a sitemap-only row be
    *upgraded* rather than duplicated: WordPress's own id wins when we have one,
    and `url_key` is the fallback. A sitemap-only row has no post id, so when REST
    finally serves that post type the incoming record matches on `url_key` and
    fills the id in.
    """

    by_post: dict[tuple[str | None, int], SourceState]
    by_url_key: dict[str, SourceState]

    def match(self, record: SourceRecord) -> SourceState | None:
        if record.wp_post_id is not None:
            found = self.by_post.get((record.wp_post_type, record.wp_post_id))
            if found is not None:
                return found
        return self.by_url_key.get(record.url_key)

    def __len__(self) -> int:
        return len(self.by_url_key)


@dataclass(frozen=True)
class FileState:
    """An existing `nrb_files` row, and whether upstream has changed it."""

    id: int
    comparison_key: str
    source_url: str
    filename: str | None
    reported_mime_type: str | None
    extension: str | None
    resource_type: str
    type_source: str
    reported_bytes: int | None
    wp_attachment_id: int | None
    host: str
    fetch_status: str
    blocked_reason: str | None

    def differs_from(self, record: FileRecord) -> bool:
        """Whether any upstream fact about this file changed.

        Compared field by field rather than through a second hash: a file has a
        handful of fields, all of them upstream facts, so the comparison IS the
        explanation of what changed. (Sources need a hash because the payload
        includes nested taxonomy and extras.)
        """
        return (
            self.source_url != record.source_url
            or self.filename != record.filename
            or self.reported_mime_type != record.reported_mime_type
            or self.extension != record.extension
            or self.resource_type != record.resource_type
            or self.type_source != record.type_source
            or self.reported_bytes != record.reported_bytes
            or self.wp_attachment_id != record.wp_attachment_id
            or self.host != record.host
            or self.fetch_status != record.fetch_status
            or self.blocked_reason != record.blocked_reason
        )


async def load_source_index(session: AsyncSession) -> SourceIndex:
    """Every known source, indexed by both identities. One SELECT."""
    rows = (
        await session.execute(
            select(
                NRBSource.id,
                NRBSource.wp_post_id,
                NRBSource.wp_post_type,
                NRBSource.page_url,
                NRBSource.url_key,
                NRBSource.metadata_hash,
                NRBSource.metadata_status,
                NRBSource.is_active,
            )
        )
    ).all()
    by_post: dict[tuple[str | None, int], SourceState] = {}
    by_url_key: dict[str, SourceState] = {}
    for row in rows:
        state = SourceState(*row)
        by_url_key[state.url_key] = state
        if state.wp_post_id is not None:
            by_post[(state.wp_post_type, state.wp_post_id)] = state
    return SourceIndex(by_post=by_post, by_url_key=by_url_key)


async def load_file_index(session: AsyncSession) -> dict[str, FileState]:
    """Every known file, keyed by `comparison_key`. One SELECT."""
    rows = (
        await session.execute(
            select(
                NRBFile.id,
                NRBFile.comparison_key,
                NRBFile.source_url,
                NRBFile.filename,
                NRBFile.reported_mime_type,
                NRBFile.extension,
                NRBFile.resource_type,
                NRBFile.type_source,
                NRBFile.reported_bytes,
                NRBFile.wp_attachment_id,
                NRBFile.host,
                NRBFile.fetch_status,
                NRBFile.blocked_reason,
            )
        )
    ).all()
    return {row[1]: FileState(*row) for row in rows}


async def load_relationships(
    session: AsyncSession,
) -> dict[int, dict[int, tuple[int, str]]]:
    """`{source_id: {file_id: (ordinal, relationship_type)}}`. One SELECT."""
    rows = (
        await session.execute(
            select(
                NRBSourceFile.source_id,
                NRBSourceFile.file_id,
                NRBSourceFile.ordinal,
                NRBSourceFile.relationship_type,
            )
        )
    ).all()
    out: dict[int, dict[int, tuple[int, str]]] = {}
    for source_id, file_id, ordinal, relationship_type in rows:
        out.setdefault(source_id, {})[file_id] = (ordinal, relationship_type)
    return out


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def _file_values(record: FileRecord) -> dict[str, Any]:
    return {
        "comparison_key": record.comparison_key,
        "source_url": record.source_url,
        "filename": record.filename,
        "reported_mime_type": record.reported_mime_type,
        "extension": record.extension,
        "resource_type": record.resource_type,
        "type_source": record.type_source,
        "reported_bytes": record.reported_bytes,
        "wp_attachment_id": record.wp_attachment_id,
        "host": record.host,
        "fetch_status": record.fetch_status,
        "blocked_reason": record.blocked_reason,
    }


async def insert_files(
    session: AsyncSession,
    records: Sequence[FileRecord],
    *,
    seen_at: datetime,
    run_id: int | None,
) -> dict[str, int]:
    """Insert new files. Returns `{comparison_key: id}` for what was inserted."""
    if not records:
        return {}
    rows = [
        {
            **_file_values(record),
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "last_sync_run_id": run_id,
        }
        for record in records
    ]
    ids: dict[str, int] = {}
    for start in range(0, len(rows), BATCH):
        result = await session.execute(
            NRBFile.__table__.insert().returning(
                NRBFile.id, NRBFile.comparison_key
            ),
            rows[start : start + BATCH],
        )
        ids.update({key: file_id for file_id, key in result.all()})
    return ids


async def update_files(
    session: AsyncSession,
    changed: Sequence[tuple[int, FileRecord]],
    *,
    seen_at: datetime,
    run_id: int | None,
) -> None:
    """Apply upstream changes to known files, by primary key."""
    if not changed:
        return
    table = NRBFile.__table__
    statement = table.update().where(table.c.id == bindparam("_id"))
    rows = [
        {
            "_id": file_id,
            **_file_values(record),
            "last_seen_at": seen_at,
            "last_sync_run_id": run_id,
        }
        for file_id, record in changed
    ]
    for start in range(0, len(rows), BATCH):
        await session.execute(statement, rows[start : start + BATCH])


async def touch_files(
    session: AsyncSession,
    file_ids: Sequence[int],
    *,
    seen_at: datetime,
    run_id: int | None,
) -> None:
    """Record that these files were seen again, unchanged. One statement per batch."""
    for start in range(0, len(file_ids), BATCH):
        chunk = file_ids[start : start + BATCH]
        await session.execute(
            update(NRBFile)
            .where(NRBFile.id.in_(chunk))
            .values(last_seen_at=seen_at, last_sync_run_id=run_id)
        )


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _source_values(record: SourceRecord) -> dict[str, Any]:
    return {
        "wp_post_id": record.wp_post_id,
        "wp_post_type": record.wp_post_type,
        "page_url": record.page_url,
        "url_key": record.url_key,
        "canonical_url": record.canonical_url,
        "slug": record.slug,
        "title": record.title,
        "published_at": record.published_at,
        "modified_at": record.modified_at,
        "sitemap_lastmod": record.sitemap_lastmod,
        "owner": record.owner,
        "page_kind": record.page_kind,
        "document_type": record.document_type,
        "classification_source": record.classification_source,
        "sections": list(record.sections),
        "raw_taxonomy": record.raw_taxonomy,
        # The COLUMN key, not the `meta` attribute — see the module docstring.
        "metadata": record.meta,
        "metadata_status": record.metadata_status,
        "metadata_hash": record.metadata_hash,
    }


async def insert_sources(
    session: AsyncSession,
    records: Sequence[SourceRecord],
    *,
    seen_at: datetime,
    run_id: int | None,
) -> dict[str, int]:
    """Insert new sources. Returns `{url_key: id}` for what was inserted."""
    if not records:
        return {}
    rows = [
        {
            **_source_values(record),
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "last_sync_run_id": run_id,
            "is_active": True,
        }
        for record in records
    ]
    ids: dict[str, int] = {}
    for start in range(0, len(rows), BATCH):
        result = await session.execute(
            NRBSource.__table__.insert().returning(NRBSource.id, NRBSource.url_key),
            rows[start : start + BATCH],
        )
        ids.update({key: source_id for source_id, key in result.all()})
    return ids


async def update_sources(
    session: AsyncSession,
    changed: Sequence[tuple[int, SourceRecord]],
    *,
    seen_at: datetime,
    run_id: int | None,
) -> None:
    """Apply upstream changes to known sources, by primary key.

    `first_seen_at` is never in the payload — the day NRB first published a
    document is not something a later sync may overwrite. `is_active` is always
    set True here: a source that came back and also changed is reactivated by the
    same statement, so the two cases need no separate pass.
    """
    if not changed:
        return
    table = NRBSource.__table__
    statement = table.update().where(table.c.id == bindparam("_id"))
    rows = [
        {
            "_id": source_id,
            **_source_values(record),
            "last_seen_at": seen_at,
            "last_sync_run_id": run_id,
            "is_active": True,
            "deactivated_at": None,
        }
        for source_id, record in changed
    ]
    for start in range(0, len(rows), BATCH):
        await session.execute(statement, rows[start : start + BATCH])


async def touch_sources(
    session: AsyncSession,
    source_ids: Sequence[int],
    *,
    seen_at: datetime,
    run_id: int | None,
    reactivate: bool = False,
) -> None:
    """Record that these sources were seen again, unchanged.

    `reactivate` handles the source that vanished, was deactivated, and came back
    byte-identical: its metadata needs no update but it must stop being inactive.
    """
    values: dict[str, Any] = {"last_seen_at": seen_at, "last_sync_run_id": run_id}
    if reactivate:
        values.update({"is_active": True, "deactivated_at": None})
    for start in range(0, len(source_ids), BATCH):
        chunk = source_ids[start : start + BATCH]
        await session.execute(
            update(NRBSource).where(NRBSource.id.in_(chunk)).values(**values)
        )


async def deactivate_unseen(
    session: AsyncSession, *, run_id: int, seen_at: datetime
) -> int:
    """Deactivate every active source this run did not stamp. Returns the count.

    The predicate is `last_sync_run_id <> run_id`, which is what makes this one
    statement instead of an 18,000-element `NOT IN`: every source the run saw was
    stamped with its id by an insert, update or touch, so "not stamped" is exactly
    "not published upstream any more".

    **Only ever called for a complete discovery.** The caller enforces that, the
    `ck_nrb_sync_runs_deactivation_needs_complete` CHECK records it, and the
    reason is that a network blip during discovery would otherwise read as NRB
    deleting thousands of documents. Rows are never hard-deleted: NRB reorganises
    its site, and a deleted row loses the fact that the document ever existed.
    """
    result = await session.execute(
        update(NRBSource)
        .where(
            NRBSource.is_active.is_(True),
            (NRBSource.last_sync_run_id.is_(None))
            | (NRBSource.last_sync_run_id != run_id),
        )
        .values(is_active=False, deactivated_at=seen_at)
    )
    return result.rowcount or 0


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #
async def insert_relationships(
    session: AsyncSession, rows: Sequence[dict[str, Any]]
) -> None:
    if not rows:
        return
    for start in range(0, len(rows), BATCH):
        await session.execute(
            NRBSourceFile.__table__.insert(), list(rows[start : start + BATCH])
        )


async def update_relationships(
    session: AsyncSession, rows: Sequence[dict[str, Any]]
) -> None:
    """Fix an existing relationship whose ordinal or field changed.

    A file moving from `document_file` to `secondary_file` (or swapping order with
    its annex) is the same pair of rows with different meaning, so it is an update
    rather than a delete-plus-insert — which would lose `created_at`.
    """
    if not rows:
        return
    table = NRBSourceFile.__table__
    statement = table.update().where(
        table.c.source_id == bindparam("_source_id"),
        table.c.file_id == bindparam("_file_id"),
    )
    params = [
        {
            "_source_id": row["source_id"],
            "_file_id": row["file_id"],
            "ordinal": row["ordinal"],
            "relationship_type": row["relationship_type"],
        }
        for row in rows
    ]
    for start in range(0, len(params), BATCH):
        await session.execute(statement, params[start : start + BATCH])


async def delete_relationships(
    session: AsyncSession, pairs: Sequence[tuple[int, int]]
) -> None:
    """Remove `(source_id, file_id)` links. The FILE ROWS ARE NOT TOUCHED.

    That is the conservative half of the design: another source may reference the
    same file, it may matter historically, and Phase 5 may already have downloaded
    it. Only the claim "this source publishes this file" goes away.
    """
    if not pairs:
        return
    for start in range(0, len(pairs), BATCH):
        chunk = list(pairs[start : start + BATCH])
        await session.execute(
            delete(NRBSourceFile).where(
                tuple_(NRBSourceFile.source_id, NRBSourceFile.file_id).in_(chunk)
            )
        )


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
async def create_run(
    session: AsyncSession, *, dry_run: bool = False
) -> tuple[int, datetime]:
    """Open a sync run. Returns `(run_id, started_at)`.

    `started_at` comes back from Postgres and becomes the run's single logical
    "seen at" instant for every row it stamps — one clock, one value, so
    `last_seen_at` is comparable across the whole run instead of drifting by
    however long the reconciliation took.
    """
    result = await session.execute(
        NRBSyncRun.__table__.insert().returning(
            NRBSyncRun.id, NRBSyncRun.started_at
        ),
        [{"status": RUN_RUNNING, "dry_run": dry_run}],
    )
    run_id, started_at = result.one()
    return run_id, started_at


async def finish_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: str,
    counters: dict[str, int],
    notes: dict[str, Any],
    discovery_complete: bool,
    deactivation_applied: bool,
) -> None:
    """Write the terminal state of a run. Only known counter columns are set."""
    columns = set(NRBSyncRun.__table__.columns.keys())
    values: dict[str, Any] = {
        key: value for key, value in counters.items() if key in columns
    }
    values.update(
        {
            "status": status,
            "completed_at": func.now(),
            "discovery_complete": discovery_complete,
            "deactivation_applied": deactivation_applied,
            "notes": notes,
        }
    )
    await session.execute(
        update(NRBSyncRun).where(NRBSyncRun.id == run_id).values(**values)
    )


async def catalog_counts(session: AsyncSession) -> dict[str, int]:
    """The numbers an operator checks after a sync. Read-only.

    Includes the two "this must be zero" integrity checks even though both are
    backed by unique indexes: a report that only prints what it expects to be true
    is not evidence.
    """
    async def scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one() or 0)

    counts = {
        "sources": await scalar(select(func.count()).select_from(NRBSource)),
        "active_sources": await scalar(
            select(func.count()).select_from(NRBSource).where(
                NRBSource.is_active.is_(True)
            )
        ),
        "inactive_sources": await scalar(
            select(func.count()).select_from(NRBSource).where(
                NRBSource.is_active.is_(False)
            )
        ),
        "rest_sources": await scalar(
            select(func.count()).select_from(NRBSource).where(
                NRBSource.metadata_status == METADATA_STATUS_REST
            )
        ),
        "sitemap_only_sources": await scalar(
            select(func.count()).select_from(NRBSource).where(
                NRBSource.metadata_status != METADATA_STATUS_REST
            )
        ),
        "untyped_sources": await scalar(
            select(func.count()).select_from(NRBSource).where(
                NRBSource.document_type.is_(None)
            )
        ),
        "files": await scalar(select(func.count()).select_from(NRBFile)),
        "blocked_files": await scalar(
            select(func.count()).select_from(NRBFile).where(
                NRBFile.fetch_status != "pending"
            )
        ),
        "relationships": await scalar(
            select(func.count()).select_from(NRBSourceFile)
        ),
    }

    duplicate_keys = select(NRBSource.url_key).group_by(NRBSource.url_key).having(
        func.count() > 1
    )
    duplicate_files = (
        select(NRBFile.comparison_key)
        .group_by(NRBFile.comparison_key)
        .having(func.count() > 1)
    )
    counts["duplicate_source_identities"] = len(
        (await session.execute(duplicate_keys)).all()
    )
    counts["duplicate_comparison_keys"] = len(
        (await session.execute(duplicate_files)).all()
    )
    return counts


# --------------------------------------------------------------------------- #
# Phase 5: selecting and recording downloads
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FetchTarget:
    """One file a download pass will try to fetch.

    Carries NRB's *claims* about the file (`reported_mime_type`, `reported_bytes`,
    `resource_type`) because the fetcher checks the bytes against them. It does not
    carry the source that references it: a file may be published by several sources
    and is fetched once.
    """

    id: int
    source_url: str
    comparison_key: str
    extension: str | None
    resource_type: str
    reported_mime_type: str | None
    reported_bytes: int | None
    fetch_attempts: int


def _bounded_keys(keys: Sequence[str]) -> list[str]:
    """Distinct keys, order-stable, refused if there are too many.

    Deduplicated because a hand-edited manifest can name the same file twice and
    that is still one download; bounded because a key list is a scope, and a scope
    that can name the whole corpus is not one.
    """
    distinct = list(dict.fromkeys(keys))
    if len(distinct) > MANIFEST_MAX_KEYS:
        raise ValueError(
            f"scope names {len(distinct)} catalog keys; the cap is "
            f"{MANIFEST_MAX_KEYS}. A key list is a benchmark cohort, not a way to "
            f"fetch the whole corpus — use --all for that, explicitly."
        )
    return distinct


async def select_fetch_targets(
    session: AsyncSession,
    *,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    keys: Sequence[str] | None = None,
    limit: int | None = None,
    retry_failed: bool = False,
    include_inactive: bool = False,
) -> list[FetchTarget]:
    """The files a fetch should attempt, in a deterministic order.

    `blocked_host` rows can never be selected — not by filtering them out here, but
    because the status list only ever contains `pending` (plus `failed` when a retry
    is asked for). That is the point of the status column: the three
    `uat.nrb.org.np` links are excluded by construction rather than by a `WHERE`
    clause someone could forget.

    Selection joins through to `nrb_sources` with EXISTS rather than a DISTINCT
    join, because a file referenced by three sources must appear once, not three
    times. `include_inactive` exists for completeness; by default a file only
    reachable from deactivated sources is not fetched — NRB withdrew it.

    `keys` is the exact-cohort scope: `nrb_files.comparison_key` values, normally
    from a benchmark manifest. It **selects from** the catalog and cannot add to
    it — a key matching no row simply selects nothing (`resolve_manifest_keys`
    reports those), and the URL that is eventually requested is the matched row's
    `source_url`, guarded at fetch time like any other. It is also purely
    additive: every predicate below still applies, so a manifest naming a
    `blocked_host` file still cannot select it, and `--manifest --core` means the
    manifest cohort restricted to the core.

    `years` filters on NRB's own publication date. It exists because id-order
    selection cannot deliberately reach a cohort: catalog ids are REST paging
    order, so "circulars, limit 60" is the 60 lowest ids, and 2019 — NRB's CMS
    migration — is half the corpus.
    """
    statuses = [FETCH_PENDING] + ([FETCH_FAILED] if retry_failed else [])
    stmt = select(
        NRBFile.id,
        NRBFile.source_url,
        NRBFile.comparison_key,
        NRBFile.extension,
        NRBFile.resource_type,
        NRBFile.reported_mime_type,
        NRBFile.reported_bytes,
        NRBFile.fetch_attempts,
    ).where(NRBFile.fetch_status.in_(statuses))

    if resource_types:
        stmt = stmt.where(NRBFile.resource_type.in_(list(resource_types)))
    if keys:
        stmt = stmt.where(NRBFile.comparison_key.in_(_bounded_keys(keys)))

    if sections or owners or years or not include_inactive:
        link = (
            select(NRBSourceFile.file_id)
            .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
            .where(NRBSourceFile.file_id == NRBFile.id)
        )
        if not include_inactive:
            link = link.where(NRBSource.is_active.is_(True))
        if sections:
            link = link.where(NRBSource.document_type.in_(list(sections)))
        if owners:
            link = link.where(NRBSource.owner.in_(list(owners)))
        if years:
            # NRB's own `date`, which Phase 3 measured at 100% coverage. A file
            # published by several sources is in scope if ANY of them is — the
            # EXISTS is already per-file, so this needs no separate handling.
            link = link.where(
                func.extract("year", NRBSource.published_at).in_(
                    [float(year) for year in years]
                )
            )
        stmt = stmt.where(link.exists())

    # Oldest row first: stable across runs, so an interrupted pass resumed later
    # continues rather than re-rolling the dice on which files got done.
    stmt = stmt.order_by(NRBFile.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [FetchTarget(*row) for row in (await session.execute(stmt)).all()]


@dataclass(frozen=True)
class ManifestResolution:
    """What an exact key list turned out to name in the catalog.

    Selection alone cannot answer this. `select_fetch_targets` returns what will be
    attempted, which for a manifest is only the `pending` slice — so a cohort of
    400 whose 380 files are already on disk selects 20, and without this the pass
    would report "20 files" and look like it had lost the other 380. Each state is
    counted separately because they mean different things to a reader: already
    fetched is done, pending is queued, previously failed needs `--retry-failed`,
    blocked can never be fetched, and missing means the catalog does not know that
    key at all — a stale manifest or a typo, and the only one that is a defect.
    """

    requested: int                      # distinct keys asked for
    duplicate_keys: int                 # entries collapsed into those
    by_status: dict[str, int]           # fetch_status -> count, over matched rows
    missing: tuple[str, ...]            # keys with no `nrb_files` row at all

    @property
    def known(self) -> int:
        return sum(self.by_status.values())

    def as_dict(self, *, sample: int = 50) -> dict[str, Any]:
        """JSON-ready, with the missing list bounded for the run record."""
        return {
            "requested": self.requested,
            "duplicate_keys": self.duplicate_keys,
            "known": self.known,
            "by_status": dict(self.by_status),
            "missing_count": len(self.missing),
            "missing": list(self.missing[:sample]),
        }


async def resolve_manifest_keys(
    session: AsyncSession, keys: Sequence[str]
) -> ManifestResolution:
    """Match exact `comparison_key` values against the catalog. Read-only.

    Deliberately reports rather than filters: a key the catalog does not know is
    returned as missing instead of being dropped, because a manifest drifting away
    from the corpus is exactly the failure this phase must not paper over.
    """
    distinct = _bounded_keys(keys)
    if not distinct:
        return ManifestResolution(0, 0, {}, ())
    found: dict[str, str] = {}
    for start in range(0, len(distinct), BATCH):
        chunk = distinct[start : start + BATCH]
        rows = (
            await session.execute(
                select(NRBFile.comparison_key, NRBFile.fetch_status).where(
                    NRBFile.comparison_key.in_(chunk)
                )
            )
        ).all()
        found.update({key: status for key, status in rows})
    by_status: dict[str, int] = {}
    for status in found.values():
        by_status[status] = by_status.get(status, 0) + 1
    return ManifestResolution(
        requested=len(distinct),
        duplicate_keys=len(keys) - len(distinct),
        by_status=by_status,
        missing=tuple(key for key in distinct if key not in found),
    )


async def record_fetch_outcomes(
    session: AsyncSession, rows: Sequence[dict[str, Any]]
) -> None:
    """Write download results back to `nrb_files`, by primary key.

    Core, with the `_id` bindparam, for the reason in the module docstring. Rows
    are written in batches by the caller so a killed pass keeps its progress.

    Rows are grouped by their KEY SET before executing. executemany requires every
    parameter dict in a batch to have identical keys, and these deliberately do not:
    a successful fetch sets the content columns, a failure omits them (so a previous
    download's pointer is not erased), and a blocked file adds `blocked_reason`.
    Grouping is what lets the caller hand over one mixed list.
    """
    if not rows:
        return
    table = NRBFile.__table__
    statement = table.update().where(table.c.id == bindparam("_id"))
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(sorted(row)), []).append(row)
    for group in groups.values():
        for start in range(0, len(group), BATCH):
            await session.execute(statement, group[start : start + BATCH])


async def create_fetch_run(
    session: AsyncSession, *, scope: dict[str, Any], dry_run: bool = False
) -> tuple[int, datetime]:
    """Open a download run. Returns `(run_id, started_at)` from the DB clock."""
    result = await session.execute(
        NRBFetchRun.__table__.insert().returning(
            NRBFetchRun.id, NRBFetchRun.started_at
        ),
        [{"status": FETCH_RUN_RUNNING, "dry_run": dry_run, "scope": scope}],
    )
    run_id, started_at = result.one()
    return run_id, started_at


async def finish_fetch_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: str,
    counters: dict[str, int],
    notes: dict[str, Any],
) -> None:
    """Write the terminal state of a download run. Only known columns are set."""
    columns = set(NRBFetchRun.__table__.columns.keys())
    values: dict[str, Any] = {
        key: value for key, value in counters.items() if key in columns
    }
    values.update({"status": status, "completed_at": func.now(), "notes": notes})
    await session.execute(
        update(NRBFetchRun).where(NRBFetchRun.id == run_id).values(**values)
    )


async def fetch_counts(session: AsyncSession) -> dict[str, int]:
    """Download state of the whole file catalog. Read-only.

    `distinct_blobs` vs `fetched` is the deduplication measure: NRB republishes the
    same PDF under several URLs, and the content-addressed store keeps one copy.
    """
    async def scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one() or 0)

    counts: dict[str, int] = {}
    for label, status in (
        ("pending", FETCH_PENDING),
        ("fetched", FETCH_FETCHED),
        ("failed", FETCH_FAILED),
        ("blocked", FETCH_BLOCKED_HOST),
    ):
        counts[label] = await scalar(
            select(func.count()).select_from(NRBFile).where(
                NRBFile.fetch_status == status
            )
        )
    counts["distinct_blobs"] = await scalar(
        select(func.count(func.distinct(NRBFile.content_sha256)))
    )
    # Sum over DISTINCT blobs, not over rows: two rows sharing a sha256 share one
    # file on disk, so summing rows would overstate the footprint.
    blobs = (
        select(NRBFile.content_sha256, NRBFile.content_length)
        .where(NRBFile.content_sha256.isnot(None))
        .distinct()
        .subquery()
    )
    counts["bytes_on_disk"] = await scalar(
        select(func.coalesce(func.sum(blobs.c.content_length), 0))
    )
    return counts


# --------------------------------------------------------------------------- #
# Phase 6A: selecting blobs to extract, and recording the results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExtractTarget:
    """One BLOB to extract — not one file row.

    `file_id` is carried for reporting only. Selection is DISTINCT on
    `content_sha256`, because two `nrb_files` rows sharing bytes are one
    extraction: extracting both would parse the same PDF twice and write two rows
    the unique index would then reject.
    """

    file_id: int
    content_sha256: str
    storage_key: str
    extension: str | None
    sniffed_mime: str | None
    resource_type: str
    content_length: int | None


async def select_extract_targets(
    session: AsyncSession,
    *,
    extractor_version: str,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    keys: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> list[ExtractTarget]:
    """Fetched blobs with no extraction at this version, in a deterministic order.

    Only `fetch_status = 'fetched'` rows can be selected, which by construction
    excludes `pending`, `failed` and the three `blocked_host` UAT links — the same
    "excluded by the status column, not by a WHERE clause someone could forget"
    rule Phase 5 uses. A `fetched` row is also guaranteed by
    `ck_nrb_files_fetched_is_complete` to name its bytes, so the sha/key checks
    below are belt and braces rather than the guarantee.

    `keys` is the benchmark cohort, the same `comparison_key` identity the fetch
    scope uses, and it composes with the other filters rather than replacing them.
    Note its interaction with DISTINCT ON: two cohort entries that turn out to
    share bytes collapse to ONE extraction, so the blob count can be lower than the
    cohort size. That is correct — it is one blob — and the report states the two
    numbers separately rather than letting the gap read as a failure.

    `force` re-selects blobs already extracted at this version, for when a rule
    changed but `EXTRACTOR_VERSION` has not been bumped yet (development only —
    bumping the version is the honest way to invalidate).
    """
    stmt = (
        select(
            NRBFile.id,
            NRBFile.content_sha256,
            NRBFile.storage_key,
            NRBFile.extension,
            NRBFile.sniffed_mime,
            NRBFile.resource_type,
            NRBFile.content_length,
        )
        .where(
            NRBFile.fetch_status == FETCH_FETCHED,
            NRBFile.content_sha256.isnot(None),
            NRBFile.storage_key.isnot(None),
        )
        # One row per blob, and a stable tiebreak, so a resumed pass continues
        # rather than re-rolling which blobs got done.
        .distinct(NRBFile.content_sha256)
        .order_by(NRBFile.content_sha256, NRBFile.id)
    )

    if resource_types:
        stmt = stmt.where(NRBFile.resource_type.in_(list(resource_types)))
    if keys:
        stmt = stmt.where(NRBFile.comparison_key.in_(_bounded_keys(keys)))

    if not force:
        done = select(NRBExtraction.id).where(
            NRBExtraction.content_sha256 == NRBFile.content_sha256,
            NRBExtraction.extractor_version == extractor_version,
        )
        stmt = stmt.where(~done.exists())

    if sections or owners or years:
        link = (
            select(NRBSourceFile.file_id)
            .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
            .where(NRBSourceFile.file_id == NRBFile.id, NRBSource.is_active.is_(True))
        )
        if sections:
            link = link.where(NRBSource.document_type.in_(list(sections)))
        if owners:
            link = link.where(NRBSource.owner.in_(list(owners)))
        if years:
            link = link.where(
                func.extract("year", NRBSource.published_at).in_(
                    [float(year) for year in years]
                )
            )
        stmt = stmt.where(link.exists())

    if limit is not None:
        # Applies to the DISTINCT result, so it counts blobs, not rows.
        stmt = stmt.limit(limit)
    return [ExtractTarget(*row) for row in (await session.execute(stmt)).all()]


# Everything an upsert must NOT overwrite: the identity itself, and the row's own
# history. Derived by subtraction from the table rather than listed positively, so
# a metric column added later is refreshed by a re-extraction instead of silently
# keeping its first value — which is the failure mode that made this a set
# difference in the first place.
_EXTRACTION_IMMUTABLE = frozenset(
    {"id", "content_sha256", "extractor_version", "created_at"}
)


async def record_extractions(
    session: AsyncSession, rows: Sequence[dict[str, Any]]
) -> None:
    """Upsert extraction results, by `(content_sha256, extractor_version)`.

    ON CONFLICT DO UPDATE rather than a plain insert: a `--force` re-extraction has
    to replace its previous answer rather than fail on the unique index, and an
    interrupted pass re-selecting a blob it had already recorded must be a no-op
    rather than an error.

    Core, with column keys, per this module's opening rule.
    """
    if not rows:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    table = NRBExtraction.__table__
    statement = pg_insert(table)
    statement = statement.on_conflict_do_update(
        index_elements=["content_sha256", "extractor_version"],
        set_={
            column.name: statement.excluded[column.name]
            for column in table.columns
            if column.name not in _EXTRACTION_IMMUTABLE
        },
    )
    for start in range(0, len(rows), BATCH):
        await session.execute(statement, list(rows[start : start + BATCH]))


async def extraction_counts(
    session: AsyncSession, *, extractor_version: str
) -> dict[str, int]:
    """Extraction state of the whole fetched catalog, at one version. Read-only.

    `blobs_fetched` counts DISTINCT sha256, not file rows: that is the true size of
    the work, and the gap between it and `fetched` is how much NRB republishes.
    `stale` counts rows written by an earlier extractor — the invalidation handle,
    and a sequential scan by design (`extractor_version` is the second column of
    the unique index, so it is not served by it; see `models.NRBExtraction`).
    """
    async def scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one() or 0)

    counts: dict[str, int] = {
        "blobs_fetched": await scalar(
            select(func.count(func.distinct(NRBFile.content_sha256))).where(
                NRBFile.fetch_status == FETCH_FETCHED
            )
        ),
        "blobs_extracted": await scalar(
            select(func.count()).select_from(NRBExtraction).where(
                NRBExtraction.extractor_version == extractor_version
            )
        ),
    }
    rows = (
        await session.execute(
            select(NRBExtraction.status, func.count())
            .where(NRBExtraction.extractor_version == extractor_version)
            .group_by(NRBExtraction.status)
        )
    ).all()
    for status, count in rows:
        counts[status] = int(count)
    counts["stale"] = await scalar(
        select(func.count()).select_from(NRBExtraction).where(
            NRBExtraction.extractor_version != extractor_version
        )
    )
    return counts


async def count_unfetched(session: AsyncSession, keys: Sequence[str]) -> int:
    """How many of these cohort keys are not on disk yet. Read-only.

    A partly-fetched cohort is a legitimate mid-download state, not an error — but
    every percentage in the profile is over the files that WERE extracted, so the
    gap has to be said out loud rather than left for a reader to notice that 400
    became 380.
    """
    if not keys:
        return 0
    distinct = _bounded_keys(keys)
    fetched = int(
        (
            await session.execute(
                select(func.count())
                .select_from(NRBFile)
                .where(
                    NRBFile.comparison_key.in_(distinct),
                    NRBFile.fetch_status == FETCH_FETCHED,
                )
            )
        ).scalar_one()
        or 0
    )
    return max(len(distinct) - fetched, 0)


async def load_sample_rows(
    session: AsyncSession,
    *,
    sections: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Every fetchable file with its stratification keys. Read-only.

    Returns FILE-level rows (one per `nrb_files` row, joined to its primary source)
    because sampling happens BEFORE anything is fetched, when `content_sha256` is
    still NULL — so the sample is keyed on `comparison_key` and the fetch is scoped
    from it.

    A file referenced by several sources is attributed to the FIRST by source id,
    which is deterministic but is an approximation: 42 files really are published
    by more than one source. The report says so rather than implying each file has
    one owner.
    """
    stmt = (
        select(
            NRBFile.comparison_key,
            NRBFile.resource_type,
            NRBFile.fetch_status,
            NRBFile.content_sha256,
            func.min(NRBSource.id).label("source_id"),
        )
        .join(NRBSourceFile, NRBSourceFile.file_id == NRBFile.id)
        .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
        .where(NRBSource.is_active.is_(True))
        .group_by(
            NRBFile.comparison_key, NRBFile.resource_type,
            NRBFile.fetch_status, NRBFile.content_sha256,
        )
    )
    if resource_types:
        stmt = stmt.where(NRBFile.resource_type.in_(list(resource_types)))
    if sections:
        stmt = stmt.where(NRBSource.document_type.in_(list(sections)))
    base = stmt.subquery()
    detailed = select(
        base.c.comparison_key,
        base.c.resource_type,
        base.c.fetch_status,
        base.c.content_sha256,
        NRBSource.document_type,
        NRBSource.owner,
        func.extract("year", NRBSource.published_at).label("year"),
    ).join(NRBSource, NRBSource.id == base.c.source_id)
    return [
        {
            "comparison_key": row[0],
            "resource_type": row[1],
            "fetch_status": row[2],
            "content_sha256": row[3],
            "document_type": row[4],
            "owner": row[5],
            "year": int(row[6]) if row[6] is not None else None,
        }
        for row in (await session.execute(detailed)).all()
    ]

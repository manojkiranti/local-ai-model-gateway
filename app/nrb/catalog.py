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
    NRBFetchRun,
    NRBFile,
    NRBSource,
    NRBSourceFile,
    NRBSyncRun,
    RUN_RUNNING,
)
from .records import FileRecord, SourceRecord

__all__ = [
    "FetchTarget",
    "FileState",
    "SourceIndex",
    "SourceState",
    "catalog_counts",
    "create_fetch_run",
    "create_run",
    "deactivate_unseen",
    "delete_relationships",
    "fetch_counts",
    "finish_fetch_run",
    "finish_run",
    "insert_files",
    "insert_relationships",
    "insert_sources",
    "load_file_index",
    "load_relationships",
    "load_source_index",
    "record_fetch_outcomes",
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


async def select_fetch_targets(
    session: AsyncSession,
    *,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
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

    if sections or owners or not include_inactive:
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
        stmt = stmt.where(link.exists())

    # Oldest row first: stable across runs, so an interrupted pass resumed later
    # continues rather than re-rolling the dice on which files got done.
    stmt = stmt.order_by(NRBFile.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [FetchTarget(*row) for row in (await session.execute(stmt)).all()]


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

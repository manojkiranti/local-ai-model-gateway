"""The NRB catalog reconciliation — idempotent, and safe when it fails.

One pass: read the whole corpus (`discovery`), turn it into rows (`records`),
diff it against what is stored (`catalog`), and apply the difference. Running it
twice against an unchanged NRB creates nothing, updates nothing and deactivates
nothing; the second run is the acceptance test, not the first.

WHAT COUNTS AS A CHANGE
    A source is `updated` only when its `metadata_hash` moves. `last_seen_at` and
    `last_sync_run_id` advance on every run for every row, and that is
    deliberately NOT an update — otherwise every sync would report 18,370 changes
    and the counters would be worthless as a change log. Files are compared field
    by field for the same reason.

FAILURE SAFETY — the rule that matters most
    Deactivation is absence-based: anything active that this run did not stamp is
    assumed gone. That inference is only valid if the run really saw everything,
    so it is gated three ways:

      1. `discovery.complete` — no fetch error, no truncating bound, sitemap read.
      2. A shrink floor. Even a "complete" run that suddenly sees 60% of the
         known corpus is refused: NRB serving empty REST collections would
         otherwise deactivate thousands of good rows in one statement. The run
         reports `partial` and says why.
      3. A CHECK constraint on `nrb_sync_runs`, so the illegal combination
         (deactivated on an incomplete run) cannot even be recorded.

    Everything before deactivation is *additive or corrective*: inserting files,
    inserting sources, updating changed rows. A crash in the middle leaves a
    catalog that is behind, never one that is wrong — and the next run finishes
    the job. That is why the phases commit separately instead of wrapping 18k
    sources in one transaction that either lands whole or loses an hour of work.

RELATIONSHIPS COMMIT AT SOURCE BOUNDARIES
    A source's whole attachment set — the inserts AND the removals — lands in one
    transaction. Half-applied relationships would mean a source that briefly
    publishes two files it does not have, which is exactly the state a reader
    would misread as real.

CONCURRENCY
    A Postgres session-level advisory lock, via `locks.advisory_lock` — the same
    mechanism the file fetch uses, on a different key. Two syncs would interleave
    their counters and race on the same rows, so the second one refuses (`SyncBusy`)
    instead of waiting. The `nrb_sync_runs` row is a record, never a mutex; a crashed
    run's row stays `running` forever and blocks nothing.

    **The lock is taken BEFORE discovery, not before reconciliation.** Ordering it
    the other way still refuses correctly, but only after the second invocation has
    spent ~190 requests and several minutes on a central bank's website to build a
    result it will immediately throw away. Discovery is read-only, so this is
    politeness rather than correctness — which is exactly why it is easy to get
    wrong and worth stating.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import catalog
from .discovery import Discovery, discover_corpus
from .locks import SYNC_LOCK_KEY, LockBusy, advisory_lock
from .models import (
    FETCH_PENDING,
    METADATA_STATUS_REST,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIAL,
)
from .records import FileRecord, SourceRecord, build_source_records

logger = logging.getLogger("app.nrb.sync")

__all__ = ["SyncBusy", "SyncResult", "reconcile", "run_sync"]

# Kept as a module-level name because it is part of this module's surface (and its
# tests); the definition lives in `locks`, shared with the fetch.
ADVISORY_LOCK_KEY = SYNC_LOCK_KEY

# Relationship operations accumulated before a commit. Commits only happen at a
# source boundary, so a source's set is never split across two transactions.
RELATIONSHIP_COMMIT_BATCH = 2000

# The minimum share of currently-active sources a run must see before it is
# allowed to deactivate anything. 0.9 is a guard against upstream returning a
# near-empty corpus, not a tuning knob — NRB's corpus grows monotonically in
# practice, and a genuine 10% deletion is something a human should confirm.
SHRINK_FLOOR = 0.9
# ...and the guard only applies to a catalog big enough for a ratio to mean
# something. With a handful of rows, "one fewer than last time" is ordinary churn;
# with 18,000, it is an incident. Without this floor the guard would block the
# very first legitimate withdrawal in a small or freshly-seeded catalog.
SHRINK_FLOOR_MIN_SOURCES = 100

# Bounded samples in `nrb_sync_runs.notes`: the counts are the aggregate, and
# 18,370 warnings in a JSONB column is an unreadable row, not an audit trail.
NOTE_SAMPLE = 50


# One exception type for callers of this module; the mechanism lives in `locks`.
SyncBusy = LockBusy


@dataclass
class SyncResult:
    """What one sync did. Everything the CLI prints comes from here."""

    run_id: int | None
    status: str
    counters: dict[str, int] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    discovery_complete: bool = False
    deactivation_applied: bool = False
    # Reported separately because they are wildly different orders of magnitude and
    # a single total hides which half is slow: reading 18.5k documents from a
    # central bank's website over ~190 paced requests takes minutes, reconciling
    # them takes seconds. A blended number would make the sync look expensive and
    # the site look fast.
    discovery_seconds: float = 0.0
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == RUN_COMPLETED


def _counters() -> dict[str, int]:
    return {
        "sitemaps_seen": 0,
        "sources_seen": 0,
        "sources_created": 0,
        "sources_updated": 0,
        "sources_unchanged": 0,
        "sources_deactivated": 0,
        "sources_reactivated": 0,
        "sitemap_only_sources": 0,
        "files_seen": 0,
        "files_created": 0,
        "files_updated": 0,
        "files_unchanged": 0,
        "files_refetch_queued": 0,
        "blocked_files": 0,
        "relationships_created": 0,
        "relationships_removed": 0,
        "relationships_updated": 0,
        "error_count": 0,
        "warning_count": 0,
    }


async def reconcile(
    session: AsyncSession,
    discovery: Discovery,
    *,
    run_id: int,
    seen_at: datetime,
    dry_run: bool = False,
) -> SyncResult:
    """Apply one discovery to the catalog. The caller owns the run row and lock.

    Commits at phase boundaries unless `dry_run`, in which case the whole thing
    runs in one transaction and is rolled back at the end — the same code path,
    so a dry run predicts what a real run would do (including any constraint it
    would violate) rather than approximating it.
    """
    counters = _counters()
    # "In the sitemap but not in REST" is only a meaningful statement when REST
    # returned the whole corpus. On a bounded or partly-failed REST pass it would
    # name thousands of URLs REST serves perfectly well, and fill the catalog with
    # contentless stubs. The gap simply is not measured this run.
    suppressed_gap = bool(discovery.sitemap_documents) and not discovery.rest_complete
    records, warnings = build_source_records(
        discovery.documents,
        {} if suppressed_gap else discovery.sitemap_documents,
    )
    errors = list(discovery.errors)
    warnings = list(discovery.warnings) + warnings
    if suppressed_gap:
        warnings.append(
            "not recording sitemap-only sources: the REST pass was incomplete "
            f"(errors={len(discovery.rest_errors)}, "
            f"truncated={discovery.rest_truncated or 'no'})"
        )
    counters["sitemaps_seen"] = discovery.sitemaps_seen
    counters["sources_seen"] = len(records)
    counters["sitemap_only_sources"] = sum(1 for r in records if r.is_sitemap_only)

    async def commit() -> None:
        if not dry_run:
            await session.commit()

    # ----------------------------------------------------------------- files #
    # Files first: a source's relationships need file ids, and a file row with no
    # relationship yet is inert, so this phase is safe to commit on its own.
    file_records = {}
    for record in records:
        for link in record.files:
            key = link.file.comparison_key
            existing = file_records.get(key)
            if existing is None:
                file_records[key] = link.file
            elif existing != link.file:
                # Two posts describing the same file differently (different MIME
                # or size). First wins — the ordering is deterministic — but the
                # disagreement is upstream's, and worth seeing.
                warnings.append(f"conflicting metadata for shared file {key}")

    counters["files_seen"] = len(file_records)
    counters["blocked_files"] = sum(1 for f in file_records.values() if f.is_blocked)

    file_index = await catalog.load_file_index(session)
    new_files = [rec for key, rec in file_records.items() if key not in file_index]
    # A known file needs writing when an upstream FACT changed, or when a real
    # upstream change justifies moving its operational fetch state
    # (`FileState.fetch_transition` — a host becoming blocked or unblocked, or
    # NRB replacing the bytes behind the URL). "The candidate says pending" is
    # NOT a change: that is the constructor default, and treating it as one used
    # to write `pending` over every completed download on every sync.
    changed_files: list[tuple[int, FileRecord, dict[str, Any]]] = []
    unchanged_files: list[int] = []
    refetch_queued = 0
    for key, rec in file_records.items():
        state = file_index.get(key)
        if state is None:
            continue
        transition = state.fetch_transition(rec)
        if state.differs_from(rec) or transition:
            changed_files.append((state.id, rec, transition))
            if transition.get("fetch_status") == FETCH_PENDING:
                refetch_queued += 1
        else:
            unchanged_files.append(state.id)

    inserted_files = await catalog.insert_files(
        session, new_files, seen_at=seen_at, run_id=run_id
    )
    await catalog.update_files(
        session, changed_files, seen_at=seen_at, run_id=run_id
    )
    await catalog.touch_files(
        session, unchanged_files, seen_at=seen_at, run_id=run_id
    )
    counters["files_created"] = len(inserted_files)
    counters["files_updated"] = len(changed_files)
    counters["files_unchanged"] = len(unchanged_files)
    # How many known files were put back in the download queue because upstream
    # replaced them or unblocked them. Reported separately from `files_updated`
    # because it is the only file counter with a COST attached, and because a
    # nonzero value on a routine sync is the signal that NRB republished
    # something (§22's supersession trigger).
    counters["files_refetch_queued"] = refetch_queued
    await commit()
    logger.info(
        "NRB sync: files reconciled — %d new, %d changed, %d unchanged, %d blocked",
        counters["files_created"], counters["files_updated"],
        counters["files_unchanged"], counters["blocked_files"],
    )

    file_ids: dict[str, int] = {
        key: (file_index[key].id if key in file_index else inserted_files[key])
        for key in file_records
    }

    # --------------------------------------------------------------- sources #
    source_index = await catalog.load_source_index(session)
    active_known = sum(1 for s in source_index.by_url_key.values() if s.is_active)

    new_sources: list[SourceRecord] = []
    changed_sources: list[tuple[int, SourceRecord]] = []
    touch_ids: list[int] = []
    reactivate_ids: list[int] = []
    # (record, source_id) for every source whose relationships this run may
    # reconcile. A source deliberately left alone is NOT in here, which is what
    # stops the "do not downgrade" and URL-collision guards from stripping its
    # files anyway — the two places that `continue` below are also opting out of
    # relationship reconciliation, and that is the point.
    resolved: list[tuple[SourceRecord, int]] = []
    left_alone = 0

    for record in records:
        state = source_index.match(record)
        if state is None:
            new_sources.append(record)
            continue

        if record.is_sitemap_only and state.metadata_status == METADATA_STATUS_REST:
            # REST did not return a post it has returned before. Treat the stored
            # REST metadata as still true rather than downgrading the row: a post
            # type dropping out of REST for one run would otherwise strip the
            # attachments off every source it owns (bfr alone is 5,400 of them)
            # while the run still called itself complete.
            warnings.append(
                f"REST did not return known source {record.page_url}; "
                "keeping stored metadata"
            )
            touch_ids.append(state.id)
            left_alone += 1
            continue

        if record.url_key != state.url_key and record.url_key in source_index.by_url_key:
            # NRB moved this post onto a URL another row already owns. Updating
            # would violate ux_nrb_sources_url_key and abort the batch, so the row
            # is left as it is and the collision is reported for a human.
            warnings.append(
                f"URL collision for {record.wp_post_type}#{record.wp_post_id}: "
                f"{record.url_key} already belongs to another source"
            )
            touch_ids.append(state.id)
            left_alone += 1
            continue

        if not state.is_active:
            counters["sources_reactivated"] += 1
        if state.metadata_hash == record.metadata_hash:
            (touch_ids if state.is_active else reactivate_ids).append(state.id)
        else:
            changed_sources.append((state.id, record))
        resolved.append((record, state.id))

    inserted_sources = await catalog.insert_sources(
        session, new_sources, seen_at=seen_at, run_id=run_id
    )
    await catalog.update_sources(
        session, changed_sources, seen_at=seen_at, run_id=run_id
    )
    await catalog.touch_sources(session, touch_ids, seen_at=seen_at, run_id=run_id)
    await catalog.touch_sources(
        session, reactivate_ids, seen_at=seen_at, run_id=run_id, reactivate=True
    )
    counters["sources_created"] = len(inserted_sources)
    counters["sources_updated"] = len(changed_sources)
    counters["sources_unchanged"] = len(touch_ids) + len(reactivate_ids)
    for record in new_sources:
        resolved.append((record, inserted_sources[record.url_key]))
    await commit()
    logger.info(
        "NRB sync: sources reconciled — %d new, %d changed, %d unchanged, "
        "%d reactivated (%d sitemap-only)",
        counters["sources_created"], counters["sources_updated"],
        counters["sources_unchanged"], counters["sources_reactivated"],
        counters["sitemap_only_sources"],
    )

    # --------------------------------------------------------- relationships #
    existing = await catalog.load_relationships(session)
    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []
    to_delete: list[tuple[int, int]] = []

    for record, source_id in resolved:
        desired = {
            file_ids[link.file.comparison_key]: (link.ordinal, link.relationship_type)
            for link in record.files
        }
        current = existing.get(source_id, {})
        for file_id, (ordinal, relationship_type) in desired.items():
            if file_id not in current:
                to_insert.append(
                    {
                        "source_id": source_id,
                        "file_id": file_id,
                        "ordinal": ordinal,
                        "relationship_type": relationship_type,
                    }
                )
            elif current[file_id] != (ordinal, relationship_type):
                to_update.append(
                    {
                        "source_id": source_id,
                        "file_id": file_id,
                        "ordinal": ordinal,
                        "relationship_type": relationship_type,
                    }
                )
        for file_id in current:
            if file_id not in desired:
                to_delete.append((source_id, file_id))

        # Checked at the SOURCE boundary, so a source's inserts and removals
        # always land in the same transaction.
        if len(to_insert) + len(to_update) + len(to_delete) >= RELATIONSHIP_COMMIT_BATCH:
            counters["relationships_created"] += len(to_insert)
            counters["relationships_updated"] += len(to_update)
            counters["relationships_removed"] += len(to_delete)
            await catalog.insert_relationships(session, to_insert)
            await catalog.update_relationships(session, to_update)
            await catalog.delete_relationships(session, to_delete)
            await commit()
            to_insert, to_update, to_delete = [], [], []

    counters["relationships_created"] += len(to_insert)
    counters["relationships_updated"] += len(to_update)
    counters["relationships_removed"] += len(to_delete)
    await catalog.insert_relationships(session, to_insert)
    await catalog.update_relationships(session, to_update)
    await catalog.delete_relationships(session, to_delete)
    await commit()
    logger.info(
        "NRB sync: relationships reconciled — %d created, %d updated, %d removed",
        counters["relationships_created"], counters["relationships_updated"],
        counters["relationships_removed"],
    )

    # ---------------------------------------------------------- deactivation #
    stamped = (
        len(new_sources) + len(changed_sources) + len(touch_ids) + len(reactivate_ids)
    )
    deactivation_applied = False
    deactivation_skipped: str | None = None
    if not discovery.complete:
        deactivation_skipped = (
            "discovery was incomplete (errors="
            f"{len(discovery.errors)}, truncated={discovery.truncated or 'no'})"
        )
    elif (
        active_known >= SHRINK_FLOOR_MIN_SOURCES
        and stamped < SHRINK_FLOOR * active_known
    ):
        deactivation_skipped = (
            f"discovery saw {stamped} sources against {active_known} known active "
            f"(below the {SHRINK_FLOOR:.0%} floor)"
        )
    else:
        counters["sources_deactivated"] = await catalog.deactivate_unseen(
            session, run_id=run_id, seen_at=seen_at
        )
        deactivation_applied = True
        await commit()

    if deactivation_skipped:
        warnings.append(f"skipped absence-based deactivation: {deactivation_skipped}")
        logger.warning("NRB sync: %s", warnings[-1])
    else:
        logger.info(
            "NRB sync: deactivated %d sources no longer published",
            counters["sources_deactivated"],
        )

    counters["error_count"] = len(errors)
    counters["warning_count"] = len(warnings)
    status = RUN_COMPLETED if (discovery.complete and not errors) else RUN_PARTIAL
    notes = {
        "errors": errors[:NOTE_SAMPLE],
        "warnings": warnings[:NOTE_SAMPLE],
        "truncated": list(discovery.truncated),
        "post_types_not_served": list(discovery.post_types_not_served),
        "skipped_sitemap_page_kinds": dict(discovery.skipped_by_page_kind),
        "sitemap_urls_seen": discovery.sitemap_urls_seen,
        "sitemap_document_urls": len(discovery.sitemap_documents),
        "deactivation_skipped": deactivation_skipped,
        # Sources stamped as seen but deliberately not rewritten (a REST dropout
        # or a URL collision). Counted as unchanged, and named here because
        # "unchanged" and "we refused to touch it" are different facts.
        "sources_left_alone": left_alone,
    }

    return SyncResult(
        run_id=run_id,
        status=status,
        counters=counters,
        notes=notes,
        dry_run=dry_run,
        discovery_complete=discovery.complete,
        deactivation_applied=deactivation_applied,
    )


async def run_sync(
    *,
    discovery: Discovery | None = None,
    discovery_seconds: float = 0.0,
    limit: int | None = None,
    include_sitemap: bool = True,
    dry_run: bool = False,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> SyncResult:
    """Discover, reconcile, record. The whole manual sync.

    Discovery happens INSIDE the advisory lock (see the module docstring), so a
    second invocation refuses immediately instead of crawling NRB first.
    `DiscoveryError` propagates: there is nothing to reconcile against, and
    reconciling against nothing is indistinguishable from NRB deleting its site.

    `discovery` may be supplied to reconcile a pass that has already been made
    (this is how the tests avoid the network). `engine`/`session_factory` are
    injectable for the same reason; both default to the application's.
    """
    from ..db.session import SessionLocal, engine as app_engine

    engine = engine or app_engine
    session_factory = session_factory or SessionLocal
    started = time.monotonic()

    async with advisory_lock(engine, ADVISORY_LOCK_KEY, what="NRB sync"):
        if discovery is None:
            discovery_started = time.monotonic()
            discovery = await discover_corpus(
                limit=limit, include_sitemap=include_sitemap
            )
            discovery_seconds = time.monotonic() - discovery_started
        logger.info(
            "NRB sync: discovery complete — %d documents, %d sitemap document "
            "URLs, complete=%s",
            len(discovery.documents), len(discovery.sitemap_documents),
            discovery.complete,
        )

        async with session_factory() as session:
            run_id, seen_at = await catalog.create_run(session, dry_run=dry_run)
            if not dry_run:
                await session.commit()
            try:
                result = await reconcile(
                    session,
                    discovery,
                    run_id=run_id,
                    seen_at=seen_at,
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
                await session.rollback()
                if not dry_run:
                    await catalog.finish_run(
                        session,
                        run_id,
                        status=RUN_FAILED,
                        counters={},
                        notes={"errors": [f"{type(exc).__name__}: {exc}"]},
                        discovery_complete=discovery.complete,
                        deactivation_applied=False,
                    )
                    await session.commit()
                logger.exception("NRB sync: failed")
                raise

            if dry_run:
                # Read the counts BEFORE rolling back: inside this
                # transaction they are the state the real run would leave.
                result.counts = await catalog.catalog_counts(session)
                # Then keep nothing, including the run row — a dry run must
                # leave the catalog byte-identical.
                await session.rollback()
                result.run_id = None
            else:
                await catalog.finish_run(
                    session,
                    run_id,
                    status=result.status,
                    counters=result.counters,
                    notes=result.notes,
                    discovery_complete=result.discovery_complete,
                    deactivation_applied=result.deactivation_applied,
                )
                await session.commit()
                result.counts = await catalog.catalog_counts(session)

        result.duration_seconds = time.monotonic() - started
        result.discovery_seconds = discovery_seconds
        return result

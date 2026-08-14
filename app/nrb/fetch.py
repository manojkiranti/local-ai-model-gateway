"""Downloading NRB's published files — safely, verifiably, and resumably.

Phase 4 recorded 18,263 fetchable files (~8.6 GB as NRB reports them, largest 46 MB)
and never touched one. This is the download: for each selected file, stream it to
disk under a byte cap, hash it, check the bytes against what NRB claimed, and record
the result. **Nothing here parses, extracts, chunks or embeds** — that is Phase 6+.
A file that lands on disk is an artefact, not yet a corpus document.

WHAT MAKES THIS SAFE
    The same trust boundary as every other NRB module: `http.check_url(...,
    require_https=True)`, re-checked here rather than trusted from the catalog, so a
    config change or a row edited by hand cannot make the fetcher reach a host the
    guard would refuse. Redirects are NOT followed — a `wp-content` file URL that
    suddenly redirects is a finding, not a hop to take. Bodies stream to disk with a
    hard cap, so an unexpectedly enormous response is abandoned mid-flight instead
    of being buffered into memory. Requests are sequential and paced by
    `NRB_CRAWL_DELAY_SECONDS`. There is no retry inside a run: a failure is recorded
    and a later `--retry-failed` pass is an explicit decision.

WHAT MAKES IT VERIFIABLE
    Every stored file is content-addressed by its own sha256 (`filestore`), so the
    path *is* the checksum. The bytes are also sniffed (`sniff`) and the result kept
    beside NRB's claim; the two disagreeing is recorded, not silently corrected.
    One disagreement IS fatal, and it is the reason this module exists rather than
    being ten lines of httpx: **WordPress answers a missing file with a 200 and a
    themed HTML error page.** Storing that as `circular-15.pdf` would give Phase 6 a
    navigation menu to index as the text of a regulatory circular. HTML where a
    document was promised is a failure, and the file stays absent.
    A `Content-Length` that disagrees with what actually arrived is also fatal — a
    truncated PDF is a plausible-looking file whose tail is missing.

WHAT MAKES IT RESUMABLE
    Selection is `fetch_status = 'pending'` in id order, and results are committed in
    batches, so a killed pass keeps everything it finished and the next one continues
    from there. Because storage is content-addressed, the worst a crash between
    "blob written" and "row committed" can leave is an unreferenced blob — and the
    next attempt at that file hashes to the same key, finds it present, and simply
    records it. There is no cleanup step to forget to run.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ..config import get_settings
from . import catalog, filestore, sniff
from .catalog import FetchTarget
from .http import check_url, open_client
from .locks import FETCH_LOCK_KEY, LockBusy, advisory_lock
from .models import (
    FETCH_BLOCKED_HOST,
    FETCH_FAILED,
    FETCH_FETCHED,
    FETCH_RUN_COMPLETED,
    FETCH_RUN_FAILED,
    FETCH_RUN_PARTIAL,
)
from .sniff import HEAD_BYTES

logger = logging.getLogger("app.nrb.fetch")

__all__ = ["FetchOutcome", "FetchResult", "fetch_one", "run_fetch"]

# Bounds, as module constants rather than settings: they follow from the corpus's
# measured shape, exactly like `sitemap.MAX_URLS`. Only pacing — how hard we may
# lean on a central bank's website — is configuration.
MAX_FILE_BYTES = 64 * 1024 * 1024   # largest file observed live: 46 MB
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 120.0                # a 46 MB PDF on a slow link
CHUNK_BYTES = 64 * 1024
USER_AGENT = "local-ai-gateway/1.0 (+nrb-document-fetch)"

# Rows written per transaction. Small enough that a killed pass loses at most this
# many downloads' bookkeeping (the blobs survive regardless), large enough not to
# commit once per file.
RECORD_BATCH = 25

# Bounded samples in `nrb_fetch_runs.notes`; the counters are the aggregate.
NOTE_SAMPLE = 50


@dataclass
class FetchOutcome:
    """What happened to one file. Maps 1:1 onto an `nrb_files` update."""

    target: FetchTarget
    status: str                       # fetched | failed | blocked_host
    sha256: str | None = None
    length: int | None = None
    storage_key: str | None = None
    sniffed_mime: str | None = None
    http_status: int | None = None
    error: str | None = None
    # True when these bytes were already on disk under another URL. Not a failure —
    # the measure of how much NRB republishes.
    duplicate: bool = False
    # NRB's claim and our sniff disagreed, but not in the fatal HTML way. Kept as a
    # finding for Phase 6, which decides what it can parse.
    type_mismatch: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == FETCH_FETCHED


@dataclass
class FetchResult:
    """What one download pass did. Everything the CLI prints comes from here."""

    run_id: int | None
    status: str
    counters: dict[str, int] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == FETCH_RUN_COMPLETED


def _counters() -> dict[str, int]:
    return {
        "files_selected": 0,
        "files_fetched": 0,
        "files_failed": 0,
        "files_skipped": 0,
        "files_deduplicated": 0,
        "bytes_downloaded": 0,
        "bytes_stored": 0,
        "error_count": 0,
    }


def _expected_family(target: FetchTarget) -> str:
    """The family NRB's own metadata promises for this file.

    Prefers the recorded MIME (WordPress determined it from the bytes at upload —
    99.6% coverage) and falls back to the `resource_type` Phase 3 derived. Only used
    to decide whether an HTML body is a soft-404, never to accept a file.
    """
    family = sniff.family_for(target.reported_mime_type)
    if family != "unknown":
        return family
    return {
        "pdf": "pdf",
        "spreadsheet": "spreadsheet",
        "document": "document",
        "image": "image",
        "archive": "archive",
        "web": "web",
    }.get(target.resource_type, "unknown")


async def fetch_one(
    client: httpx.AsyncClient, target: FetchTarget, *, base: Path | None = None
) -> FetchOutcome:
    """Download one file. Never raises: every failure is a recorded outcome.

    A pass over thousands of files must not die on one bad URL, and *how* a file
    failed is the finding — same contract as `wp_api`'s `FetchError`.
    """
    # Re-checked here rather than trusted from the row: the catalog's answer was
    # correct when it was written, and this is the code that actually opens a socket.
    if (why := check_url(target.source_url, require_https=True)) is not None:
        return FetchOutcome(target, FETCH_BLOCKED_HOST, error=why)

    temp_path = filestore.new_temp_path(base)
    digest = hashlib.sha256()
    written = 0
    head = b""
    http_status: int | None = None
    declared: str | None = None
    # ONE try/finally around everything, so no early return can leak a `.part`
    # file. On the success path `promote` has already moved it and the unlink is a
    # no-op; on every other path this is the cleanup.
    try:
        try:
            async with client.stream("GET", target.source_url) as response:
                http_status = response.status_code
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    return FetchOutcome(
                        target, FETCH_FAILED, http_status=http_status,
                        error=f"refused to follow a redirect to {location!r}",
                    )
                if response.status_code >= 400:
                    return FetchOutcome(
                        target, FETCH_FAILED, http_status=http_status,
                        error=f"HTTP {response.status_code}",
                    )
                declared = response.headers.get("content-length")
                with temp_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes(CHUNK_BYTES):
                        written += len(chunk)
                        if written > MAX_FILE_BYTES:
                            return FetchOutcome(
                                target, FETCH_FAILED, http_status=http_status,
                                error=f"body exceeded {MAX_FILE_BYTES} bytes",
                            )
                        if len(head) < HEAD_BYTES:
                            head += chunk[: HEAD_BYTES - len(head)]
                        digest.update(chunk)
                        handle.write(chunk)
        except httpx.TimeoutException:
            return FetchOutcome(target, FETCH_FAILED, http_status=http_status,
                                error="timed out")
        except httpx.HTTPError as exc:
            return FetchOutcome(
                target, FETCH_FAILED, http_status=http_status,
                error=f"transport error ({type(exc).__name__})",
            )

        if written == 0:
            return FetchOutcome(
                target, FETCH_FAILED, http_status=http_status, error="empty body"
            )
        if declared is not None and declared.isdigit() and int(declared) != written:
            # A truncated PDF still opens and still parses — its tail is simply
            # missing. That is the worst kind of corruption for a document corpus.
            return FetchOutcome(
                target, FETCH_FAILED, http_status=http_status,
                error=f"truncated transfer: Content-Length {declared}, got {written}",
            )

        sniffed, evidence = sniff.sniff(head)
        got = sniff.family_for(sniffed)
        promised = _expected_family(target)
        if got == "web" and promised != "web":
            # THE case this module exists for: WordPress's themed 200 error page.
            return FetchOutcome(
                target, FETCH_FAILED, http_status=http_status, sniffed_mime=sniffed,
                error=(
                    f"served HTML where {promised} was promised ({evidence}) — "
                    "almost certainly a soft 404, so nothing was stored"
                ),
            )

        sha256 = digest.hexdigest()
        storage_key = filestore.storage_key_for(sha256, target.extension)
        stored_new = filestore.promote(temp_path, storage_key, base)
        mismatch = None
        if got != promised and got != "unknown":
            mismatch = f"NRB claimed {promised}, bytes are {got} ({evidence})"
        return FetchOutcome(
            target, FETCH_FETCHED, sha256=sha256, length=written,
            storage_key=storage_key, sniffed_mime=sniffed, http_status=http_status,
            duplicate=not stored_new, type_mismatch=mismatch,
        )
    except (filestore.FileStoreError, OSError) as exc:
        return FetchOutcome(
            target, FETCH_FAILED, http_status=http_status,
            error=f"could not store the file ({exc})",
        )
    finally:
        temp_path.unlink(missing_ok=True)


def _row_for(outcome: FetchOutcome, *, run_id: int, now: Any) -> dict[str, Any]:
    """One `nrb_files` update. Column keys, per `catalog`'s Core-only rule."""
    row: dict[str, Any] = {
        "_id": outcome.target.id,
        "fetch_status": outcome.status,
        "fetch_attempts": outcome.target.fetch_attempts + 1,
        "http_status": outcome.http_status,
        "last_fetch_run_id": run_id,
        "sniffed_mime": outcome.sniffed_mime,
        # Cleared on success so a row that failed once and then succeeded does not
        # keep advertising a stale reason.
        "fetch_error": outcome.error or outcome.type_mismatch,
    }
    if outcome.ok:
        row.update(
            {
                "content_sha256": outcome.sha256,
                "content_length": outcome.length,
                "storage_key": outcome.storage_key,
                "downloaded_at": now,
            }
        )
    # A failing row deliberately omits the content columns rather than nulling
    # them: if bytes were ever downloaded for this file they are still on disk, and
    # a later failed attempt must not erase the pointer to them. The CHECK only
    # constrains rows that CLAIM to be `fetched`, so keeping the columns while the
    # status moves to `failed` is both legal and the honest record ("we have these
    # bytes; the last attempt failed"). `record_fetch_outcomes` groups rows by their
    # key set, so a mixed batch is not a problem.
    if outcome.status == FETCH_BLOCKED_HOST:
        row["blocked_reason"] = outcome.error
    return row


async def run_fetch(
    *,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
    limit: int | None = None,
    retry_failed: bool = False,
    max_bytes: int | None = None,
    dry_run: bool = False,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FetchResult:
    """Select, download, record. The whole manual fetch.

    `dry_run` here means **no HTTP at all** — it reports what would be fetched and
    how many bytes NRB says that is. (The sync's dry run does the opposite: it does
    all the work in a rolled-back transaction. The difference is deliberate: a
    rolled-back download would still have pulled gigabytes off a central bank's
    site, which is the cost we are trying to preview.)
    """
    from ..db.session import SessionLocal, engine as app_engine

    engine = engine or app_engine
    session_factory = session_factory or SessionLocal
    started = time.monotonic()
    counters = _counters()
    scope = {
        "sections": list(sections or []),
        "owners": list(owners or []),
        "resource_types": list(resource_types or []),
        "limit": limit,
        "retry_failed": retry_failed,
        "max_bytes": max_bytes,
    }
    errors: list[str] = []
    warnings: list[str] = []
    stopped: str | None = None

    async with advisory_lock(engine, FETCH_LOCK_KEY, what="NRB fetch"):
        async with session_factory() as session:
            targets = await catalog.select_fetch_targets(
                session,
                sections=sections,
                owners=owners,
                resource_types=resource_types,
                limit=limit,
                retry_failed=retry_failed,
            )
            counters["files_selected"] = len(targets)
            reported = sum(t.reported_bytes or 0 for t in targets)
            logger.info(
                "NRB fetch: %d files selected (%.1f MB as NRB reports them)",
                len(targets), reported / 1_048_576,
            )

            if dry_run:
                result = FetchResult(
                    run_id=None,
                    status=FETCH_RUN_COMPLETED,
                    counters=counters,
                    scope=scope,
                    dry_run=True,
                    notes={
                        "reported_bytes_selected": reported,
                        "unknown_size_files": sum(
                            1 for t in targets if t.reported_bytes is None
                        ),
                        "errors": [],
                        "warnings": [],
                        "stopped": None,
                    },
                    counts=await catalog.fetch_counts(session),
                )
                result.duration_seconds = time.monotonic() - started
                return result

            run_id, started_at = await catalog.create_fetch_run(
                session, scope=scope, dry_run=False
            )
            await session.commit()

            delay = get_settings().nrb_crawl_delay_seconds
            base = filestore.base_dir()
            base.mkdir(parents=True, exist_ok=True)
            pending_rows: list[dict[str, Any]] = []
            client = open_client(
                USER_AGENT, accept="*/*",
                connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
            )
            try:  # noqa: PLR1702 — the inner finally is the resumability guarantee
                for index, target in enumerate(targets, start=1):
                    if max_bytes is not None and counters["bytes_downloaded"] >= max_bytes:
                        counters["files_skipped"] = len(targets) - index + 1
                        stopped = (
                            f"byte budget reached ({counters['bytes_downloaded']} of "
                            f"{max_bytes}); {counters['files_skipped']} files not attempted"
                        )
                        logger.warning("NRB fetch: %s", stopped)
                        break

                    outcome = await fetch_one(client, target, base=base)
                    pending_rows.append(_row_for(outcome, run_id=run_id, now=started_at))

                    if outcome.ok:
                        counters["files_fetched"] += 1
                        counters["bytes_downloaded"] += outcome.length or 0
                        if outcome.duplicate:
                            counters["files_deduplicated"] += 1
                        else:
                            counters["bytes_stored"] += outcome.length or 0
                        if outcome.type_mismatch:
                            warnings.append(f"{target.source_url}: {outcome.type_mismatch}")
                    else:
                        counters["files_failed"] += 1
                        errors.append(f"{target.source_url}: {outcome.error}")
                        logger.warning(
                            "NRB fetch: %s — %s", target.source_url, outcome.error
                        )

                    if len(pending_rows) >= RECORD_BATCH:
                        await catalog.record_fetch_outcomes(session, pending_rows)
                        await session.commit()
                        pending_rows = []
                        logger.info(
                            "NRB fetch: %d/%d done (%d ok, %d failed, %.1f MB)",
                            index, len(targets), counters["files_fetched"],
                            counters["files_failed"],
                            counters["bytes_downloaded"] / 1_048_576,
                        )
                    if delay:
                        await asyncio.sleep(delay)
            except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
                # The run row must not be left saying `running` forever after a
                # crash: an operator reading it would take a dead pass for a live
                # one. The rows written so far stand — see the finally below.
                await catalog.finish_fetch_run(
                    session, run_id, status=FETCH_RUN_FAILED, counters=counters,
                    notes={"errors": [f"{type(exc).__name__}: {exc}"], "stopped": "crashed"},
                )
                await session.commit()
                logger.exception("NRB fetch: failed")
                raise
            finally:
                await client.aclose()
                # Whatever is in hand is committed even on the way out of an
                # exception: the blobs are already on disk, and losing the rows
                # that point at them is the one avoidable waste here.
                if pending_rows:
                    await catalog.record_fetch_outcomes(session, pending_rows)
                    await session.commit()

            counters["error_count"] = len(errors)
            status = (
                FETCH_RUN_COMPLETED
                if not counters["files_failed"] and stopped is None
                else FETCH_RUN_PARTIAL
            )
            notes = {
                "errors": errors[:NOTE_SAMPLE],
                "warnings": warnings[:NOTE_SAMPLE],
                "warning_count": len(warnings),
                "reported_bytes_selected": reported,
                "stopped": stopped,
            }
            await catalog.finish_fetch_run(
                session, run_id, status=status, counters=counters, notes=notes
            )
            await session.commit()
            result = FetchResult(
                run_id=run_id,
                status=status,
                counters=counters,
                notes=notes,
                scope=scope,
                counts=await catalog.fetch_counts(session),
            )

    result.duration_seconds = time.monotonic() - started
    logger.info(
        "NRB fetch: %s — %d fetched, %d failed, %.1f MB downloaded",
        status, counters["files_fetched"], counters["files_failed"],
        counters["bytes_downloaded"] / 1_048_576,
    )
    return result


# Re-exported so callers catch one exception type from this module rather than
# reaching into `locks`.
FetchBusy = LockBusy

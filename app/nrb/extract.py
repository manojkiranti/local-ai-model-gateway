"""One native-extraction pass over fetched NRB blobs.

Same shape as `fetch.py` — select, work, record in batches, hold an advisory lock
— and for the same reasons, minus the network: **Phase 6A makes no HTTP request
at all.** It reads local blobs by `storage_key` through `filestore.resolve_path`,
which refuses anything escaping the base directory, and verifies each blob
against the sha256 in its own filename before parsing.

THE MANIFEST NAMES FILES; THIS PASS EXTRACTS BLOBS
    The resolution runs one way only:

        manifest comparison_key
          -> nrb_files row (matched on comparison_key, never on a URL)
          -> content_sha256 + storage_key of the bytes already on disk
          -> ONE extraction target per distinct sha

    A URL in a manifest is never an input here — there is no fetch in this
    module and no code path that opens a path the catalog did not supply. Nothing
    scans a directory. A key the catalog does not know is reported missing and is
    not substituted, and an unfetched key is reported unfetched and is not
    replaced by some other file to make the count up. **The frozen cohort is the
    benchmark; this pass measures whatever part of it exists and says which part
    that was.**

RESUMABLE, NOT IDEMPOTENT — the distinction Phase 5 draws
    Selection is "fetched blobs with no extraction at THIS `extractor_version`",
    so a second pass takes the next blobs rather than redoing the last ones, and
    an interrupted pass keeps its progress. A repeat pass over an exhausted scope
    selects zero. Bumping `EXTRACTOR_VERSION` makes the corpus selectable again
    without deleting a row — and a row written by an OLDER extractor never makes
    a blob current, because "current" is an exact `(content_sha256,
    extractor_version)` match and nothing else.

WHY THE BLOB IS VERIFIED BEFORE PARSING
    The path IS the checksum (`filestore`), so a blob that no longer hashes to
    its own filename is corrupt on disk — and a truncated PDF is exactly the
    input that produces plausible-looking partial text. Recording `failed` for it
    is cheaper than discovering it in the numbers.

FAILURE ISOLATION
    `extraction.extract_file` never raises; a bad file becomes a `failed` row and
    the pass continues. That is not defensive coding, it is the requirement: a
    profiling batch that dies on file 200 of 400 has measured nothing. What is
    NOT swallowed is a database or programming error — those propagate, because
    turning one into a recorded `extracted` row would be a fabricated
    measurement.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import catalog, filestore, profile, sniff
from .catalog import ExtractTarget
from .extraction import EXTRACTOR_VERSION, ExtractionResult, extract_file
from .locks import EXTRACT_LOCK_KEY, advisory_lock
from .quality import STATUS_FAILED

logger = logging.getLogger("app.nrb.extract")

__all__ = [
    "COMMIT_EVERY",
    "ExtractResult",
    "manifest_rank",
    "order_targets",
    "run_extract",
    "verify_blob",
]

# Results are written every N blobs, so an interrupted pass keeps its progress.
# Matches `fetch.COMMIT_EVERY`'s intent: small enough that a Ctrl-C costs little,
# large enough that the pass is not a commit per file.
COMMIT_EVERY = 25

# How many failures/samples a run record keeps. The full record is the table.
NOTE_SAMPLE = 25

# Bytes read per hash chunk. A blob can be 46 MB; the verification must not need
# to hold it in memory when `extract_file` is about to do that anyway.
_HASH_CHUNK = 1024 * 1024


@dataclass
class ExtractResult:
    """One pass, in numbers. Source-level and blob-level accounting stay apart."""

    status: str
    dry_run: bool
    extractor_version: str
    scope: dict[str, Any]
    counters: dict[str, int]
    cohort: dict[str, Any] | None            # `profile.Cohort.as_dict()`, or None
    counts: dict[str, int]                   # whole-catalog extraction state
    notes: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    # sha -> verdict, for a pass with NO cohort (`--section`, `--owner`, `--all`).
    # A cohort carries its own, over the whole cohort rather than only what this
    # pass touched, and the report prefers those. Without this a non-benchmark
    # pass would report "49 blobs persisted" and no statuses at all.
    verdicts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def _counters() -> dict[str, int]:
    """Every counter, always present and always zero-initialised.

    A missing key and a zero mean different things to a reader, and a summary
    that omits `blobs_failed` because nothing failed is indistinguishable from
    one that forgot to count it.
    """
    return {
        "blobs_selected": 0,
        "blobs_attempted": 0,
        "blobs_persisted": 0,
        "blobs_failed": 0,
        "blobs_missing_on_disk": 0,
        "blobs_corrupt_on_disk": 0,
        "pages_read": 0,
    }


def verify_blob(path: Path, expected_sha256: str) -> str | None:
    """Hash the file on disk and compare it with the sha in its own name.

    Returns None when the bytes are what they claim to be, or a short reason
    otherwise. The message never carries the path — the same rule
    `app/files/documents.py` follows, because these strings reach the database.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
    except FileNotFoundError:
        return "blob is missing from the store"
    except OSError as exc:
        return f"OSError: {exc.strerror or 'unreadable'}"
    if digest.hexdigest() != expected_sha256:
        return "blob does not hash to its own storage key"
    return None


def _row_for(
    target: ExtractTarget, result: ExtractionResult, *, extractor_version: str, now
) -> dict[str, Any]:
    """One `nrb_extractions` row, built from the blob's own measurements.

    The promoted severity columns are read from `result.metrics` rather than
    recomputed here: the metric set is `quality.py`'s to define, and a second
    computation is a second answer waiting to disagree with the first.
    """
    metrics = result.metrics or {}
    return {
        "content_sha256": target.content_sha256,
        "extractor_version": extractor_version,
        "parser": result.parser,
        "media_family": result.family,
        "status": result.status,
        "reason": result.reason,
        "warnings": list(result.warnings),
        "page_count": result.page_count,
        "pages_with_text": result.pages_with_text,
        "text_page_coverage": result.text_page_coverage,
        "median_chars_per_page": metrics.get("median_chars_per_page"),
        "median_chars_per_text_page": metrics.get("median_chars_per_text_page"),
        "char_count": result.char_count,
        "devanagari_ratio": result.devanagari_ratio,
        "legacy_line_ratio": metrics.get("legacy_line_ratio"),
        "legacy_lines": metrics.get("legacy_lines"),
        "judged_lines": metrics.get("judged_lines"),
        "metrics": metrics,
        "preview": result.preview or None,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "extracted_at": now,
    }


def _failed_row(
    target: ExtractTarget, error: str, *, extractor_version: str, now
) -> dict[str, Any]:
    """A blob that never reached a parser: missing, corrupt, or unreadable.

    Recorded rather than skipped. A silent skip would leave the blob `pending`
    forever and make every later pass re-attempt it, and the gap would never
    appear in a status count — which is precisely how a corrupt file becomes an
    invisible hole in a benchmark.
    """
    return {
        "content_sha256": target.content_sha256,
        "extractor_version": extractor_version,
        "parser": "none",
        "media_family": sniff.family_for(target.sniffed_mime),
        "status": STATUS_FAILED,
        "reason": "parser_error",
        "warnings": [],
        "page_count": None,
        "pages_with_text": None,
        "text_page_coverage": None,
        "median_chars_per_page": None,
        "median_chars_per_text_page": None,
        "char_count": 0,
        "devanagari_ratio": None,
        "legacy_line_ratio": None,
        "legacy_lines": None,
        "judged_lines": None,
        "metrics": {},
        "preview": None,
        "error": error,
        "duration_ms": 0,
        "extracted_at": now,
    }


def order_targets(
    targets: Sequence[ExtractTarget], *, rank: dict[str, int] | None = None
) -> list[ExtractTarget]:
    """Deterministic work order, independent of what SQL returned.

    With a manifest, `rank` maps each blob's sha to the position of the EARLIEST
    cohort entry that resolves to it, so the order is the frozen benchmark's own
    canonical order and `--limit 10` means "the first ten of the benchmark" —
    reproducible, and the same ten every time. Without one, sha order, which is
    stable but arbitrary.

    The sha is always the final tiebreak, so two blobs sharing a rank (they
    cannot, but the ordering must be total anyway) never swap between runs.
    """
    ranks = rank or {}
    fallback = len(ranks)
    return sorted(
        targets,
        key=lambda t: (ranks.get(t.content_sha256, fallback), t.content_sha256),
    )


def manifest_rank(cohort: profile.Cohort) -> dict[str, int]:
    """sha -> the position of the first cohort key that resolves to it.

    `cohort.keys` preserves the requested order, which for a benchmark is the
    manifest's canonical (sample-rank) order.
    """
    rank: dict[str, int] = {}
    for position, key in enumerate(cohort.keys):
        if key.fetched and key.content_sha256 and key.content_sha256 not in rank:
            rank[key.content_sha256] = position
    return rank


async def run_extract(
    *,
    keys: Sequence[str] | None = None,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    limit: int | None = None,
    force: bool = False,
    extractor_version: str = EXTRACTOR_VERSION,
    dry_run: bool = False,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    base_dir: Path | None = None,
) -> ExtractResult:
    """Select, extract, record. The whole extraction pass.

    `dry_run` resolves the cohort, deduplicates the blobs, works out exactly
    which of them are pending at this version and reports that — and calls no
    parser, writes no row and opens no blob. It is the same promise
    `nrb_fetch.py --dry-run` makes about the network, for the same reason: the
    cost being previewed (CPU over hundreds of documents) must not be paid by
    the preview.

    `keys` is the frozen benchmark cohort. It changes *which* blobs are selected
    and nothing else; it can only ever select rows the catalog already holds.
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
        "years": list(years or []),
        # The COUNT, not the keys: the manifest file is the durable record of
        # which files those were, and a 400-element list in every log line is
        # unreadable.
        "manifest_keys": len(set(keys or ())),
        "limit": limit,
        "force": force,
    }
    failures: list[str] = []
    verdicts: dict[str, Any] = {}

    async with advisory_lock(engine, EXTRACT_LOCK_KEY, what="NRB extraction"):
        async with session_factory() as session:
            # Resolved BEFORE selection, and reported whether or not anything is
            # extracted: selection returns only the pending blobs, so the
            # cohort's own accounting (fetched / unfetched / missing / already
            # extracted / duplicate content) is not recoverable from it.
            cohort: profile.Cohort | None = None
            rank: dict[str, int] | None = None
            if keys:
                cohort = await profile.load_cohort(
                    session, keys=keys, extractor_version=extractor_version
                )
                rank = manifest_rank(cohort)
                if cohort.missing:
                    logger.warning(
                        "NRB extraction: %d of %d cohort keys are not in the "
                        "catalog", len(cohort.missing), cohort.requested,
                    )
                if cohort.unfetched:
                    logger.warning(
                        "NRB extraction: %d of %d cohort files are not fetched "
                        "yet — they are reported, not substituted",
                        cohort.unfetched, cohort.requested,
                    )

            # `limit` is applied AFTER ordering, in Python, so it selects the
            # first n of the benchmark rather than the first n of a SQL scan.
            targets = order_targets(
                await catalog.select_extract_targets(
                    session,
                    extractor_version=extractor_version,
                    sections=sections,
                    owners=owners,
                    resource_types=resource_types,
                    years=years,
                    keys=keys,
                    force=force,
                ),
                rank=rank,
            )
            if limit is not None:
                targets = targets[: max(limit, 0)]
            counters["blobs_selected"] = len(targets)

            if not dry_run:
                await _extract_all(
                    session,
                    targets,
                    counters=counters,
                    failures=failures,
                    extractor_version=extractor_version,
                    base_dir=base_dir,
                )
                # Reloaded so the report describes the state AFTER the pass
                # rather than the state it started from.
                if keys:
                    cohort = await profile.load_cohort(
                        session, keys=keys, extractor_version=extractor_version
                    )
                else:
                    # No cohort to carry them, so the pass reports the verdicts
                    # for the blobs it just wrote — read back from the table
                    # rather than remembered, so what is reported is what was
                    # actually persisted.
                    verdicts = await profile.load_verdicts(
                        session,
                        shas=[target.content_sha256 for target in targets],
                        extractor_version=extractor_version,
                    )

            counts = await catalog.extraction_counts(
                session, extractor_version=extractor_version
            )

    return ExtractResult(
        status="completed",
        dry_run=dry_run,
        extractor_version=extractor_version,
        scope=scope,
        counters=counters,
        cohort=cohort.as_dict() if cohort is not None else None,
        counts=counts,
        notes={
            "failures": failures[:NOTE_SAMPLE],
            "failure_count": len(failures),
        },
        duration_seconds=time.monotonic() - started,
        verdicts=verdicts,
    )


async def _extract_all(
    session: AsyncSession,
    targets: Sequence[ExtractTarget],
    *,
    counters: dict[str, int],
    failures: list[str],
    extractor_version: str,
    base_dir: Path | None,
) -> None:
    """Extract every target, committing in batches. One bad blob never aborts it.

    The try covers reading and parsing ONE blob. It deliberately does not cover
    the commit: a database error is not a property of the document, and recording
    it as a failed extraction would put a fabricated measurement in the table.
    """
    pending: list[dict[str, Any]] = []
    for index, target in enumerate(targets, start=1):
        now = datetime.now(timezone.utc)
        try:
            path = filestore.resolve_path(target.storage_key, base_dir)
            problem = verify_blob(path, target.content_sha256)
            if problem is not None:
                if "missing" in problem:
                    counters["blobs_missing_on_disk"] += 1
                else:
                    counters["blobs_corrupt_on_disk"] += 1
                row = _failed_row(
                    target, problem, extractor_version=extractor_version, now=now
                )
            else:
                result = extract_file(
                    path,
                    family=sniff.family_for(target.sniffed_mime),
                    extension=target.extension,
                )
                row = _row_for(
                    target, result, extractor_version=extractor_version, now=now
                )
                counters["pages_read"] += result.page_count or 0
        except Exception as exc:  # noqa: BLE001 - one blob must never kill a pass
            # `extract_file` does not raise, so reaching here means the store
            # refused the key or the filesystem did something unexpected. Still a
            # recorded outcome, still never a stack trace or a path in the row.
            logger.warning(
                "NRB extraction: blob %s could not be read (%s)",
                target.content_sha256[:12], type(exc).__name__,
            )
            row = _failed_row(
                target, f"{type(exc).__name__}: unreadable blob",
                extractor_version=extractor_version, now=now,
            )
            counters["blobs_corrupt_on_disk"] += 1

        counters["blobs_attempted"] += 1
        if row["status"] == STATUS_FAILED:
            counters["blobs_failed"] += 1
            failures.append(f"{target.content_sha256[:12]}: {row['error']}")
        pending.append(row)

        if len(pending) >= COMMIT_EVERY or index == len(targets):
            await catalog.record_extractions(session, pending)
            await session.commit()
            counters["blobs_persisted"] += len(pending)
            logger.info(
                "NRB extraction: %d/%d blobs recorded", index, len(targets)
            )
            pending = []

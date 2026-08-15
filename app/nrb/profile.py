"""The read-time view of a benchmark cohort: what it names, and what came back.

This is the one place that turns a list of `comparison_key`s into the two
different populations a Phase 6A report has to keep apart:

    400 comparison_keys        <- the frozen benchmark. Source/file identity.
      |  (some not fetched yet, some may not be in the catalog at all)
      v
    N unique content_sha256    <- the extraction unit. Blob identity.

**They are not the same number and must never be printed as though they were.**
A cohort of 400 whose files include two byte-identical PDFs is 399 blobs, and a
cohort 380 of which are downloaded is 380 acquisitions of a 400-file benchmark.
Collapsing either gap produces a report that reads as complete while measuring
something smaller — `select_extract_targets` returns only the *pending* slice, so
without this a cohort already extracted would look like a cohort that had lost
its files.

WHY THE SOURCE METADATA IS NOT HERE
    `nrb_extractions` is content-intrinsic: every column is a function of the
    bytes, because one blob is shared by several sources and a title-derived
    verdict would depend on which source the pass reached first (see
    `models.NRBExtraction`). So year, document type and owner are joined back at
    REPORT time — and for a frozen benchmark they come from the manifest's own
    entries rather than from a fresh catalog read, because the catalog moves: a
    source re-typed by a later sync must not silently re-label a cohort that has
    already been profiled. `report.summarize_extraction` does that join, purely.

WHY VERDICTS ARE KEYED BY SHA AND NOT BY FILE
    Two manifest entries sharing bytes get the SAME verdict object here, and that
    is the point: one blob, one extraction, one answer, reported against both
    entries. Deduplication, not benchmark shrinkage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .catalog import bounded_keys
from .models import FETCH_FETCHED, NRBExtraction, NRBFile

__all__ = [
    "BlobVerdict",
    "Cohort",
    "CohortKey",
    "load_cohort",
    "load_verdicts",
]


@dataclass(frozen=True)
class CohortKey:
    """One requested `comparison_key` and its acquisition state."""

    comparison_key: str
    fetch_status: str
    content_sha256: str | None

    @property
    def fetched(self) -> bool:
        return self.fetch_status == FETCH_FETCHED and bool(self.content_sha256)


@dataclass(frozen=True)
class BlobVerdict:
    """One `nrb_extractions` row at the current extractor version.

    Engine-neutral on purpose. `parser` is carried as a fact, not as a branch:
    the later Docling calibration writes rows of exactly this shape through the
    same classifier, so nothing downstream may key on pypdf-specific internals.
    """

    content_sha256: str
    parser: str
    media_family: str
    status: str
    reason: str
    warnings: tuple[str, ...]
    page_count: int | None
    pages_with_text: int | None
    text_page_coverage: float | None
    median_chars_per_text_page: float | None
    char_count: int
    devanagari_ratio: float | None
    legacy_line_ratio: float | None
    legacy_lines: int | None
    judged_lines: int | None
    metrics: dict[str, Any]
    preview: str | None
    error: str | None
    duration_ms: int | None


@dataclass(frozen=True)
class Cohort:
    """A benchmark cohort resolved against the catalog and the extraction table.

    Every count a report needs is derived here rather than recomputed by each
    caller, so the source/blob distinction cannot drift between the pass's
    summary and the profile's.
    """

    requested: int                      # distinct keys asked for
    duplicate_keys: int                 # entries collapsed into those
    keys: tuple[CohortKey, ...]         # the ones the catalog knows
    missing: tuple[str, ...]            # the ones it does not — a real defect
    verdicts: dict[str, BlobVerdict]    # sha -> verdict at THIS version
    extractor_version: str

    # --- source/file population ------------------------------------------ #
    @property
    def by_fetch_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for key in self.keys:
            counts[key.fetch_status] = counts.get(key.fetch_status, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def fetched_keys(self) -> tuple[CohortKey, ...]:
        return tuple(key for key in self.keys if key.fetched)

    @property
    def unfetched(self) -> int:
        """Requested minus acquired — including keys the catalog never had.

        The manifest is the denominator, not the subset that happens to be on
        disk, which is the whole reason this property exists rather than a
        `len(fetched)` at each call site.
        """
        return self.requested - len(self.fetched_keys)

    # --- blob population -------------------------------------------------- #
    @property
    def shas(self) -> tuple[str, ...]:
        """Unique blobs behind the fetched keys, in stable sha order."""
        return tuple(sorted({key.content_sha256 for key in self.fetched_keys
                             if key.content_sha256}))

    @property
    def duplicates_collapsed(self) -> int:
        """Fetched FILES minus unique BLOBS. Deduplication, not shrinkage."""
        return len(self.fetched_keys) - len(self.shas)

    @property
    def extracted_shas(self) -> tuple[str, ...]:
        return tuple(sha for sha in self.shas if sha in self.verdicts)

    @property
    def pending_shas(self) -> tuple[str, ...]:
        return tuple(sha for sha in self.shas if sha not in self.verdicts)

    def keys_for(self, sha: str) -> tuple[str, ...]:
        """Which cohort entries a blob's single verdict speaks for."""
        return tuple(sorted(key.comparison_key for key in self.fetched_keys
                            if key.content_sha256 == sha))

    def as_dict(self, *, sample: int = 25) -> dict[str, Any]:
        """JSON-ready accounting. Source-level and blob-level, never merged."""
        return {
            "extractor_version": self.extractor_version,
            "source": {
                "requested": self.requested,
                "duplicate_entries": self.duplicate_keys,
                "in_catalog": len(self.keys),
                "missing_from_catalog": len(self.missing),
                "fetched": len(self.fetched_keys),
                "unfetched": self.unfetched,
                "by_fetch_status": self.by_fetch_status,
                "missing_sample": list(self.missing[:sample]),
            },
            "blob": {
                "unique_fetched": len(self.shas),
                "duplicates_collapsed": self.duplicates_collapsed,
                "already_extracted": len(self.extracted_shas),
                "pending_extraction": len(self.pending_shas),
            },
        }


async def load_cohort(
    session: AsyncSession, *, keys: Sequence[str], extractor_version: str
) -> Cohort:
    """Resolve exact cohort keys to catalog rows and current-version verdicts.

    Read-only, and it reports rather than filters: a key the catalog does not
    know comes back in `missing` instead of being dropped, because a manifest
    drifting away from the corpus is exactly the failure a frozen benchmark must
    not paper over. Same rule as `catalog.resolve_manifest_keys`, which answers
    the acquisition half of the same question.

    Bounded by `catalog.MANIFEST_MAX_KEYS` through `bounded_keys`, so this
    cannot become a whole-corpus scan by being handed a bigger list.
    """
    distinct = bounded_keys(keys)
    if not distinct:
        return Cohort(0, 0, (), (), {}, extractor_version)

    rows = (
        await session.execute(
            select(
                NRBFile.comparison_key,
                NRBFile.fetch_status,
                NRBFile.content_sha256,
            ).where(NRBFile.comparison_key.in_(distinct))
        )
    ).all()
    found = {
        comparison_key: CohortKey(comparison_key, fetch_status, sha)
        for comparison_key, fetch_status, sha in rows
    }
    # Requested order is the manifest's canonical order, and it is preserved:
    # `--limit` ranks by it, so a limited pass is the FIRST n of the frozen
    # cohort rather than the first n of whatever the database returned.
    cohort_keys = tuple(found[key] for key in distinct if key in found)
    missing = tuple(key for key in distinct if key not in found)

    shas = sorted({key.content_sha256 for key in cohort_keys
                   if key.fetched and key.content_sha256})
    verdicts = await load_verdicts(
        session, shas=shas, extractor_version=extractor_version
    )

    return Cohort(
        requested=len(distinct),
        duplicate_keys=len(keys) - len(distinct),
        keys=cohort_keys,
        missing=missing,
        verdicts=verdicts,
        extractor_version=extractor_version,
    )


async def load_verdicts(
    session: AsyncSession, *, shas: Sequence[str], extractor_version: str
) -> dict[str, BlobVerdict]:
    """Current-version verdicts for a set of blobs. Read-only.

    **Exact `(content_sha256, extractor_version)` matches only.** A row written by
    an OLDER extractor is a previous answer to a question the current rules would
    answer differently, so it does not make a blob current and does not appear
    here — which is the entire point of versioning the extractor. That is also
    why this reads by sha (the leading column of
    `ux_nrb_extractions_content_version`) rather than scanning for
    `extractor_version <> current`; the version-only staleness query stays an
    occasional operator scan, not something the pass or the report runs.

    Split out of `load_cohort` because a pass scoped by section or owner has no
    cohort and still has to report its verdicts — a report that showed 49 blobs
    persisted and no statuses would be worse than no report.
    """
    distinct = sorted(set(shas))
    if not distinct:
        return {}
    from .catalog import BATCH

    verdicts: dict[str, BlobVerdict] = {}
    for start in range(0, len(distinct), BATCH):
        chunk = distinct[start : start + BATCH]
        rows = (
            await session.execute(
                select(NRBExtraction).where(
                    NRBExtraction.content_sha256.in_(chunk),
                    NRBExtraction.extractor_version == extractor_version,
                )
            )
        ).scalars().all()
        for row in rows:
            verdicts[row.content_sha256] = BlobVerdict(
                content_sha256=row.content_sha256,
                parser=row.parser,
                media_family=row.media_family,
                status=row.status,
                reason=row.reason,
                warnings=tuple(row.warnings or ()),
                page_count=row.page_count,
                pages_with_text=row.pages_with_text,
                text_page_coverage=row.text_page_coverage,
                median_chars_per_text_page=row.median_chars_per_text_page,
                char_count=row.char_count,
                devanagari_ratio=row.devanagari_ratio,
                legacy_line_ratio=row.legacy_line_ratio,
                legacy_lines=row.legacy_lines,
                judged_lines=row.judged_lines,
                metrics=dict(row.metrics or {}),
                preview=row.preview,
                error=row.error,
                duration_ms=row.duration_ms,
            )
    return verdicts

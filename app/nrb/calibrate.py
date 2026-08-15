"""One pypdf-vs-Docling comparison pass over the frozen calibration subset.

Phase 6A screens the corpus with pypdf. This pass is the evidence for that
choice: the same PDFs read by both engines, both outputs scored by the SAME
`quality.py` classifier at the SAME thresholds, and the disagreements counted in
both directions.

WHAT IS COMPARED — EXTRACTION, NOT PIPELINES
    Not `parsing.parse_to_chunks`. That is RAG's pipeline (Docling, then
    `merge_blocks`, then `drop_small_blocks`, then front-matter skipping, then
    chunking), so a disagreement there could come from the chunk filter rather
    than from what Docling read off the page, and the number would not mean what
    it claimed to. `extraction.docling_extract` walks Docling's own item stream
    with no filtering, and `extraction.result_from_pages` scores both sides.

TWO POPULATIONS, AS EVERYWHERE ELSE IN PHASE 6A
    The subset names 40 FILES; the comparison runs over unique BLOBS. Two subset
    files with identical bytes are one comparison, reported against both keys —
    `BlobComparison.comparison_keys` is how the result finds its way back to the
    benchmark. Parsing that PDF twice would double the most expensive step in the
    phase to learn nothing.

WHAT THIS PASS NEVER DOES
    It writes NOTHING. `nrb_extractions` is the canonical screen at a specific
    `extractor_version`; this is bounded experimental calibration data, and mixing
    the two would make the official table's meaning depend on which engine
    happened to run last. The session here is read-only, there is no advisory lock
    because there is nothing to serialise, and there is no migration.

    It also makes no HTTP request and never fetches: a subset member that is not
    on disk is reported as not-fetched and is NEVER substituted with another file
    to make the count up. The frozen 40 are the frozen 40.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import catalog, filestore, profile, sniff
from .calibration import CalibrationSubset
from .catalog import ExtractTarget
from .extraction import (
    EXTRACTOR_VERSION,
    DoclingEngine,
    ExtractionResult,
    extract_file,
)
from .extract import manifest_rank, order_targets, verify_blob
from .quality import STATUS_EXTRACTED, STATUS_FAILED, STATUS_SUSPICIOUS

logger = logging.getLogger("app.nrb.calibrate")

__all__ = [
    "CATEGORIES",
    "BlobComparison",
    "CalibrationResult",
    "ENGINE_DOCLING",
    "ENGINE_PYPDF",
    "ParserSide",
    "USABLE_STATUSES",
    "categorize",
    "run_calibration",
    "side_from",
]

ENGINE_PYPDF = "pypdf"
ENGINE_DOCLING = "docling"

# "Usable" is `extracted` and nothing else, and that is the whole basis of the
# word "rescued" below. `suspicious` means text came out that nobody should index
# unreviewed; `needs_ocr` means no usable text layer at all. Counting either as a
# success would let a rescue rate be reported for documents that are still
# unreadable. Ties break toward doubt here for the same reason `quality.classify`
# does.
USABLE_STATUSES = (STATUS_EXTRACTED,)

# Mutually exclusive; every ordered pair of statuses maps to exactly one.
CATEGORIES = (
    "both_extracted",
    "both_suspicious",
    "both_failed",
    "agreed_other",
    "docling_rescued_pypdf",
    "pypdf_rescued_docling",
    "disagreed_neither_usable",
)


@dataclass(frozen=True)
class ParserSide:
    """One engine's answer about one blob. Parser-neutral by construction.

    Every field is something both engines produce. `parser` is carried as a fact,
    never branched on: the moment the model knows which engine it is describing,
    the comparison stops being like-for-like.
    """

    engine: str
    parser: str
    status: str
    reason: str
    warnings: tuple[str, ...]
    char_count: int
    devanagari_ratio: float | None
    legacy_line_ratio: float | None
    text_page_coverage: float | None
    page_count: int | None
    pages_with_text: int | None
    median_chars_per_text_page: float | None
    duration_ms: int
    preview: str
    error: str | None
    metrics: dict[str, Any]

    @property
    def usable(self) -> bool:
        return self.status in USABLE_STATUSES

    def as_dict(self, *, preview_chars: int | None = None) -> dict[str, Any]:
        preview = self.preview
        if preview_chars is not None:
            preview = preview[:preview_chars]
        return {
            "engine": self.engine,
            "status": self.status,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "char_count": self.char_count,
            "devanagari_ratio": self.devanagari_ratio,
            "legacy_line_ratio": self.legacy_line_ratio,
            "text_page_coverage": self.text_page_coverage,
            "page_count": self.page_count,
            "pages_with_text": self.pages_with_text,
            "duration_ms": self.duration_ms,
            "preview": preview,
            "error": self.error,
        }


def side_from(result: ExtractionResult, *, engine: str) -> ParserSide:
    """Flatten one engine's `ExtractionResult` into a comparable side.

    The severity metrics are read out of `metrics` rather than recomputed, so both
    sides carry the numbers their own classification was actually made on.
    """
    metrics = dict(result.metrics or {})
    return ParserSide(
        engine=engine,
        parser=result.parser,
        status=result.status,
        reason=result.reason,
        warnings=tuple(result.warnings or ()),
        char_count=result.char_count,
        devanagari_ratio=result.devanagari_ratio,
        legacy_line_ratio=metrics.get("legacy_line_ratio"),
        text_page_coverage=result.text_page_coverage,
        page_count=result.page_count,
        pages_with_text=result.pages_with_text,
        median_chars_per_text_page=metrics.get("median_chars_per_text_page"),
        duration_ms=result.duration_ms,
        preview=result.preview,
        error=result.error,
        metrics=metrics,
    )


def categorize(native: ParserSide, docling: ParserSide) -> str:
    """The one category this pair falls in. Deterministic, first match wins.

    **"A rescued B" means: B's verdict is not usable and A's is.** Not "A read
    more characters", not "A disagreed" — a rescue is specifically the case where
    one engine produced text a reader could trust and the other did not, because
    that is the only disagreement that would change the choice of screen.
    """
    if native.status == docling.status:
        if native.status == STATUS_EXTRACTED:
            return "both_extracted"
        if native.status == STATUS_SUSPICIOUS:
            return "both_suspicious"
        if native.status == STATUS_FAILED:
            return "both_failed"
        return "agreed_other"
    if docling.usable and not native.usable:
        return "docling_rescued_pypdf"
    if native.usable and not docling.usable:
        return "pypdf_rescued_docling"
    return "disagreed_neither_usable"


@dataclass(frozen=True)
class BlobComparison:
    """Both engines on ONE blob, with every subset file that blob stands for."""

    content_sha256: str
    comparison_keys: tuple[str, ...]
    native: ParserSide
    docling: ParserSide

    @property
    def status_agreement(self) -> bool:
        return self.native.status == self.docling.status

    @property
    def reason_agreement(self) -> bool:
        return self.native.reason == self.docling.reason

    @property
    def category(self) -> str:
        return categorize(self.native, self.docling)

    def as_dict(self, *, preview_chars: int | None = None) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "comparison_keys": list(self.comparison_keys),
            "category": self.category,
            "status_agreement": self.status_agreement,
            "reason_agreement": self.reason_agreement,
            ENGINE_PYPDF: self.native.as_dict(preview_chars=preview_chars),
            ENGINE_DOCLING: self.docling.as_dict(preview_chars=preview_chars),
        }


@dataclass
class CalibrationResult:
    """One calibration pass, in numbers. Nothing here was written to the DB."""

    status: str
    dry_run: bool
    subset_path: str | None
    subset_selection_sha256: str
    parent_selection_sha256: str
    counters: dict[str, int]
    cohort: dict[str, Any] | None        # `profile.Cohort.as_dict()`
    comparisons: tuple[BlobComparison, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    docling_init_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def _counters() -> dict[str, int]:
    """Every counter, always present and always zero-initialised — a missing key
    and a zero mean different things to a reader."""
    return {
        "subset_entries": 0,
        "subset_files_in_catalog": 0,
        "subset_files_fetched": 0,
        "blobs_selected": 0,
        "comparisons_run": 0,
        "blobs_missing_on_disk": 0,
        "blobs_corrupt_on_disk": 0,
        "pypdf_failed": 0,
        "docling_failed": 0,
    }


NativeExtract = Callable[[Path, str, "str | None"], ExtractionResult]


def _default_native(path: Path, family: str, extension: str | None):
    return extract_file(path, family=family, extension=extension)


async def _select(
    session: AsyncSession,
    subset: CalibrationSubset,
    *,
    limit: int | None,
    extractor_version: str,
) -> tuple[profile.Cohort, list[ExtractTarget]]:
    """Resolve the frozen keys to catalog rows, then to unique local blobs.

    One direction only:

        subset comparison_key
          -> nrb_files row (matched on comparison_key, never on a URL)
          -> content_sha256 + storage_key of bytes already on disk
          -> ONE comparison target per distinct sha

    `force=True` because the calibration is independent of the screen's own
    bookkeeping: a blob already extracted at the current `extractor_version` is
    still a valid subject for the parser comparison, and filtering it out would
    make the comparison set depend on when the screen last ran.
    """
    cohort = await profile.load_cohort(
        session, keys=subset.keys(), extractor_version=extractor_version
    )
    targets = order_targets(
        await catalog.select_extract_targets(
            session,
            extractor_version=extractor_version,
            keys=subset.keys(),
            resource_types=[subset.resource_type],
            force=True,
        ),
        rank=manifest_rank(cohort),
    )
    if limit is not None:
        targets = targets[: max(limit, 0)]
    return cohort, targets


async def run_calibration(
    *,
    subset: CalibrationSubset,
    subset_path: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    extractor_version: str = EXTRACTOR_VERSION,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    base_dir: Path | None = None,
    native_extract: NativeExtract | None = None,
    docling_engine: Any | None = None,
) -> CalibrationResult:
    """Compare both engines over the frozen subset. Reads only; writes nothing.

    `dry_run` resolves the subset, deduplicates the blobs and reports exactly what
    would be compared — and calls NEITHER parser, opens no blob and builds no
    Docling converter. Docling is minutes per dozen files on CPU; a preview that
    paid that cost would not be a preview.

    `native_extract` and `docling_engine` are injection points for tests, so the
    pass can be exercised without loading torch. In production they default to
    pypdf and one reused Docling converter.
    """
    from ..db.session import SessionLocal

    session_factory = session_factory or SessionLocal
    native_extract = native_extract or _default_native
    started = time.monotonic()
    counters = _counters()
    counters["subset_entries"] = len(subset.entries)
    notes: dict[str, Any] = {"failures": [], "engine": None}
    comparisons: list[BlobComparison] = []
    init_seconds = 0.0

    async with session_factory() as session:
        cohort, targets = await _select(
            session, subset, limit=limit, extractor_version=extractor_version
        )
        counters["subset_files_in_catalog"] = len(cohort.keys)
        counters["subset_files_fetched"] = len(cohort.fetched_keys)
        counters["blobs_selected"] = len(targets)

        if not dry_run and targets:
            engine = docling_engine if docling_engine is not None else DoclingEngine()
            ok, evidence = engine.open()
            notes["engine"] = evidence
            init_seconds = getattr(engine, "init_seconds", 0.0)
            try:
                if not ok:
                    logger.warning("NRB calibrate: %s", evidence)
                comparisons = _compare_all(
                    targets,
                    cohort=cohort,
                    counters=counters,
                    notes=notes,
                    native_extract=native_extract,
                    engine=engine,
                    base_dir=base_dir,
                )
            finally:
                engine.close()

    return CalibrationResult(
        status="completed",
        dry_run=dry_run,
        subset_path=subset_path,
        subset_selection_sha256=subset.subset_selection_sha256,
        parent_selection_sha256=subset.parent_selection_sha256,
        counters=counters,
        cohort=cohort.as_dict(),
        comparisons=tuple(comparisons),
        notes=notes,
        duration_seconds=time.monotonic() - started,
        docling_init_seconds=round(init_seconds, 3),
    )


def _compare_all(
    targets: Sequence[ExtractTarget],
    *,
    cohort: profile.Cohort,
    counters: dict[str, int],
    notes: dict[str, Any],
    native_extract: NativeExtract,
    engine: Any,
    base_dir: Path | None,
) -> list[BlobComparison]:
    """Run both engines over each blob in order. One bad file never stops the run.

    Neither engine raises by contract (`extract_file` and `docling_extract` both
    record failures instead), and a blob missing or corrupt on disk is counted and
    skipped rather than compared — a truncated PDF would produce two plausible
    partial texts and a meaningless agreement.
    """
    base = base_dir or filestore.base_dir()
    comparisons: list[BlobComparison] = []
    for target in targets:
        try:
            path = filestore.resolve_path(target.storage_key, base)
        except ValueError as exc:
            counters["blobs_missing_on_disk"] += 1
            notes["failures"].append(f"{target.content_sha256[:12]}: {exc}")
            continue
        if not path.exists():
            counters["blobs_missing_on_disk"] += 1
            continue
        problem = verify_blob(path, target.content_sha256)
        if problem:
            counters["blobs_corrupt_on_disk"] += 1
            notes["failures"].append(f"{target.content_sha256[:12]}: {problem}")
            continue

        family = sniff.family_for(target.sniffed_mime)
        native = side_from(
            native_extract(path, family, target.extension), engine=ENGINE_PYPDF
        )
        docling = side_from(engine.extract(path), engine=ENGINE_DOCLING)
        counters["pypdf_failed"] += int(native.status == STATUS_FAILED)
        counters["docling_failed"] += int(docling.status == STATUS_FAILED)
        counters["comparisons_run"] += 1
        comparisons.append(
            BlobComparison(
                content_sha256=target.content_sha256,
                comparison_keys=cohort.keys_for(target.content_sha256),
                native=native,
                docling=docling,
            )
        )
    return comparisons

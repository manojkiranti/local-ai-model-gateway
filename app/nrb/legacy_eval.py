"""Phase 6B Task 1 evidence gathering: read blobs, draw a frozen cohort, convert.

The I/O half of the legacy-font evaluation. `legacy_convert.py` holds the rules;
this holds the database reads, the blob reads and the deterministic sampling.

**It writes nothing.** No `nrb_extractions` row is created, updated or deleted, no
blob is rewritten, no chunk, embedding or `documents` row is produced, and no HTTP
request is made — every byte comes from `NRB_FILES_DIR`, which Phase 5 already
filled. The native-1 measurements this evaluation is judged against stay exactly
as committed, which is what makes a before/after comparison meaningful.

**Text is re-extracted, not read back.** `nrb_extractions` deliberately persists
no document text — `preview` is capped at 300 characters by
`ck_nrb_extractions_preview_is_bounded` — so the evaluation re-parses each blob
through the same `extraction.extract_file` that produced the committed rows.
Extraction is a pure function of the bytes, so the text is identical to what
native-1 measured; the profile's `status`/`reason` are re-derived and compared as
a check on exactly that.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import extraction, filestore, sniff
from .models import NRBExtraction, NRBFile

__all__ = [
    "BlobRef",
    "COHORT_ALGORITHM",
    "CohortEntry",
    "SEVERITY_BANDS",
    "band_for",
    "cohort_fingerprint",
    "load_blob_refs",
    "read_blob_text",
    "select_cohort",
]

COHORT_ALGORITHM = "legacy-conversion-eval-v1"

# Severity bands over `legacy_line_ratio`, matching the bands Phase 6A's profile
# already reports so the two documents can be read against each other. The
# evaluation stratifies across them because the failure modes differ by severity:
# the false positives cluster just above 0.20, the unambiguous Preeti sits at
# >= 0.80, and a cohort drawn without regard to that would be mostly one or the
# other depending on which way the corpus happened to lean.
SEVERITY_BANDS = (
    ("0.20-0.50", 0.20, 0.50),
    ("0.50-0.80", 0.50, 0.80),
    ("0.80-1.00", 0.80, 1.01),
)


def band_for(ratio: float) -> str:
    """The severity band a ratio falls in, or `below-0.20` for anything the
    classifier would not have flagged at all."""
    for name, low, high in SEVERITY_BANDS:
        if low <= ratio < high:
            return name
    return "below-0.20"


@dataclass(frozen=True)
class BlobRef:
    """One blob, and everything needed to re-read and re-parse it."""

    content_sha256: str
    storage_key: str
    extension: str | None
    sniffed_mime: str | None
    status: str
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)
    comparison_keys: tuple[str, ...] = ()

    @property
    def family(self) -> str:
        return sniff.family_for(self.sniffed_mime)

    @property
    def legacy_line_ratio(self) -> float:
        return float(self.metrics.get("legacy_line_ratio") or 0.0)

    @property
    def devanagari_ratio(self) -> float:
        return float(self.metrics.get("devanagari_ratio") or 0.0)

    @property
    def short_sha(self) -> str:
        return self.content_sha256[:12]


async def load_blob_refs(
    session: AsyncSession,
    *,
    extractor_version: str,
    reasons: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    shas: Sequence[str] | None = None,
) -> list[BlobRef]:
    """Extraction rows joined to their file rows, in `content_sha256` order.

    Read-only, and the ordering is the identity order rather than an id order:
    Phase 6A learned that selecting in catalog-id order measures the order REST
    paged the corpus (§11, "the benchmark cohort is named, not approximated").
    Ordering on the content hash makes any slice of this reproducible on another
    machine with a different insert history.

    One extraction row can be reached by several file rows (a blob shared by two
    sources — Phase 3 measured 42 such references), so `comparison_keys` is
    aggregated rather than picking whichever row the join reached first.
    """
    stmt = (
        select(
            NRBExtraction.content_sha256,
            NRBExtraction.status,
            NRBExtraction.reason,
            NRBExtraction.metrics,
            NRBFile.storage_key,
            NRBFile.extension,
            NRBFile.sniffed_mime,
            NRBFile.comparison_key,
        )
        .join(NRBFile, NRBFile.content_sha256 == NRBExtraction.content_sha256)
        .where(
            NRBExtraction.extractor_version == extractor_version,
            NRBFile.storage_key.isnot(None),
        )
        .order_by(NRBExtraction.content_sha256, NRBFile.comparison_key)
    )
    if reasons:
        stmt = stmt.where(NRBExtraction.reason.in_(list(reasons)))
    if statuses:
        stmt = stmt.where(NRBExtraction.status.in_(list(statuses)))
    if shas:
        stmt = stmt.where(NRBExtraction.content_sha256.in_(list(shas)))

    merged: dict[str, dict[str, Any]] = {}
    for row in (await session.execute(stmt)).all():
        entry = merged.setdefault(
            row.content_sha256,
            {
                "content_sha256": row.content_sha256,
                "storage_key": row.storage_key,
                "extension": row.extension,
                "sniffed_mime": row.sniffed_mime,
                "status": row.status,
                "reason": row.reason,
                "metrics": dict(row.metrics or {}),
                "keys": [],
            },
        )
        if row.comparison_key:
            entry["keys"].append(row.comparison_key)

    return [
        BlobRef(
            content_sha256=e["content_sha256"],
            storage_key=e["storage_key"],
            extension=e["extension"],
            sniffed_mime=e["sniffed_mime"],
            status=e["status"],
            reason=e["reason"],
            metrics=e["metrics"],
            comparison_keys=tuple(sorted(set(e["keys"]))),
        )
        for e in merged.values()
    ]


def read_blob_text(ref: BlobRef, base_dir: Path | None = None):
    """Re-parse one blob. Returns `extraction.ExtractionResult`; never raises.

    Goes through `filestore.resolve_path`, so a `storage_key` round-tripped
    through the database is still treated as untrusted, and through
    `extraction.extract_file`, so the text is byte-identical to what native-1
    measured rather than a second parser's opinion of the same file.
    """
    path = filestore.resolve_path(ref.storage_key, base_dir)
    return extraction.extract_file(
        path, family=ref.family, extension=ref.extension
    )


@dataclass(frozen=True)
class CohortEntry:
    """One selected blob, with the stratum it was drawn for and its draw rank."""

    content_sha256: str
    band: str
    legacy_line_ratio: float
    status: str
    reason: str
    family: str
    rank: str
    role: str = "legacy"   # legacy | control_english_table | control_unicode | ...
    comparison_keys: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "band": self.band,
            "legacy_line_ratio": self.legacy_line_ratio,
            "status": self.status,
            "reason": self.reason,
            "family": self.family,
            "role": self.role,
            "rank": self.rank,
            "comparison_keys": list(self.comparison_keys),
        }


def _rank(parent_fingerprint: str, band: str, content_sha256: str) -> str:
    """A blob's draw position: deterministic, and independent of input order.

    Bound to the PARENT benchmark fingerprint and to the algorithm name, exactly
    like `manifest.selection_sha256` and the Docling subset id. Two consequences
    that matter: this cohort cannot be silently redrawn against a different
    benchmark, and changing what the algorithm means cannot reuse an existing
    selection. Hashing the blob's own identity (not its index) is what makes a
    shuffled input produce the same cohort.
    """
    h = hashlib.sha256()
    h.update(parent_fingerprint.encode())
    h.update(b"\x00" + COHORT_ALGORITHM.encode())
    h.update(b"\x00" + band.encode())
    h.update(b"\x00" + content_sha256.encode())
    return h.hexdigest()


def select_cohort(
    candidates: Sequence[BlobRef],
    *,
    parent_fingerprint: str,
    per_band: int,
    role: str = "legacy",
) -> list[CohortEntry]:
    """Draw `per_band` blobs from each severity band, deterministically.

    Selection happens BEFORE any converter runs and never looks at a conversion
    outcome — the ranking key is the blob's content hash, which is fixed long
    before this phase existed. That is the guard against the most tempting
    mistake available here: picking the cases that happened to work.

    A band with fewer candidates than `per_band` contributes all of them. It is
    not topped up from another band, because the point of the stratification is
    that the bands are different populations.
    """
    by_band: dict[str, list[BlobRef]] = {name: [] for name, _, _ in SEVERITY_BANDS}
    for ref in candidates:
        band = band_for(ref.legacy_line_ratio)
        if band in by_band:
            by_band[band].append(ref)

    chosen: list[CohortEntry] = []
    for band, refs in by_band.items():
        ranked = sorted(
            refs, key=lambda r: (_rank(parent_fingerprint, band, r.content_sha256),
                                 r.content_sha256)
        )
        for ref in ranked[:per_band]:
            chosen.append(
                CohortEntry(
                    content_sha256=ref.content_sha256,
                    band=band,
                    legacy_line_ratio=ref.legacy_line_ratio,
                    status=ref.status,
                    reason=ref.reason,
                    family=ref.family,
                    rank=_rank(parent_fingerprint, band, ref.content_sha256)[:16],
                    role=role,
                    comparison_keys=ref.comparison_keys,
                )
            )
    # Sorted by identity, not by draw order, so the artifact is byte-stable.
    return sorted(chosen, key=lambda e: (e.band, e.content_sha256))


def cohort_fingerprint(
    entries: Sequence[CohortEntry], *, parent_fingerprint: str
) -> str:
    """Identity of a frozen cohort, over its content and its parent."""
    h = hashlib.sha256()
    h.update(parent_fingerprint.encode())
    h.update(b"\x00" + COHORT_ALGORITHM.encode() + b"\x00")
    for entry in sorted(entries, key=lambda e: (e.role, e.band, e.content_sha256)):
        h.update(f"{entry.role}\x1f{entry.band}\x1f{entry.content_sha256}\n".encode())
    return h.hexdigest()

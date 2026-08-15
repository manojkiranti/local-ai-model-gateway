"""The benchmark manifest: the Phase 6A cohort, drawn once and written down.

WHY A FILE, AND NOT A FLAG
    The first draft of this plan fetched the sample with broad
    `--section`/`--year`/`--limit` passes and then re-sampled whatever landed on
    disk. That is wrong twice over. Phase 5 selects `pending` rows in **id order**
    within a scope, and catalog id order is the order REST paged the post types —
    so "circulars from 2019, limit 60" returns the 60 with the lowest ids, and
    stratifying over that measures the id order rather than the corpus. It is also
    not reproducible: any later fetch changes what is on disk and therefore what
    would be re-sampled, so two runs of the same profile would describe two
    different cohorts.

    So the sample is drawn ONCE from the full catalog, saved with each file's
    exact `comparison_key` and its strata, and every later step — fetch, extract,
    calibrate — names that file. The manifest is committed, which is what makes
    the published profile something a reader can re-run rather than take on trust.

WHAT A MANIFEST IS NOT
    **It is not a list of URLs to fetch.** Every key is matched against
    `nrb_files.comparison_key`; what actually gets requested is the `source_url`
    the catalog holds for the matched row, re-checked by `http.check_url` at fetch
    time exactly as in any other pass. A key naming a host NRB never published
    simply matches no row and is reported missing — there is nothing here for it
    to bypass. That is why the identity is `comparison_key` and not a URL field:
    the manifest *selects from* the catalog, it cannot *add to* it.

WHAT IS RECORDED, AND WHY EACH PART
    * `entries` — the exact keys, each with `year`, `document_type`,
      `resource_type`, `owner` and its `stratum`. The strata are stored rather
      than recomputed because the catalog moves: a source re-typed by a later sync
      must not silently re-label a cohort that has already been profiled.
    * `sampler` — size, floor, cohort cap, sampler version. Reproducing the draw
      needs the parameters, not just the result.
    * `catalog_counts` + `drawn_at` — what the corpus looked like when the sample
      was taken, so a reader can tell whether the corpus has moved since.
    * `shortfall` + `notes` — carried verbatim from the sampler. A cohort that
      could not be filled is a caveat on every number downstream, and it belongs
      with the cohort rather than in someone's memory.

    `comparison_key` is the identity, not `content_sha256`: the sample is drawn
    BEFORE anything is fetched, when the hash does not exist yet.

THIS MODULE IS THE FORMAT ONLY
    Reading, writing and validating a manifest. `build_manifest` — turning a drawn
    `sampling.Sample` into one — arrives with the sampler it depends on. The
    fetch path needs to *read* a manifest before anything can draw one, so the two
    halves land separately and this half imports nothing from `sampling`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MANIFEST_MAX_KEYS",
    "MANIFEST_VERSION",
    "Manifest",
    "read_manifest",
    "write_manifest",
]

# Bumped if the file's shape changes. `read_manifest` refuses anything else
# rather than half-understanding it — a manifest is a benchmark definition, and
# quietly misreading one would silently redefine the benchmark.
MANIFEST_VERSION = "manifest-1"

# A manifest is a benchmark cohort, not a back door around the scope-is-required
# rule. 5,000 keys is ~12x the planned 400-file sample and far under the 18,263
# file corpus. `catalog.MANIFEST_MAX_KEYS` is the same bound applied at the query;
# `test_the_cap_matches_the_one_the_catalog_enforces` holds the two together, so a
# manifest this module accepts cannot be refused later by the query that uses it.
MANIFEST_MAX_KEYS = 5000


@dataclass(frozen=True)
class Manifest:
    version: str
    drawn_at: str
    requested: int
    shortfall: int
    sampler: dict[str, Any]
    catalog_counts: dict[str, Any]
    strata: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]
    entries: tuple[dict[str, Any], ...]

    def keys(self) -> tuple[str, ...]:
        """The exact `comparison_key` values this cohort consists of.

        **Deduplicated, order-stable.** A hand-edited manifest can name the same
        file twice; it is still one file, one download and one extraction, so the
        duplicate is collapsed here rather than left for each consumer to
        rediscover. `duplicate_entries` keeps the discrepancy visible — a manifest
        of 400 entries that resolves to 398 files should say so, not quietly
        report a cohort two files smaller than the one that was drawn.
        """
        return tuple(dict.fromkeys(entry["comparison_key"] for entry in self.entries))

    @property
    def duplicate_entries(self) -> int:
        """How many entries name a key an earlier entry already named."""
        return len(self.entries) - len(self.keys())


def write_manifest(manifest: Manifest, path: str | Path) -> None:
    """Write the manifest as indented, non-escaped JSON.

    `ensure_ascii=False` so NRB's Devanagari filenames stay readable in the
    committed file rather than becoming a wall of `\\uXXXX`. `sort_keys=True` and
    a fixed indent so re-writing an unchanged manifest is byte-identical and a
    real change diffs cleanly.
    """
    payload = {
        "version": manifest.version,
        "drawn_at": manifest.drawn_at,
        "requested": manifest.requested,
        "shortfall": manifest.shortfall,
        "sampler": manifest.sampler,
        "catalog_counts": manifest.catalog_counts,
        "strata": list(manifest.strata),
        "notes": list(manifest.notes),
        "entries": list(manifest.entries),
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_manifest(path: str | Path) -> Manifest:
    """Load a manifest, refusing anything this code cannot read exactly.

    Three refusals, all for the same reason: a manifest defines a benchmark, so
    partly understanding one silently redefines it. An unknown `version` is
    refused rather than best-effort parsed; an entry with no `comparison_key`
    would drop a file out of the cohort with no trace; and a file over
    `MANIFEST_MAX_KEYS` is refused here rather than at the query, so the bound is
    reported before anything is loaded.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != MANIFEST_VERSION:
        raise ValueError(
            f"manifest version {version!r} is not {MANIFEST_VERSION!r} — refusing "
            f"to half-read a benchmark definition"
        )
    entries = tuple(payload.get("entries") or ())
    if len(entries) > MANIFEST_MAX_KEYS:
        raise ValueError(
            f"manifest names {len(entries)} entries; the cap is {MANIFEST_MAX_KEYS}. "
            f"A manifest is a benchmark cohort, not a way to fetch the corpus."
        )
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("comparison_key"):
            raise ValueError(
                f"manifest entry {position} has no comparison_key — that is the "
                f"catalog identity, and an entry without one names no file"
            )
    return Manifest(
        version=version,
        drawn_at=payload.get("drawn_at", ""),
        requested=int(payload.get("requested", 0)),
        shortfall=int(payload.get("shortfall", 0)),
        sampler=payload.get("sampler") or {},
        catalog_counts=payload.get("catalog_counts") or {},
        strata=tuple(payload.get("strata") or ()),
        notes=tuple(payload.get("notes") or ()),
        entries=entries,
    )

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

    * `selection_sha256` — the cryptographic identity of the cohort itself
      (§ below). Two people holding a manifest each can compare one 64-character
      string and know whether they are profiling the same 400 files.

THE FINGERPRINT BINDS THE COHORT, NOT THE FILE
    `selection_sha256` covers the manifest schema version, the sampling algorithm
    version, the seed, every sampler parameter and the ordered list of selected
    keys — and nothing else. Deliberately excluded: `drawn_at`, the output path
    and every database id. A fingerprint that changed when the file was rewritten,
    or when the same cohort was drawn on another machine, would prove nothing
    about the cohort; this one changes if and only if the benchmark changes.

THIS MODULE IS THE FORMAT
    Reading, writing, validating and fingerprinting a manifest, plus
    `build_manifest`, which freezes a drawn `sampling.Sample` into one. The
    dependency runs one way: `sampling` knows nothing about files or JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .sampling import Sample, rank_for

__all__ = [
    "MANIFEST_MAX_KEYS",
    "MANIFEST_VERSION",
    "Manifest",
    "Verification",
    "build_manifest",
    "compute_selection_sha256",
    "read_manifest",
    "select_manifest_subset",
    "verify_manifest",
    "write_manifest",
    "write_new_manifest",
]

# Bumped if the file's shape changes. `read_manifest` refuses anything else
# rather than half-understanding it — a manifest is a benchmark definition, and
# quietly misreading one would silently redefine the benchmark. `manifest-2` adds
# the sampler's algorithm version, seed, allocation diagnostics and the selection
# fingerprint; no `manifest-1` file was ever written.
MANIFEST_VERSION = "manifest-2"

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
    # Added in `manifest-2`. Defaulted so a hand-authored fixture that only cares
    # about the key list stays constructible; a manifest DRAWN by the sampler
    # always carries all five.
    algorithm_version: str = ""
    seed: str = ""
    selected: int = 0
    selection_sha256: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

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
        "algorithm_version": manifest.algorithm_version,
        "seed": manifest.seed,
        "requested": manifest.requested,
        "selected": manifest.selected,
        "shortfall": manifest.shortfall,
        "selection_sha256": manifest.selection_sha256,
        "sampler": manifest.sampler,
        "diagnostics": manifest.diagnostics,
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
        algorithm_version=payload.get("algorithm_version", ""),
        seed=payload.get("seed", ""),
        selected=int(payload.get("selected", 0)),
        selection_sha256=payload.get("selection_sha256", ""),
        diagnostics=payload.get("diagnostics") or {},
    )


# --------------------------------------------------------------------------- #
# The selection fingerprint
# --------------------------------------------------------------------------- #
def compute_selection_sha256(
    *,
    manifest_version: str,
    algorithm_version: str,
    seed: str,
    parameters: dict[str, Any],
    keys: Sequence[str],
) -> str:
    """The cryptographic identity of one drawn cohort.

    Canonical serialization: JSON with sorted keys, no insignificant whitespace
    and `ensure_ascii=False`, so a Devanagari key contributes its own UTF-8 bytes
    rather than an escape sequence that a different writer might spell
    differently. The key order is the manifest's canonical order (the sample
    rank), not the order any database returned.

    What is bound: the schema version, the algorithm version, the seed, every
    sampler parameter and the ordered keys. What is not: the timestamp, the path
    and the row ids. Change one selected file and this changes; rewrite the same
    cohort tomorrow on another machine and it does not.
    """
    canonical = json.dumps(
        {
            "manifest_version": manifest_version,
            "algorithm_version": algorithm_version,
            "seed": seed,
            "parameters": parameters,
            "keys": list(keys),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Verification:
    """The result of re-deriving a manifest's fingerprint from its own contents."""

    ok: bool
    reason: str            # ok | no_fingerprint_recorded | fingerprint_mismatch
    recorded: str
    recomputed: str


def verify_manifest(manifest: Manifest) -> Verification:
    """Recompute the fingerprint from the manifest's own entries and parameters.

    This does NOT resample and does not touch the catalog: it answers "is this
    file internally consistent — do these keys, under these parameters, still hash
    to what it claims", which is what catches an edited cohort. It cannot answer
    "would the sampler still draw this", because the catalog moves and that is a
    different question with a different answer.
    """
    recomputed = compute_selection_sha256(
        manifest_version=manifest.version,
        algorithm_version=manifest.algorithm_version,
        seed=manifest.seed,
        parameters=manifest.sampler,
        keys=manifest.keys(),
    )
    if not manifest.selection_sha256:
        return Verification(False, "no_fingerprint_recorded", "", recomputed)
    if manifest.selection_sha256 != recomputed:
        return Verification(
            False, "fingerprint_mismatch", manifest.selection_sha256, recomputed
        )
    return Verification(True, "ok", manifest.selection_sha256, recomputed)


# --------------------------------------------------------------------------- #
# Drawing and freezing
# --------------------------------------------------------------------------- #
def build_manifest(
    sample: Sample,
    *,
    drawn_at: str,
    catalog_counts: dict[str, Any] | None = None,
) -> Manifest:
    """Freeze a drawn `Sample` into a durable cohort definition.

    Everything comes from the sample — the keys, their strata, the parameters and
    the allocation diagnostics — so there is no second path by which a key could
    enter a manifest. `drawn_at` and `catalog_counts` are the only outside facts,
    and neither is part of the fingerprint.
    """
    if len(sample.keys) > MANIFEST_MAX_KEYS:
        raise ValueError(
            f"manifest would name {len(sample.keys)} keys; the cap is "
            f"{MANIFEST_MAX_KEYS}. A manifest is a benchmark cohort, not a way "
            f"to fetch the whole corpus."
        )
    sampler = dict(sample.parameters)
    sampler["algorithm_version"] = sample.algorithm_version
    sampler["seed"] = sample.seed
    return Manifest(
        version=MANIFEST_VERSION,
        drawn_at=drawn_at,
        algorithm_version=sample.algorithm_version,
        seed=sample.seed,
        requested=sample.requested,
        selected=len(sample.keys),
        shortfall=sample.shortfall,
        selection_sha256=compute_selection_sha256(
            manifest_version=MANIFEST_VERSION,
            algorithm_version=sample.algorithm_version,
            seed=sample.seed,
            parameters=sampler,
            keys=sample.keys,
        ),
        sampler=sampler,
        diagnostics=sample.diagnostics.as_dict(),
        catalog_counts=dict(catalog_counts or {}),
        strata=tuple(stratum.as_dict() for stratum in sample.strata),
        notes=tuple(sample.notes),
        entries=tuple(entry.as_entry() for entry in sample.entries),
    )


def write_new_manifest(
    manifest: Manifest, path: str | Path, *, overwrite: bool = False
) -> str | None:
    """Write a manifest, refusing to replace one that is already there.

    The whole point of a manifest is that the cohort is frozen: silently
    overwriting it would make a new profile incomparable with every number
    already published from the old one, with nothing in the diff to say so.
    Returns the fingerprint of the manifest that was replaced (or None), so the
    caller can print the before and after rather than just the after.
    """
    target = Path(path)
    previous: str | None = None
    if target.exists():
        if not overwrite:
            raise FileExistsError(
                f"{target} already exists: a benchmark cohort is drawn ONCE, and "
                f"re-drawing it makes the new profile incomparable with every "
                f"number published from the old one. Pass the overwrite flag if "
                f"that is really what you want."
            )
        try:
            previous = read_manifest(target).selection_sha256 or "(none recorded)"
        except (OSError, ValueError, json.JSONDecodeError):
            previous = "(unreadable)"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(manifest, target)
    return previous


def select_manifest_subset(
    manifest: Manifest,
    *,
    size: int,
    seed: str,
    purpose: str = "docling-calibration",
) -> tuple[str, ...]:
    """A deterministic sub-cohort drawn from a manifest and nothing else.

    Phase 6A's Docling calibration runs over ~40 files, and they have to come from
    the SAME cohort the pypdf screen ran over — comparing two engines on two
    different file sets measures the file sets. So the candidate pool here is the
    manifest's own entries: this function never queries the catalog, so there is
    no path by which a key outside the benchmark can enter the subset.

    Ranked by `sha256(purpose | seed | key)`. The purpose is part of the
    pre-image so two different sub-cohorts drawn with the same seed are not the
    same prefix of one ordering.

    It imports nothing from Docling and runs no calibration; it only says which
    files a calibration would use.
    """
    keys = manifest.keys()
    ordered = sorted(keys, key=lambda key: (rank_for(purpose, seed, key), key))
    return tuple(ordered[: max(size, 0)])

"""The Docling calibration subset: which benchmark PDFs the two parsers compare on.

Phase 6A screens the corpus with `pypdf` because it reads the same embedded text
layer as Docling at ~41 pages/s against Docling's ~1-2 on CPU. That is a claim,
and this is the instrument that makes it a measurement: a bounded pypdf-vs-Docling
comparison over a fixed set of PDFs, drawn from the frozen benchmark itself.

WHY THE SUBSET IS FROZEN TOO
    The same argument as the parent cohort (`manifest.py`), one level down. If the
    comparison ran over "whatever benchmark PDFs happen to be on disk", the number
    it produced would describe the download order, and re-running it after another
    fetch would produce a different number with no way to tell which of the two
    was right. So the 40 files are chosen ONCE, from the parent manifest's own
    entries, written down, and named by every later run.

WHY THE CANDIDATES ARE THE PARENT'S ENTRIES AND NOT THE CATALOG
    A calibration that reached into `nrb_files` could compare the engines on
    documents the screen never saw, and then the agreement rate would not be about
    the benchmark at all. `build_subset` takes a `Manifest` and nothing else — no
    session, no engine — so there is no path by which a key outside the frozen
    cohort can enter the comparison.

WHY PDFs ONLY
    The question is *pypdf* versus *Docling native PDF extraction*. pypdf does not
    read `.docx` or `.xlsx` — `extraction.py` routes those to python-docx and
    openpyxl — so including them would compare two different pairs of parsers and
    average the results. The parent benchmark keeps every format; this subset is
    the PDF parser-calibration slice of it, and says so in the artifact.

WHAT MAY NOT MOVE THE SELECTION
    Not fetch state, not what is on disk, not a pypdf verdict, not
    `legacy_line_ratio`, not `char_count`, not database order. Selecting the files
    pypdf already found suspicious would guarantee a rescue rate and measure
    nothing. The rank is a sha256 over the parent fingerprint, the algorithm
    version and the `comparison_key` — three values that are all frozen before a
    single byte is downloaded.

    Binding the parent fingerprint into the rank (rather than a free-text seed)
    means a different benchmark cannot accidentally draw the same 40, and that the
    subset is reproducible from the committed manifest alone.

WHAT IS DELIBERATELY NOT HERE
    No Docling import, no parser, no database, no HTTP. This module says which
    files a calibration would use; `calibrate.py` runs it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .manifest import Manifest, Verification, verify_manifest
from .sampling import rank_for

__all__ = [
    "CALIBRATION_RESOURCE_TYPE",
    "CalibrationSubset",
    "DEFAULT_SUBSET_SIZE",
    "PURPOSE",
    "SUBSET_ALGORITHM_VERSION",
    "SUBSET_VERSION",
    "build_subset",
    "compute_subset_sha256",
    "read_subset",
    "select_calibration_entries",
    "verify_subset",
    "write_new_subset",
    "write_subset",
]

# The file's shape. Refused if unknown, for the same reason `manifest.py` refuses
# one: half-reading a frozen definition silently redefines it.
SUBSET_VERSION = "calibration-1"

# The selection rule's identity, and the namespace its ranks are drawn in.
# Bumping it re-draws the subset — which is a decision, not a side effect, and is
# why it is written into the artifact and into the fingerprint.
SUBSET_ALGORITHM_VERSION = "docling-calibration-v1"

PURPOSE = "docling-calibration"

# pypdf's format. See the module docstring.
CALIBRATION_RESOURCE_TYPE = "pdf"

# Docling is minutes per dozen files on CPU. 40 is a bounded engineering
# calibration — enough to see whether the two engines disagree systematically,
# and deliberately NOT a statistically powered estimate of the corpus. A rescue
# rate measured here describes these 40 documents.
DEFAULT_SUBSET_SIZE = 40


@dataclass(frozen=True)
class CalibrationSubset:
    """The frozen list of PDFs a pypdf-vs-Docling comparison runs over."""

    version: str
    purpose: str
    subset_algorithm_version: str
    parent_manifest_path: str
    parent_selection_sha256: str
    resource_type: str
    requested_size: int
    selected_size: int
    subset_selection_sha256: str
    generated_at: str
    entries: tuple[dict[str, Any], ...]

    def keys(self) -> tuple[str, ...]:
        """The exact `comparison_key` values, deduplicated, in subset rank order."""
        return tuple(
            dict.fromkeys(entry["comparison_key"] for entry in self.entries)
        )

    def entries_for(self, key: str) -> tuple[dict[str, Any], ...]:
        return tuple(e for e in self.entries if e["comparison_key"] == key)


def _cohort_of(entry: dict[str, Any]) -> str:
    """The year cohort, from the parent's own recorded stratum.

    `sampling.Candidate.stratum` is `cohort/document_type/resource_type`, and the
    cohort is read back out of it rather than recomputed from `year`: the parent
    manifest froze the strata deliberately (a source re-typed by a later sync must
    not re-label a cohort that has already been profiled), and re-deriving here
    would reintroduce exactly that drift one level down.
    """
    stratum = str(entry.get("sampling_stratum") or entry.get("stratum") or "")
    if stratum:
        return stratum.split("/")[0]
    return str(entry.get("cohort") or "unknown")


def select_calibration_entries(
    manifest: Manifest,
    *,
    size: int = DEFAULT_SUBSET_SIZE,
    algorithm_version: str = SUBSET_ALGORITHM_VERSION,
    resource_type: str = CALIBRATION_RESOURCE_TYPE,
) -> tuple[dict[str, Any], ...]:
    """The deterministic pick, as subset entries. Pure: manifest in, entries out.

    Ranked by `sha256(algorithm_version | parent_selection_sha256 |
    comparison_key)`, with the key itself as the final tiebreak so the ordering is
    total. Never Python's `hash()`, which is salted per process and would draw a
    different 40 on every run.

    The parent's position of each entry is carried through as `parent_rank`, so a
    reader can see where in the benchmark these files sit, and so a limited
    calibration run can order by the benchmark's own canonical order.
    """
    parent_rank: dict[str, int] = {}
    candidates: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(manifest.entries):
        key = entry["comparison_key"]
        parent_rank.setdefault(key, position)
        if entry.get("resource_type") != resource_type or key in candidates:
            continue
        candidates[key] = entry

    ordered = sorted(
        candidates.values(),
        key=lambda entry: (
            rank_for(
                algorithm_version,
                manifest.selection_sha256,
                entry["comparison_key"],
            ),
            entry["comparison_key"],
        ),
    )

    return tuple(
        {
            "comparison_key": entry["comparison_key"],
            "subset_rank": rank,
            "parent_rank": parent_rank[entry["comparison_key"]],
            "year": entry.get("year"),
            "cohort": _cohort_of(entry),
            "document_type": entry.get("document_type"),
            "resource_type": entry.get("resource_type"),
            "owner": entry.get("owner"),
            "owners": list(entry.get("owners") or ()),
            "sampling_stratum": entry.get("sampling_stratum"),
        }
        for rank, entry in enumerate(ordered[: max(size, 0)])
    )


def compute_subset_sha256(
    *,
    schema_version: str,
    algorithm_version: str,
    purpose: str,
    parent_selection_sha256: str,
    requested_size: int,
    resource_type: str,
    keys: Sequence[str],
) -> str:
    """The cryptographic identity of one calibration subset.

    Binds the schema, the selection rule, the purpose, the PARENT cohort, the
    requested size, the candidate restriction and the ordered keys. Excluded, on
    purpose: `generated_at`, the output path, the selected count (it is a function
    of the keys) and every database id — a fingerprint that changed when the file
    was rewritten would prove nothing about the subset.

    Same canonical serialization as `manifest.compute_selection_sha256`: sorted
    keys, no insignificant whitespace, `ensure_ascii=False` so a Devanagari key
    contributes its own UTF-8 bytes rather than an escape another writer might
    spell differently.
    """
    canonical = json.dumps(
        {
            "schema_version": schema_version,
            "algorithm_version": algorithm_version,
            "purpose": purpose,
            "parent_selection_sha256": parent_selection_sha256,
            "requested_size": requested_size,
            "resource_type": resource_type,
            "keys": list(keys),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_subset(
    manifest: Manifest,
    *,
    parent_manifest_path: str,
    size: int = DEFAULT_SUBSET_SIZE,
    generated_at: str,
    expect_parent_sha256: str | None = None,
    algorithm_version: str = SUBSET_ALGORITHM_VERSION,
    resource_type: str = CALIBRATION_RESOURCE_TYPE,
) -> CalibrationSubset:
    """Draw and freeze the calibration subset. Takes a manifest and nothing else.

    The parent is verified FIRST, twice over: its own fingerprint must recompute
    from its own contents, and — when the caller states which benchmark it expects
    — it must be that one. Both raise rather than regenerate: a subset silently
    re-drawn against an edited parent is a calibration whose files no longer
    belong to the cohort it claims to describe.
    """
    verification = verify_manifest(manifest)
    if not verification.ok:
        raise ValueError(
            f"the parent manifest does not verify ({verification.reason}: recorded "
            f"{verification.recorded or '(none)'}, recomputed "
            f"{verification.recomputed}) — refusing to draw a calibration subset "
            f"from an edited benchmark"
        )
    if expect_parent_sha256 and manifest.selection_sha256 != expect_parent_sha256:
        raise ValueError(
            f"the parent manifest is {manifest.selection_sha256}, not the expected "
            f"{expect_parent_sha256}. The calibration subset is bound to ONE "
            f"benchmark cohort; drawing it from another silently changes what the "
            f"comparison describes."
        )

    entries = select_calibration_entries(
        manifest,
        size=size,
        algorithm_version=algorithm_version,
        resource_type=resource_type,
    )
    keys = tuple(entry["comparison_key"] for entry in entries)
    return CalibrationSubset(
        version=SUBSET_VERSION,
        purpose=PURPOSE,
        subset_algorithm_version=algorithm_version,
        parent_manifest_path=parent_manifest_path,
        parent_selection_sha256=manifest.selection_sha256,
        resource_type=resource_type,
        requested_size=size,
        selected_size=len(keys),
        subset_selection_sha256=compute_subset_sha256(
            schema_version=SUBSET_VERSION,
            algorithm_version=algorithm_version,
            purpose=PURPOSE,
            parent_selection_sha256=manifest.selection_sha256,
            requested_size=size,
            resource_type=resource_type,
            keys=keys,
        ),
        generated_at=generated_at,
        entries=entries,
    )


def verify_subset(subset: CalibrationSubset) -> Verification:
    """Recompute the fingerprint from the subset's own contents.

    Answers "is this file internally consistent", which is what catches an edited
    subset. It cannot answer "is this still the right 40" — that needs the parent,
    and `verify_against_parent` is the function for it.
    """
    recomputed = compute_subset_sha256(
        schema_version=subset.version,
        algorithm_version=subset.subset_algorithm_version,
        purpose=subset.purpose,
        parent_selection_sha256=subset.parent_selection_sha256,
        requested_size=subset.requested_size,
        resource_type=subset.resource_type,
        keys=subset.keys(),
    )
    if not subset.subset_selection_sha256:
        return Verification(False, "no_fingerprint_recorded", "", recomputed)
    if subset.subset_selection_sha256 != recomputed:
        return Verification(
            False, "fingerprint_mismatch", subset.subset_selection_sha256, recomputed
        )
    return Verification(True, "ok", subset.subset_selection_sha256, recomputed)


def verify_against_parent(
    subset: CalibrationSubset, manifest: Manifest
) -> Verification:
    """Is this subset the one that belongs to THIS benchmark cohort?

    Reported as a `Verification` like the other two checks so a CLI can print all
    three the same way.
    """
    if subset.parent_selection_sha256 != manifest.selection_sha256:
        return Verification(
            False,
            "parent_mismatch",
            subset.parent_selection_sha256,
            manifest.selection_sha256,
        )
    outside = set(subset.keys()) - set(manifest.keys())
    if outside:
        return Verification(
            False, "keys_outside_parent", str(len(outside)), "0"
        )
    return Verification(True, "ok", subset.parent_selection_sha256,
                        manifest.selection_sha256)


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def write_subset(subset: CalibrationSubset, path: str | Path) -> None:
    """Indented, sorted, non-escaped JSON — same rules as the parent manifest, so
    rewriting an unchanged subset is byte-identical and a real change diffs."""
    payload = {
        "version": subset.version,
        "purpose": subset.purpose,
        "subset_algorithm_version": subset.subset_algorithm_version,
        "parent_manifest_path": subset.parent_manifest_path,
        "parent_selection_sha256": subset.parent_selection_sha256,
        "resource_type": subset.resource_type,
        "requested_size": subset.requested_size,
        "selected_size": subset.selected_size,
        "subset_selection_sha256": subset.subset_selection_sha256,
        "generated_at": subset.generated_at,
        "entries": list(subset.entries),
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_subset(path: str | Path) -> CalibrationSubset:
    """Load a subset, refusing anything this code cannot read exactly."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != SUBSET_VERSION:
        raise ValueError(
            f"calibration subset version {version!r} is not {SUBSET_VERSION!r} — "
            f"refusing to half-read a frozen definition"
        )
    entries = tuple(payload.get("entries") or ())
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("comparison_key"):
            raise ValueError(
                f"calibration entry {position} has no comparison_key — that is the "
                f"catalog identity, and an entry without one names no file"
            )
    return CalibrationSubset(
        version=version,
        purpose=payload.get("purpose", ""),
        subset_algorithm_version=payload.get("subset_algorithm_version", ""),
        parent_manifest_path=payload.get("parent_manifest_path", ""),
        parent_selection_sha256=payload.get("parent_selection_sha256", ""),
        resource_type=payload.get("resource_type", ""),
        requested_size=int(payload.get("requested_size", 0)),
        selected_size=int(payload.get("selected_size", 0)),
        subset_selection_sha256=payload.get("subset_selection_sha256", ""),
        generated_at=payload.get("generated_at", ""),
        entries=entries,
    )


def write_new_subset(
    subset: CalibrationSubset, path: str | Path, *, overwrite: bool = False
) -> str | None:
    """Write a subset, refusing to replace one that is already there.

    Returns the fingerprint of whatever was replaced (or None), so a caller told
    to overwrite anyway can print the before as well as the after.
    """
    target = Path(path)
    previous: str | None = None
    if target.exists():
        if not overwrite:
            raise FileExistsError(
                f"{target} already exists: a calibration subset is drawn ONCE, and "
                f"re-drawing it makes the new comparison incomparable with every "
                f"number published from the old one. Pass the overwrite flag if "
                f"that is really what you want."
            )
        try:
            previous = read_subset(target).subset_selection_sha256 or "(none recorded)"
        except (OSError, ValueError, json.JSONDecodeError):
            previous = "(unreadable)"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_subset(subset, target)
    return previous

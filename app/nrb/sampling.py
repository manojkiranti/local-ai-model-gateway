"""Choosing the Phase 6A benchmark cohort. Pure — no DB, no network, no files.

`rows[:400]` is the wrong answer twice over. The catalog's id order follows the
order REST paged the post types in, so the first 400 rows are one post type from
one department — a profile of that, dressed as a profile of the corpus. And any
order that depends on the database's row order is not reproducible: run it again
after a sync and it describes a different cohort.

So: stratify, then order deterministically WITHIN each stratum by a hash of the
row's own identity. The hash is uncorrelated with publication date, department
and insertion order, and it is stable across machines, processes and runs — which
Python's `hash()` is not (it is salted per process, so a sample ordered by it
would be a different sample every time).

THE SAMPLING UNIT IS `comparison_key`, AND IT IS DRAWN BEFORE ANYTHING IS FETCHED
    Not a URL a caller supplied, not `content_sha256` (which does not exist yet
    when the sample is drawn), not a local blob, not a source. One catalog file =
    one candidate = at most one entry in the manifest. A file NRB publishes from
    two pages is still one file, one download and one extraction, so it gets one
    chance of being drawn, not two.

ALLOCATION FOLLOWS REPRESENTATION, NOT PARITY
    Proportional to stratum size, with a floor so small strata appear at all, and
    a per-cohort cap so 2019 cannot swallow the sample. 2019 is 9,178 of 18,266
    catalog rows (NRB's CMS migration, spec §2 and the Phase 3 measurements):
    purely proportional allocation would spend half the budget there, and
    equal-sized strata would over-represent a 3-file stratum a hundredfold.
    Neither is honest.

    A stratum that cannot fill its floor is reported `weak`, never padded. The
    report names every stratum with n < `WEAK_THRESHOLD` so no conclusion is
    drawn from one silently.

THE CAP MUST NOT SHRINK THE SAMPLE
    The obvious implementation — allocate 400, trim 2019 down to its cap, return
    what is left — returns 350 and reads downstream as "we profiled 400 files".
    Every slot the cap removes is handed back to strata that still have headroom,
    repeatedly, until the budget is filled or nothing legal remains. If 400 are
    requested and 400 can legally be selected, 400 come back. If they cannot, the
    shortfall and the constraint that bound it are reported rather than hidden.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from .classify import SECTIONS

__all__ = [
    "ALGORITHM_VERSION",
    "COHORTS",
    "CAPPED_COHORT",
    "Candidate",
    "DEFAULT_FLOOR",
    "DEFAULT_MAX_COHORT_SHARE",
    "DEFAULT_SEED",
    "Diagnostics",
    "Sample",
    "Stratum",
    "UNKNOWN_OWNER",
    "UNKNOWN_RESOURCE",
    "UNTYPED",
    "WEAK_THRESHOLD",
    "build_candidates",
    "rank_for",
    "stratified_sample",
    "year_cohort",
]

# The stratum definition AND the allocation semantics, versioned together. Bumped
# by hand whenever either changes, because a cohort drawn under different rules is
# a different benchmark even at the same size and seed — and the version is bound
# into the manifest fingerprint so the two can never be silently compared.
ALGORITHM_VERSION = "nrb-stratified-v1"

# Cohorts chosen from the MEASURED distribution (files joined to their source's
# publication year, scratch DB, 2026-08-15): <=2018 886, 2019 9,178, 2020-2022
# 3,095, 2023-2026 5,109. 2019 stands alone because Phase 3 measured its document
# typing at 47.5% against 89-100% everywhere else, and the open question Phase 6A
# has to answer is whether its EXTRACTION quality is as different as its metadata
# quality. Merged into a neighbour it would be unanswerable.
COHORTS = ("<=2018", "2019", "2020-2022", "2023-2026", "unknown")

# The cohort whose size makes it dangerous. Named rather than hardcoded at each
# use so "which cohort is the CMS migration" is one fact in one place.
CAPPED_COHORT = "2019"

# Below this, a stratum's numbers are reported but no conclusion is drawn.
WEAK_THRESHOLD = 10

# Buckets for absent metadata. Explicit values, never a dropped row: a file with
# no document type is a fact about the corpus (5,052 of the 2019 cohort sit in
# NRB's catch-all `upload-files` category) and excluding it would quietly make
# the benchmark a benchmark of well-catalogued files only.
UNTYPED = "untyped"
UNKNOWN_RESOURCE = "unknown"
UNKNOWN_OWNER = "unknown"

# ---------------------------------------------------------------------------
# Policy defaults. PROVISIONAL — the canonical Phase 6A values are not approved
# yet (see `docs/nrb-integration.md`). They live here as named constants rather
# than as literals inside the allocator precisely so the approved values can be
# set in one place, and so the CLI can print what it actually used.
# ---------------------------------------------------------------------------
DEFAULT_SEED = "phase6a-v1"
DEFAULT_FLOOR = 5
DEFAULT_MAX_COHORT_SHARE = Fraction(3, 10)

# Byte separator inside the ranking pre-image. A unit separator cannot occur in a
# URL, an algorithm version or a seed, so no two different triples can produce the
# same pre-image by concatenation.
_SEP = "\x1f"

# `classify.SECTIONS` is ordered regulatory-first, and `documents.Taxonomy`
# already resolves a post filed under several categories with it. Reused here so
# a file's stratum comes from the catalog's own priority order rather than from
# whichever joined row the database returned first.
_SECTION_RANK = {name: index for index, name in enumerate(SECTIONS)}


def year_cohort(year: int | None) -> str:
    if year is None:
        return "unknown"
    if year <= 2018:
        return "<=2018"
    if year == 2019:
        return "2019"
    if year <= 2022:
        return "2020-2022"
    return "2023-2026"


def rank_for(namespace: str, seed: str, key: str) -> str:
    """The deterministic rank of one key within one draw.

    sha256 over `namespace | seed | key` rather than `random.shuffle(seed=…)`:
    no dependence on Python's PRNG implementation, no dependence on the process
    hash seed, and the same key sorts to the same position whatever else is in
    the corpus — so growing the catalog does not reshuffle an existing sample.

    `namespace` is the algorithm version for a draw and the purpose string for a
    subset, so the same seed used for two different jobs does not produce two
    correlated orderings.
    """
    payload = f"{namespace}{_SEP}{seed}{_SEP}{key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# Candidates — one per comparison_key, canonicalized explicitly
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candidate:
    """One catalog file, with every metadata field resolved to a single value.

    A file can be referenced by several sources (Phase 3 measured 42 such files),
    and those sources can disagree about year, document type and owner. Sampling
    needs exactly one stratum per file, so the disagreement is resolved HERE, by
    stated rule, and never by which row the database returned first:

    * `document_type` — the earliest of the file's types in `classify.SECTIONS`
      order, which is the catalog's own regulatory-first priority (the same rule
      `documents.Taxonomy.section_for` uses). Types outside that vocabulary sort
      after the known ones, alphabetically.
    * `year` — the EARLIEST publication year of any referencing source. Earliest
      rather than latest because it is the year the document entered the corpus,
      and because a later republication must not move an already-profiled file
      into another cohort.
    * `owner` — every owner is kept in `owners` (sorted, deduplicated); `owner`
      is the first of them, and is reported rather than stratified on. 33 owner
      codes crossed with cohort x type x format shatters into single-digit cells.
    * `resource_type` — a file-level column, so normally single-valued; the
      lowest sorted value is taken if rows ever disagree.

    `source_rows` records how many catalog rows collapsed into this candidate, so
    "one key, several sources" is visible rather than inferred.
    """

    comparison_key: str
    year: int | None
    cohort: str
    document_type: str
    resource_type: str
    owner: str
    owners: tuple[str, ...]
    source_rows: int

    @property
    def stratum(self) -> tuple[str, str, str]:
        return (self.cohort, self.document_type, self.resource_type)

    @property
    def stratum_label(self) -> str:
        return "/".join(self.stratum)

    def as_entry(self) -> dict[str, Any]:
        """The manifest entry for this candidate. `comparison_key` is the
        authoritative identity; everything else is the metadata the report breaks
        the cohort down by, frozen at draw time so a later re-typing sync cannot
        silently re-label an already-profiled cohort."""
        return {
            "comparison_key": self.comparison_key,
            "year": self.year,
            "document_type": self.document_type,
            "resource_type": self.resource_type,
            "owner": self.owner,
            "owners": list(self.owners),
            "sampling_stratum": self.stratum_label,
        }


def _section_order(name: str) -> tuple[int, str]:
    return (_SECTION_RANK.get(name, len(SECTIONS)), name)


def build_candidates(rows: Iterable[Mapping[str, Any]]) -> tuple[Candidate, ...]:
    """Collapse catalog rows into one candidate per `comparison_key`.

    Order-independent by construction: every field is resolved with a rule over
    the SET of values (min, sorted-first, priority order), never by taking the
    first row seen, and the result is returned in sorted key order.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("comparison_key")
        if not key:
            continue
        bucket = grouped.setdefault(
            key, {"years": set(), "types": set(), "owners": set(),
                  "resources": set(), "rows": 0}
        )
        bucket["rows"] += 1
        year = row.get("year")
        if year is not None:
            bucket["years"].add(int(year))
        if row.get("document_type"):
            bucket["types"].add(str(row["document_type"]))
        if row.get("owner"):
            bucket["owners"].add(str(row["owner"]))
        if row.get("resource_type"):
            bucket["resources"].add(str(row["resource_type"]))

    candidates = []
    for key in sorted(grouped):
        bucket = grouped[key]
        year = min(bucket["years"]) if bucket["years"] else None
        types = sorted(bucket["types"], key=_section_order)
        owners = tuple(sorted(bucket["owners"]))
        resources = sorted(bucket["resources"])
        candidates.append(
            Candidate(
                comparison_key=key,
                year=year,
                cohort=year_cohort(year),
                document_type=types[0] if types else UNTYPED,
                resource_type=resources[0] if resources else UNKNOWN_RESOURCE,
                owner=owners[0] if owners else UNKNOWN_OWNER,
                owners=owners or (UNKNOWN_OWNER,),
                source_rows=bucket["rows"],
            )
        )
    return tuple(candidates)


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Stratum:
    cohort: str
    document_type: str
    resource_type: str
    available: int
    allocated: int
    selected: int
    weak: bool

    @property
    def label(self) -> str:
        return f"{self.cohort}/{self.document_type}/{self.resource_type}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort,
            "document_type": self.document_type,
            "resource_type": self.resource_type,
            "stratum": self.label,
            "available": self.available,
            "allocated": self.allocated,
            "selected": self.selected,
            "weak": self.weak,
        }


@dataclass(frozen=True)
class Diagnostics:
    """Why the sample is the size and shape it is.

    A list of keys cannot answer "why 387 and not 400", and that is exactly the
    question a short benchmark raises. Every constraint that removed a slot is
    counted here, so the answer is read off the manifest rather than reconstructed
    by re-running the sampler under a debugger.
    """

    requested: int
    selected: int
    candidates: int
    strata: int
    allocation_by_stratum: dict[str, int]
    allocation_by_cohort: dict[str, int]
    pre_cap_allocation_by_cohort: dict[str, int]
    candidates_by_cohort: dict[str, int]
    capped_cohort: str
    capped_cohort_candidates: int
    capped_cohort_selected: int
    cohort_caps: dict[str, int]
    capped_cohorts: tuple[str, ...]
    floor: int
    floor_requested_slots: int
    floor_allocated_slots: int
    floor_shortfall_slots: int
    floor_short_strata: tuple[str, ...]
    slots_removed_by_cap: int
    slots_redistributed: int
    redistribution_rounds: int
    unfillable_slots: int
    exhausted_strata: tuple[str, ...]
    complete: bool
    incomplete_reason: str | None

    @property
    def floor_short_strata_count(self) -> int:
        return len(self.floor_short_strata)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, for the manifest. Sorted keys everywhere so two runs over
        the same catalog write byte-identical JSON."""
        return {
            "requested": self.requested,
            "selected": self.selected,
            "candidates": self.candidates,
            "strata": self.strata,
            "allocation_by_stratum": dict(sorted(self.allocation_by_stratum.items())),
            "allocation_by_cohort": dict(sorted(self.allocation_by_cohort.items())),
            "pre_cap_allocation_by_cohort": dict(
                sorted(self.pre_cap_allocation_by_cohort.items())
            ),
            "candidates_by_cohort": dict(sorted(self.candidates_by_cohort.items())),
            "capped_cohort": self.capped_cohort,
            "capped_cohort_candidates": self.capped_cohort_candidates,
            "capped_cohort_selected": self.capped_cohort_selected,
            "cohort_caps": dict(sorted(self.cohort_caps.items())),
            "capped_cohorts": list(self.capped_cohorts),
            "floor": self.floor,
            "floor_requested_slots": self.floor_requested_slots,
            "floor_allocated_slots": self.floor_allocated_slots,
            "floor_shortfall_slots": self.floor_shortfall_slots,
            "floor_short_strata": list(self.floor_short_strata),
            "floor_short_strata_count": self.floor_short_strata_count,
            "slots_removed_by_cap": self.slots_removed_by_cap,
            "slots_redistributed": self.slots_redistributed,
            "redistribution_rounds": self.redistribution_rounds,
            "unfillable_slots": self.unfillable_slots,
            "exhausted_strata": list(self.exhausted_strata),
            "complete": self.complete,
            "incomplete_reason": self.incomplete_reason,
        }


@dataclass(frozen=True)
class Sample:
    keys: tuple[str, ...]
    entries: tuple[Candidate, ...]
    strata: tuple[Stratum, ...]
    requested: int
    algorithm_version: str
    seed: str
    parameters: dict[str, Any]
    diagnostics: Diagnostics
    # requested - len(keys). Non-zero means the constraints could not all be met.
    # A short sample that SAYS it is short is fine; one that reads as complete is
    # not, so this is printed rather than inferred from a length.
    shortfall: int = 0
    notes: tuple[str, ...] = ()

    def fingerprint_payload(self) -> dict[str, Any]:
        """Everything the selection fingerprint binds, minus the manifest schema
        version, which `manifest.py` adds because it owns the file format.

        Deliberately absent: the draw timestamp, the output path and every
        database id. A fingerprint that moved when the file was rewritten would
        prove nothing about the cohort.
        """
        return {
            "algorithm_version": self.algorithm_version,
            "seed": self.seed,
            "parameters": self.parameters,
            "keys": list(self.keys),
        }


# --------------------------------------------------------------------------- #
# The allocator
# --------------------------------------------------------------------------- #
def _as_fraction(value: Fraction | int | float) -> Fraction:
    """Exact rational from whatever the caller passed.

    `Fraction(str(0.30))` is 3/10; `Fraction(0.30)` is
    5404319552844595/18014398509481984, which would make `int(size * share)` a
    coin flip at the boundary. Integer arithmetic everywhere below for the same
    reason — an allocation that depends on float rounding is an allocation that
    can differ between machines.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(str(value))


def _cap_for(
    cohort: str, *, size: int, share: Fraction, explicit: Mapping[str, int]
) -> int:
    """The hard ceiling on one cohort's allocation.

    The share cap floors to at least 1 for a non-zero budget: a share of 0.30 on
    a request of 2 would otherwise compute a cap of 0 and silently exclude every
    cohort, which is not what "no cohort may exceed 30%" means.
    """
    share_cap = size if share >= 1 else max(1, int(size * share))
    named = explicit.get(cohort)
    return min(share_cap, named) if named is not None else share_cap


def stratified_sample(
    rows: Sequence[Mapping[str, Any]] | Sequence[Candidate],
    *,
    size: int,
    seed: str = DEFAULT_SEED,
    floor: int = DEFAULT_FLOOR,
    max_cohort_share: Fraction | float | int = DEFAULT_MAX_COHORT_SHARE,
    cohort_caps: Mapping[str, int] | None = None,
    algorithm_version: str = ALGORITHM_VERSION,
) -> Sample:
    """A reproducible, representative sample of `size` files.

    Four passes, in this order, because each constrains the next:

      1. **Floor, round-robin** — one slot at a time across every non-empty
         stratum, in a seeded hash order, so a 12-document type is measurable at
         all. Round-robin and not "walk the sorted list handing out `floor` each
         until the budget dies": that second form is what a `for … break` loop
         does, and when the budget cannot cover every stratum it gives everything
         to the lexicographically early ones and calls the rest empty. One slot
         at a time means an insufficient budget costs every stratum its depth,
         never its existence — and the hash order means even the last slot of a
         partial round is not handed out alphabetically.
      2. **Proportional** — the remaining budget split by stratum headroom with
         largest-remainder rounding, so a 700-file stratum is not represented as
         thinly as a 3-file one. Integer arithmetic throughout.
      3. **Cohort cap** — no cohort may exceed its ceiling. Passes 1 and 2 are
         cap-blind on purpose: the cap is applied here, in the open, so the
         number of slots it removes is a reported figure rather than an invisible
         side effect of the allocation order.
      4. **Redistribution** — every slot the cap removed goes back into the pool
         and is handed out again, same round-robin, to strata that still have
         headroom in a cohort that is not at its cap. This REPEATS: a stratum
         that fills drops out and the remaining slots keep flowing to the others,
         for as many rounds as it takes. A single pass would stop at the first
         exhausted recipient and quietly return a smaller cohort than was asked
         for.

    When the request is genuinely infeasible — fewer candidates than `size`, or
    every cohort at its cap — the result carries a `shortfall`, an
    `incomplete_reason` and a note naming the constraint that bound. It is never
    silently rounded down, and a cap is never breached to reach the number.

    Strata are `(year cohort, document type, resource type)`. Owner is carried
    through for the report but is not a stratification key — 33 codes crossed
    with the rest shatters into single-digit cells.
    """
    share = _as_fraction(max_cohort_share)
    explicit_caps = dict(cohort_caps or {})
    parameters = {
        "size": size,
        "floor": floor,
        "max_cohort_share": f"{share.numerator}/{share.denominator}",
        "cohort_caps": dict(sorted(explicit_caps.items())),
        "weak_threshold": WEAK_THRESHOLD,
    }

    if rows and isinstance(rows[0], Candidate):
        candidates: tuple[Candidate, ...] = tuple(rows)  # type: ignore[arg-type]
    else:
        candidates = build_candidates(rows)  # type: ignore[arg-type]

    size = max(size, 0)
    if not candidates or size == 0:
        return _empty_sample(
            size, seed, algorithm_version, parameters, len(candidates), floor,
            explicit_caps, share,
        )

    # Buckets, and the deterministic order of the members inside each. Sorting the
    # MEMBERS is what makes the result independent of the input order; sorting the
    # bucket keys makes every loop below stable.
    buckets: dict[tuple[str, str, str], list[Candidate]] = {}
    for candidate in candidates:
        buckets.setdefault(candidate.stratum, []).append(candidate)
    for members in buckets.values():
        members.sort(key=lambda c: (rank_for(algorithm_version, seed, c.comparison_key),
                                    c.comparison_key))

    stratum_keys = sorted(buckets)
    capacity = {key: len(buckets[key]) for key in stratum_keys}
    # Round-robin order: a seeded hash of the stratum label, so the strata that
    # win a partial round are not the alphabetically early ones. Ties (impossible
    # for distinct labels, but free) fall back to the label itself.
    rr_order = sorted(
        stratum_keys,
        key=lambda k: (rank_for(algorithm_version, seed, "stratum:" + "/".join(k)), k),
    )

    allocation = {key: 0 for key in stratum_keys}
    cohorts_present = sorted({key[0] for key in stratum_keys})
    cohort_alloc = {cohort: 0 for cohort in cohorts_present}
    caps = {
        cohort: _cap_for(cohort, size=size, share=share, explicit=explicit_caps)
        for cohort in cohorts_present
    }
    notes: list[str] = []

    def grant(key: tuple[str, str, str]) -> None:
        allocation[key] += 1
        cohort_alloc[key[0]] += 1

    def revoke(key: tuple[str, str, str]) -> None:
        allocation[key] -= 1
        cohort_alloc[key[0]] -= 1

    # --- pass 1: floor, round-robin ---------------------------------------- #
    spent = 0
    progress = True
    while spent < size and progress:
        progress = False
        for key in rr_order:
            if spent >= size:
                break
            if allocation[key] >= floor or allocation[key] >= capacity[key]:
                continue
            grant(key)
            spent += 1
            progress = True

    # --- pass 2: proportional over remaining headroom, largest remainder ---- #
    remaining = size - spent
    if remaining > 0:
        headroom = {key: capacity[key] - allocation[key] for key in stratum_keys}
        total_headroom = sum(headroom.values())
        if total_headroom > 0:
            if remaining >= total_headroom:
                for key in stratum_keys:
                    for _ in range(headroom[key]):
                        grant(key)
                        spent += 1
            else:
                remainders: list[tuple[int, str, tuple[str, str, str]]] = []
                for key in stratum_keys:
                    exact = remaining * headroom[key]
                    base = exact // total_headroom
                    for _ in range(base):
                        grant(key)
                        spent += 1
                    remainders.append(
                        (
                            exact - base * total_headroom,
                            rank_for(algorithm_version, seed,
                                     "remainder:" + "/".join(key)),
                            key,
                        )
                    )
                # Largest remainder first; ties broken by a seeded hash of the
                # stratum, never by its name.
                remainders.sort(key=lambda item: (-item[0], item[1], item[2]))
                for _, _rank, key in remainders:
                    if spent >= size:
                        break
                    if allocation[key] < capacity[key]:
                        grant(key)
                        spent += 1

    pre_cap_by_cohort = dict(cohort_alloc)

    # --- pass 3: cohort caps ------------------------------------------------ #
    removed_by_cap = 0
    floor_forced: set[str] = set()
    for cohort in cohorts_present:
        cap = caps[cohort]
        members = [key for key in stratum_keys if key[0] == cohort]
        while cohort_alloc[cohort] > cap:
            # Trim depth before breadth: a stratum above the floor loses a slot
            # before one at or below it does. Only when nothing is above the floor
            # does the cap start costing representation — and it says so, because
            # a cap and a floor genuinely can conflict (a cohort with more than
            # cap/floor strata) and silently keeping the floor would mean silently
            # breaching the cap.
            trimmable = [key for key in members if allocation[key] > floor]
            if not trimmable:
                trimmable = [key for key in members if allocation[key] > 0]
                if trimmable:
                    floor_forced.add(cohort)
            if not trimmable:
                notes.append(f"cohort {cohort}: cannot be trimmed to its cap of {cap}")
                break
            # Largest allocation first, so the cap comes off the deepest stratum;
            # ties broken by a seeded hash rather than by the stratum's name.
            victim = max(
                trimmable,
                key=lambda k: (
                    allocation[k],
                    rank_for(algorithm_version, seed, "trim:" + "/".join(k)),
                    k,
                ),
            )
            revoke(victim)
            removed_by_cap += 1
            spent -= 1

    for cohort in sorted(floor_forced):
        notes.append(
            f"cohort {cohort}: its cap of {caps[cohort]} forced strata below the "
            f"floor of {floor} — the cap is hard, the floor is a preference"
        )

    # --- pass 4: redistribution, for as many rounds as it takes ------------- #
    redistributed = 0
    rounds = 0
    progress = True
    while spent < size and progress:
        progress = False
        rounds += 1
        for key in rr_order:
            if spent >= size:
                break
            if allocation[key] >= capacity[key]:
                continue
            if cohort_alloc[key[0]] >= caps[key[0]]:
                continue
            grant(key)
            spent += 1
            redistributed += 1
            progress = True

    # --- assemble ----------------------------------------------------------- #
    selected: list[Candidate] = []
    strata: list[Stratum] = []
    for key in stratum_keys:
        take = allocation[key]
        chosen = buckets[key][:take]
        selected.extend(chosen)
        strata.append(
            Stratum(
                cohort=key[0],
                document_type=key[1],
                resource_type=key[2],
                available=capacity[key],
                allocated=take,
                selected=len(chosen),
                weak=len(chosen) < WEAK_THRESHOLD,
            )
        )

    # Canonical output order: the sample rank, globally. Not stratum order, not
    # database order — the ordering the fingerprint is taken over has to be a
    # property of the cohort itself.
    selected.sort(
        key=lambda c: (rank_for(algorithm_version, seed, c.comparison_key),
                       c.comparison_key)
    )
    keys = tuple(candidate.comparison_key for candidate in selected)

    unfillable = size - len(keys)
    exhausted = tuple(
        "/".join(key) for key in stratum_keys if allocation[key] >= capacity[key]
    )
    at_cap = tuple(
        cohort for cohort in cohorts_present if cohort_alloc[cohort] >= caps[cohort]
    )
    reason: str | None = None
    if unfillable > 0:
        if all(allocation[key] >= capacity[key] for key in stratum_keys):
            reason = "corpus_exhausted"
            notes.append(
                f"corpus exhausted before the requested size: "
                f"{len(keys)} of {size} candidates available"
            )
        elif all(
            allocation[key] >= capacity[key] or cohort_alloc[key[0]] >= caps[key[0]]
            for key in stratum_keys
        ):
            reason = "cohort_caps_reached"
            notes.append(
                "every cohort with headroom reached its cap before the requested "
                f"size ({len(keys)} of {size}); caps: "
                + ", ".join(f"{c}={caps[c]}" for c in cohorts_present)
            )
        else:  # pragma: no cover - defensive; the two branches above are total
            reason = "unallocated"
            notes.append(f"{unfillable} slots could not be allocated")

    floor_requested = sum(min(floor, capacity[key]) for key in stratum_keys)
    floor_allocated = sum(min(floor, allocation[key]) for key in stratum_keys)
    floor_short = tuple(
        "/".join(key)
        for key in stratum_keys
        if allocation[key] < min(floor, capacity[key])
    )
    candidates_by_cohort: dict[str, int] = {}
    for candidate in candidates:
        candidates_by_cohort[candidate.cohort] = (
            candidates_by_cohort.get(candidate.cohort, 0) + 1
        )

    diagnostics = Diagnostics(
        requested=size,
        selected=len(keys),
        candidates=len(candidates),
        strata=len(stratum_keys),
        allocation_by_stratum={
            "/".join(key): allocation[key]
            for key in stratum_keys
            if allocation[key]
        },
        allocation_by_cohort={c: n for c, n in sorted(cohort_alloc.items()) if n},
        pre_cap_allocation_by_cohort=dict(sorted(pre_cap_by_cohort.items())),
        candidates_by_cohort=dict(sorted(candidates_by_cohort.items())),
        capped_cohort=CAPPED_COHORT,
        capped_cohort_candidates=candidates_by_cohort.get(CAPPED_COHORT, 0),
        capped_cohort_selected=cohort_alloc.get(CAPPED_COHORT, 0),
        cohort_caps=dict(sorted(caps.items())),
        capped_cohorts=at_cap,
        floor=floor,
        floor_requested_slots=floor_requested,
        floor_allocated_slots=floor_allocated,
        floor_shortfall_slots=floor_requested - floor_allocated,
        floor_short_strata=floor_short,
        slots_removed_by_cap=removed_by_cap,
        slots_redistributed=redistributed,
        redistribution_rounds=rounds,
        unfillable_slots=unfillable,
        exhausted_strata=exhausted,
        complete=unfillable == 0,
        incomplete_reason=reason,
    )
    if floor_short:
        notes.append(
            f"floor {floor} unmet in {len(floor_short)} strata "
            f"({floor_requested - floor_allocated} slots short)"
        )

    return Sample(
        keys=keys,
        entries=tuple(selected),
        strata=tuple(strata),
        requested=size,
        algorithm_version=algorithm_version,
        seed=seed,
        parameters=parameters,
        diagnostics=diagnostics,
        shortfall=unfillable,
        notes=tuple(notes),
    )


def _empty_sample(
    size: int,
    seed: str,
    algorithm_version: str,
    parameters: dict[str, Any],
    candidates: int,
    floor: int,
    explicit_caps: Mapping[str, int],
    share: Fraction,
) -> Sample:
    """Nothing to draw from, or nothing asked for. Still a fully-formed answer:
    an empty sample that carries its diagnostics is auditable, an empty tuple is
    not."""
    diagnostics = Diagnostics(
        requested=size,
        selected=0,
        candidates=candidates,
        strata=0,
        allocation_by_stratum={},
        allocation_by_cohort={},
        pre_cap_allocation_by_cohort={},
        candidates_by_cohort={},
        capped_cohort=CAPPED_COHORT,
        capped_cohort_candidates=0,
        capped_cohort_selected=0,
        cohort_caps={},
        capped_cohorts=(),
        floor=floor,
        floor_requested_slots=0,
        floor_allocated_slots=0,
        floor_shortfall_slots=0,
        floor_short_strata=(),
        slots_removed_by_cap=0,
        slots_redistributed=0,
        redistribution_rounds=0,
        unfillable_slots=size,
        exhausted_strata=(),
        complete=size == 0,
        incomplete_reason=None if size == 0 else "no_candidates",
    )
    return Sample(
        keys=(),
        entries=(),
        strata=(),
        requested=size,
        algorithm_version=algorithm_version,
        seed=seed,
        parameters=parameters,
        diagnostics=diagnostics,
        shortfall=size,
        notes=("no candidates to sample",) if size > 0 else (),
    )

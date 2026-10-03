#!/usr/bin/env python
"""Draw and freeze the native-3 cohort (spec §6.2). EVIDENCE: local_ai_gateway_p4 only.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \\
        .venv/bin/python scripts/nrb_native3_cohort.py --out docs/nrb/native3-cohort.json
    # pre-registered top-up, only if the evidence pass says so (Task 12):
    ... scripts/nrb_native3_cohort.py --top-up --out docs/nrb/native3-cohort-topup.json

Drawn BEFORE any network access, from catalog METADATA alone, so nothing
native-3 computes can shape it. Every spent set is withheld before
stratification and the withheld set is bound by hash. The detector constants
are written INTO the frozen manifest: that is the record that D and N were
fixed before the cohort was drawn.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import localtime  # noqa: E402
from app.nrb import dbguard, fontrepair, manifest, sampling  # noqa: E402

ALGORITHM = "nrb-native3-v1"
SEED = "native3-2026-10-02"
# The spec's original {act, rule_bylaw, directive} >= 2015 lies wholly inside the
# withheld production scope (251 of 251), so the enriched stratum takes NRB's
# remaining regulatory, Word-authored sections. Metadata-only; decided by the
# user on 2026-10-03, before any network access.
ENRICHED_SECTIONS = frozenset({"circular", "guideline_manual"})
ENRICHED_SINCE = 2015
SIZES = {"enriched": 250, "random": 250}
TOP_UP = {"threshold_detected": 20, "size": 250, "stratum": "enriched"}
SPENT_SETS = (
    "docs/nrb/phase6a-manifest.json",
    "docs/nrb/phase6b-routing-holdout.json",
    "docs/nrb/phase7-validation-cohort.json",
    "docs/nrb/prod-corpus-scope.json",
)


def spent_keys(paths) -> frozenset[str]:
    keys: set[str] = set()
    for path in paths:
        for entry in json.loads(Path(path).read_text(encoding="utf-8"))["entries"]:
            keys.add(entry["comparison_key"])
    return frozenset(keys)


def stratum_of(c: sampling.Candidate) -> str:
    if c.document_type in ENRICHED_SECTIONS and c.year is not None and c.year >= ENRICHED_SINCE:
        return "enriched"
    return "random"


def draw(candidates, *, excluded: frozenset[str], seed: str, sizes: dict[str, int], offset: int = 0):
    pool = [c for c in candidates if c.resource_type == "pdf" and c.comparison_key not in excluded]
    out = []
    for stratum in ("enriched", "random"):
        members = sorted(
            (c for c in pool if stratum_of(c) == stratum),
            key=lambda c: sampling.rank_for(ALGORITHM, seed, c.comparison_key),
        )
        out.extend((stratum, c) for c in members[offset:offset + sizes.get(stratum, 0)])
    return out


def build(candidates, *, excluded, seed, sizes, offset, drawn_at, catalog_counts, excluded_sets):
    drawn = draw(candidates, excluded=excluded, seed=seed, sizes=sizes, offset=offset)
    entries = tuple({**c.as_entry(), "sampling_stratum": stratum} for stratum, c in drawn)
    keys = [e["comparison_key"] for e in entries]
    parameters = {
        "algorithm_version": ALGORITHM,
        "seed": seed,
        "frame": "nrb_files.resource_type = 'pdf' (the REST claim)",
        "enriched_sections": sorted(ENRICHED_SECTIONS),
        "enriched_since": ENRICHED_SINCE,
        "sizes": dict(sizes),
        "offset": offset,
        "top_up_rule": TOP_UP,
        "exclude_keys_sha256": hashlib.sha256("\n".join(sorted(excluded)).encode()).hexdigest(),
        "excluded_sets": list(excluded_sets),
        "detector": {"density": fontrepair.DETECT_DENSITY,
                     "min_devanagari": fontrepair.DETECT_MIN_DEVANAGARI},
    }
    requested = sum(sizes.values())
    strata = tuple(
        {"stratum": s, "selected": sum(1 for x, _ in drawn if x == s), "requested": sizes.get(s, 0)}
        for s in ("enriched", "random")
    )
    return manifest.Manifest(
        version=manifest.MANIFEST_VERSION,
        drawn_at=drawn_at,
        requested=requested,
        shortfall=requested - len(keys),
        sampler=parameters,
        catalog_counts=dict(catalog_counts),
        strata=strata,
        notes=(
            "native-3 cohort; population claims from the random stratum only (spec §6.2)",
            "enriched = circular/guideline_manual ≥2015: the spec's act/rule_bylaw/directive ≥2015 "
            "lies wholly inside the withheld production scope (user amendment 2026-10-03, "
            "before any network access)",
        ),
        entries=entries,
        algorithm_version=ALGORITHM,
        seed=seed,
        selected=len(keys),
        selection_sha256=manifest.compute_selection_sha256(
            manifest_version=manifest.MANIFEST_VERSION, algorithm_version=ALGORITHM,
            seed=seed, parameters=parameters, keys=keys),
        provenance={"excluded_manifests": list(excluded_sets)},
    )


async def _catalog():
    from app.db.session import SessionLocal
    from app.nrb import catalog

    async with SessionLocal() as session:
        rows = await catalog.load_sample_rows(session)
        counts = await catalog.catalog_counts(session)
    return rows, counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-up", action="store_true", help="the pre-registered next 250 enriched ranks")
    args = ap.parse_args()
    try:
        name = dbguard.require(os.environ.get("DATABASE_URL", ""), dbguard.EVIDENCE_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")
    rows, counts = asyncio.run(_catalog())
    repo = Path(__file__).resolve().parents[1]
    excluded = spent_keys([repo / p for p in SPENT_SETS])
    sizes = {"enriched": TOP_UP["size"], "random": 0} if args.top_up else SIZES
    offset = SIZES["enriched"] if args.top_up else 0
    built = build(sampling.build_candidates(rows), excluded=excluded, seed=SEED, sizes=sizes,
                  offset=offset, drawn_at=localtime.now().isoformat(timespec="seconds"),
                  catalog_counts=counts, excluded_sets=list(SPENT_SETS))
    manifest.write_new_manifest(built, args.out)
    print(json.dumps({"selected": built.selected, "shortfall": built.shortfall,
                      "strata": list(built.strata), "excluded": len(excluded)}, indent=2))
    print(f"selection_sha256 {built.selection_sha256}\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

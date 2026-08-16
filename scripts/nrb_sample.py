#!/usr/bin/env python
"""Draw the Phase 6A benchmark cohort ONCE and write it to a manifest.

    # look first — computes the draw and prints it, writes nothing
    .venv/bin/python scripts/nrb_sample.py --size 400 --seed phase6a-v1 --dry-run

    # freeze it
    .venv/bin/python scripts/nrb_sample.py --size 400 --seed phase6a-v1 \
        --floor 5 --max-cohort-share 0.30 --out docs/nrb/phase6a-manifest.json

    # check a committed manifest still hashes to what it claims
    .venv/bin/python scripts/nrb_sample.py --verify docs/nrb/phase6a-manifest.json

Separate from `nrb_extract.py` on purpose: sampling and extraction must not be the
same command, or a second profiling run would silently re-draw the cohort and the
two runs' numbers would not be comparable. This writes a file; everything else
reads it.

THE COHORT IS FROZEN BEFORE ANYTHING IS DOWNLOADED
    The sampling unit is `nrb_files.comparison_key`, drawn from the catalog — not
    from what happens to be on disk, and not from `content_sha256`, which does not
    exist until the file has been fetched. `scripts/nrb_fetch.py --manifest <path>`
    downloads exactly this cohort afterwards, through every Phase 5 guard.

    Which is why this command **refuses to overwrite an existing manifest**
    without `--overwrite`, and prints the old and new fingerprints when it does.
    Re-drawing a cohort silently makes every number already published from the old
    one incomparable, with nothing in the diff to say so.

MAKES NO NETWORK REQUEST AND DOWNLOADS NOTHING. It reads the catalog and writes
JSON.

Exit codes: 0 the cohort was drawn (or verified) in full, 1 it was drawn but
short of the requested size, or verification failed, 2 it refused to start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb import sampling  # noqa: E402
from app.nrb.manifest import (  # noqa: E402
    build_manifest,
    read_manifest,
    verify_manifest,
    write_new_manifest,
)
from app.nrb.report import render_sample, summarize_sample  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    draw = parser.add_argument_group("the draw")
    draw.add_argument(
        "--size", type=int, default=400, metavar="N",
        help="how many files the cohort should contain (default: 400)",
    )
    draw.add_argument(
        "--seed", default=sampling.DEFAULT_SEED, metavar="TEXT",
        help="the sampling seed. Part of the fingerprint: the same seed over the "
             f"same catalog draws the same cohort (default: {sampling.DEFAULT_SEED})",
    )
    draw.add_argument(
        "--floor", type=int, default=sampling.DEFAULT_FLOOR, metavar="N",
        help="minimum files per non-empty stratum, budget permitting "
             f"(default: {sampling.DEFAULT_FLOOR})",
    )
    draw.add_argument(
        "--max-cohort-share", type=str, default="0.30", metavar="FRACTION",
        help="no year cohort may exceed this share of the cohort (default: 0.30). "
             "Read as an exact rational, so 0.30 is 3/10 and not a float.",
    )
    draw.add_argument(
        "--year-2019-cap", type=int, default=None, metavar="N",
        help="an absolute ceiling on the 2019 cohort, in files. 2019 is NRB's CMS "
             "migration and half the corpus. Applied on top of --max-cohort-share; "
             "whichever is smaller wins.",
    )
    draw.add_argument(
        "--cohort-cap", action="append", default=None, metavar="COHORT=N",
        help="an absolute ceiling on any year cohort (e.g. '<=2018=40'); repeatable",
    )
    draw.add_argument(
        "--section", action="append", default=None, metavar="TYPE",
        help="restrict the DRAW to these document types; repeatable",
    )
    draw.add_argument(
        "--type", action="append", default=None, metavar="KIND", dest="resource_type",
        help="restrict the DRAW to these resource types; repeatable",
    )
    draw.add_argument(
        "--exclude-manifest", action="append", default=None, metavar="PATH",
        help="withhold every comparison_key named by this manifest from the "
             "candidate population BEFORE the draw; repeatable. The mechanism a "
             "holdout uses to exclude the cohort it validates (Phase 6A), so no "
             "file that shaped native-2 can enter its own validation set. The "
             "excluded set is fingerprinted into the sampler parameters; the "
             "source manifest's path and cohort fingerprint are recorded as "
             "provenance.",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--out", "--output", dest="out", default=None, metavar="PATH",
        help="where to write the manifest. Required unless --dry-run or --verify.",
    )
    output.add_argument(
        "--overwrite", "--force", dest="overwrite", action="store_true",
        help="replace an existing manifest. Prints the old and new fingerprints; "
             "a benchmark is meant to be drawn once, so this is never implicit.",
    )
    output.add_argument(
        "--dry-run", action="store_true",
        help="draw and report, write nothing. Makes no HTTP request either way.",
    )
    output.add_argument(
        "--verify", default=None, metavar="PATH",
        help="recompute an existing manifest's selection fingerprint from its own "
             "contents and report whether it still matches. Does not resample and "
             "does not read the catalog.",
    )
    output.add_argument("--json", action="store_true",
                        help="emit the summary as JSON")
    return parser.parse_args(argv)


def _cohort_caps(args: argparse.Namespace) -> dict[str, int]:
    """Absolute per-cohort ceilings, from the two flags that can set them.

    `--year-2019-cap` is spelled out separately because 2019 is the cohort the
    whole cap exists for, and burying it in a generic `--cohort-cap 2019=80` makes
    the one policy value that matters easy to leave off by accident.
    """
    caps: dict[str, int] = {}
    for raw in args.cohort_cap or []:
        # rpartition, not partition: a cohort label legitimately contains an '='
        # (`<=2018`), so the LAST separator is the one that divides name from value.
        cohort, _, value = raw.rpartition("=")
        if not cohort or not value.strip().isdigit():
            raise ValueError(f"--cohort-cap wants COHORT=N, got {raw!r}")
        caps[cohort.strip()] = int(value)
    if args.year_2019_cap is not None:
        caps[sampling.CAPPED_COHORT] = args.year_2019_cap
    return caps


def _load_exclusions(
    args: argparse.Namespace,
) -> tuple[set[str], dict[str, Any]]:
    """The union of comparison_keys named by every `--exclude-manifest`, plus the
    provenance the output manifest records about them.

    Reads each source manifest through `read_manifest` (so an unknown version or a
    keyless entry is refused, not silently skipped — a leaky exclusion is worse
    than none) and records each one's path, cohort fingerprint and key count. The
    union is what the sampler withholds; the provenance is what a later reader uses
    to see exactly which cohorts were held out.
    """
    excluded: set[str] = set()
    sources: list[dict[str, Any]] = []
    for path in args.exclude_manifest or []:
        manifest = read_manifest(path)
        keys = manifest.keys()
        excluded.update(keys)
        sources.append({
            "path": str(path),
            "selection_sha256": manifest.selection_sha256,
            "keys": len(keys),
        })
    provenance = {"excluded_manifests": sources} if sources else {}
    return excluded, provenance


async def _load_catalog(args: argparse.Namespace):
    """The only part of this command that touches the database. Read-only, and a
    separate function so the rest can be exercised without one."""
    from app.db.session import SessionLocal
    from app.nrb import catalog

    async with SessionLocal() as session:
        rows = await catalog.load_sample_rows(
            session, sections=args.section, resource_types=args.resource_type
        )
        counts = await catalog.catalog_counts(session)
    return rows, counts


def _verify(path: str, as_json: bool) -> int:
    try:
        manifest = read_manifest(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    result = verify_manifest(manifest)
    payload = {
        "path": str(path),
        "ok": result.ok,
        "reason": result.reason,
        "recorded": result.recorded,
        "recomputed": result.recomputed,
        "entries": len(manifest.entries),
        "keys": len(manifest.keys()),
        "algorithm_version": manifest.algorithm_version,
        "seed": manifest.seed,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"{path}: {len(manifest.keys())} keys, {manifest.algorithm_version}, "
              f"seed {manifest.seed!r}")
        print(f"  recorded:   {result.recorded or '(none)'}")
        print(f"  recomputed: {result.recomputed}")
        print(f"  verdict:    {result.reason}")
    return 0 if result.ok else 1


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.verify:
        return _verify(args.verify, args.json)

    if not args.out and not args.dry_run:
        print(
            "refusing to start: no --out given. A benchmark cohort that is not "
            "written down is not a benchmark — use --dry-run to look at a draw "
            "without freezing it.",
            file=sys.stderr,
        )
        return 2

    try:
        caps = _cohort_caps(args)
    except ValueError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    # Checked BEFORE the catalog is read, so a run that cannot write anything
    # costs nothing and cannot look like it half-succeeded.
    out = Path(args.out) if args.out else None
    if out is not None and out.exists() and not args.overwrite:
        print(
            f"refusing to overwrite {out}: a benchmark cohort is drawn ONCE, and "
            f"re-drawing it makes the new profile incomparable with every number "
            f"published from the old one. Pass --overwrite if that is really what "
            f"you want (it will print both fingerprints).",
            file=sys.stderr,
        )
        return 2

    try:
        exclude_keys, provenance = _load_exclusions(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"refusing to start: unreadable --exclude-manifest — {exc}",
              file=sys.stderr)
        return 2

    rows, counts = await _load_catalog(args)
    sample = sampling.stratified_sample(
        rows,
        size=args.size,
        seed=args.seed,
        floor=args.floor,
        max_cohort_share=args.max_cohort_share,
        cohort_caps=caps or None,
        exclude_keys=exclude_keys or None,
    )
    manifest = build_manifest(
        sample,
        drawn_at=datetime.now(timezone.utc).isoformat(),
        catalog_counts=counts,
        provenance=provenance or None,
    )
    if exclude_keys:
        print(
            f"excluded {len(exclude_keys)} keys from "
            f"{len(provenance['excluded_manifests'])} manifest(s) before drawing",
            file=sys.stderr,
        )

    summary = summarize_sample(manifest)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)
          if args.json else render_sample(summary))

    if out is None:
        print("DRY RUN — nothing written.", file=sys.stderr)
    else:
        previous = write_new_manifest(manifest, out, overwrite=args.overwrite)
        if previous is not None:
            print(f"replaced cohort {previous}", file=sys.stderr)
            print(f"with     cohort {manifest.selection_sha256}", file=sys.stderr)
        print(
            f"wrote {len(manifest.entries)} of {args.size} requested -> {out}",
            file=sys.stderr,
        )

    if manifest.shortfall:
        print(
            f"SHORTFALL {manifest.shortfall}: "
            f"{manifest.diagnostics.get('incomplete_reason')} — "
            + "; ".join(manifest.notes),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

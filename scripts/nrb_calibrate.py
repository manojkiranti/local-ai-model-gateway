#!/usr/bin/env python
"""Compare pypdf and Docling NATIVE EXTRACTION over a frozen benchmark slice (Phase 6A).

Phase 6A screens with pypdf (~41 pages/s) rather than Docling (~1-2 pages/s on
CPU) because both read the same embedded text layer to answer the same question.
This command is the evidence for that choice — and the answer to "is native
Docling sufficient for a meaningful percentage of the corpus?", which cannot be
answered by asserting it.

    # 1. freeze the subset (once) — 40 PDFs drawn from the benchmark itself
    .venv/bin/python scripts/nrb_calibrate.py --freeze \
        --manifest docs/nrb/phase6a-manifest.json \
        --out docs/nrb/phase6a-docling-calibration.json

    # 2. check a committed subset against itself and its parent
    .venv/bin/python scripts/nrb_calibrate.py \
        --verify docs/nrb/phase6a-docling-calibration.json

    # 3. look before running — resolves everything, parses nothing
    .venv/bin/python scripts/nrb_calibrate.py \
        --subset docs/nrb/phase6a-docling-calibration.json --dry-run

    # 4. …then run it (WORKER DEPENDENCIES: docling, torch. Slow by design.)
    .venv/bin/python scripts/nrb_calibrate.py \
        --subset docs/nrb/phase6a-docling-calibration.json

WHAT IS COMPARED
    Extraction, not pipelines. It does NOT call `parse_to_chunks`, which layers
    RAG's chunk merging, small-block dropping and front-matter skipping on top of
    Docling — a disagreement there could come from the filter rather than the
    parser. Both engines' raw text goes through the SAME Phase 6A metrics and
    classifier, at the same thresholds.

WHAT IS REPORTED
    Status and reason agreement, both engines' own distributions, the pairwise
    deltas, and the two ASYMMETRIC counts that matter more than any average —

      * DOCLING RESCUED PYPDF — pypdf says needs_ocr/suspicious/failed, Docling
        says extracted. This is the case that would invalidate the screen.
      * PYPDF RESCUES DOCLING — the reverse.

    A single agreement percentage would hide both inside it.

WHAT IT NEVER DOES
    No HTTP, no fetch, no OCR, no legacy-font conversion, no chunking, no
    embedding, and NO WRITE: this is bounded experimental calibration, and
    `nrb_extractions` is the canonical screen. A subset member that is not
    downloaded is reported as such and is never substituted.

Exit codes: 0 completed, 1 verification failed or something could not be
compared, 2 it could not start (no mode, unreadable file, refusing to overwrite).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb import calibration  # noqa: E402
from app.nrb.calibrate import run_calibration  # noqa: E402
from app.nrb.extraction import EXTRACTOR_VERSION, docling_pipeline_is_native  # noqa: E402
from app.nrb.manifest import read_manifest  # noqa: E402
from app.nrb.report import render_calibration, summarize_calibration  # noqa: E402

DEFAULT_SUBSET = "docs/nrb/phase6a-docling-calibration.json"
DEFAULT_MANIFEST = "docs/nrb/phase6a-manifest.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_argument_group("mode (exactly one is REQUIRED)")
    mode.add_argument(
        "--subset", default=None, metavar="PATH",
        help="run the comparison over this frozen calibration subset",
    )
    mode.add_argument(
        "--freeze", action="store_true",
        help="draw and write the calibration subset from a benchmark manifest",
    )
    mode.add_argument(
        "--verify", default=None, metavar="PATH",
        help="recompute a subset's fingerprint and check it against its parent",
    )

    freeze = parser.add_argument_group("freeze")
    freeze.add_argument("--manifest", default=DEFAULT_MANIFEST, metavar="PATH",
                        help=f"the parent benchmark (default: {DEFAULT_MANIFEST})")
    freeze.add_argument("--out", "--output", dest="out", default=DEFAULT_SUBSET,
                        metavar="PATH", help=f"default: {DEFAULT_SUBSET}")
    freeze.add_argument(
        "--size", type=int, default=calibration.DEFAULT_SUBSET_SIZE, metavar="N",
        help=f"how many PDFs to compare (default {calibration.DEFAULT_SUBSET_SIZE}). "
             f"Docling is minutes per dozen files; this is a bounded engineering "
             f"calibration, not a powered corpus estimate.",
    )
    freeze.add_argument(
        "--expect-parent", default=None, metavar="SHA256",
        help="refuse unless the parent manifest has exactly this fingerprint",
    )
    freeze.add_argument(
        "--overwrite", "--force", dest="overwrite", action="store_true",
        help="replace an existing subset (both fingerprints are printed)",
    )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="compare at most N blobs. Applied AFTER subset resolution, fetched-"
             "state filtering and content deduplication, to a list ordered by the "
             "subset's own rank — so --limit 10 is the same 10 every run.",
    )
    behaviour.add_argument(
        "--dry-run", action="store_true",
        help="report exactly what would be compared. Builds no Docling converter, "
             "calls neither parser, opens no blob, writes nothing.",
    )
    behaviour.add_argument(
        "--extractor-version", default=EXTRACTOR_VERSION, metavar="TAG",
        help=f"which screen results to report the subset against (default: "
             f"{EXTRACTOR_VERSION}). Nothing is written at this version or any "
             f"other.",
    )
    behaviour.add_argument("--json", action="store_true",
                           help="emit the summary as JSON")
    behaviour.add_argument("-v", "--verbose", action="store_true",
                           help="show progress logs")
    return parser.parse_args(argv)


def _freeze(args: argparse.Namespace) -> int:
    try:
        manifest = read_manifest(args.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"refusing to freeze: unreadable manifest — {exc}", file=sys.stderr)
        return 2

    try:
        subset = calibration.build_subset(
            manifest,
            parent_manifest_path=args.manifest,
            size=args.size,
            generated_at=datetime.now(timezone.utc).isoformat(),
            expect_parent_sha256=args.expect_parent,
        )
    except ValueError as exc:
        print(f"refusing to freeze: {exc}", file=sys.stderr)
        return 2

    try:
        previous = calibration.write_new_subset(
            subset, args.out, overwrite=args.overwrite
        )
    except FileExistsError as exc:
        print(f"refusing to freeze: {exc}", file=sys.stderr)
        return 2

    if previous:
        print(f"REPLACED a subset whose fingerprint was {previous}")
    print(f"{args.out}: {subset.selected_size} of {subset.requested_size} requested "
          f"{subset.resource_type} files")
    print(f"  parent:  {subset.parent_selection_sha256}")
    print(f"  subset:  {subset.subset_selection_sha256}")
    return 0


def _verify(path: str, manifest_path: str) -> int:
    try:
        subset = calibration.read_subset(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"unreadable calibration subset — {exc}", file=sys.stderr)
        return 2

    own = calibration.verify_subset(subset)
    print(f"{path}: {subset.selected_size} keys, "
          f"{subset.subset_algorithm_version}, purpose {subset.purpose}")
    print(f"  recorded:   {own.recorded or '(none)'}")
    print(f"  recomputed: {own.recomputed}")
    print(f"  verdict:    {own.reason}")

    parent_path = manifest_path or subset.parent_manifest_path
    try:
        manifest = read_manifest(parent_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"  parent:     UNREADABLE ({exc})", file=sys.stderr)
        return 1
    against = calibration.verify_against_parent(subset, manifest)
    print(f"  parent:     {against.reason}  ({parent_path})")
    return 0 if (own.ok and against.ok) else 1


async def _run(args: argparse.Namespace) -> int:
    try:
        subset = calibration.read_subset(args.subset)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"refusing to start: unreadable calibration subset — {exc}",
              file=sys.stderr)
        return 2

    # A warning, not a refusal: a hand-authored subset with no fingerprint is
    # legitimate for development. One whose fingerprint does not match its own
    # contents has been edited, and every number below would be attributed to a
    # calibration slice that no longer exists.
    check = calibration.verify_subset(subset)
    if not check.ok:
        print(f"WARNING: subset fingerprint {check.reason} — this is not the "
              f"calibration slice its subset_selection_sha256 describes",
              file=sys.stderr)
    print(f"subset: {subset.selected_size} {subset.resource_type} files of the "
          f"benchmark {subset.parent_selection_sha256}", file=sys.stderr)

    if not args.dry_run:
        ok, evidence = docling_pipeline_is_native()
        if not ok:
            print(f"refusing to calibrate: {evidence}. The shared Docling pipeline "
                  f"is no longer CPU/no-OCR, so this would not be a native "
                  f"comparison — and Phase 6A does not run OCR.", file=sys.stderr)
            return 2

    result = await run_calibration(
        subset=subset,
        subset_path=args.subset,
        limit=args.limit,
        dry_run=args.dry_run,
        extractor_version=args.extractor_version,
    )
    summary = summarize_calibration(result, subset=subset, subset_path=args.subset)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)
          if args.json else render_calibration(summary))

    skipped = (result.counters.get("blobs_missing_on_disk", 0)
               + result.counters.get("blobs_corrupt_on_disk", 0))
    if skipped:
        print(f"NOTE: {skipped} selected blobs could not be compared (missing or "
              f"corrupt on disk)", file=sys.stderr)
        return 1
    return 0


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,   # keeps stdout clean for --json redirection
    )

    chosen = [bool(args.subset), args.freeze, bool(args.verify)]
    if sum(chosen) != 1:
        print(
            "refusing to start: choose exactly one of --freeze, --verify PATH or "
            "--subset PATH. There is no default mode, because freezing a subset "
            "and running a multi-minute Docling comparison are not the same "
            "decision.",
            file=sys.stderr,
        )
        return 2

    if args.freeze:
        return _freeze(args)
    if args.verify:
        return _verify(args.verify, args.manifest)
    return await _run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

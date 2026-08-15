#!/usr/bin/env python
"""Extract text from fetched NRB blobs and classify how trustworthy it is (Phase 6A).

Reads blobs that are ALREADY on disk, parses them natively (pypdf / python-docx /
openpyxl), measures the text and records a verdict per blob. It downloads nothing,
runs no OCR, converts no legacy font, chunks nothing and embeds nothing.

    # the frozen benchmark cohort — look first, parse nothing
    .venv/bin/python scripts/nrb_extract.py \
        --manifest docs/nrb/phase6a-manifest.json --dry-run

    # …then run it
    .venv/bin/python scripts/nrb_extract.py --manifest docs/nrb/phase6a-manifest.json

    # a bounded developer slice: the FIRST 10 pending blobs of the benchmark,
    # in the manifest's own order, so it is the same 10 every time
    .venv/bin/python scripts/nrb_extract.py \
        --manifest docs/nrb/phase6a-manifest.json --limit 10 -v

    # a non-benchmark slice, for trying the pass out on real files
    .venv/bin/python scripts/nrb_extract.py --section circular --limit 25

SCOPE IS REQUIRED — THERE IS NO DEFAULT
    Same rule as `nrb_fetch.py`, for a different cost: extraction is CPU-bound
    over a corpus of 18.3k files, so a bare invocation exits 2 rather than
    quietly starting the lot. `--all` exists and means it.

THE MANIFEST NAMES FILES; THE PASS EXTRACTS BLOBS
    A cohort of 400 `comparison_key`s is not 400 extractions. Some are not
    downloaded yet, and two cohort files with identical bytes are ONE blob and one
    verdict. The report keeps the two populations apart, and never substitutes a
    different file for a missing one — the frozen cohort is the benchmark, and
    what is measured is whatever part of it exists, stated as such.

RESUMABLE
    Selection is "fetched blobs with no extraction at this extractor version", and
    results commit every 25 blobs, so an interrupted pass keeps its progress and
    re-running continues. A repeat pass over an exhausted scope selects zero.
    Bumping `EXTRACTOR_VERSION` (or `--extractor-version`) makes work selectable
    again without deleting a row.

Exit codes: 0 the pass completed with no failures, 1 it ran but something failed,
2 it could not start (no scope, unreadable manifest, or another extraction holds
the lock).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb.extract import run_extract  # noqa: E402
from app.nrb.extraction import EXTRACTOR_VERSION  # noqa: E402
from app.nrb.locks import LockBusy  # noqa: E402
from app.nrb.manifest import read_manifest, verify_manifest  # noqa: E402
from app.nrb.report import render_extraction, summarize_extraction  # noqa: E402

# The regulatory core, in `classify.SECTIONS` vocabulary — the same set
# `nrb_fetch.py --core` downloads, so the two commands mean the same thing by it.
CORE_SECTIONS = (
    "directive", "circular", "act", "rule_bylaw", "guideline_manual", "monetary_policy",
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    scope = parser.add_argument_group("scope (at least one is REQUIRED)")
    scope.add_argument(
        "--manifest", default=None, metavar="PATH",
        help="extract EXACTLY the blobs a benchmark manifest's files resolve to. "
             "The manifest holds catalog keys, not URLs and not paths: it can only "
             "select rows the catalog already has, and a key that is not fetched "
             "yet is reported, never substituted.",
    )
    scope.add_argument(
        "--core", action="store_true",
        help=f"the regulatory core: {', '.join(CORE_SECTIONS)}",
    )
    scope.add_argument(
        "--section", action="append", default=None, metavar="TYPE",
        help="restrict to this document_type; repeatable",
    )
    scope.add_argument(
        "--owner", action="append", default=None, metavar="CODE",
        help="restrict to this NRB department/office code; repeatable",
    )
    scope.add_argument(
        "--type", action="append", default=None, metavar="KIND", dest="resource_type",
        help="restrict to this resource_type (pdf, spreadsheet, document, image)",
    )
    scope.add_argument(
        "--year", action="append", type=int, default=None, metavar="YYYY",
        help="restrict to documents NRB published in this year; repeatable",
    )
    scope.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="extract at most N blobs. Applied AFTER cohort resolution, content "
             "deduplication and current-version filtering, to a list ordered by "
             "the manifest's own rank — so --limit 10 is the same 10 blobs every "
             "run, not the first 10 a query happened to return.",
    )
    scope.add_argument(
        "--all", action="store_true",
        help="every fetched blob with no current extraction. Explicit, and it "
             "means the whole corpus.",
    )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument(
        "--extractor-version", default=EXTRACTOR_VERSION, metavar="TAG",
        help=f"the version to record and to treat as current (default: "
             f"{EXTRACTOR_VERSION}). A blob is skipped only on an EXACT "
             f"(content_sha256, extractor_version) match; a result from an older "
             f"version does not make it current.",
    )
    behaviour.add_argument(
        "--force", action="store_true",
        help="re-extract blobs already recorded at this version (development; "
             "bumping the version is the honest way to invalidate)",
    )
    behaviour.add_argument(
        "--dry-run", action="store_true",
        help="report exactly what would be extracted. Opens no blob, calls no "
             "parser, writes no row.",
    )
    behaviour.add_argument("--json", action="store_true",
                           help="emit the summary as JSON")
    behaviour.add_argument("-v", "--verbose", action="store_true",
                           help="show progress logs")
    return parser.parse_args(argv)


def _sections_for(args: argparse.Namespace) -> list[str]:
    """The document types this invocation asks for, order-stable and deduplicated."""
    sections = list(args.section or [])
    if args.core:
        sections.extend(CORE_SECTIONS)
    return list(dict.fromkeys(sections))


def _scope_given(args: argparse.Namespace) -> bool:
    """Whether ANY slice was named — the rule the whole command turns on.

    A separate function because a new scope flag that is not added here silently
    becomes a whole-corpus extraction.
    """
    return bool(
        _sections_for(args)
        or args.manifest
        or args.owner
        or args.resource_type
        or args.year
        or args.limit
        or args.all
    )


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,   # keeps stdout clean for --json redirection
    )

    if not _scope_given(args):
        print(
            "refusing to start: no scope given. Extraction is CPU-bound over a "
            "corpus of ~18.3k files, so a slice must be chosen explicitly — try "
            "--manifest docs/nrb/phase6a-manifest.json --dry-run, or --all if you "
            "really mean all of it.",
            file=sys.stderr,
        )
        return 2

    manifest = None
    keys = None
    if args.manifest:
        try:
            manifest = read_manifest(args.manifest)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"refusing to start: unreadable manifest — {exc}", file=sys.stderr)
            return 2
        keys = manifest.keys()
        check = verify_manifest(manifest)
        # A warning, not a refusal: a hand-authored cohort with no fingerprint is
        # legitimate for development. A cohort whose fingerprint does not match
        # its own contents has been edited, and every number below would be
        # attributed to a benchmark that no longer exists — so say so loudly.
        if not check.ok:
            print(
                f"WARNING: manifest fingerprint {check.reason} — this cohort is "
                f"not the one its selection_sha256 describes",
                file=sys.stderr,
            )
        print(
            f"manifest: {len(keys)} files, drawn {manifest.drawn_at or '?'}, "
            f"cohort {manifest.selection_sha256 or '(unfingerprinted)'}",
            file=sys.stderr,
        )

    try:
        result = await run_extract(
            keys=keys,
            sections=_sections_for(args) or None,
            owners=args.owner,
            resource_types=args.resource_type,
            years=args.year,
            limit=args.limit,
            force=args.force,
            extractor_version=args.extractor_version,
            dry_run=args.dry_run,
        )
    except LockBusy as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    cohort = None
    if keys:
        # Re-resolved for the report so the metadata breakdowns can be joined
        # against the manifest's own frozen entries. Read-only.
        from app.db.session import SessionLocal
        from app.nrb import profile

        async with SessionLocal() as session:
            cohort = await profile.load_cohort(
                session, keys=keys, extractor_version=args.extractor_version
            )

    summary = summarize_extraction(
        result, cohort=cohort, manifest=manifest, manifest_path=args.manifest
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)
          if args.json else render_extraction(summary))

    if result.counters.get("blobs_failed"):
        print(
            f"NOTE: {result.counters['blobs_failed']} blobs failed extraction "
            f"(recorded as `failed` rows, not skipped)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

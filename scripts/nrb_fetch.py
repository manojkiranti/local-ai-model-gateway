#!/usr/bin/env python
"""Download NRB's published files into the local blob store (Phase 5).

Takes files the Phase 4 catalog has recorded as fetchable, downloads them under a
byte cap, verifies the bytes against what NRB claimed, hashes them, and stores them
content-addressed under `NRB_FILES_DIR`. Writes one `nrb_fetch_runs` row per pass
and updates each `nrb_files` row with its sha256, length, storage key and sniffed
type.

**This is where Phase 5 stops.** Nothing here parses a PDF, runs OCR, chunks,
embeds, creates a `documents` row or exposes a tool. A stored file is a raw
artefact; deciding what it *says* is Phase 6+.

SCOPE IS REQUIRED — THERE IS NO DEFAULT
    The full corpus is ~18.3k files and ~8.6 GB against a central bank's website, so
    this command refuses to run without being told what to fetch. Pick a slice:

    # the regulatory core: ~1,800 files, ~1.5 GB
    .venv/bin/python scripts/nrb_fetch.py --core

    # dry run first — no HTTP at all, just what would be fetched and how big
    .venv/bin/python scripts/nrb_fetch.py --core --dry-run

    # a small live smoke test
    .venv/bin/python scripts/nrb_fetch.py --section circular --limit 25 -v

    # one department, spreadsheets only
    .venv/bin/python scripts/nrb_fetch.py --owner red --type spreadsheet

    # everything (explicit, and it means 8.6 GB)
    .venv/bin/python scripts/nrb_fetch.py --all --max-bytes 2000000000

    # retry what failed last time, and nothing else
    .venv/bin/python scripts/nrb_fetch.py --core --retry-failed

WHAT IT WILL NOT DO
    Fetch over plain http, or from any host but `NRB_SITE_BASE_URL`'s (the three
    known `uat.nrb.org.np` attachments are `blocked_host` in the catalog and can
    never be selected). Follow a redirect. Buffer a body in memory. Retry inside a
    pass. Store an HTML error page as a document — WordPress answers a missing file
    with a themed 200 page, so a body that sniffs as HTML where a document was
    promised is recorded as a failure and nothing is written.

RESUMABILITY
    Results are committed every 25 files and selection is `pending`-only in id
    order, so an interrupted pass keeps its progress and re-running continues from
    where it stopped. Storage is content-addressed, so the worst an interruption can
    leave is an unreferenced blob, which the next attempt at that file recognises.

Exit codes: 0 the pass completed with no failures, 1 it ran but something failed or
it stopped early, 2 it could not start (no scope given, or another fetch holds the
lock).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb.fetch import run_fetch  # noqa: E402
from app.nrb.locks import LockBusy  # noqa: E402
from app.nrb.report import render_fetch, summarize_fetch  # noqa: E402

# The regulatory core, in `classify.SECTIONS` vocabulary. Measured live: 1,804
# files / ~1.5 GB, and it is exactly the set whose document type is most reliable
# (~95% coverage post-2019, versus the 2019 `upload-files` backlog).
CORE_SECTIONS = (
    "directive", "circular", "act", "rule_bylaw", "guideline_manual", "monetary_policy",
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    scope = parser.add_argument_group("scope (at least one is REQUIRED)")
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
        "--limit", type=int, default=None, metavar="N",
        help="fetch at most N files (oldest catalog rows first, so it resumes)",
    )
    scope.add_argument(
        "--all", action="store_true",
        help="every fetchable file (~18.3k, ~8.6 GB). Required for a full download.",
    )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument(
        "--retry-failed", action="store_true",
        help="also re-attempt files whose last fetch failed",
    )
    behaviour.add_argument(
        "--max-bytes", type=int, default=None, metavar="N",
        help="stop once this many bytes have been downloaded (the rest are reported "
             "as skipped, and a later pass picks them up)",
    )
    behaviour.add_argument(
        "--dry-run", action="store_true",
        help="report what would be fetched and how large NRB says it is. Makes NO "
             "HTTP requests at all.",
    )
    behaviour.add_argument("--json", action="store_true", help="emit the summary as JSON")
    behaviour.add_argument("-v", "--verbose", action="store_true", help="show progress logs")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,   # keeps stdout clean for --json redirection
    )

    sections = list(args.section or [])
    if args.core:
        sections.extend(CORE_SECTIONS)
    # Deduplicated but order-stable, so the recorded scope reads the way it was typed.
    sections = list(dict.fromkeys(sections))

    if not (sections or args.owner or args.resource_type or args.limit or args.all):
        print(
            "refusing to start: no scope given. The whole corpus is ~18.3k files and "
            "~8.6 GB against a central bank's website, so a slice must be chosen "
            "explicitly — try --core --dry-run, or --all if you really mean all of it.",
            file=sys.stderr,
        )
        return 2

    try:
        result = await run_fetch(
            sections=sections or None,
            owners=args.owner,
            resource_types=args.resource_type,
            limit=args.limit,
            retry_failed=args.retry_failed,
            max_bytes=args.max_bytes,
            dry_run=args.dry_run,
        )
    except LockBusy as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    summary = summarize_fetch(result)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)
          if args.json else render_fetch(summary))

    if not result.ok:
        print(
            f"NOTE: pass status is {result.status} "
            f"(failed={result.counters.get('files_failed', 0)}, "
            f"skipped={result.counters.get('files_skipped', 0)})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

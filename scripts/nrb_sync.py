#!/usr/bin/env python
"""Reconcile NRB's published corpus into the persistent catalog (Phase 4).

Reads the whole corpus from NRB (WordPress REST API + sitemap), then inserts,
updates and deactivates rows in `nrb_sources` / `nrb_files` / `nrb_source_files`
so the catalog matches what NRB publishes today. Writes one `nrb_sync_runs` row
recording exactly what changed.

**Nothing is downloaded.** No attachment is fetched, no bytes are hashed, no text
is extracted, nothing is embedded, no `ingest_jobs` row is created and no tool is
exposed to the model — those are Phase 5+. This command only records what exists
and where it lives.

USAGE
    .venv/bin/python scripts/nrb_sync.py                # the real sync
    .venv/bin/python scripts/nrb_sync.py --dry-run      # compute, change nothing
    .venv/bin/python scripts/nrb_sync.py --json         # machine-readable summary
    .venv/bin/python scripts/nrb_sync.py -v             # progress logs

    # bounded smoke test — cannot deactivate anything (see below)
    .venv/bin/python scripts/nrb_sync.py --limit 300

IDEMPOTENCY IS THE ACCEPTANCE TEST
    Run it twice. The second run against an unchanged NRB must report
    `created: 0`, `updated: 0` and everything else `unchanged` — only
    `last_seen_at` moves. If the second run shows updates, either NRB changed
    between the runs (the counters say which sources) or the metadata hash is
    picking up something that is not upstream metadata.

WHY A BOUNDED RUN NEVER DEACTIVATES
    Deactivation is absence-based: an active source this run did not see is
    treated as withdrawn. That is only true if the run saw everything, so
    `--limit`, `--no-sitemap` and any fetch failure mark the discovery incomplete
    and the deactivation step is skipped with a reason. A truncated run that
    deactivated the rows it never reached would destroy thousands of good records
    on a transient network fault.

Requests are sequential and paced by `NRB_CRAWL_DELAY_SECONDS`, the same
politeness as the Phase 2/3 inventories. A full run is ~190 REST requests plus
~60 sitemaps.

Exit codes: 0 the run completed, 1 it ran but was partial/had errors, 2 it could
not start (no taxonomy, no sitemap root, or another sync holds the lock).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb.discovery import DiscoveryError  # noqa: E402
from app.nrb.report import render_sync, summarize_sync  # noqa: E402
from app.nrb.sync import SyncBusy, run_sync  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="compute the whole reconciliation, then roll it back — the database "
             "is left byte-identical",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="stop after N documents (a smoke test). Marks the run incomplete, so "
             "nothing can be deactivated.",
    )
    parser.add_argument(
        "--no-sitemap", action="store_true",
        help="skip sitemap discovery (faster, but the corpus gap cannot be "
             "measured, so the run is incomplete and cannot deactivate)",
    )
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="show progress logs")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,   # keeps stdout clean for --json redirection
    )

    try:
        # Discovery happens inside `run_sync`, under the advisory lock, so a
        # second invocation refuses in milliseconds instead of first spending
        # ~190 requests and several minutes on NRB's website.
        result = await run_sync(
            limit=args.limit,
            include_sitemap=not args.no_sitemap,
            dry_run=args.dry_run,
        )
    except SyncBusy as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    except DiscoveryError as exc:
        # Nothing to reconcile against — and reconciling against nothing is
        # indistinguishable from NRB having deleted its entire site.
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 2

    summary = summarize_sync(result)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)
          if args.json else render_sync(summary))

    if not result.ok:
        print(
            f"NOTE: run status is {result.status} "
            f"(errors={result.counters.get('error_count', 0)}, "
            f"complete={result.discovery_complete})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

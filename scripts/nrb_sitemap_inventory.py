#!/usr/bin/env python
"""Developer-facing NRB sitemap inventory (Phase 2 discovery).

Fetches Nepal Rastra Bank's published sitemap tree, classifies every URL, and
prints what NRB actually publishes. Read-only: nothing is written to Postgres, no
document is downloaded, no embedding is produced, and no tool is exposed to the
model. It exists so the Phase 3 source model and sync design can be argued from
the real site instead of an assumption about it.

USAGE
    .venv/bin/python scripts/nrb_sitemap_inventory.py
    .venv/bin/python scripts/nrb_sitemap_inventory.py --json > inventory.json
    .venv/bin/python scripts/nrb_sitemap_inventory.py --urls > urls.tsv
    .venv/bin/python scripts/nrb_sitemap_inventory.py --sample 60
    .venv/bin/python scripts/nrb_sitemap_inventory.py --root https://www.nrb.org.np/bfr-sitemap1.xml

    # -v shows the per-sitemap progress lines while it runs (~60 requests)
    .venv/bin/python scripts/nrb_sitemap_inventory.py -v

This lives in `scripts/` alongside `eval_nrb_forex_routing.py` rather than being
`python -m app.nrb.…`: in this repo a `python -m` entrypoint means a long-running
production process (`app.rag.worker`), and this is a report you run by hand.

It requires network access by design — it is the live probe, not a unit test. The
unit tests (`tests/test_nrb_sitemap.py`) cover the same code with mocked HTTP and
never touch the internet.

Exit codes: 0 on a clean run, 1 if a bound truncated the inventory or any sitemap
failed — a partial inventory that exits 0 is one that gets mistaken for the whole
site.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb.report import SAMPLE_SIZE, render, summarize  # noqa: E402
from app.nrb.sitemap import SitemapError, discover  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=None,
        help="skip root probing and start from this sitemap "
             "(still host-checked against NRB_SITE_BASE_URL)",
    )
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    parser.add_argument(
        "--urls",
        action="store_true",
        help="emit one TSV row per URL (url, section, page_kind, department, "
             "resource_type, lastmod, source_sitemap) instead of the summary",
    )
    parser.add_argument(
        "--sample", type=int, default=SAMPLE_SIZE,
        help=f"unclassified URLs to show (default {SAMPLE_SIZE})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show progress logs")
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,   # keeps stdout clean for `--json` / `--urls` redirection
    )

    try:
        inventory = await discover(args.root)
    except SitemapError as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 2

    if args.urls:
        for entry in sorted(inventory.urls, key=lambda e: e.normalized_url):
            print("\t".join((
                entry.url,
                entry.section,
                entry.page_kind,
                entry.department or "",
                entry.resource_type,
                entry.last_modified or "",
                entry.source_sitemap,
            )))
    else:
        summary = summarize(inventory, sample_size=args.sample)
        print(json.dumps(summary, indent=2, ensure_ascii=False) if args.json
              else render(summary))

    if inventory.truncated or inventory.errors:
        print(
            "NOTE: the inventory is incomplete "
            f"(truncated={inventory.truncated or 'no'}, errors={len(inventory.errors)})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

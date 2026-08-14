#!/usr/bin/env python
"""Developer-facing NRB document inventory (Phase 3 discovery).

Enumerates Nepal Rastra Bank's document posts through its WordPress REST API,
normalizes each into an `NRBDocument`, and reports what Phase 4 would be building
on: how many documents have a discoverable file, what kind of file, where it is
hosted, and how often NRB's own metadata determines the document type.

Read-only. Nothing is written to Postgres, no attachment is downloaded, no text is
extracted, nothing is embedded, and no tool is exposed to the model.

WHY REST AND NOT A PAGE CRAWL
    The brief asked for a page-level HTML crawl. Measuring the site first said
    otherwise: 104 of 110 sampled post URLs answer **302 straight to the file**,
    the HTML that does exist carries **no dates at all**, and REST returns the
    same corpus with `acf.document_file` (url + authoritative `mime_type` +
    filesize), real `date`/`modified`, and category ids — in ~190 requests
    instead of 18,567 against a central bank's website. `--verify` keeps the page
    crawl in its honest role: checking that the redirect really lands on the file
    REST claims. See `docs/nrb-integration.md` §7.

USAGE
    # bounded by default: 200 documents, mixed across owners
    .venv/bin/python scripts/nrb_document_inventory.py

    .venv/bin/python scripts/nrb_document_inventory.py --owner bfr --limit 50
    .venv/bin/python scripts/nrb_document_inventory.py --sample 100 --seed 7
    .venv/bin/python scripts/nrb_document_inventory.py --json > inventory.json
    .venv/bin/python scripts/nrb_document_inventory.py --urls > documents.tsv

    # check 25 post URLs really redirect to the file REST named
    .venv/bin/python scripts/nrb_document_inventory.py --limit 300 --verify 25

    # iterate on extraction without re-fetching
    .venv/bin/python scripts/nrb_document_inventory.py --all --save-raw raw.json
    .venv/bin/python scripts/nrb_document_inventory.py --from-raw raw.json --json

    # the full corpus — never the default
    .venv/bin/python scripts/nrb_document_inventory.py --all

    # cross-check REST enumeration against the Phase 2 sitemap inventory
    .venv/bin/python scripts/nrb_sitemap_inventory.py --urls > urls.tsv
    .venv/bin/python scripts/nrb_document_inventory.py --all --url-list urls.tsv

`--all` is required for a full crawl: an accidental invocation must not walk
18,567 documents. Requests are paced by `NRB_CRAWL_DELAY_SECONDS` and issued
sequentially — deterministic aggregation, and gentle on the source.

Exit codes: 0 clean, 1 if anything failed or a bound truncated the run, 2 if
discovery could not start.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.nrb import wp_api  # noqa: E402
from app.nrb.attachments import comparison_key  # noqa: E402
from app.nrb.classify import DEPARTMENT_CODES  # noqa: E402
# The corpus scope lives in `discovery` so this inventory and the Phase 4 sync
# cannot disagree about what the corpus IS — a scope difference between them
# would show up as a sync bug rather than as the config change it really is.
from app.nrb.discovery import CONTENT_POST_TYPES  # noqa: E402
from app.nrb.documents import Taxonomy, build_document  # noqa: E402
from app.nrb.http import open_client  # noqa: E402
from app.nrb.page import open_page_client, probe_page  # noqa: E402
from app.nrb.report import SAMPLE_SIZE, render_documents, summarize_documents  # noqa: E402

DEFAULT_LIMIT = 200   # bounded by default; --all opts into the whole corpus


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NRB document discovery inventory (read-only)."
    )
    scope = parser.add_argument_group("scope (bounded unless --all)")
    scope.add_argument(
        "--all", action="store_true",
        help="enumerate every document post (~18.5k). Required for a full crawl.",
    )
    scope.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"maximum documents to collect (default {DEFAULT_LIMIT}; ignored with --all)",
    )
    scope.add_argument(
        "--owner", action="append", default=None, metavar="CODE",
        help="restrict to this owner/post type; repeatable (e.g. --owner bfr --owner psd)",
    )
    scope.add_argument(
        "--offset", type=int, default=0, metavar="N",
        help="skip N REST pages (100 posts each) per post type",
    )
    scope.add_argument(
        "--sample", type=int, default=None, metavar="N",
        help="after collecting, keep a random sample of N documents (see --seed)",
    )
    scope.add_argument(
        "--seed", type=int, default=0,
        help="seed for --sample; fixed so a sampled run is reproducible (default 0)",
    )
    scope.add_argument(
        "--verify", type=int, default=0, metavar="N",
        help="additionally probe N post URLs to confirm they redirect to the file "
             "REST named (bounded, off by default)",
    )

    source = parser.add_argument_group("source / output")
    source.add_argument(
        "--url-list", metavar="TSV",
        help="a `nrb_sitemap_inventory.py --urls` file; documents not present in it "
             "are reported as a REST/sitemap discrepancy",
    )
    source.add_argument("--save-raw", metavar="FILE", help="write the raw REST posts to FILE")
    source.add_argument(
        "--from-raw", metavar="FILE",
        help="re-run extraction against a saved FILE instead of the network",
    )
    source.add_argument("--json", action="store_true", help="emit the summary as JSON")
    source.add_argument(
        "--urls", action="store_true",
        help="emit one TSV row per document instead of the summary",
    )
    source.add_argument(
        "--sample-size", type=int, default=SAMPLE_SIZE,
        help=f"examples to show per category in the report (default {SAMPLE_SIZE})",
    )
    source.add_argument("-v", "--verbose", action="store_true", help="show progress logs")
    return parser.parse_args(argv)


def _post_types(requested: list[str] | None) -> list[str]:
    """The post types to enumerate, deterministically ordered."""
    if requested:
        return sorted({code.strip() for code in requested if code.strip()})
    return sorted(DEPARTMENT_CODES) + sorted(CONTENT_POST_TYPES)


async def _collect(
    args: argparse.Namespace,
) -> tuple[list[dict], list, list[str], list[str]]:
    """Raw REST posts, errors, truncation notes, and REST-invisible post types."""
    posts: list[dict] = []
    errors: list = []
    truncated: list[str] = []
    remaining = None if args.all else max(0, args.limit)

    types = _post_types(args.owner)

    client = open_client(
        wp_api.USER_AGENT, accept="application/json",
        connect_timeout=wp_api.CONNECT_TIMEOUT, read_timeout=wp_api.READ_TIMEOUT,
    )
    unavailable: list[str] = []
    try:
        # Ask REST which post types it actually serves. Measured: `economic-review`
        # (49 URLs) and `er-article` (147) appear in the sitemap but are NOT among
        # the 46 REST-registered types, so they 404. That is a corpus gap Phase 4
        # must know about, not a transport error to bury in the failure count.
        type_result = await wp_api.fetch_post_types(client)
        served = {info.rest_base for info in type_result.items}
        if served:
            unavailable = [name for name in types if name not in served]
            if unavailable:
                logging.warning(
                    "NRB documents: %d post types are not served by REST: %s",
                    len(unavailable), ", ".join(unavailable),
                )
            types = [name for name in types if name in served]
        else:
            errors.extend(type_result.errors)

        # A bounded run must SAMPLE the corpus, not just read the top of it.
        # Without a per-type share, --limit 600 returned 600 bfr documents and
        # told us nothing about the other 34 owners. Rounded up so small owners
        # are not excluded, then still capped by `remaining` overall.
        per_type = None if remaining is None else max(1, -(-remaining // max(1, len(types))))
        for post_type in types:
            if remaining is not None and remaining <= 0:
                truncated.append(f"--limit={args.limit}")
                break
            result = await wp_api.fetch_posts(
                post_type, client=client,
                max_items=None if remaining is None else min(per_type, remaining),
                offset_pages=args.offset,
            )
            errors.extend(result.errors)
            truncated.extend(result.truncated)
            fetched = [item for item in result.items if isinstance(item, dict)]
            posts.extend(fetched)
            if remaining is not None:
                remaining -= len(fetched)
            logging.info(
                "NRB documents: %s -> %d posts (total reported %s)",
                post_type, len(fetched), result.total_reported,
            )
    finally:
        await client.aclose()
    return posts, errors, sorted(set(truncated)), unavailable


async def _verify(documents: list, count: int) -> list:
    """Probe up to `count` post URLs and record whether the redirect matches REST.

    Chosen from the front of the (deterministically ordered) document list rather
    than at random so two runs probe the same URLs and can be diffed.
    """
    delay = get_settings().nrb_crawl_delay_seconds
    probes = []
    targets = [d for d in documents if d.url][:count]
    client = open_page_client()
    try:
        for document in targets:
            probe = await probe_page(client, document.url)
            expected = document.attachments[0].url if document.attachments else None
            # Compared in `comparison_key` form: the 302 Location percent-encodes
            # the Devanagari that REST returns literally, so a raw string compare
            # reports that equivalence as two phantom disagreements per 30 probes.
            # Attached rather than stored on PageProbe: what REST claimed is the
            # inventory's business, not the fetcher's.
            setattr(probe, "expected_attachment",
                    comparison_key(expected) if expected else None)
            if probe.final_url:
                probe.final_url = comparison_key(probe.final_url)
            probes.append(probe)
            if delay:
                await asyncio.sleep(delay)
    finally:
        await client.aclose()
    return probes


def _load_url_list(path: str) -> set[str]:
    """URLs from a `--urls` TSV produced by the sitemap inventory."""
    urls: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for row in csv.reader(handle, delimiter="\t"):
            if row and row[0].startswith("http"):
                urls.add(row[0])
    return urls


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,   # keeps stdout clean for --json / --urls redirection
    )

    if args.from_raw:
        with open(args.from_raw, encoding="utf-8") as handle:
            saved = json.load(handle)
        posts = saved.get("posts", saved)
        categories_raw = saved.get("categories", []) if isinstance(saved, dict) else []
        taxonomy = Taxonomy([wp_api.Category(**c) for c in categories_raw])
        errors, truncated, unavailable = [], [], []
    else:
        category_result = await wp_api.fetch_categories()
        if category_result.errors and not category_result.items:
            print(f"could not read the NRB category taxonomy: "
                  f"{category_result.errors[0]}", file=sys.stderr)
            return 2
        taxonomy = Taxonomy(category_result.items)
        logging.info("NRB documents: taxonomy has %d categories", len(taxonomy))
        posts, errors, truncated, unavailable = await _collect(args)
        errors.extend(category_result.errors)
        categories_raw = [vars(c) for c in category_result.items]

    if args.sample is not None and len(posts) > args.sample:
        rng = random.Random(args.seed)
        posts = rng.sample(posts, args.sample)
        truncated.append(f"--sample={args.sample} (seed {args.seed})")

    if args.save_raw:
        with open(args.save_raw, "w", encoding="utf-8") as handle:
            json.dump({"posts": posts, "categories": categories_raw}, handle,
                      ensure_ascii=False)
        logging.info("NRB documents: wrote %d raw posts to %s", len(posts), args.save_raw)

    # Deterministic order: two runs over the same corpus produce the same report.
    documents = sorted(
        (build_document(post, taxonomy=taxonomy) for post in posts),
        key=lambda d: (d.url, d.post_id or 0),
    )

    if args.url_list:
        known = _load_url_list(args.url_list)
        missing = [d.url for d in documents if d.url and d.url not in known]
        if missing:
            print(f"NOTE: {len(missing)} REST documents are not in the sitemap list "
                  f"(e.g. {missing[0]})", file=sys.stderr)

    probes = await _verify(documents, args.verify) if args.verify else []

    if args.urls:
        for document in documents:
            first = document.attachments[0] if document.attachments else None
            print("\t".join((
                document.url,
                document.primary_section,
                document.owner or "",
                document.published or "",
                str(document.attachment_count),
                first.url if first else "",
                first.resource_type if first else "",
                first.mime_type if first and first.mime_type else "",
                (document.title or "").replace("\t", " "),
            )))
    else:
        summary = summarize_documents(
            documents, attempted=len(posts), errors=errors, probes=probes,
            rest_unavailable=unavailable, sample_size=args.sample_size,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False) if args.json
              else render_documents(summary))

    if errors or truncated:
        print(
            f"NOTE: run is incomplete (failures={len(errors)}, "
            f"truncated={truncated or 'no'})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

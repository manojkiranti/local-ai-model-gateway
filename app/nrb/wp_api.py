"""Reader for Nepal Rastra Bank's WordPress REST API — the Phase 3 document source.

Phase 2 concluded that NRB's sitemap names the *owner* of every document and the
*kind* of almost none, and that the WordPress REST API was unavailable. **The
second half of that was wrong**, and finding out changed this phase's design:
`/wp-json/` really is disabled, but NRB moved the REST prefix to **`/api/`**
(advertised by every page as `<link rel='https://api.w.org/' href='…/api/'>`).
The API is fully open and read-only.

What that buys, measured live on 2026-08-13 over 2,786 sampled posts:

  * **Attachments are a data field, not a scraped link.** Each document post
    carries `acf.document_file`, a WordPress attachment object with `url`,
    `filename`, `filesize` and — the part HTML cannot give — an authoritative
    `mime_type`. `acf.secondary_file` holds a second one when present (2.5%).
    This is also *why* a post URL 302s to a PDF: `acf.document_redirect_to_file`.
  * **Real dates.** `date`/`date_gmt`/`modified` are exposed here. The rendered
    HTML page publishes **no** date metadata at all (no `article:published_time`,
    no JSON-LD), so the page is strictly the poorer source.
  * **Category ids**, which resolve through `/categories` to the very taxonomy
    Phase 2 already mapped — so document type comes from NRB's own metadata
    rather than from guessing at a Devanagari slug.
  * **~190 requests instead of 18,567.** `per_page` maxes at 100 (101 is a
    `rest_invalid_param` 400) and `X-WP-Total`/`X-WP-TotalPages` are returned, so
    the whole corpus is enumerable in batches. Politeness to a central bank's
    site is a design constraint, not a nicety.

Cross-check that this is the same corpus Phase 2 saw: REST totals per owner match
the sitemap's document-post counts exactly (bfr 5,398 · pdm 3,582 · red 2,297 ·
ofg 2,294 · gsd 949 · fxm 542 · hrm 536 · psd 431 · fmd 382 · fiu 206 · mfd 199).

Trust model is unchanged and shared with `sitemap.py` via `http.py`: the host is
`NRB_SITE_BASE_URL`, the `/api/wp/v2` prefix is hardcoded here, redirects are not
followed, responses are capped, and nothing in this module is reachable from the
model. This is not `fetch_url`.

Failures are returned as `FetchError`, never raised: an inventory over thousands
of posts must survive one bad response, and *how* it failed is the finding.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from .http import (
    HTTP_STATUS,
    MALFORMED_BODY,
    NETWORK,
    REJECTED_HOST,
    REJECTED_REDIRECT,
    TIMEOUT,
    TOO_LARGE,
    UNEXPECTED_CONTENT_TYPE,
    FetchError,
    check_url,
    open_client,
    read_capped,
)

logger = logging.getLogger("app.nrb.wp_api")

# Hardcoded like `client.py`'s `/rates`: a site fact, not a tuning knob. NRB's
# REST prefix is `/api/`, NOT the WordPress default `/wp-json/` — see the
# module docstring.
REST_PREFIX = "/api/wp/v2"

PER_PAGE = 100                    # the API's maximum; 101 is a 400
MAX_PAGES = 400                   # backstop: 40,000 posts of one type
MAX_RESPONSE_BYTES = 20_000_000   # a 100-post page with content runs ~1.2 MB
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 60.0               # 100 posts with rendered content is a big body
USER_AGENT = "local-ai-gateway/1.0 (+nrb-document-discovery)"

__all__ = [
    "Category",
    "PostTypeInfo",
    "WPResult",
    "fetch_categories",
    "fetch_post_types",
    "fetch_posts",
    "rest_url",
]


def rest_url(path: str, params: dict[str, Any] | None = None) -> str:
    """Build a REST URL from config. The host is never an argument."""
    base = get_settings().nrb_site_base_url.rstrip("/")
    query = f"?{urlencode(params)}" if params else ""
    return f"{base}{REST_PREFIX}/{path.lstrip('/')}{query}"


@dataclass(frozen=True)
class Category:
    """One WordPress category. `parent` is 0 at the top of the tree."""

    id: int
    slug: str
    name: str
    parent: int
    count: int


@dataclass(frozen=True)
class PostTypeInfo:
    """A registered post type and the REST collection that serves it."""

    name: str
    rest_base: str
    label: str
    total: int | None = None


@dataclass
class WPResult:
    """Items plus the failures encountered collecting them.

    Both halves matter: a run that returned 5,000 posts and 12 errors is a
    different fact from one that returned 5,000 cleanly, and a caller that can
    only see the items cannot tell them apart.
    """

    items: list[Any] = field(default_factory=list)
    errors: list[FetchError] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    total_reported: int | None = None   # from X-WP-Total, when the API sent it


# --------------------------------------------------------------------------- #
# One request
# --------------------------------------------------------------------------- #
async def _get_json(
    client: httpx.AsyncClient, url: str
) -> tuple[Any | None, dict[str, str], FetchError | None]:
    """GET one REST URL. Returns `(decoded, headers, error)`.

    Every distinguishable failure mode gets its own `FetchError.kind` so the
    inventory report can aggregate them — "12 timeouts" and "12 rejected hosts"
    demand completely different responses.
    """
    if (why := check_url(url, require_https=True)) is not None:
        return None, {}, FetchError(REJECTED_HOST, why, url)
    try:
        async with client.stream("GET", url) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                return None, headers, FetchError(
                    REJECTED_REDIRECT, f"redirect to {location!r}", url, resp.status_code
                )
            if resp.status_code >= 400:
                return None, headers, FetchError(
                    HTTP_STATUS, f"HTTP {resp.status_code}", url, resp.status_code
                )
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and "json" not in content_type:
                # NRB answers an unknown REST route with a ~100 KB HTML 404 page,
                # so this is the guard that stops us parsing a theme template.
                return None, headers, FetchError(
                    UNEXPECTED_CONTENT_TYPE, f"content-type {content_type!r}", url,
                    resp.status_code,
                )
            body = await read_capped(resp, MAX_RESPONSE_BYTES)
            if body is None:
                return None, headers, FetchError(
                    TOO_LARGE, f"response exceeded {MAX_RESPONSE_BYTES} bytes", url
                )
    except httpx.TimeoutException:
        return None, {}, FetchError(TIMEOUT, "timed out", url)
    except httpx.HTTPError as exc:
        return None, {}, FetchError(NETWORK, f"transport error ({type(exc).__name__})", url)

    try:
        return json.loads(body), headers, None
    except ValueError as exc:
        return None, headers, FetchError(MALFORMED_BODY, f"undecodable JSON ({exc})", url)


# --------------------------------------------------------------------------- #
# Paged collections
# --------------------------------------------------------------------------- #
async def _fetch_collection(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    max_items: int | None = None,
    offset_pages: int = 0,
    delay: float | None = None,
) -> WPResult:
    """Walk a paged REST collection.

    Stops on the first page that errors rather than pressing on: a mid-collection
    failure means the enumeration has a hole, and continuing would produce a
    result that looks complete. The hole is recorded in `errors` either way.

    `delay` paces requests. It defaults to the configured crawl delay because the
    polite default must be the one you get by forgetting to pass it.
    """
    result = WPResult()
    pause = get_settings().nrb_crawl_delay_seconds if delay is None else delay
    page = 1 + offset_pages
    seen_pages = 0

    while True:
        if seen_pages >= MAX_PAGES:
            result.truncated.append(f"MAX_PAGES={MAX_PAGES}")
            logger.warning("NRB REST: %s stopped at MAX_PAGES=%d", path, MAX_PAGES)
            break
        query = {"per_page": PER_PAGE, "page": page, **(params or {})}
        url = rest_url(path, query)
        payload, headers, error = await _get_json(client, url)
        if error is not None:
            # A page past the end is a 400 `rest_post_invalid_page_number`, which
            # is a normal terminator rather than a fault — only report it when we
            # have not already collected the count the API promised.
            exhausted = (
                error.status == 400
                and result.total_reported is not None
                and len(result.items) >= result.total_reported
            )
            if not exhausted:
                result.errors.append(error)
                logger.warning("NRB REST: %s page %d — %s", path, page, error)
            break
        seen_pages += 1
        if result.total_reported is None and "x-wp-total" in headers:
            try:
                result.total_reported = int(headers["x-wp-total"])
            except ValueError:
                pass
        if not isinstance(payload, list):
            result.errors.append(
                FetchError(MALFORMED_BODY, "collection was not a JSON array", url)
            )
            break
        if not payload:
            break
        result.items.extend(payload)
        if max_items is not None and len(result.items) >= max_items:
            del result.items[max_items:]
            break
        if len(payload) < PER_PAGE:
            break
        page += 1
        if pause:
            await asyncio.sleep(pause)

    return result


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
async def fetch_categories(client: httpx.AsyncClient | None = None) -> WPResult:
    """Every category, as `Category` records. NRB publishes 284.

    The full taxonomy is fetched rather than looked up per post: 3 requests
    against 18,567 lookups, and the parent chain has to be resolvable anyway.
    """
    own = client is None
    client = client or open_client(
        USER_AGENT, accept="application/json",
        connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
    )
    try:
        raw = await _fetch_collection(client, "categories")
    finally:
        if own:
            await client.aclose()

    out = WPResult(errors=raw.errors, truncated=raw.truncated,
                   total_reported=raw.total_reported)
    for item in raw.items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        out.items.append(
            Category(
                id=item["id"],
                slug=str(item.get("slug") or ""),
                name=str(item.get("name") or ""),
                parent=item["parent"] if isinstance(item.get("parent"), int) else 0,
                count=item["count"] if isinstance(item.get("count"), int) else 0,
            )
        )
    return out


async def fetch_post_types(client: httpx.AsyncClient | None = None) -> WPResult:
    """Registered post types and their REST bases.

    `/types` is an object keyed by post-type name, not an array, so it is fetched
    directly rather than through `_fetch_collection`.
    """
    own = client is None
    client = client or open_client(
        USER_AGENT, accept="application/json",
        connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
    )
    url = rest_url("types")
    try:
        payload, _headers, error = await _get_json(client, url)
    finally:
        if own:
            await client.aclose()

    result = WPResult()
    if error is not None:
        result.errors.append(error)
        return result
    if not isinstance(payload, dict):
        result.errors.append(FetchError(MALFORMED_BODY, "/types was not an object", url))
        return result
    for name, info in sorted(payload.items()):
        if not isinstance(info, dict):
            continue
        rest_base = info.get("rest_base")
        if not isinstance(rest_base, str) or not rest_base:
            continue
        result.items.append(
            PostTypeInfo(name=name, rest_base=rest_base, label=str(info.get("name") or name))
        )
    return result


async def fetch_posts(
    rest_base: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_items: int | None = None,
    offset_pages: int = 0,
    delay: float | None = None,
) -> WPResult:
    """Raw post dicts for one post type, oldest-page-first as the API orders them.

    Returned raw rather than normalized here: `documents.build_document` is pure
    and testable precisely because this module hands it exactly what NRB sent.
    """
    own = client is None
    client = client or open_client(
        USER_AGENT, accept="application/json",
        connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
    )
    try:
        return await _fetch_collection(
            client, rest_base, max_items=max_items,
            offset_pages=offset_pages, delay=delay,
        )
    finally:
        if own:
            await client.aclose()

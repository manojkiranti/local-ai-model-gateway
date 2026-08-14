"""Discovery of Nepal Rastra Bank's published sitemap — transport + XML parsing.

Phase 2 of the NRB integration (`docs/nrb-integration.md`) is an **inventory**:
find out what NRB publishes and how it is organised, so the persistent source
model, the incremental sync and the ingestion rules of Phases 3–5 can be designed
against the real site instead of a guess. Nothing here writes to the database,
downloads a document, or is exposed to the model — there is no `SPEC`, and
`LOCAL_TOOLS` is untouched.

Trust model, inherited from `client.py`: the host is application config
(`NRB_SITE_BASE_URL`), never an argument. Every URL we fetch — including a child
sitemap NRB itself pointed us at — is re-checked against that host, so a
compromised or misconfigured sitemap cannot walk us off site. This is
deliberately NOT `fetch_url` with an XML parser: `fetch_url` exists to reach
arbitrary public hosts under SSRF guards, and none of its guards are reused,
relaxed or duplicated here.

What the live site actually looks like (probed 2026-08-13) — the facts the bounds
below are sized against:

  * `/sitemap.xml` **301s** to `/sitemap_index.xml`; the index is Yoast's.
  * The index lists **59** child sitemaps, all `urlset` — depth 2, no nesting.
  * Paged post types split at 1000 URLs each (`bfr-sitemap1..6`), so the URL
    total is tens of thousands: a small `MAX_URLS` would silently truncate the
    inventory, which is why exceeding it is reported, not just logged.
  * Every `<url>` carries a `<lastmod>`. Publication chronology is therefore
    available from the sitemap alone — Phase 3 will need it for the
    directive/amendment ordering, so it is retained verbatim.
  * **No `.pdf` (or any attachment) URL appears anywhere**, so extension-based
    resource typing is a no-op against NRB today. It is implemented anyway
    because it is three lines and the alternative is mis-reading a future sitemap
    as all-HTML. (Phase 3 found where the attachments actually are: the WordPress
    REST API's `acf.document_file`, which is also what the post URL 302s to. See
    `wp_api.py`.)
  * A 404 returns a ~100 KB HTML error page, so "is this XML" has to be decided
    on the parsed root element, not on the status code alone.

XML safety: stdlib `ElementTree` does not resolve *external* entities, but it
does expand internal ones, so a 1 KB sitemap can still be a billion-laughs bomb
that a byte cap cannot catch. `parse_sitemap` refuses any document containing a
doctype or entity declaration before handing it to the parser. Real sitemaps have
neither. No new dependency (`defusedxml`) is pulled in for this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx

from ..config import get_settings
from .classify import DiscoveredURL, classify_url
# The host guard and URL normalization live in `http.py` so that every NRB
# integration shares one trust boundary; they are re-exported here because they
# were part of this module's surface first.
from .http import (
    TRACKING_PARAMS,
    allowed_host,
    check_url,
    normalize_url,
    open_client,
    read_capped,
)

logger = logging.getLogger("app.nrb.sitemap")

# Probed in order. The first is the real root; `/sitemap.xml` is kept because it
# is the conventional location and 301s to the real one, so a future NRB CMS
# change that drops the Yoast-specific name still resolves.
ROOT_CANDIDATE_PATHS = ("/sitemap_index.xml", "/sitemap.xml")

MAX_DEPTH = 3            # index -> child -> grandchild. NRB uses 2.
MAX_SITEMAPS = 300       # NRB publishes 59.
MAX_URLS = 200_000       # NRB is in the tens of thousands.
MAX_RESPONSE_BYTES = 10_000_000   # largest observed child sitemap: ~166 KB.
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0      # sitemaps are bigger than a rates page
USER_AGENT = "local-ai-gateway/1.0 (+nrb-sitemap-discovery)"

__all__ = [
    "SitemapError",
    "SitemapRef",
    "SitemapDoc",
    "Inventory",
    "allowed_host",
    "check_url",
    "normalize_url",
    "parse_sitemap",
    "discover",
]


class SitemapError(Exception):
    """A failure fetching or understanding a sitemap.

    Distinct from `NRBError` on purpose: `NRBError.message` is contracted to be
    safe to hand straight to the model, and nothing here is model-facing. These
    messages are for a developer reading the inventory report.
    """


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SitemapRef:
    """A `<sitemap>` entry from a `<sitemapindex>`."""

    loc: str
    lastmod: str | None = None


@dataclass(frozen=True)
class SitemapDoc:
    """One parsed sitemap document.

    Exactly one of `sitemaps` / `urls` is populated, per `kind`.
    """

    kind: str                                # "sitemapindex" | "urlset"
    sitemaps: tuple[SitemapRef, ...] = ()
    urls: tuple[SitemapRef, ...] = ()        # same shape: a loc plus a lastmod


@dataclass
class Inventory:
    """The result of one discovery run. Pure data — nothing is persisted."""

    root: str
    urls: list[DiscoveredURL] = field(default_factory=list)   # deduplicated
    sitemaps_fetched: list[str] = field(default_factory=list)
    sitemap_lastmods: dict[str, str] = field(default_factory=dict)
    total_entries: int = 0                   # before deduplication
    duplicates: int = 0
    # Things a reviewer needs to see rather than have swallowed by a log line.
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (url, why)
    errors: list[tuple[str, str]] = field(default_factory=list)     # (url, why)
    truncated: list[str] = field(default_factory=list)              # bound names

    @property
    def unique_urls(self) -> int:
        return len(self.urls)


# --------------------------------------------------------------------------- #
# XML parsing (pure)
# --------------------------------------------------------------------------- #
def _localname(tag: str) -> str:
    """`{http://…/sitemap/0.9}urlset` -> `urlset`.

    Matching on the local name rather than a hardcoded namespace URI: Yoast, the
    sitemaps.org schema and hand-written sitemaps all differ in prefix and
    occasionally in the URI, and none of that changes the meaning of the element.
    """
    return tag.rpartition("}")[2]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _localname(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def parse_sitemap(text: str) -> SitemapDoc:
    """Parse one sitemap document. Raises SitemapError on anything unusable.

    Entries with no `<loc>` are skipped rather than failing the document: one
    malformed row in a 1000-row sitemap should not cost us the other 999.
    """
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        # See the module docstring: ElementTree expands internal entities, so
        # this is the entity-expansion (billion laughs) guard. A byte cap on the
        # response cannot substitute for it.
        raise SitemapError("sitemap contains a doctype or entity declaration")

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise SitemapError(f"malformed XML ({exc})") from exc

    kind = _localname(root.tag)
    if kind == "sitemapindex":
        refs = [
            SitemapRef(loc, _child_text(entry, "lastmod"))
            for entry in root
            if _localname(entry.tag) == "sitemap"
            and (loc := _child_text(entry, "loc"))
        ]
        return SitemapDoc(kind="sitemapindex", sitemaps=tuple(refs))
    if kind == "urlset":
        refs = [
            SitemapRef(loc, _child_text(entry, "lastmod"))
            for entry in root
            if _localname(entry.tag) == "url"
            and (loc := _child_text(entry, "loc"))
        ]
        return SitemapDoc(kind="urlset", urls=tuple(refs))
    raise SitemapError(f"root element is <{kind}>, not a sitemap")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None, str | None]:
    """GET one URL under a byte cap.

    Returns `(text, redirect_location, error)` with exactly one of the three set.
    A redirect is returned rather than followed so the caller decides — only the
    root probe is allowed to act on one.
    """
    if (why := check_url(url, require_https=True)) is not None:
        return None, None, why
    try:
        async with client.stream("GET", url) as resp:
            if resp.is_redirect:
                return None, resp.headers.get("location", ""), None
            if resp.status_code >= 400:
                return None, None, f"HTTP {resp.status_code}"
            body = await read_capped(resp, MAX_RESPONSE_BYTES)
            if body is None:
                return None, None, f"response exceeded {MAX_RESPONSE_BYTES} bytes"
    except httpx.TimeoutException:
        return None, None, "timed out"
    except httpx.HTTPError as exc:
        return None, None, f"transport error ({type(exc).__name__})"
    return body.decode("utf-8", errors="replace"), None, None


async def _probe_root(client: httpx.AsyncClient, inventory_errors: list[tuple[str, str]]) -> tuple[str, str]:
    """Find the published sitemap root. Returns `(url, text)`.

    Tries the candidate paths in order and follows at most ONE redirect per
    candidate, only to a URL that passes the host guard. That single hop is not
    an exception to the no-redirects rule so much as the reason the rule exists:
    `/sitemap.xml` really does 301 to `/sitemap_index.xml`, and we would rather
    honour that one documented hop than hardcode the Yoast filename as the only
    way in.
    """
    base = get_settings().nrb_site_base_url.rstrip("/")
    for path in ROOT_CANDIDATE_PATHS:
        url = f"{base}{path}"
        text, location, error = await _fetch(client, url)
        if location:
            target = urljoin(url, location)
            if (why := check_url(target, require_https=True)) is not None:
                inventory_errors.append((url, f"redirect refused: {why}"))
                continue
            logger.info("NRB sitemap: %s redirects to %s", url, target)
            text, location, error = await _fetch(client, target)
            if location:
                inventory_errors.append((target, "redirect chain longer than one hop"))
                continue
            url = target
        if error:
            inventory_errors.append((url, error))
            continue
        if text is not None:
            return url, text
    raise SitemapError(
        "no sitemap found at " + ", ".join(ROOT_CANDIDATE_PATHS) + f" on {base}"
    )


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #
async def discover(root: str | None = None) -> Inventory:
    """Walk NRB's sitemap tree and return a classified, deduplicated inventory.

    Sequential on purpose: this is a manually-run report against a central bank's
    public site, and ~60 polite requests are cheaper than explaining a burst.

    Every bound (depth, sitemap count, URL count) records itself in
    `inventory.truncated` when it bites. A silently truncated inventory would be
    read as "this is everything NRB publishes", which is exactly the conclusion
    Phase 3's design must not be built on.
    """
    inventory = Inventory(root=root or "")
    seen_sitemaps: set[str] = set()
    by_normalized: dict[str, DiscoveredURL] = {}

    async with open_client(USER_AGENT, accept="application/xml, text/xml",
                           connect_timeout=CONNECT_TIMEOUT,
                           read_timeout=READ_TIMEOUT) as client:
        if root is None:
            root, text = await _probe_root(client, inventory.errors)
        else:
            if (why := check_url(root, require_https=True)) is not None:
                raise SitemapError(f"refusing to fetch {root}: {why}")
            text, location, error = await _fetch(client, root)
            if location:
                raise SitemapError(f"refusing to follow a redirect from {root}")
            if error or text is None:
                raise SitemapError(f"could not fetch {root}: {error}")
        inventory.root = root
        logger.info("NRB sitemap: root is %s", root)

        # (url, depth, pre-fetched text or None)
        queue: list[tuple[str, int, str | None]] = [(root, 0, text)]
        url_cap_reached = False
        while queue and not url_cap_reached:
            url, depth, body = queue.pop(0)
            normalized_sitemap = normalize_url(url)
            if normalized_sitemap in seen_sitemaps:
                # A duplicate <sitemap> entry costs a wasted fetch, not a loop —
                # but skip it so counts stay honest.
                logger.info("NRB sitemap: already seen %s", url)
                continue
            seen_sitemaps.add(normalized_sitemap)

            if len(inventory.sitemaps_fetched) >= MAX_SITEMAPS:
                inventory.truncated.append(f"MAX_SITEMAPS={MAX_SITEMAPS}")
                logger.warning("NRB sitemap: stopped at MAX_SITEMAPS=%d", MAX_SITEMAPS)
                break

            if body is None:
                body, location, error = await _fetch(client, url)
                if location:
                    inventory.rejected.append((url, "unexpected redirect"))
                    logger.warning("NRB sitemap: unexpected redirect from %s", url)
                    continue
                if error or body is None:
                    inventory.errors.append((url, error or "empty response"))
                    logger.warning("NRB sitemap: %s — %s", url, error)
                    continue

            try:
                doc = parse_sitemap(body)
            except SitemapError as exc:
                inventory.errors.append((url, str(exc)))
                logger.warning("NRB sitemap: %s — %s", url, exc)
                continue

            inventory.sitemaps_fetched.append(url)

            if doc.kind == "sitemapindex":
                if depth + 1 >= MAX_DEPTH:
                    inventory.truncated.append(f"MAX_DEPTH={MAX_DEPTH}")
                    logger.warning(
                        "NRB sitemap: %s nests past MAX_DEPTH=%d", url, MAX_DEPTH
                    )
                    continue
                for ref in doc.sitemaps:
                    if (why := check_url(ref.loc, require_https=True)) is not None:
                        inventory.rejected.append((ref.loc, why))
                        logger.warning(
                            "NRB sitemap: refusing child sitemap %s (%s)", ref.loc, why
                        )
                        continue
                    if ref.lastmod:
                        inventory.sitemap_lastmods[ref.loc] = ref.lastmod
                    queue.append((ref.loc, depth + 1, None))
                logger.info(
                    "NRB sitemap: index %s lists %d child sitemaps",
                    url, len(doc.sitemaps),
                )
                continue

            # A urlset. Classify and deduplicate, but never fetch these.
            kept = 0
            for ref in doc.urls:
                if (why := check_url(ref.loc)) is not None:
                    inventory.rejected.append((ref.loc, why))
                    continue
                inventory.total_entries += 1
                normalized = normalize_url(ref.loc)
                if normalized in by_normalized:
                    inventory.duplicates += 1
                    continue
                if len(by_normalized) >= MAX_URLS:
                    # Stop the whole walk, not just this sitemap: once the cap is
                    # hit nothing further can be recorded, so fetching the
                    # remaining ~50 sitemaps would only add load.
                    inventory.truncated.append(f"MAX_URLS={MAX_URLS}")
                    logger.warning("NRB sitemap: stopped at MAX_URLS=%d", MAX_URLS)
                    url_cap_reached = True
                    break
                by_normalized[normalized] = classify_url(
                    url=ref.loc,
                    normalized_url=normalized,
                    source_sitemap=url,
                    last_modified=ref.lastmod,
                )
                kept += 1
            # One line per sitemap, never one per URL.
            logger.info("NRB sitemap: %s — %d URLs (%d new)", url, len(doc.urls), kept)

    inventory.urls = list(by_normalized.values())
    logger.info(
        "NRB sitemap: %d sitemaps, %d entries, %d unique",
        len(inventory.sitemaps_fetched), inventory.total_entries, len(inventory.urls),
    )
    return inventory

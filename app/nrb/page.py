"""Bounded probe of an NRB post URL — the verification path, not the data path.

Phase 3's plan was to crawl 18,567 post pages and scrape their attachment links.
Measuring the live site made that the wrong mechanism, and this module is what is
left of it after the evidence came in:

  * **95% of document posts are not HTML pages.** Of 110 sampled post URLs, 104
    answered **302** straight to the file (97 PDF, 4 xlsx, 3 jpg) and only 6
    returned a 200 HTML page. The redirect is `acf.document_redirect_to_file`.
  * **The HTML that does exist has less metadata than the REST record**: no
    `article:published_time`, no JSON-LD, no dates of any kind. Compare
    `wp_api.py`, which returns `date`, `modified` and an authoritative
    `mime_type`.
  * A page-per-post crawl is ~18,567 requests against a central bank's site where
    REST needs ~190 for the same corpus, and strictly more of it.

So the primary source is `wp_api.py`, and this module's job is to *check* it: does
the post URL really redirect to the file that `acf.document_file` claims, and do
the page's `article:section`, breadcrumb and title agree with what we derived?
That is a real question about trustworthiness, answerable on a bounded sample, and
it is the only reason to fetch a page at all in this phase.

It also earns one thing REST does not give: the breadcrumb spells out the owner
code (`fmd` → "Financial Management Departments"), which is how `owner_label` gets
populated from NRB's own words instead of a guess.

Security: the shared `http.py` guards. Redirects are followed here — that is the
point — but at most `MAX_REDIRECTS` hops, each destination re-checked against the
host guard before it is requested, and a hop off-host ends the probe with
`rejected_redirect` rather than being chased. HTML is parsed with the stdlib
`html.parser` (see `attachments.py` for why not beautifulsoup).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

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
    normalize_url,
    open_client,
    read_capped,
)

logger = logging.getLogger("app.nrb.page")

MAX_REDIRECTS = 3            # live posts use exactly one
MAX_RESPONSE_BYTES = 5_000_000   # an NRB post page is ~115 KB
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
USER_AGENT = "local-ai-gateway/1.0 (+nrb-document-discovery)"

# Outcomes. `redirect_to_file` is the common case, which is why it is an outcome
# rather than an error.
OUTCOME_HTML = "html"
OUTCOME_REDIRECT_TO_FILE = "redirect_to_file"
OUTCOME_ERROR = "error"

__all__ = [
    "PageMeta",
    "PageProbe",
    "OUTCOME_HTML",
    "OUTCOME_REDIRECT_TO_FILE",
    "OUTCOME_ERROR",
    "parse_page_meta",
    "probe_page",
]


@dataclass(frozen=True)
class PageMeta:
    """Deterministic metadata a post page exposes. Every field may be None.

    Only explicit structured values are read — `<link rel=canonical>`, OpenGraph /
    `article:*` meta, the theme's single `.main-title`, and the Yoast breadcrumb.
    Nothing is taken from visual position or from prose.
    """

    canonical_url: str | None = None
    og_title: str | None = None
    title_tag: str | None = None
    main_title: str | None = None
    article_section: str | None = None    # WordPress category, as Yoast emits it
    og_description: str | None = None
    breadcrumbs: tuple[str, ...] = ()     # ["Home", "<owner label>", "<title>"]

    @property
    def owner_label(self) -> str | None:
        """The breadcrumb's middle entry — NRB's own name for the owner.

        `Home » Financial Management Departments » Ashwin 2079` → the department
        name. Returned only for the exact three-entry shape every sampled page
        used; anything else is not this pattern and gets None rather than a guess.
        """
        if len(self.breadcrumbs) == 3 and self.breadcrumbs[0].lower() == "home":
            return self.breadcrumbs[1] or None
        return None


@dataclass
class PageProbe:
    """One post URL probed. `outcome` says which of the three cases happened."""

    url: str
    outcome: str
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    meta: PageMeta | None = None
    error: FetchError | None = None

    @property
    def ok(self) -> bool:
        return self.outcome != OUTCOME_ERROR


# --------------------------------------------------------------------------- #
# HTML metadata (pure, stdlib)
# --------------------------------------------------------------------------- #
class _MetaParser(HTMLParser):
    """Pull the handful of deterministic fields out of an NRB post page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.title_tag: str | None = None
        self.main_title: str | None = None
        self.breadcrumbs: list[str] = []
        self._in_title = False
        self._title: list[str] = []
        # Depth counters: the theme nests plain <div>s inside both regions, so a
        # naive "until the next </div>" would stop at the wrong tag.
        self._main_depth: int | None = None
        self._main_text: list[str] = []
        self._crumb_depth: int | None = None
        self._crumb_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rels = attributes.get("rel", "").lower().split()
            if "canonical" in rels and self.canonical is None:
                self.canonical = attributes.get("href", "").strip() or None
        elif tag == "title" and self.title_tag is None:
            self._in_title = True
        elif tag == "div":
            classes = attributes.get("class", "").split()
            if self._main_depth is not None:
                self._main_depth += 1
            elif "main-title" in classes and self.main_title is None:
                self._main_depth = 0
            if self._crumb_depth is not None:
                self._crumb_depth += 1
            elif "breadcrumb" in classes and not self.breadcrumbs:
                self._crumb_depth = 0

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title_tag = " ".join("".join(self._title).split()) or None
        elif tag == "div":
            if self._main_depth is not None:
                if self._main_depth == 0:
                    self.main_title = " ".join("".join(self._main_text).split()) or None
                    self._main_depth = None
                else:
                    self._main_depth -= 1
            if self._crumb_depth is not None:
                if self._crumb_depth == 0:
                    # Yoast separates entries with '»' inside nested spans, so the
                    # flattened text splits cleanly on it.
                    raw = " ".join("".join(self._crumb_text).split())
                    self.breadcrumbs = [
                        part.strip() for part in raw.split("»") if part.strip()
                    ]
                    self._crumb_depth = None
                else:
                    self._crumb_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        if self._main_depth is not None:
            self._main_text.append(data)
        if self._crumb_depth is not None:
            self._crumb_text.append(data)

    def close(self) -> None:
        """Flush regions left open by truncated markup.

        A page cut off mid-`<title>` still has a usable title, and losing it
        because the document ended is a parser bug, not tolerance.
        """
        super().close()
        if self._in_title and self.title_tag is None:
            self._in_title = False
            self.title_tag = " ".join("".join(self._title).split()) or None
        if self._main_depth is not None and self.main_title is None:
            self.main_title = " ".join("".join(self._main_text).split()) or None
            self._main_depth = None


def _strip_site_suffix(title: str | None, site_name: str | None) -> str | None:
    """`"Ashwin 2079 - the official site of…"` → `"Ashwin 2079"`.

    Only the exact `" - <og:site_name>"` suffix the page itself declares is
    removed. Without a declared site name nothing is stripped — guessing at a
    separator would truncate a real title containing a dash.
    """
    if not title:
        return None
    if site_name:
        suffix = f" - {site_name}"
        if title.endswith(suffix):
            return title[: -len(suffix)].strip() or None
    return title


def parse_page_meta(html: str) -> PageMeta:
    """Extract deterministic metadata from post-page HTML. Never raises."""
    parser = _MetaParser()
    parser.feed(html)
    parser.close()
    site_name = parser.meta.get("og:site_name")
    return PageMeta(
        canonical_url=parser.canonical,
        og_title=_strip_site_suffix(parser.meta.get("og:title"), site_name),
        title_tag=_strip_site_suffix(parser.title_tag, site_name),
        main_title=parser.main_title,
        article_section=parser.meta.get("article:section"),
        og_description=parser.meta.get("og:description"),
        breadcrumbs=tuple(parser.breadcrumbs),
    )


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
async def probe_page(client: httpx.AsyncClient, url: str) -> PageProbe:
    """Fetch one post URL, following NRB's own redirect at most MAX_REDIRECTS hops.

    Three outcomes, all returned rather than raised:

      * `redirect_to_file` — the chain left the HTML world (any non-HTML
        content-type). `final_url` is the attachment, which is the fact worth
        checking against `acf.document_file.url`.
      * `html` — a real page; `meta` is populated.
      * `error` — `error.kind` says which failure, from `http.py`'s vocabulary.
    """
    probe = PageProbe(url=url, outcome=OUTCOME_ERROR)
    current = url

    for hop in range(MAX_REDIRECTS + 1):
        if (why := check_url(current, require_https=True)) is not None:
            # Reached only for a redirect destination: the first URL is checked by
            # the caller too, but re-checking every hop is the actual guarantee.
            kind = REJECTED_REDIRECT if hop else REJECTED_HOST
            probe.error = FetchError(kind, why, current)
            return probe
        try:
            async with client.stream("GET", current) as resp:
                probe.status = resp.status_code
                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                probe.content_type = content_type or None

                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        probe.error = FetchError(
                            MALFORMED_BODY, "redirect with no Location", current,
                            resp.status_code,
                        )
                        return probe
                    # Relative Locations are legal and NRB has used them.
                    target = urljoin(current, location)
                    probe.redirect_chain.append(target)
                    current = target
                    continue

                if resp.status_code >= 400:
                    probe.error = FetchError(
                        HTTP_STATUS, f"HTTP {resp.status_code}", current, resp.status_code
                    )
                    return probe

                probe.final_url = normalize_url(current)

                if content_type and content_type not in ("text/html", "application/xhtml+xml"):
                    # The 95% case: the post *is* its attachment. Not an error, and
                    # deliberately not parsed — Phase 3 downloads nothing.
                    probe.outcome = OUTCOME_REDIRECT_TO_FILE
                    return probe

                body = await read_capped(resp, MAX_RESPONSE_BYTES)
                if body is None:
                    probe.error = FetchError(
                        TOO_LARGE, f"response exceeded {MAX_RESPONSE_BYTES} bytes", current
                    )
                    return probe
        except httpx.TimeoutException:
            probe.error = FetchError(TIMEOUT, "timed out", current)
            return probe
        except httpx.HTTPError as exc:
            probe.error = FetchError(
                NETWORK, f"transport error ({type(exc).__name__})", current
            )
            return probe

        if not content_type:
            # No content-type at all: refuse to guess whether it is markup.
            probe.error = FetchError(
                UNEXPECTED_CONTENT_TYPE, "response declared no content-type", current,
                probe.status,
            )
            return probe

        text = body.decode("utf-8", errors="replace")
        probe.meta = parse_page_meta(text)
        probe.outcome = OUTCOME_HTML
        return probe

    probe.error = FetchError(
        REJECTED_REDIRECT, f"more than {MAX_REDIRECTS} redirects", url, probe.status
    )
    return probe


def open_page_client() -> httpx.AsyncClient:
    """A client for page probing. Redirects are handled by `probe_page`, not httpx."""
    return open_client(
        USER_AGENT, accept="text/html,application/xhtml+xml",
        connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT,
    )

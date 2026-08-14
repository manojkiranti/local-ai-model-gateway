"""Shared HTTP guards and result types for every NRB integration.

There is exactly ONE trust boundary for NRB access and it lives here, so the
Forex client, the sitemap walker, the WordPress REST reader and the page probe
cannot drift apart on what counts as an acceptable URL. `sitemap.py` re-exports
these names, which is why Phase 2's public surface is unchanged.

The rules, unchanged from Phase 1/2:

  * the host is application config (`NRB_SITE_BASE_URL`), never an argument;
  * that host exactly — no subdomains, no userinfo;
  * https for anything we fetch;
  * a byte cap on every response;
  * finite timeouts, no retries;
  * redirects are not followed by default, and where a caller does follow one the
    destination is re-checked here rather than trusted.

`FetchError` exists because an inventory run over thousands of URLs must not be
aborted by one bad response, and "it failed" is not a useful report: the whole
point of a discovery phase is knowing *how* things fail. `kind` is a small closed
vocabulary so failures aggregate; `detail` is for a human reading the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..config import get_settings

__all__ = [
    "FetchError",
    "NETWORK", "TIMEOUT", "REJECTED_HOST", "REJECTED_REDIRECT", "HTTP_STATUS",
    "TOO_LARGE", "UNEXPECTED_CONTENT_TYPE", "MALFORMED_BODY", "FETCH_KINDS",
    "TRACKING_PARAMS",
    "allowed_host",
    "check_url",
    "normalize_url",
    "open_client",
    "read_capped",
]

# The closed vocabulary of failure kinds. Report aggregation keys off these, so a
# new kind means updating the report's ordering too.
NETWORK = "network"
TIMEOUT = "timeout"
REJECTED_HOST = "rejected_host"
REJECTED_REDIRECT = "rejected_redirect"
HTTP_STATUS = "http_status"
TOO_LARGE = "too_large"
UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
MALFORMED_BODY = "malformed_body"

FETCH_KINDS = (
    NETWORK, TIMEOUT, REJECTED_HOST, REJECTED_REDIRECT, HTTP_STATUS,
    TOO_LARGE, UNEXPECTED_CONTENT_TYPE, MALFORMED_BODY,
)


@dataclass(frozen=True)
class FetchError:
    """A structured, aggregatable failure. Never raised across a crawl."""

    kind: str
    detail: str
    url: str
    status: int | None = None

    def __str__(self) -> str:  # what the report prints
        code = f" [{self.status}]" if self.status is not None else ""
        return f"{self.kind}{code}: {self.detail}"


# Dropped during normalization. Every other query parameter is KEPT — on a
# WordPress site a query string can select a genuinely different resource, and a
# download URL may legitimately need one.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga",
    }
)


class _HostError(Exception):
    """Raised only by `allowed_host` when configuration is unusable."""


def allowed_host() -> str:
    """The single host every NRB integration trusts, derived from config."""
    host = urlsplit(get_settings().nrb_site_base_url).hostname
    if not host:
        raise _HostError("NRB_SITE_BASE_URL does not contain a hostname.")
    return host.lower()


def check_url(url: str, *, require_https: bool = False) -> str | None:
    """None if `url` is an acceptable NRB URL, else a short reason it is not.

    `require_https=True` for anything we will actually fetch. Without it an
    `http://` URL is *inventoried and reported* rather than silently dropped —
    we want to know if NRB publishes one.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "unparseable URL"
    if parts.scheme.lower() not in ("http", "https"):
        return f"scheme {parts.scheme!r} is not http/https"
    if require_https and parts.scheme.lower() != "https":
        return "refusing to fetch over plain http"
    # `https://www.nrb.org.np@evil.example/` parses with hostname evil.example, so
    # the host check below already catches it — but userinfo has no legitimate
    # place in an NRB URL, and naming it makes the rejection readable.
    if parts.username or parts.password:
        return "URL carries userinfo"
    host = (parts.hostname or "").lower()
    if not host:
        return "URL has no host"
    if host != allowed_host():
        # Subdomains are NOT auto-trusted. If NRB legitimately starts publishing
        # under one, this surfaces in the report so widening is a decision.
        return f"host {host!r} is not {allowed_host()!r}"
    return None


def normalize_url(url: str) -> str:
    """A canonical form for deduplication. Conservative by design.

    Applied: lowercase scheme and host, drop a default port, drop the fragment,
    drop known tracking parameters. NOT applied: adding or removing trailing
    slashes, re-casing percent-escapes (NRB emits lowercase triplets for its
    Devanagari slugs, and rewriting them changes the string we would later
    fetch), or reordering the query.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if (port is None or default) else f"{host}:{port}"

    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and pair.split("=", 1)[0].lower() not in TRACKING_PARAMS
    )
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def open_client(
    user_agent: str,
    *,
    accept: str,
    connect_timeout: float = 5.0,
    read_timeout: float = 30.0,
) -> httpx.AsyncClient:
    """An NRB client with redirects OFF — where we go stays our decision.

    Callers that need to observe a redirect read `response.is_redirect` and
    re-check the destination with `check_url`.
    """
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
        headers={"User-Agent": user_agent, "Accept": accept},
    )


async def read_capped(response: httpx.Response, max_bytes: int) -> bytes | None:
    """Stream a body, returning None the moment it exceeds `max_bytes`.

    Streamed rather than `response.content` so an unexpectedly huge body is
    abandoned mid-flight instead of being buffered first.
    """
    body = b""
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > max_bytes:
            return None
    return body

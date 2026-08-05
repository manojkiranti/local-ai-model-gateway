"""Local tool: fetch_url (SSRF-guarded outbound HTTP GET).

Lets the model read PUBLIC web pages/APIs. Security is central: the gateway sits
next to Postgres, Ollama and the MCP server (all on localhost) and, on a cloud
host, the metadata endpoint (169.254.169.254) — so a naive fetcher is an SSRF
hole. Defenses, always on:

  * scheme allowlist (http/https only)
  * every resolved IP must be PUBLIC — loopback/private/link-local/reserved/
    multicast/unspecified are refused (this is what blocks localhost:11434,
    :5432, :3333 and cloud metadata)
  * redirects are followed manually (max 3), re-checking each hop's host
  * GET only, 10s timeout, ~2 MB download cap, output truncated

Known residual risk: a resolve-then-connect window allows theoretical DNS
rebinding. Acceptable for a single-tenant internal gateway; IP-pinning is future
hardening. Optional host allowlist (FETCH_URL_ALLOWLIST) tightens this further.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ...config import get_settings
from .base import LocalToolSpec

ALLOWED_SCHEMES = ("http", "https")
TIMEOUT_SECONDS = 10.0
MAX_BYTES = 2_000_000       # hard cap on bytes downloaded
MAX_TEXT_CHARS = 15_000     # cap on text handed back to the model
MAX_REDIRECTS = 3
_TEXTY_HINTS = ("text/", "json", "xml", "javascript", "csv")


class _Blocked(Exception):
    """A target refused by the security policy (surfaced as a friendly ERROR)."""


@dataclass
class _Resp:
    final_url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool  # True if the download hit MAX_BYTES


# --------------------------------------------------------------------------- #
# Security helpers (pure / DNS only — no HTTP)
# --------------------------------------------------------------------------- #
def _ip_is_public(ip_str: str) -> bool:
    """True only for a routable public address; everything internal is False."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # unwrap ::ffff:127.0.0.1 style addresses
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _host_allowed(host: str, allowed: list[str]) -> bool:
    """Empty allowlist -> any host. Otherwise host must equal, or be a subdomain
    of, an allowlisted domain."""
    if not allowed:
        return True
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed)


def _check_target(url: str, allowed_hosts: list[str]) -> None:
    """Validate scheme + host + that EVERY resolved IP is public. Raises _Blocked."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise _Blocked(f"only http/https URLs are allowed (got '{parts.scheme or 'none'}')")
    host = parts.hostname
    if not host:
        raise _Blocked("URL has no host")
    if not _host_allowed(host, allowed_hosts):
        raise _Blocked(f"host '{host}' is not in the allowlist")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise _Blocked(f"could not resolve host '{host}' ({exc.strerror or exc})")
    # Refuse if ANY resolved address is non-public (defends against a name that
    # resolves to a mix, and against literal-IP obfuscation like decimal IPs).
    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise _Blocked(f"host '{host}' resolves to a non-public address ({ip})")


# --------------------------------------------------------------------------- #
# HTML -> readable text
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "noscript", "svg"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # collapse runs of whitespace but keep paragraph breaks
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML shouldn't crash the tool
        pass
    return parser.text()


def _looks_texty(content_type: str) -> bool:
    ct = content_type.lower()
    return any(hint in ct for hint in _TEXTY_HINTS)


# --------------------------------------------------------------------------- #
# Download (httpx) + formatting
# --------------------------------------------------------------------------- #
async def _download(url: str) -> _Resp:
    """GET with manual, re-validated redirects and a byte cap. Raises _Blocked
    for policy violations; lets httpx errors propagate to the caller."""
    allowed_hosts = get_settings().fetch_url_allowed_hosts
    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=TIMEOUT_SECONDS,
        headers={"User-Agent": "local-ai-gateway/1.0 (+fetch_url)"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _check_target(current, allowed_hosts)  # re-checked on every hop
            async with client.stream("GET", current) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
                body = b""
                truncated = False
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        body = body[:MAX_BYTES]
                        truncated = True
                        break
                return _Resp(
                    final_url=str(resp.url),
                    status=resp.status_code,
                    content_type=resp.headers.get("content-type", ""),
                    body=body,
                    truncated=truncated,
                )
    raise _Blocked(f"too many redirects (>{MAX_REDIRECTS})")


def _format_result(resp: _Resp) -> str:
    size = len(resp.body)
    header = (
        f"Fetched {resp.final_url} (HTTP {resp.status}, "
        f"{resp.content_type or 'unknown type'}, {size} bytes"
        f"{', capped' if resp.truncated else ''})"
    )
    if not _looks_texty(resp.content_type):
        return f"{header}\n\n[non-text content ({resp.content_type or 'unknown'}); body not shown]"

    text = resp.body.decode("utf-8", errors="replace")
    if "html" in resp.content_type.lower():
        text = _html_to_text(text)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip() + "\n…[truncated]"
    return f"{header}\n\n{text}"


async def _fetch_url(args: dict[str, Any]) -> str:
    settings = get_settings()
    if not getattr(settings, "fetch_url_enabled", True):
        return "ERROR: the fetch_url tool is disabled by server configuration."

    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return "ERROR: 'url' is required and must be a non-empty http(s) URL."
    url = url.strip()

    try:
        resp = await _download(url)
    except _Blocked as exc:
        return f"ERROR: refused to fetch this URL ({exc})."
    except httpx.TimeoutException:
        return f"ERROR: request timed out after {int(TIMEOUT_SECONDS)}s."
    except httpx.HTTPError as exc:
        return f"ERROR: could not fetch the URL ({type(exc).__name__}: {exc})."

    return _format_result(resp)


SPEC = LocalToolSpec(
    name="fetch_url",
    description=(
        "Fetch a public web page or API over HTTP(S) and return its text content "
        "(HTML is reduced to readable text). Use this to look up current "
        "information from a specific URL. GET only. Internal/private addresses "
        "are blocked; responses are size-limited and may be truncated. Provide "
        "the full 'url' (must start with http:// or https://)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full public URL to fetch, e.g. 'https://example.com/page'.",
            },
        },
        "required": ["url"],
    },
    func=_fetch_url,
)

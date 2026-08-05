"""Offline tests for the fetch_url local tool.

The security-critical parts (SSRF IP filtering, scheme/host allowlisting, HTML
extraction, truncation, binary handling) are all pure/deterministic and tested
without network. The happy-path fetch monkeypatches the low-level downloader so
no real request is made. A live network smoke test is done manually.
"""

import asyncio

import pytest

from app.tools.local import fetch_url


def _run(url):
    return asyncio.run(fetch_url.SPEC.func({"url": url}))


# ---- SSRF: internal / non-public targets must be blocked (no network) ----

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",              # loopback
        "http://localhost:11434/",         # loopback name (Ollama)
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://10.1.2.3/",                # private
        "http://192.168.0.1/",             # private
        "http://172.16.5.4/",              # private
        "http://[::1]/",                   # IPv6 loopback
        "http://0.0.0.0/",                 # unspecified
    ],
)
def test_internal_targets_blocked(url):
    result = _run(url)
    assert result.startswith("ERROR"), f"{url} should be blocked, got: {result!r}"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "data:text/plain,hello",
        "not-a-url",
        "http://",       # no host
    ],
)
def test_bad_scheme_or_url_blocked(url):
    assert _run(url).startswith("ERROR")


def test_missing_url():
    assert asyncio.run(fetch_url.SPEC.func({})).startswith("ERROR")


# ---- pure helpers ----

@pytest.mark.parametrize(
    "ip,public",
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("10.0.0.1", False),
        ("192.168.1.1", False),
        ("172.16.0.1", False),
        ("::1", False),
        ("0.0.0.0", False),
        ("fc00::1", False),   # unique-local
        ("fe80::1", False),   # link-local
    ],
)
def test_ip_is_public(ip, public):
    assert fetch_url._ip_is_public(ip) is public


def test_host_allowed_matches_domain_and_subdomains():
    allowed = ["example.com", "api.test.org"]
    assert fetch_url._host_allowed("example.com", allowed)
    assert fetch_url._host_allowed("www.example.com", allowed)   # subdomain
    assert fetch_url._host_allowed("api.test.org", allowed)
    assert not fetch_url._host_allowed("evil.com", allowed)
    assert not fetch_url._host_allowed("notexample.com", allowed)
    # empty allowlist = allow anything (IP filter still applies elsewhere)
    assert fetch_url._host_allowed("anything.com", [])


def test_html_to_text_strips_tags_and_scripts():
    html = (
        "<html><head><style>.x{color:red}</style>"
        "<script>alert('x')</script></head>"
        "<body><h1>Title</h1><p>Hello <b>world</b>.</p></body></html>"
    )
    text = fetch_url._html_to_text(html)
    assert "Title" in text and "Hello" in text and "world" in text
    assert "alert" not in text and "color:red" not in text
    assert "<" not in text


def test_format_result_extracts_html_and_reports_status():
    resp = fetch_url._Resp(
        final_url="https://example.com/",
        status=200,
        content_type="text/html; charset=utf-8",
        body=b"<html><body><h1>Hi</h1><p>Body text</p></body></html>",
        truncated=False,
    )
    out = fetch_url._format_result(resp)
    assert "200" in out and "example.com" in out
    assert "Hi" in out and "Body text" in out


def test_format_result_binary_gives_metadata_note_not_bytes():
    resp = fetch_url._Resp(
        final_url="https://example.com/img.png",
        status=200,
        content_type="image/png",
        body=b"\x89PNG\r\n\x1a\n\x00\x00",
        truncated=False,
    )
    out = fetch_url._format_result(resp)
    assert "image/png" in out
    assert "PNG" not in out or "\x89" not in out  # raw bytes not dumped


def test_long_text_is_truncated():
    big = "A" * (fetch_url.MAX_TEXT_CHARS + 5000)
    resp = fetch_url._Resp(
        final_url="https://example.com/",
        status=200,
        content_type="text/plain",
        body=big.encode(),
        truncated=False,
    )
    out = fetch_url._format_result(resp)
    assert "[truncated]" in out
    assert len(out) < len(big)


# ---- happy path with the network layer monkeypatched ----

def test_successful_fetch_returns_readable_text(monkeypatch):
    async def fake_download(url):
        return fetch_url._Resp(
            final_url="https://example.com/",
            status=200,
            content_type="text/html",
            body=b"<html><body><p>Fetched content here</p></body></html>",
            truncated=False,
        )

    monkeypatch.setattr(fetch_url, "_download", fake_download)
    result = _run("https://example.com/")
    assert not result.startswith("ERROR")
    assert "Fetched content here" in result


def test_disabled_via_config(monkeypatch):
    class _S:
        fetch_url_enabled = False
        fetch_url_allowed_hosts = []

    monkeypatch.setattr(fetch_url, "get_settings", lambda: _S())
    assert _run("https://example.com/").startswith("ERROR")


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "fetch_url" for spec in LOCAL_TOOLS)

"""Unit tests for NRB sitemap discovery, classification and reporting.

No test here touches the network: every `httpx.AsyncClient` the discovery opens is
routed through a `MockTransport`, the same pattern `test_nrb_forex.py` uses. The
live probe is `scripts/nrb_sitemap_inventory.py`, which is a report, not a test.

The XML fixtures are shaped like NRB's real output (Yoast: an `xml-stylesheet`
processing instruction, tab indentation, a `lastmod` on every entry,
percent-encoded Devanagari slugs) because several of the behaviours under test —
namespace handling, the `/sitemap.xml` redirect, the department-code-in-first-path-
segment rule — only exist because of what the real site does.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.nrb import classify, report
from app.nrb import sitemap as sm

HOST = "https://www.nrb.org.np"

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
STYLESHEET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<?xml-stylesheet type="text/xsl" href="//www.nrb.org.np/main-sitemap.xsl"?>'
)


def _index(*entries: tuple[str, str | None]) -> str:
    rows = "".join(
        f"\t<sitemap>\n\t\t<loc>{loc}</loc>\n"
        + (f"\t\t<lastmod>{mod}</lastmod>\n" if mod else "")
        + "\t</sitemap>\n"
        for loc, mod in entries
    )
    return f"{STYLESHEET}\n<sitemapindex {NS}>\n{rows}</sitemapindex>"


def _urlset(*entries: tuple[str, str | None]) -> str:
    rows = "".join(
        f"\t<url>\n\t\t<loc>{loc}</loc>\n"
        + (f"\t\t<lastmod>{mod}</lastmod>\n" if mod else "")
        + "\t</url>\n"
        for loc, mod in entries
    )
    return f"{STYLESHEET}\n<urlset {NS}>\n{rows}</urlset>"


# --------------------------------------------------------------------------- #
# Parsing (pure)
# --------------------------------------------------------------------------- #
def test_parses_a_urlset_and_keeps_lastmod():
    """lastmod is the only publication chronology the sitemap gives us."""
    doc = sm.parse_sitemap(_urlset((f"{HOST}/bfr/x/", "2026-08-13T16:01:28+05:45")))
    assert doc.kind == "urlset"
    assert doc.urls == (sm.SitemapRef(f"{HOST}/bfr/x/", "2026-08-13T16:01:28+05:45"),)


def test_parses_a_sitemapindex_and_keeps_lastmod():
    doc = sm.parse_sitemap(_index((f"{HOST}/post-sitemap.xml", "2026-08-10T11:55:02+05:45")))
    assert doc.kind == "sitemapindex"
    assert doc.sitemaps[0].loc == f"{HOST}/post-sitemap.xml"
    assert doc.sitemaps[0].lastmod == "2026-08-10T11:55:02+05:45"


def test_namespace_prefix_and_uri_do_not_matter():
    """Matching is on the element's local name, not a hardcoded namespace URI."""
    xml = (
        '<sm:urlset xmlns:sm="http://example.invalid/other/ns">'
        f"<sm:url><sm:loc>{HOST}/a/</sm:loc>"
        "<sm:lastmod>2026-01-01</sm:lastmod></sm:url></sm:urlset>"
    )
    doc = sm.parse_sitemap(xml)
    assert doc.kind == "urlset" and doc.urls[0].loc == f"{HOST}/a/"


def test_no_namespace_at_all_still_parses():
    doc = sm.parse_sitemap(f"<urlset><url><loc>{HOST}/a/</loc></url></urlset>")
    assert doc.urls[0].loc == f"{HOST}/a/" and doc.urls[0].lastmod is None


def test_malformed_xml_raises():
    with pytest.raises(sm.SitemapError, match="malformed XML"):
        sm.parse_sitemap("<urlset><url><loc>oops")


def test_html_error_page_is_rejected_on_the_root_element():
    """NRB answers a 404 with a ~100 KB HTML page, so this is a real input."""
    with pytest.raises(sm.SitemapError, match="root element is <html>"):
        sm.parse_sitemap("<html><body>Not found</body></html>")


def test_empty_urlset_is_valid_and_empty():
    doc = sm.parse_sitemap(_urlset())
    assert doc.kind == "urlset" and doc.urls == ()


def test_entry_without_a_loc_is_skipped_not_fatal():
    """One bad row in a 1000-row sitemap must not cost the other 999."""
    xml = (
        f"<urlset {NS}><url><lastmod>2026-01-01</lastmod></url>"
        f"<url><loc>{HOST}/a/</loc></url></urlset>"
    )
    assert sm.parse_sitemap(xml).urls == (sm.SitemapRef(f"{HOST}/a/", None),)


@pytest.mark.parametrize(
    "xml",
    [
        '<!DOCTYPE urlset [<!ENTITY a "x">]><urlset><url><loc>u</loc></url></urlset>',
        '<!doctype foo><urlset/>',
    ],
)
def test_doctype_and_entity_declarations_are_refused(xml):
    """ElementTree expands internal entities, so a byte cap is not a bomb guard."""
    with pytest.raises(sm.SitemapError, match="doctype or entity"):
        sm.parse_sitemap(xml)


# --------------------------------------------------------------------------- #
# Host guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,fragment",
    [
        ("https://evil.example/sitemap.xml", "not 'www.nrb.org.np'"),
        ("https://nrb.org.np/sitemap.xml", "not 'www.nrb.org.np'"),      # bare domain
        ("https://docs.www.nrb.org.np/s.xml", "not 'www.nrb.org.np'"),   # subdomain
        ("https://www.nrb.org.np.evil.example/s.xml", "not 'www.nrb.org.np'"),
        ("ftp://www.nrb.org.np/s.xml", "not http/https"),
        ("javascript:alert(1)", "not http/https"),
        ("https://user:pw@www.nrb.org.np/s.xml", "userinfo"),
    ],
)
def test_check_url_rejects(url, fragment):
    reason = sm.check_url(url)
    assert reason is not None and fragment in reason


def test_userinfo_trick_host_is_rejected():
    """`https://www.nrb.org.np@evil.example/` really points at evil.example.

    The userinfo check fires first and is the one reported, but either rule alone
    rejects it: with the userinfo stripped, the host is still evil.example.
    """
    assert "userinfo" in (sm.check_url("https://www.nrb.org.np@evil.example/x") or "")
    assert "evil.example" in (sm.check_url("https://evil.example/x") or "")


def test_https_is_required_only_for_things_we_fetch():
    """An http loc is inventoried and reported, but never fetched."""
    assert sm.check_url(f"http://www.nrb.org.np/a/") is None
    assert "plain http" in (sm.check_url("http://www.nrb.org.np/a/", require_https=True) or "")


def test_a_good_url_passes():
    assert sm.check_url(f"{HOST}/bfr/x/", require_https=True) is None


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def test_fragment_is_dropped_and_scheme_host_lowercased():
    assert sm.normalize_url("HTTPS://WWW.NRB.ORG.NP/bfr/x/#section") == f"{HOST}/bfr/x/"


def test_default_port_is_dropped_but_a_real_one_is_kept():
    assert sm.normalize_url(f"{HOST}:443/a/") == f"{HOST}/a/"
    assert sm.normalize_url(f"{HOST}:8443/a/") == "https://www.nrb.org.np:8443/a/"


def test_tracking_params_are_dropped_and_real_ones_kept_in_order():
    """Conservative: only known trackers go. A WP query can select a real page."""
    url = f"{HOST}/search/?s=directive&utm_source=x&page=2&fbclid=y"
    assert sm.normalize_url(url) == f"{HOST}/search/?s=directive&page=2"


def test_trailing_slash_and_percent_encoding_are_left_alone():
    """Rewriting either would invent a URL NRB never published."""
    encoded = f"{HOST}/psd/%e0%a4%85-%e0%a4%aa%e0%a5%8d%e0%a4%b0%e0%a4%be/"
    assert sm.normalize_url(encoded) == encoded
    assert sm.normalize_url(f"{HOST}/bfr") == f"{HOST}/bfr"
    assert sm.normalize_url(f"{HOST}/bfr/") == f"{HOST}/bfr/"


# --------------------------------------------------------------------------- #
# Discovery over a mocked transport
# --------------------------------------------------------------------------- #
def _with_routes(monkeypatch, routes, *, log=None):
    """Serve `routes` (url -> httpx.Response | callable) to every AsyncClient."""
    real_init = httpx.AsyncClient.__init__

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if log is not None:
            log.append(url)
        entry = routes.get(url)
        if entry is None:
            return httpx.Response(404, text="<html>not found</html>")
        return entry(request) if callable(entry) else entry

    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _xml(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/xml"})


def test_discovers_the_root_and_walks_one_level(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/bfr-sitemap1.xml", "2026-08-13"))),
        f"{HOST}/bfr-sitemap1.xml": _xml(
            _urlset((f"{HOST}/bfr/", "2026-08-13"), (f"{HOST}/bfr/x/", "2026-08-01"))
        ),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert inv.root == f"{HOST}/sitemap_index.xml"
    assert inv.sitemaps_fetched == [f"{HOST}/sitemap_index.xml", f"{HOST}/bfr-sitemap1.xml"]
    assert inv.sitemap_lastmods[f"{HOST}/bfr-sitemap1.xml"] == "2026-08-13"
    assert inv.unique_urls == 2 and inv.duplicates == 0 and inv.truncated == []


def test_root_probe_follows_one_same_host_redirect(monkeypatch):
    """`/sitemap.xml` really 301s to `/sitemap_index.xml` on the live site."""
    routes = {
        f"{HOST}/sitemap_index.xml": httpx.Response(404, text="<html>gone</html>"),
        f"{HOST}/sitemap.xml": httpx.Response(
            301, headers={"location": f"{HOST}/real-sitemap.xml"}
        ),
        f"{HOST}/real-sitemap.xml": _xml(_urlset((f"{HOST}/a/", None))),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert inv.root == f"{HOST}/real-sitemap.xml" and inv.unique_urls == 1


def test_root_redirect_to_a_foreign_host_is_refused(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": httpx.Response(
            302, headers={"location": "https://evil.example/sitemap.xml"}
        ),
        f"{HOST}/sitemap.xml": _xml(_urlset((f"{HOST}/a/", None))),
    }
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    inv = asyncio.run(sm.discover())
    assert inv.root == f"{HOST}/sitemap.xml"
    assert "https://evil.example/sitemap.xml" not in log      # never requested
    assert any("redirect refused" in why for _, why in inv.errors)


def test_no_sitemap_anywhere_raises(monkeypatch):
    _with_routes(monkeypatch, {})
    with pytest.raises(sm.SitemapError, match="no sitemap found"):
        asyncio.run(sm.discover())


def test_child_sitemap_on_a_foreign_host_is_rejected_and_the_rest_still_walked(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(
            _index(
                ("https://evil.example/sitemap.xml", None),
                (f"{HOST}/ok-sitemap.xml", None),
            )
        ),
        f"{HOST}/ok-sitemap.xml": _xml(_urlset((f"{HOST}/a/", None))),
    }
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    inv = asyncio.run(sm.discover())
    assert "https://evil.example/sitemap.xml" not in log
    assert inv.rejected and "evil.example" in inv.rejected[0][1]
    assert inv.unique_urls == 1                                # the good one survived


def test_page_url_on_a_foreign_host_is_rejected_not_inventoried(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/s.xml", None))),
        f"{HOST}/s.xml": _xml(
            _urlset(("https://evil.example/x/", None), (f"{HOST}/a/", None))
        ),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert [u.url for u in inv.urls] == [f"{HOST}/a/"]
    assert inv.rejected and "evil.example" in inv.rejected[0][0]


def test_nested_sitemap_index_is_followed(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/mid.xml", None))),
        f"{HOST}/mid.xml": _xml(_index((f"{HOST}/leaf.xml", None))),
        f"{HOST}/leaf.xml": _xml(_urlset((f"{HOST}/a/", None))),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert len(inv.sitemaps_fetched) == 3 and inv.unique_urls == 1


def test_depth_bound_stops_the_walk_and_reports_it(monkeypatch):
    monkeypatch.setattr(sm, "MAX_DEPTH", 1)
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/child.xml", None))),
        f"{HOST}/child.xml": _xml(_urlset((f"{HOST}/a/", None))),
    }
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    inv = asyncio.run(sm.discover())
    assert f"{HOST}/child.xml" not in log
    assert inv.truncated == ["MAX_DEPTH=1"] and inv.unique_urls == 0


def test_sitemap_count_bound_stops_the_walk_and_reports_it(monkeypatch):
    monkeypatch.setattr(sm, "MAX_SITEMAPS", 2)
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(
            _index((f"{HOST}/a.xml", None), (f"{HOST}/b.xml", None))
        ),
        f"{HOST}/a.xml": _xml(_urlset((f"{HOST}/a/", None))),
        f"{HOST}/b.xml": _xml(_urlset((f"{HOST}/b/", None))),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert inv.truncated == ["MAX_SITEMAPS=2"]
    assert len(inv.sitemaps_fetched) == 2 and inv.unique_urls == 1


def test_url_count_bound_stops_everything_and_reports_it(monkeypatch):
    """A silently truncated inventory reads as 'this is the whole site'."""
    monkeypatch.setattr(sm, "MAX_URLS", 2)
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(
            _index((f"{HOST}/a.xml", None), (f"{HOST}/b.xml", None))
        ),
        f"{HOST}/a.xml": _xml(
            _urlset((f"{HOST}/1/", None), (f"{HOST}/2/", None), (f"{HOST}/3/", None))
        ),
        f"{HOST}/b.xml": _xml(_urlset((f"{HOST}/4/", None))),
    }
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    inv = asyncio.run(sm.discover())
    assert inv.unique_urls == 2
    assert inv.truncated == ["MAX_URLS=2"]
    assert f"{HOST}/b.xml" not in log       # the walk stopped, not just this sitemap


def test_oversized_response_is_capped_and_reported(monkeypatch):
    # Large enough for the small index, far too small for the 50-URL child, so the
    # cap is proven to bite mid-walk rather than on the very first request.
    monkeypatch.setattr(sm, "MAX_RESPONSE_BYTES", 500)
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/big.xml", None))),
        f"{HOST}/big.xml": _xml(_urlset(*[(f"{HOST}/{i}/", None) for i in range(50)])),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert any("exceeded 500 bytes" in why for _, why in inv.errors)
    assert inv.unique_urls == 0


def test_timeout_on_a_child_is_an_error_not_an_exception(monkeypatch):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    routes = {
        f"{HOST}/sitemap_index.xml": _xml(
            _index((f"{HOST}/slow.xml", None), (f"{HOST}/ok.xml", None))
        ),
        f"{HOST}/slow.xml": boom,
        f"{HOST}/ok.xml": _xml(_urlset((f"{HOST}/a/", None))),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert (f"{HOST}/slow.xml", "timed out") in inv.errors
    assert inv.unique_urls == 1


def test_transport_error_on_a_child_is_an_error_not_an_exception(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/dead.xml", None))),
        f"{HOST}/dead.xml": boom,
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert any("ConnectError" in why for _, why in inv.errors)


def test_http_error_on_a_child_is_recorded(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/gone.xml", None))),
        f"{HOST}/gone.xml": httpx.Response(500, text="boom"),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert (f"{HOST}/gone.xml", "HTTP 500") in inv.errors


def test_redirect_on_a_child_sitemap_is_not_followed(monkeypatch):
    """Only the root probe may act on a 3xx; anywhere else it is a change to notice."""
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/moved.xml", None))),
        f"{HOST}/moved.xml": httpx.Response(301, headers={"location": f"{HOST}/new.xml"}),
        f"{HOST}/new.xml": _xml(_urlset((f"{HOST}/a/", None))),
    }
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    inv = asyncio.run(sm.discover())
    assert f"{HOST}/new.xml" not in log
    assert (f"{HOST}/moved.xml", "unexpected redirect") in inv.rejected


def test_duplicate_urls_across_sitemaps_are_deduplicated(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(
            _index((f"{HOST}/a.xml", None), (f"{HOST}/b.xml", None))
        ),
        f"{HOST}/a.xml": _xml(_urlset((f"{HOST}/x/", "2026-01-01"))),
        f"{HOST}/b.xml": _xml(
            _urlset((f"{HOST}/x/#top", "2026-02-02"), (f"{HOST}/y/", None))
        ),
    }
    _with_routes(monkeypatch, routes)
    inv = asyncio.run(sm.discover())
    assert inv.total_entries == 3 and inv.unique_urls == 2 and inv.duplicates == 1
    # First writer wins, so the entry keeps the sitemap it was first seen in.
    first = next(u for u in inv.urls if u.normalized_url == f"{HOST}/x/")
    assert first.source_sitemap == f"{HOST}/a.xml"


def test_a_duplicate_sitemap_entry_is_fetched_once(monkeypatch):
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(
            _index((f"{HOST}/a.xml", None), (f"{HOST}/a.xml", None))
        ),
        f"{HOST}/a.xml": _xml(_urlset((f"{HOST}/x/", None))),
    }
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    inv = asyncio.run(sm.discover())
    assert log.count(f"{HOST}/a.xml") == 1
    assert inv.sitemaps_fetched.count(f"{HOST}/a.xml") == 1


def test_an_explicit_foreign_root_is_refused_before_any_request(monkeypatch):
    """The root is a developer argument, but it is still host-checked."""
    log: list[str] = []
    _with_routes(monkeypatch, {}, log=log)
    with pytest.raises(sm.SitemapError, match="refusing to fetch"):
        asyncio.run(sm.discover("https://evil.example/sitemap.xml"))
    assert log == []


def test_an_explicit_root_skips_probing(monkeypatch):
    routes = {f"{HOST}/bfr-sitemap1.xml": _xml(_urlset((f"{HOST}/bfr/x/", None)))}
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    inv = asyncio.run(sm.discover(f"{HOST}/bfr-sitemap1.xml"))
    assert log == [f"{HOST}/bfr-sitemap1.xml"] and inv.unique_urls == 1


def test_sitemap_urls_are_fetched_but_page_urls_are_not(monkeypatch):
    """This phase inventories pages; it never downloads one."""
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/a.xml", None))),
        f"{HOST}/a.xml": _xml(_urlset((f"{HOST}/bfr/x/", None))),
    }
    log: list[str] = []
    _with_routes(monkeypatch, routes, log=log)
    asyncio.run(sm.discover())
    assert log == [f"{HOST}/sitemap_index.xml", f"{HOST}/a.xml"]


# --------------------------------------------------------------------------- #
# Classification (pure). URL shapes are copied from the live sitemap.
# --------------------------------------------------------------------------- #
def _classify(path: str):
    url = f"{HOST}{path}"
    return classify.classify_url(
        url=url, normalized_url=url, source_sitemap=f"{HOST}/s.xml", last_modified=None
    )


@pytest.mark.parametrize(
    "path,section",
    [
        ("/category/unified-directives/", "directive"),
        ("/category/directives/", "directive"),
        ("/category/aml-cft-directives/", "directive"),
        ("/category/circulars/2082-83/", "circular"),
        ("/category/fxm-circulars/", "circular"),
        ("/category/monetary-policy/monetary-policy-english/", "monetary_policy"),
        ("/category/acts/", "act"),
        ("/category/laws-legislations/", "act"),
        ("/category/rules-by-laws/", "rule_bylaw"),
        ("/category/manual-guidelines/manuals/", "guideline_manual"),
        ("/category/notices/", "notice"),
        ("/category/public-notices/", "notice"),
        ("/category/annual-reports/annual-reports-english/", "report"),
        ("/category/monthly-statistics/", "statistics"),
        ("/category/nrb-working-paper/", "research"),
        ("/category/special-publications/", "publication"),
        ("/category/tenders/global-tenders/", "procurement"),
        ("/category/open-competition-syllabus/", "career"),
        ("/category/press-releases/", "media"),
        ("/category/enforcement-action/", "enforcement_action"),
        ("/category/list-of-bfis/", "license_registry"),
        ("/category/faqs/faq_fxm/faq_fxm-remittance/", "faq"),
        ("/category/fxm/", "forex"),
    ],
)
def test_category_sections(path, section):
    entry = _classify(path)
    assert entry.section == section
    assert entry.page_kind == "taxonomy_archive"
    assert entry.department is None      # a category is not owned by a department


def test_an_unmapped_category_is_other_and_names_itself_as_a_todo():
    entry = _classify("/category/some-brand-new-category/")
    assert entry.section == "other"
    assert entry.evidence == "unmapped category root 'some-brand-new-category'"


def test_a_reviewed_miscellaneous_category_is_other_but_not_a_todo():
    """Explicit `other` means 'looked at'; only absent categories are to-dos."""
    entry = _classify("/category/useful-links/")
    assert entry.section == "other"
    assert entry.evidence == "category root 'useful-links'"
    assert "unmapped" not in entry.evidence


@pytest.mark.parametrize(
    "path,section",
    [
        # `public-debt-operations-archive` mixes archived circulars, archived acts
        # and reports, so the parent alone would be wrong for most of its children.
        ("/category/public-debt-operations-archive/public-debt-circulars-archived/", "circular"),
        ("/category/public-debt-operations-archive/pdmd-acts-bylaws-archived/", "act"),
        ("/category/public-debt-operations-archive/reports-osgs/", "report"),
        ("/category/public-debt-operations-archive/treasury-bills/", "other"),
        ("/category/monetary-operations/repo-reverse-repo/", "monetary_operations"),
    ],
)
def test_two_segment_category_keys_beat_the_parent(path, section):
    assert _classify(path).section == section


def test_federal_office_posts_put_the_owner_in_the_second_segment():
    """385 live URLs: the office sitemaps publish /federal-offices/<code>/<slug>/."""
    entry = _classify("/federal-offices/brg/some-notice/")
    assert entry.department == "brg" and entry.page_kind == "document_post"
    assert entry.evidence == "office post type 'federal-offices'/'brg'"
    assert _classify("/federal-offices/brg/").page_kind == "post_type_archive"


def test_an_unknown_federal_office_code_is_retained_not_guessed():
    entry = _classify("/federal-offices/newtown/x/")
    assert entry.department is None and entry.section == "other"
    assert "unrecognised office code" in entry.evidence


@pytest.mark.parametrize(
    "path,section",
    [
        ("/database-on-nepalese-economy/real-sector/", "statistics"),
        ("/weighted-average-treasury-bills-rate/", "statistics"),
        ("/bank-list/", "license_registry"),
        ("/international-conference/2nd-international-conference-2015/", "research"),
        ("/faqs/", "faq"),
    ],
)
def test_known_standalone_pages(path, section):
    entry = _classify(path)
    assert entry.section == section and entry.page_kind == "page"


def test_percent_encoded_devanagari_category_matches():
    """NRB publishes Nepali category slugs; the table is written in Devanagari."""
    entry = _classify(
        "/category/%e0%a4%9a%e0%a5%8c%e0%a4%a5%e0%a5%8b-"
        "%e0%a4%97%e0%a5%8d%e0%a4%b0%e0%a4%be%e0%a4%ae%e0%a5%80%e0%a4%a3-"
        "%e0%a4%95%e0%a4%b0%e0%a5%8d%e0%a4%9c%e0%a4%be-"
        "%e0%a4%b8%e0%a4%b0%e0%a5%8d%e0%a4%b5%e0%a5%87/"
    )
    assert entry.section == "research"


def test_a_department_document_post_names_its_owner_but_not_its_section():
    """The single most important finding: the kind is NOT in the URL."""
    entry = _classify("/bfr/2008-05_mid_may/")
    assert entry.department == "bfr"
    assert entry.page_kind == "document_post"
    assert entry.section == "unknown"
    assert entry.evidence == "owner post type 'bfr'"


def test_a_post_type_root_is_an_archive_not_a_document():
    entry = _classify("/bfr/")
    assert entry.page_kind == "post_type_archive" and entry.department == "bfr"


def test_departments_page_carries_the_code():
    entry = _classify("/departments/dbs/")
    assert entry.page_kind == "department_page" and entry.department == "dbs"


def test_a_content_post_type_carries_a_section_and_no_department():
    """`/ticker/…` comes from ditty_news_ticker-sitemap.xml — filename != path."""
    entry = _classify("/ticker/covid-19-related-information/")
    assert entry.section == "media" and entry.department is None
    assert entry.page_kind == "document_post"


def test_economic_review_articles_are_research():
    assert _classify("/er-article/some-paper/").section == "research"


def test_dated_permalinks_are_news_posts():
    entry = _classify("/2019/12/from-the-governor/")
    assert entry.page_kind == "news_post" and entry.section == "unknown"
    assert entry.department is None


def test_keyword_and_tag_archives_are_taxonomy_pages():
    for path in ("/keyword/arima/", "/post_tag/inflation/"):
        entry = _classify(path)
        assert entry.page_kind == "taxonomy_archive" and entry.section == "other"


def test_known_pages_and_the_site_root():
    assert _classify("/forex/").section == "forex"
    assert _classify("/download-forex/").section == "forex"
    assert _classify("/").page_kind == "landing_page"


def test_an_unrecognised_path_root_is_retained_and_named():
    """Nothing is ever dropped for being unrecognised — that is the review list."""
    entry = _classify("/some-new-thing/page/")
    assert entry.page_kind == "page" and entry.section == "other"
    assert entry.evidence == "unrecognised path root 'some-new-thing'"


@pytest.mark.parametrize(
    "path,kind",
    [
        ("/contents/uploads/2026/08/directive.pdf", "pdf"),
        ("/contents/uploads/form.DOCX", "document"),
        ("/contents/uploads/old.doc", "document"),
        ("/contents/uploads/stats.xlsx", "spreadsheet"),
        ("/contents/uploads/data.csv", "spreadsheet"),
        ("/contents/uploads/bundle.zip", "archive"),
        ("/bfr/x/", "html"),
        ("/", "html"),
    ],
)
def test_resource_type_from_extension(path, kind):
    assert _classify(path).resource_type == kind


def test_classification_is_deterministic():
    assert _classify("/bfr/x/") == _classify("/bfr/x/")


def test_every_department_code_comes_from_the_published_sitemap_index():
    """Guards against inventing a code (there is no `fepd` on NRB's site)."""
    assert "fepd" not in classify.DEPARTMENT_CODES
    for code in ("bfr", "psd", "fxm", "ficpd", "red", "pdm", "dbs", "fiu"):
        assert code in classify.DEPARTMENT_CODES
    assert "category" not in classify.DEPARTMENT_CODES
    assert "ticker" not in classify.DEPARTMENT_CODES


def test_every_mapped_category_targets_a_declared_section():
    """A typo'd section name would silently vanish from the report ordering."""
    assert set(classify.CATEGORY_SECTIONS.values()) <= set(classify.SECTIONS)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _inventory(monkeypatch, extra_urls=()):
    urls = [
        (f"{HOST}/category/circulars/", None),
        (f"{HOST}/category/unified-directives/", None),
        (f"{HOST}/bfr/a/", "2020-01-01"),
        (f"{HOST}/bfr/b/", "2026-08-13"),
        (f"{HOST}/psd/c/", "2024-06-06"),
        (f"{HOST}/category/brand-new-thing/", None),
        (f"{HOST}/mystery-root/x/", None),
        *extra_urls,
    ]
    routes = {
        f"{HOST}/sitemap_index.xml": _xml(_index((f"{HOST}/a.xml", None))),
        f"{HOST}/a.xml": _xml(_urlset(*urls)),
    }
    _with_routes(monkeypatch, routes)
    return asyncio.run(sm.discover())


def test_summary_counts_are_deterministic_and_add_up(monkeypatch):
    inv = _inventory(monkeypatch)
    first = report.summarize(inv)
    assert first == report.summarize(inv)                  # stable, including order
    assert first["unique_urls"] == 7
    assert sum(first["by_section"].values()) == 7
    assert sum(first["by_page_kind"].values()) == 7
    assert sum(first["by_resource_type"].values()) == 7
    assert first["by_section"]["circular"] == 1
    assert first["by_section"]["unknown"] == 3             # the three document posts
    assert first["by_department"]["bfr"] == 2
    assert first["by_department"]["unknown"] == 4
    assert first["document_posts_by_owner"] == {"bfr": 2, "psd": 1}


def test_summary_surfaces_the_classification_gaps(monkeypatch):
    summary = report.summarize(_inventory(monkeypatch))
    assert summary["unmapped_categories"] == {"'brand-new-thing'": 1}
    assert summary["unrecognised_path_roots"] == {"'mystery-root'": 1}


def test_lastmod_range_is_reported(monkeypatch):
    summary = report.summarize(_inventory(monkeypatch))
    assert summary["lastmod_earliest"] == "2020-01-01"
    assert summary["lastmod_latest"] == "2026-08-13"


def test_unclassified_sample_is_bounded_and_reproducible(monkeypatch):
    extra = tuple((f"{HOST}/bfr/extra-{i}/", None) for i in range(40))
    inv = _inventory(monkeypatch, extra)
    summary = report.summarize(inv, sample_size=5)
    assert summary["unclassified_total"] > 5
    assert len(summary["unclassified_sample"]) == 5
    assert summary["unclassified_sample"] == sorted(summary["unclassified_sample"])
    assert summary == report.summarize(inv, sample_size=5)


def test_render_produces_text_and_shouts_about_truncation(monkeypatch):
    summary = report.summarize(_inventory(monkeypatch))
    text = report.render(summary)
    assert "Nepal Rastra Bank sitemap discovery" in text
    assert "INVENTORY TRUNCATED" not in text

    summary["truncated"] = ["MAX_URLS=2"]
    assert "INVENTORY TRUNCATED" in report.render(summary)
    assert "MAX_URLS=2" in report.render(summary)


def test_summary_is_json_serializable(monkeypatch):
    import json

    json.dumps(report.summarize(_inventory(monkeypatch)), ensure_ascii=False)


def test_discovery_registers_no_model_facing_tool():
    """Phase 2 is an inventory. Nothing here may reach the agent."""
    from app.tools.local import LOCAL_TOOLS

    names = {spec.name for spec in LOCAL_TOOLS}
    assert not {"nrb_sitemap", "discover_nrb_documents", "search_nrb_documents"} & names
    assert not hasattr(sm, "SPEC") and not hasattr(classify, "SPEC")

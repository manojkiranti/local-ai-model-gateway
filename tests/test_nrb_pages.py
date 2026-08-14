"""Unit tests for NRB Phase 3 document discovery — REST, attachments, page probe.

No test here touches the network: every `httpx.AsyncClient` is routed through a
`MockTransport`, the same pattern `test_nrb_forex.py` and `test_nrb_sitemap.py`
use. The live probe is `scripts/nrb_document_inventory.py`.

The fixtures are shaped like NRB's real payloads, because most of what is under
test exists only because of what the live site does: `acf.document_file` being
`false` rather than absent, `acf` itself being `[]` on a post with no custom
fields, a post URL 302ing to a PDF instead of rendering, categories being filed
under children (`domestic-tenders` under `tenders`), and `og:title` carrying a
site-name suffix.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib

import httpx
import pytest

from app.nrb import attachments as att
from app.nrb import documents as docs
from app.nrb import http as nrb_http
from app.nrb import page as page_mod
from app.nrb import report, wp_api

HOST = "https://www.nrb.org.np"
UPLOADS = f"{HOST}/contents/uploads/2026/08"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Captured once, at import: `_init_with` must always wrap the REAL __init__.
# Wrapping an already-patched one silently keeps the first test's handler.
_ORIGINAL_INIT = httpx.AsyncClient.__init__


def _init_with(handler):
    """Route every AsyncClient opened through `handler`."""
    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        _ORIGINAL_INIT(self, *a, **kw)

    return patched_init


def _with_routes(monkeypatch, routes, *, log=None):
    """Serve `routes` (url -> Response | callable) to every AsyncClient opened."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if log is not None:
            log.append(url)
        entry = routes.get(url)
        if entry is None:
            return httpx.Response(404, text="<html>not found</html>",
                                  headers={"content-type": "text/html"})
        return entry(request) if callable(entry) else entry

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))


def _json(payload, *, headers=None, status=200):
    return httpx.Response(
        status, json=payload,
        headers={"content-type": "application/json; charset=UTF-8", **(headers or {})},
    )


def _file_field(name="Directive.pdf", mime="application/pdf", **over):
    """A WordPress attachment object as ACF returns it."""
    return {
        "ID": 1234, "id": 1234, "title": name.rsplit(".", 1)[0],
        "filename": name, "filesize": 1013486,
        "url": f"{UPLOADS}/{name}", "mime_type": mime,
        "subtype": name.rsplit(".", 1)[-1], "type": "application",
        "date": "2026-08-13 10:14:03", **over,
    }


def _post(**over):
    """A document post as `/api/wp/v2/<type>` returns it."""
    base = {
        "id": 137697,
        "type": "bfr",
        "slug": "some-slug",
        "status": "publish",
        "link": f"{HOST}/bfr/some-slug/",
        "date": "2026-08-13T15:59:54",
        "date_gmt": "2026-08-13T10:14:54",
        "modified": "2026-08-13T16:01:28",
        "title": {"rendered": "Notice on subsidised loans"},
        "content": {"rendered": ""},
        "categories": [27],
        "acf": {
            "document_file": _file_field(),
            "document_redirect_to_file": True,
            "secondary_file": False,
            "document_file_expiry": "",
            "no_homepage": False,
        },
    }
    base.update(over)
    return base


def _taxonomy(*rows):
    """rows: (id, slug, parent). Defaults to a small real-shaped tree."""
    rows = rows or (
        (27, "circulars", 0), (5, "notices", 0), (26, "upload-files", 0),
        (99, "unified-directives", 0), (300, "tenders", 0),
        (301, "domestic-tenders", 300),          # child, as NRB really files them
        (400, "brand-new-category", 0),
    )
    return docs.Taxonomy([
        wp_api.Category(id=i, slug=s, name=s.replace("-", " ").title(), parent=p, count=1)
        for i, s, p in rows
    ])


# =========================================================================== #
# wp_api — transport, bounds, security
# =========================================================================== #
def test_rest_url_is_built_from_config_not_from_an_argument():
    url = wp_api.rest_url("bfr", {"per_page": 100, "page": 2})
    assert url.startswith(f"{HOST}/api/wp/v2/bfr?")
    assert "per_page=100" in url and "page=2" in url


def test_rest_prefix_is_api_not_wp_json():
    """NRB moved the REST prefix; /wp-json/ really is 404 there."""
    assert wp_api.REST_PREFIX == "/api/wp/v2"


def test_fetch_posts_paginates_and_stops_on_a_short_page(monkeypatch):
    pages = {
        1: [_post(id=i, link=f"{HOST}/bfr/p{i}/") for i in range(100)],
        2: [_post(id=200, link=f"{HOST}/bfr/last/")],
    }
    seen = []

    def handler(request):
        page = int(request.url.params["page"])
        seen.append(page)
        return _json(pages.get(page, []), headers={"x-wp-total": "101"})

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))
    result = asyncio.run(wp_api.fetch_posts("bfr", delay=0))
    assert seen == [1, 2]
    assert len(result.items) == 101
    assert result.total_reported == 101
    assert result.errors == []


def test_fetch_posts_honours_max_items(monkeypatch):
    def handler(request):
        return _json([_post(id=i, link=f"{HOST}/bfr/p{i}/") for i in range(100)])

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))
    result = asyncio.run(wp_api.fetch_posts("bfr", max_items=25, delay=0))
    assert len(result.items) == 25


def test_page_bound_is_reported(monkeypatch):
    monkeypatch.setattr(wp_api, "MAX_PAGES", 2)

    def handler(request):
        return _json([_post(id=i) for i in range(100)])

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))
    result = asyncio.run(wp_api.fetch_posts("bfr", delay=0))
    assert result.truncated == ["MAX_PAGES=2"]
    assert len(result.items) == 200


def test_a_foreign_rest_host_is_refused_before_any_request(monkeypatch):
    log = []
    _with_routes(monkeypatch, {}, log=log)
    client = nrb_http.open_client("t", accept="application/json")

    async def run():
        try:
            return await wp_api._get_json(client, "https://evil.example/api/wp/v2/bfr")
        finally:
            await client.aclose()

    _payload, _headers, error = asyncio.run(run())
    assert error.kind == nrb_http.REJECTED_HOST and "evil.example" in error.detail
    assert log == []


@pytest.mark.parametrize(
    "response,kind",
    [
        (httpx.Response(500, text="boom", headers={"content-type": "application/json"}),
         nrb_http.HTTP_STATUS),
        (httpx.Response(301, headers={"location": f"{HOST}/elsewhere"}),
         nrb_http.REJECTED_REDIRECT),
        (httpx.Response(200, text="<html>404 page</html>",
                        headers={"content-type": "text/html"}),
         nrb_http.UNEXPECTED_CONTENT_TYPE),
        (httpx.Response(200, text="{not json",
                        headers={"content-type": "application/json"}),
         nrb_http.MALFORMED_BODY),
    ],
)
def test_rest_failures_are_structured_by_kind(monkeypatch, response, kind):
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(lambda r: response))
    result = asyncio.run(wp_api.fetch_posts("bfr", delay=0))
    assert [e.kind for e in result.errors] == [kind]
    assert result.items == []


def test_rest_timeout_and_network_errors_are_structured(monkeypatch):
    for exc, kind in (
        (httpx.ReadTimeout, nrb_http.TIMEOUT),
        (httpx.ConnectError, nrb_http.NETWORK),
    ):
        def handler(request, exc=exc):
            raise exc("nope", request=request)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))
        result = asyncio.run(wp_api.fetch_posts("bfr", delay=0))
        assert [e.kind for e in result.errors] == [kind]


def test_oversized_rest_response_is_capped(monkeypatch):
    monkeypatch.setattr(wp_api, "MAX_RESPONSE_BYTES", 50)
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__",
        _init_with(lambda r: _json([_post() for _ in range(20)])),
    )
    result = asyncio.run(wp_api.fetch_posts("bfr", delay=0))
    assert [e.kind for e in result.errors] == [nrb_http.TOO_LARGE]


def test_a_non_array_collection_is_malformed(monkeypatch):
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__", _init_with(lambda r: _json({"code": "oops"}))
    )
    result = asyncio.run(wp_api.fetch_posts("bfr", delay=0))
    assert [e.kind for e in result.errors] == [nrb_http.MALFORMED_BODY]


def test_running_off_the_end_of_a_collection_is_not_reported_as_an_error(monkeypatch):
    """WP answers a page past the end with a 400, which is a terminator."""
    def handler(request):
        page = int(request.url.params["page"])
        if page == 1:
            return _json([_post(id=i) for i in range(100)], headers={"x-wp-total": "100"})
        return _json({"code": "rest_post_invalid_page_number"}, status=400)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))
    result = asyncio.run(wp_api.fetch_posts("bfr", delay=0))
    assert len(result.items) == 100 and result.errors == []


def test_categories_are_typed_and_bad_rows_skipped(monkeypatch):
    payload = [
        {"id": 27, "slug": "circulars", "name": "Circulars", "parent": 0, "count": 12},
        {"slug": "no-id"},                                    # dropped
        {"id": 301, "slug": "domestic-tenders", "name": "D", "parent": 300, "count": 3},
    ]
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(lambda r: _json(payload)))
    result = asyncio.run(wp_api.fetch_categories())
    assert [c.id for c in result.items] == [27, 301]
    assert result.items[1].parent == 300


def test_post_types_expose_their_rest_base(monkeypatch):
    payload = {
        "bfr": {"name": "BFR", "rest_base": "bfr"},
        "broken": {"name": "no rest base"},
        "post": {"name": "Posts", "rest_base": "posts"},
    }
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(lambda r: _json(payload)))
    result = asyncio.run(wp_api.fetch_post_types())
    assert [(t.name, t.rest_base) for t in result.items] == [("bfr", "bfr"), ("post", "posts")]


# =========================================================================== #
# attachments
# =========================================================================== #
def test_acf_document_file_is_the_primary_attachment():
    found, warnings = att.extract_attachments(_post(), base_url=f"{HOST}/bfr/some-slug/")
    assert len(found) == 1 and warnings == []
    attachment = found[0]
    assert attachment.url == f"{UPLOADS}/Directive.pdf"
    assert attachment.source == "acf:document_file"
    assert attachment.resource_type == "pdf" and attachment.type_source == "mime"
    assert attachment.mime_type == "application/pdf" and attachment.filesize == 1013486
    assert attachment.filename == "Directive.pdf" and attachment.extension == "pdf"
    assert attachment.on_allowed_host and attachment.wp_id == 1234


def test_secondary_file_is_a_second_attachment_in_a_fixed_order():
    post = _post()
    post["acf"]["secondary_file"] = _file_field("Annex.xlsx", "application/vnd.ms-excel")
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert [a.source for a in found] == ["acf:document_file", "acf:secondary_file"]
    assert found[1].resource_type == "spreadsheet"


def test_an_unset_acf_file_field_is_false_not_missing():
    """WordPress writes `false`; treating that as an object would invent a file."""
    post = _post()
    post["acf"]["document_file"] = False
    found, warnings = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found == [] and warnings == []


def test_acf_can_be_a_list_on_a_post_with_no_custom_fields():
    """48 of 2,786 sampled posts returned `acf: []`."""
    found, warnings = att.extract_attachments(_post(acf=[]), base_url=f"{HOST}/bfr/x/")
    assert found == [] and warnings == []


def test_an_unrecognised_acf_file_field_is_still_found_by_shape():
    post = _post()
    post["acf"]["economic_review_volume_pdf_file"] = _file_field("Volume.pdf")
    post["acf"]["document_file"] = False
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert [a.source for a in found] == ["acf:economic_review_volume_pdf_file"]


def test_a_list_valued_acf_field_of_files_is_expanded():
    post = _post(acf={"document_file": False,
                      "gallery": [_file_field("a.pdf"), _file_field("b.pdf")]})
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert [a.filename for a in found] == ["a.pdf", "b.pdf"]


def test_page_furniture_fields_are_not_attachments():
    post = _post(acf={"document_file": False,
                      "banner_details": _file_field("banner.jpg", "image/jpeg")})
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found == []


def test_a_non_object_in_a_known_file_field_warns_rather_than_guessing():
    post = _post()
    post["acf"]["document_file"] = "https://www.nrb.org.np/x.pdf"
    found, warnings = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found == []
    assert warnings and "not a file object" in warnings[0]


# --- body links ---
def _body_post(html):
    return _post(acf=[], content={"rendered": html})


def test_a_relative_body_href_is_resolved():
    post = _body_post('<p><a href="/contents/uploads/2022/11/Report.pdf">Report</a></p>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/fmd/ashwin-2079/")
    assert found[0].url == f"{HOST}/contents/uploads/2022/11/Report.pdf"
    assert found[0].href == "/contents/uploads/2022/11/Report.pdf"
    assert found[0].source == "body_link" and found[0].link_text == "Report"
    assert found[0].type_source == "extension"   # no MIME from a bare anchor


def test_an_absolute_body_href_is_kept():
    post = _body_post(f'<a href="{UPLOADS}/A.pdf">x</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/fmd/x/")
    assert found[0].url == f"{UPLOADS}/A.pdf"


def test_a_url_encoded_filename_survives_and_still_types():
    encoded = "%e0%a4%b8%e0%a5%82%e0%a4%9a%e0%a4%a8%e0%a4%be.pdf"
    post = _body_post(f'<a href="{UPLOADS}/{encoded}">सूचना</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found[0].url == f"{UPLOADS}/{encoded}"      # not re-encoded or decoded
    assert found[0].extension == "pdf" and found[0].resource_type == "pdf"
    assert found[0].filename == "सूचना.pdf"            # decoded only for display
    assert found[0].link_text == "सूचना"


def test_a_query_string_is_preserved_but_a_fragment_is_dropped():
    post = _body_post(f'<a href="{UPLOADS}/f.pdf?ver=2#page=3">x</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found[0].url == f"{UPLOADS}/f.pdf?ver=2"


def test_tracking_parameters_are_dropped_so_equivalent_urls_dedupe():
    post = _body_post(
        f'<a href="{UPLOADS}/f.pdf">a</a><a href="{UPLOADS}/f.pdf?utm_source=x">b</a>'
    )
    found, warnings = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert len(found) == 1
    assert any("duplicate attachment reference" in w for w in warnings)


def test_duplicate_hrefs_are_deduplicated_and_counted():
    post = _body_post(f'<a href="{UPLOADS}/f.pdf">a</a><a href="{UPLOADS}/f.pdf">b</a>')
    found, warnings = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert len(found) == 1 and found[0].link_text == "a"     # first wins
    assert len(warnings) == 1


def test_the_acf_record_wins_over_a_body_link_to_the_same_file():
    """The ACF copy carries the MIME type and size; the anchor carries neither."""
    post = _post(content={"rendered": f'<a href="{UPLOADS}/Directive.pdf">PDF</a>'})
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert len(found) == 1
    assert found[0].source == "acf:document_file" and found[0].mime_type


def test_misleading_link_text_never_decides_the_type():
    post = _body_post(f'<a href="{UPLOADS}/data.xlsx">Download PDF</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found[0].resource_type == "spreadsheet"
    assert found[0].link_text == "Download PDF"


def test_a_pdf_extension_is_recorded_as_url_derived_not_verified():
    post = _body_post(f'<a href="{UPLOADS}/x.pdf">x</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found[0].resource_type == "pdf"
    assert found[0].type_source == "extension"      # NOT "mime"
    assert found[0].mime_type is None               # nothing was fetched


def test_mime_type_overrides_a_disagreeing_extension():
    post = _post()
    post["acf"]["document_file"] = _file_field("report.pdf", "application/vnd.ms-excel")
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found[0].resource_type == "spreadsheet" and found[0].type_source == "mime"
    assert found[0].extension == "pdf"              # both facts kept


def test_an_extensionless_body_link_is_not_treated_as_a_document():
    post = _body_post(f'<a href="{HOST}/bfr/another-post/">see also</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found == []


@pytest.mark.parametrize("href", ["#top", "mailto:a@b.np", "tel:123", "javascript:x()"])
def test_non_resource_hrefs_are_ignored(href):
    post = _body_post(f'<a href="{href}">x</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found == []


def test_an_off_host_attachment_is_kept_and_flagged():
    post = _body_post('<a href="https://files.example/x.pdf">x</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert len(found) == 1
    assert not found[0].on_allowed_host
    assert "files.example" in found[0].host_reason


def test_malformed_html_still_yields_its_links():
    post = _body_post(f'<p><a href="{UPLOADS}/a.pdf">unclosed')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert [a.filename for a in found] == ["a.pdf"]


def test_html_entities_in_link_text_are_decoded():
    post = _body_post(f'<a href="{UPLOADS}/a.pdf">Rules &amp; Bylaws</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found[0].link_text == "Rules & Bylaws"


@pytest.mark.parametrize(
    "name,kind",
    [("a.pdf", "pdf"), ("a.PDF", "pdf"), ("a.xlsx", "spreadsheet"), ("a.csv", "spreadsheet"),
     ("a.docx", "document"), ("a.zip", "archive"), ("a.jpg", "image"), ("a.html", "web")],
)
def test_extension_typing(name, kind):
    assert att.resource_type_for(f"{UPLOADS}/{name}", None) == (kind, "extension")


def test_a_url_with_no_extension_is_unknown_not_guessed():
    assert att.resource_type_for(f"{HOST}/bfr/some-post/", None) == ("unknown", "none")


def test_a_dotted_path_segment_is_not_read_as_an_extension():
    assert att.resource_type_for(f"{UPLOADS}/2083.04.27_LoI", None) == ("unknown", "none")


# =========================================================================== #
# documents — normalization + classification
# =========================================================================== #
def test_build_document_extracts_the_authoritative_metadata():
    document = docs.build_document(_post(), taxonomy=_taxonomy())
    assert document.post_id == 137697 and document.post_type == "bfr"
    assert document.url == f"{HOST}/bfr/some-slug/"
    assert document.title == "Notice on subsidised loans"
    assert document.published == "2026-08-13T15:59:54"
    assert document.modified == "2026-08-13T16:01:28"
    assert document.owner == "bfr" and document.page_kind == "document_post"
    assert document.sections == ("circular",)
    assert document.primary_section == "circular"
    assert document.attachment_count == 1 and document.has_pdf
    assert document.redirects_to_file is True
    assert document.warnings == []


def test_a_devanagari_title_is_preserved_exactly():
    title = "सूचना नं. २/०८३ –८४ : सहुलियतपुर्ण कर्जा"
    document = docs.build_document(_post(title={"rendered": title}), taxonomy=_taxonomy())
    assert document.title == title


def test_html_entities_in_a_title_are_decoded_not_stripped():
    document = docs.build_document(
        _post(title={"rendered": "Rules &amp; Bylaws &#8211; 2082"}), taxonomy=_taxonomy()
    )
    assert document.title == "Rules & Bylaws – 2082"


def test_a_missing_title_is_none_not_a_slug_derived_guess():
    document = docs.build_document(_post(title={}, slug="some-slug"), taxonomy=_taxonomy())
    assert document.title is None and document.slug == "some-slug"


def test_a_post_with_no_dates_reports_none():
    post = _post()
    del post["date"], post["modified"]
    document = docs.build_document(post, taxonomy=_taxonomy())
    assert document.published is None and document.modified is None


def test_owner_is_independent_of_document_type():
    """Phase 2's finding: the owner does not imply the kind. Still true here."""
    document = docs.build_document(_post(categories=[99]), taxonomy=_taxonomy())
    assert document.owner == "bfr"                  # who published it
    assert document.sections == ("directive",)      # what it is — from categories


def test_unknown_stays_unknown_without_category_evidence():
    document = docs.build_document(_post(categories=[]), taxonomy=_taxonomy())
    assert document.sections == () and document.primary_section == "unknown"


def test_a_miscellaneous_category_does_not_claim_a_section():
    """`upload-files` is a real NRB category that genuinely means nothing."""
    document = docs.build_document(_post(categories=[26]), taxonomy=_taxonomy())
    assert document.sections == () and document.primary_section == "unknown"


def test_an_unmapped_category_is_reported_as_a_todo():
    document = docs.build_document(_post(categories=[400]), taxonomy=_taxonomy())
    assert document.sections == ()
    assert document.section_evidence == ("unmapped category 'brand-new-category'",)


def test_a_child_category_resolves_through_its_parent():
    """NRB files posts under children; Phase 2's map is keyed on the parents."""
    document = docs.build_document(_post(categories=[301]), taxonomy=_taxonomy())
    assert document.sections == ("procurement",)
    assert document.section_evidence == (
        "category 'domestic-tenders' via ancestor 'tenders'",
    )


def test_a_post_can_hold_several_sections_and_the_primary_is_deterministic():
    document = docs.build_document(_post(categories=[5, 27]), taxonomy=_taxonomy())
    assert document.sections == ("circular", "notice")     # SECTIONS order
    assert document.primary_section == "circular"          # regulatory wins


def test_category_slugs_and_names_are_kept_as_raw_evidence():
    document = docs.build_document(_post(categories=[27]), taxonomy=_taxonomy())
    assert document.category_ids == (27,)
    assert document.category_slugs == ("circulars",)
    assert document.category_names == ("Circulars",)


def test_a_cyclic_category_parent_does_not_hang():
    taxonomy = _taxonomy((1, "a", 2), (2, "b", 1))
    assert taxonomy.chain(1) == ["a", "b"]


def test_deterministic_acf_scalars_are_kept_as_extras():
    post = _post()
    post["acf"]["circular_number"] = "12/2082-83"
    post["acf"]["fiscal_year"] = "2082/83"
    document = docs.build_document(post, taxonomy=_taxonomy())
    assert document.extras == {"circular_number": "12/2082-83", "fiscal_year": "2082/83"}


def test_a_post_with_no_link_still_produces_a_record_with_a_warning():
    post = _post()
    del post["link"]
    document = docs.build_document(post, taxonomy=_taxonomy())
    assert document.url == "" and "post has no link" in document.warnings


def test_build_document_works_without_a_taxonomy():
    document = docs.build_document(_post(), taxonomy=None)
    assert document.sections == () and document.attachment_count == 1


def test_a_federal_office_post_keeps_the_url_owner():
    post = _post(type="brg", link=f"{HOST}/federal-offices/brg/some-notice/")
    document = docs.build_document(post, taxonomy=_taxonomy())
    assert document.owner == "brg" and document.page_kind == "document_post"


def test_build_document_is_deterministic():
    assert docs.build_document(_post(), taxonomy=_taxonomy()) == \
           docs.build_document(_post(), taxonomy=_taxonomy())


# =========================================================================== #
# page probe
# =========================================================================== #
PAGE_HTML = """<!doctype html><html><head>
<title>Ashwin 2079 - the official site of the Central Bank of Nepal</title>
<link rel="canonical" href="https://www.nrb.org.np/fmd/ashwin-2079/" />
<meta property="og:site_name" content="the official site of the Central Bank of Nepal" />
<meta property="og:title" content="Ashwin 2079 - the official site of the Central Bank of Nepal" />
<meta property="og:description" content="Monthly Balance Sheet-79.06" />
<meta property="article:section" content="Balance Sheet (FY 2079/80)" />
</head><body>
<div class="breadcrumb"><span><span><a href="/">Home</a> &raquo; <span>
<a href="/fmd/">Financial Management Departments</a> &raquo;
<span class="breadcrumb_last">Ashwin 2079</span></span></span></span></div>
<div class="card-bt-body"><div class="px-2"><div class="main-title">Ashwin 2079</div>
<div><p><a href="/contents/uploads/2022/11/MBS.pdf">Monthly Balance Sheet</a></p></div>
</div></div></body></html>"""


def _html(text=PAGE_HTML, status=200):
    return httpx.Response(status, text=text,
                          headers={"content-type": "text/html; charset=UTF-8"})


def _probe(monkeypatch, routes, url=f"{HOST}/fmd/ashwin-2079/", log=None):
    _with_routes(monkeypatch, routes, log=log)

    async def run():
        client = page_mod.open_page_client()
        try:
            return await page_mod.probe_page(client, url)
        finally:
            await client.aclose()

    return asyncio.run(run())


def test_a_post_page_that_renders_is_parsed():
    meta = page_mod.parse_page_meta(PAGE_HTML)
    assert meta.canonical_url == f"{HOST}/fmd/ashwin-2079/"
    assert meta.og_title == "Ashwin 2079"           # site-name suffix stripped
    assert meta.title_tag == "Ashwin 2079"
    assert meta.main_title == "Ashwin 2079"
    assert meta.article_section == "Balance Sheet (FY 2079/80)"
    assert meta.og_description == "Monthly Balance Sheet-79.06"
    assert meta.breadcrumbs == ("Home", "Financial Management Departments", "Ashwin 2079")
    assert meta.owner_label == "Financial Management Departments"


def test_a_title_without_a_declared_site_name_is_left_alone():
    meta = page_mod.parse_page_meta(
        '<html><head><title>Report - 2082 - something</title></head></html>'
    )
    assert meta.title_tag == "Report - 2082 - something"


def test_a_devanagari_page_title_survives_parsing():
    title = "अ.प्रा. निर्देशन नं. १"
    meta = page_mod.parse_page_meta(
        f'<html><head><meta property="og:title" content="{title}"></head>'
        f'<body><div class="main-title">{title}</div></body></html>'
    )
    assert meta.og_title == title and meta.main_title == title


def test_missing_optional_metadata_is_none_not_invented():
    meta = page_mod.parse_page_meta("<html><head></head><body>hi</body></html>")
    assert meta.canonical_url is None and meta.article_section is None
    assert meta.breadcrumbs == () and meta.owner_label is None


def test_an_unexpected_breadcrumb_shape_yields_no_owner_label():
    meta = page_mod.parse_page_meta('<div class="breadcrumb">Home &raquo; A &raquo; B &raquo; C</div>')
    assert meta.breadcrumbs == ("Home", "A", "B", "C") and meta.owner_label is None


def test_malformed_html_is_tolerated():
    meta = page_mod.parse_page_meta('<html><head><title>Half')
    assert meta.title_tag == "Half"


def test_a_nested_div_does_not_end_the_title_region_early():
    meta = page_mod.parse_page_meta(
        '<div class="main-title">Notice <div><span>2082</span></div> final</div>'
    )
    assert meta.main_title == "Notice 2082 final"


def test_the_common_case_is_a_redirect_straight_to_the_file(monkeypatch):
    routes = {
        f"{HOST}/bfr/x/": httpx.Response(302, headers={"location": f"{UPLOADS}/D.pdf"}),
        f"{UPLOADS}/D.pdf": httpx.Response(200, content=b"%PDF-1.4",
                                           headers={"content-type": "application/pdf"}),
    }
    probe = _probe(monkeypatch, routes, url=f"{HOST}/bfr/x/")
    assert probe.outcome == page_mod.OUTCOME_REDIRECT_TO_FILE
    assert probe.final_url == f"{UPLOADS}/D.pdf"
    assert probe.content_type == "application/pdf"
    assert probe.redirect_chain == [f"{UPLOADS}/D.pdf"]
    assert probe.ok


def test_a_relative_redirect_location_is_resolved(monkeypatch):
    routes = {
        f"{HOST}/bfr/x/": httpx.Response(302, headers={"location": "/contents/uploads/2026/08/D.pdf"}),
        f"{UPLOADS}/D.pdf": httpx.Response(200, content=b"%PDF",
                                           headers={"content-type": "application/pdf"}),
    }
    probe = _probe(monkeypatch, routes, url=f"{HOST}/bfr/x/")
    assert probe.final_url == f"{UPLOADS}/D.pdf"


def test_an_html_page_is_fetched_and_parsed(monkeypatch):
    probe = _probe(monkeypatch, {f"{HOST}/fmd/ashwin-2079/": _html()})
    assert probe.outcome == page_mod.OUTCOME_HTML
    assert probe.meta.main_title == "Ashwin 2079"


def test_a_redirect_to_a_foreign_host_is_refused_and_never_requested(monkeypatch):
    log = []
    routes = {
        f"{HOST}/bfr/x/": httpx.Response(302, headers={"location": "https://evil.example/x.pdf"}),
    }
    probe = _probe(monkeypatch, routes, url=f"{HOST}/bfr/x/", log=log)
    assert probe.outcome == page_mod.OUTCOME_ERROR
    assert probe.error.kind == nrb_http.REJECTED_REDIRECT
    assert "https://evil.example/x.pdf" not in log


def test_a_foreign_start_url_is_refused(monkeypatch):
    log = []
    probe = _probe(monkeypatch, {}, url="https://evil.example/post/", log=log)
    assert probe.error.kind == nrb_http.REJECTED_HOST and log == []


def test_a_redirect_loop_is_bounded(monkeypatch):
    routes = {
        f"{HOST}/a/": httpx.Response(302, headers={"location": f"{HOST}/b/"}),
        f"{HOST}/b/": httpx.Response(302, headers={"location": f"{HOST}/a/"}),
    }
    probe = _probe(monkeypatch, routes, url=f"{HOST}/a/")
    assert probe.error.kind == nrb_http.REJECTED_REDIRECT
    assert "more than" in probe.error.detail


def test_a_redirect_with_no_location_is_malformed(monkeypatch):
    probe = _probe(monkeypatch, {f"{HOST}/a/": httpx.Response(302)}, url=f"{HOST}/a/")
    assert probe.error.kind == nrb_http.MALFORMED_BODY


@pytest.mark.parametrize(
    "response,kind",
    [
        (httpx.Response(404, text="gone", headers={"content-type": "text/html"}),
         nrb_http.HTTP_STATUS),
        (httpx.Response(200, content=b"x", headers={"content-type": ""}),
         nrb_http.UNEXPECTED_CONTENT_TYPE),
    ],
)
def test_page_failures_are_structured(monkeypatch, response, kind):
    probe = _probe(monkeypatch, {f"{HOST}/a/": response}, url=f"{HOST}/a/")
    assert probe.error.kind == kind and not probe.ok


def test_a_non_html_content_type_is_an_outcome_not_a_failure(monkeypatch):
    """A post URL answering with anything but markup means it IS the resource."""
    probe = _probe(
        monkeypatch,
        {f"{HOST}/a/": httpx.Response(200, content=b"x",
                                      headers={"content-type": "text/plain"})},
        url=f"{HOST}/a/",
    )
    assert probe.outcome == page_mod.OUTCOME_REDIRECT_TO_FILE
    assert probe.content_type == "text/plain" and probe.ok


def test_page_timeout_and_network_errors_are_structured(monkeypatch):
    for exc, kind in ((httpx.ReadTimeout, nrb_http.TIMEOUT),
                      (httpx.ConnectError, nrb_http.NETWORK)):
        def handler(request, exc=exc):
            raise exc("nope", request=request)

        probe = _probe(monkeypatch, {f"{HOST}/a/": handler}, url=f"{HOST}/a/")
        assert probe.error.kind == kind


def test_an_oversized_page_is_capped(monkeypatch):
    monkeypatch.setattr(page_mod, "MAX_RESPONSE_BYTES", 32)
    probe = _probe(monkeypatch, {f"{HOST}/a/": _html()}, url=f"{HOST}/a/")
    assert probe.error.kind == nrb_http.TOO_LARGE


# =========================================================================== #
# report
# =========================================================================== #
def _documents(taxonomy=None):
    taxonomy = taxonomy or _taxonomy()
    posts = [
        _post(id=1, link=f"{HOST}/bfr/a/", categories=[27]),
        _post(id=2, link=f"{HOST}/psd/b/", type="psd", categories=[5],
              acf={"document_file": _file_field("s.xlsx", "application/vnd.ms-excel"),
                   "secondary_file": _file_field("annex.pdf")}),
        _post(id=3, link=f"{HOST}/psd/c/", type="psd", categories=[], acf=[]),
        _post(id=4, link=f"{HOST}/fmd/d/", type="fmd", categories=[400],
              acf=[], content={"rendered": '<a href="https://files.example/x.pdf">x</a>'}),
    ]
    return sorted((docs.build_document(p, taxonomy=taxonomy) for p in posts),
                  key=lambda d: d.url)


def test_document_summary_counts_add_up_and_are_deterministic():
    documents = _documents()
    first = report.summarize_documents(documents)
    assert first == report.summarize_documents(documents)
    assert first["documents_normalized"] == 4
    assert first["posts_by_attachment_count"] == {"0": 1, "1": 2, "2": 1}
    assert first["posts_with_no_attachment"] == 1
    assert first["posts_with_one_attachment"] == 2
    assert first["posts_with_multiple_attachments"] == 1
    assert first["attachment_links_total"] == 4
    assert first["attachment_urls_unique"] == 4
    assert first["pdf_like_attachments"] == 3
    assert first["non_pdf_attachments"] == 1


def test_document_summary_separates_known_from_unknown_type():
    summary = report.summarize_documents(_documents())
    assert summary["documents_with_known_type"] == 2      # circular + notice
    assert summary["documents_with_unknown_type"] == 2    # no cats + unmapped cat
    assert summary["type_coverage"] == 0.5
    assert summary["unmapped_categories"] == {"'brand-new-category'": 1}


def test_document_summary_flags_off_host_attachments():
    summary = report.summarize_documents(_documents())
    assert summary["off_host_attachments"] == 1
    assert summary["off_host_examples"] == ["https://files.example/x.pdf"]
    assert "www.nrb.org.np (configured NRB host)" in summary["attachment_hosts"]


def test_document_summary_aggregates_failures_by_kind():
    errors = [
        nrb_http.FetchError(nrb_http.TIMEOUT, "timed out", f"{HOST}/api/wp/v2/bfr"),
        nrb_http.FetchError(nrb_http.TIMEOUT, "timed out", f"{HOST}/api/wp/v2/psd"),
        nrb_http.FetchError(nrb_http.HTTP_STATUS, "HTTP 500", f"{HOST}/api/wp/v2/red", 500),
    ]
    summary = report.summarize_documents(_documents(), errors=errors)
    assert summary["fetch_failures"] == 3
    assert summary["failures_by_kind"] == {"timeout": 2, "http_status": 1}
    assert len(summary["failure_examples"]) == 3


def test_document_summary_records_probe_agreement():
    class _Probe:
        def __init__(self, url, final, expected, outcome="redirect_to_file"):
            self.url, self.final_url, self.outcome = url, final, outcome
            self.expected_attachment = expected

    probes = [
        _Probe(f"{HOST}/bfr/a/", f"{UPLOADS}/Directive.pdf", f"{UPLOADS}/Directive.pdf"),
        _Probe(f"{HOST}/bfr/b/", f"{UPLOADS}/Other.pdf", f"{UPLOADS}/Directive.pdf"),
    ]
    summary = report.summarize_documents(_documents(), probes=probes)
    assert summary["probes_run"] == 2
    assert summary["probe_attachment_agreements"] == 1
    assert summary["probe_attachment_disagreements"] == 1
    assert summary["probe_disagreement_examples"][0][0] == f"{HOST}/bfr/b/"


def test_document_summary_samples_are_bounded_and_sorted():
    documents = _documents() * 20
    summary = report.summarize_documents(documents, sample_size=3)
    assert len(summary["no_attachment_examples"]) == 3
    assert summary["no_attachment_examples"] == sorted(summary["no_attachment_examples"])


def test_document_summary_is_json_serializable_and_stable():
    summary = report.summarize_documents(_documents())
    assert json.dumps(summary, ensure_ascii=False, sort_keys=True) == \
           json.dumps(report.summarize_documents(_documents()), ensure_ascii=False,
                      sort_keys=True)


def test_render_documents_produces_text():
    text = report.render_documents(report.summarize_documents(_documents()))
    assert "Nepal Rastra Bank document discovery (Phase 3)" in text
    assert "Document type (from NRB's own category metadata)" in text
    assert "Off-host attachments" in text


# =========================================================================== #
# the script
# =========================================================================== #
def _script():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "nrb_document_inventory.py"
    spec = importlib.util.spec_from_file_location("nrb_document_inventory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_invocation_is_bounded_and_cannot_crawl_the_whole_site():
    """An accidental run must not walk 18,567 documents."""
    args = _script()._parse_args([])
    assert args.all is False
    assert args.limit == 200 and args.limit < 18_567
    assert args.verify == 0          # no page fetching unless asked


def test_a_full_crawl_requires_an_explicit_flag():
    assert _script()._parse_args(["--all"]).all is True


def test_script_arguments_parse():
    args = _script()._parse_args(
        ["--owner", "bfr", "--owner", "psd", "--limit", "50", "--sample", "10",
         "--seed", "7", "--offset", "2", "--verify", "5", "--json", "-v"]
    )
    assert args.owner == ["bfr", "psd"] and args.limit == 50
    assert args.sample == 10 and args.seed == 7 and args.offset == 2
    assert args.verify == 5 and args.json is True and args.verbose is True


def test_post_types_are_deterministic_and_exclude_forex():
    """NRB's 10,485 `forex` posts are the get_nrb_forex tool's corpus, not this one."""
    module = _script()
    types = module._post_types(None)
    assert types == module._post_types(None)     # stable order
    assert "forex" not in types
    assert "bfr" in types and "er-article" in types


def test_explicit_owners_override_the_default_set():
    assert _script()._post_types(["psd", "bfr"]) == ["bfr", "psd"]


def test_phase_3_registers_no_model_facing_tool():
    """Discovery only. Nothing here may reach the agent."""
    from app.tools.local import LOCAL_TOOLS

    names = {spec.name for spec in LOCAL_TOOLS}
    assert not {"search_nrb_documents", "nrb_documents", "fetch_nrb_document"} & names
    for module in (wp_api, docs, att, page_mod):
        assert not hasattr(module, "SPEC")


# =========================================================================== #
# Percent-encoding equivalence (found by the live verification probe)
# =========================================================================== #
LITERAL = f"{UPLOADS}/आगलागी-२०७४.pdf"
ENCODED = (f"{UPLOADS}/%E0%A4%86%E0%A4%97%E0%A4%B2%E0%A4%BE%E0%A4%97%E0%A5%80-"
           "%E0%A5%A8%E0%A5%A6%E0%A5%AD%E0%A5%AA.pdf")


def test_literal_and_percent_encoded_devanagari_name_the_same_file():
    """REST returns the literal form; the post URL's 302 returns the encoded one."""
    assert att.comparison_key(LITERAL) == att.comparison_key(ENCODED)
    assert LITERAL != ENCODED          # the raw strings really do differ


def test_the_published_spelling_is_preserved_despite_the_comparison_key():
    """`url` stays what NRB published — that is the string a downloader uses."""
    post = _body_post(f'<a href="{ENCODED}">x</a>')
    found, _ = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    assert found[0].url == ENCODED
    assert found[0].dedup_key == att.comparison_key(LITERAL)


def test_the_two_spellings_deduplicate_within_a_post():
    post = _post(acf={"document_file": _file_field(), "secondary_file": False},
                 content={"rendered": f'<a href="{ENCODED}">a</a><a href="{LITERAL}">b</a>'})
    found, warnings = att.extract_attachments(post, base_url=f"{HOST}/bfr/x/")
    body = [a for a in found if a.source == "body_link"]
    assert len(body) == 1
    assert any("duplicate attachment reference" in w for w in warnings)


def test_the_report_counts_the_two_spellings_as_one_unique_url():
    documents = [
        docs.build_document(
            _post(id=1, link=f"{HOST}/bfr/a/",
                  acf={"document_file": _file_field("x.pdf", url=LITERAL)}),
            taxonomy=_taxonomy()),
        docs.build_document(
            _post(id=2, link=f"{HOST}/bfr/b/",
                  acf={"document_file": _file_field("x.pdf", url=ENCODED)}),
            taxonomy=_taxonomy()),
    ]
    summary = report.summarize_documents(documents)
    assert summary["attachment_links_total"] == 2
    assert summary["attachment_urls_unique"] == 1
    assert summary["duplicate_attachment_references"] == 1


def test_a_query_string_still_distinguishes_two_urls():
    """Percent-decoding the PATH must not collapse genuinely different resources."""
    assert att.comparison_key(f"{UPLOADS}/f.pdf?v=1") != att.comparison_key(f"{UPLOADS}/f.pdf?v=2")


# =========================================================================== #
# Bounded runs must sample the corpus, not read the top of one owner
# =========================================================================== #
def _stub_collect(monkeypatch, module, served=None):
    """Stub REST so `_collect` never touches the network. Returns the ask-log."""
    asked: list[tuple[str, int | None]] = []
    names = module._post_types(None) if served is None else served

    async def fake_fetch_post_types(client=None):
        return wp_api.WPResult(
            items=[wp_api.PostTypeInfo(name=n, rest_base=n, label=n) for n in names]
        )

    async def fake_fetch_posts(rest_base, *, client=None, max_items=None, **kw):
        asked.append((rest_base, max_items))
        return wp_api.WPResult(items=[_post(id=1, link=f"{HOST}/{rest_base}/a/")])

    monkeypatch.setattr(module.wp_api, "fetch_post_types", fake_fetch_post_types)
    monkeypatch.setattr(module.wp_api, "fetch_posts", fake_fetch_posts)
    return asked


def test_a_bounded_run_spreads_its_limit_across_post_types(monkeypatch):
    """--limit 600 once returned 600 bfr documents and nothing from 34 owners."""
    module = _script()
    asked = _stub_collect(monkeypatch, module)
    args = module._parse_args(["--limit", "70"])
    posts, errors, truncated, unavailable = asyncio.run(module._collect(args))

    types = module._post_types(None)
    assert len(asked) == len(types)                     # every owner sampled
    assert {n for _, n in asked} == {2}                 # 70 spread over 39 types
    assert len(posts) == len(types) and errors == [] and unavailable == []


def test_a_post_type_rest_does_not_serve_is_a_finding_not_a_404(monkeypatch):
    """`economic-review` (49 URLs) and `er-article` (147) are sitemap-only."""
    module = _script()
    served = [n for n in module._post_types(None)
              if n not in ("economic-review", "er-article")]
    asked = _stub_collect(monkeypatch, module, served=served)
    args = module._parse_args(["--limit", "50"])
    _posts, errors, _truncated, unavailable = asyncio.run(module._collect(args))

    assert unavailable == ["economic-review", "er-article"]
    assert errors == []                                  # not counted as failures
    assert "economic-review" not in {name for name, _ in asked}   # never requested


def test_the_report_shouts_about_rest_invisible_post_types():
    summary = report.summarize_documents(
        _documents(), rest_unavailable=["er-article", "economic-review"]
    )
    assert summary["post_types_not_served_by_rest"] == ["economic-review", "er-article"]
    text = report.render_documents(summary)
    assert "NOT served by the REST API" in text and "er-article" in text


def test_an_explicit_owner_gets_the_whole_limit():
    module = _script()
    args = module._parse_args(["--owner", "bfr", "--limit", "50"])
    assert module._post_types(args.owner) == ["bfr"]


def test_type_coverage_is_broken_out_by_year():
    """One blended percentage hides NRB's 2019 legacy-migration backlog."""
    documents = [
        docs.build_document(_post(id=1, link=f"{HOST}/bfr/a/", categories=[27],
                                  date="2019-12-05T12:20:10"), taxonomy=_taxonomy()),
        docs.build_document(_post(id=2, link=f"{HOST}/bfr/b/", categories=[26],
                                  date="2019-12-06T12:20:10"), taxonomy=_taxonomy()),
        docs.build_document(_post(id=3, link=f"{HOST}/bfr/c/", categories=[27],
                                  date="2026-08-13T15:59:54"), taxonomy=_taxonomy()),
    ]
    summary = report.summarize_documents(documents)
    assert summary["type_coverage_by_year"] == {
        "2019": {"documents": 2, "typed": 1, "coverage": 0.5},
        "2026": {"documents": 1, "typed": 1, "coverage": 1.0},
    }
    assert summary["type_coverage"] == round(2 / 3, 4)
    assert "by publication year:" in report.render_documents(summary)


def test_a_document_with_no_date_lands_in_an_unknown_year_bucket():
    post = _post()
    del post["date"]
    summary = report.summarize_documents([docs.build_document(post, taxonomy=_taxonomy())])
    assert "unknown" in summary["type_coverage_by_year"]

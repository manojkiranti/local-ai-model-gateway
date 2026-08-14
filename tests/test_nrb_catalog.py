"""Phase 4 catalog records — pure unit tests. No database, no network.

Everything here is a function of its arguments: identity keys, the metadata
fingerprint, timestamp interpretation, and the discovery-to-rows mapping. The
reconciliation semantics that need Postgres live in
`tests/test_nrb_sync_integration.py`.

Fixtures are shaped like the live REST payloads Phase 3 measured, including the
awkward parts: `acf` arriving as `[]`, an unset file field arriving as `false`,
and the same attachment path spelled once with literal Devanagari and once
percent-encoded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.nrb.attachments import comparison_key
from app.nrb.classify import classify_url
from app.nrb.discovery import CONTENT_POST_TYPES, DOCUMENT_POST_TYPES, Discovery
from app.nrb.documents import Taxonomy, build_document
from app.nrb.http import normalize_url
from app.nrb.models import (
    FETCH_BLOCKED_HOST,
    FETCH_PENDING,
    METADATA_STATUS_REST,
    METADATA_STATUS_SITEMAP_ONLY,
    REL_ACF,
    REL_BODY_LINK,
    REL_PRIMARY,
    REL_SECONDARY,
)
from app.nrb.records import (
    build_source_records,
    file_record,
    metadata_digest,
    page_key,
    parse_lastmod,
    parse_wp_datetime,
    relationship_type_for,
    site_utc_offset,
    source_from_document,
    source_from_sitemap,
)
from app.nrb.report import render_sync, summarize_sync
from app.nrb.sync import SyncResult, _counters
from app.nrb.wp_api import Category

# NRB's real taxonomy shape: posts are filed under a CHILD of the mapped parent.
CATEGORIES = [
    Category(id=10, slug="circulars", name="Circulars", parent=0, count=100),
    Category(id=11, slug="2082-83", name="2082/83", parent=10, count=12),
    Category(id=20, slug="upload-files", name="Upload Files", parent=0, count=5052),
    Category(id=30, slug="notices", name="Notices", parent=0, count=40),
]
TAXONOMY = Taxonomy(CATEGORIES)

# A Devanagari filename, in the two spellings NRB actually serves.
DEVANAGARI_FILE = "https://www.nrb.org.np/wp-content/uploads/2021/11/आगलागी-२०७४.pdf"
ENCODED_FILE = (
    "https://www.nrb.org.np/wp-content/uploads/2021/11/"
    "%E0%A4%86%E0%A4%97%E0%A4%B2%E0%A4%BE%E0%A4%97%E0%A5%80-%E0%A5%A8%E0%A5%A6%E0%A5%AD%E0%A5%AA.pdf"
)


def wp_file(url: str, **overrides) -> dict:
    """A WordPress/ACF attachment object as REST returns one."""
    payload = {
        "ID": 5001,
        "url": url,
        "filename": url.rsplit("/", 1)[-1],
        "mime_type": "application/pdf",
        "filesize": 123456,
        "date": "2021-11-02 09:15:00",
        "title": "circular",
    }
    payload.update(overrides)
    return payload


def wp_post(**overrides) -> dict:
    """One REST post. Defaults are a typical `bfr` circular with one PDF."""
    post: dict = {
        "id": 9001,
        "type": "bfr",
        "link": "https://www.nrb.org.np/bfr/circular-15/",
        "slug": "circular-15",
        "title": {"rendered": "एकीकृत निर्देशन &#8211; 2082"},
        # Site-local and UTC for the same instant: 5h45m apart, which is what
        # makes the offset derivable rather than assumed.
        "date": "2026-01-02T10:00:00",
        "date_gmt": "2026-01-02T04:15:00",
        "modified": "2026-01-05T12:30:00",
        "status": "publish",
        "categories": [11],
        "acf": {
            "document_file": wp_file(DEVANAGARI_FILE),
            "secondary_file": False,   # WP writes `false`, not absent
            "circular_number": "15/082/83",
            "fiscal_year": "2082/83",
        },
        "content": {"rendered": ""},
    }
    post.update(overrides)
    return post


def sitemap_entry(url: str, lastmod: str | None = "2026-08-13T09:12:34+00:00"):
    return classify_url(
        url=url,
        normalized_url=normalize_url(url),
        source_sitemap="https://www.nrb.org.np/bfr-sitemap1.xml",
        last_modified=lastmod,
    )


def record_for(post: dict, **kwargs):
    return source_from_document(build_document(post, taxonomy=TAXONOMY), **kwargs)


# --------------------------------------------------------------------------- #
# Identity: page_key
# --------------------------------------------------------------------------- #
def test_page_key_is_equal_for_encoded_and_literal_devanagari():
    """The trap: REST returns literal UTF-8, the sitemap percent-encodes.

    Without this, every REST document looks absent from the sitemap and is
    inserted a second time as a sitemap-only row.
    """
    literal = "https://www.nrb.org.np/bfr/आगलागी-सूचना/"
    encoded = (
        "https://www.nrb.org.np/bfr/"
        "%E0%A4%86%E0%A4%97%E0%A4%B2%E0%A4%BE%E0%A4%97%E0%A5%80-"
        "%E0%A4%B8%E0%A5%82%E0%A4%9A%E0%A4%A8%E0%A4%BE/"
    )
    assert page_key(literal) == page_key(encoded)


def test_page_key_ignores_a_trailing_slash():
    assert page_key("https://www.nrb.org.np/bfr/x/") == page_key(
        "https://www.nrb.org.np/bfr/x"
    )


def test_page_key_lowercases_scheme_and_host_and_drops_the_fragment():
    assert page_key("HTTPS://WWW.NRB.ORG.NP/bfr/x/#top") == (
        "https://www.nrb.org.np/bfr/x"
    )


def test_page_key_keeps_the_query_because_wordpress_uses_it():
    assert "p=91" in page_key("https://www.nrb.org.np/?p=91")


def test_page_key_does_not_collapse_two_different_documents():
    assert page_key("https://www.nrb.org.np/bfr/a/") != page_key(
        "https://www.nrb.org.np/bfr/b/"
    )


# --------------------------------------------------------------------------- #
# Identity: files reuse comparison_key
# --------------------------------------------------------------------------- #
def test_file_identity_is_the_phase_3_comparison_key():
    """No second URL normalization exists; this is the one from Phase 3."""
    literal = file_record(build_document(wp_post(), taxonomy=TAXONOMY).attachments[0])
    encoded_post = wp_post(acf={"document_file": wp_file(ENCODED_FILE)})
    encoded = file_record(
        build_document(encoded_post, taxonomy=TAXONOMY).attachments[0]
    )
    assert literal.comparison_key == encoded.comparison_key == comparison_key(
        DEVANAGARI_FILE
    )


def test_file_keeps_nrbs_own_spelling_as_the_download_target():
    """`comparison_key` is for comparing; `source_url` is what you request."""
    encoded_post = wp_post(acf={"document_file": wp_file(ENCODED_FILE)})
    record = file_record(
        build_document(encoded_post, taxonomy=TAXONOMY).attachments[0]
    )
    assert record.source_url == ENCODED_FILE
    assert record.source_url != record.comparison_key


def test_reported_mime_and_size_are_persisted_from_wordpress():
    record = file_record(build_document(wp_post(), taxonomy=TAXONOMY).attachments[0])
    assert record.reported_mime_type == "application/pdf"
    assert record.reported_bytes == 123456
    assert record.type_source == "mime"
    assert record.resource_type == "pdf"
    assert record.wp_attachment_id == 5001


def test_an_on_host_https_file_is_pending_not_blocked():
    record = file_record(build_document(wp_post(), taxonomy=TAXONOMY).attachments[0])
    assert record.fetch_status == FETCH_PENDING
    assert record.blocked_reason is None
    assert not record.is_blocked


def test_the_live_uat_attachment_is_persisted_as_blocked():
    """The three real `http://uat.nrb.org.np/…` links.

    The catalog must record that NRB referenced them while nothing can fetch them.
    """
    post = wp_post(
        acf={
            "document_file": wp_file(
                "http://uat.nrb.org.np/wp-content/uploads/2019/12/report.pdf"
            )
        }
    )
    record = file_record(build_document(post, taxonomy=TAXONOMY).attachments[0])
    assert record.fetch_status == FETCH_BLOCKED_HOST
    # The guard refuses on the scheme first, so that is the reason it reports;
    # `host` is what records WHERE NRB pointed.
    assert record.blocked_reason == "refusing to fetch over plain http"
    assert record.host == "uat.nrb.org.np"
    assert record.is_blocked


def test_the_staging_host_is_blocked_even_over_https():
    """The scheme is not the only thing wrong with the UAT links."""
    post = wp_post(
        acf={"document_file": wp_file("https://uat.nrb.org.np/uploads/report.pdf")}
    )
    record = file_record(build_document(post, taxonomy=TAXONOMY).attachments[0])
    assert record.fetch_status == FETCH_BLOCKED_HOST
    assert "uat.nrb.org.np" in record.blocked_reason


def test_plain_http_on_the_right_host_is_also_blocked():
    """Stricter than Phase 3's `on_allowed_host`, and deliberately so: a fetcher
    refuses plain http, so the catalog must not call the file fetchable."""
    post = wp_post(
        acf={"document_file": wp_file("http://www.nrb.org.np/uploads/x.pdf")}
    )
    record = file_record(build_document(post, taxonomy=TAXONOMY).attachments[0])
    assert record.fetch_status == FETCH_BLOCKED_HOST
    assert "http" in record.blocked_reason


# --------------------------------------------------------------------------- #
# Relationship vocabulary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source,expected",
    [
        ("acf:document_file", REL_PRIMARY),
        ("acf:secondary_file", REL_SECONDARY),
        ("acf:economic_review_volume_pdf_file", REL_ACF),
        ("body_link", REL_BODY_LINK),
    ],
)
def test_relationship_type_vocabulary(source, expected):
    assert relationship_type_for(source) == expected


def test_two_files_keep_nrbs_order_and_field_identity():
    """133 live posts carry a circular plus its annex. Which is which matters."""
    post = wp_post(
        acf={
            "document_file": wp_file("https://www.nrb.org.np/uploads/circular.pdf"),
            "secondary_file": wp_file("https://www.nrb.org.np/uploads/annex.pdf"),
        }
    )
    record = record_for(post)
    assert [link.ordinal for link in record.files] == [0, 1]
    assert [link.relationship_type for link in record.files] == [
        REL_PRIMARY,
        REL_SECONDARY,
    ]
    assert record.files[0].file.filename == "circular.pdf"


def test_a_post_with_no_attachment_yields_no_files():
    """205 live posts have none — a real state, not an error."""
    record = record_for(wp_post(acf=[]))       # `acf` is [] on fieldless posts
    assert record.files == ()
    assert record.title is not None            # the source itself is still real


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def test_the_site_offset_is_derived_from_nrbs_own_pair():
    assert site_utc_offset("2026-01-02T10:00:00", "2026-01-02T04:15:00") == timedelta(
        hours=5, minutes=45
    )


def test_published_at_uses_the_gmt_value_verbatim():
    record = record_for(wp_post())
    assert record.published_at == datetime(2026, 1, 2, 4, 15, tzinfo=timezone.utc)


def test_modified_at_uses_the_offset_this_post_proved():
    """`modified` has no GMT twin, so the offset comes from `date`/`date_gmt`."""
    record = record_for(wp_post())
    assert record.modified_at == datetime(2026, 1, 5, 6, 45, tzinfo=timezone.utc)


def test_a_site_configured_in_utc_is_not_shifted_by_an_assumption():
    """If NRB's WordPress ran on UTC, applying +05:45 to `modified` would be
    5h45m wrong. The derived offset is 0 and nothing moves."""
    post = wp_post(
        date="2026-01-02T04:15:00",
        date_gmt="2026-01-02T04:15:00",
        modified="2026-01-05T06:45:00",
    )
    record = record_for(post)
    assert record.modified_at == datetime(2026, 1, 5, 6, 45, tzinfo=timezone.utc)


def test_nepal_offset_is_the_last_resort_when_there_is_no_gmt_pair():
    got = parse_wp_datetime("2026-01-02T10:00:00", None)
    assert got == datetime(2026, 1, 2, 4, 15, tzinfo=timezone.utc)


def test_a_trailing_z_parses_on_python_310():
    assert parse_wp_datetime(None, "2026-01-02T04:15:00Z") == datetime(
        2026, 1, 2, 4, 15, tzinfo=timezone.utc
    )


def test_unparseable_timestamps_are_none_rather_than_an_exception():
    assert parse_wp_datetime("not a date", None) is None
    assert parse_wp_datetime(None, None) is None
    assert parse_lastmod("") is None


def test_a_bare_date_lastmod_is_read_as_midnight_utc():
    assert parse_lastmod("2026-08-13") == datetime(2026, 8, 13, tzinfo=timezone.utc)


def test_an_offset_bearing_lastmod_is_converted_to_utc():
    assert parse_lastmod("2026-08-13T09:12:34+05:45") == datetime(
        2026, 8, 13, 3, 27, 34, tzinfo=timezone.utc
    )


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_document_type_comes_from_the_category_parent_chain():
    """Posts are filed under `2082-83`, a child of `circulars`."""
    record = record_for(wp_post())
    assert record.document_type == "circular"
    assert record.sections == ("circular",)
    assert "circulars" in (record.classification_source or "")


def test_an_unmapped_catchall_category_leaves_the_type_null():
    """The 5,052 documents in `upload-files`. NULL, never a guess from the title."""
    record = record_for(wp_post(categories=[20]))
    assert record.document_type is None
    assert record.sections == ()


def test_a_post_with_no_categories_leaves_the_type_null():
    record = record_for(wp_post(categories=[]))
    assert record.document_type is None


def test_raw_taxonomy_is_preserved_for_later_reclassification():
    record = record_for(wp_post(categories=[11, 30]))
    assert record.raw_taxonomy["category_ids"] == [11, 30]
    assert record.raw_taxonomy["category_slugs"] == ["2082-83", "notices"]
    assert record.raw_taxonomy["category_names"] == ["2082/83", "Notices"]
    assert len(record.raw_taxonomy["section_evidence"]) == 2


def test_several_sections_are_all_kept_ordered_regulatory_first():
    record = record_for(wp_post(categories=[30, 11]))
    assert record.sections == ("circular", "notice")
    assert record.document_type == "circular"


def test_acf_extras_are_kept_verbatim():
    record = record_for(wp_post())
    assert record.meta["extras"]["circular_number"] == "15/082/83"
    assert record.meta["extras"]["fiscal_year"] == "2082/83"


def test_the_raw_timestamp_strings_survive_so_parsing_is_auditable():
    record = record_for(wp_post())
    assert record.meta["wp_date"] == "2026-01-02T10:00:00"
    assert record.meta["wp_date_gmt"] == "2026-01-02T04:15:00"
    assert record.meta["wp_modified"] == "2026-01-05T12:30:00"


# --------------------------------------------------------------------------- #
# The metadata fingerprint
# --------------------------------------------------------------------------- #
def test_the_hash_is_stable_for_equivalent_metadata():
    assert record_for(wp_post()).metadata_hash == record_for(wp_post()).metadata_hash


def test_the_hash_is_stable_across_devanagari_serialization():
    """`ensure_ascii=False` is pinned: escaping would change every Nepali title's
    digest and rehash the whole catalog."""
    digest = metadata_digest({"title": "एकीकृत निर्देशन"})
    assert digest == metadata_digest({"title": "एकीकृत निर्देशन"})
    assert len(digest) == 64


def test_the_hash_ignores_key_order():
    assert metadata_digest({"a": 1, "b": 2}) == metadata_digest({"b": 2, "a": 1})


@pytest.mark.parametrize(
    "change",
    [
        {"title": {"rendered": "a different title"}},
        {"modified": "2026-02-01T00:00:00"},
        {"categories": [30]},
        {"link": "https://www.nrb.org.np/bfr/circular-16/"},
        {"slug": "circular-16"},
        {"status": "draft"},
        {"acf": {"document_file": wp_file(DEVANAGARI_FILE), "circular_number": "16"}},
    ],
)
def test_meaningful_metadata_changes_move_the_hash(change):
    assert record_for(wp_post()).metadata_hash != record_for(
        wp_post(**change)
    ).metadata_hash


def test_an_attachment_set_change_moves_the_source_hash():
    """Gaining or losing a file is a change to the source, not only to files."""
    two = wp_post(
        acf={
            "document_file": wp_file(DEVANAGARI_FILE),
            "secondary_file": wp_file("https://www.nrb.org.np/uploads/annex.pdf"),
        }
    )
    assert record_for(wp_post()).metadata_hash != record_for(two).metadata_hash


def test_an_encoding_only_attachment_change_does_not_move_the_source_hash():
    """The hash carries `comparison_key`s, so a respelled URL is not a change."""
    encoded = wp_post(acf={"document_file": wp_file(ENCODED_FILE)})
    plain = wp_post(acf={"document_file": wp_file(DEVANAGARI_FILE)})
    assert record_for(encoded).metadata_hash == record_for(plain).metadata_hash


def test_a_file_metadata_change_does_not_move_the_source_hash():
    """A MIME or size edit is a FILE change; counting it twice would report one
    upstream edit as both a source update and a file update."""
    base = wp_post()
    resized = wp_post(
        acf={
            **base["acf"],
            "document_file": wp_file(
                DEVANAGARI_FILE, filesize=999, mime_type="application/vnd.ms-excel"
            ),
        }
    )
    assert record_for(base).metadata_hash == record_for(resized).metadata_hash
    # ...and the file record itself definitely did change.
    assert record_for(base).files[0].file != record_for(resized).files[0].file


def test_the_hash_excludes_sitemap_lastmod():
    """Yoast derives lastmod from post_modified, which IS hashed. Including it
    would make a run without sitemap discovery report every source as changed."""
    with_lastmod = record_for(
        wp_post(), sitemap_lastmod=datetime(2026, 8, 13, tzinfo=timezone.utc)
    )
    without = record_for(wp_post())
    assert with_lastmod.metadata_hash == without.metadata_hash
    assert with_lastmod.sitemap_lastmod is not None   # still persisted


def test_the_hash_has_no_observational_fields_at_all():
    """Belt and braces on the idempotency requirement: assert the payload keys
    rather than trusting that nobody adds `last_seen_at` later."""
    import json

    # Rebuild the digest input by hashing candidate payloads: if any of these
    # names were in the payload, a differing value would change the digest.
    for observational in ("last_seen_at", "last_synced_at", "first_seen_at"):
        base = {"title": "x"}
        assert metadata_digest(base) == metadata_digest(dict(base))
        assert metadata_digest(base) != metadata_digest(
            {**base, observational: json.dumps("2026-01-01")}
        ), "sanity: the digest does respond to added keys"


# --------------------------------------------------------------------------- #
# Sitemap-only sources
# --------------------------------------------------------------------------- #
def test_a_sitemap_only_source_stores_only_what_the_sitemap_states():
    """`economic-review` / `er-article`: 196 live URLs REST cannot serve."""
    record = source_from_sitemap(
        sitemap_entry("https://www.nrb.org.np/economic-review/vol-35/")
    )
    assert record.metadata_status == METADATA_STATUS_SITEMAP_ONLY
    assert record.wp_post_id is None
    assert record.title is None
    assert record.published_at is None
    assert record.modified_at is None
    assert record.document_type is None
    assert record.files == ()
    assert record.sitemap_lastmod == datetime(2026, 8, 13, 9, 12, 34, tzinfo=timezone.utc)


def test_a_sitemap_only_source_infers_the_post_type_from_the_path():
    record = source_from_sitemap(
        sitemap_entry("https://www.nrb.org.np/er-article/some-article/")
    )
    assert record.wp_post_type == "er-article"
    assert record.wp_post_type in CONTENT_POST_TYPES


def test_a_sitemap_only_owner_post_infers_its_owner_code():
    record = source_from_sitemap(sitemap_entry("https://www.nrb.org.np/bfr/x/"))
    assert record.wp_post_type == "bfr"
    assert record.owner == "bfr"


def test_a_federal_office_url_finds_the_owner_in_the_second_segment():
    record = source_from_sitemap(
        sitemap_entry("https://www.nrb.org.np/federal-offices/skt/notice/")
    )
    assert record.wp_post_type == "skt"


def test_an_unknown_path_root_does_not_get_a_guessed_post_type():
    record = source_from_sitemap(sitemap_entry("https://www.nrb.org.np/mystery/x/"))
    assert record.wp_post_type is None


# --------------------------------------------------------------------------- #
# build_source_records
# --------------------------------------------------------------------------- #
def _documents(*posts):
    return [build_document(post, taxonomy=TAXONOMY) for post in posts]


def test_rest_and_sitemap_are_combined_without_duplicating_a_document():
    """The whole point of `url_key`: the same document from both sources is ONE
    record, even though the sitemap spells the URL differently."""
    post = wp_post(link="https://www.nrb.org.np/bfr/आगलागी/")
    entry = sitemap_entry(
        "https://www.nrb.org.np/bfr/"
        "%E0%A4%86%E0%A4%97%E0%A4%B2%E0%A4%BE%E0%A4%97%E0%A5%80/"
    )
    records, warnings = build_source_records(
        _documents(post), {page_key(entry.normalized_url): entry}
    )
    assert len(records) == 1
    assert records[0].metadata_status == METADATA_STATUS_REST
    assert not warnings


def test_the_sitemap_lastmod_is_attached_to_the_matching_rest_record():
    post = wp_post()
    entry = sitemap_entry(post["link"], lastmod="2026-08-01T00:00:00+00:00")
    records, _ = build_source_records(
        _documents(post), {page_key(entry.normalized_url): entry}
    )
    assert records[0].sitemap_lastmod == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_only_sitemap_urls_rest_never_returned_become_sitemap_only_records():
    post = wp_post()
    known = sitemap_entry(post["link"])
    unknown = sitemap_entry("https://www.nrb.org.np/economic-review/vol-35/")
    records, _ = build_source_records(
        _documents(post),
        {
            page_key(known.normalized_url): known,
            page_key(unknown.normalized_url): unknown,
        },
    )
    statuses = {r.page_url: r.metadata_status for r in records}
    assert len(records) == 2
    assert statuses[post["link"]] == METADATA_STATUS_REST
    assert (
        statuses["https://www.nrb.org.np/economic-review/vol-35/"]
        == METADATA_STATUS_SITEMAP_ONLY
    )


def test_duplicate_urls_in_discovery_are_collapsed_with_a_warning():
    """One duplicate in 18,370 rows would otherwise abort a batch on the unique
    index and fail an otherwise good sync."""
    records, warnings = build_source_records(
        _documents(wp_post(id=1), wp_post(id=2))
    )
    assert len(records) == 1
    assert any("duplicate source URL" in w for w in warnings)


def test_a_duplicate_wordpress_identity_is_collapsed_with_a_warning():
    records, warnings = build_source_records(
        _documents(
            wp_post(),
            wp_post(link="https://www.nrb.org.np/bfr/other/", slug="other"),
        )
    )
    assert len(records) == 1
    assert any("duplicate WordPress identity" in w for w in warnings)


def test_a_post_with_no_link_is_skipped_and_reported():
    records, warnings = build_source_records(_documents(wp_post(link=None)))
    assert records == []
    assert any("no URL" in w for w in warnings)


def test_record_order_is_deterministic():
    posts = [
        wp_post(id=1, link="https://www.nrb.org.np/bfr/b/", slug="b"),
        wp_post(id=2, link="https://www.nrb.org.np/bfr/a/", slug="a"),
    ]
    first, _ = build_source_records(_documents(*posts))
    second, _ = build_source_records(_documents(*posts))
    assert [r.url_key for r in first] == [r.url_key for r in second]


# --------------------------------------------------------------------------- #
# Discovery completeness — the deactivation gate
# --------------------------------------------------------------------------- #
def test_a_clean_discovery_is_complete():
    assert Discovery(sitemaps_seen=60).complete is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sitemaps_seen": 60, "errors": ["timeout"]},
        {"sitemaps_seen": 60, "truncated": ["limit=300"]},
        {"sitemaps_seen": 0},                       # sitemap never read
    ],
)
def test_a_flawed_discovery_is_not_complete(kwargs):
    assert Discovery(**kwargs).complete is False


def test_rest_completeness_is_a_separate_question_from_overall_completeness():
    """A sitemap failure must not suppress the corpus-gap rows; a REST failure
    must (otherwise a bounded run invents thousands of sitemap-only stubs)."""
    sitemap_broke = Discovery(sitemaps_seen=0, errors=["sitemap discovery failed"])
    assert sitemap_broke.rest_complete is True
    assert sitemap_broke.complete is False

    rest_broke = Discovery(
        sitemaps_seen=60,
        errors=["limit=300"],
        truncated=["limit=300"],
        rest_truncated=["limit=300"],
    )
    assert rest_broke.rest_complete is False
    assert rest_broke.complete is False


def test_a_post_type_rest_does_not_serve_does_not_make_a_run_incomplete():
    """A measured, permanent gap the sitemap covers — a finding, not a failure."""
    discovery = Discovery(
        sitemaps_seen=60, post_types_not_served=["economic-review", "er-article"]
    )
    assert discovery.complete is True


def test_the_corpus_scope_excludes_forex_and_includes_the_content_types():
    assert "forex" not in DOCUMENT_POST_TYPES
    assert "bfr" in DOCUMENT_POST_TYPES
    for name in CONTENT_POST_TYPES:
        assert name in DOCUMENT_POST_TYPES


# --------------------------------------------------------------------------- #
# The operator report
# --------------------------------------------------------------------------- #
def test_render_sync_shows_the_numbers_an_operator_decides_on():
    counters = _counters()
    counters.update(
        sources_seen=18566, sources_created=18566, files_seen=18256,
        files_created=18256, blocked_files=3, sitemap_only_sources=196,
        sitemaps_seen=60, relationships_created=18389,
    )
    result = SyncResult(
        run_id=7,
        status="completed",
        counters=counters,
        notes={
            "errors": [], "warnings": [], "truncated": [],
            "post_types_not_served": ["economic-review"],
            "skipped_sitemap_page_kinds": {"taxonomy_archive": 359},
            "sitemap_urls_seen": 19480, "sitemap_document_urls": 18567,
            "deactivation_skipped": None,
        },
        counts={"sources": 18566, "files": 18256, "duplicate_comparison_keys": 0},
        discovery_complete=True,
        deactivation_applied=True,
        duration_seconds=312.4,
    )
    text = render_sync(summarize_sync(result))
    assert "18,566" in text and "196" in text and "completed" in text
    assert "taxonomy_archive" in text          # skipped URLs are visible
    assert "economic-review" in text           # the corpus gap is visible
    assert "duplicate comparison keys:   0" in text


def test_render_sync_says_loudly_when_deactivation_was_skipped():
    result = SyncResult(
        run_id=8, status="partial", counters=_counters(),
        notes={"deactivation_skipped": "discovery was incomplete", "errors": [],
               "warnings": [], "truncated": ["limit=300"]},
        discovery_complete=False,
    )
    text = render_sync(summarize_sync(result))
    assert "deactivation was SKIPPED" in text
    assert "limit=300" in text


def test_a_dry_run_is_labelled_in_the_report():
    result = SyncResult(run_id=None, status="completed", counters=_counters(),
                        notes={}, dry_run=True)
    assert "DRY RUN" in render_sync(summarize_sync(result))

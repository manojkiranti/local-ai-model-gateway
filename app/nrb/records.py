"""Discovery output -> catalog rows. Pure: no HTTP, no DB, no clock.

This is the seam between Phase 3's `NRBDocument` (what NRB says) and Phase 4's
tables (what we store). It exists as its own module so every hard decision —
identity, the change fingerprint, how a WordPress timestamp becomes an instant —
is a function of its arguments and can be tested without a database or a network.

Three things worth reading before changing anything here.

**Identity is a decoded URL, not a URL.** `attachments.comparison_key` already
solved this for files: NRB serves the same path as literal Devanagari (REST) and
percent-encoded (the sitemap, and the post's own 302). `page_key` is that same
function plus a trailing-slash strip, because WordPress treats `…/slug/` and
`…/slug` as one page while a file path never ends in a slash. Without it, every
REST document looks absent from the sitemap and gets inserted a second time as a
"sitemap only" row — 18,370 duplicates instead of the 196 genuine gaps.

**The metadata hash answers exactly one question:** did the material metadata
change upstream? So it excludes everything observational (`last_seen_at`,
`last_sync_run_id`, `first_seen_at`) — a second identical sync must produce the
same digest — and it excludes `sitemap_lastmod`, which Yoast derives from
`post_modified`, a field that IS hashed. Including it would mean a run made
without sitemap discovery reported all 18,370 sources as changed. It *does*
include the derived classification (`document_type`, `sections`), so editing
`CATEGORY_SECTIONS` legitimately shows up as updated rows: the stored record
really did change. It does NOT include the per-category evidence strings, which
are prose about our own rules.

**Attachment identity is in the source hash; attachment metadata is not.** The
ordered tuple of `comparison_key`s is hashed, so a post gaining, losing or
reordering a file counts as a source change. A file's MIME, size or filename
changing is a *file* change and moves `files_updated` instead. Hashing both would
double-count one upstream edit.

Timestamps: WordPress publishes `date` (site-local, naive) and `date_gmt` (UTC,
naive) for the same instant, so the site's UTC offset is *derivable* — and is
derived, per post, rather than assumed to be Nepal's +05:45. That matters because
`modified` has no GMT twin: applying an assumed offset to it would shift the
chronology later phases use for amendment ordering by hours if NRB's WordPress
site timezone is ever not what we guessed. Nepal's fixed offset is only the last
resort, and the raw strings are persisted either way.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from ..localtime import NPT
from .attachments import Attachment, comparison_key
from .classify import CONTENT_POST_TYPES, DEPARTMENT_CODES, DiscoveredURL
from .documents import NRBDocument
from .http import check_url
from .models import (
    FETCH_BLOCKED_HOST,
    FETCH_PENDING,
    METADATA_STATUS_REST,
    METADATA_STATUS_SITEMAP_ONLY,
    REL_ACF,
    REL_BODY_LINK,
    REL_PRIMARY,
    REL_SECONDARY,
)

__all__ = [
    "FileLink",
    "FileRecord",
    "SourceRecord",
    "build_source_records",
    "file_record",
    "metadata_digest",
    "page_key",
    "parse_lastmod",
    "parse_wp_datetime",
    "relationship_type_for",
    "site_utc_offset",
    "source_from_document",
    "source_from_sitemap",
]

# `acf:<field>` -> relationship_type. Anything else file-shaped in `acf` is
# `acf`; a body anchor is `body_link`. Kept as a mapping rather than string
# surgery so the vocabulary matches the CHECK constraint by construction.
_RELATIONSHIPS = {
    "acf:document_file": REL_PRIMARY,
    "acf:secondary_file": REL_SECONDARY,
    "body_link": REL_BODY_LINK,
}

# Nepal's fixed UTC offset, taken from `app/localtime.py` rather than written out
# again — that module is the single source of "what time is it in Nepal" for the
# whole codebase. Used ONLY as the last-resort fallback in `parse_wp_datetime`;
# the preferred path derives the offset from NRB's own data.
NEPAL_OFFSET: timedelta = NPT.utcoffset(None) or timedelta(hours=5, minutes=45)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def page_key(url: str) -> str:
    """Comparison identity for a *page* URL.

    `attachments.comparison_key` (percent-decoded path, lowercased scheme/host,
    fragment dropped) plus a trailing-slash strip. The slash strip is the only
    difference and it is specific to pages: WordPress serves `/bfr/slug/` and
    `/bfr/slug` as the same post, and REST, the sitemap and the theme's canonical
    link do not agree on which form to emit.

    Never used to fetch anything — a decoded URL is a comparison form, not a
    request target.
    """
    parts = urlsplit(comparison_key(url))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def relationship_type_for(attachment_source: str) -> str:
    """Which NRB field an attachment came from, in the CHECK's vocabulary."""
    if attachment_source in _RELATIONSHIPS:
        return _RELATIONSHIPS[attachment_source]
    return REL_ACF if attachment_source.startswith("acf:") else REL_BODY_LINK


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def _iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string tolerantly. None on anything unusable.

    `Z` is rewritten because `datetime.fromisoformat` does not accept it on
    Python 3.10 (the version this project pins), and Yoast emits offsets in the
    `+00:00` form that it does accept.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def site_utc_offset(local: Any, gmt: Any) -> timedelta | None:
    """The site's UTC offset, from WordPress's own pair of naive timestamps.

    `date` minus `date_gmt` is the offset NRB's WordPress is configured with —
    measured evidence rather than an assumption about Kathmandu. None when either
    string is missing or already carries an offset (in which case nothing needs
    inferring).
    """
    left, right = _iso(local), _iso(gmt)
    if left is None or right is None:
        return None
    if left.tzinfo is not None or right.tzinfo is not None:
        return None
    return left - right


def parse_wp_datetime(
    local: Any, gmt: Any, *, offset: timedelta | None = None
) -> datetime | None:
    """One WordPress timestamp as an aware UTC instant.

    Order of preference, most to least trustworthy:

      1. `gmt` — already UTC, no interpretation needed.
      2. `local` carrying an explicit offset.
      3. `local` plus `offset` derived from this post's own `date`/`date_gmt`
         pair (this is the `modified` path — it has no GMT twin).
      4. `local` plus Nepal's fixed +05:45, the last resort.
    """
    if (instant := _iso(gmt)) is not None:
        return (
            instant.replace(tzinfo=timezone.utc)
            if instant.tzinfo is None
            else instant.astimezone(timezone.utc)
        )
    if (instant := _iso(local)) is None:
        return None
    if instant.tzinfo is not None:
        return instant.astimezone(timezone.utc)
    return (instant - (offset if offset is not None else NEPAL_OFFSET)).replace(
        tzinfo=timezone.utc
    )


def parse_lastmod(value: Any) -> datetime | None:
    """A sitemap `<lastmod>` as an aware UTC instant.

    Yoast emits a full offset (`2026-08-13T09:12:34+00:00`); a bare date
    (`2026-08-13`) is also legal per the sitemaps.org schema and is read as
    midnight UTC rather than discarded.
    """
    instant = _iso(value)
    if instant is None:
        return None
    return (
        instant.replace(tzinfo=timezone.utc)
        if instant.tzinfo is None
        else instant.astimezone(timezone.utc)
    )


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FileRecord:
    """One `nrb_files` row's worth of upstream truth."""

    comparison_key: str
    source_url: str
    filename: str | None
    reported_mime_type: str | None
    extension: str | None
    resource_type: str
    type_source: str
    reported_bytes: int | None
    wp_attachment_id: int | None
    host: str
    fetch_status: str
    blocked_reason: str | None

    @property
    def is_blocked(self) -> bool:
        return self.fetch_status == FETCH_BLOCKED_HOST


@dataclass(frozen=True)
class FileLink:
    """A file as one source publishes it: which field, and in what position."""

    file: FileRecord
    relationship_type: str
    ordinal: int


@dataclass(frozen=True)
class SourceRecord:
    """One `nrb_sources` row plus the files it publishes.

    Field names match the columns exactly, so the catalog layer maps rather than
    translates. `metadata_hash` is computed at construction (see
    `metadata_digest`) and is the only derived field on here.
    """

    page_url: str
    url_key: str
    metadata_status: str
    metadata_hash: str
    wp_post_id: int | None = None
    wp_post_type: str | None = None
    canonical_url: str | None = None
    slug: str | None = None
    title: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    sitemap_lastmod: datetime | None = None
    owner: str | None = None
    page_kind: str | None = None
    document_type: str | None = None
    classification_source: str | None = None
    sections: tuple[str, ...] = ()
    raw_taxonomy: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    files: tuple[FileLink, ...] = ()

    @property
    def is_sitemap_only(self) -> bool:
        return self.metadata_status == METADATA_STATUS_SITEMAP_ONLY


def metadata_digest(payload: dict[str, Any]) -> str:
    """A stable sha256 over normalized metadata.

    Stability is the whole point, so serialization is pinned: sorted keys,
    no whitespace, `ensure_ascii=False` (a Devanagari title must hash the same
    whether or not the serializer escapes it), UTF-8 bytes. Changing any of
    those rehashes the entire catalog and reports 18k phantom updates.
    """
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def file_record(attachment: Attachment) -> FileRecord:
    """One discovered attachment as a catalog row.

    Fetchability is decided by the SAME guard the fetchers use —
    `http.check_url(..., require_https=True)` — not by a second opinion written
    here. That is stricter than Phase 3's `on_allowed_host` (which tolerates
    `http` so an inventory can *report* one): a plain-http URL on the right host
    is inventoried as blocked, because Phase 5 will refuse to fetch it. The three
    live `http://uat.nrb.org.np/` attachments are blocked on both counts.
    """
    reason = check_url(attachment.url, require_https=True)
    host = (urlsplit(attachment.url).hostname or "").lower()
    return FileRecord(
        comparison_key=comparison_key(attachment.url),
        source_url=attachment.url,
        filename=attachment.filename,
        reported_mime_type=attachment.mime_type,
        extension=attachment.extension,
        resource_type=attachment.resource_type,
        type_source=attachment.type_source,
        reported_bytes=attachment.filesize,
        wp_attachment_id=attachment.wp_id,
        host=host,
        fetch_status=FETCH_PENDING if reason is None else FETCH_BLOCKED_HOST,
        blocked_reason=reason,
    )


def source_from_document(
    document: NRBDocument, *, sitemap_lastmod: datetime | None = None
) -> SourceRecord:
    """A REST-discovered post as a catalog row.

    Nothing is inferred that NRB did not supply: a missing title stays None, an
    unresolved section leaves `document_type` NULL. The 5,052 documents in
    WordPress's `upload-files` catch-all are stored untyped on purpose — a type
    guessed from a Devanagari title would be a fabricated fact in a regulatory
    catalog, and it would be indistinguishable from a real one.
    """
    offset = site_utc_offset(document.published, document.published_gmt)
    published_at = parse_wp_datetime(document.published, document.published_gmt)
    # `modified` has no GMT twin in the REST payload, so it is interpreted with
    # the offset this same post proved above.
    modified_at = parse_wp_datetime(document.modified, None, offset=offset)

    links = tuple(
        FileLink(
            file=file_record(attachment),
            relationship_type=relationship_type_for(attachment.source),
            ordinal=index,
        )
        for index, attachment in enumerate(document.attachments)
    )

    sections = tuple(document.sections)
    document_type = document.primary_section if sections else None
    raw_taxonomy = {
        "category_ids": list(document.category_ids),
        "category_slugs": list(document.category_slugs),
        "category_names": list(document.category_names),
        "section_evidence": list(document.section_evidence),
    }
    meta = {
        # The raw timestamps, so the parsing above is auditable and a future
        # phase can re-derive without re-crawling.
        "wp_date": document.published,
        "wp_date_gmt": document.published_gmt,
        "wp_modified": document.modified,
        "wp_status": document.status,
        "owner_label": document.owner_label,
        "redirects_to_file": document.redirects_to_file,
        "extras": dict(document.extras),
        "warnings": list(document.warnings),
    }

    url = document.url or document.canonical_url or ""
    digest = metadata_digest(
        {
            "wp_post_id": document.post_id,
            "wp_post_type": document.post_type,
            "page_url": url,
            "canonical_url": document.canonical_url,
            "slug": document.slug,
            "title": document.title,
            "owner": document.owner,
            "page_kind": document.page_kind,
            "document_type": document_type,
            "sections": list(sections),
            "category_ids": sorted(document.category_ids),
            "published_at": _iso_or_none(published_at),
            "modified_at": _iso_or_none(modified_at),
            "wp_status": document.status,
            "extras": dict(document.extras),
            # Identity only — a file's own metadata moves `files_updated`.
            "files": [link.file.comparison_key for link in links],
            "metadata_status": METADATA_STATUS_REST,
        }
    )

    return SourceRecord(
        page_url=url,
        url_key=page_key(url),
        metadata_status=METADATA_STATUS_REST,
        metadata_hash=digest,
        wp_post_id=document.post_id,
        wp_post_type=document.post_type,
        canonical_url=document.canonical_url,
        slug=document.slug,
        title=document.title,
        published_at=published_at,
        modified_at=modified_at,
        sitemap_lastmod=sitemap_lastmod,
        owner=document.owner,
        page_kind=document.page_kind,
        document_type=document_type,
        classification_source=(
            document.section_evidence[0] if document.section_evidence else None
        ),
        sections=sections,
        raw_taxonomy=raw_taxonomy,
        meta=meta,
        files=links,
    )


def _inferred_post_type(url: str) -> str | None:
    """The post type a URL's own path implies, or None.

    Only ever a segment NRB itself uses as a post-type root (an owner code or a
    known content type); `/federal-offices/<code>/…` nests it one level deeper.
    Anything else stays None rather than being guessed from a slug.
    """
    segments = [unquote(part) for part in urlsplit(url).path.split("/") if part]
    if not segments:
        return None
    root = segments[0]
    if root in DEPARTMENT_CODES or root in CONTENT_POST_TYPES:
        return root
    if root == "federal-offices" and len(segments) > 1 and segments[1] in DEPARTMENT_CODES:
        return segments[1]
    return None


def source_from_sitemap(entry: DiscoveredURL) -> SourceRecord:
    """A document post that exists in the sitemap but not in the REST API.

    196 URLs measured live: `economic-review` (49) and `er-article` (147) are
    published in the sitemap and are not REST-registered post types, so they 404.
    They are persisted so the corpus gap is a queryable row rather than a
    footnote in a report nobody re-reads.

    Only what the sitemap actually states is stored. No title, no publication
    date, no attachment, no document type — the sitemap carries none of those,
    and inventing them would make a stub indistinguishable from a real record.
    `wp_post_id` stays NULL, so this row is identified by `url_key` alone until
    (and if) REST ever serves it, at which point the sync upgrades this row in
    place instead of inserting a duplicate.
    """
    lastmod = parse_lastmod(entry.last_modified)
    post_type = _inferred_post_type(entry.normalized_url)
    digest = metadata_digest(
        {
            "page_url": entry.normalized_url,
            "wp_post_type": post_type,
            "owner": entry.department,
            "page_kind": entry.page_kind,
            "metadata_status": METADATA_STATUS_SITEMAP_ONLY,
        }
    )
    return SourceRecord(
        page_url=entry.normalized_url,
        url_key=page_key(entry.normalized_url),
        metadata_status=METADATA_STATUS_SITEMAP_ONLY,
        metadata_hash=digest,
        wp_post_type=post_type,
        owner=entry.department,
        page_kind=entry.page_kind,
        sitemap_lastmod=lastmod,
        meta={
            "sitemap_source": entry.source_sitemap,
            "sitemap_lastmod_raw": entry.last_modified,
            "classify_evidence": entry.evidence,
        },
    )


def build_source_records(
    documents: list[NRBDocument],
    sitemap_documents: dict[str, DiscoveredURL] | None = None,
) -> tuple[list[SourceRecord], list[str]]:
    """Every catalog row one discovery pass implies. Returns `(records, warnings)`.

    Order: REST documents (already deterministically ordered by the discovery
    layer), then the sitemap-only remainder sorted by `url_key`. Two runs over the
    same corpus therefore produce identical batches.

    Duplicates are collapsed HERE rather than left for Postgres to reject, because
    both `url_key` and `(wp_post_type, wp_post_id)` are unique: one duplicate in
    18,370 rows would abort a batch and fail an otherwise good sync. The first
    occurrence wins and the collision is a warning, so it is visible rather than
    silently deduplicated.
    """
    sitemap_documents = sitemap_documents or {}
    records: list[SourceRecord] = []
    warnings: list[str] = []
    seen_keys: set[str] = set()
    seen_posts: set[tuple[str | None, int]] = set()

    for document in documents:
        url = document.url or document.canonical_url or ""
        if not url:
            # `url_key` would be meaningless and every such post would collide
            # with every other. Phase 3 measured zero of these; a non-zero count
            # is a finding.
            warnings.append(
                f"skipped a post with no URL (post_id={document.post_id})"
            )
            continue
        key = page_key(url)
        if key in seen_keys:
            warnings.append(f"duplicate source URL in discovery: {url}")
            continue
        if document.post_id is not None:
            identity = (document.post_type, document.post_id)
            if identity in seen_posts:
                warnings.append(
                    f"duplicate WordPress identity in discovery: "
                    f"{document.post_type}#{document.post_id}"
                )
                continue
            seen_posts.add(identity)
        seen_keys.add(key)
        entry = sitemap_documents.get(key)
        records.append(
            source_from_document(
                document,
                sitemap_lastmod=parse_lastmod(entry.last_modified) if entry else None,
            )
        )

    for key in sorted(set(sitemap_documents) - seen_keys):
        records.append(source_from_sitemap(sitemap_documents[key]))

    return records, warnings

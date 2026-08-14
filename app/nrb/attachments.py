"""Attachment discovery for an NRB document post. Pure — no HTTP, no DB.

Two sources, in descending order of trustworthiness, both from the same REST
payload so neither costs an extra request:

  1. **ACF file fields** — `acf.document_file` and `acf.secondary_file` are
     WordPress attachment objects carrying `url`, `filename`, `filesize` and an
     authoritative `mime_type` recorded at upload. This is the real answer for
     ~92% of posts.
  2. **Anchors in `acf`-less `content.rendered`** — the rendered post body. Only
     22 of 2,786 sampled posts have an `<a>` in their body at all, but those are
     real documents and a body link is the only place they appear.

Measured over 2,786 posts (2026-08-13): 0 attachments 204 (7.3%), exactly 1
2,512 (90.2%), 2 70 (2.5%). MIME: PDF 2,228, xlsx 341, xls 55, jpeg 21, doc 4,
docx 3 — **so "everything is a PDF" is false**, and 100% of attachment URLs were
on `www.nrb.org.np` (no CDN, no off-host).

Three deliberate refusals, each of which would otherwise put a wrong fact into a
regulatory corpus:

  * **Link text never determines type.** A body anchor reading "PDF" pointing at
    an `.xlsx` is typed from the URL, and `mime_type` (when ACF gave us one) wins
    over both. `resource_type` records where its answer came from via
    `type_source`, so Phase 4 can decide what it trusts.
  * **A URL-derived type is not a verified type.** `.pdf` in a path means the
    filename says PDF, nothing more; nothing here fetches a byte of the file.
  * **Nothing is inferred from a Devanagari slug or a title.**

`content.rendered` is parsed with the stdlib `html.parser`, deliberately: the
venv has beautifulsoup4/lxml, but only as *docling* transitive dependencies,
which `CLAUDE.md` requires stay out of the API image. `html.parser` is tolerant
of the malformed markup WordPress emits and adds no dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .http import check_url, normalize_url

__all__ = [
    "Attachment",
    "RESOURCE_TYPES",
    "comparison_key",
    "extract_attachments",
    "extract_body_links",
    "resource_type_for",
]

RESOURCE_TYPES = ("pdf", "spreadsheet", "document", "archive", "image", "web", "unknown")

# Extension -> resource type. Kept separate from `documents.py`'s section
# vocabulary: what a file IS and what a document MEANS are different questions.
_EXTENSIONS = {
    "pdf": "pdf",
    "doc": "document", "docx": "document", "rtf": "document", "odt": "document",
    "xls": "spreadsheet", "xlsx": "spreadsheet", "xlsm": "spreadsheet",
    "csv": "spreadsheet", "ods": "spreadsheet",
    "zip": "archive", "rar": "archive", "7z": "archive", "gz": "archive",
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "webp": "image", "svg": "image", "bmp": "image", "tif": "image", "tiff": "image",
    "ppt": "document", "pptx": "document",
    "htm": "web", "html": "web",
}

# MIME -> resource type, for the authoritative ACF value.
_MIME_PREFIXES = {
    "application/pdf": "pdf",
    "application/msword": "document",
    "application/rtf": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml": "document",
    "application/vnd.openxmlformats-officedocument.presentationml": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.ms-excel": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml": "spreadsheet",
    "application/vnd.oasis.opendocument.spreadsheet": "spreadsheet",
    "text/csv": "spreadsheet",
    "application/zip": "archive",
    "application/x-rar": "archive",
    "image/": "image",
    "text/html": "web",
}

# ACF fields known to hold a single file object, in the order they are reported.
# `document_file` first because it is the post's primary document — the one the
# post URL redirects to.
_KNOWN_FILE_FIELDS = ("document_file", "secondary_file")

# ACF keys that hold images used as page furniture rather than published
# documents. Excluded from attachments but named here so the exclusion is a
# reviewable list, not a silent filter.
_FURNITURE_FIELDS = frozenset({"banner_details", "banner_image", "icon"})


def comparison_key(url: str) -> str:
    """A form in which two spellings of the same NRB file compare equal.

    Measured on the live site: the REST API returns
    `…/uploads/2021/11/आगलागी-….pdf` with literal UTF-8 in the path, while the
    post URL's 302 `Location` returns the *same file* as
    `…/uploads/2021/11/%E0%A4%86%E0%A4%97%E0%A4%B2%E0%A4%BE%E0%A4%97%E0%A5%80-….pdf`.
    Both are correct and both fetch the same bytes, so comparing raw strings
    reports a phantom disagreement — and, worse, would double-count the file in a
    Phase 4 corpus.

    Percent-decoding the path is what makes them equal. This is a COMPARISON key
    only: `Attachment.url` keeps the spelling NRB actually published, because that
    is the string a downloader should use.
    """
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), (parts.hostname or "").lower(),
         unquote(parts.path), parts.query, "")
    )


@dataclass(frozen=True)
class Attachment:
    """One discovered downloadable resource.

    `url` is normalized for deduplication; `href` keeps exactly what NRB
    published (relative or absolute) so a normalization bug stays visible.
    """

    url: str                     # resolved + normalized absolute URL
    href: str                    # the original href / acf url, verbatim
    source: str                  # "acf:<field>" | "body_link"
    filename: str | None
    extension: str | None        # lowercased, no dot; None if the URL has none
    resource_type: str
    type_source: str             # "mime" | "extension" | "none"
    link_text: str | None        # anchor text, or the ACF title
    mime_type: str | None        # ACF's recorded MIME. NOT verified by us.
    filesize: int | None         # ACF's recorded byte size
    on_allowed_host: bool
    host_reason: str | None      # why not, when off-host
    wp_id: int | None = None     # WordPress attachment post id, when ACF gave one
    uploaded: str | None = None  # ACF attachment date, when present

    @property
    def dedup_key(self) -> str:
        """Identity for deduplication — see `comparison_key`."""
        return comparison_key(self.url)


# --------------------------------------------------------------------------- #
# Typing
# --------------------------------------------------------------------------- #
def _extension_of(url: str) -> str | None:
    """The path's extension, decoded. Query strings and fragments never count.

    Percent-decoded first: NRB publishes `…/%e0%a4%a8….pdf` and an extension
    hidden behind an escape is still an extension.
    """
    path = unquote(urlsplit(url).path)
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if "." not in last:
        return None
    ext = last.rsplit(".", 1)[-1].lower()
    # Guard against a dotted directory or a version-looking suffix being read as
    # a file type: real extensions here are short and alphanumeric.
    if not ext.isalnum() or not 1 <= len(ext) <= 5:
        return None
    return ext


def resource_type_for(url: str, mime_type: str | None) -> tuple[str, str]:
    """`(resource_type, type_source)` for one resource.

    MIME wins when WordPress recorded one — it was determined at upload from the
    bytes, which is strictly better evidence than a filename. Otherwise the URL's
    extension. Never the link text: see the module docstring.
    """
    if mime_type:
        lowered = mime_type.strip().lower()
        for prefix, kind in _MIME_PREFIXES.items():
            if lowered.startswith(prefix):
                return kind, "mime"
        return "unknown", "mime"
    extension = _extension_of(url)
    if extension is None:
        return "unknown", "none"
    return _EXTENSIONS.get(extension, "unknown"), "extension"


def _filename_of(url: str, acf_filename: str | None) -> str | None:
    if acf_filename:
        return acf_filename
    last = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]
    return last or None


def _build(url: str, href: str, source: str, *, link_text: str | None = None,
           acf: dict | None = None) -> Attachment | None:
    """Assemble one Attachment, or None if the URL is unusable."""
    if not url:
        return None
    host_reason = check_url(url)
    normalized = normalize_url(url) if host_reason is None else url.strip()
    acf = acf or {}
    mime = acf.get("mime_type")
    mime = mime.strip() if isinstance(mime, str) and mime.strip() else None
    resource_type, type_source = resource_type_for(normalized, mime)
    filesize = acf.get("filesize")
    wp_id = acf.get("ID") if isinstance(acf.get("ID"), int) else acf.get("id")
    return Attachment(
        url=normalized,
        href=href,
        source=source,
        filename=_filename_of(normalized, acf.get("filename")),
        extension=_extension_of(normalized),
        resource_type=resource_type,
        type_source=type_source,
        link_text=link_text or (acf.get("title") if isinstance(acf.get("title"), str) else None) or None,
        mime_type=mime,
        filesize=filesize if isinstance(filesize, int) else None,
        on_allowed_host=host_reason is None,
        host_reason=host_reason,
        wp_id=wp_id if isinstance(wp_id, int) else None,
        uploaded=acf.get("date") if isinstance(acf.get("date"), str) else None,
    )


# --------------------------------------------------------------------------- #
# HTML body links (stdlib parser)
# --------------------------------------------------------------------------- #
class _AnchorParser(HTMLParser):
    """Collect `(href, text)` for every anchor, in document order.

    `convert_charrefs` (the default) resolves `&amp;` in text for us. Malformed
    markup is tolerated: unclosed tags simply mean the text run continues, which
    is the WordPress house style.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        self._flush()
        for name, value in attrs:
            if name.lower() == "href" and value:
                self._href = value.strip()
                break

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def _flush(self) -> None:
        if self._href is not None:
            text = " ".join("".join(self._text).split())
            self.anchors.append((self._href, text))
        self._href = None
        self._text = []

    def close(self) -> None:      # an unclosed final <a> still counts
        super().close()
        self._flush()


def extract_body_links(html: str, base_url: str) -> list[tuple[str, str, str]]:
    """`(resolved_url, href, link_text)` for every anchor in a post body.

    Relative hrefs are resolved against `base_url`; `#fragment`-only,
    `mailto:`, `tel:` and `javascript:` hrefs are dropped as non-resources.
    """
    parser = _AnchorParser()
    parser.feed(html)
    parser.close()

    out: list[tuple[str, str, str]] = []
    for href, text in parser.anchors:
        if not href or href.startswith("#"):
            continue
        scheme = urlsplit(href).scheme.lower()
        if scheme and scheme not in ("http", "https"):
            continue           # mailto:, tel:, javascript:, data:
        try:
            resolved = urljoin(base_url, href)
        except ValueError:
            continue           # malformed href — dropped, and counted by caller
        out.append((resolved, href, text))
    return out


# --------------------------------------------------------------------------- #
# The extractor
# --------------------------------------------------------------------------- #
def _is_wp_file_object(value: object) -> bool:
    """A WordPress attachment object, judged on shape rather than field name.

    `acf.document_file` and `acf.secondary_file` are the two known fields, but NRB
    also has one-off ones (`economic_review_volume_pdf_file`). Recognising the
    shape catches those without guessing at names — and requiring a `url` means a
    stray dict cannot become a phantom attachment.
    """
    return isinstance(value, dict) and isinstance(value.get("url"), str) and bool(value["url"])


def extract_attachments(post: dict, *, base_url: str) -> tuple[list[Attachment], list[str]]:
    """Every attachment on one REST post, deduplicated. Returns `(attachments, warnings)`.

    Order is deterministic: `document_file`, `secondary_file`, then any other
    file-shaped ACF field sorted by field name, then body links in document
    order. Deduplication is on the normalized URL and keeps the FIRST occurrence,
    so the primary ACF record wins over a body link to the same file — the ACF
    one carries the MIME type and size.
    """
    attachments: list[Attachment] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def add(attachment: Attachment | None) -> None:
        if attachment is None:
            return
        key = attachment.dedup_key
        if key in seen:
            warnings.append(f"duplicate attachment reference: {attachment.url}")
            return
        seen.add(key)
        attachments.append(attachment)

    acf = post.get("acf")
    # ACF is `{}`/`[]` on posts with no custom fields — 48 of 2,786 sampled posts
    # returned a list, so this is not a defensive hypothetical.
    if isinstance(acf, dict):
        handled: set[str] = set()
        for field_name in _KNOWN_FILE_FIELDS:
            value = acf.get(field_name)
            handled.add(field_name)
            if value in (None, False, "", [], {}):
                continue          # WP writes `false` for an unset file field
            if not _is_wp_file_object(value):
                warnings.append(f"acf.{field_name} is not a file object ({type(value).__name__})")
                continue
            add(_build(value["url"], value["url"], f"acf:{field_name}", acf=value))

        for field_name in sorted(set(acf) - handled):
            if field_name in _FURNITURE_FIELDS:
                continue
            value = acf[field_name]
            if _is_wp_file_object(value):
                add(_build(value["url"], value["url"], f"acf:{field_name}", acf=value))
            elif isinstance(value, list):
                for item in value:
                    if _is_wp_file_object(item):
                        add(_build(item["url"], item["url"], f"acf:{field_name}", acf=item))
    elif acf not in (None, [], {}):
        warnings.append(f"acf is {type(acf).__name__}, not an object")

    body = post.get("content")
    rendered = body.get("rendered") if isinstance(body, dict) else None
    if isinstance(rendered, str) and "<a" in rendered:
        for resolved, href, text in extract_body_links(rendered, base_url):
            attachment = _build(resolved, href, "body_link", link_text=text or None)
            if attachment is None:
                warnings.append(f"unusable href: {href!r}")
                continue
            # A body anchor may be an ordinary cross-link rather than a file. Only
            # count it when the URL itself says it is a downloadable resource;
            # link text is never allowed to decide (see the module docstring).
            if attachment.resource_type in ("unknown", "web"):
                continue
            add(attachment)

    return attachments, warnings

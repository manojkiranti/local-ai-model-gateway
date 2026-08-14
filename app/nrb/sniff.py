"""What a downloaded file actually IS, from its first bytes. Pure — no I/O.

Phase 3 recorded what NRB *claims* a file is (`acf.mime_type`, 99.6% coverage) and
Phase 4 stored that claim. Neither verified it, and the docstrings said so. This is
the verification, and it exists for one specific failure rather than as hygiene:

**WordPress answers a missing file with a 200 and a ~100 KB HTML error page.**
Phase 2 already measured that on this site (a 404 returns a themed HTML page). So
a fetcher that trusts the status code and the promised MIME will happily store a
themed error page as `circular-15.pdf`, and Phase 6 will then extract its
navigation menu as the text of a regulatory circular. A wrong document that parses
is far worse than a recorded gap, so an HTML body where a document was promised is
a **failure**, not a file.

Deliberately signature-based and stdlib-only. `python-magic` would mean libmagic in
the API image for a corpus that is 91% PDF and 8% Office (measured); `mimetypes`
only maps extensions, which is the claim we are trying to check. The table below is
short because the corpus is narrow — and `evidence` names the rule that fired, so a
disagreement is traceable rather than arguable.

Two honest limits, both stated rather than papered over:

  * **OLE2 (`.xls`/`.doc`) is identified as a family, not a format.** Telling a
    legacy Word file from a legacy Excel file means walking the OLE directory
    stream; that is a parser, and Phase 6 owns parsers. `application/x-ole-storage`
    with `family == "office_legacy"` is as far as bytes-at-the-front honestly go.
  * **A ZIP container's flavour is read from the first local file names** (`xl/`,
    `word/`, `ppt/`), not from the central directory at the end of the file. That
    is a heuristic on a 4 KB head; when it does not fire the answer degrades to
    `application/zip` rather than guessing.
"""

from __future__ import annotations

__all__ = [
    "FAMILIES",
    "family_for",
    "is_documentish",
    "sniff",
]

# How many leading bytes any rule here may look at. Big enough for a ZIP's first
# few local headers, small enough to be a cheap read.
HEAD_BYTES = 4096

# Coarse classes, matching `attachments.RESOURCE_TYPES` where they overlap so a
# sniffed answer can be compared with NRB's claimed `resource_type` directly.
FAMILIES = (
    "pdf",
    "spreadsheet",
    "document",
    "office_legacy",   # OLE2: .xls or .doc, indistinguishable from the header
    "archive",
    "image",
    "web",             # HTML/XML — the soft-404 shape
    "text",
    "unknown",
)

_MIME_FAMILIES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "application/vnd.oasis.opendocument.spreadsheet": "spreadsheet",
    "application/vnd.oasis.opendocument.text": "document",
    "application/x-ole-storage": "office_legacy",
    "application/rtf": "document",
    "application/zip": "archive",
    "application/x-rar-compressed": "archive",
    "application/gzip": "archive",
    "application/x-7z-compressed": "archive",
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/bmp": "image",
    "image/tiff": "image",
    "image/webp": "image",
    "text/html": "web",
    "application/xml": "web",
    "text/plain": "text",
    "text/csv": "spreadsheet",
    "application/octet-stream": "unknown",
}

# (offset, magic, mime, evidence). Order matters only where one prefix could
# shadow another; each entry is exact, so the table is read in order and the first
# match wins.
_SIGNATURES: tuple[tuple[int, bytes, str, str], ...] = (
    (0, b"%PDF-", "application/pdf", "%PDF- header"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage", "OLE2 header"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png", "PNG header"),
    (0, b"\xff\xd8\xff", "image/jpeg", "JPEG SOI"),
    (0, b"GIF87a", "image/gif", "GIF87a header"),
    (0, b"GIF89a", "image/gif", "GIF89a header"),
    (0, b"BM", "image/bmp", "BMP header"),
    (0, b"II*\x00", "image/tiff", "TIFF (little-endian) header"),
    (0, b"MM\x00*", "image/tiff", "TIFF (big-endian) header"),
    (0, b"{\\rtf", "application/rtf", "RTF header"),
    (0, b"Rar!\x1a\x07", "application/x-rar-compressed", "RAR header"),
    (0, b"\x1f\x8b", "application/gzip", "gzip header"),
    (0, b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed", "7z header"),
)

# Inside a ZIP head: which member names betray which OOXML flavour.
_ZIP_FLAVOURS: tuple[tuple[bytes, str, str], ...] = (
    (b"xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     "ZIP containing 'xl/'"),
    (b"word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
     "ZIP containing 'word/'"),
    (b"ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
     "ZIP containing 'ppt/'"),
    (b"mimetypeapplication/vnd.oasis.opendocument.spreadsheet",
     "application/vnd.oasis.opendocument.spreadsheet", "ODF spreadsheet mimetype entry"),
    (b"mimetypeapplication/vnd.oasis.opendocument.text",
     "application/vnd.oasis.opendocument.text", "ODF text mimetype entry"),
)

# Markup openings. Checked after a leading-whitespace strip because WordPress
# emits a blank line or a BOM before `<!DOCTYPE html>` often enough to matter.
_MARKUP = (
    (b"<!doctype html", "text/html", "<!DOCTYPE html>"),
    (b"<html", "text/html", "<html> element"),
    (b"<head", "text/html", "<head> element"),
    (b"<body", "text/html", "<body> element"),
    (b"<br", "text/html", "HTML fragment"),
    (b"<!--", "text/html", "HTML comment"),
    (b"<?xml", "application/xml", "XML declaration"),
)

_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")

# Control bytes a text file legitimately contains: tab, newline, vertical tab,
# form feed, carriage return. Everything else below 0x20 is a binary tell.
_TEXT_CONTROLS = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D})


def sniff(head: bytes) -> tuple[str, str]:
    """`(mime, evidence)` for a file's leading bytes.

    `application/octet-stream` means "no rule matched", which is a real answer and
    NOT a failure: NRB publishes a handful of files whose type nothing at the front
    identifies. The caller decides what to do about it; this function never guesses
    from a filename, because the filename is the claim under test.
    """
    if not head:
        return "application/octet-stream", "empty body"

    for offset, magic, mime, evidence in _SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return mime, evidence

    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        for needle, mime, evidence in _ZIP_FLAVOURS:
            if needle in head:
                return mime, evidence
        # A real ZIP whose flavour is not in the first 4 KB. Honest answer.
        return "application/zip", "ZIP header, flavour not in the head"

    if head[:12].startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp", "RIFF/WEBP header"

    # Text-ish: strip a BOM and leading whitespace before looking for markup, then
    # decide text vs binary on decodability rather than on a byte histogram.
    body = head
    for bom in _BOMS:
        if body.startswith(bom):
            body = body[len(bom) :]
            break
    stripped = body.lstrip()
    lowered = stripped[:512].lower()
    for needle, mime, evidence in _MARKUP:
        if lowered.startswith(needle):
            return mime, evidence

    if b"\x00" in head[:1024]:
        return "application/octet-stream", "binary (NUL byte in the first 1 KB)"
    # A NUL is the obvious binary tell, but plenty of binary formats have none in
    # their first kilobyte and still decode as UTF-8 by accident. Control characters
    # that no text file uses are the second tell: >5% of them means these bytes are
    # not prose, whatever `.decode()` says. (Tab/newline/CR and friends excluded, of
    # course.)
    sample = head[:1024]
    controls = sum(1 for byte in sample if byte < 0x20 and byte not in _TEXT_CONTROLS)
    if sample and controls / len(sample) > 0.05:
        return "application/octet-stream", "binary (control characters in the head)"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A truncated multi-byte character at the cap is not evidence of binary.
        try:
            head[:-4].decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream", "not decodable as UTF-8"
    # `<` anywhere early in otherwise-plain text is treated as markup: a themed
    # error page that starts with a script tag or a stray newline still reads as
    # HTML, and that is the case this module exists for.
    if b"<" in lowered and (b"</" in lowered or b"<div" in lowered):
        return "text/html", "markup tags in decodable text"
    return "text/plain", "decodable as UTF-8, no signature"


def family_for(mime: str | None) -> str:
    """The coarse class of a MIME type, in `FAMILIES`.

    Prefix-tolerant for `image/*` and `text/*` so an unlisted image subtype does
    not read as `unknown`; anything genuinely unrecognised is `unknown`, which is
    the honest answer and not a synonym for "broken".
    """
    if not mime:
        return "unknown"
    lowered = mime.split(";")[0].strip().lower()
    if lowered in _MIME_FAMILIES:
        return _MIME_FAMILIES[lowered]
    if lowered.startswith("image/"):
        return "image"
    if lowered in ("application/msword", "application/vnd.ms-word"):
        return "office_legacy"
    if lowered in ("application/vnd.ms-excel", "application/excel"):
        return "office_legacy"
    if lowered.startswith("text/"):
        return "text"
    return "unknown"


def is_documentish(family: str) -> bool:
    """Whether this family is something a document corpus would want to keep.

    `web` and `unknown` are excluded on purpose: those are the two shapes a soft-404
    or a truncated transfer arrives in.
    """
    return family in ("pdf", "spreadsheet", "document", "office_legacy", "image", "text")

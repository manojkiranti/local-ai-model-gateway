"""Per-page PDF provenance: does this page carry a font, and is it a scan?

Local files only — no DB, no network, no subprocess. Never raises.

WHY THIS EXISTS
---------------
Native-2 answers "is this text trustworthy". It reads the TEXT and nothing else,
by design (`routing.py`), and on that evidence it is right about NRB's scans: a
150-300 dpi page carrying a hidden scanner text layer (`Htqft Hfrqq aFrerr{ hrn`)
is neither English nor Unicode and does look glyph-mapped, so
`legacy_font_suspected` is the correct verdict.

What that verdict does NOT say is *why* the text is untrustworthy, and the two
causes need opposite treatment. An embedded Preeti font means the bytes are a
glyph mapping and npttf2utf recovers them deterministically. A scan means there
is no mapping to invert — the bytes are one OCR engine's guess, and running a
font converter over them produces confident nonsense. The Phase 6B OCR spike
measured that split on the frozen `>=0.80` queue: of 56 routed documents, 8 PDFs
embed no font at all, and **all 4 documents the converter left `unresolved` are
in that 8** (`docs/nrb/phase6b-ocr-spike.md` §1).

So provenance is a routing input, not a classifier fix. Nothing here changes
native-2, and nothing here is a threshold.

WHY pypdf AND NOT pdffonts/pdfimages
------------------------------------
The spike used the Poppler CLI diagnostically. It is not needed in production:
pypdf is already a `requirements.txt` dependency (`app/files/documents.py` reads
every PDF with it), and a page's `/Resources` gives both signals directly —
`/Font` with a `/FontDescriptor` carrying `/FontFile*`, and `/XObject` entries of
`/Subtype /Image`. Verified against the spike's own findings on the seven
diagnostic blobs, including the two hard ones (`7820b1f49fc1`, stripped font
names; `e08988860534`, page 1 a scan inside a font-embedded document).

That keeps the worker free of a system package, free of a subprocess timeout
policy, and free of a "the binary is missing" failure mode.

TWO RULES THIS MODULE EXISTS TO KEEP HONEST
-------------------------------------------
1. **A stripped font name is not a scan.** `7820b1f49fc1`'s producer emitted
   `/CIDFont+F1 … /CIDFont+F6` — no recognisable family — and its deterministic
   conversion is good. Embedded font OBJECTS are the eligibility signal;
   recognisable names (`is_legacy_font_name`) are supporting evidence only.
2. **A page is not judged on the document.** `e08988860534` page 1 is a 300 dpi
   scan and pages 2-50 embed real Preeti. Provenance is recorded per page so the
   router can send one page to OCR and the next to the converter.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..files.documents import MAX_PDF_PAGES

__all__ = [
    "DocumentProvenance",
    "LEGACY_FONT_HINTS",
    "MAX_XOBJECT_DEPTH",
    "PageProvenance",
    "is_legacy_font_name",
    "read_pdf_provenance",
]

# The keys that mean "the glyph program is inside this file". Any one of them on
# a font descriptor makes the font embedded: Type1/CFF, TrueType and OpenType
# respectively.
_EMBED_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")

# A Form XObject can carry its own `/Resources`, and a scanner's output routinely
# wraps the page image in one. Recursion is bounded and cycle-guarded — a PDF is
# a graph and a malformed one can point at itself.
MAX_XOBJECT_DEPTH = 3

# Font families NRB actually publishes Devanagari-as-latin in, observed in the
# fetched corpus. **Supporting evidence only** — see rule 1 in the module
# docstring. Matched case-insensitively against a substring of `/BaseFont`, which
# arrives subset-prefixed (`ABCDEE+Preeti`, `FNNOBH+Preeti`).
LEGACY_FONT_HINTS = (
    "preeti",
    "kantipur",
    "sagarmatha",
    "fontasy",
    "himali",
    "pcs nepali",
    "bishall",
)

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def is_legacy_font_name(name: str) -> bool:
    """Is this `/BaseFont` a known legacy Nepali family?

    Never a REQUIREMENT for conversion eligibility and never a veto: it exists so
    a report can say "this page embeds Preeti" rather than "this page embeds
    something", and so a page that references a legacy font WITHOUT embedding it
    (relying on the reader's system font — legal, and the bytes are still glyph
    mapped) is still routed to the converter.
    """
    lowered = _SUBSET_PREFIX.sub("", name.lstrip("/")).lower()
    return any(hint in lowered for hint in LEGACY_FONT_HINTS)


@dataclass(frozen=True)
class PageProvenance:
    """What one page's resource dictionary declares. Facts only, no route."""

    page_number: int
    fonts: int
    embedded_fonts: int
    legacy_font_names: tuple[str, ...]
    font_names: tuple[str, ...]
    images: int
    largest_image_pixels: int

    @property
    def has_embedded_font(self) -> bool:
        """The page carries a glyph program. Enough, on its own, to keep
        deterministic conversion eligible — see rule 1."""
        return self.embedded_fonts > 0

    @property
    def has_legacy_font_name(self) -> bool:
        return bool(self.legacy_font_names)

    @property
    def has_image(self) -> bool:
        return self.images > 0

    @property
    def scan_backed(self) -> bool:
        """No glyph program of its own, and pixels on the page.

        Deliberately NOT "has an image": a circular with an embedded Preeti font
        and the bank's logo has an image too, and `268bcfe86d03` (logo 260x167,
        embedded Preeti) is exactly that shape.
        """
        return self.images > 0 and self.embedded_fonts == 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentProvenance:
    """Every page's provenance, in page order.

    `error` is set when the file could not be opened at all. `pages` is then
    empty, and the router must fail CLOSED (keep the native text) rather than
    guess a route — an unopenable PDF is not evidence of a scan.
    """

    pages: tuple[PageProvenance, ...]
    page_count: int
    truncated: int
    error: str | None = None

    def page(self, number: int) -> PageProvenance | None:
        """1-indexed lookup, or None when this page was never read."""
        if 1 <= number <= len(self.pages):
            return self.pages[number - 1]
        return None

    @property
    def embedded_font_pages(self) -> int:
        return sum(1 for p in self.pages if p.has_embedded_font)

    @property
    def scan_pages(self) -> int:
        return sum(1 for p in self.pages if p.scan_backed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_count": self.page_count,
            "truncated": self.truncated,
            "error": self.error,
            "embedded_font_pages": self.embedded_font_pages,
            "scan_pages": self.scan_pages,
            "pages": [p.as_dict() for p in self.pages],
        }


def _resolve(obj: Any) -> Any:
    """Follow an indirect reference. pypdf returns either kind from a dict get."""
    getter = getattr(obj, "get_object", None)
    return getter() if callable(getter) else obj


def _font_is_embedded(font: Any) -> bool:
    """Does this font object carry its own glyph program?

    A `/Type0` composite font holds nothing itself — the descriptor lives on its
    descendant, which is where NRB's subsetted CID fonts (`/CIDFont+F1`) keep
    theirs. A `/Type3` font's glyphs ARE content streams in the file, so it is
    embedded by construction and has no `/FontFile` to find.
    """
    subtype = str(font.get("/Subtype", "")) if hasattr(font, "get") else ""
    if subtype == "/Type3":
        return True
    descendants = [font]
    if subtype == "/Type0":
        descendants = [_resolve(d) for d in (_resolve(font.get("/DescendantFonts")) or [])]
    for descendant in descendants:
        if not hasattr(descendant, "get"):
            continue
        descriptor = _resolve(descendant.get("/FontDescriptor"))
        if descriptor is not None and any(k in descriptor for k in _EMBED_KEYS):
            return True
    return False


def _walk(resources: Any, seen: set[int], depth: int, acc: dict[str, Any]) -> None:
    """Accumulate fonts and images from one `/Resources`, recursing into Forms."""
    if resources is None or depth > MAX_XOBJECT_DEPTH or not hasattr(resources, "get"):
        return
    fonts = _resolve(resources.get("/Font")) or {}
    if hasattr(fonts, "keys"):
        for key in list(fonts.keys()):
            font = _resolve(fonts[key])
            if not hasattr(font, "get"):
                continue
            name = str(font.get("/BaseFont", "") or key)
            acc["font_names"].append(name)
            if _font_is_embedded(font):
                acc["embedded"] += 1
            if is_legacy_font_name(name):
                acc["legacy_names"].append(name)

    xobjects = _resolve(resources.get("/XObject")) or {}
    if not hasattr(xobjects, "keys"):
        return
    for key in list(xobjects.keys()):
        xobject = _resolve(xobjects[key])
        if not hasattr(xobject, "get"):
            continue
        subtype = str(xobject.get("/Subtype", ""))
        if subtype == "/Image":
            acc["images"] += 1
            pixels = int(xobject.get("/Width", 0) or 0) * int(xobject.get("/Height", 0) or 0)
            acc["largest"] = max(acc["largest"], pixels)
        elif subtype == "/Form":
            marker = id(xobject)
            if marker in seen:
                continue
            seen.add(marker)
            _walk(_resolve(xobject.get("/Resources")), seen, depth + 1, acc)


def _page_provenance(page: Any, number: int) -> PageProvenance:
    """One page, defensively. A resource dictionary is untrusted input."""
    acc: dict[str, Any] = {
        "font_names": [], "legacy_names": [], "embedded": 0, "images": 0, "largest": 0,
    }
    try:
        # `/Resources` is an inheritable page-tree attribute; a page that declares
        # none inherits its parent's, and reading the key directly would miss it.
        resources = page.get_inherited("/Resources")
    except Exception:  # noqa: BLE001 - fall back to the direct key
        resources = None
    if resources is None:
        try:
            resources = page.get("/Resources")
        except Exception:  # noqa: BLE001 - a malformed page yields no provenance
            resources = None
    try:
        _walk(_resolve(resources), set(), 0, acc)
    except Exception:  # noqa: BLE001 - one bad page must not lose the document
        pass
    # De-duplicated for reporting, order preserved so the first font on the page
    # reads first. The COUNTS above are per font object, not per unique name.
    names = list(dict.fromkeys(acc["font_names"]))
    return PageProvenance(
        page_number=number,
        fonts=len(acc["font_names"]),
        embedded_fonts=acc["embedded"],
        legacy_font_names=tuple(dict.fromkeys(acc["legacy_names"])),
        font_names=tuple(names),
        images=acc["images"],
        largest_image_pixels=acc["largest"],
    )


def read_pdf_provenance(path: Path) -> DocumentProvenance:
    """Per-page font and image provenance for one PDF. NEVER raises.

    Same contract as `extraction.extract_file`: a corpus pass must survive any
    single malformed file, and *how* a file failed is itself the finding. A
    failure returns an empty `pages` with `error` set, which the router reads as
    "no provenance" and treats fail-closed.

    Page cap and encryption handling mirror `documents.read_pdf_pages`, so
    provenance and text agree about which pages exist.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:  # noqa: BLE001 - a failed decrypt is just "locked"
                opened = 0
            if not opened:
                return DocumentProvenance((), 0, 0, "encrypted")
        total = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - no path may reach the caller
        return DocumentProvenance((), 0, 0, type(exc).__name__)

    limit = min(total, MAX_PDF_PAGES)
    pages = []
    for index in range(limit):
        try:
            page = reader.pages[index]
        except Exception:  # noqa: BLE001 - an unreadable page has no provenance
            pages.append(PageProvenance(index + 1, 0, 0, (), (), 0, 0))
            continue
        pages.append(_page_provenance(page, index + 1))
    return DocumentProvenance(tuple(pages), total, total - limit, None)

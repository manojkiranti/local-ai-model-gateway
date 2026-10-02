"""Read a PDF page through its embedded font when the PDF's own ToUnicode is wrong.

THE PROBLEM (docs/prod-incident-2026-09-20.md §9.8, §9.10 B; nrb-integration §17.6)
    Microsoft Word labels each glyph of a Devanagari cluster with the character
    at the same POSITION in the cluster, and the first cluster to use a glyph
    decides its label for the whole file. So Kalimati's gid 143 (त) is labelled
    ि in every Word-2013 file, and ~6% of the NRB corpus extracts as
    `कार्ाालर्` for कार्यालय. pypdf and poppler agree byte for byte: the PDFs
    are wrong, not the extractor. OCR fixes the spelling and loses 28-53% of an
    Act, so it cannot replace this text.

THE REPAIR, PER PAGE
    1. Find the page's SUSPECT fonts: Type0 over CIDFontType2, embedded
       `FontFile2`, Identity-H, an Identity or stream CIDToGIDMap, and a
       ToUnicode that contradicts the font's own cmap on at least one code.
       Judged per FONT across its whole ToUnicode (plan clarification 1).
    2. Build each suspect font's `GlyphTable` from its own cmap + GSUB.
    3. Install a CORRECTED ToUnicode — the table's tokens, markers included —
       on an in-memory reader, and extract the page with the SAME pypdf call
       native extraction makes (`documents.read_pdf_pages`), so the page's
       layout (lines, spaces) is pypdf's, unchanged.
    4. `reorder` the result visual → logical.

HIDDEN JOINERS (CHECKPOINT A, 2026-10-02)
    Word's font subsets carry no glyph for ZWJ, so the shaper draws a typed
    joiner as the SPACE glyph: राख्‍नु is drawn [र ा ख्(half) space न ु], and
    read as a space it shapes back to [र ा ख ् space न ु] — every such run
    failed the round trip. A half form (`GlyphTable.half_forms`) is formed only
    before a consonant or a ZWJ, so the space glyph right after one IS a hidden
    ZWJ; after anything else it is a space. One code cannot carry both labels,
    and relabelling the space glyph is not an option: pypdf finds a font's
    space width through the code labelled " ", and without one it adds spaces
    of its own (measured: double spaces on every repaired page). So the
    ToUnicode keeps " ", and `rejoin` finds each run — in content order,
    pypdf's whitespace ignored, as the layout check does — in pypdf's text and
    puts the ZWJ back at that run's own space glyph. A run it cannot find fails
    the page `layout`.

THE GATE — all four, or the page keeps today's text (spec §4.3)
    coverage   every glyph a suspect font draws resolves in its table
    orphans    reordering attaches every marker, per run and on the page
    roundtrip  every suspect-font run — consecutive text operators in one font
               inside one BT…ET — decoded, reordered and SHAPED with that
               font's own program reproduces the run's drawn gids, 100%
    layout     the served text contains every round-tripped run, whitespace
               ignored, in content order: pypdf only ADDED whitespace — and
               the walker declined no form (past MAX_XOBJECT_DEPTH, or one
               drawing its own ancestor), because pypdf's extract_text has no
               such limit and would serve glyphs no check ever saw

    Density is the DETECTOR (`detect`), never the acceptance: a misread rakar
    produced पर्कृति — wrong, and with zero illegal clusters (§1.1).

FAILURE IS A STATUS, NEVER AN EXCEPTION
    `repair_page` never raises for a bad page or a bad file. Missing
    dependencies, an unreadable file, an unsupported font and every gate
    failure come back as `unrepaired:<reason>` with the native text; the caller
    (`recovery.native_unit`) keeps that text, non-authoritative — rule 6.

Module scope imports only the standard library and our own pure modules.
pypdf is imported inside the functions that need it, fontTools only through
`glyphtable`, uharfbuzz only through `shaping`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import devanagari, glyphtable, shaping
from .reorder import PREBASE, REPH, reorder

logger = logging.getLogger("app.nrb.fontrepair")

__all__ = [
    "COVERAGE", "DETECT_DENSITY", "DETECT_MIN_DEVANAGARI", "Detection",
    "ENGINE_ERROR", "ENGINE_PREFIX", "FontRepairEngine", "LAYOUT",
    "NOT_APPLICABLE", "NO_SUSPECT_FONT", "ORPHANS", "READ_FAILED",
    "REPAIR_VERSION", "ROUNDTRIP", "RepairOutcome", "SHAPER_UNAVAILABLE",
    "STATUS_REPAIRED", "UNAVAILABLE", "UNREPAIRED_PREFIX", "UNSUPPORTED_FONT",
    "ZWJ",
    "corrected_cmap", "detect", "document_evidence", "engine_available",
    "engine_version", "fonts_report", "missing_in_order", "parse_tounicode",
    "rejoin", "unrepaired",
]

# --- identity --------------------------------------------------------------- #
# REPAIR_VERSION is bumped BY HAND for any change to glyphtable's rules,
# reorder, or this gate — after Task 14 of the plan, i.e. once units exist
# under it. The detector constants and the HarfBuzz version are read LIVE into
# `engine_version`, so editing them cannot be forgotten.
ENGINE_PREFIX = "native-3"
REPAIR_VERSION = "repair-1"
UNAVAILABLE = f"{ENGINE_PREFIX}/repair-unavailable"

# --- the detector (spec §4.1), frozen before the cohort is drawn ------------ #
# Development set (production scope, 2026-10-02): affected documents 5.05-185
# illegal clusters per 1k Devanagari characters, cleanest clean one 0.09.
DETECT_DENSITY = 0.001
DETECT_MIN_DEVANAGARI = 500

# --- statuses: a closed vocabulary, like recovery.ROUTES -------------------- #
STATUS_REPAIRED = "font_tables"
NOT_APPLICABLE = "not_applicable"
UNREPAIRED_PREFIX = "unrepaired:"
COVERAGE = "coverage"
ORPHANS = "orphans"
ROUNDTRIP = "roundtrip"
LAYOUT = "layout"
NO_SUSPECT_FONT = "no_suspect_font"
UNSUPPORTED_FONT = "unsupported_font"
SHAPER_UNAVAILABLE = "shaper_unavailable"
READ_FAILED = "read_failed"
ENGINE_ERROR = "engine_error"
GATE_CHECKS = (COVERAGE, ORPHANS, ROUNDTRIP, LAYOUT)
UNREPAIRED_REASONS = (
    *GATE_CHECKS, NO_SUSPECT_FONT, UNSUPPORTED_FONT, SHAPER_UNAVAILABLE,
    READ_FAILED, ENGINE_ERROR,
)

MAX_EXAMPLES = 3
ZWJ = "\u200d"
# The signs Word positions on their own glyph by glyph: the reph and pre-base
# markers, the dependent vowel signs and the other marks, nukta and virama.
# pypdf reads each such offset as a word gap. (`rejoin`.)
_SIGNS = frozenset({REPH, PREBASE, "\u093c", "\u094d"}) | frozenset(
    chr(c) for c in (0x0900, 0x0901, 0x0902, 0x0903, 0x093A, 0x093B,
                     *range(0x093E, 0x094D), 0x094E, 0x094F,
                     0x0955, 0x0956, 0x0957, 0x0962, 0x0963))
MAX_XOBJECT_DEPTH = 3
_SHOW_OPS = frozenset({b"Tj", b"TJ", b"'", b'"'})


def unrepaired(reason: str) -> str:
    if reason not in UNREPAIRED_REASONS:
        raise ValueError(f"unknown unrepaired reason {reason!r}")
    return UNREPAIRED_PREFIX + reason


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Detection:
    suspect: bool
    density: float
    devanagari_chars: int
    illegal_clusters: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect(texts: Sequence[str]) -> Detection:
    """Illegal clusters per Devanagari character, over a document's native pages."""
    illegal = chars = 0
    for text in texts:
        shape = devanagari.measure_devanagari(text)
        illegal += shape.illegal_clusters
        chars += shape.devanagari_chars
    density = illegal / chars if chars else 0.0
    return Detection(
        suspect=chars >= DETECT_MIN_DEVANAGARI and density >= DETECT_DENSITY,
        density=round(density, 6),
        devanagari_chars=chars,
        illegal_clusters=illegal,
    )


# --------------------------------------------------------------------------- #
# Engine identity
# --------------------------------------------------------------------------- #
def engine_available() -> bool:
    if not shaping.available():
        return False
    try:
        import fontTools  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def engine_version() -> str:
    if not engine_available():
        return UNAVAILABLE
    return (
        f"{ENGINE_PREFIX}/{REPAIR_VERSION}"
        f"/D={DETECT_DENSITY * 1000:g}/N={DETECT_MIN_DEVANAGARI}"
        f"/lang={shaping.LANGUAGE}/hb-{shaping.version()}"
    )


# --------------------------------------------------------------------------- #
# ToUnicode
# --------------------------------------------------------------------------- #
_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_PAIR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>")
_RANGE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", re.S)
_HEX = re.compile(rb"<([0-9A-Fa-f]*)>")


def _utf16(hexstr: bytes) -> str:
    if len(hexstr) % 2:
        hexstr = hexstr + b"0"
    return bytes.fromhex(hexstr.decode("ascii")).decode("utf-16-be", "replace")


def parse_tounicode(data: bytes) -> dict[int, str]:
    labels: dict[int, str] = {}
    for block in _BFCHAR.findall(data):
        for src, dst in _PAIR.findall(block):
            labels[int(src, 16)] = _utf16(dst)
    for block in _BFRANGE.findall(data):
        for lo_hex, hi_hex, dst in _RANGE.findall(block):
            lo, hi = int(lo_hex, 16), int(hi_hex, 16)
            if hi < lo or hi - lo > 0xFFFF:
                continue
            if dst.startswith(b"["):
                for k, item in enumerate(_HEX.findall(dst)):
                    labels[lo + k] = _utf16(item)
                continue
            base = _utf16(dst[1:-1])
            if not base:
                continue
            for k in range(hi - lo + 1):
                last = ord(base[-1]) + k
                if last <= 0x10FFFF:
                    labels[lo + k] = base[:-1] + chr(last)
    return labels


def corrected_cmap(labels: dict[int, str]) -> bytes:
    rows = sorted(labels.items())
    out = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
           b"/CMapName /NRB-native3-repair def", b"/CMapType 2 def",
           b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        out.append(f"{len(chunk)} beginbfchar".encode())
        out += [f"<{c:04X}> <{t.encode('utf-16-be').hex().upper()}>".encode() for c, t in chunk]
        out.append(b"endbfchar")
    out += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(out)


def _hidden_joiners(
    gids: Sequence[int], tokens: Sequence[str], half_forms: frozenset[int]
) -> tuple[str, frozenset[int]]:
    """A run as pypdf reads it, and the indexes in it of the space glyphs that
    are hidden ZWJs: a space glyph right after a half form (module docstring)."""
    read: list[str] = []
    hidden: set[int] = set()
    offset = 0
    for i, token in enumerate(tokens):
        if token == " " and i and gids[i - 1] in half_forms:
            hidden.add(offset)
        read.append(token)
        offset += len(token)
    return "".join(read), frozenset(hidden)


def rejoin(raw: str, runs: Sequence[tuple[str, frozenset[int]]]) -> tuple[str, int] | None:
    """`raw` with what pypdf's layout split inside a run put back together. Pure.

    `runs` are (the run as pypdf reads it, the indexes of its hidden joiners),
    in content order. Each run is found in `raw` after the previous one,
    whitespace ignored exactly as `missing_in_order` ignores it. Inside a run:

      * a hidden joiner takes ALL the whitespace pypdf wrote at its place;
      * spaces pypdf wrote beside a sign Word positions on its own (`_SIGNS`:
        reph, pre-base ि, the dependent signs) are removed WHERE THE RUN DREW
        NO SPACE — pypdf reads each positioning offset as a word gap, so
        मार्फत was served "माफ <R> त" and फैसला "फ ै सला" — and where the run
        did draw one, pypdf's spaces there become the run's own. Measured
        (CHECKPOINT A, 6 documents): 2,492 such spaces, every one beside a
        sign, none between two letters and none a line break. A line break is
        never removed, and a gap between two letters is never touched.

    Returns the text and how many gaps it closed, or None when any run cannot
    be found: nothing is ever moved anywhere but inside its own run.
    """
    edits: list[tuple[int, int, str]] = []
    closed = 0
    cursor = 0
    for read, hidden in runs:
        items = [(k, ch) for k, ch in enumerate(read) if k in hidden or not ch.isspace()]
        if not items:
            continue
        pattern: list[str] = []
        fills: list[str] = []
        prev: tuple[int, str] | None = None
        for k, ch in items:
            if prev is not None and prev[0] not in hidden and k not in hidden:
                drew = "".join(c for c in read[prev[0] + 1:k] if c in " \t")
                if prev[1] in _SIGNS or ch in _SIGNS:
                    pattern.append(r"([ \t]*)\s*")
                    fills.append(drew)
                else:
                    pattern.append(r"\s*")
            if k in hidden:
                pattern.append(r"(\s*)")
                fills.append(ZWJ)
            else:
                pattern.append(re.escape(ch))
            prev = (k, ch)
        found = re.compile("".join(pattern)).search(raw, cursor)
        if found is None:
            return None
        for group, fill in enumerate(fills, start=1):
            start, end = found.span(group)
            if fill == ZWJ or (end > start and raw[start:end] != fill):
                edits.append((start, end, fill))
                closed += fill != ZWJ
        cursor = found.end()
    for start, end, fill in sorted(edits, reverse=True):
        raw = raw[:start] + fill + raw[end:]
    return raw, closed


def missing_in_order(needles: Sequence[str], haystack: str) -> int:
    """How many `needles` do not appear in `haystack` in order, whitespace ignored."""
    hay = "".join(haystack.split())
    cursor = missing = 0
    for needle in needles:
        compact = "".join(needle.split())
        if not compact:
            continue
        at = hay.find(compact, cursor)
        if at < 0:
            missing += 1
        else:
            cursor = at + len(compact)
    return missing


# --------------------------------------------------------------------------- #
# Fonts, documents, runs
# --------------------------------------------------------------------------- #
def _resolve(obj: Any) -> Any:
    getter = getattr(obj, "get_object", None)
    return getter() if callable(getter) else obj


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@dataclass
class _Font:
    name: str
    font_obj: Any
    usable: bool = False
    why: str | None = None
    program: bytes = b""
    table: glyphtable.GlyphTable | None = None
    cid_to_gid: dict[int, int] | None = None
    labels: dict[int, str] = field(default_factory=dict)
    contradictions: int = 0
    corrected: bool = False

    @property
    def suspect(self) -> bool:
        return self.usable and self.contradictions > 0

    def gid(self, code: int) -> int:
        return code if self.cid_to_gid is None else self.cid_to_gid.get(code, 0)

    def codes_for(self, gid: int) -> list[int]:
        if self.cid_to_gid is None:
            return [gid]
        return [code for code, g in self.cid_to_gid.items() if g == gid]

    def install_corrected(self) -> None:
        """Replace this font's ToUnicode, in memory, with the table's tokens."""
        if self.corrected or self.table is None:
            return
        from pypdf.generic import DecodedStreamObject, NameObject

        labels = dict(self.labels)
        for gid, token in self.table.tokens.items():
            if gid in self.table.ambiguous:
                continue
            for code in self.codes_for(gid):
                labels[code] = token
        stream = DecodedStreamObject()
        stream.set_data(corrected_cmap(labels))
        self.font_obj[NameObject("/ToUnicode")] = stream
        self.corrected = True


def _cid_to_gid(obj: Any) -> dict[int, int] | None:
    resolved = _resolve(obj)
    if resolved is None or str(resolved) == "/Identity":
        return None
    data = resolved.get_data()
    return {i: int.from_bytes(data[2 * i:2 * i + 2], "big") for i in range(len(data) // 2)}


def _contradictions(font: _Font) -> int:
    count = 0
    table = font.table
    assert table is not None
    for code, label in font.labels.items():
        chars = table.cmap_chars.get(font.gid(code))
        if chars and _nfc(label) not in {_nfc(c) for c in chars}:
            count += 1
    return count


def _analyse(font: Any) -> _Font:
    out = _Font(name=str(font.get("/BaseFont", "") or ""), font_obj=font)
    if str(font.get("/Subtype", "")) != "/Type0":
        out.why = "not_type0"
        return out
    out.why = UNSUPPORTED_FONT
    if str(_resolve(font.get("/Encoding"))) != "/Identity-H":
        return out
    descendants = _resolve(font.get("/DescendantFonts")) or []
    if not descendants:
        return out
    cid = _resolve(descendants[0])
    if str(cid.get("/Subtype", "")) != "/CIDFontType2":
        return out
    descriptor = _resolve(cid.get("/FontDescriptor"))
    program_ref = descriptor.get("/FontFile2") if descriptor is not None else None
    if program_ref is None:
        return out
    try:
        out.program = _resolve(program_ref).get_data()
        out.cid_to_gid = _cid_to_gid(cid.get("/CIDToGIDMap"))
        out.table = glyphtable.build_glyph_table(out.program)
        tounicode = font.get("/ToUnicode")
        out.labels = parse_tounicode(_resolve(tounicode).get_data()) if tounicode is not None else {}
    except Exception as exc:  # noqa: BLE001 - an unreadable program is a status
        logger.info("native-3: font %s unusable (%s)", out.name, type(exc).__name__)
        return out
    out.usable, out.why = True, None
    out.contradictions = _contradictions(out)
    return out


@dataclass
class _Run:
    font: _Font
    gids: list[int]


def _raw(obj: Any) -> bytes | None:
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)
    original = getattr(obj, "original_bytes", None)
    if original is not None:
        return bytes(original)
    return None


def _strings(op: bytes, operands: Sequence[Any]) -> list[bytes]:
    if op == b"TJ":
        return [b for b in (_raw(x) for x in (operands[0] if operands else [])) if b is not None]
    item = operands[2] if op == b'"' and len(operands) > 2 else (operands[0] if operands else None)
    raw = _raw(item)
    return [raw] if raw is not None else []


def _codes(raw: bytes) -> list[int]:
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]


class _Document:
    """One PDF opened for repair. Its fonts are analysed once and corrected in place."""

    def __init__(self, path: Path) -> None:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:  # noqa: BLE001
                opened = 0
            if not opened:
                raise PermissionError("encrypted")
        self.reader = reader
        self._fonts: dict[Any, _Font] = {}
        self.declined = 0  # forms the last `runs()` was asked to draw and did not walk

    def font(self, fonts: Any, key: Any) -> _Font | None:
        if fonts is None or not hasattr(fonts, "get"):
            return None
        raw = fonts.raw_get(key) if hasattr(fonts, "raw_get") else fonts.get(key)
        if raw is None:
            return None
        ident = (raw.idnum, raw.generation) if hasattr(raw, "idnum") else id(raw)
        if ident not in self._fonts:
            self._fonts[ident] = _analyse(_resolve(raw))
        return self._fonts[ident]

    def runs(self, page: Any) -> tuple[list[_Run], set[str]]:
        """Every text run in a usable Type0 font, and the names of unusable ones."""
        try:
            resources = page.get_inherited("/Resources")
        except Exception:  # noqa: BLE001
            resources = None
        if resources is None:
            resources = page.get("/Resources")
        out: list[_Run] = []
        unusable: set[str] = set()
        self.declined = 0
        contents = page.get_contents()
        if contents is not None:
            self._walk(contents, _resolve(resources), out, unusable, 0, ())
        return out, unusable

    def _walk(self, content: Any, resources: Any, out: list[_Run], unusable: set[str],
              depth: int, stack: tuple[Any, ...]) -> None:
        from pypdf.generic import ContentStream

        stream = content if hasattr(content, "operations") else ContentStream(content, self.reader)
        fonts = _resolve(resources.get("/Font")) if resources is not None else None
        xobjects = _resolve(resources.get("/XObject")) if resources is not None else None
        current: _Font | None = None
        saved: list[_Font | None] = []
        run: _Run | None = None

        def flush() -> None:
            nonlocal run
            if run is not None and run.gids:
                out.append(run)
            run = None

        for operands, op in stream.operations:
            if op == b"q":
                saved.append(current)
            elif op == b"Q":
                restored = saved.pop() if saved else current
                if restored is not current:
                    flush()
                current = restored
            elif op in (b"BT", b"ET"):
                flush()
            elif op == b"Tf" and operands:
                font = self.font(fonts, operands[0])
                if font is not current:
                    flush()
                current = font
                if font is not None and not font.usable and font.why == UNSUPPORTED_FONT:
                    unusable.add(font.name)
            elif op in _SHOW_OPS:
                if current is None or not current.usable:
                    flush()
                    continue
                if run is None:
                    run = _Run(current, [])
                for raw in _strings(op, operands):
                    run.gids.extend(current.gid(c) for c in _codes(raw))
            elif op == b"Do" and operands and xobjects is not None:
                ref = xobjects.raw_get(operands[0]) if hasattr(xobjects, "raw_get") else xobjects.get(operands[0])
                if ref is None:
                    continue
                ident = (ref.idnum, ref.generation) if hasattr(ref, "idnum") else id(ref)
                xobject = _resolve(ref)
                subtype = str(xobject.get("/Subtype", ""))
                if subtype == "/Image":
                    continue
                flush()
                if subtype != "/Form":
                    # pypdf extracts ANY non-Image XObject as a form; we walk
                    # only real forms, so an unknown subtype is declined.
                    self.declined += 1
                    continue
                # A form we decline to walk is text the gate never checked, yet
                # pypdf's extract_text has no such limit: count it, fail closed.
                # `stack` holds the ANCESTORS only, so a form drawn twice is
                # walked twice (it may inherit different resources each time).
                if depth >= MAX_XOBJECT_DEPTH or ident in stack:
                    self.declined += 1
                    continue
                inner = _resolve(xobject.get("/Resources")) or resources
                self._walk(xobject, inner, out, unusable, depth + 1, stack + (ident,))
        flush()


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RepairOutcome:
    status: str
    text: str
    detail: dict[str, Any] = field(default_factory=dict)


def _example(detail: dict[str, Any], check: str, **facts: Any) -> None:
    if len(detail["examples"]) < MAX_EXAMPLES:
        detail["examples"].append({"check": check, **facts})


class FontRepairEngine:
    """The native route's repair engine. One per process; one PDF open at a time.

    Not thread-safe, and need not be: the worker recovers one document at a
    time inside `asyncio.to_thread`.
    """

    name = "font_tables"

    def __init__(self) -> None:
        self._key: tuple[str, int, int] | None = None
        self._doc: _Document | None = None

    @property
    def available(self) -> bool:
        return engine_available()

    @property
    def version(self) -> str:
        return engine_version()

    def _document(self, path: Path) -> _Document:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        if key != self._key or self._doc is None:
            self._doc = _Document(path)
            self._key = key
        return self._doc

    def repair_page(self, path: Path, page_number: int, native_text: str) -> RepairOutcome:
        path = Path(path)
        if devanagari.measure_devanagari(native_text).devanagari_chars == 0:
            return RepairOutcome(NOT_APPLICABLE, native_text)
        if not engine_available():
            return RepairOutcome(unrepaired(SHAPER_UNAVAILABLE), native_text)
        try:
            doc = self._document(path)
            page = doc.reader.pages[page_number - 1]
            runs, unusable = doc.runs(page)
        except Exception as exc:  # noqa: BLE001 - a bad file is a status
            return RepairOutcome(unrepaired(READ_FAILED), native_text, {"error": type(exc).__name__})

        suspect = {id(r.font): r.font for r in runs if r.font.suspect}
        if not suspect:
            reason = UNSUPPORTED_FONT if unusable else NO_SUSPECT_FONT
            return RepairOutcome(unrepaired(reason), native_text,
                                 {"unusable_fonts": sorted(unusable)})
        declined = doc.declined
        try:
            return self._attempt(page, native_text, runs, suspect, declined)
        except Exception as exc:  # noqa: BLE001 - repair_page never raises
            logger.warning("native-3: page %s engine error (%s)", page_number, type(exc).__name__)
            return RepairOutcome(unrepaired(ENGINE_ERROR), native_text, {"error": type(exc).__name__})

    def _attempt(self, page: Any, native_text: str, runs: list[_Run],
                 suspect: dict[int, _Font], declined: int) -> RepairOutcome:
        for font in suspect.values():
            font.install_corrected()

        detail: dict[str, Any] = {
            "runs": 0, "runs_matched": 0, "glyph_uses": 0, "unresolved_uses": 0,
            "orphans": 0, "layout_missing": 0, "declined_forms": declined,
            "hidden_joiners": 0, "joiners_unplaced": 0, "gaps_closed": 0,
            "fonts": sorted({f.name for f in suspect.values()}),
            "font_identities": sorted({f.table.identity.label() for f in suspect.values() if f.table}),
            "examples": [],
        }
        logical_runs: list[str] = []
        read_runs: list[tuple[str, frozenset[int]]] = []  # as pypdf reads them
        for run in runs:
            if not run.font.suspect:
                continue
            detail["runs"] += 1
            table = run.font.table
            assert table is not None
            tokens = [table.token(g) for g in run.gids]
            detail["glyph_uses"] += len(tokens)
            missing = [g for g, t in zip(run.gids, tokens) if t is None]
            if missing:
                detail["unresolved_uses"] += len(missing)
                _example(detail, COVERAGE, gids=missing[:12])
                continue
            read, hidden = _hidden_joiners(run.gids, tokens, table.half_forms)  # type: ignore[arg-type]
            read_runs.append((read, hidden))
            detail["hidden_joiners"] += len(hidden)
            out = reorder("".join(ZWJ if k in hidden else c for k, c in enumerate(read)))
            if out.orphans:
                detail["orphans"] += out.orphans
                _example(detail, ORPHANS, text=out.text[:80])
                continue
            shaped = list(shaping.shape(run.font.program, out.text))
            if shaped == run.gids:
                detail["runs_matched"] += 1
            else:
                _example(detail, ROUNDTRIP, text=out.text[:80],
                         drawn=run.gids[:24], shaped=shaped[:24])
            logical_runs.append(out.text)

        raw = page.extract_text() or ""
        rejoined = rejoin(raw, read_runs)
        if rejoined is not None:
            raw, detail["gaps_closed"] = rejoined
        elif detail["hidden_joiners"]:
            # Otherwise a run is missing from pypdf's text, which the layout
            # check below refuses on its own.
            detail["joiners_unplaced"] = detail["hidden_joiners"]
        served = reorder(raw)
        detail["orphans"] += served.orphans
        detail["layout_missing"] = missing_in_order(logical_runs, served.text)
        detail["illegal_before"] = devanagari.illegal_cluster_count(native_text)
        detail["illegal_after"] = devanagari.illegal_cluster_count(served.text)

        failed = None
        if detail["unresolved_uses"]:
            failed = COVERAGE
        elif detail["orphans"]:
            failed = ORPHANS
        elif detail["runs_matched"] < detail["runs"]:
            failed = ROUNDTRIP
        elif detail["layout_missing"] or declined or detail["joiners_unplaced"]:
            failed = LAYOUT
        if failed is not None:
            return RepairOutcome(unrepaired(failed), native_text, detail)
        return RepairOutcome(STATUS_REPAIRED, served.text, detail)


# --------------------------------------------------------------------------- #
# Evidence helpers (scripts and the native-3 extractor; no serving path uses them)
# --------------------------------------------------------------------------- #
def fonts_report(path: Path) -> dict[str, Any]:
    """Every Type0 font any page draws with, and whether one contradicts its cmap."""
    try:
        doc = _Document(Path(path))
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "fonts": [], "contradicts": False}
    fonts: dict[int, _Font] = {}
    for page in doc.reader.pages:
        try:
            runs, _ = doc.runs(page)
        except Exception:  # noqa: BLE001
            continue
        for run in runs:
            fonts[id(run.font)] = run.font
    rows = [
        {
            "name": f.name,
            "identity": f.table.identity.label() if f.table else None,
            "has_gsub": bool(f.table and f.table.has_gsub),
            "contradictions": f.contradictions,
        }
        for f in fonts.values()
    ]
    return {"fonts": rows, "contradicts": any(f.suspect for f in fonts.values())}


def document_evidence(
    engine: FontRepairEngine, path: Path, pages: Sequence[str], native_numbers: Sequence[int]
) -> dict[str, Any]:
    """Detection over the native pages and, when it fires, the repair of each."""
    detection = detect([pages[n - 1] for n in native_numbers])
    out: dict[str, Any] = {
        "engine": engine.version,
        "detection": detection.as_dict(),
        "native_pages": len(native_numbers),
        "statuses": {},
        "pages": [],
        "runs": 0,
        "runs_matched": 0,
        "font_identities": [],
        "examples": [],
    }
    if not detection.suspect:
        return out
    for number in native_numbers:
        outcome = engine.repair_page(path, number, pages[number - 1])
        out["statuses"][outcome.status] = out["statuses"].get(outcome.status, 0) + 1
        out["pages"].append({"page": number, "status": outcome.status,
                             "font_identities": outcome.detail.get("font_identities", [])})
        out["runs"] += outcome.detail.get("runs", 0)
        out["runs_matched"] += outcome.detail.get("runs_matched", 0)
        for label in outcome.detail.get("font_identities", []):
            if label not in out["font_identities"]:
                out["font_identities"].append(label)
        for example in outcome.detail.get("examples", []):
            if len(out["examples"]) < 5:
                out["examples"].append({"page": number, **example})
    return out

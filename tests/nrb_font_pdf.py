"""PDFs that draw Devanagari the way a Microsoft Word export does — native-3 test helper.

Word shapes each line with the font, writes the GLYPH IDS into the content
stream (Type0 font, Identity-H, CIDToGIDMap Identity), and writes ONE ToUnicode
label per glyph. For a Devanagari cluster it labels each glyph with the
character at the same POSITION in the cluster, and the first cluster to use a
glyph decides that glyph's label for the whole file — which is how gid 143 (त)
came to be labelled ि in every Word-2013 Kalimati file (prod-incident §9.10 B).
`word_bug_labels` reproduces that rule, so these PDFs garble the way the NRB
files do, and the ground truth is the text that was shaped.

The font is Lohit Devanagari (OFL-1.1, tests/fixtures/fonts). The system
Kalimati, when installed, exercises the old-spec `deva` path; it is never
committed.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

FIXTURES = Path(__file__).parent / "fixtures" / "fonts"
LOHIT = FIXTURES / "Lohit-Devanagari.ttf"
SYSTEM_KALIMATI = Path("/usr/share/fonts/truetype/fonts-deva-extra/kalimati.ttf")

LINES = (
    "नेपाल राष्ट्र बैंकको निर्देशन अनुसार कार्यालय",
    "प्रकृति विक्रेता गर्दा त्यस्तो वस्तु फिर्ता",
    "क्षेत्र संस्था स्थापना भित्र मिति सकिने",
)
# 6 x 3 lines is ~660 Devanagari characters: a safe margin over the detector's
# N = 500 floor even after Word's labels drop or merge characters.
REPEAT = 6


def clusters(program: bytes, text: str) -> list[tuple[list[int], str]]:
    """[(glyph ids in visual order, the logical text they draw)] per HarfBuzz cluster."""
    import uharfbuzz as hb

    font = hb.Font(hb.Face(hb.Blob(program)))
    buf = hb.Buffer()
    buf.add_codepoints([ord(c) for c in text])  # clusters index codepoints
    buf.direction = "ltr"
    buf.script = "Deva"
    buf.language = "ne"
    hb.shape(font, buf, {})
    infos = buf.glyph_infos
    starts = sorted({i.cluster for i in infos})
    end = {s: (starts[k + 1] if k + 1 < len(starts) else len(text)) for k, s in enumerate(starts)}
    out: list[tuple[list[int], str]] = []
    last = None
    for info in infos:
        if info.cluster == last:
            out[-1][0].append(info.codepoint)
        else:
            out.append(([info.codepoint], text[info.cluster:end[info.cluster]]))
            last = info.cluster
    return out


def shaped_lines(program: bytes, lines: Sequence[str]) -> list[list[int]]:
    return [[g for gids, _ in clusters(program, line) for g in gids] for line in lines]


def word_bug_labels(program: bytes, lines: Sequence[str]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for line in lines:
        for gids, text in clusters(program, line):
            for i, gid in enumerate(gids):
                if i < len(gids) - 1:
                    piece = text[i] if i < len(text) else ""
                else:
                    piece = text[i:] if i < len(text) else ""
                if piece and gid not in labels:
                    labels[gid] = piece
    return labels


def honest_labels(program: bytes, lines: Sequence[str]) -> dict[int, str]:
    """Each drawn glyph labelled with what the font's own tables say it draws:
    a ToUnicode that agrees with the cmap everywhere, so it is never suspect."""
    from app.nrb import glyphtable
    from app.nrb.reorder import PREBASE, REPH

    table = glyphtable.build_glyph_table(program)
    out: dict[int, str] = {}
    for gids in shaped_lines(program, lines):
        for g in gids:
            token = table.token(g)
            if token is not None:
                out[g] = token.replace(PREBASE, "ि").replace(REPH, "र्")
    return out


def prebase_gid(program: bytes) -> int:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    return font.getGlyphOrder().index(font.getBestCmap()[0x093F])


def _tounicode(labels: dict[int, str]) -> bytes:
    rows = sorted(labels.items())
    out = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
           b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def",
           b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        out.append(f"{len(chunk)} beginbfchar".encode())
        out += [f"<{c:04X}> <{t.encode('utf-16-be').hex().upper()}>".encode() for c, t in chunk]
        out.append(b"endbfchar")
    out += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(out)


def build_pdf(
    program: bytes,
    pages: Sequence[Sequence[Sequence[int]]],
    labels: dict[int, str],
    *,
    base_font: str = "ABCDEE+TestDeva",
    in_form: bool = False,
    save_restore: bool = False,
    nest: int = 0,
    twice: bool = False,
    form_subtype: str = "Form",
    isolate: frozenset[int] = frozenset(),
) -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    def stream(entries: str, data: bytes) -> bytes:
        return f"<< {entries} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"

    catalog = add(b"")
    tree = add(b"")
    program_obj = add(stream(f"/Length1 {len(program)}", program))
    descriptor = add(
        f"<< /Type /FontDescriptor /FontName /{base_font} /Flags 4 "
        f"/FontBBox [-500 -500 1500 1200] /ItalicAngle 0 /Ascent 1000 /Descent -400 "
        f"/CapHeight 700 /StemV 80 /FontFile2 {program_obj} 0 R >>".encode()
    )
    cidfont = add(
        f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base_font} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor {descriptor} 0 R /CIDToGIDMap /Identity /DW 600 >>".encode()
    )
    cmap = add(stream("", _tounicode(labels)))
    font = add(
        f"<< /Type /Font /Subtype /Type0 /BaseFont /{base_font} /Encoding /Identity-H "
        f"/DescendantFonts [{cidfont} 0 R] /ToUnicode {cmap} 0 R >>".encode()
    )
    kids = []
    for lines in pages:
        def hexed(line):
            return "".join(f"{g:04X}" for g in line)

        def shown_line(line):
            if not any(g in isolate for g in line):
                return f"<{hexed(line)}> Tj"
            # Word positions some glyphs (the reph) on their own inside a TJ;
            # pypdf reads each large offset as a word gap and writes a space.
            parts = []
            for g in line:
                if g in isolate:
                    parts.append(f"900 <{g:04X}> -900")
                else:
                    parts.append(f"<{g:04X}>")
            return f"[{' '.join(parts)}] TJ"

        if save_restore:
            # The font is set ONCE, in its own text object, and every line is
            # drawn inside q...Q without a Tf of its own: a walker that resets
            # the font at BT, or loses it across q/Q, drops every run.
            shown = "BT /F1 12 Tf ET q " + " ".join(
                f"BT 72 {740 - 18 * i} Td <{hexed(line)}> Tj ET" for i, line in enumerate(lines)
            ) + " Q"
        else:
            # One text object per line, as Word writes them: a run never spans
            # two lines, so a syllable is never split and lines never merge.
            shown = " ".join(
                f"BT /F1 12 Tf 72 {740 - 18 * i} Td {shown_line(line)} ET"
                for i, line in enumerate(lines)
            )
        body = shown
        if nest:
            # The page's own text, plus a chain of `nest` forms (the last draws
            # the first line): a form past the walker's depth limit.
            inner = add(stream(
                f"/Type /XObject /Subtype /{form_subtype} /BBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >>",
                f"BT /F1 12 Tf 72 100 Td <{hexed(lines[0])}> Tj ET".encode()))
            for _ in range(nest - 1):
                inner = add(stream(
                    f"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                    f"/Resources << /XObject << /Fm1 {inner} 0 R >> >>", b"q /Fm1 Do Q"))
            contents = add(stream("", (body + " q /Fm1 Do Q").encode()))
            resources = f"/Font << /F1 {font} 0 R >> /XObject << /Fm1 {inner} 0 R >>"
        elif twice:
            form = add(stream(
                f"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >>", body.encode()))
            contents = add(stream("", b"q /Fm1 Do Q q /Fm1 Do Q"))
            resources = f"/XObject << /Fm1 {form} 0 R >>"
        elif in_form:
            form = add(stream(
                f"/Type /XObject /Subtype /{form_subtype} /BBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >>", body.encode()))
            contents = add(stream("", b"q /Fm1 Do Q"))
            resources = f"/XObject << /Fm1 {form} 0 R >>"
        else:
            contents = add(stream("", body.encode()))
            resources = f"/Font << /F1 {font} 0 R >>"
        kids.append(add(
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << {resources} >> /Contents {contents} 0 R >>".encode()))
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    objs[tree - 1] = (
        f"<< /Type /Pages /Count {len(kids)} "
        f"/Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode()
    )
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def word_pdf(
    tmp_path: Path,
    shaping_program: bytes,
    *,
    embed: bytes | None = None,
    lines: Sequence[str] = LINES,
    repeat: int = REPEAT,
    pages: int = 1,
    labels: dict[int, str] | None = None,
    extra_line: Sequence[int] | None = None,
    name: str = "word.pdf",
    **kw,
) -> Path:
    """Shape with `shaping_program`, embed `embed` (default: the same program).
    Embedding a GSUB-stripped copy of the shaping font is the Word-365 case."""
    body = list(lines) * repeat
    gids = shaped_lines(shaping_program, body)
    if extra_line is not None:
        gids = gids + [list(extra_line)]
    chosen = labels if labels is not None else word_bug_labels(shaping_program, body)
    path = tmp_path / name
    path.write_bytes(build_pdf(embed or shaping_program, [gids] * pages, chosen, **kw))
    return path


def strip_gsub(program: bytes) -> bytes:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    del font["GSUB"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def without_codepoints(program: bytes, codepoints: Sequence[int]) -> bytes:
    """The font with `codepoints` removed from every cmap subtable — Word's
    subsets carry no glyph for ZWJ/ZWNJ, so a shaper draws a hidden joiner as
    the SPACE glyph (measured on the production Kalimati subsets, 2026-10-02)."""
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    for sub in font["cmap"].tables:
        for cp in codepoints:
            sub.cmap.pop(cp, None)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def symbol_cmap_only(program: bytes) -> bytes:
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    font = TTFont(io.BytesIO(program))
    sub = CmapSubtable.newSubtable(4)
    sub.platformID, sub.platEncID, sub.language = 3, 0, 0
    sub.cmap = {0xF041: font.getGlyphOrder()[1]}
    font["cmap"].tables = [sub]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()

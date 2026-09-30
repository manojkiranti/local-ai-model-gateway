"""Local tool: create_pptx (structured slide deck -> .pptx download link).

Slide-based content model — a deck's unit is a slide, so the model supplies
`slides[]` of {title?, bullets?, table?, stats?, image?, chart?} plus an
optional deck `title`/`subtitle` that becomes a title slide. The `table` shape
is create_docx's exactly, and `chart` is create_chart's exactly (validated by
reusing that tool's own `_validate`, then rendered as a native, editable
python-pptx chart — not a picture), so the model reuses what it already
knows. `image` embeds an already-uploaded/generated raster file by `file_id`
(owner-scoped via `resolve_file`, resolved up front in `_create_pptx` since
the actual render is sync and can't do its own async lookups). `image`/`chart`
are full-slide content — mutually exclusive with `bullets`/`table`/`stats` on
the same slide, to avoid a combinatorial layout explosion; put them on their
own slide instead. A file tool, so it flows through the per-user file sink
like create_docx/create_pdf.

Rendered on the org's branded template (`assets/pptx_template.pptx`) rather
than python-pptx's generic default, so every deck comes out looking the same
— not a per-generation "custom" look. The template's slide MASTER carries the
brand background (logo, divider line, anniversary badge) with no override on
any of its 11 stock-named layouts, so every layout inherits it automatically;
`_LAYOUT_TITLE`/`_LAYOUT_TITLE_AND_CONTENT`/`_LAYOUT_TITLE_ONLY` index into
that same standard Office layout order (verified against the file's own
slideMaster1.xml `sldLayoutIdLst`, not assumed), so they need no remapping.
The template ships 3 example slides from whoever built it in PowerPoint. One
of them is a bold full-slide "cover" (its own picture background, no stock
layout has this) — `_find_cover_slide` locates it and it is REUSED directly
as the deck's title slide (`_add_cover_title_text` adds plain text boxes onto
it; no image is re-embedded or reconstructed), while `_remove_other_slides`
drops the remaining two example slides, or every deck would open with
unwanted blank slides ahead of its content. If the asset is missing (e.g. a
checkout that stripped it) or has no such cover slide, fall back to a
placeholder-based title slide on python-pptx's blank default rather than
failing the tool outright.

Caps are module constants, not arguments: the model must not be able to raise
the limit that bounds the file it asks the process to build.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import Any

from ...files import images, ingest
from ...files.readers import ReadError
from ...files.store import PPTX_MEDIA_TYPE, file_store, resolve_file
from . import chart as chart_tool
from .base import LocalToolSpec

_TEMPLATE_PATH = Path(__file__).parent / "assets" / "pptx_template.pptx"

MAX_SLIDES = 50
MAX_BULLETS_PER_SLIDE = 20
# A table does not paginate on a slide the way it does in a Word document — it
# just overflows past the slide's edge with no error. Measured on the default
# template: 13 rows fit from the bullets-free table position, 8 when bullets
# share the slide (the table starts lower). Caps are row count = data rows +
# 1 if headers, enforced in _validate (never a tool argument), same rule as
# MAX_SLIDES/MAX_BULLETS_PER_SLIDE.
MAX_TABLE_ROWS_PER_SLIDE = 12
MAX_TABLE_ROWS_WITH_BULLETS = 8
# One row, so a clean 1x4 grid rather than wrapping — matches the reference
# "key facts at a glance" layout this exists to reproduce.
MAX_STATS_PER_SLIDE = 4

_DEFAULT_FILENAME = "presentation.pptx"


def _validate(args: dict[str, Any]) -> tuple[str, str, list[dict], str] | str:
    """Return (title, subtitle, slides, filename) on success, or an ERROR: string."""
    raw_slides = args.get("slides")
    if not isinstance(raw_slides, list) or not raw_slides:
        return "ERROR: 'slides' is required and must be a non-empty array of {title?, bullets?, table?}."
    if len(raw_slides) > MAX_SLIDES:
        return f"ERROR: too many slides ({len(raw_slides)}); the limit is {MAX_SLIDES}. Split the deck."

    for idx, slide in enumerate(raw_slides):
        if not isinstance(slide, dict):
            return f"ERROR: slides[{idx}] must be an object with title/bullets/table."
        bullets = slide.get("bullets")
        table = slide.get("table")
        stats = slide.get("stats")
        image = slide.get("image")
        chart = slide.get("chart")
        if not (slide.get("title") or bullets or table is not None or stats or image or chart):
            return (
                f"ERROR: slides[{idx}] needs at least one of 'title', 'bullets', 'table', "
                "'stats', 'image', or 'chart'."
            )
        # image/chart are full-slide content (a picture or a native chart filling
        # the whole area below the title), not one more thing stacked alongside
        # bullets/table/stats — combining them would need a layout this tool
        # doesn't have, so it's refused rather than silently overlapping shapes.
        if (image or chart) and (bullets or table is not None or stats):
            return (
                f"ERROR: slides[{idx}] combines 'image'/'chart' with 'bullets'/'table'/'stats' — "
                "put the image or chart on its own slide (title + image/chart only), and the "
                "rest on another slide."
            )
        if image and chart:
            return f"ERROR: slides[{idx}] has both 'image' and 'chart' — use one per slide."
        if image is not None:
            if not isinstance(image, dict) or not str(image.get("file_id") or "").strip():
                return f"ERROR: slides[{idx}].image must be an object with a 'file_id' string."
            caption = image.get("caption")
            if caption is not None and not isinstance(caption, str):
                return f"ERROR: slides[{idx}].image.caption must be a string."
        if chart is not None:
            if not isinstance(chart, dict):
                return (
                    f"ERROR: slides[{idx}].chart must be an object with 'chart_type', "
                    "'labels', and 'series' (create_chart's own shape)."
                )
            # Reuse create_chart's own validator rather than duplicating its
            # rules — same shape, so the model doesn't learn a second schema,
            # and the two tools can't silently drift on what counts as valid.
            chart_check = chart_tool._validate(chart)
            if isinstance(chart_check, str):
                return f"ERROR: slides[{idx}].chart: {chart_check.removeprefix('ERROR: ')}"
        if bullets is not None:
            if not isinstance(bullets, list) or not all(isinstance(b, str) for b in bullets):
                return f"ERROR: slides[{idx}].bullets must be an array of strings."
            if len(bullets) > MAX_BULLETS_PER_SLIDE:
                return (
                    f"ERROR: slides[{idx}].bullets has {len(bullets)} items; "
                    f"the limit is {MAX_BULLETS_PER_SLIDE} per slide. Split across slides."
                )
        if table is not None:
            if not isinstance(table, dict):
                return f"ERROR: slides[{idx}].table must be an object with 'rows' (and optional 'headers')."
            rows = table.get("rows")
            if not isinstance(rows, list) or not rows:
                return f"ERROR: slides[{idx}].table.rows must be a non-empty array of rows."
            for ridx, row in enumerate(rows):
                if not isinstance(row, (list, tuple)):
                    return f"ERROR: slides[{idx}].table.rows[{ridx}] must be an array of cell values."
            headers = table.get("headers")
            if headers is not None and not isinstance(headers, list):
                return f"ERROR: slides[{idx}].table.headers must be an array of column names."
            nrows = len(rows) + (1 if headers else 0)
            has_bullets = bool(bullets)
            cap = MAX_TABLE_ROWS_WITH_BULLETS if has_bullets else MAX_TABLE_ROWS_PER_SLIDE
            if nrows > cap:
                return (
                    f"ERROR: slides[{idx}].table has {nrows} rows (incl. header); the limit is "
                    f"{cap} on a slide {'with bullets' if has_bullets else 'without bullets'}. "
                    "Split the table across slides."
                )
        if stats is not None:
            if not isinstance(stats, list) or not stats:
                return f"ERROR: slides[{idx}].stats must be a non-empty array of {{value, label, note?}}."
            if len(stats) > MAX_STATS_PER_SLIDE:
                return (
                    f"ERROR: slides[{idx}].stats has {len(stats)} items; the limit is "
                    f"{MAX_STATS_PER_SLIDE} per slide."
                )
            for sidx, stat in enumerate(stats):
                if not isinstance(stat, dict) or not stat.get("value") or not stat.get("label"):
                    return (
                        f"ERROR: slides[{idx}].stats[{sidx}] must be an object with "
                        "'value' and 'label' (both required; 'note' optional)."
                    )

    title = str(args.get("title") or "")
    subtitle = str(args.get("subtitle") or "")
    filename = str(args.get("filename") or _DEFAULT_FILENAME)
    if not filename.lower().endswith(".pptx"):
        filename += ".pptx"
    return title, subtitle, raw_slides, filename


_LAYOUT_TITLE = 0
_LAYOUT_TITLE_AND_CONTENT = 1
_LAYOUT_TITLE_ONLY = 5


def _add_bullets(slide, bullets: list[str]) -> None:
    """Fill the content placeholder (index 1 on the Title-and-Content layout)."""
    body = slide.placeholders[1].text_frame
    body.clear()  # leaves one empty paragraph
    for i, text in enumerate(bullets):
        paragraph = body.paragraphs[0] if i == 0 else body.add_paragraph()
        paragraph.text = text
        paragraph.level = 0


def _add_table(slide, table: dict, top_emu: int, slide_width: int) -> None:
    from pptx.util import Emu

    headers = table.get("headers")
    rows = table["rows"]
    ncols = max([len(headers or [])] + [len(r) for r in rows]) or 1
    nrows = len(rows) + (1 if headers else 0)

    margin = Emu(int(slide_width * 0.05))
    width = Emu(slide_width - 2 * margin)
    row_h = Emu(370_000)  # ~1 cm; python-pptx sizes rows to content anyway
    shape = slide.shapes.add_table(nrows, ncols, margin, Emu(top_emu), width, Emu(row_h * nrows))
    grid = shape.table

    r = 0
    if headers:
        for c in range(ncols):
            grid.cell(0, c).text = str(headers[c]) if c < len(headers) else ""
        r = 1
    for row in rows:
        for c in range(ncols):
            grid.cell(r, c).text = str(row[c]) if c < len(row) else ""
        r += 1


def _add_image(
    slide, image: dict, image_paths: dict[str, str], top_emu: int, zone_height_emu: int, slide_width: int
) -> None:
    """Embed an already-uploaded/generated raster image, scaled to fit inside
    the content zone with its aspect ratio preserved and centered
    horizontally, with an optional caption below it. `image_paths` is resolved
    up front in `_create_pptx` (async, owner-scoped `resolve_file`) since this
    whole render runs sync off the loop — a fresh resolve here would need to
    be async and couldn't be."""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu, Pt

    path = image_paths[image["file_id"]]
    caption = image.get("caption")

    # Reserve room for the caption so the picture doesn't crowd it out; the
    # summary read is cheap (declared dimensions, not a full decode) and is
    # the same pixel-bomb-safe entry point every image path in this app uses.
    caption_h = int(zone_height_emu * 0.12) if caption else 0
    pic_zone_h = zone_height_emu - caption_h
    summary = images.summarize_image(Path(path))

    margin = int(slide_width * 0.05)
    max_w = slide_width - 2 * margin
    scale = min(max_w / summary.width, pic_zone_h / summary.height)
    w = int(summary.width * scale)
    h = int(summary.height * scale)
    left = (slide_width - w) // 2
    slide.shapes.add_picture(path, Emu(left), Emu(top_emu), width=Emu(w), height=Emu(h))

    if caption:
        box = slide.shapes.add_textbox(
            Emu(margin), Emu(top_emu + h), Emu(slide_width - 2 * margin), Emu(max(caption_h, 1))
        )
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = caption
        run.font.size = Pt(11)
        run.font.italic = True


def _add_chart(slide, chart: dict, top_emu: int, height_emu: int, slide_width: int) -> None:
    """A native, editable PowerPoint chart (python-pptx's own chart engine —
    not a picture), from the exact same {chart_type, labels, series} shape
    create_chart validates, coerced the same way _add_table coerces its cells
    (str.format/float at render time, not at validation time)."""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Emu

    chart_type = chart["chart_type"]
    labels = [str(lab) for lab in chart["labels"]]
    raw_series = chart["series"]
    series = [
        (str(s.get("name") or f"Series {i + 1}"), [float(v) for v in s["data"]])
        for i, s in enumerate(raw_series)
    ]
    if chart_type in ("pie", "donut"):
        # Same restriction create_chart's own description states: a pie/donut
        # is parts-of-a-whole, so only the first series is meaningful.
        series = series[:1]

    type_map = {
        "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "hbar": XL_CHART_TYPE.BAR_CLUSTERED,
        "line": XL_CHART_TYPE.LINE_MARKERS,
        "area": XL_CHART_TYPE.AREA,
        "pie": XL_CHART_TYPE.PIE,
        "donut": XL_CHART_TYPE.DOUGHNUT,
    }

    chart_data = CategoryChartData()
    chart_data.categories = labels
    for name, data in series:
        chart_data.add_series(name, data)

    margin = int(slide_width * 0.05)
    width = slide_width - 2 * margin
    graphic_frame = slide.shapes.add_chart(
        type_map[chart_type], Emu(margin), Emu(top_emu), Emu(width), Emu(height_emu), chart_data
    )
    graphic_frame.chart.has_legend = True


# Brand red (#E60012, matching the frontend's own --primary token). Hardcoded
# rather than pulled from the template's theme palette because that palette
# is still stock Office blue/orange (verified against its own theme1.xml) —
# only the master's BACKGROUND was branded, never its color scheme. This
# matches the tool's existing brand-specific assumptions (the whole template
# is NIC-ASIA-specific already); a different org's template would want these
# recolored too.
_STAT_VALUE_COLOR = (0xE6, 0x00, 0x12)
_STAT_CARD_FILL = (0xFC, 0xEA, 0xEB)
_STAT_CARD_BORDER = (0xF0, 0xC5, 0xC9)


def _add_stats(slide, stats: list[dict], top_emu: int, height_emu: int, slide_width: int) -> None:
    """A row of up to MAX_STATS_PER_SLIDE rounded-rectangle "stat cards" — a
    large colored value, a label, and an optional smaller note — for
    highlight-figures content ("key facts at a glance"). The single
    highest-impact layout beyond a plain bullet list for the kind of numeric
    highlights a deck author reaches for."""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu, Pt

    n = len(stats)
    margin = int(slide_width * 0.05)
    gap = int(slide_width * 0.02)
    card_width = (slide_width - 2 * margin - (n - 1) * gap) // n

    for i, stat in enumerate(stats):
        left = margin + i * (card_width + gap)
        shape = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Emu(left), Emu(top_emu), Emu(card_width), Emu(height_emu)
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(*_STAT_CARD_FILL)
        shape.line.color.rgb = RGBColor(*_STAT_CARD_BORDER)
        shape.line.width = Pt(0.75)
        shape.shadow.inherit = False

        tf = shape.text_frame
        tf.word_wrap = True
        inset = Emu(int(card_width * 0.09))
        tf.margin_left = inset
        tf.margin_right = inset
        tf.margin_top = Emu(int(height_emu * 0.12))

        p_value = tf.paragraphs[0]
        p_value.alignment = PP_ALIGN.LEFT
        run_value = p_value.add_run()
        run_value.text = str(stat["value"])
        run_value.font.size = Pt(28)
        run_value.font.bold = True
        run_value.font.color.rgb = RGBColor(*_STAT_VALUE_COLOR)

        p_label = tf.add_paragraph()
        run_label = p_label.add_run()
        run_label.text = str(stat["label"])
        run_label.font.size = Pt(12)
        run_label.font.bold = True

        note = stat.get("note")
        if note:
            p_note = tf.add_paragraph()
            run_note = p_note.add_run()
            run_note.text = str(note)
            run_note.font.size = Pt(9)
            run_note.font.color.rgb = RGBColor(0x66, 0x66, 0x66)


_P14_SECTION_EXT_URI = "{521415D9-36F7-43E2-AB2F-B90AF26B5E84}"

# Every deck is set in one typeface. Applied last, by `_apply_deck_font`, over
# the finished deck rather than at each `run.font` call site, so a text shape
# added later cannot forget it.
DECK_FONT = "Arial"

# Position/font of the title-slide cover's title+subtitle text, lifted directly
# from the template's own slideLayout1.xml ctrTitle/subTitle placeholders (and
# the master's titleStyle/bodyStyle for font) so plain text boxes land exactly
# where those placeholders would have, without being placeholders themselves.
_COVER_TITLE_XFRM = (1524000, 1122363, 9144000, 2387600)
_COVER_SUBTITLE_XFRM = (1524000, 3602038, 9144000, 1655762)


def _find_cover_slide(prs):
    """Return (rId, Slide) for the template's own example slide that has a
    full-slide picture background override (its bold cover design — logo +
    corner graphics), or (None, None). Generic by structure (first slide with
    its own `<p:bg>` picture-fill override) rather than hardcoded to a
    filename, so it still works if the template is swapped for a different
    branded file with the same authoring pattern. Pairs each `<p:sldId>` XML
    entry with its Slide by walking both lists in lockstep (they share order),
    so the rId can be used later to decide which example slide survives
    `_remove_other_slides` without a second lookup."""
    from pptx.oxml.ns import qn

    for sld_elem, slide in zip(prs.slides._sldIdLst, prs.slides):
        cSld = slide._element.find(qn("p:cSld"))
        bg = cSld.find(qn("p:bg"))
        if bg is not None and bg.find(f'{qn("p:bgPr")}/{qn("a:blipFill")}') is not None:
            return sld_elem.rId, slide
    return None, None


def _picture_background_bytes(slide_or_master) -> bytes | None:
    """Raw image bytes behind a slide's or slide master's own full-slide
    picture background (its `<p:bg>` blipFill override), or None if it has no
    such override. Shared by the cover-slide reuse above and by the branding
    endpoints below, which read the master's own background — the plain
    header/logo/badge art every content slide inherits with no override."""
    from pptx.oxml.ns import qn

    cSld = slide_or_master._element.find(qn("p:cSld"))
    bg = cSld.find(qn("p:bg")) if cSld is not None else None
    if bg is None:
        return None
    blip = bg.find(f'{qn("p:bgPr")}/{qn("a:blipFill")}/{qn("a:blip")}')
    if blip is None:
        return None
    rid = blip.get(qn("r:embed"))
    return slide_or_master.part.rels[rid].target_part.blob


def cover_background_bytes() -> bytes | None:
    """The template's own cover-slide background artwork, read fresh from the
    template file every call (not cached) — see the module docstring: the
    slide/generation path already re-reads the template each time, and this
    keeps the frontend's deck-preview branding equally in sync with whatever
    template file is on disk right now, with no separate update step if the
    template is swapped. None if the asset is missing or has no such slide."""
    if not _TEMPLATE_PATH.exists():
        return None
    from pptx import Presentation

    prs = Presentation(str(_TEMPLATE_PATH))
    _, slide = _find_cover_slide(prs)
    return _picture_background_bytes(slide) if slide is not None else None


def header_background_bytes() -> bytes | None:
    """The template's slide MASTER's own background artwork — the plain
    header/logo/badge look every content slide inherits — read fresh from the
    template file every call, same reasoning as `cover_background_bytes`."""
    if not _TEMPLATE_PATH.exists():
        return None
    from pptx import Presentation

    prs = Presentation(str(_TEMPLATE_PATH))
    return _picture_background_bytes(prs.slide_masters[0])


def _remove_other_slides(prs, keep_rid: str | None) -> None:
    """Drop every template example slide except the one whose relationship id
    is `keep_rid` (or all of them, if None) — leaving the slide masters/layouts
    intact. Standard python-pptx idiom: no `delete_slide` API exists.

    PowerPoint's "Sections" feature keeps its own slide-id list in a
    presentation.xml extLst extension, duplicating sldIdLst. Draining sldIdLst
    without also dropping that extension leaves it pointing at slide ids that
    exist nowhere in the file — python-pptx's own reader doesn't validate this
    (so a reload-and-inspect check won't catch it), but PowerPoint silently
    "repairs" it and stricter readers (WPS) refuse to open the file at all.
    Stale regardless of whether one example slide survives or none do, so this
    strip always runs.
    """
    from pptx.oxml.ns import qn

    xml_slides = prs.slides._sldIdLst
    for sld in list(xml_slides):
        if sld.rId == keep_rid:
            continue
        prs.part.drop_rel(sld.rId)
        xml_slides.remove(sld)

    ext_lst = prs.part._element.find(qn("p:extLst"))
    if ext_lst is not None:
        for ext in ext_lst.findall(qn("p:ext")):
            if ext.get("uri") == _P14_SECTION_EXT_URI:
                ext_lst.remove(ext)


def _add_cover_title_text(slide, title: str, subtitle: str) -> None:
    """Add title/subtitle as plain text boxes directly onto the template's own
    cover slide — reusing its existing background untouched, no image
    re-embedding — styled to match its Title Slide layout's ctrTitle/subTitle
    placeholders (position/size/font lifted from slideLayout1.xml and the
    master's title/body styles). Deliberately plain text boxes, not
    placeholders: this slide's own layout link (Blank) defines neither
    ctrTitle nor subTitle, and adding placeholder shapes with no matching
    layout definition is exactly the kind of structural mismatch a strict
    reader can refuse (see `_remove_other_slides`'s Sections lesson)."""
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.util import Emu, Pt

    title_box = slide.shapes.add_textbox(*(Emu(v) for v in _COVER_TITLE_XFRM))
    tf = title_box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.BOTTOM
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = title
    run.font.size = Pt(60)
    run.font.name = DECK_FONT

    if subtitle:
        sub_box = slide.shapes.add_textbox(*(Emu(v) for v in _COVER_SUBTITLE_XFRM))
        tf2 = sub_box.text_frame
        tf2.word_wrap = True
        p2 = tf2.paragraphs[0]
        p2.alignment = PP_ALIGN.CENTER
        run2 = p2.add_run()
        run2.text = subtitle
        run2.font.size = Pt(24)
        run2.font.name = DECK_FONT


def _apply_deck_font(prs, typeface: str) -> None:
    """Set the whole deck in `typeface`, across every part of the package.

    Themes get their major/minor Latin font replaced — that is what
    `+mj-lt`/`+mn-lt` references and un-styled runs resolve to. Every other
    XML part (slides, layouts, masters, notes, charts) has each EXPLICIT Latin
    typeface replaced: the branded template's master and layouts hard-code
    Calibri in their list styles, so fixing the theme alone would leave text
    inheriting from those styles in Calibri. Theme references (`+…`) are kept,
    since they now resolve to `typeface` anyway. Charts also get it set on the
    chart itself, as some viewers ignore the theme for chart text.

    Only the LATIN slot is touched: Arial has no Devanagari, and leaving the
    complex-script slot alone keeps Nepali text on the viewer's fallback font
    instead of rendering boxes."""
    from lxml import etree
    from pptx.opc.constants import CONTENT_TYPE as CT
    from pptx.oxml.ns import qn

    latin_tag = qn("a:latin")
    for part in prs.part.package.iter_parts():
        if part.content_type == CT.OFC_THEME:
            root = etree.fromstring(part.blob)
            for latin in root.iterfind(f".//{qn('a:fontScheme')}/*/{latin_tag}"):
                latin.set("typeface", typeface)
            part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        elif hasattr(part, "_element"):
            for latin in part._element.iter(latin_tag):
                if not latin.get("typeface", "").startswith("+"):
                    latin.set("typeface", typeface)

    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_chart:
                shape.chart.font.name = typeface


def _build_pptx_bytes(
    title: str, subtitle: str, slides: list[dict], image_paths: dict[str, str]
) -> bytes:
    """Render the deck with python-pptx. Sync — run in a thread."""
    from pptx import Presentation
    from pptx.util import Emu

    cover_slide = None
    if _TEMPLATE_PATH.exists():
        prs = Presentation(str(_TEMPLATE_PATH))
        cover_rid, cover_slide = _find_cover_slide(prs)
        # No deck title -> no title slide at all, matching the pre-template
        # contract; drop the cover example along with the other two.
        _remove_other_slides(prs, keep_rid=cover_rid if title else None)
        if not title:
            cover_slide = None
    else:
        prs = Presentation()

    if title:
        if cover_slide is not None:
            _add_cover_title_text(cover_slide, title, subtitle)
        else:
            ts = prs.slides.add_slide(prs.slide_layouts[_LAYOUT_TITLE])
            ts.shapes.title.text = title
            ts.placeholders[1].text = subtitle  # empty string leaves the placeholder blank

    for spec in slides:
        bullets = spec.get("bullets") or []
        table = spec.get("table")
        stats = spec.get("stats") or []
        image = spec.get("image")
        chart = spec.get("chart")
        layout = _LAYOUT_TITLE_AND_CONTENT if bullets else _LAYOUT_TITLE_ONLY
        slide = prs.slides.add_slide(prs.slide_layouts[layout])
        slide.shapes.title.text = str(spec.get("title") or "")

        # Zone offsets (fractions of slide height) are calibrated against this
        # template's own title placeholder, which the master pushes down to
        # ~29% to clear the branded header artwork (logo + divider line) --
        # not the stock Office default's ~5%-25%. A template swap would need
        # these recalibrated the same way.

        # image/chart are full-slide content, mutually exclusive with
        # bullets/table/stats (enforced in _validate) — a simpler layout than
        # trying to stack a picture or a chart alongside the other zones.
        if image is not None:
            _add_image(
                slide, image, image_paths, int(prs.slide_height * 0.30),
                int(prs.slide_height * 0.58), prs.slide_width,
            )
            continue
        if chart is not None:
            _add_chart(
                slide, chart, int(prs.slide_height * 0.30),
                int(prs.slide_height * 0.58), prs.slide_width,
            )
            continue

        if stats:
            _add_stats(
                slide, stats, int(prs.slide_height * 0.30), int(prs.slide_height * 0.22), prs.slide_width
            )

        if bullets:
            _add_bullets(slide, bullets)
            if stats or table is not None:
                # Shrink/reposition the bullet box so it doesn't collide with
                # whatever else shares the slide. Capture the inherited
                # left/width first: python-pptx placeholder position setters
                # write a bare xfrm, so setting only top/height zeroes
                # left/width instead of keeping the layout's values. Below the
                # stats row when one is present (matching the reference "cards
                # + one summary line" layout); otherwise the existing
                # bullets+table split.
                body = slide.placeholders[1]
                left, width = body.left, body.width
                if stats:
                    body.top = Emu(int(prs.slide_height * 0.55))
                    body.height = Emu(int(prs.slide_height * 0.33))
                else:
                    body.top = Emu(int(prs.slide_height * 0.30))
                    body.height = Emu(int(prs.slide_height * 0.22))
                body.left = left
                body.width = width
        if table is not None:
            # All three (stats+bullets+table) on one slide is an unsupported
            # edge case visually -- it renders without error but cramped, and
            # is not a combination the tool's own description encourages.
            if stats:
                table_top = int(prs.slide_height * (0.80 if bullets else 0.55))
            else:
                table_top = int(prs.slide_height * (0.55 if bullets else 0.32))
            _add_table(slide, table, table_top, prs.slide_width)

    _apply_deck_font(prs, DECK_FONT)
    buffer = BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


async def _create_pptx(args: dict[str, Any]) -> str:
    validated = _validate(args)
    if isinstance(validated, str):  # an ERROR: message
        return validated
    title, subtitle, slides, filename = validated

    # Resolve any embedded image file_ids up front: resolve_file is async
    # (owner-scoped lookup) but the actual render is sync python-pptx work run
    # off the loop via asyncio.to_thread below, so it can't resolve anything
    # itself. Keyed by file_id, not by slide index, so the same image
    # referenced twice is only resolved once.
    image_paths: dict[str, str] = {}
    for idx, spec in enumerate(slides):
        image = spec.get("image")
        if image is None or image["file_id"] in image_paths:
            continue
        file_id = image["file_id"]
        record = await resolve_file(file_id)
        if record is None:
            return f"ERROR: slides[{idx}].image.file_id: no such file (unknown id, or you don't own it)."
        ext = Path(record.path).suffix.lower()
        if ext == ".svg":
            return (
                f"ERROR: slides[{idx}].image.file_id is an SVG (e.g. from create_chart) — SVG "
                "can't be embedded directly. Use this slide's own 'chart' field for a native "
                "chart, or 'image' only for a raster photo/logo (PNG/JPEG/etc)."
            )
        if ext not in ingest.IMAGE_EXTS:
            return (
                f"ERROR: slides[{idx}].image.file_id: '{ext or 'unknown'}' is not an image "
                "this tool can embed (PNG/JPEG/WebP/TIFF/BMP)."
            )
        image_paths[file_id] = record.path

    try:
        data = await asyncio.to_thread(_build_pptx_bytes, title, subtitle, slides, image_paths)
    except ReadError as exc:
        return f"ERROR: could not read an embedded image ({exc})."
    except Exception as exc:  # noqa: BLE001 - report back, don't raise into the loop
        return f"ERROR: failed to build PPTX: {exc}"

    # The exact validated args, not a re-extraction from the saved bytes — lets
    # the frontend render a faithful per-slide preview (real bullets/tables)
    # instead of parsing the binary file, which has no browser-native renderer.
    preview = {"title": title, "subtitle": subtitle, "slides": slides}
    record = await file_store.save(
        data, filename=filename, media_type=PPTX_MEDIA_TYPE, preview=preview
    )
    # Same string shape as create_excel/create_pdf/create_docx so the frontend parses it identically.
    return (
        f"Created PowerPoint presentation '{record.filename}' "
        f"({record.size} bytes, {len(slides)} slide(s)). "
        f"Download it at: GET /v1/files/{record.id}"
    )


SPEC = LocalToolSpec(
    name="create_pptx",
    description=(
        "Create a PowerPoint (.pptx) slide deck and return a download link. Use this "
        "when the user asks for a presentation, slides, or a deck. Provide 'slides' "
        "(array of {title?, bullets?, table?, stats?, image?, chart?}) and optionally "
        "a deck 'title' and 'subtitle' (rendered as a title slide) and a 'filename'. "
        "Each slide may have a 'title', 'bullets' (array of short strings, one "
        "level), a 'table' ({headers?, rows[][]}), and/or 'stats' (array of up to "
        f"{MAX_STATS_PER_SLIDE} {{value, label, note?}} — a row of highlight-number "
        "cards, e.g. {value: '1956', label: 'Year established'}). Prefer 'stats' over "
        "'bullets' for a handful of key figures/facts at a glance — it reads far "
        "better than the same numbers written out as a bullet list, and is what a "
        "human deck author would build for that content. "
        "A slide may INSTEAD have 'image' ({file_id, caption?} — a photo/logo/"
        "screenshot already uploaded or generated this conversation, embedded at "
        "native aspect ratio) or 'chart' ({chart_type, labels, series} — exactly "
        "create_chart's own shape, rendered as a real, native, editable PowerPoint "
        "chart, not a picture; prefer this over calling create_chart separately when "
        "the chart belongs IN the deck). 'image'/'chart' fill the whole slide below "
        "the title and cannot be combined with 'bullets'/'table'/'stats' on the same "
        "slide — put those on their own slide instead. Full Unicode is supported. "
        f"Keep decks under {MAX_SLIDES} slides and {MAX_BULLETS_PER_SLIDE} bullets per "
        f"slide, and a table to {MAX_TABLE_ROWS_PER_SLIDE} rows (incl. header) on a "
        f"slide without bullets or {MAX_TABLE_ROWS_WITH_BULLETS} on one with bullets "
        "— a table does not paginate on a slide, unlike a document. For a "
        "document rather than slides use create_docx or create_pdf."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Optional deck title (makes a title slide)."},
            "subtitle": {"type": "string", "description": "Optional subtitle for the title slide."},
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Optional slide title."},
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional bullet points, one string each.",
                        },
                        "table": {
                            "type": "object",
                            "properties": {
                                "headers": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional column headers.",
                                },
                                "rows": {
                                    "type": "array",
                                    "items": {"type": "array"},
                                    "description": "Rows of cell values; each row is an array.",
                                },
                            },
                            "required": ["rows"],
                            "description": "Optional simple table.",
                        },
                        "stats": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "value": {
                                        "type": "string",
                                        "description": "The highlight figure, e.g. '1956' or '18.4B'.",
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": "What the figure is, e.g. 'Year established'.",
                                    },
                                    "note": {
                                        "type": "string",
                                        "description": "Optional smaller supporting detail.",
                                    },
                                },
                                "required": ["value", "label"],
                            },
                            "description": (
                                f"Optional row of up to {MAX_STATS_PER_SLIDE} highlight-number "
                                "cards — prefer this over 'bullets' for key figures at a glance."
                            ),
                        },
                        "image": {
                            "type": "object",
                            "properties": {
                                "file_id": {
                                    "type": "string",
                                    "description": (
                                        "Id of an already-uploaded or generated raster image "
                                        "(PNG/JPEG/WebP/TIFF/BMP) — not an SVG chart file."
                                    ),
                                },
                                "caption": {
                                    "type": "string",
                                    "description": "Optional small caption shown below the image.",
                                },
                            },
                            "required": ["file_id"],
                            "description": (
                                "Optional full-slide image (a photo/logo/screenshot). Fills the "
                                "slide below the title; cannot combine with bullets/table/stats "
                                "or with 'chart' on the same slide."
                            ),
                        },
                        "chart": {
                            "type": "object",
                            "properties": {
                                "chart_type": {
                                    "type": "string",
                                    "enum": list(chart_tool.CHART_TYPES),
                                    "description": "Same as create_chart's 'chart_type'.",
                                },
                                "labels": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Same as create_chart's 'labels'.",
                                },
                                "series": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "data": {"type": "array", "items": {"type": "number"}},
                                        },
                                        "required": ["data"],
                                    },
                                    "description": "Same as create_chart's 'series'.",
                                },
                            },
                            "required": ["chart_type", "labels", "series"],
                            "description": (
                                "Optional full-slide native chart — exactly create_chart's own "
                                "shape, rendered as a real editable PowerPoint chart rather than "
                                "a picture. Fills the slide below the title; cannot combine with "
                                "bullets/table/stats or with 'image' on the same slide."
                            ),
                        },
                    },
                },
                "description": (
                    "Slides in order; each needs at least a title, bullets, a table, stats, an "
                    "image, or a chart."
                ),
            },
            "filename": {
                "type": "string",
                "description": "Output file name, e.g. 'review.pptx' (default 'presentation.pptx').",
            },
        },
        "required": ["slides"],
    },
    func=_create_pptx,
)

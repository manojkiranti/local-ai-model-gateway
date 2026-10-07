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
import json
from io import BytesIO
from pathlib import Path
from typing import Any

from ...files import images, ingest
from ...files.readers import ReadError
from ...files.store import PPTX_MEDIA_TYPE, file_store, resolve_file
from . import chart as chart_tool
from . import pptx_icons
from .memo import _balance_brackets  # create_memo's measured repair; one copy
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

# Infographic layouts. Each is a WHOLE-slide layout (only a title and a
# `takeaway` banner may share the slide), so its geometry is fixed and the
# frontend preview can mirror it exactly. Item caps bound the grid; the text
# caps are what keep a slide glanceable — a card that needs 300 characters is
# a paragraph, and refusing it (with the limit named) is what makes the model
# write slide copy instead of prose. Constants, never arguments.
# One item is accepted (it renders fine) even though the description asks for
# 2+: refusing a one-column comparison was one more way to loop the model.
MIN_LAYOUT_ITEMS = 1
MAX_CARDS = 6
MAX_STEPS = 6
MAX_TIMELINE = 6
MAX_COLUMNS = 3
MAX_POINTS_PER_COLUMN = 5
MAX_PROGRESS = 6
# A chart/image may share its slide with a short side panel of takeaways.
MAX_SIDE_BULLETS = 4
MAX_SIDE_STATS = 3
_TEXT_CAPS = {
    "card.title": 40, "card.text": 110,
    "step.title": 28, "step.text": 100,
    "event.date": 20, "event.title": 32, "event.text": 90,
    "column.heading": 30, "column.point": 70,
    "progress.label": 40, "progress.note": 50,
    "side.bullet": 90, "takeaway": 140,
}
_LAYOUT_KEYS = ("cards", "steps", "timeline", "comparison", "progress")

_DEFAULT_FILENAME = "presentation.pptx"


# How far past its comfortable length text may run before it is refused. Up
# to the comfortable length it renders at full size; beyond it `_fit` steps
# the font down instead. Measured live: refusing a 35-character title against
# a 32-character limit made the model re-send it unchanged until the turn hit
# max_iterations with NO deck — a model cannot count characters, so a hard
# limit near the comfortable length is a retry loop, not a style rule. Only
# text this far over (a paragraph, not slide copy) is refused.
_HARD_CAP_FACTOR = 3


def _cap(value: Any, key: str, where: str) -> str | None:
    """An ERROR: string if `value` is not a string, or is paragraph-length."""
    if not isinstance(value, str):
        return f"ERROR: {where} must be a string."
    limit = _TEXT_CAPS[key] * _HARD_CAP_FACTOR
    if len(value) > limit:
        return (
            f"ERROR: {where} is {len(value)} characters — that is a paragraph, not slide copy. "
            f"Cut it to a short phrase (aim for {_TEXT_CAPS[key]} characters or fewer)."
        )
    return None


def _fit(text: str, base_pt: int, key: str) -> int:
    """The font size for `text`: full size up to its comfortable length, then
    2 pt smaller up to 1.5x it, then 4 pt smaller (never below 8 pt). The
    frontend's `fitPt` (deck-layout.ts) is this same rule."""
    n, comfortable = len(text), _TEXT_CAPS[key]
    if n <= comfortable:
        return base_pt
    return max(base_pt - (2 if n <= comfortable * 1.5 else 4), 8)


def _items(slide: dict, key: str, idx: int, low: int, high: int) -> list | str:
    items = slide.get(key)
    if not isinstance(items, list) or not (low <= len(items) <= high):
        count = len(items) if isinstance(items, list) else "a non-array"
        return f"ERROR: slides[{idx}].{key} must be an array of {low}-{high} items (got {count})."
    return items


def _validate_layout(slide: dict, key: str, idx: int) -> dict | str:
    """Validate one whole-slide infographic layout; return the slide with its
    icons resolved (unknown names dropped — see pptx_icons), or an ERROR."""
    where = f"slides[{idx}].{key}"
    out = dict(slide)

    if key == "comparison":
        cols = _items(slide, key, idx, MIN_LAYOUT_ITEMS, MAX_COLUMNS)
        if isinstance(cols, str):
            return cols
        clean = []
        for c, col in enumerate(cols):
            if not isinstance(col, dict) or not col.get("heading"):
                return f"ERROR: {where}[{c}] must be an object with 'heading' and 'points'."
            if err := _cap(col["heading"], "column.heading", f"{where}[{c}].heading"):
                return err
            points = col.get("points")
            if not isinstance(points, list) or not (1 <= len(points) <= MAX_POINTS_PER_COLUMN):
                return f"ERROR: {where}[{c}].points must be an array of 1-{MAX_POINTS_PER_COLUMN} short strings."
            for p, point in enumerate(points):
                if err := _cap(point, "column.point", f"{where}[{c}].points[{p}]"):
                    return err
            clean.append({"heading": col["heading"], "points": list(points), "icon": pptx_icons.resolve_icon(col.get("icon"))})
        out[key] = clean
        return out

    if key == "progress":
        bars = _items(slide, key, idx, 1, MAX_PROGRESS)
        if isinstance(bars, str):
            return bars
        clean = []
        for b, bar in enumerate(bars):
            if not isinstance(bar, dict) or not bar.get("label"):
                return f"ERROR: {where}[{b}] must be an object with 'label' and 'value' (0-100)."
            if err := _cap(bar["label"], "progress.label", f"{where}[{b}].label"):
                return err
            try:
                value = float(str(bar.get("value")).strip().rstrip("%"))
            except ValueError:
                return f"ERROR: {where}[{b}].value must be a number from 0 to 100 (a percentage)."
            if not 0 <= value <= 100:
                return f"ERROR: {where}[{b}].value is {value:g}; it must be 0-100 (a percentage)."
            note = bar.get("note")
            if note is not None and (err := _cap(note, "progress.note", f"{where}[{b}].note")):
                return err
            clean.append({"label": bar["label"], "value": value, "note": note})
        out[key] = clean
        return out

    # cards / steps / timeline: the same {title, text?, (icon|date)} family.
    high = {"cards": MAX_CARDS, "steps": MAX_STEPS, "timeline": MAX_TIMELINE}[key]
    prefix = {"cards": "card", "steps": "step", "timeline": "event"}[key]
    items = _items(slide, key, idx, MIN_LAYOUT_ITEMS, high)
    if isinstance(items, str):
        return items
    clean = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("title"):
            need = "'date' and 'title'" if key == "timeline" else "'title'"
            return f"ERROR: {where}[{i}] must be an object with {need} (and optional 'text')."
        if err := _cap(item["title"], f"{prefix}.title", f"{where}[{i}].title"):
            return err
        text = item.get("text")
        if text is not None and (err := _cap(text, f"{prefix}.text", f"{where}[{i}].text")):
            return err
        entry = {"title": item["title"], "text": text}
        if key == "timeline":
            if not item.get("date"):
                return f"ERROR: {where}[{i}].date is required (e.g. 'Q1 2026' or 'Shrawan 2082')."
            if err := _cap(item["date"], "event.date", f"{where}[{i}].date"):
                return err
            entry["date"] = item["date"]
        else:
            entry["icon"] = pptx_icons.resolve_icon(item.get("icon"))
        clean.append(entry)
    out[key] = clean
    return out


_STRUCTURED_KEYS = (
    "bullets", "table", "stats", "image", "chart",
    "cards", "steps", "timeline", "comparison", "progress",
)


def _decode_json(value: Any) -> Any:
    """A JSON-encoded string where an array/object belongs, decoded.

    Measured live (qwen3.5:35b-a3b, create_pptx): the model sometimes sends
    `slides` as a JSON STRING. Refused with "must be an array" it re-sent the
    same string eight times and the turn ended at max_iterations with no deck
    — the failure create_memo measured and fixed first, so this reuses its
    bracket repair. Anything that still is not JSON is left as-is, for the
    ordinary validation to refuse."""
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        text = value.strip()
        for candidate in (text, _balance_brackets(text)):
            try:
                return json.loads(candidate)
            except ValueError:
                continue
    return value


def _decode_slide(slide: Any) -> Any:
    slide = _decode_json(slide)
    if isinstance(slide, dict):
        slide = {k: _decode_json(v) if k in _STRUCTURED_KEYS else v for k, v in slide.items()}
    return slide


def _side_panel_fits(slide: dict) -> bool:
    """Can this chart/image slide's extras sit in its side panel as they are?"""
    bullets, stats = slide.get("bullets"), slide.get("stats")
    if slide.get("table") is not None or (bullets and stats):
        return False
    if bullets:
        return (
            isinstance(bullets, list)
            and len(bullets) <= MAX_SIDE_BULLETS
            and all(isinstance(b, str) and len(b) <= _TEXT_CAPS["side.bullet"] * 1.5 for b in bullets)
        )
    if stats:
        return isinstance(stats, list) and len(stats) <= MAX_SIDE_STATS
    return True


def _split_overpacked(slide: Any) -> list[Any]:
    """Split a slide that packs more than one layout into consecutive slides.

    Measured live against qwen3.5:35b-a3b: asked for a deck, it routinely puts
    `bullets` beside `cards` on every slide. Refusing that made the model
    retry the whole deck, fix some slides, break others, and end the turn at
    max_iterations with NO deck at all. So an over-packed slide is REPAIRED —
    each layout gets its own slide, and leftover bullets/stats/table move to a
    continuation slide under the same title — instead of refused. Nothing the
    model wrote is dropped. The `takeaway` stays on the first slide.
    """
    if not isinstance(slide, dict):
        return [slide]
    layouts = [k for k in _LAYOUT_KEYS if slide.get(k) is not None]
    visual_keys = [k for k in ("chart", "image") if slide.get(k) is not None]
    extras = {k: slide[k] for k in ("bullets", "stats", "table") if slide.get(k) is not None}
    base = {"title": slide.get("title")} if slide.get("title") is not None else {}

    if not layouts and len(visual_keys) <= 1 and (not visual_keys or _side_panel_fits(slide)):
        return [slide]  # nothing to repair (or nothing this can repair)

    primaries: list[dict] = [{**base, key: slide[key]} for key in layouts + visual_keys]
    if visual_keys and not layouts and len(visual_keys) == 1 and extras:
        # One visual whose extras overflow the side panel: keep what fits
        # beside it (stats first, as the denser read), move the rest on.
        candidate = {**primaries[0], **{k: v for k, v in extras.items() if k == "stats"}}
        if extras.get("stats") is not None and _side_panel_fits(candidate):
            primaries[0] = candidate
            extras = {k: v for k, v in extras.items() if k != "stats"}
    if slide.get("takeaway") is not None:
        primaries[0]["takeaway"] = slide["takeaway"]
    return primaries + ([{**base, **extras}] if extras else [])


def _validate(args: dict[str, Any]) -> tuple[str, str, list[dict], str] | str:
    """Return (title, subtitle, slides, filename) on success, or an ERROR: string.

    `slides` is NORMALIZED (icons resolved, progress values numeric), not the
    raw args: it is both what gets rendered and what the preview is built from,
    so the two can never disagree about which icon a card shows."""
    raw_slides = _decode_json(args.get("slides"))
    if not isinstance(raw_slides, list) or not raw_slides:
        return (
            "ERROR: 'slides' is required and must be a non-empty array of slide objects — "
            "send it as a real JSON array, not a string."
        )
    raw_slides = [_decode_slide(slide) for slide in raw_slides]
    if len(raw_slides) > MAX_SLIDES:
        return f"ERROR: too many slides ({len(raw_slides)}); the limit is {MAX_SLIDES}. Split the deck."
    raw_slides = [part for slide in raw_slides for part in _split_overpacked(slide)]
    if len(raw_slides) > MAX_SLIDES:
        return (
            f"ERROR: too many slides ({len(raw_slides)} once slides holding several layouts are "
            f"split one layout per slide); the limit is {MAX_SLIDES}. Use fewer slides."
        )

    slides: list[dict] = []
    for idx, slide in enumerate(raw_slides):
        if not isinstance(slide, dict):
            return f"ERROR: slides[{idx}] must be an object (title plus one layout)."
        bullets = slide.get("bullets")
        table = slide.get("table")
        stats = slide.get("stats")
        image = slide.get("image")
        chart = slide.get("chart")
        takeaway = slide.get("takeaway")
        layouts = [k for k in _LAYOUT_KEYS if slide.get(k) is not None]
        if not (slide.get("title") or bullets or table is not None or stats or image or chart or layouts):
            return (
                f"ERROR: slides[{idx}] needs at least one of 'title', 'bullets', 'table', 'stats', "
                "'image', 'chart', 'cards', 'steps', 'timeline', 'comparison' or 'progress'."
            )

        if len(layouts) > 1:
            return (
                f"ERROR: slides[{idx}] has {', '.join(repr(k) for k in layouts)} — one layout per "
                "slide. Put each on its own slide."
            )
        if layouts and (bullets or table is not None or stats or image or chart):
            return (
                f"ERROR: slides[{idx}].{layouts[0]} is a whole-slide layout — only 'title' and "
                "'takeaway' may share its slide. Move bullets/table/stats/image/chart to another slide."
            )

        # image/chart: full-slide on their own, or a split slide with a short
        # side panel of takeaways (bullets OR stats). Never with a table — a
        # table needs the width the visual is using.
        if image and chart:
            return f"ERROR: slides[{idx}] has both 'image' and 'chart' — use one per slide."
        if (image or chart) and table is not None:
            return (
                f"ERROR: slides[{idx}] combines 'image'/'chart' with 'table' — put the table on its "
                "own slide."
            )
        if (image or chart) and bullets and stats:
            return (
                f"ERROR: slides[{idx}] puts both 'bullets' and 'stats' beside an image/chart — the "
                "side panel holds one of them. Choose one, or split the slide."
            )
        if (image or chart) and bullets:
            if not isinstance(bullets, list) or not 1 <= len(bullets) <= MAX_SIDE_BULLETS:
                return (
                    f"ERROR: slides[{idx}].bullets beside an image/chart must be 1-{MAX_SIDE_BULLETS} "
                    "short takeaways."
                )
            for b, text in enumerate(bullets):
                if err := _cap(text, "side.bullet", f"slides[{idx}].bullets[{b}]"):
                    return err
        if (image or chart) and stats and (not isinstance(stats, list) or len(stats) > MAX_SIDE_STATS):
            return (
                f"ERROR: slides[{idx}].stats beside an image/chart is limited to {MAX_SIDE_STATS} "
                "cards (they stack in the side panel)."
            )

        if takeaway is not None:
            if err := _cap(takeaway, "takeaway", f"slides[{idx}].takeaway"):
                return err
            if table is not None:
                return (
                    f"ERROR: slides[{idx}].takeaway cannot share a slide with a table (a table "
                    "fills the slide). Put the takeaway on a slide with a layout, stats, a chart, "
                    "an image or bullets."
                )

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

        if layouts:
            normalized = _validate_layout(slide, layouts[0], idx)
            if isinstance(normalized, str):
                return normalized
            slides.append(normalized)
        else:
            slides.append(slide)

    title = str(args.get("title") or "")
    subtitle = str(args.get("subtitle") or "")
    filename = str(args.get("filename") or _DEFAULT_FILENAME)
    if not filename.lower().endswith(".pptx"):
        filename += ".pptx"
    return title, subtitle, slides, filename


_LAYOUT_TITLE = 0
_LAYOUT_TITLE_AND_CONTENT = 1
_LAYOUT_TITLE_ONLY = 5
_LAYOUT_BLANK = 6


def _add_bullets(slide, bullets: list[str]) -> None:
    """Fill the content placeholder (index 1 on the Title-and-Content layout)."""
    body = slide.placeholders[1].text_frame
    body.clear()  # leaves one empty paragraph
    for i, text in enumerate(bullets):
        paragraph = body.paragraphs[0] if i == 0 else body.add_paragraph()
        paragraph.text = text
        paragraph.level = 0
    _set_text_size(body, CONTENT_BODY_PT)


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
    for cell in grid.iter_cells():
        _set_text_size(cell.text_frame, CONTENT_BODY_PT)


def _add_image(
    slide,
    image: dict,
    image_paths: dict[str, str],
    top_emu: int,
    zone_height_emu: int,
    slide_width: int,
    left_emu: int | None = None,
    width_emu: int | None = None,
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

    # Default zone: the full width inside the 5% side margins; a split slide
    # passes its left-hand visual box instead.
    margin = int(slide_width * 0.05)
    zone_left = margin if left_emu is None else left_emu
    max_w = slide_width - 2 * margin if width_emu is None else width_emu
    scale = min(max_w / summary.width, pic_zone_h / summary.height)
    w = int(summary.width * scale)
    h = int(summary.height * scale)
    left = zone_left + (max_w - w) // 2
    slide.shapes.add_picture(path, Emu(left), Emu(top_emu), width=Emu(w), height=Emu(h))

    if caption:
        box = slide.shapes.add_textbox(
            Emu(zone_left), Emu(top_emu + h), Emu(max_w), Emu(max(caption_h, 1))
        )
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = caption
        run.font.size = Pt(11)
        run.font.italic = True


def _add_chart(
    slide,
    chart: dict,
    top_emu: int,
    height_emu: int,
    slide_width: int,
    left_emu: int | None = None,
    width_emu: int | None = None,
) -> None:
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
    left = margin if left_emu is None else left_emu
    width = slide_width - 2 * margin if width_emu is None else width_emu
    graphic_frame = slide.shapes.add_chart(
        type_map[chart_type], Emu(left), Emu(top_emu), Emu(width), Emu(height_emu), chart_data
    )
    rendered = graphic_frame.chart
    # The slide title already names the chart; PowerPoint's auto-title would
    # only repeat the series name.
    rendered.has_title = False
    if chart_type in ("pie", "donut") or len(series) > 1:
        rendered.has_legend = True
        rendered.legend.include_in_layout = False
    else:
        # One series: PowerPoint colours every bar differently and adds a
        # legend of the CATEGORIES, which reads as N series. One brand colour,
        # no legend — the axis labels already name the categories.
        from pptx.dml.color import RGBColor

        rendered.has_legend = False
        plot = rendered.plots[0]
        plot.vary_by_categories = False
        fmt = plot.series[0].format
        if chart_type in ("line",):
            fmt.line.color.rgb = RGBColor(*_STAT_VALUE_COLOR)
        else:
            fmt.fill.solid()
            fmt.fill.fore_color.rgb = RGBColor(*_STAT_VALUE_COLOR)


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
_STAT_LABEL_COLOR = (0x22, 0x22, 0x22)


def _stat_card(slide, stat: dict, left: int, top: int, width: int, height: int) -> None:
    """One rounded "stat card" — a large colored value, a label, and an
    optional smaller note — at an EMU box. Shared by the stats row and the
    split slide's stacked side panel."""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu, Pt

    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Emu(left), Emu(top), Emu(width), Emu(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(*_STAT_CARD_FILL)
    shape.line.color.rgb = RGBColor(*_STAT_CARD_BORDER)
    shape.line.width = Pt(0.75)
    shape.shadow.inherit = False

    tf = shape.text_frame
    tf.word_wrap = True
    inset = Emu(int(width * 0.09))
    tf.margin_left = inset
    tf.margin_right = inset
    tf.margin_top = Emu(int(height * 0.12))

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
    # Explicit: a filled shape's text defaults to the theme's light colour
    # (white), which is invisible on the pale card.
    run_label.font.color.rgb = RGBColor(*_STAT_LABEL_COLOR)

    note = stat.get("note")
    if note:
        p_note = tf.add_paragraph()
        run_note = p_note.add_run()
        run_note.text = str(note)
        run_note.font.size = Pt(9)
        run_note.font.color.rgb = RGBColor(0x66, 0x66, 0x66)


def _add_stats(slide, stats: list[dict], top_emu: int, height_emu: int, slide_width: int) -> None:
    """A row of up to MAX_STATS_PER_SLIDE stat cards ("key facts at a
    glance") — the single highest-impact layout beyond a plain bullet list
    for the numeric highlights a deck author reaches for."""
    n = len(stats)
    margin = int(slide_width * 0.05)
    gap = int(slide_width * 0.02)
    card_width = (slide_width - 2 * margin - (n - 1) * gap) // n
    for i, stat in enumerate(stats):
        _stat_card(slide, stat, margin + i * (card_width + gap), top_emu, card_width, height_emu)


# --- Infographic layouts ------------------------------------------------------
# Geometry is in FRACTIONS of the slide (x of width, y of height), mirrored by
# the frontend's `src/lib/deck-layout.ts` so the in-app preview matches the
# downloaded deck — change a number here and change it there. Every layout
# fills the zone from the content top down to `_ZONE_BOTTOM`, which stops above
# the template's bottom-corner badge; a `takeaway` banner takes the bottom band
# and pulls the zone up to `_ZONE_BOTTOM_WITH_TAKEAWAY`.
_MARGIN_X = 0.05
_CONTENT_W = 0.90
_ZONE_BOTTOM = 0.86
_ZONE_BOTTOM_WITH_TAKEAWAY = 0.755
_TAKEAWAY_BOX = (0.05, 0.775, 0.90, 0.075)
_SPLIT_VISUAL_X = (0.05, 0.55)  # left, width
_SPLIT_SIDE_X = (0.62, 0.33)
_SPLIT_STAT_GAP = 0.025
_SPLIT_STAT_MAX_H = 0.20
_CARD_GAP_X = 0.02
_CARD_GAP_Y = 0.03
_CARD_MAX_H = 0.44
_CARD_INSET_X = 0.015
_CARD_INSET_Y = 0.03
_CARD_ICON_D = 0.09  # icon diameters are fractions of slide HEIGHT (circles stay round)
_STEP_BAND_H = 0.13
_STEP_ICON_D = 0.08
_STEP_TEXT_H = 0.12
_TIMELINE_LINE_Y = 0.20  # below the block top
_TIMELINE_DOT_D = 0.045
_TIMELINE_BLOCK_H = 0.42  # date band + rule + title/text below
_COMPARE_GAP = 0.025
_COMPARE_HEAD_H = 0.11
_COMPARE_POINT_H = 0.065  # one 15 pt point plus its spacing
_COMPARE_ICON_D = 0.06
_PROGRESS_ROW_MAX_H = 0.11
_PROGRESS_LABEL_X = (0.05, 0.27)
_PROGRESS_TRACK_X = (0.34, 0.50)
_PROGRESS_VALUE_X = (0.86, 0.09)
_PROGRESS_TRACK_H = 0.035

# Palette: the brand red the stat cards already use, its dark end for step
# gradients, and neutrals. Same reasoning as _STAT_VALUE_COLOR — the
# template's theme palette is stock Office, so brand colours are explicit.
_BRAND = _STAT_VALUE_COLOR
_BRAND_DARK = (0x7A, 0x00, 0x0A)
_PALE = _STAT_CARD_FILL
_BORDER = _STAT_CARD_BORDER
_INK = _STAT_LABEL_COLOR
_MUTED = (0x55, 0x55, 0x55)
_WHITE = (0xFF, 0xFF, 0xFF)
_TRACK = (0xEE, 0xEE, 0xEE)
_RULE = (0xD9, 0xD9, 0xD9)
_PANEL = (0xF7, 0xF7, 0xF7)
_NEUTRAL_DARK = (0x3A, 0x3A, 0x3A)
_COMPARE_HEADS = (_BRAND, _NEUTRAL_DARK, _BRAND_DARK)


def _blend(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class _Geo:
    """Fractions of the slide -> EMU. x/w scale by width, y/h by height."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height

    def x(self, f: float):
        from pptx.util import Emu

        return Emu(int(self.width * f))

    def y(self, f: float):
        from pptx.util import Emu

        return Emu(int(self.height * f))

    def square(self, d_h: float) -> float:
        """A height-fraction diameter as a width fraction (so circles stay round)."""
        return d_h * self.height / self.width


def _shape(slide, kind, g: _Geo, box: tuple, fill: tuple, line: tuple | None = None):
    """A filled auto-shape at a fractional (left, top, width, height) box."""
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    left, top, width, height = box
    shape = slide.shapes.add_shape(kind, g.x(left), g.y(top), g.x(width), g.y(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(*fill)
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = RGBColor(*line)
        shape.line.width = Pt(0.75)
    shape.shadow.inherit = False
    return shape


def _text(
    slide,
    g: _Geo,
    box: tuple,
    paragraphs: list[list[tuple]],
    *,
    align: str = "left",
    anchor: str = "top",
    space_after: int = 0,
) -> None:
    """A text box at a fractional box. Each paragraph is a list of runs
    `(text, size_pt, bold, rgb)`; colours are always explicit, because text on
    a filled shape otherwise inherits the theme's light colour."""
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.util import Emu, Pt

    left, top, width, height = box
    tb = slide.shapes.add_textbox(g.x(left), g.y(top), g.x(width), g.y(max(height, 0.01)))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Emu(0)
    tf.vertical_anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}[anchor]
    for i, runs in enumerate(paragraphs):
        paragraph = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        paragraph.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER}[align]
        if space_after:
            paragraph.space_after = Pt(space_after)
        for text, size, bold, color in runs:
            run = paragraph.add_run()
            run.text = str(text)
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = RGBColor(*color)


def _icon(slide, g: _Geo, name: str, left: float, top: float, d_h: float, *, badge: bool = True) -> None:
    """An icon `d_h` (height fraction) across: red in a pale circle (`badge`),
    or bare white for use on a brand-coloured band. Missing asset -> no icon."""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu

    path = pptx_icons.icon_path(name, "red" if badge else "white")
    if path is None:
        return
    d = int(g.height * d_h)
    x, y = int(g.width * left), int(g.height * top)
    if badge:
        _shape(slide, MSO_SHAPE.OVAL, g, (left, top, g.square(d_h), d_h), _PALE)
        inner = int(d * 0.56)
        offset = (d - inner) // 2
        slide.shapes.add_picture(str(path), Emu(x + offset), Emu(y + offset), Emu(inner), Emu(inner))
    else:
        slide.shapes.add_picture(str(path), Emu(x), Emu(y), Emu(d), Emu(d))


def _centered(top: float, bottom: float, block: float) -> float:
    """Where a block of height `block` starts so it sits mid-zone (never above `top`)."""
    return top + max(0.0, (bottom - top - block) / 2)


def _grid(n: int) -> tuple[int, int]:
    """(columns, rows) for n cards: one row up to 3, 2x2 for 4, 3 across for 5-6."""
    cols = n if n <= 3 else (2 if n == 4 else 3)
    return cols, -(-n // cols)


def _add_cards(slide, g: _Geo, cards: list[dict], top: float, bottom: float) -> None:
    """Icon cards: an icon badge, a bold title and one short line per card.
    A partial last row is centered rather than left-hanging."""
    from pptx.enum.shapes import MSO_SHAPE

    n = len(cards)
    cols, rows = _grid(n)
    cw = (_CONTENT_W - (cols - 1) * _CARD_GAP_X) / cols
    ch = min((bottom - top - (rows - 1) * _CARD_GAP_Y) / rows, _CARD_MAX_H)
    top = _centered(top, bottom, rows * ch + (rows - 1) * _CARD_GAP_Y)
    for i, card in enumerate(cards):
        r, c = divmod(i, cols)
        in_row = cols if r < rows - 1 else n - (rows - 1) * cols
        row_w = in_row * cw + (in_row - 1) * _CARD_GAP_X
        left = _MARGIN_X + (_CONTENT_W - row_w) / 2 + c * (cw + _CARD_GAP_X)
        t = top + r * (ch + _CARD_GAP_Y)
        _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, g, (left, t, cw, ch), _WHITE, _BORDER)
        y = t + _CARD_INSET_Y
        if card.get("icon"):
            _icon(slide, g, card["icon"], left + _CARD_INSET_X, y, _CARD_ICON_D)
            y += _CARD_ICON_D + 0.02
        paragraphs = [[(card["title"], _fit(card["title"], 16, "card.title"), True, _INK)]]
        if card.get("text"):
            paragraphs.append([(card["text"], _fit(card["text"], 12, "card.text"), False, _MUTED)])
        _text(
            slide, g, (left + _CARD_INSET_X, y, cw - 2 * _CARD_INSET_X, t + ch - y - 0.015),
            paragraphs, space_after=4,
        )


def _add_steps(slide, g: _Geo, steps: list[dict], top: float, bottom: float) -> None:
    """A left-to-right process: a band of chevrons (brand red darkening along
    the flow) carrying each step's title, with an optional icon and a short
    line underneath. Icons line up across steps whenever any step has one."""
    from pptx.enum.shapes import MSO_SHAPE

    n = len(steps)
    slot = _CONTENT_W / n
    any_icon = any(s.get("icon") for s in steps)
    any_text = any(s.get("text") for s in steps)
    block = (
        _STEP_BAND_H + 0.035
        + (_STEP_ICON_D + 0.02 if any_icon else 0)
        + (_STEP_TEXT_H if any_text else 0)
    )
    top = _centered(top, bottom, block)
    for i, step in enumerate(steps):
        left = _MARGIN_X + i * slot
        kind = MSO_SHAPE.PENTAGON if i == 0 else MSO_SHAPE.CHEVRON
        color = _blend(_BRAND, _BRAND_DARK, i / (n - 1) if n > 1 else 0)
        _shape(slide, kind, g, (left, top, slot, _STEP_BAND_H), color)
        _text(
            slide, g, (left + slot * 0.14, top, slot * 0.70, _STEP_BAND_H),
            [[(step["title"], _fit(step["title"], 14, "step.title"), True, _WHITE)]], align="center", anchor="middle",
        )
        y = top + _STEP_BAND_H + 0.035
        if any_icon:
            if step.get("icon"):
                _icon(slide, g, step["icon"], left + (slot - g.square(_STEP_ICON_D)) / 2, y, _STEP_ICON_D)
            y += _STEP_ICON_D + 0.02
        if step.get("text"):
            _text(
                slide, g, (left + 0.008, y, slot - 0.016, bottom - y),
                [[(step["text"], _fit(step["text"], 12, "step.text"), False, _MUTED)]], align="center",
            )


def _add_timeline(slide, g: _Geo, events: list[dict], top: float, bottom: float) -> None:
    """A horizontal timeline: a rule with a marker per event, the date above
    it in brand red and the title + short line below."""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Pt

    n = len(events)
    slot = _CONTENT_W / n
    top = _centered(top, bottom, _TIMELINE_BLOCK_H)
    bottom = min(bottom, top + _TIMELINE_BLOCK_H)
    line_y = top + _TIMELINE_LINE_Y
    _shape(slide, MSO_SHAPE.RECTANGLE, g, (_MARGIN_X, line_y - 0.003, _CONTENT_W, 0.006), _RULE)
    dot_w = g.square(_TIMELINE_DOT_D)
    for i, event in enumerate(events):
        left = _MARGIN_X + i * slot
        _text(
            slide, g, (left + 0.005, top + 0.03, slot - 0.01, 0.13),
            [[(event["date"], _fit(event["date"], 16, "event.date"), True, _BRAND)]], align="center", anchor="bottom",
        )
        dot = _shape(
            slide, MSO_SHAPE.OVAL, g,
            (left + (slot - dot_w) / 2, line_y - _TIMELINE_DOT_D / 2, dot_w, _TIMELINE_DOT_D),
            _BRAND, _WHITE,
        )
        dot.line.width = Pt(2)
        paragraphs = [[(event["title"], _fit(event["title"], 14, "event.title"), True, _INK)]]
        if event.get("text"):
            paragraphs.append([(event["text"], _fit(event["text"], 12, "event.text"), False, _MUTED)])
        below = line_y + 0.05
        _text(
            slide, g, (left + 0.008, below, slot - 0.016, bottom - below),
            paragraphs, align="center", space_after=4,
        )


def _add_comparison(slide, g: _Geo, columns: list[dict], top: float, bottom: float) -> None:
    """2-3 side-by-side columns: a coloured header band (optional white icon +
    heading) over a pale panel of short points."""
    from pptx.enum.shapes import MSO_SHAPE

    n = len(columns)
    cw = (_CONTENT_W - (n - 1) * _COMPARE_GAP) / n
    # Panels are sized to the longest column's points, then centred, so a
    # three-point comparison is not a mostly-empty grey slab.
    most = max(len(col["points"]) for col in columns)
    block = min(_COMPARE_HEAD_H + 0.06 + most * _COMPARE_POINT_H, bottom - top)
    top = _centered(top, bottom, block)
    bottom = top + block
    for c, col in enumerate(columns):
        left = _MARGIN_X + c * (cw + _COMPARE_GAP)
        head = _COMPARE_HEADS[c % len(_COMPARE_HEADS)]
        _shape(slide, MSO_SHAPE.RECTANGLE, g, (left, top, cw, _COMPARE_HEAD_H), head)
        text_x = left + 0.015
        if col.get("icon"):
            _icon(
                slide, g, col["icon"], text_x, top + (_COMPARE_HEAD_H - _COMPARE_ICON_D) / 2,
                _COMPARE_ICON_D, badge=False,
            )
            text_x += g.square(_COMPARE_ICON_D) + 0.01
        _text(
            slide, g, (text_x, top, left + cw - 0.015 - text_x, _COMPARE_HEAD_H),
            [[(col["heading"], _fit(col["heading"], 18, "column.heading"), True, _WHITE)]], anchor="middle",
        )
        body_top = top + _COMPARE_HEAD_H
        _shape(slide, MSO_SHAPE.RECTANGLE, g, (left, body_top, cw, bottom - body_top), _PANEL)
        _text(
            slide, g, (left + 0.02, body_top + 0.03, cw - 0.04, bottom - body_top - 0.05),
            [[("●  ", 10, False, head), (point, _fit(point, 15, "column.point"), False, _INK)] for point in col["points"]],
            space_after=10,
        )


def _percent(value: float) -> str:
    return f"{value:g}%"


def _add_progress(slide, g: _Geo, bars: list[dict], top: float, bottom: float) -> None:
    """Labelled percentage bars: label (+ note) left, a rounded track filled
    to the value in brand red, the value as a figure on the right."""
    from pptx.enum.shapes import MSO_SHAPE

    n = len(bars)
    row_h = min((bottom - top) / n, _PROGRESS_ROW_MAX_H)
    top = _centered(top, bottom, n * row_h)
    for i, bar in enumerate(bars):
        y = top + i * row_h
        paragraphs = [[(bar["label"], _fit(bar["label"], 14, "progress.label"), True, _INK)]]
        if bar.get("note"):
            paragraphs.append([(bar["note"], _fit(bar["note"], 10, "progress.note"), False, _MUTED)])
        _text(slide, g, (_PROGRESS_LABEL_X[0], y, _PROGRESS_LABEL_X[1], row_h * 0.9), paragraphs, anchor="middle")
        track_y = y + (row_h * 0.9 - _PROGRESS_TRACK_H) / 2
        track_left, track_w = _PROGRESS_TRACK_X
        track = _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, g, (track_left, track_y, track_w, _PROGRESS_TRACK_H), _TRACK)
        track.adjustments[0] = 0.5
        if bar["value"] > 0:
            fill_w = max(track_w * bar["value"] / 100, _PROGRESS_TRACK_H * g.height / g.width)
            fill = _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, g, (track_left, track_y, fill_w, _PROGRESS_TRACK_H), _BRAND)
            fill.adjustments[0] = 0.5
        _text(
            slide, g, (_PROGRESS_VALUE_X[0], y, _PROGRESS_VALUE_X[1], row_h * 0.9),
            [[(_percent(bar["value"]), 18, True, _BRAND)]], anchor="middle",
        )


def _add_side_panel(slide, g: _Geo, spec: dict, top: float, bottom: float) -> None:
    """The right-hand panel of a split chart/image slide: stacked stat cards,
    or 1-4 short takeaways with brand-red markers."""
    left, width = _SPLIT_SIDE_X
    stats = spec.get("stats") or []
    if stats:
        n = len(stats)
        h = min((bottom - top - (n - 1) * _SPLIT_STAT_GAP) / n, _SPLIT_STAT_MAX_H)
        for i, stat in enumerate(stats):
            _stat_card(
                slide, stat, int(g.x(left)), int(g.y(top + i * (h + _SPLIT_STAT_GAP))),
                int(g.x(width)), int(g.y(h)),
            )
        return
    _text(
        slide, g, (left, top, width, bottom - top),
        [[("●  ", 12, False, _BRAND), (b, _fit(b, 16, "side.bullet"), False, _INK)] for b in spec.get("bullets") or []],
        space_after=12,
    )


def _add_takeaway(slide, g: _Geo, text: str) -> None:
    """The slide's one-line key message: a pale banner with a brand-red edge."""
    from pptx.enum.shapes import MSO_SHAPE

    left, top, width, height = _TAKEAWAY_BOX
    _shape(slide, MSO_SHAPE.RECTANGLE, g, (left, top, width, height), _PALE)
    _shape(slide, MSO_SHAPE.RECTANGLE, g, (left, top, 0.006, height), _BRAND)
    _text(slide, g, (left + 0.02, top, width - 0.03, height), [[(text, _fit(text, 14, "takeaway"), True, _INK)]], anchor="middle")


_LAYOUT_RENDERERS = {
    "cards": _add_cards,
    "steps": _add_steps,
    "timeline": _add_timeline,
    "comparison": _add_comparison,
    "progress": _add_progress,
}


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


# Content-slide title in the branded header band: left-aligned above the red
# divider line (which sits at ~15% of slide height) and stopping short of the
# logo (which starts at ~86% of slide width). Fractions of slide size.
_HEADER_TITLE_BOX = (0.025, 0.02, 0.80, 0.13)  # left, top, width, height
# Content slides (every slide but the cover): title and body text sizes.
CONTENT_TITLE_PT = 20
CONTENT_BODY_PT = 16
# Where content starts below the header. The branded layouts' own title sits
# BELOW the divider (18-29%), with the body at 30%; once the title moves up
# into the header, content moves up by the same margin. The stock python-pptx
# fallback keeps its own title placement, so it keeps the old offset.
_CONTENT_TOP_BRANDED = 0.20
_CONTENT_TOP_DEFAULT = 0.30


def _place_title_in_header(slide, slide_width: int, slide_height: int) -> None:
    """Move a content slide's title placeholder into the header band, left
    aligned and vertically centered, like the template's own examples."""
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Emu, Pt

    title = slide.shapes.title
    left, top, width, height = _HEADER_TITLE_BOX
    title.left = Emu(int(slide_width * left))
    title.top = Emu(int(slide_height * top))
    title.width = Emu(int(slide_width * width))
    title.height = Emu(int(slide_height * height))
    tf = title.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    for paragraph in tf.paragraphs:
        paragraph.alignment = PP_ALIGN.LEFT


def _set_text_size(text_frame, size_pt: int, *, bold: bool | None = None) -> None:
    """Size (and optionally bold) every paragraph AND run: PowerPoint renders
    existing text from its runs, so a paragraph-level default alone is ignored."""
    from pptx.util import Pt

    for paragraph in text_frame.paragraphs:
        fonts = [paragraph.font, *(run.font for run in paragraph.runs)]
        for font in fonts:
            font.size = Pt(size_pt)
            if bold is not None:
                font.bold = bold


# The closing "Thank You" slide every deck ends on: the cover's own artwork as
# its background, a brand-red disc with a white heart, the words, and a short
# red rule — with a fade transition in, the icon zooming in, then the words
# fading up. Fixed copy and layout (fractions of slide size), like the cover.
CLOSING_TEXT = "Thank You"
CLOSING_TITLE_PT = 54
_CLOSING_ICON = (0.5, 0.24, 0.20)  # center x, top, diameter (fraction of slide HEIGHT)
_CLOSING_TITLE_BOX = (0.10, 0.50, 0.80, 0.16)  # left, top, width, height
_CLOSING_RULE = (0.5, 0.69, 0.08, 0.006)  # center x, top, width, height
_CLOSING_TEXT_COLOR = (0x22, 0x22, 0x22)


def _closing_timing_xml(icon_id: int, title_id: int, rule_id: int) -> str:
    """Slide-show animation for the closing slide: the icon zooms in as the
    slide opens (no click), then the title and rule fade in together after it.
    The standard PowerPoint entrance markup (Zoom = preset 53, Fade = 10)."""
    ids = iter(range(5, 100))

    def visible(spid: int) -> str:
        return (
            f'<p:set><p:cBhvr><p:cTn id="{next(ids)}" dur="1" fill="hold"><p:stCondLst>'
            f'<p:cond delay="0"/></p:stCondLst></p:cTn><p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl>'
            "<p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst></p:cBhvr>"
            '<p:to><p:strVal val="visible"/></p:to></p:set>'
        )

    def grow(spid: int, attr: str) -> str:
        return (
            f'<p:anim calcmode="lin" valueType="num"><p:cBhvr><p:cTn id="{next(ids)}" dur="600" fill="hold"/>'
            f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl><p:attrNameLst><p:attrName>{attr}</p:attrName>'
            '</p:attrNameLst></p:cBhvr><p:tavLst><p:tav tm="0"><p:val><p:fltVal val="0"/></p:val></p:tav>'
            f'<p:tav tm="100000"><p:val><p:strVal val="#{attr}"/></p:val></p:tav></p:tavLst></p:anim>'
        )

    def fade(spid: int) -> str:
        return (
            f'<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="{next(ids)}" dur="600"/>'
            f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cBhvr></p:animEffect>'
        )

    def effect(spid: int, node_type: str, zoom: bool) -> str:
        preset = 'presetID="53" presetClass="entr" presetSubtype="16"' if zoom else (
            'presetID="10" presetClass="entr" presetSubtype="0"'
        )
        body = visible(spid) + (grow(spid, "ppt_w") + grow(spid, "ppt_h") if zoom else "") + fade(spid)
        return (
            f'<p:par><p:cTn id="{next(ids)}" {preset} fill="hold" grpId="0" nodeType="{node_type}">'
            f'<p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>{body}</p:childTnLst></p:cTn></p:par>'
        )

    def step(delay: int, effects: str) -> str:
        return (
            f'<p:par><p:cTn id="{next(ids)}" fill="hold"><p:stCondLst><p:cond delay="{delay}"/>'
            f"</p:stCondLst><p:childTnLst>{effects}</p:childTnLst></p:cTn></p:par>"
        )

    first = step(0, effect(icon_id, "afterEffect", zoom=True))
    second = step(600, effect(title_id, "afterEffect", zoom=False) + effect(rule_id, "withEffect", zoom=False))
    return (
        '<p:timing xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:tnLst><p:par>'
        '<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
        '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
        '<p:par><p:cTn id="3" fill="hold"><p:stCondLst><p:cond delay="indefinite"/>'
        '<p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond></p:stCondLst>'
        f"<p:childTnLst>{first}{second}</p:childTnLst></p:cTn></p:par>"
        "</p:childTnLst></p:cTn>"
        '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
        '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
        "</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>"
        f'<p:bldLst><p:bldP spid="{title_id}" grpId="0"/><p:bldP spid="{rule_id}" grpId="0" animBg="1"/></p:bldLst>'
        "</p:timing>"
    )


def _add_closing_slide(prs, cover_bg) -> None:
    """Append the closing "Thank You" slide. `cover_bg` is the template cover
    slide's own `<p:bg>` element (or None on the unbranded fallback): it is
    copied with its relationship re-pointed at the SAME image part, so the
    artwork is shared, not embedded twice — and it survives even when the
    cover slide itself was dropped (a deck with no title)."""
    import copy

    from lxml import etree
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.oxml.ns import qn
    from pptx.util import Emu, Pt

    slide = prs.slides.add_slide(prs.slide_layouts[_LAYOUT_BLANK])
    width, height = prs.slide_width, prs.slide_height

    if cover_bg is not None:
        bg_elem, image_part = cover_bg
        bg = copy.deepcopy(bg_elem)
        blip = bg.find(f'{qn("p:bgPr")}/{qn("a:blipFill")}/{qn("a:blip")}')
        blip.set(qn("r:embed"), slide.part.relate_to(image_part, RT.IMAGE))
        slide._element.find(qn("p:cSld")).insert(0, bg)

    red = RGBColor(*_STAT_VALUE_COLOR)

    # Icon: a heart on a red disc, grouped so it animates as one shape.
    cx, top, diameter = _CLOSING_ICON
    d = int(height * diameter)
    left = int(width * cx) - d // 2
    icon = slide.shapes.add_group_shape()
    disc = icon.shapes.add_shape(MSO_SHAPE.OVAL, Emu(left), Emu(int(height * top)), Emu(d), Emu(d))
    heart_d = int(d * 0.5)
    heart = icon.shapes.add_shape(
        MSO_SHAPE.HEART,
        Emu(left + (d - heart_d) // 2),
        Emu(int(height * top) + int(d * 0.27)),
        Emu(heart_d),
        Emu(int(heart_d * 0.9)),
    )
    for shape, color in ((disc, red), (heart, RGBColor(0xFF, 0xFF, 0xFF))):
        shape.fill.solid()
        shape.fill.fore_color.rgb = color
        shape.line.fill.background()
        shape.shadow.inherit = False

    bl, bt, bw, bh = _CLOSING_TITLE_BOX
    box = slide.shapes.add_textbox(Emu(int(width * bl)), Emu(int(height * bt)), Emu(int(width * bw)), Emu(int(height * bh)))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = CLOSING_TEXT
    run.font.size = Pt(CLOSING_TITLE_PT)
    run.font.bold = True
    run.font.name = DECK_FONT
    run.font.color.rgb = RGBColor(*_CLOSING_TEXT_COLOR)

    rx, rt, rw, rh = _CLOSING_RULE
    rule = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(int(width * (rx - rw / 2))),
        Emu(int(height * rt)),
        Emu(int(width * rw)),
        Emu(max(int(height * rh), 1)),
    )
    rule.fill.solid()
    rule.fill.fore_color.rgb = red
    rule.line.fill.background()
    rule.shadow.inherit = False

    # <p:sld> children are ordered: cSld, clrMapOvr, transition, timing.
    sld = slide._element
    sld.append(etree.fromstring(
        '<p:transition xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" spd="slow">'
        "<p:fade/></p:transition>"
    ))
    sld.append(etree.fromstring(_closing_timing_xml(icon.shape_id, box.shape_id, rule.shape_id)))


def _cover_background(cover_slide):
    """The cover slide's `<p:bg>` and the image part it shows, or None."""
    from pptx.oxml.ns import qn

    if cover_slide is None:
        return None
    bg = cover_slide._element.find(qn("p:cSld")).find(qn("p:bg"))
    blip = bg.find(f'{qn("p:bgPr")}/{qn("a:blipFill")}/{qn("a:blip")}') if bg is not None else None
    if blip is None:
        return None
    return bg, cover_slide.part.rels[blip.get(qn("r:embed"))].target_part



def _build_pptx_bytes(
    title: str, subtitle: str, slides: list[dict], image_paths: dict[str, str]
) -> bytes:
    """Render the deck with python-pptx. Sync — run in a thread."""
    from pptx import Presentation
    from pptx.util import Emu

    cover_slide = None
    branded = _TEMPLATE_PATH.exists()
    if branded:
        prs = Presentation(str(_TEMPLATE_PATH))
        cover_rid, cover_slide = _find_cover_slide(prs)
        # Taken before the cover may be dropped: the closing slide reuses its art.
        cover_bg = _cover_background(cover_slide)
        # No deck title -> no title slide at all, matching the pre-template
        # contract; drop the cover example along with the other two.
        _remove_other_slides(prs, keep_rid=cover_rid if title else None)
        if not title:
            cover_slide = None
    else:
        prs = Presentation()
        cover_bg = None

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
        takeaway = spec.get("takeaway")
        visual = image if image is not None else chart
        # Bullets beside a chart/image are drawn in the side panel's own text
        # box, so only a plain bullet slide uses the layout's body placeholder.
        layout = _LAYOUT_TITLE_AND_CONTENT if bullets and visual is None else _LAYOUT_TITLE_ONLY
        slide = prs.slides.add_slide(prs.slide_layouts[layout])
        slide.shapes.title.text = str(spec.get("title") or "")
        _set_text_size(slide.shapes.title.text_frame, CONTENT_TITLE_PT, bold=True)

        # Zone offsets are fractions of slide height, all relative to `top`:
        # on the branded template the title sits in the header band above
        # the divider line, so content starts just below it. A template swap
        # would need _HEADER_TITLE_BOX and these recalibrated together.
        if branded:
            _place_title_in_header(slide, prs.slide_width, prs.slide_height)
            top = _CONTENT_TOP_BRANDED
        else:
            top = _CONTENT_TOP_DEFAULT
        shift = top - _CONTENT_TOP_DEFAULT  # 0 on the fallback, negative when branded
        g = _Geo(prs.slide_width, prs.slide_height)
        bottom = _ZONE_BOTTOM_WITH_TAKEAWAY if takeaway else _ZONE_BOTTOM
        if takeaway:
            _add_takeaway(slide, g, takeaway)

        # Whole-slide infographic layouts (validated as the only content
        # besides the title/takeaway).
        layout_key = next((k for k in _LAYOUT_KEYS if spec.get(k) is not None), None)
        if layout_key is not None:
            _LAYOUT_RENDERERS[layout_key](slide, g, spec[layout_key], top, bottom)
            continue

        # image/chart: full width on their own, or the left of a split slide
        # whose right-hand panel holds stats or short takeaways. Never with a
        # table (enforced in _validate).
        if visual is not None:
            split = bool(bullets or stats)
            height = (bottom - top) if split else min(0.58, bottom - top)
            left, width = (g.x(_SPLIT_VISUAL_X[0]), g.x(_SPLIT_VISUAL_X[1])) if split else (None, None)
            if image is not None:
                _add_image(
                    slide, image, image_paths, int(g.y(top)), int(g.y(height)), prs.slide_width,
                    left_emu=left, width_emu=width,
                )
            else:
                _add_chart(
                    slide, chart, int(g.y(top)), int(g.y(height)), prs.slide_width,
                    left_emu=left, width_emu=width,
                )
            if split:
                _add_side_panel(slide, g, spec, top, bottom)
            continue

        if stats:
            _add_stats(
                slide, stats, int(prs.slide_height * top), int(prs.slide_height * 0.22), prs.slide_width
            )

        if bullets:
            _add_bullets(slide, bullets)
            if stats or table is not None or shift or takeaway:
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
                    body_top, body_h = 0.55 + shift, 0.33
                elif table is not None:
                    body_top, body_h = top, 0.22
                else:
                    # Bullets alone: the layout's body, lifted to just under
                    # the header, stopping above the bottom-corner badge.
                    body_top, body_h = top, 0.66
                if takeaway:
                    # ...or above the takeaway banner (never with a table).
                    body_h = min(body_h, _ZONE_BOTTOM_WITH_TAKEAWAY - body_top)
                body.top = Emu(int(prs.slide_height * body_top))
                body.height = Emu(int(prs.slide_height * body_h))
                body.left = left
                body.width = width
        if table is not None:
            # All three (stats+bullets+table) on one slide is an unsupported
            # edge case visually -- it renders without error but cramped, and
            # is not a combination the tool's own description encourages.
            if stats:
                table_top = int(prs.slide_height * ((0.80 if bullets else 0.55) + shift))
            else:
                table_top = int(prs.slide_height * ((0.55 if bullets else 0.32) + shift))
            _add_table(slide, table, table_top, prs.slide_width)

    _add_closing_slide(prs, cover_bg)
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
    # `closing`: the deck ends on the fixed "Thank You" slide, which the
    # preview draws itself (decks saved before it existed have no such key).
    preview = {"title": title, "subtitle": subtitle, "slides": slides, "closing": True}
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
        "when the user asks for a presentation, slides, or a deck. DESIGN RULE: build "
        "an infographic deck, not a text deck — give EVERY content slide a short "
        "'title' (its headline) and a visual layout, and keep all copy to a few words. Pick per slide: 'cards' (2-6 "
        "{icon, title, text?} — key points/features/pillars), 'steps' (2-6 {title, "
        "text?, icon?} — a process or workflow), 'timeline' (2-6 {date, title, text?} "
        "— milestones/roadmap), 'comparison' (2-3 {heading, icon?, points[]} — "
        "options, before/after, pros/cons), 'progress' (1-6 {label, value 0-100, "
        "note?} — targets, completion, shares), 'stats' (up to "
        f"{MAX_STATS_PER_SLIDE} {{value, label, note?}} — headline numbers), 'chart' "
        "({chart_type, labels, series} — exactly create_chart's shape, rendered as a "
        "native editable chart; use it for any series of numbers), or 'image' "
        "({file_id, caption?} — a raster image already uploaded or generated this "
        "conversation). A chart/image may share its slide with a side panel of up to "
        f"{MAX_SIDE_BULLETS} short 'bullets' OR {MAX_SIDE_STATS} 'stats'. Any slide "
        "except a table or plain-bullet slide may add a one-line 'takeaway' (its key "
        "message). Icons are named from: " + ", ".join(pptx_icons.ICON_NAMES) + ". "
        "Use plain 'bullets' (at most 4 short lines) or a 'table' ({headers?, "
        f"rows[][]}}, up to {MAX_TABLE_ROWS_PER_SLIDE} rows incl. header) only when "
        "no layout fits. One layout per slide. Keep copy short — longer text is set "
        "smaller and paragraph-length text is refused. Optionally give a deck 'title'/'subtitle' (a cover slide) "
        f"and a 'filename'. Up to {MAX_SLIDES} slides; full Unicode is supported. "
        "For a document rather than slides use create_docx or create_pdf."
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
                        "title": {"type": "string", "description": "The slide's headline — always give one."},
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
                        "takeaway": {
                            "type": "string",
                            "description": "Optional one-line key message, shown as a banner at the bottom.",
                        },
                        "cards": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "icon": {"type": "string", "description": "An icon name from the list above."},
                                    "title": {"type": "string", "description": "2-5 words."},
                                    "text": {"type": "string", "description": "Optional, one short line."},
                                },
                                "required": ["title"],
                            },
                            "description": "2-6 icon cards — key points, features or pillars.",
                        },
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string", "description": "2-4 words."},
                                    "text": {"type": "string", "description": "Optional, one short line."},
                                    "icon": {"type": "string", "description": "Optional icon name."},
                                },
                                "required": ["title"],
                            },
                            "description": "2-6 process steps, left to right.",
                        },
                        "timeline": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string", "description": "e.g. 'Q1 2026' or 'Shrawan 2082'."},
                                    "title": {"type": "string"},
                                    "text": {"type": "string", "description": "Optional, one short line."},
                                },
                                "required": ["date", "title"],
                            },
                            "description": "2-6 dated milestones, in order.",
                        },
                        "comparison": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {"type": "string"},
                                    "icon": {"type": "string", "description": "Optional icon name."},
                                    "points": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": f"1-{MAX_POINTS_PER_COLUMN} short points.",
                                    },
                                },
                                "required": ["heading", "points"],
                            },
                            "description": "2-3 side-by-side columns (options, before/after, pros/cons).",
                        },
                        "progress": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "value": {"type": "number", "description": "A percentage, 0-100."},
                                    "note": {"type": "string", "description": "Optional short detail."},
                                },
                                "required": ["label", "value"],
                            },
                            "description": "1-6 labelled percentage bars.",
                        },
                    },
                    "required": ["title"],
                },
                "description": (
                    "Slides in order; each needs a title plus one layout (cards, steps, timeline, "
                    "comparison, progress, stats, chart, image), or bullets/table."
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

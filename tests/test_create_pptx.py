"""Offline tests for the create_pptx local tool (python-pptx -> .pptx).

No network: calls the tool fn directly against a temp-configured fallback file
store and asserts (a) a valid .pptx (zip w/ ppt/presentation.xml) is produced
and stored, (b) deck title / slide titles / bullets / table cells land in the
deck, (c) full Unicode survives, (d) validation returns friendly ERROR strings,
and (e) the slide/bullet caps refuse rather than build.
"""

import asyncio
import zipfile

import pytest

from app.files.store import PPTX_MEDIA_TYPE, file_store
from app.tools.local import pptx as pptx_tool


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _run(args):
    return asyncio.run(pptx_tool.SPEC.func(args))


def _save_image(*, width=800, height=400, media_type="image/png", filename="photo.png", content=None):
    """Save a real (or deliberately non-image) file via the fallback store and
    return its id, for use as an 'image' field's file_id."""
    async def _save():
        if content is not None:
            data = content
        else:
            from io import BytesIO

            from PIL import Image

            img = Image.new("RGB", (width, height), color=(230, 0, 18))
            buf = BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        record = await file_store.save(data, filename=filename, media_type=media_type)
        return record.id

    return asyncio.run(_save())


def _link_id(result: str) -> str:
    assert "Download it at: GET /v1/files/" in result, result
    return result.split("/v1/files/")[1].strip().split()[0]


def _slide_texts(result: str) -> list[str]:
    """Reopen the stored file with python-pptx and return every text run, in order."""
    from pptx import Presentation

    record = file_store.get(_link_id(result))
    assert record is not None
    assert record.media_type == PPTX_MEDIA_TYPE
    with zipfile.ZipFile(record.path) as zf:
        assert "ppt/presentation.xml" in zf.namelist()  # it's a real .pptx
    prs = Presentation(record.path)
    texts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.extend(p.text for p in shape.text_frame.paragraphs)
            if shape.has_table:
                for row in shape.table.rows:
                    texts.extend(cell.text for cell in row.cells)
    return texts


# ---- validation -------------------------------------------------------------


def test_missing_slides_is_error():
    assert _run({}).startswith("ERROR: 'slides' is required")
    assert _run({"slides": []}).startswith("ERROR: 'slides' is required")


def test_slide_not_object_is_error():
    assert _run({"slides": ["just text"]}).startswith("ERROR: slides[0] must be an object")


def test_empty_slide_is_error():
    assert _run({"slides": [{}]}).startswith("ERROR: slides[0] needs at least one of")


def test_bullets_must_be_array_of_strings():
    assert _run({"slides": [{"bullets": "one"}]}).startswith("ERROR: slides[0].bullets must be an array")
    assert _run({"slides": [{"bullets": [1, 2]}]}).startswith("ERROR: slides[0].bullets must be an array")


def test_bad_table_is_error():
    assert _run({"slides": [{"table": "x"}]}).startswith("ERROR: slides[0].table must be an object")
    assert _run({"slides": [{"table": {"rows": []}}]}).startswith("ERROR: slides[0].table.rows must be")
    assert _run({"slides": [{"table": {"rows": ["a"]}}]}).startswith("ERROR: slides[0].table.rows[0] must be")
    assert _run({"slides": [{"table": {"rows": [["a"]], "headers": "h"}}]}).startswith(
        "ERROR: slides[0].table.headers must be"
    )


def test_too_many_slides_is_refused():
    slides = [{"title": f"s{i}"} for i in range(pptx_tool.MAX_SLIDES + 1)]
    result = _run({"slides": slides})
    assert result.startswith("ERROR:") and str(pptx_tool.MAX_SLIDES) in result


def test_too_many_bullets_is_refused():
    bullets = [f"b{i}" for i in range(pptx_tool.MAX_BULLETS_PER_SLIDE + 1)]
    result = _run({"slides": [{"title": "x", "bullets": bullets}]})
    assert result.startswith("ERROR: slides[0].bullets") and str(pptx_tool.MAX_BULLETS_PER_SLIDE) in result


def test_table_over_cap_without_bullets_is_refused():
    rows = [[str(i)] for i in range(pptx_tool.MAX_TABLE_ROWS_PER_SLIDE + 1)]  # no header, so 13 rows total
    result = _run({"slides": [{"title": "x", "table": {"rows": rows}}]})
    assert result.startswith("ERROR: slides[0].table has")
    assert str(pptx_tool.MAX_TABLE_ROWS_PER_SLIDE) in result


def test_table_at_cap_without_bullets_is_created():
    rows = [[str(i)] for i in range(pptx_tool.MAX_TABLE_ROWS_PER_SLIDE)]  # no header, exactly at the cap
    result = _run({"slides": [{"title": "x", "table": {"rows": rows}}]})
    assert result.startswith("Created"), result


def test_table_over_cap_with_bullets_is_refused():
    rows = [[str(i)] for i in range(pptx_tool.MAX_TABLE_ROWS_WITH_BULLETS + 1)]  # over the bullets cap
    result = _run({"slides": [{"title": "x", "bullets": ["b"], "table": {"rows": rows}}]})
    assert result.startswith("ERROR: slides[0].table has")
    assert str(pptx_tool.MAX_TABLE_ROWS_WITH_BULLETS) in result
    assert "with bullets" in result


def test_table_at_cap_with_bullets_is_created():
    rows = [[str(i)] for i in range(pptx_tool.MAX_TABLE_ROWS_WITH_BULLETS)]  # at the bullets cap
    result = _run({"slides": [{"title": "x", "bullets": ["b"], "table": {"rows": rows}}]})
    assert result.startswith("Created"), result


def test_bad_stats_is_error():
    # An empty list (or dict) is falsy, so it hits the generic "needs at least
    # one of" check first — same as bullets: [] already does; a non-empty
    # value of the wrong type reaches the stats-specific validation.
    assert _run({"slides": [{"stats": "x"}]}).startswith("ERROR: slides[0].stats must be")
    assert _run({"slides": [{"stats": {"not": "a list"}}]}).startswith("ERROR: slides[0].stats must be")
    assert _run({"slides": [{"stats": [{"label": "no value"}]}]}).startswith(
        "ERROR: slides[0].stats[0] must be an object with 'value' and 'label'"
    )
    assert _run({"slides": [{"stats": [{"value": "1"}]}]}).startswith(
        "ERROR: slides[0].stats[0] must be an object with 'value' and 'label'"
    )


def test_too_many_stats_is_refused():
    stats = [{"value": str(i), "label": f"l{i}"} for i in range(pptx_tool.MAX_STATS_PER_SLIDE + 1)]
    result = _run({"slides": [{"title": "x", "stats": stats}]})
    assert result.startswith("ERROR: slides[0].stats has")
    assert str(pptx_tool.MAX_STATS_PER_SLIDE) in result


def test_stats_at_cap_is_created():
    stats = [{"value": str(i), "label": f"l{i}"} for i in range(pptx_tool.MAX_STATS_PER_SLIDE)]
    result = _run({"slides": [{"title": "x", "stats": stats}]})
    assert result.startswith("Created"), result


def test_slide_with_only_stats_is_valid():
    # 'stats' alone satisfies the "needs at least one of" requirement, same as
    # bullets/table alone.
    result = _run({"slides": [{"stats": [{"value": "1", "label": "l"}]}]})
    assert result.startswith("Created"), result


def test_slide_needing_content_error_mentions_stats():
    assert "stats" in _run({"slides": [{}]})


# ---- rendering --------------------------------------------------------------


def test_title_slide_and_bullets_present():
    result = _run(
        {
            "title": "Quarterly Review",
            "subtitle": "Finance",
            "slides": [
                {"title": "Highlights", "bullets": ["Revenue up", "Costs flat"]},
                {"bullets": ["A slide with no title"]},
            ],
        }
    )
    assert "3 slide(s)" not in result  # the count reports CONTENT slides only
    assert "2 slide(s)" in result
    texts = _slide_texts(result)
    assert "Quarterly Review" in texts and "Finance" in texts
    assert "Highlights" in texts
    assert "Revenue up" in texts and "Costs flat" in texts
    assert "A slide with no title" in texts


def test_no_deck_title_means_no_title_slide():
    from pptx import Presentation

    result = _run({"slides": [{"title": "Only"}]})
    record = file_store.get(_link_id(result))
    assert len(Presentation(record.path).slides) == 1


def test_table_slide_present():
    result = _run(
        {
            "slides": [
                {
                    "title": "Numbers",
                    "table": {"headers": ["Month", "Sales"], "rows": [["Jan", 10], ["Feb", 20]]},
                }
            ]
        }
    )
    texts = _slide_texts(result)
    assert "Numbers" in texts
    assert "Month" in texts and "Sales" in texts and "Jan" in texts and "20" in texts


def test_bullets_and_table_on_one_slide():
    result = _run(
        {
            "slides": [
                {
                    "title": "Both",
                    "bullets": ["point one"],
                    "table": {"rows": [["a", "b"]]},
                }
            ]
        }
    )
    texts = _slide_texts(result)
    assert "point one" in texts and "a" in texts and "b" in texts


def test_content_slide_title_sits_in_the_header_band_above_the_divider():
    """On the branded template the title goes in the header band, left
    aligned, above the red divider (~15% down) and clear of the logo (~86%
    across) — and the bullets start below the divider, not under the old
    title position."""
    from pptx import Presentation
    from pptx.enum.text import PP_ALIGN

    result = _run({"slides": [{"title": "Hello", "bullets": ["point one"]}]})
    prs = Presentation(file_store.get(_link_id(result)).path)
    W, H = prs.slide_width, prs.slide_height
    slide = prs.slides[0]
    title = slide.shapes.title
    assert title.top + title.height <= H * 0.155
    assert title.left < W * 0.05
    assert title.left + title.width <= W * 0.86
    assert title.text_frame.paragraphs[0].alignment == PP_ALIGN.LEFT
    body = slide.placeholders[1]
    assert H * 0.155 < body.top <= H * 0.21


def test_bullets_and_table_shrink_keeps_body_left_and_width():
    """Regression: shrinking the bullet box for a table must not zero its
    left/width. python-pptx placeholder position setters write a bare xfrm,
    so writing only top/height (without re-asserting left/width) defaults the
    other two to 0 — invisible bullets, even though the text is still in the
    XML. Compare against the layout's own placeholder, the value the shrink
    must preserve."""
    from pptx import Presentation

    result = _run({"slides": [{"title": "Both", "bullets": ["point one"], "table": {"rows": [["a", "b"]]}}]})
    record = file_store.get(_link_id(result))
    prs = Presentation(record.path)
    slide = prs.slides[0]
    body = slide.placeholders[1]
    layout_body = slide.slide_layout.placeholders[1]
    assert body.left == layout_body.left
    assert body.width == layout_body.width


def test_stats_slide_present():
    result = _run(
        {
            "slides": [
                {
                    "title": "Institution at a Glance",
                    "stats": [
                        {"value": "1956", "label": "Year established"},
                        {"value": "18.4B", "label": "USD in reserves", "note": "As of Mar 2024"},
                    ],
                }
            ]
        }
    )
    texts = _slide_texts(result)
    assert "Institution at a Glance" in texts
    assert "1956" in texts and "Year established" in texts
    assert "18.4B" in texts and "USD in reserves" in texts and "As of Mar 2024" in texts


def test_stats_and_bullets_on_one_slide_do_not_overlap():
    """Regression: the stats row and the (repositioned) bullet box below it
    must not collide — both must render with distinct, non-overlapping
    vertical spans."""
    result = _run(
        {
            "slides": [
                {
                    "title": "Both",
                    "stats": [{"value": "1", "label": "one"}, {"value": "2", "label": "two"}],
                    "bullets": ["a summary line"],
                }
            ]
        }
    )
    texts = _slide_texts(result)
    assert "1" in texts and "one" in texts and "a summary line" in texts

    from pptx import Presentation

    record = file_store.get(_link_id(result))
    prs = Presentation(record.path)
    slide = prs.slides[0]
    body = slide.placeholders[1]
    stat_shapes = [s for s in slide.shapes if s.shape_type == 1]  # MSO_SHAPE_TYPE.AUTO_SHAPE
    assert len(stat_shapes) == 2
    stats_bottom = max(s.top + s.height for s in stat_shapes)
    assert body.top >= stats_bottom  # bullets start at/after the stats row ends


def test_ragged_table_rows_are_padded():
    result = _run({"slides": [{"table": {"headers": ["x", "y", "z"], "rows": [["1"], ["1", "2", "3", "4"]]}}]})
    assert result.startswith("Created"), result
    texts = _slide_texts(result)
    assert "4" in texts  # the widest row sets the column count


def test_full_unicode_preserved():
    result = _run({"title": "Café 🚀 “quotes”", "slides": [{"bullets": ["नेपाल राष्ट्र बैंक", "smile 😀"]}]})
    texts = _slide_texts(result)
    assert "Café 🚀 “quotes”" in texts
    assert "नेपाल राष्ट्र बैंक" in texts and "smile 😀" in texts


def test_every_typeface_in_the_deck_is_arial():
    """Theme fonts, the template master/layout styles, explicit runs on the
    cover, bullets, tables and stat cards, and chart text — nothing left in
    Calibri. `+mj-lt`/`+mn-lt` are theme references and resolve to Arial."""
    import re

    result = _run(
        {
            "title": "Quarterly Review",
            "subtitle": "Q3 2026",
            "slides": [
                {"title": "Highlights", "bullets": ["a", "b"]},
                {"title": "Table", "table": {"headers": ["x", "y"], "rows": [["1", "2"]]}},
                {"title": "Stats", "stats": [{"value": "12%", "label": "Growth", "note": "YoY"}]},
                {"title": "Chart", "chart": {"chart_type": "bar", "labels": ["a"], "series": [{"data": [1]}]}},
            ],
        }
    )
    assert result.startswith("Created"), result
    with zipfile.ZipFile(file_store.get(_link_id(result)).path) as z:
        faces = {
            face
            for name in z.namelist()
            if name.endswith(".xml")
            for face in re.findall(rb'<a:latin typeface="([^"]*)"', z.read(name))
        }
    assert faces - {b"+mj-lt", b"+mn-lt"} == {pptx_tool.DECK_FONT.encode()}


def test_saved_record_carries_the_structured_preview():
    """The frontend renders a per-slide preview from the exact validated args,
    not a re-extraction of the saved bytes — so the saved FileRecord's
    `.preview` must be that same {title, subtitle, slides}, verbatim."""
    result = _run(
        {
            "title": "Quarterly Review",
            "subtitle": "Q3 2026",
            "slides": [{"title": "Highlights", "bullets": ["a", "b"]}],
        }
    )
    record = file_store.get(_link_id(result))
    assert record is not None
    assert record.preview == {
        "title": "Quarterly Review",
        "subtitle": "Q3 2026",
        "slides": [{"title": "Highlights", "bullets": ["a", "b"]}],
    }


def test_filename_suffix_is_forced():
    result = _run({"slides": [{"title": "x"}], "filename": "deck"})
    assert "'deck.pptx'" in result
    result = _run({"slides": [{"title": "x"}]})
    assert "'presentation.pptx'" in result


def test_image_and_chart_cannot_combine_with_bullets_table_or_stats():
    for field, value in [
        ("bullets", ["a"]),
        ("table", {"rows": [["a"]]}),
        ("stats", [{"value": "1", "label": "l"}]),
    ]:
        for media_field, media_value in [
            ("image", {"file_id": "whatever"}),
            ("chart", {"chart_type": "bar", "labels": ["a"], "series": [{"data": [1]}]}),
        ]:
            result = _run({"slides": [{"title": "x", field: value, media_field: media_value}]})
            assert result.startswith("ERROR:") and "combines" in result, result


def test_image_and_chart_together_is_refused():
    result = _run(
        {
            "slides": [
                {
                    "title": "x",
                    "image": {"file_id": "whatever"},
                    "chart": {"chart_type": "bar", "labels": ["a"], "series": [{"data": [1]}]},
                }
            ]
        }
    )
    assert result.startswith("ERROR:") and "both 'image' and 'chart'" in result


def test_bad_image_is_error():
    # An empty dict is falsy, so it hits the generic "needs at least one of"
    # check first (same as stats: {} / stats: [] already do); a non-empty
    # value of the wrong shape reaches the image-specific validation.
    assert _run({"slides": [{"image": "x"}]}).startswith("ERROR: slides[0].image must be")
    assert _run({"slides": [{"image": {"caption": "no file_id"}}]}).startswith(
        "ERROR: slides[0].image must be"
    )
    assert _run({"slides": [{"image": {"file_id": "abc", "caption": 5}}]}).startswith(
        "ERROR: slides[0].image.caption must be a string"
    )


def test_bad_chart_reuses_create_chart_validation():
    """create_pptx's chart field is validated by create_chart's own _validate —
    same error, just re-scoped to the slide/field path."""
    result = _run({"slides": [{"chart": {"chart_type": "nope", "labels": ["a"], "series": [{"data": [1]}]}}]})
    assert result == "ERROR: slides[0].chart: 'chart_type' is required and must be one of: bar, line, pie."


def test_unknown_image_file_id_is_error():
    result = _run({"slides": [{"title": "x", "image": {"file_id": "does-not-exist"}}]})
    assert result == "ERROR: slides[0].image.file_id: no such file (unknown id, or you don't own it)."


def test_non_image_file_id_is_error():
    file_id = _save_image(content=b"not an image", media_type="text/plain", filename="notes.txt")
    result = _run({"slides": [{"title": "x", "image": {"file_id": file_id}}]})
    assert "'.txt' is not an image" in result


def test_svg_file_id_names_the_chart_field_as_the_alternative():
    file_id = _save_image(content=b"<svg></svg>", media_type="image/svg+xml", filename="chart.svg")
    result = _run({"slides": [{"title": "x", "image": {"file_id": file_id}}]})
    assert "SVG" in result and "'chart' field" in result


def test_image_slide_renders_at_native_aspect_ratio_and_is_centered():
    from pptx import Presentation

    file_id = _save_image(width=800, height=400)
    result = _run({"slides": [{"title": "Office", "image": {"file_id": file_id, "caption": "HQ"}}]})
    assert result.startswith("Created"), result

    record = file_store.get(_link_id(result))
    prs = Presentation(record.path)
    slide = prs.slides[0]
    pictures = [s for s in slide.shapes if s.shape_type == 13]  # MSO_SHAPE_TYPE.PICTURE
    assert len(pictures) == 1
    pic = pictures[0]
    # 800x400 is 2:1 -- the embedded picture must keep that ratio.
    assert abs(pic.width / pic.height - 2.0) < 0.01
    # Centered horizontally.
    assert abs((pic.left + pic.width / 2) - prs.slide_width / 2) < 1000
    assert "HQ" in _slide_texts(result)


def test_image_and_chart_slides_do_not_get_the_bullets_layout():
    """Regression: an earlier version could pick the Title-and-Content layout
    for an image/chart slide (since that branch only checked `bullets`),
    leaving an unused, empty content placeholder behind the picture/chart."""
    file_id = _save_image()
    result = _run({"slides": [{"title": "x", "image": {"file_id": file_id}}]})
    from pptx import Presentation

    record = file_store.get(_link_id(result))
    slide = Presentation(record.path).slides[0]
    assert slide.slide_layout.name == "Title Only"


def test_chart_slide_renders_native_editable_chart_with_correct_data():
    result = _run(
        {
            "slides": [
                {
                    "title": "Revenue",
                    "chart": {
                        "chart_type": "bar",
                        "labels": ["Q1", "Q2", "Q3"],
                        "series": [{"name": "Revenue", "data": [100, 120, 134]}],
                    },
                }
            ]
        }
    )
    assert result.startswith("Created"), result

    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE

    record = file_store.get(_link_id(result))
    slide = Presentation(record.path).slides[0]
    charts = [s for s in slide.shapes if s.has_chart]
    assert len(charts) == 1
    chart = charts[0].chart
    assert chart.chart_type == XL_CHART_TYPE.COLUMN_CLUSTERED
    assert list(chart.plots[0].categories) == ["Q1", "Q2", "Q3"]
    series = list(chart.series)
    assert len(series) == 1
    assert series[0].name == "Revenue"
    assert list(series[0].values) == [100.0, 120.0, 134.0]


def test_pie_chart_uses_only_the_first_series():
    result = _run(
        {
            "slides": [
                {
                    "title": "Split",
                    "chart": {
                        "chart_type": "pie",
                        "labels": ["A", "B"],
                        "series": [{"data": [1, 2]}, {"data": [3, 4]}],
                    },
                }
            ]
        }
    )
    assert result.startswith("Created"), result

    from pptx import Presentation

    record = file_store.get(_link_id(result))
    slide = Presentation(record.path).slides[0]
    chart = [s for s in slide.shapes if s.has_chart][0].chart
    assert len(list(chart.series)) == 1


def test_hbar_and_donut_chart_types_map_correctly():
    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE

    for chart_type, expected in [("hbar", XL_CHART_TYPE.BAR_CLUSTERED), ("donut", XL_CHART_TYPE.DOUGHNUT)]:
        result = _run(
            {"slides": [{"title": "x", "chart": {"chart_type": chart_type, "labels": ["a"], "series": [{"data": [1]}]}}]}
        )
        assert result.startswith("Created"), result
        record = file_store.get(_link_id(result))
        slide = Presentation(record.path).slides[0]
        chart = [s for s in slide.shapes if s.has_chart][0].chart
        assert chart.chart_type == expected


# ---- registration -----------------------------------------------------------


def test_tool_is_registered_once():
    from app.tools.local import LOCAL_TOOLS

    names = [t.name for t in LOCAL_TOOLS]
    assert names.count("create_pptx") == 1
    assert names.index("create_pptx") == names.index("create_docx") + 1


def test_description_routes_documents_to_create_docx():
    assert "create_docx" in pptx_tool.SPEC.description

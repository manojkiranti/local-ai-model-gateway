"""Offline tests for the create_chart local tool (server-rendered SVG).

No network: calls the tool fn directly against a temp-configured file store, and
asserts (a) each chart type renders a script-free SVG, (b) validation returns
friendly ERROR strings (never raises), and (c) the result links to a retrievable
SVG file in the store.
"""

import asyncio

import pytest

from app.files.store import SVG_MEDIA_TYPE, file_store
from app.tools.local import chart


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _run(args):
    # asyncio.run gives each call its own loop — robust when other tests in the
    # suite have already created/closed the default loop.
    return asyncio.run(chart.SPEC.func(args))


def _link_id(result: str) -> str:
    assert "Download it at: GET /v1/files/" in result, result
    return result.split("/v1/files/")[1].strip().split()[0]


# ---- happy paths: each type renders a script-free SVG in the store ----

@pytest.mark.parametrize("chart_type", ["bar", "line", "pie", "hbar", "area", "donut"])
def test_each_type_renders_svg(chart_type):
    result = _run(
        {
            "chart_type": chart_type,
            "title": f"My {chart_type}",
            "labels": ["A", "B", "C"],
            "series": [{"name": "s1", "data": [3, 1, 2]}],
        }
    )
    record = file_store.get(_link_id(result))
    assert record is not None
    assert record.media_type == SVG_MEDIA_TYPE
    svg = open(record.path, encoding="utf-8").read()
    assert svg.lstrip().startswith("<svg") and "</svg>" in svg
    # Safety: server-rendered SVG must never contain executable script.
    assert "<script" not in svg.lower()
    assert "My " in svg  # title rendered


def test_multi_series_bar_has_legend():
    result = _run(
        {
            "chart_type": "bar",
            "labels": ["Q1", "Q2"],
            "series": [
                {"name": "2025", "data": [10, 20]},
                {"name": "2026", "data": [12, 18]},
            ],
        }
    )
    svg = open(file_store.get(_link_id(result)).path, encoding="utf-8").read()
    # Legend labels present so identity isn't color-alone (dataviz relief rule).
    assert "2025" in svg and "2026" in svg


# ---- validation: friendly ERROR strings, never exceptions ----

def test_bad_chart_type():
    assert _run({"chart_type": "radar", "labels": ["A"], "series": [{"data": [1]}]}).startswith("ERROR")


def test_missing_chart_type():
    assert _run({"labels": ["A"], "series": [{"data": [1]}]}).startswith("ERROR")


def test_empty_labels():
    assert _run({"chart_type": "bar", "labels": [], "series": [{"data": []}]}).startswith("ERROR")


def test_empty_series():
    assert _run({"chart_type": "bar", "labels": ["A"], "series": []}).startswith("ERROR")


def test_length_mismatch():
    r = _run({"chart_type": "bar", "labels": ["A", "B"], "series": [{"data": [1]}]})
    assert r.startswith("ERROR")


def test_non_numeric_data():
    r = _run({"chart_type": "line", "labels": ["A", "B"], "series": [{"data": [1, "x"]}]})
    assert r.startswith("ERROR")


def test_pie_negative_rejected():
    r = _run({"chart_type": "pie", "labels": ["A", "B"], "series": [{"data": [1, -2]}]})
    assert r.startswith("ERROR")


def test_donut_negative_rejected():
    r = _run({"chart_type": "donut", "labels": ["A", "B"], "series": [{"data": [1, -2]}]})
    assert r.startswith("ERROR")


def test_multi_series_area_has_legend():
    result = _run(
        {
            "chart_type": "area",
            "labels": ["Jan", "Feb", "Mar"],
            "series": [
                {"name": "Web", "data": [3, 5, 4]},
                {"name": "Mobile", "data": [2, 4, 6]},
            ],
        }
    )
    svg = open(file_store.get(_link_id(result)).path, encoding="utf-8").read()
    assert "Web" in svg and "Mobile" in svg


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "create_chart" for spec in LOCAL_TOOLS)

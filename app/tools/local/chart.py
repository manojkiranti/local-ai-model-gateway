"""Local tool: create_chart (structured data -> static SVG chart download link).

The model supplies data only (chart_type + labels + series); the gateway renders
a deterministic, script-free SVG (see `_svg.py`) and stores it, mirroring how
create_excel takes rows and returns a /v1/files/{id} link. Bar, line, pie.
"""

from __future__ import annotations

from numbers import Real
from typing import Any

from ...files.store import SVG_MEDIA_TYPE, file_store
from ._svg import render_chart
from .base import LocalToolSpec

CHART_TYPES = ("bar", "hbar", "line", "area", "pie", "donut")
# Types that use a single series as parts-of-a-whole (non-negative, positive sum).
_WHOLE_TYPES = ("pie", "donut")


def _validate(args: dict[str, Any]) -> tuple[str, list[str], list[dict]] | str:
    """Return (chart_type, labels, series) on success, or an ERROR: string."""
    chart_type = args.get("chart_type")
    if chart_type not in CHART_TYPES:
        return "ERROR: 'chart_type' is required and must be one of: bar, line, pie."

    labels = args.get("labels")
    if not isinstance(labels, list) or not labels:
        return "ERROR: 'labels' is required and must be a non-empty array of strings."
    labels = [str(lab) for lab in labels]

    raw_series = args.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        return "ERROR: 'series' is required and must be a non-empty array of {name?, data[]}."

    series: list[dict] = []
    for idx, s in enumerate(raw_series):
        if not isinstance(s, dict):
            return f"ERROR: series[{idx}] must be an object with a numeric 'data' array."
        data = s.get("data")
        if not isinstance(data, list) or len(data) != len(labels):
            return (
                f"ERROR: series[{idx}].data must be an array the same length as "
                f"labels ({len(labels)})."
            )
        nums: list[float] = []
        for v in data:
            # bool is a subclass of int — reject it so True/False can't be a value.
            if isinstance(v, bool) or not isinstance(v, Real):
                return f"ERROR: series[{idx}].data must contain only numbers."
            nums.append(float(v))
        name = str(s.get("name") or f"Series {idx + 1}")
        series.append({"name": name, "data": nums})

    if chart_type in _WHOLE_TYPES:
        data0 = series[0]["data"]
        if any(v < 0 for v in data0):
            return f"ERROR: {chart_type} chart values must be non-negative."
        if sum(data0) <= 0:
            return f"ERROR: {chart_type} chart needs a positive total."

    return chart_type, labels, series


async def _create_chart(args: dict[str, Any]) -> str:
    validated = _validate(args)
    if isinstance(validated, str):  # an ERROR: message
        return validated
    chart_type, labels, series = validated

    title = str(args.get("title") or "")
    filename = str(args.get("filename") or "chart.svg")
    if not filename.lower().endswith(".svg"):
        filename += ".svg"

    try:
        svg = render_chart(chart_type, title, labels, series)
    except Exception as exc:  # noqa: BLE001 - report back, don't raise into the loop
        return f"ERROR: failed to render chart: {exc}"

    record = await file_store.save(svg.encode("utf-8"), filename=filename, media_type=SVG_MEDIA_TYPE)
    # Same string shape as create_excel/create_html so the frontend parses it identically.
    return (
        f"Created chart '{record.filename}' "
        f"({record.size} bytes, {chart_type}). "
        f"Download it at: GET /v1/files/{record.id}"
    )


SPEC = LocalToolSpec(
    name="create_chart",
    description=(
        "Create a chart as an SVG image and return a download link. Provide "
        "'chart_type' (one of: 'bar', 'hbar' (horizontal bar), 'line', 'area', "
        "'pie', 'donut'), 'labels' (category/x-axis labels), and 'series' (array "
        "of {name?, data[]} where each data array aligns to labels). Optionally "
        "'title' and 'filename'. For pie/donut, the first series is used and "
        "values must be non-negative."
    ),
    parameters={
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": list(CHART_TYPES),
                "description": (
                    "Chart kind: 'bar', 'hbar' (horizontal bar, good for many/long "
                    "labels), 'line', 'area', 'pie', or 'donut'."
                ),
            },
            "title": {"type": "string", "description": "Optional chart title."},
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Category / x-axis labels (pie: slice labels).",
            },
            "series": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Series name (for the legend)."},
                        "data": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "Numeric values, one per label.",
                        },
                    },
                    "required": ["data"],
                },
                "description": "One or more data series; each data array aligns to labels.",
            },
            "filename": {
                "type": "string",
                "description": "Output file name, e.g. 'chart.svg' (default 'chart.svg').",
            },
        },
        "required": ["chart_type", "labels", "series"],
    },
    func=_create_chart,
)

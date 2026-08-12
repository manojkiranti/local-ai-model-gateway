"""Local (in-process, always-available) tools.

Each tool lives in its own module and exports a `SPEC` (a `LocalToolSpec`).
Adding a tool = add a module here + one line in `LOCAL_TOOLS` below; the engine
in `registry.py` never changes.
"""

from __future__ import annotations

from . import (
    aggregate_excel,
    calculator,
    chart,
    csv,
    date_math,
    docx,
    excel,
    fetch_url,
    get_nrb_forex,
    html,
    inspect_excel,
    pdf,
    read_document,
    read_excel,
    search_department_docs,
    time,
)
from .base import LocalFn, LocalToolSpec

LOCAL_TOOLS: list[LocalToolSpec] = [
    time.SPEC,
    excel.SPEC,
    html.SPEC,
    chart.SPEC,
    pdf.SPEC,
    calculator.SPEC,
    csv.SPEC,
    date_math.SPEC,
    docx.SPEC,
    fetch_url.SPEC,
    inspect_excel.SPEC,
    read_excel.SPEC,
    read_document.SPEC,
    aggregate_excel.SPEC,
    search_department_docs.SPEC,
    get_nrb_forex.SPEC,
]

__all__ = ["LOCAL_TOOLS", "LocalToolSpec", "LocalFn"]

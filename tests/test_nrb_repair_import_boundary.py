"""fontTools and uharfbuzz must never load because the API (or recovery) did.

Subprocess on purpose: `sys.modules` is process-global and other tests import
both libraries. Same reasoning as tests/test_image_ocr_import_boundary.py.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

FORBIDDEN = ("fontTools", "uharfbuzz")


def _probe(import_line: str) -> str:
    code = (f"import sys; {import_line};"
            f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]; print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.parametrize("module", [
    "app.main", "app.nrb.recovery", "app.nrb.rag", "app.nrb.fontrepair",
    "app.nrb.glyphtable", "app.nrb.shaping", "app.nrb.extraction",
])
def test_importing_does_not_load_the_repair_stack(module):
    assert _probe(f"import {module}") == ""

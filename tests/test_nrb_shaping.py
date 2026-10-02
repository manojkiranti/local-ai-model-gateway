"""HarfBuzz, the repair gate's reference shaper (spec §4.3).

A repaired page is accepted only when its text, shaped with the page's own
embedded font, reproduces the glyph ids the page draws. These tests pin the
shaper's contract; the gate itself is tests/test_nrb_fontrepair.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("uharfbuzz")

from app.nrb import shaping  # noqa: E402

LOHIT = Path(__file__).parent / "fixtures" / "fonts" / "Lohit-Devanagari.ttf"


def _program() -> bytes:
    return LOHIT.read_bytes()


def test_the_shaper_is_available_and_names_its_harfbuzz():
    assert shaping.available() is True
    assert shaping.version()[0].isdigit()


def test_a_bare_consonant_shapes_to_one_real_glyph():
    gids = shaping.shape(_program(), "क")
    assert len(gids) == 1 and gids[0] != 0


def test_a_reph_cluster_is_fewer_glyphs_than_characters():
    """र्क is three characters drawn as क plus a reph. A ToUnicode label per
    GLYPH therefore cannot spell it, which is why Word's labels go wrong."""
    assert len(shaping.shape(_program(), "र्क")) < 3


def test_script_and_language_are_fixed():
    """Part of the engine identity: Nepali `locl` changes glyph choice."""
    assert (shaping.SCRIPT, shaping.LANGUAGE) == ("Deva", "ne")


def test_a_missing_uharfbuzz_raises_its_own_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "uharfbuzz", None)
    shaping._font.cache_clear()
    assert shaping.available() is False
    assert shaping.version() == "unavailable"
    with pytest.raises(shaping.ShaperUnavailable):
        shaping.shape(_program(), "क")

"""gid → what the glyph draws, from the font's own cmap + GSUB (spec §3.3).

Two halves. The derivation rules are tested PURE, on hand-written `Rule`s, so
each rule is provable without a font. The real-font half proves those rules are
enough for a real Devanagari font: every glyph HarfBuzz draws for the sample
resolves, and tokens → reorder → shape reproduces the drawn glyphs.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("fontTools")
pytest.importorskip("uharfbuzz")

from app.nrb import glyphtable as GT  # noqa: E402
from app.nrb import shaping  # noqa: E402
from app.nrb.reorder import PREBASE, REPH, reorder  # noqa: E402

FONTS = Path(__file__).parent / "fixtures" / "fonts"
LOHIT = FONTS / "Lohit-Devanagari.ttf"
SYSTEM_KALIMATI = Path("/usr/share/fonts/truetype/fonts-deva-extra/kalimati.ttf")

SAMPLE = (
    "नेपाल राष्ट्र बैंकको निर्देशन अनुसार कार्यालय",
    "प्रकृति विक्रेता गर्दा त्यस्तो वस्तु फिर्ता",
    "क्षेत्र संस्था स्थापना भित्र मिति सकिने",
)


def _r(tags, inputs, output, lookup=0):
    return GT.Rule(frozenset(tags), lookup, tuple(inputs), output)


# --------------------------------------------------------------------------- #
# The rules, pure
# --------------------------------------------------------------------------- #
def test_a_cmap_glyph_says_its_own_character():
    assert GT.derive({1: "क"}, []) == ({1: "क"}, frozenset())


def test_the_seed_marks_the_prebase_sign():
    assert GT.seed_tokens({5: frozenset({"ि"})}) == {5: PREBASE}


def test_the_seed_prefers_the_devanagari_codepoint():
    assert GT.seed_tokens({7: frozenset({"|", "।"})}) == {7: "।"}


def test_a_ligature_concatenates_its_components():
    tokens, _ = GT.derive({1: "क", 2: "्", 3: "ष"}, [_r({"akhn"}, (1, 2, 3), 10)])
    assert tokens[10] == "क्ष"


def test_an_rphf_ligature_is_the_reph_marker():
    tokens, _ = GT.derive({1: "र", 2: "्"}, [_r({"rphf"}, (1, 2), 10)])
    assert tokens[10] == REPH


def test_an_alternate_of_the_reph_is_still_the_reph():
    """The probe's first bug: a reph alternate lost its class."""
    rules = [_r({"rphf"}, (1, 2), 10), _r({"abvs"}, (10,), 11, lookup=1)]
    tokens, _ = GT.derive({1: "र", 2: "्"}, rules)
    assert tokens[11] == REPH


def test_an_old_spec_below_base_ra_reads_as_virama_ra():
    """Kalimati (an old-spec `deva` font) forms the below-base ra from the glyph
    pair [र, ्]; it stands for the logical ्र. Read as र्, प्रकृति became
    पर्कृति (measured 2026-10-02, the round trip refused it)."""
    rules = [_r({"blwf"}, (1, 2), 10), _r({"vatu"}, (3, 10), 11, lookup=1)]
    tokens, _ = GT.derive({1: "र", 2: "्", 3: "प"}, rules)
    assert tokens[10] == "्र"
    assert tokens[11] == "प्र"


def test_a_new_spec_below_base_ra_is_already_logical():
    tokens, _ = GT.derive({1: "्", 2: "र"}, [_r({"blwf"}, (1, 2), 10)])
    assert tokens[10] == "्र"


def test_a_matra_plus_reph_ligature_keeps_both():
    rules = [_r({"rphf"}, (1, 2), 10), _r({"abvs"}, (5, 10), 12, lookup=1)]
    tokens, _ = GT.derive({1: "र", 2: "्", 5: "े"}, rules)
    assert tokens[12] == "े" + REPH


def test_two_derivations_that_disagree_make_the_glyph_ambiguous():
    rules = [_r({"pres"}, (1,), 10), _r({"pres"}, (2,), 10, lookup=1)]
    tokens, ambiguous = GT.derive({1: "क", 2: "ख"}, rules)
    assert 10 in ambiguous and 10 not in tokens


def test_ambiguity_propagates_into_a_ligature_built_from_it():
    rules = [_r({"pres"}, (1,), 10), _r({"pres"}, (2,), 10, lookup=1),
             _r({"pres"}, (10, 3), 11, lookup=2)]
    tokens, ambiguous = GT.derive({1: "क", 2: "ख", 3: "्"}, rules)
    assert 11 in ambiguous and 11 not in tokens


def test_a_lookup_shared_by_rphf_and_another_feature_is_refused():
    tokens, ambiguous = GT.derive({1: "र", 2: "्"}, [_r({"rphf", "half"}, (1, 2), 10)])
    assert 10 in ambiguous and 10 not in tokens


def test_a_non_default_feature_is_ignored():
    """`aalt` is not applied by Word or HarfBuzz; following it would make the
    target ambiguous for no reason."""
    tokens, ambiguous = GT.derive({1: "क"}, [_r({"aalt"}, (1,), 10)])
    assert 10 not in tokens and 10 not in ambiguous


def test_canonically_equal_derivations_are_one_token():
    """क़ precomposed (U+0958) and क + nukta are the same text under NFC."""
    tokens, ambiguous = GT.derive({1: "क़", 2: "क", 3: "़"},
                                  [_r({"nukt"}, (2, 3), 1)])
    assert 1 in tokens and 1 not in ambiguous


# --------------------------------------------------------------------------- #
# A real font
# --------------------------------------------------------------------------- #
def _table(path: Path = LOHIT) -> GT.GlyphTable:
    return GT.build_glyph_table(path.read_bytes())


def _fonts():
    yield LOHIT
    if SYSTEM_KALIMATI.exists():
        yield SYSTEM_KALIMATI


@pytest.mark.parametrize("font", list(_fonts()), ids=lambda p: p.stem)
def test_every_glyph_harfbuzz_draws_for_the_sample_resolves(font):
    table, program = _table(font), font.read_bytes()
    for text in SAMPLE:
        for gid in shaping.shape(program, text):
            assert table.token(gid) is not None, (text, gid)


@pytest.mark.parametrize("font", list(_fonts()), ids=lambda p: p.stem)
def test_the_sample_round_trips_through_the_table(font):
    """The gate in miniature: glyphs → tokens → reorder → shape == glyphs."""
    table, program = _table(font), font.read_bytes()
    for text in SAMPLE:
        gids = shaping.shape(program, text)
        out = reorder("".join(table.token(g) for g in gids))
        assert out.orphans == 0, text
        assert out.text == text
        assert shaping.shape(program, out.text) == gids


def test_identity_records_the_glyph_count_and_the_advance_widths():
    identity = _table().identity
    assert identity.glyph_count == 711
    assert len(identity.advance_sha256) == 64
    assert identity.label().endswith("/711")


def _without_gsub(program: bytes) -> bytes:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    del font["GSUB"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def _symbol_cmap_only(program: bytes) -> bytes:
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


def test_a_font_without_gsub_resolves_only_its_cmap_glyphs():
    """The Word-365 shape (spec §7): cmap kept, GSUB stripped."""
    full = LOHIT.read_bytes()
    stripped = GT.build_glyph_table(_without_gsub(full))
    assert stripped.has_gsub is False
    drawn = shaping.shape(full, "र्क")
    assert any(stripped.token(g) is None for g in drawn)


def test_a_font_with_no_unicode_cmap_says_nothing():
    """Review Focus 1: measured on 3 of 7 Word documents. No crash, no tokens."""
    table = GT.build_glyph_table(_symbol_cmap_only(LOHIT.read_bytes()))
    assert table.cmap_chars == {}
    assert table.tokens == {}

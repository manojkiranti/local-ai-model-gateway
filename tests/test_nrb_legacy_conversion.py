"""Phase 6B legacy-font conversion: adapter, guards, validation, routing.

Pure and offline. No database, no network, no blob reads. The Preeti fixtures are
copied verbatim from real NRB benchmark extractions (`docs/nrb/phase6a-profile.txt`
STEP 5) with their hand-verified Unicode, so a rule change is scored against a
labelled set rather than eyeballed.

The negative controls carry the weight here. The spike's central finding is that a
converter fed English produces confident-looking Devanagari — 91% script, legacy
ratio collapsed to 0.0, character count preserved — so a suite that only checked
"did Nepali come out" would pass while the pipeline destroyed the corpus's English
tables.
"""

import json

import pytest

from app.nrb import devanagari, legacy_convert as LC, legacy_font, lexicon as LX, quality

# --------------------------------------------------------------------------- #
# Fixtures: real text, real answers.
# --------------------------------------------------------------------------- #

# `041902065a1d`, Akhtiyar Pratyayojan Biniyamawali 2065.
PREETI_LINE = "g]kfn /fi6« a}+ssf] k|of]hgsf] nflu dfq"
PREETI_LINE_UNICODE = "नेपाल राष्ट्र बैंकको प्रयोजनको लागि मात्र"

# Three tokens — BELOW `quality.LEGACY_LINE_MIN_TOKENS`, so the detector returns
# None for it. The single most important routing fixture in the file: this is a
# document heading, and a two-branch router leaves it in Preeti forever.
PREETI_HEADING = "g]kfn /fi6« a}+s"
PREETI_HEADING_UNICODE = "नेपाल राष्ट्र बैंक"

PREETI_SENTENCE = ";~rfns ;ldltn] b]xfosf ljlgodx? agfPsf] 5 ."
PREETI_SENTENCE_UNICODE = "सञ्चालक समितिले देहायका विनियमहरु बनाएको छ ।"

# `05fa82badf94` — the benchmark's known FALSE POSITIVE: a completely readable
# English statistics table that native-1 flags `legacy_font_suspected` at 0.2632.
ENGLISH_TABLE_HEADER = "Instruments Times Offer Amount"
ENGLISH_TABLE_UNITS = "(Rs. in crore)"
ENGLISH_TABLE_ROW = "   iv. NRB Bond -                    -                    -"

# `075bf12eb087` — genuine Unicode Devanagari, correctly classified clean.
UNICODE_NEPALI = (
    "सम्पत्ति शुद्धीकरण (मनी लाउन्डररङ) निवारण ऐन, २०६४ र सो अन्तर्गत बनेका "
    "नियमावलीहरुको प्रभावकारी कार्यान्वयन गर्न"
)

ENGLISH_PROSE = (
    "The Bank shall issue a circular to all licensed institutions and every "
    "bank shall report its exposure within thirty days of the quarter end."
)


@pytest.fixture(scope="module")
def lexicon() -> LX.Lexicon:
    """A small hand-built vocabulary.

    Deliberately NOT the frozen corpus artifact: these tests must state their own
    inputs, and a suite that depended on `docs/nrb/phase6b-lexicon.json` would
    start failing whenever the corpus was re-harvested — for reasons unrelated to
    the rule under test.
    """
    english = frozenset(
        """the bank shall issue circular all licensed institutions and every
        report its exposure within thirty days quarter end instruments times
        offer amount rs crore nrb bond slf turnover outstanding liquidity
        absorbing""".split()
    )
    nepali = frozenset(
        """नेपाल राष्ट्र बैंक बैंकको प्रयोजनको लागि मात्र सञ्चालक समितिले देहायका
        विनियमहरु बनाएको छ आर्थिक वर्ष को मौद्रिक नीतिको""".split()
    )
    return LX.Lexicon(
        version=LX.LEXICON_VERSION,
        english=english,
        nepali=nepali,
        fingerprint=LX.lexicon_fingerprint(LX.LEXICON_VERSION, english, nepali),
        provenance={"source": "test fixture"},
    )


@pytest.fixture(scope="module")
def preeti():
    return legacy_font.converter_for("Preeti")


# --------------------------------------------------------------------------- #
# 1. The adapter.
# --------------------------------------------------------------------------- #

def test_preeti_converts_a_known_line_to_the_expected_unicode(preeti):
    assert preeti.convert(PREETI_LINE) == PREETI_LINE_UNICODE
    assert preeti.convert(PREETI_HEADING) == PREETI_HEADING_UNICODE
    assert preeti.convert(PREETI_SENTENCE) == PREETI_SENTENCE_UNICODE


def test_conversion_is_deterministic(preeti):
    assert preeti.convert(PREETI_LINE) == preeti.convert(PREETI_LINE)


def test_empty_input_converts_to_empty_rather_than_raising(preeti):
    assert preeti.convert("") == ""


def test_the_adapter_satisfies_the_protocol(preeti):
    assert isinstance(preeti, legacy_font.LegacyFontConverter)
    assert preeti.name == "npttf2utf"
    assert preeti.mapping == "Preeti"
    assert preeti.version


def test_an_unknown_mapping_fails_explicitly_and_never_substitutes():
    with pytest.raises(legacy_font.ConverterUnavailable) as exc:
        legacy_font.converter_for("Helvetica")
    # Naming what IS available is what stops the caller guessing.
    assert "Preeti" in str(exc.value)


def test_a_missing_backend_is_an_explicit_failure(monkeypatch):
    """Absence must raise, never quietly return the input.

    A converter that no-ops is indistinguishable from a document that needed no
    conversion, and this whole phase exists to tell those two apart.
    """
    legacy_font._font_mapper.cache_clear()
    monkeypatch.setattr(
        legacy_font, "_font_mapper",
        lambda: (_ for _ in ()).throw(legacy_font.ConverterUnavailable("not installed")),
    )
    conv = legacy_font.Npttf2UtfConverter(mapping="Preeti", version="0.0.0")
    with pytest.raises(legacy_font.ConverterUnavailable):
        conv.convert(PREETI_LINE)


def test_every_declared_mapping_is_available():
    """`MAPPINGS` is what 0.3.7 shipped; a pin bump that drops one must be loud."""
    assert set(legacy_font.available_mappings()) == set(legacy_font.MAPPINGS)


# --------------------------------------------------------------------------- #
# 2. Structural plausibility — what `devanagari_ratio` cannot see.
# --------------------------------------------------------------------------- #

def test_correct_conversion_has_no_illegal_clusters_and_no_latin_residue():
    shape = devanagari.measure_devanagari(PREETI_LINE_UNICODE)
    assert shape.illegal_clusters == 0
    assert shape.latin_residue_tokens == 0
    assert shape.devanagari_ratio > 0.9


def test_converted_english_is_structurally_impossible_devanagari(preeti):
    """The finding that shaped the phase, asserted.

    High Devanagari ratio, and every structural signal screaming.
    """
    wreck = preeti.convert(ENGLISH_TABLE_HEADER)
    shape = devanagari.measure_devanagari(wreck)
    assert shape.devanagari_ratio > 0.8          # looks like a success…
    assert shape.illegal_clusters > 0            # …and is not one
    assert shape.latin_residue_ratio > 0.5


def test_an_independent_vowel_cannot_take_a_matra():
    assert devanagari.illegal_cluster_count("इाा") == 2
    assert devanagari.illegal_cluster_count("इन") == 0


def test_a_mark_cannot_open_a_token():
    # Two faults in `ािक`: the leading matra has no base, and the second matra
    # follows the first. Both are counted — the measure is of sequences, not of
    # tokens, so one badly-formed token can contribute several.
    assert devanagari.illegal_cluster_count("ािक") == 2
    assert devanagari.illegal_cluster_count("्रम") == 1


def test_legal_conjuncts_are_not_counted_as_illegal():
    """Unusual is not impossible. Over-counting here would turn the measure into
    an opinion about content."""
    for legal in ("राष्ट्र", "क्षेत्र", "विद्युत्", "सम्बन्धी", "कार्यान्वयन"):
        assert devanagari.illegal_cluster_count(legal) == 0, legal


def test_structure_alone_cannot_tell_a_wrong_mapping_from_a_right_one():
    """Documents the limit that makes vocabulary necessary.

    `द्दण्टछ` is FONTASY nonsense where `२०६५` is the correct Preeti reading, and
    both are perfectly legal Devanagari.
    """
    assert devanagari.illegal_cluster_count("द्दण्टछ") == 0
    assert devanagari.illegal_cluster_count("२०६५") == 0


# --------------------------------------------------------------------------- #
# 3. The pre-conversion English guard.
# --------------------------------------------------------------------------- #

def test_the_english_table_lines_are_guarded(lexicon):
    for line in (ENGLISH_TABLE_HEADER, ENGLISH_TABLE_UNITS, ENGLISH_PROSE):
        assert LX.is_confidently_english(line, lexicon), line


def test_a_single_known_english_word_on_its_own_line_is_guarded(lexicon):
    """`Turnover` and `Outstanding` are whole lines in the benchmark table, and a
    flat two-run floor abstained on them."""
    assert LX.is_confidently_english("Turnover", lexicon)
    assert LX.is_confidently_english("Outstanding   ", lexicon)


def test_preeti_is_never_mistaken_for_english(lexicon):
    for line in (PREETI_LINE, PREETI_HEADING, PREETI_SENTENCE):
        assert not LX.is_confidently_english(line, lexicon), line


def test_a_short_latin_fragment_is_not_enough_to_guard(lexicon):
    """One three-letter run must not veto conversion — that is the shape of
    glyph-mapped text, not of an English line."""
    assert not LX.is_confidently_english("kfn", lexicon)


def test_devanagari_digits_and_dandas_are_not_nepali_words():
    """A mapping that puts digits in the right BLOCK but gets every digit wrong
    must not score for it."""
    assert LX.nepali_tokens("नेपाल २०६५ । बैंक") == ["नेपाल", "बैंक"]


def test_the_english_harvest_skips_glyph_mapped_lines():
    """The measured poisoning: Preeti tokens inside an otherwise-English document
    became "English words" and disabled the guard on real Nepali."""
    document = f"{ENGLISH_PROSE}\n{PREETI_SENTENCE}\n"
    harvested = set(LX.english_harvest(document))
    assert "circular" in harvested
    assert "ljlgodx" not in harvested


def test_a_lexicon_round_trips_and_an_edited_one_is_rejected(lexicon, tmp_path):
    path = tmp_path / "lex.json"
    path.write_text(json.dumps(lexicon.as_json()), encoding="utf-8")
    assert LX.load_lexicon(path).fingerprint == lexicon.fingerprint

    tampered = lexicon.as_json()
    tampered["english"] = sorted(set(tampered["english"]) | {"smuggled"})
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(LX.LexiconError, match="has been edited"):
        LX.load_lexicon(path)


def test_a_lexicon_that_is_not_the_one_named_is_rejected(lexicon, tmp_path):
    path = tmp_path / "lex.json"
    path.write_text(json.dumps(lexicon.as_json()), encoding="utf-8")
    with pytest.raises(LX.LexiconError, match="not the expected"):
        LX.load_lexicon(path, expect_fingerprint="0" * 64)


# --------------------------------------------------------------------------- #
# 4. Validation.
# --------------------------------------------------------------------------- #

def test_a_known_good_conversion_is_accepted(lexicon):
    out = LC.validate_conversion(PREETI_LINE, PREETI_LINE_UNICODE, lexicon)
    assert out.outcome == LC.ACCEPTED
    assert out.reasons == ()


def test_converted_english_is_rejected_on_structure(lexicon, preeti):
    out = LC.validate_conversion(
        ENGLISH_TABLE_HEADER, preeti.convert(ENGLISH_TABLE_HEADER), lexicon
    )
    assert out.outcome == LC.REJECTED
    assert "illegal_devanagari_clusters" in out.reasons
    assert "latin_residue" in out.reasons


def test_catastrophic_empty_output_is_rejected(lexicon):
    out = LC.validate_conversion(PREETI_LINE, "", lexicon)
    assert out.outcome == LC.REJECTED
    assert "empty_output" in out.reasons


def test_a_conversion_producing_no_devanagari_is_rejected(lexicon):
    out = LC.validate_conversion(PREETI_LINE, "still latin text here", lexicon)
    assert out.outcome == LC.REJECTED
    assert "no_devanagari_emerged" in out.reasons


def test_extreme_expansion_is_rejected(lexicon):
    out = LC.validate_conversion(PREETI_HEADING, PREETI_HEADING_UNICODE * 40, lexicon)
    assert out.outcome == LC.REJECTED
    assert "excessive_expansion" in out.reasons


def test_normal_devanagari_expansion_is_not_rejected(lexicon):
    """Devanagari legitimately grows — the Preeti workbook expanded 42.6%. A
    naive length-preservation rule would reject every correct conversion."""
    out = LC.validate_conversion(PREETI_SENTENCE, PREETI_SENTENCE_UNICODE, lexicon)
    assert "excessive_expansion" not in out.reasons
    assert "excessive_shrinkage" not in out.reasons


def test_vocabulary_cannot_reject_only_leave_ambiguous(lexicon):
    """Structure rejects; vocabulary only confirms.

    Genuine Preeti lines with plenty of Devanagari score zero against a thin
    lexicon about 10% of the time. Rejecting on that would throw away correct
    Nepali, so an unconfirmable line is AMBIGUOUS and keeps its conversion.
    """
    unknown_but_valid = "यसै सम्बन्धमा तपसिलका कुराहरु उल्लेख गरिएको व्यहोरा जानकारी"
    out = LC.validate_conversion(PREETI_SENTENCE, unknown_but_valid, lexicon)
    assert out.outcome == LC.AMBIGUOUS
    assert out.outcome != LC.REJECTED


# --------------------------------------------------------------------------- #
# 5. Line-level routing and preservation.
# --------------------------------------------------------------------------- #

def test_a_mixed_document_converts_nepali_and_preserves_english(lexicon, preeti):
    """THE mixed-document test. A converter that recovers the Nepali but corrupts
    the English annex is not acceptable."""
    document = "\n".join(
        [PREETI_HEADING, PREETI_LINE, PREETI_SENTENCE, "", ENGLISH_PROSE,
         ENGLISH_TABLE_HEADER, ENGLISH_TABLE_UNITS]
    )
    result = LC.convert_document(document, preeti, lexicon)
    lines = result.text.split("\n")

    assert lines[0] == PREETI_HEADING_UNICODE      # the UNJUDGED heading, recovered
    assert lines[1] == PREETI_LINE_UNICODE
    assert lines[2] == PREETI_SENTENCE_UNICODE
    assert lines[3] == ""
    assert lines[4] == ENGLISH_PROSE               # byte-for-byte
    assert lines[5] == ENGLISH_TABLE_HEADER
    assert lines[6] == ENGLISH_TABLE_UNITS
    assert len(lines) == 7                         # order and count preserved


def test_an_unjudged_short_line_is_converted_inside_a_legacy_document(lexicon, preeti):
    """`quality.line_looks_glyph_mapped` returns None here; a two-branch router
    would leave 26.3% of the cohort's non-empty lines in Preeti."""
    assert quality.line_looks_glyph_mapped(PREETI_HEADING) is None
    result = LC.convert_document(PREETI_HEADING, preeti, lexicon,
                                 document_legacy_ratio=1.0)
    assert result.text == PREETI_HEADING_UNICODE
    assert result.lines[0].disposition in (LC.CONVERTED_UNJUDGED, LC.AMBIGUOUS_LINE)


def test_an_unjudged_line_outside_a_legacy_document_is_left_alone(lexicon, preeti):
    result = LC.convert_document(PREETI_HEADING, preeti, lexicon,
                                 document_legacy_ratio=0.0)
    assert result.text == PREETI_HEADING
    assert result.lines[0].disposition == LC.KEPT_CLEAN


def test_genuine_unicode_nepali_is_never_touched(lexicon, preeti):
    """The converter is NOT a no-op on correct Devanagari — measured, it turns
    `(मनी लाउन्डररङ)` into `९मनी लाउन्डररङ०` while RAISING the Devanagari ratio.
    Only the guard stops it."""
    result = LC.convert_document(UNICODE_NEPALI, preeti, lexicon)
    assert result.text == UNICODE_NEPALI
    assert result.counts[LC.KEPT_UNICODE] == 1
    assert result.converted_lines == 0


def test_an_english_table_document_comes_back_byte_identical(lexicon, preeti):
    document = "\n".join([ENGLISH_TABLE_HEADER, ENGLISH_TABLE_UNITS,
                          ENGLISH_TABLE_ROW, ENGLISH_PROSE])
    result = LC.convert_document(document, preeti, lexicon)
    assert result.text == document
    assert result.converted_lines == 0


def test_empty_and_no_text_input_is_not_converted(lexicon, preeti):
    """The OCR negative control: a blob with no text layer extracts to "" and the
    converter must not fabricate a recovery from it."""
    result = LC.convert_document("", preeti, lexicon)
    assert result.text == ""
    assert result.converted_lines == 0

    whitespace = LC.convert_document("\n   \n\n", preeti, lexicon)
    assert whitespace.text == "\n   \n\n"
    assert whitespace.converted_lines == 0


def test_reconstruction_is_byte_exact_including_line_endings(lexicon, preeti):
    """`"\\n".join(splitlines())` silently normalises CRLF and eats a trailing
    newline, which would make the negative controls report a diff they did not
    cause."""
    document = f"{ENGLISH_PROSE}\r\n{ENGLISH_TABLE_UNITS}\n"
    assert LC.convert_document(document, preeti, lexicon).text == document


def test_a_rejected_line_keeps_its_original_text(lexicon, preeti):
    document = "Liquidity Absorbing Instruments Times"
    result = LC.convert_document(document, preeti, lexicon)
    assert result.text == document
    assert all(line.disposition != LC.CONVERTED for line in result.lines)


def test_counts_always_report_every_disposition(lexicon, preeti):
    """A report that omits empty buckets makes "no English was guarded" and "the
    guard never ran" look identical."""
    result = LC.convert_document(PREETI_LINE, preeti, lexicon)
    assert set(result.counts) == set(LC.DISPOSITIONS)


# --------------------------------------------------------------------------- #
# 6. Spreadsheets — per cell, never per rendered row.
# --------------------------------------------------------------------------- #

def test_cells_convert_without_destroying_the_column_separator(lexicon, preeti):
    """`extraction.py` renders a row as `" | ".join(cells)` and `|` is a Preeti
    codepoint that maps to `्र`. Converting rendered rows turns every separator
    into a conjunct — measured on `8df7b02f8a13`."""
    rows = [(PREETI_HEADING, ENGLISH_TABLE_HEADER), (PREETI_LINE, "1,234.00")]
    conversion, grid = LC.convert_cells(rows, preeti, lexicon)

    assert grid[0][0] == PREETI_HEADING_UNICODE
    assert grid[0][1] == ENGLISH_TABLE_HEADER      # untouched
    assert grid[1][0] == PREETI_LINE_UNICODE
    assert grid[1][1] == "1,234.00"
    assert [len(r) for r in grid] == [2, 2]        # shape preserved
    # And the separator survives: rebuilding the row from converted CELLS keeps
    # ` | `, where converting the rendered row would have eaten it. (`्र` cannot
    # be searched for directly — राष्ट्र legitimately contains it.)
    assert " | ".join(grid[0]) == f"{PREETI_HEADING_UNICODE} | {ENGLISH_TABLE_HEADER}"


def test_a_numeric_cell_is_never_converted(lexicon, preeti):
    """Preeti maps ASCII digits to Devanagari digits, so `1,234.00` becomes
    `ज्ञ,द्दघद्ध।ण्ण्` — high Devanagari, no illegal clusters, no latin residue. It
    passes every validation rule while destroying a number, so it has to be
    stopped at routing by the detector's own `LEGACY_MIN_LATIN` condition."""
    rows = [("1,234.00", "56.7%"), ("2,123,180.00", "-")]
    _, grid = LC.convert_cells(rows, preeti, lexicon)
    assert grid == (("1,234.00", "56.7%"), ("2,123,180.00", "-"))


def test_converting_a_rendered_row_is_what_we_must_not_do(preeti):
    """Documents the trap directly rather than only avoiding it."""
    rendered = " | ".join([PREETI_HEADING, "x"])
    assert "्र" in preeti.convert(rendered)


# --------------------------------------------------------------------------- #
# 7. Mapping independence.
# --------------------------------------------------------------------------- #

def test_every_mapping_starts_from_the_same_original_text(lexicon):
    """No cascading: one mapping's corruption must never become another's input.

    Asserted by construction — each converter is handed the ORIGINAL and the
    results are compared, never chained.
    """
    outputs = {}
    for conv in legacy_font.converters():
        outputs[conv.mapping] = LC.convert_document(
            PREETI_LINE, conv, lexicon
        ).text
    # Preeti is right here; FONTASY/PCS differ. What matters is that re-running
    # any mapping on the original reproduces its own answer exactly.
    for conv in legacy_font.converters():
        again = LC.convert_document(PREETI_LINE, conv, lexicon).text
        assert again == outputs[conv.mapping]
    assert outputs["Preeti"] == PREETI_LINE_UNICODE


def test_mappings_disagree_on_digits_which_is_why_all_are_evaluated():
    """`@)^%` is २०६५ under Preeti and nonsense under FONTASY — and the WRONG one
    scores the higher Devanagari ratio. Recorded as the reason no mapping is
    chosen automatically in this task."""
    preeti = legacy_font.converter_for("Preeti").convert("@)^%")
    fontasy = legacy_font.converter_for("FONTASY_HIMALI_TT").convert("@)^%")
    assert preeti == "२०६५"
    assert fontasy != preeti
    assert devanagari.measure_devanagari(fontasy).devanagari_ratio >= (
        devanagari.measure_devanagari(preeti).devanagari_ratio
    )


# --------------------------------------------------------------------------- #
# 8. No side effects.
# --------------------------------------------------------------------------- #

def test_conversion_touches_no_database_and_no_network(monkeypatch, lexicon, preeti):
    """`legacy_convert` is pure. Anything it imported that could do I/O would show
    up here as an exception rather than as a surprise in production."""
    import socket

    def _no_network(*args, **kwargs):  # pragma: no cover - the point is not calling it
        raise AssertionError("conversion attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    result = LC.convert_document(f"{PREETI_LINE}\n{ENGLISH_PROSE}", preeti, lexicon)
    assert result.converted_lines == 1

"""Deterministic extraction-quality metrics. Pure — no DB, no network, no files.

The inputs here are hand-authored rather than sampled, because the point is a
LABELLED set: each string has a known correct answer, so a rule change is scored
rather than eyeballed. The legacy-font strings are copied from real NRB circular
extractions (docs/superpowers/specs/2026-08-15-… §2), not invented.
"""

from app.nrb import quality

ENGLISH = (
    "Nepal Rastra Bank issued a circular to all licensed institutions today. "
    "The circular requires that every bank shall report its exposure to the "
    "central bank within thirty days of the end of the quarter."
)

# Unicode Devanagari — a correctly extracted Nepali document. The single most
# important negative case in the suite: this must never be called suspicious.
NEPALI = (
    "नेपाल राष्ट्र बैंकले सम्पूर्ण इजाजतपत्रप्राप्त संस्थाहरूलाई परिपत्र जारी गरेको छ। "
    "प्रत्येक बैंकले आफ्नो जोखिम विवरण तीस दिनभित्र केन्द्रीय बैंकमा पेश गर्नुपर्नेछ।"
)

# Legacy-font (Preeti/Kantipur) extraction, verbatim shape from a real NRB
# circular: latin codepoints carrying Devanagari glyphs, with the English
# letterhead fragments that make a naive "is this English" test say yes.
LEGACY = (
    "ffihW\\ffifiHrz\\reU=,.\n"
    "iqrn rrq *+,\n"
    "ff{T ffi {eil Fuqq h}Trtr\n"
    "qtq i.: YYlqqoYr/{\n"
    "Web Site: www.nrb.org.np\n"
    "frq qw:igl ffi; Qoec,/o!/lR q{ {@r : i.H.H.tq\n"
    "fi156dqgfq1 r4r \"€\" ( \"1T\" 4{i-4;f t+ Oqf ffiq d1rT[6{\n"
    "q_fie( ffirra, i.;. oi Frtqm { R/oeq +1 gET d r. ]T qR-{-rq qq-r{\n"
)


def test_empty_text_is_all_zero_and_never_divides_by_zero():
    m = quality.measure_text("")
    assert m.char_count == 0
    assert m.token_count == 0
    assert m.printable_ratio == 0.0
    assert m.devanagari_ratio == 0.0
    assert m.stopword_rate == 0.0


def test_whitespace_only_text_has_no_non_whitespace_characters():
    m = quality.measure_text("   \n\n\t  ")
    assert m.char_count == 8
    assert m.non_whitespace_chars == 0
    assert m.devanagari_ratio == 0.0
    assert m.latin_letter_ratio == 0.0


def test_english_prose_has_a_high_stopword_rate_and_latin_dominance():
    m = quality.measure_text(ENGLISH)
    assert m.latin_letter_ratio > 0.7
    assert m.devanagari_ratio == 0.0
    # Real English prose runs 0.15-0.25. This is the signal the legacy detector
    # keys on, so its floor matters more than its exact value.
    assert m.stopword_rate > 0.15
    assert m.vowelless_token_ratio < 0.10
    assert m.intraword_symbol_ratio < 0.05


def test_unicode_nepali_is_devanagari_dominant_with_no_english_structure():
    m = quality.measure_text(NEPALI)
    assert m.devanagari_ratio > 0.7
    assert m.latin_letter_ratio < 0.05
    # Zero English stopwords, exactly like the legacy case — which is WHY the
    # detector must gate on latin_letter_ratio before it looks at this number.
    assert m.stopword_rate < 0.02


def test_mixed_nepali_and_english_lands_between_the_two():
    m = quality.measure_text(NEPALI + "\n" + ENGLISH)
    assert 0.2 < m.devanagari_ratio < 0.8
    assert 0.2 < m.latin_letter_ratio < 0.8


def test_legacy_font_output_is_latin_with_no_english_structure():
    m = quality.measure_text(LEGACY)
    assert m.devanagari_ratio < 0.01
    assert m.latin_letter_ratio > 0.35
    # The signals the detector combines, each asserted separately so a
    # regression names which one moved.
    assert m.stopword_rate < 0.02
    assert m.vowelless_token_ratio > 0.30
    assert m.intraword_symbol_ratio > 0.15


def test_control_characters_are_counted_and_lower_the_printable_ratio():
    m = quality.measure_text("abc\x00\x01\x02\x03def")
    assert m.control_char_ratio > 0.3
    assert m.printable_ratio < 0.7


def test_tab_and_newline_are_printable_not_control():
    m = quality.measure_text("a\tb\nc\r\nd")
    assert m.control_char_ratio == 0.0
    assert m.printable_ratio == 1.0


def test_replacement_characters_are_counted_and_rationed():
    m = quality.measure_text("abcd����")
    assert m.replacement_char_count == 4
    assert m.replacement_char_ratio == 0.5


def test_digits_and_punctuation_are_measured_separately_from_letters():
    m = quality.measure_text("1,234.56 | 7,890.12 | 3,456.78")
    assert m.digit_ratio > 0.5
    assert m.latin_letter_ratio == 0.0


def test_ratios_are_over_non_whitespace_so_indentation_cannot_move_them():
    tight = quality.measure_text("नेपाल")
    padded = quality.measure_text("        नेपाल        \n\n\n")
    assert tight.devanagari_ratio == padded.devanagari_ratio == 1.0


def test_intraword_case_switch_catches_legacy_shapes_but_not_acronyms():
    # `ljQLo` / `k|fKt` switch case mid-token; `NRB` and `PDF` do not (all upper).
    assert quality.measure_text("ljQLo k|fKt aBcDe").intraword_case_switch_ratio > 0.5
    assert quality.measure_text("NRB PDF USD IMF").intraword_case_switch_ratio == 0.0


def test_devanagari_combining_marks_are_word_characters_not_symbols():
    """Nepali vowel signs are category Mn, so `isalnum()` calls them symbols.

    Left uncorrected, correctly extracted Nepali scores an intraword_symbol_ratio
    of ~0.95 — HIGHER than the legacy-font garbage this metric exists to detect.
    It never changes a verdict (the legacy rule gates on devanagari_ratio first),
    but the metric is persisted and printed, so an inverted one would mislead the
    first person to compare a Nepali document with an English one.
    """
    assert quality.measure_text(NEPALI).intraword_symbol_ratio < 0.05
    # ...while the legacy-font shape still registers.
    assert quality.measure_text(LEGACY).intraword_symbol_ratio > 0.15


def test_line_counts_distinguish_blank_lines():
    m = quality.measure_text("one\n\ntwo\n\n\nthree")
    assert m.line_count == 6
    assert m.non_empty_lines == 3


def test_as_dict_round_trips_every_field_as_a_json_safe_scalar():
    d = quality.measure_text(ENGLISH).as_dict()
    assert set(d) == {f.name for f in quality.TextMetrics.__dataclass_fields__.values()}
    assert all(isinstance(v, (int, float)) for v in d.values())


def test_measure_text_is_deterministic():
    assert quality.measure_text(LEGACY) == quality.measure_text(LEGACY)

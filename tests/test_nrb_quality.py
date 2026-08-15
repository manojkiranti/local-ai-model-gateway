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


# --------------------------------------------------------------------------- #
# Page/sheet statistics and the classifier
# --------------------------------------------------------------------------- #
from app.nrb.quality import (  # noqa: E402
    Evidence,
    SheetStats,
    STATUS_EXTRACTED,
    STATUS_FAILED,
    STATUS_NEEDS_OCR,
    STATUS_SUSPICIOUS,
    STATUS_UNSUPPORTED,
)


def _pdf_evidence(text, page_texts):
    return Evidence(
        family="pdf",
        parsed=True,
        error=None,
        text_metrics=quality.measure_text(text),
        pages=quality.measure_pages(page_texts),
        sheets=None,
    )


# --- page statistics ------------------------------------------------------- #

def test_page_coverage_counts_pages_that_produced_text():
    stats = quality.measure_pages(["hello there", "", "more words", ""])
    assert stats.page_count == 4
    assert stats.pages_with_text == 2
    assert stats.text_page_coverage == 0.5


def test_page_stats_on_zero_pages_do_not_divide_by_zero():
    stats = quality.measure_pages([])
    assert stats.page_count == 0
    assert stats.text_page_coverage == 0.0
    assert stats.median_chars_per_page == 0.0


def test_the_two_medians_separate_a_partial_scan_from_a_sparse_text_layer():
    """One number cannot tell these apart, which is why there are two.

    A median over ALL pages collapses to 0 as soon as more than half the pages
    are blank — so a partly-scanned document with one perfectly readable page
    would report a "sparse text layer", which is a different and wrong diagnosis.
    """
    prose = "x" * 400
    partial = quality.measure_pages([prose, "", "", "", "", ""])
    assert partial.text_page_coverage < 0.2          # few pages have text...
    assert partial.median_chars_per_text_page == 400  # ...but those that do are fine
    assert partial.median_chars_per_page == 0.0       # the all-pages median lies

    stamped = quality.measure_pages(["1", "2", "3", "4", "5", "6"])
    assert stamped.text_page_coverage == 1.0          # every page has "text"...
    assert stamped.median_chars_per_text_page == 1.0  # ...and none of it is real


def test_a_verdict_keeps_its_warnings_even_when_a_page_rule_returns_first():
    """Warnings are collected before any branch returns.

    The page-structure rules used to return a bare Verdict, silently dropping an
    already-accumulated `insufficient_text`.
    """
    verdict = quality.classify(_pdf_evidence("k|fKt ljQLo", ["k|fKt ljQLo"]))
    assert verdict.status == STATUS_NEEDS_OCR
    assert "insufficient_text" in verdict.warnings


def test_median_chars_per_page_resists_one_huge_page():
    # Mean would be ~2000 and call this document text-rich; the median says 10.
    stats = quality.measure_pages(["x" * 10, "x" * 10, "x" * 10, "x" * 8000])
    assert stats.median_chars_per_page == 10.0


# --- classification -------------------------------------------------------- #

def test_good_english_pdf_is_extracted():
    verdict = quality.classify(_pdf_evidence(ENGLISH * 4, [ENGLISH] * 4))
    assert verdict.status == STATUS_EXTRACTED


def test_unicode_nepali_pdf_is_extracted_not_suspicious():
    # THE false-positive test. A correctly extracted Nepali document must pass.
    verdict = quality.classify(_pdf_evidence(NEPALI * 6, [NEPALI] * 6))
    assert verdict.status == STATUS_EXTRACTED


def test_legacy_font_pdf_is_suspicious_with_the_legacy_reason():
    verdict = quality.classify(_pdf_evidence(LEGACY * 6, [LEGACY] * 6))
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "legacy_font_suspected"


def test_pdf_with_pages_but_no_text_needs_ocr():
    verdict = quality.classify(_pdf_evidence("", ["", "", "", ""]))
    assert verdict.status == STATUS_NEEDS_OCR
    assert verdict.reason == "no_text_layer"


def test_pdf_with_a_trivial_text_layer_needs_ocr():
    # A scanned PDF whose only text is a stamped page number per page.
    verdict = quality.classify(_pdf_evidence("1 2 3 4", ["1", "2", "3", "4"]))
    assert verdict.status == STATUS_NEEDS_OCR
    assert verdict.reason == "sparse_text_layer"


def test_partly_scanned_pdf_is_suspicious_not_extracted():
    pages = [ENGLISH, "", "", "", "", ""]   # coverage 0.166
    verdict = quality.classify(_pdf_evidence(ENGLISH, pages))
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "partial_text_coverage"


def test_mostly_readable_pdf_is_extracted_but_warns_about_the_gap():
    pages = [ENGLISH] * 7 + ["", ""]        # coverage 0.777
    verdict = quality.classify(_pdf_evidence(ENGLISH * 7, pages))
    assert verdict.status == STATUS_EXTRACTED
    assert "partial_text_coverage" in verdict.warnings


def test_mojibake_is_suspicious():
    text = ENGLISH + "�" * 40
    verdict = quality.classify(_pdf_evidence(text, [text]))
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "replacement_characters"


def test_control_character_soup_is_suspicious():
    text = ENGLISH + "\x00\x01\x02\x03\x04\x05\x06\x07" * 6
    verdict = quality.classify(_pdf_evidence(text, [text]))
    assert verdict.status == STATUS_SUSPICIOUS


def test_an_image_needs_ocr_and_is_never_failed():
    verdict = quality.classify(
        Evidence(family="image", parsed=False, error=None,
                 text_metrics=None, pages=None, sheets=None)
    )
    assert verdict.status == STATUS_NEEDS_OCR
    assert verdict.reason == "image_file"


def test_legacy_office_is_unsupported():
    verdict = quality.classify(
        Evidence(family="office_legacy", parsed=False, error=None,
                 text_metrics=None, pages=None, sheets=None)
    )
    assert verdict.status == STATUS_UNSUPPORTED
    assert verdict.reason == "no_native_parser"


def test_a_parser_error_is_failed():
    verdict = quality.classify(
        Evidence(family="pdf", parsed=False, error="PdfReadError",
                 text_metrics=None, pages=None, sheets=None)
    )
    assert verdict.status == STATUS_FAILED


def test_a_populated_spreadsheet_is_extracted():
    verdict = quality.classify(
        Evidence(family="spreadsheet", parsed=True, error=None,
                 text_metrics=quality.measure_text("a b c"),
                 pages=None,
                 sheets=SheetStats(sheet_count=2, row_count=400,
                                   non_empty_cells=1800, populated_ratio=0.75))
    )
    assert verdict.status == STATUS_EXTRACTED


def test_an_empty_spreadsheet_is_suspicious():
    verdict = quality.classify(
        Evidence(family="spreadsheet", parsed=True, error=None,
                 text_metrics=quality.measure_text(""),
                 pages=None,
                 sheets=SheetStats(sheet_count=1, row_count=0,
                                   non_empty_cells=0, populated_ratio=0.0))
    )
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "empty_spreadsheet"


def test_a_spreadsheet_is_never_judged_on_devanagari_ratio():
    # Numeric statistical tables have no letters at all. That is normal for a
    # spreadsheet and must not read as legacy-font output.
    verdict = quality.classify(
        Evidence(family="spreadsheet", parsed=True, error=None,
                 text_metrics=quality.measure_text("1,204 3,880 91.2 44.0 " * 60),
                 pages=None,
                 sheets=SheetStats(sheet_count=1, row_count=240,
                                   non_empty_cells=960, populated_ratio=0.9))
    )
    assert verdict.status == STATUS_EXTRACTED


def test_a_mixed_english_annex_over_a_legacy_nepali_note_is_still_suspicious():
    """The case a document-level average gets wrong, and gets wrong dangerously.

    Real NRB circulars publish a Preeti-encoded Nepali directive followed by a
    long English annex (an audit scope, a Basel capital table). Whole-document
    statistics come out looking like English — one such file measured a
    `stopword_rate` of 0.248, HIGHER than real English prose — so the operative
    Nepali instruction, which is the part that is unreadable, would have been
    indexed as if it were fine. Seven of the 49 fetched circulars were this shape.
    """
    document = "\n".join([LEGACY] * 2 + [ENGLISH] * 6)
    metrics = quality.measure_text(document)
    # Document-level signals genuinely read as English here — that is the trap.
    assert metrics.stopword_rate > 0.10
    assert metrics.vowelless_token_ratio < 0.30
    # The per-line measurement still sees the unreadable portion.
    assert metrics.legacy_line_ratio > quality.LEGACY_LINE_RATIO
    assert quality.classify(
        _pdf_evidence(document, [document])
    ).reason == "legacy_font_suspected"


def test_stopword_rate_is_reported_but_is_no_longer_a_gate():
    """It is kept as a metric and removed as a veto.

    A stopword gate is what the seven mixed documents escaped through: glyph-
    mapped text is full of one- and two-character ASCII tokens (`a`, `t`, `is`,
    `on`) that match short stopwords by chance.
    """
    removed_gate = 0.02   # the old rule required stopword_rate < this to proceed
    document = "\n".join([LEGACY] * 3 + [ENGLISH] * 2)
    metrics = quality.measure_text(document)
    assert metrics.stopword_rate > removed_gate    # the old gate would have vetoed
    assert quality.looks_like_legacy_font(metrics) is True


def test_a_line_too_short_to_judge_is_excluded_from_both_sides_of_the_ratio():
    # Counting a two-word heading as clean would dilute exactly the documents
    # that are worst.
    assert quality.legacy_line_ratio("NRB\nBank\nnote") == 0.0
    mixed = "ok\n" + LEGACY
    assert quality.legacy_line_ratio(mixed) == quality.legacy_line_ratio(LEGACY)


def test_unicode_devanagari_lines_are_never_counted_as_glyph_mapped():
    assert quality.legacy_line_ratio(NEPALI * 4) == 0.0
    assert quality.measure_text(NEPALI * 4).legacy_line_ratio == 0.0


def test_a_digit_heavy_pdf_table_is_not_called_legacy_font():
    # The legacy gate requires latin_letter_ratio > 0.35 for exactly this reason.
    text = "1,204 3,880 91.2 44.0 12,556 8.7 " * 40
    verdict = quality.classify(_pdf_evidence(text, [text] * 3))
    assert verdict.reason != "legacy_font_suspected"


def test_short_text_is_never_called_legacy_font():
    verdict = quality.classify(_pdf_evidence("k|fKt ljQLo", ["k|fKt ljQLo"]))
    assert verdict.reason != "legacy_font_suspected"
    assert "insufficient_text" in verdict.warnings


def test_every_verdict_status_is_in_the_closed_vocabulary():
    for ev in [
        _pdf_evidence(ENGLISH * 4, [ENGLISH] * 4),
        _pdf_evidence(LEGACY * 6, [LEGACY] * 6),
        _pdf_evidence("", ["", ""]),
        Evidence("image", False, None, None, None, None),
        Evidence("unknown", False, "boom", None, None, None),
    ]:
        assert quality.classify(ev).status in quality.STATUSES


def test_every_verdict_reason_is_in_the_closed_vocabulary():
    for ev in [
        _pdf_evidence(ENGLISH * 4, [ENGLISH] * 4),
        _pdf_evidence(LEGACY * 6, [LEGACY] * 6),
        _pdf_evidence("", ["", ""]),
        _pdf_evidence("1 2 3 4", ["1", "2", "3", "4"]),
        Evidence("image", False, None, None, None, None),
        Evidence("office_legacy", False, None, None, None, None),
        Evidence("unknown", False, "boom", None, None, None),
    ]:
        assert quality.classify(ev).reason in quality.REASONS


def test_classify_is_deterministic():
    ev = _pdf_evidence(LEGACY * 6, [LEGACY] * 6)
    assert quality.classify(ev) == quality.classify(ev)

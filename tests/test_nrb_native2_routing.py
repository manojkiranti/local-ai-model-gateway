"""native-2: unit judgment, the English/table guard, spreadsheets, minority regions.

Pure and offline. No database, no blob reads, no network — and, asserted
explicitly, **no converter**: native-2 must classify correctly on a machine where
npttf2utf was never installed, because a classifier that needs a GPL-3 dependency
to decide what is suspicious is not a classifier we can ship.

Fixtures are text SHAPES, never document identities. §7 of the brief is explicit
about this and it matters: a test that special-cased `05fa82badf94` would pass
while the rule underneath it was still broken for every other English table in
the corpus.
"""

import pytest

from app.nrb import extraction, quality, routing, units

# --------------------------------------------------------------------------- #
# Fixtures — real shapes from the frozen benchmark.
# --------------------------------------------------------------------------- #

# `05fa82badf94`, the hand-reviewed English-table false positive. Every line here
# is verbatim from it, including the three that used to trip the detector.
ENGLISH_TABLE = "\n".join([
    "Monetary Operations in (F/Y 2081/82)",
    "Instruments Times Offer Amount",
    "(Rs. in crore)",
    "i. Reverse Repo -                                   -",
    "iii. Outright Sale -                    -",
    "iv. NRB Bond -                    -                    -",
    "ii. SLF -                    -                            -",
    "General Instruments -                 -                        -",
    "Under IRC 1                120.00                  120.00              -",
    "TOTAL -                 -                        -",
    "(Updated on 13 July 2025)",
])

# `7e2257c289d2` — the FIU newsletter, whose hyphenated compounds were the last
# cause of a false flag.
ENGLISH_COMPOUNDS = "\n".join([
    "Issue II                  FIU-Nepal Newsletter                  October, 2022",
    "Policy & Planning Division (FIU-Nepal)",
    "Plan 2022 on AML/CFT.",
    "Head Assistants: 5 and Assistants (IT): 2 in the division",
])

ENGLISH_PROSE = (
    "Nepal Rastra Bank issued a circular to all licensed institutions today. "
    "The circular requires that every bank shall report its exposure to the "
    "central bank within thirty days of the end of the quarter."
)

NUMERIC_TABLE = "\n".join([
    "2,123,180.00   1,500.00   3.25   0.00",
    "4,200.00   300.00   8,000.00   12.50",
    "- - - -",
    "1,00,000   50,000   25,000   10,000",
])

# `041902065a1d` / `5e0ca4500f8f` — verbatim Preeti.
PREETI = "\n".join([
    "g]kfn /fi6« a}+ssf] k|of]hgsf] nflu dfq",
    ";~rfns ;ldltn] b]xfosf ljlgodx? agfPsf] 5 .",
    ";+:yfnfO{ xhf{gf nufpg] ljifodf lg0f{o ug]{,",
    "pQm a}+snfO{ @)&).%.@ b]lv zL3| ;'wf/fTds sf/afxL ;DaGwL",
    "Jofkfl/s e|d0f / :6n ef8f cflb",
])

UNICODE_NEPALI = "\n".join([
    "नेपाल राष्ट्र बैंकले सम्पूर्ण इजाजतपत्रप्राप्त संस्थाहरूलाई परिपत्र जारी गरेको छ।",
    "प्रत्येक बैंकले आफ्नो जोखिम विवरण तीस दिनभित्र केन्द्रीय बैंकमा पेश गर्नुपर्नेछ।",
    "सम्पत्ति शुद्धीकरण निवारण ऐन, २०६४ र सो अन्तर्गत बनेका नियमावलीहरुको",
])


def _assess(text):
    return [units.assess_unit(u) for u in units.units_from_text(text)]


def _profile(text):
    return units.profile_units(_assess(text))


# --------------------------------------------------------------------------- #
# 1. Three-state unit judgment.
# --------------------------------------------------------------------------- #

def test_the_three_states_are_a_closed_vocabulary():
    assert set(units.STATES) == {
        units.STATE_LEGACY, units.STATE_TRUSTED, units.STATE_UNJUDGED
    }
    for text in (ENGLISH_PROSE, PREETI, UNICODE_NEPALI, "", "   ", "1,234.00"):
        for a in _assess(text):
            assert a.state in units.STATES
            assert a.kind in units.KINDS


def test_uninformative_units_are_unjudged_not_clean():
    """The dilution bug: counting a blank line or a numeric row as evidence of
    cleanliness is how a legacy minority disappears under a threshold."""
    for text, kind in (
        ("", units.KIND_EMPTY),
        ("    ", units.KIND_EMPTY),
        ("Page 3", units.KIND_TOO_SHORT),
        ("1,234.00  5,678.90  3.25  0.00", units.KIND_NUMERIC),
    ):
        a = units.assess_unit(text)
        assert a.state == units.STATE_UNJUDGED, text
        assert a.kind == kind, text


def test_unjudged_units_are_in_neither_half_of_the_ratio():
    profile = _profile("\n\n" + PREETI + "\n\n1,234.00 5,678.90 3.25 0.00\n\n")
    assert profile.legacy == 5
    assert profile.judged == 5            # the blanks and the numeric row are out
    assert profile.unjudged >= 3
    assert profile.legacy_unit_ratio == 1.0


# --------------------------------------------------------------------------- #
# 2. The English / table guard — input-side, orthographic, not stopwords.
# --------------------------------------------------------------------------- #

def test_english_prose_is_positively_identified_and_stays_clean():
    profile = _profile(ENGLISH_PROSE)
    assert profile.legacy == 0
    assert profile.english_units >= 1


def test_english_statistical_tables_with_formatted_numbers_stay_clean():
    """Defect 1, as a shape. `2,123,180.00` is not a glyph-mapped word."""
    profile = _profile(ENGLISH_TABLE)
    assert profile.legacy == 0, [a.kind for a in _assess(ENGLISH_TABLE)]
    assert profile.legacy_unit_ratio == 0.0


def test_hyphenated_and_slashed_english_compounds_stay_clean():
    profile = _profile(ENGLISH_COMPOUNDS)
    assert profile.legacy == 0


def test_acronyms_are_not_treated_as_vowelless_words():
    """`NRB`, `SLF`, `IRC` have no vowels because that is what an acronym is."""
    for line in ("iv. NRB Bond -                    -                    -",
                 "ii. SLF -                    -                            -",
                 "Under IRC 1                120.00                  120.00"):
        assert not units.assess_unit(line).is_legacy, line


def test_a_numeric_table_is_unjudged_rather_than_trusted():
    profile = _profile(NUMERIC_TABLE)
    assert profile.legacy == 0
    assert profile.numeric_units >= 3
    assert profile.judged == 0            # it says nothing either way


def test_arbitrary_latin_garbage_is_not_exempted_just_for_having_letters():
    """Defect D of the brief: the guard is a POSITIVE identification of English,
    not a free pass for anything in the latin script."""
    garbage = "\n".join([
        "xk qz wm vbn jkl mnp qrs tvw xyz bcd",
        "zxc vbn mqw ert yui opa sdf ghj klz",
        "pqr stv wxy zab cde fgh ijk lmn opq",
    ])
    profile = _profile(garbage)
    assert profile.english_units == 0
    assert profile.legacy >= 1


def test_a_few_accidental_english_stopwords_do_not_exempt_preeti():
    """Phase 6A's measured trap: glyph-mapped text is full of short ASCII tokens
    that match English stopwords by chance — one real circular scored a HIGHER
    stopword rate than English prose. The guard must not be re-buildable from
    stopwords, so salting Preeti with them changes nothing."""
    salted = "\n".join(
        f"{line} is on to a" for line in PREETI.splitlines()
    )
    profile = _profile(salted)
    assert profile.legacy == len(PREETI.splitlines())
    assert profile.english_units == 0


def test_genuine_preeti_is_still_legacy_after_every_guard():
    profile = _profile(PREETI)
    assert profile.legacy == 5
    assert profile.legacy_unit_ratio == 1.0


# --------------------------------------------------------------------------- #
# 3. Genuine Unicode protection.
# --------------------------------------------------------------------------- #

def test_unicode_devanagari_is_trusted_and_never_legacy():
    profile = _profile(UNICODE_NEPALI)
    assert profile.legacy == 0
    assert profile.unicode_units == 3


def test_pure_unicode_is_distinguishable_from_mixed_unicode_plus_preeti():
    pure = _profile(UNICODE_NEPALI)
    mixed = _profile(UNICODE_NEPALI + "\n" + PREETI)
    assert pure.legacy == 0 and not routing.minority_legacy_detected(pure)
    assert mixed.legacy == 5
    assert mixed.unicode_units == 3


# --------------------------------------------------------------------------- #
# 4. Minority legacy regions — Defect 3.
# --------------------------------------------------------------------------- #

def _mixed_document(legacy_lines: int) -> str:
    """A Unicode-majority document with a contiguous Preeti region, the shape of
    `84862ab6866a`."""
    body = [UNICODE_NEPALI] * 40
    region = [PREETI.splitlines()[i % 5] for i in range(legacy_lines)]
    return "\n".join(body + region + body)


def test_a_meaningful_legacy_region_is_detected_despite_a_low_global_ratio():
    text = _mixed_document(29)
    profile = _profile(text)
    # The global measure native-1 uses stays innocent…
    assert quality.measure_text(text).legacy_line_ratio < quality.LEGACY_LINE_RATIO
    # …and the region signal sees it anyway.
    assert routing.minority_legacy_detected(profile)
    assert profile.max_legacy_run >= routing.MINORITY_MIN_RUN
    assert profile.contested_legacy_ratio >= routing.MINORITY_MIN_CONTESTED_RATIO


def test_the_global_threshold_was_not_lowered_to_achieve_that():
    """The rule the brief forbids. 0.20 still means 0.20."""
    assert quality.LEGACY_LINE_RATIO == 0.20
    assert routing._legacy_by_units(_profile(_mixed_document(29))) is False


def test_a_couple_of_stray_lines_do_not_trigger_a_minority_region():
    assert not routing.minority_legacy_detected(_profile(_mixed_document(2)))


def test_scattered_punctuation_does_not_trigger_a_minority_region():
    text = "\n".join([UNICODE_NEPALI, "*** --- ***", "...", "|||", ENGLISH_PROSE])
    assert not routing.minority_legacy_detected(_profile(text))


def test_pure_english_never_triggers_a_minority_region():
    assert not routing.minority_legacy_detected(_profile(ENGLISH_TABLE))
    assert not routing.minority_legacy_detected(_profile(ENGLISH_COMPOUNDS))


def test_unjudged_units_do_not_break_a_legacy_run():
    """A blank line or a page number sits inside a real legacy region constantly;
    letting it chop the run into fragments would hide the region."""
    interrupted = "\n".join([
        PREETI.splitlines()[0], "", PREETI.splitlines()[1], "Page 4",
        PREETI.splitlines()[2],
    ])
    assert _profile(interrupted).max_legacy_run == 3


def test_a_positively_clean_unit_does_break_a_legacy_run():
    interrupted = "\n".join([
        PREETI.splitlines()[0], ENGLISH_PROSE, PREETI.splitlines()[1],
    ])
    assert _profile(interrupted).max_legacy_run == 1


# --------------------------------------------------------------------------- #
# 5. Spreadsheets — Defect 2.
# --------------------------------------------------------------------------- #

def test_cells_are_the_unit_and_the_row_separator_never_participates():
    rows = [("g]kfn /fi6« a}+s", "Amount"), ("s'n shf{ ljt/0f -?= xhf/df_", "1,234.00")]
    cells = units.cells_from_rows(rows)
    assert cells == ("g]kfn /fi6« a}+s", "Amount",
                     "s'n shf{ ljt/0f -?= xhf/df_", "1,234.00")
    assert not any(" | " in c for c in cells)


def test_the_rendered_row_is_what_we_must_not_judge():
    """`|` is a Preeti codepoint. Scoring the joined row scores the separator."""
    rows = [("g]kfn /fi6« a}+s", "Amount")]
    rendered = " | ".join(rows[0])
    assert "|" in rendered
    assert "|" not in "".join(units.cells_from_rows(rows))


def _sheet_evidence(cells, *, non_empty=None):
    text = "\n".join(cells)
    base = quality.Evidence(
        family="spreadsheet", parsed=True, error=None,
        text_metrics=quality.measure_text(text), pages=None,
        sheets=quality.SheetStats(
            sheet_count=1, row_count=len(cells),
            non_empty_cells=len(cells) if non_empty is None else non_empty,
            populated_ratio=1.0,
        ),
    )
    return routing.RoutingEvidence.build(base, cells)


def test_an_english_spreadsheet_is_clean():
    cells = ["Instruments", "Times", "Offer Amount", "Reverse Repo",
             "Total outstanding balance of the quarter", "Rs. in crore"]
    verdict = routing.classify_v2(_sheet_evidence(cells))
    assert (verdict.status, verdict.reason) == ("extracted", "clean")


def test_a_numeric_spreadsheet_is_clean():
    cells = ["1,234.00", "5,678.90", "3.25", "0.00", "12,000", "-"]
    verdict = routing.classify_v2(_sheet_evidence(cells))
    assert (verdict.status, verdict.reason) == ("extracted", "clean")


def test_preeti_cells_make_a_structurally_valid_workbook_suspicious():
    """The Phase 6A false negative: structural validity is not linguistic trust."""
    cells = [
        "sfo{If]q ePsf] lhNnf ;+Vof", ";]jf k'u]sf] lhNnf ;++Vof",
        "s'n shf{ ljt/0f -?= xhf/df_", "n3' Joj;fo shf{ -?= xhf/df_",
        "cGo shf{ -?= xhf/df_", "shf{sf] ;fFjf c;'nL -?= xhf/df_",
    ]
    verdict = routing.classify_v2(_sheet_evidence(cells))
    assert (verdict.status, verdict.reason) == ("suspicious", "legacy_font_suspected")


def test_a_mixed_english_and_preeti_workbook_is_detected():
    cells = [
        "District", "Amount", "1,234.00", "5,678.00",
        "sfo{If]q ePsf] lhNnf ;+Vof", ";]jf k'u]sf] lhNnf ;++Vof",
        "s'n shf{ ljt/0f -?= xhf/df_", "n3' Joj;fo shf{ -?= xhf/df_",
    ]
    profile = _sheet_evidence(cells).profile
    assert profile.legacy == 4
    verdict = routing.classify_v2(_sheet_evidence(cells))
    assert verdict.reason == "legacy_font_suspected"


def test_an_empty_workbook_is_still_reported_structurally():
    verdict = routing.classify_v2(_sheet_evidence(["", ""], non_empty=0))
    assert (verdict.status, verdict.reason) == ("suspicious", "empty_spreadsheet")


def test_structure_no_longer_short_circuits_the_linguistic_rules():
    """native-1 returns on `non_empty_cells > 0`; native-2 must not."""
    cells = ["s'n shf{ ljt/0f -?= xhf/df_"] * 12
    base = _sheet_evidence(cells).base
    assert quality.classify(base).reason == "clean"                 # native-1
    assert routing.classify_v2(_sheet_evidence(cells)).reason == \
        "legacy_font_suspected"                                     # native-2


# --------------------------------------------------------------------------- #
# 6. The classifier, and what it kept.
# --------------------------------------------------------------------------- #

def _text_evidence(text, **kw):
    base = quality.Evidence(
        family=kw.pop("family", "text"), parsed=True, error=None,
        text_metrics=quality.measure_text(text), pages=kw.pop("pages", None),
        sheets=None,
    )
    return routing.RoutingEvidence.build(base, units.units_from_text(text))


def test_the_reason_vocabulary_is_unchanged():
    """No `preeti` reason: native-2 detects a legacy CANDIDATE and never claims
    to know the font mapping."""
    verdict = routing.classify_v2(_text_evidence(PREETI * 10))
    assert verdict.reason in quality.REASONS
    assert verdict.reason == "legacy_font_suspected"
    assert "preeti" not in " ".join(quality.REASONS)


def test_non_legacy_rules_are_carried_over_unchanged():
    for family, expected in (("image", ("needs_ocr", "image_file")),
                             ("office_legacy", ("unsupported", "no_native_parser")),
                             ("archive", ("unsupported", "no_native_parser"))):
        base = quality.Evidence(family, False, None, None, None, None)
        verdict = routing.classify_v2(routing.RoutingEvidence.build(base, []))
        assert (verdict.status, verdict.reason) == expected


def test_a_parser_error_is_still_failed():
    base = quality.Evidence("pdf", False, "PdfReadError: boom", None, None, None)
    verdict = routing.classify_v2(routing.RoutingEvidence.build(base, []))
    assert (verdict.status, verdict.reason) == ("failed", "parser_error")


def test_a_scan_still_needs_ocr_and_is_never_called_legacy():
    base = quality.Evidence(
        "pdf", True, None, quality.measure_text(""),
        quality.measure_pages(["", "", "", ""]), None,
    )
    verdict = routing.classify_v2(routing.RoutingEvidence.build(base, []))
    assert (verdict.status, verdict.reason) == ("needs_ocr", "no_text_layer")


def test_a_minority_region_verdict_names_itself_in_the_warnings():
    verdict = routing.classify_v2(_text_evidence(_mixed_document(29)))
    assert verdict.reason == "legacy_font_suspected"
    assert "minority_legacy_region" in verdict.warnings


# --------------------------------------------------------------------------- #
# 7. Metrics.
# --------------------------------------------------------------------------- #

def test_native2_keeps_every_native1_metric_and_adds_its_own(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text(PREETI * 20, encoding="utf-8")
    v1 = extraction.extract_file(path, family="text", extension="txt")
    v2 = extraction.extract_file(
        path, family="text", extension="txt", extractor_version="native-2"
    )
    for key in ("legacy_line_ratio", "legacy_lines", "judged_lines",
                "devanagari_ratio", "char_count", "token_count",
                "stopword_rate", "printable_ratio"):
        assert key in v1.metrics and key in v2.metrics, key
        assert v1.metrics[key] == v2.metrics[key], key   # native-1 semantics kept
    for key in ("unit_total", "unit_judged", "unit_legacy_candidates",
                "unit_trusted", "unit_unjudged", "unit_english", "unit_unicode",
                "unit_numeric", "unit_legacy_ratio",
                "unit_contested_legacy_ratio", "unit_max_legacy_run",
                "minority_legacy_detected"):
        assert key not in v1.metrics, key
        assert key in v2.metrics, key


def test_the_historic_legacy_line_ratio_is_still_measurable_under_native2():
    """§20: native-1 results must not be retrospectively redefined."""
    text = PREETI * 20
    v2 = routing.RoutingEvidence.build(
        quality.Evidence("text", True, None, quality.measure_text(text), None, None),
        units.units_from_text(text),
    )
    assert v2.base.text_metrics.legacy_line_ratio == \
        quality.measure_text(text).legacy_line_ratio


# --------------------------------------------------------------------------- #
# 8. Version isolation, and no converter.
# --------------------------------------------------------------------------- #

def test_native1_is_the_default_and_is_byte_for_byte_unchanged(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text(ENGLISH_TABLE, encoding="utf-8")
    default = extraction.extract_file(path, family="text", extension="txt")
    explicit = extraction.extract_file(
        path, family="text", extension="txt", extractor_version="native-1"
    )
    assert extraction.EXTRACTOR_VERSION == "native-1"
    assert default.metrics == explicit.metrics
    assert (default.status, default.reason) == (explicit.status, explicit.reason)


def test_the_two_versions_disagree_on_the_english_table_which_is_the_point(tmp_path):
    path = tmp_path / "table.txt"
    path.write_text(ENGLISH_TABLE * 6, encoding="utf-8")
    v1 = extraction.extract_file(path, family="text", extension="txt")
    v2 = extraction.extract_file(
        path, family="text", extension="txt", extractor_version="native-2"
    )
    assert v1.reason == "legacy_font_suspected"
    assert v2.reason == "clean"


def test_both_versions_still_flag_real_preeti(tmp_path):
    path = tmp_path / "preeti.txt"
    path.write_text(PREETI * 20, encoding="utf-8")
    for version in ("native-1", "native-2"):
        result = extraction.extract_file(
            path, family="text", extension="txt", extractor_version=version
        )
        assert result.reason == "legacy_font_suspected", version


def test_no_legacy_converter_is_invoked_during_classification(monkeypatch, tmp_path):
    """native-2 must stand up without npttf2utf. The dependency is GPL-3 and
    excluded from the API image; a classifier that needed it would drag the
    licence gate into every deployment."""
    import app.nrb.legacy_font as legacy_font

    def _boom(*args, **kwargs):
        raise AssertionError("native-2 invoked the legacy converter")

    monkeypatch.setattr(legacy_font, "_font_mapper", _boom)
    monkeypatch.setattr(legacy_font, "converter_for", _boom)
    monkeypatch.setattr(legacy_font, "converters", _boom)
    monkeypatch.setattr(legacy_font.Npttf2UtfConverter, "convert", _boom)

    path = tmp_path / "preeti.txt"
    path.write_text(PREETI * 20, encoding="utf-8")
    result = extraction.extract_file(
        path, family="text", extension="txt", extractor_version="native-2"
    )
    assert result.reason == "legacy_font_suspected"


def test_routing_does_not_import_the_converter_module():
    """A stronger statement than 'it was not called': native-2's classifier holds
    no reference to the converter in code at all.

    Checks executable lines only — both modules discuss the converter at length in
    their docstrings, which is the point of them.
    """
    import ast
    import inspect

    for module in (routing, units):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "legacy_font" not in imported, module.__name__
        assert "npttf2utf" not in imported, module.__name__
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"legacy_font", "npttf2utf"}, module.__name__


def test_classification_is_deterministic():
    first = routing.classify_v2(_text_evidence(PREETI * 5))
    second = routing.classify_v2(_text_evidence(PREETI * 5))
    assert (first.status, first.reason, first.warnings) == \
        (second.status, second.reason, second.warnings)


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", "1", "|", " | | "])
def test_degenerate_input_never_raises(text):
    profile = _profile(text)
    assert profile.units >= 0
    routing.classify_v2(_text_evidence(text))


# --------------------------------------------------------------------------- #
# 9. The small-denominator floor — a defect native-2 introduced and the
#    benchmark caught.
# --------------------------------------------------------------------------- #

def test_one_legacy_unit_among_three_is_not_a_document_verdict():
    """Marking uninformative units `unjudged` shrinks the denominator, and on the
    benchmark that flagged six documents on the strength of a SINGLE unit out of
    three or four judged. A ratio over four units is not a measurement."""
    text = "\n".join([
        "Notice of the meeting scheduled for the coming week",
        "All licensed institutions are requested to attend it",
        "1,234.00   5,678.00   9,012.00   3,456.00",
        "k|fKt ljj/0f cg';f/ ;+:yfsf] gfd",
    ])
    profile = _profile(text)
    assert profile.legacy == 1
    assert profile.judged < routing.MIN_JUDGED_FOR_RATIO
    assert profile.legacy_unit_ratio > quality.LEGACY_LINE_RATIO   # the ratio bites…
    assert routing._legacy_by_units(profile) is False              # …the floor holds


def test_a_short_but_wholly_preeti_document_still_flags():
    """The escape hatch. `0503f02d7d8c` is 15 judged units, every one legacy — a
    floor with no absolute override would lose it."""
    text = "\n".join(PREETI.splitlines()[:4])
    profile = _profile(text)
    assert profile.judged < routing.MIN_JUDGED_FOR_RATIO
    assert profile.legacy >= routing.MIN_LEGACY_ABSOLUTE
    assert routing._legacy_by_units(profile) is True


def test_the_floor_does_not_rescue_a_long_english_document():
    profile = _profile(ENGLISH_TABLE)
    assert profile.legacy == 0
    assert routing._legacy_by_units(profile) is False

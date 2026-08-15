"""pypdf vs Docling: the comparison model and the deterministic calibration report.

Fake parser sides throughout — the point of a parser-neutral comparison is that
neither engine's internals reach the model, so these tests need no Docling, no
torch and no model download. The real-engine smoke tests live in
`tests/test_nrb_extraction.py` behind an `importorskip`.

What is being pinned here: that a rescue means something specific, that both
engines are judged by the SAME classifier at the SAME threshold, and that the
report does not change when the comparisons arrive in a different order.
"""

from __future__ import annotations

import random

import pytest

from app.nrb import calibrate, extraction, quality, report

SHA = "{:064x}"


def _side(engine="pypdf", *, status=quality.STATUS_EXTRACTED, reason="clean", **over):
    fields = dict(
        engine=engine,
        parser=engine,
        status=status,
        reason=reason,
        warnings=(),
        char_count=3600,
        devanagari_ratio=0.61,
        legacy_line_ratio=0.0,
        text_page_coverage=1.0,
        page_count=4,
        pages_with_text=4,
        median_chars_per_text_page=900.0,
        duration_ms=120,
        preview="Nepal Rastra Bank circular",
        error=None,
        metrics={"char_count": 3600},
    )
    fields.update(over)
    return calibrate.ParserSide(**fields)


def _comparison(native=None, docling=None, *, sha=1, keys=("a",)):
    return calibrate.BlobComparison(
        content_sha256=SHA.format(sha),
        comparison_keys=tuple(keys),
        native=native or _side("pypdf"),
        docling=docling or _side("docling"),
    )


def _result(comparisons, **over):
    fields = dict(
        status="completed",
        dry_run=False,
        subset_path="docs/nrb/phase6a-docling-calibration.json",
        subset_selection_sha256="s" * 64,
        parent_selection_sha256="p" * 64,
        counters={"subset_entries": 40, "blobs_selected": len(comparisons),
                  "comparisons_run": len(comparisons)},
        cohort={"source": {"requested": 40, "fetched": len(comparisons)},
                "blob": {"unique_fetched": len(comparisons)}},
        comparisons=tuple(comparisons),
    )
    fields.update(over)
    return calibrate.CalibrationResult(**fields)


# --------------------------------------------------------------------------- #
# A/D/E. Agreement
# --------------------------------------------------------------------------- #
def test_the_same_text_through_both_engines_agrees():
    comparison = _comparison()
    assert comparison.status_agreement
    assert comparison.reason_agreement
    assert comparison.category == "both_extracted"


def test_both_suspicious_is_its_own_category():
    suspicious = dict(status=quality.STATUS_SUSPICIOUS, reason="legacy_font_suspected")
    comparison = _comparison(
        _side("pypdf", **suspicious), _side("docling", **suspicious)
    )
    assert comparison.status_agreement
    assert comparison.category == "both_suspicious"


def test_both_failed_is_its_own_category():
    failed = dict(status=quality.STATUS_FAILED, reason="parser_error",
                  error="ValueError")
    assert _comparison(_side("pypdf", **failed),
                       _side("docling", **failed)).category == "both_failed"


def test_agreeing_on_a_status_with_different_reasons_is_not_reason_agreement():
    comparison = _comparison(
        _side("pypdf", status=quality.STATUS_NEEDS_OCR, reason="no_text_layer"),
        _side("docling", status=quality.STATUS_NEEDS_OCR, reason="sparse_text_layer"),
    )
    assert comparison.status_agreement
    assert not comparison.reason_agreement


# --------------------------------------------------------------------------- #
# B/C. Rescues — the asymmetric cases the whole calibration exists for
# --------------------------------------------------------------------------- #
def test_docling_extracting_what_pypdf_called_suspicious_is_a_docling_rescue():
    comparison = _comparison(
        _side("pypdf", status=quality.STATUS_SUSPICIOUS,
              reason="legacy_font_suspected"),
        _side("docling"),
    )
    assert comparison.category == "docling_rescued_pypdf"
    assert not comparison.status_agreement


def test_pypdf_extracting_what_docling_called_suspicious_is_a_pypdf_rescue():
    comparison = _comparison(
        _side("pypdf"),
        _side("docling", status=quality.STATUS_SUSPICIOUS,
              reason="legacy_font_suspected"),
    )
    assert comparison.category == "pypdf_rescued_docling"


def test_docling_extracting_what_pypdf_could_not_read_at_all_is_a_rescue():
    """needs_ocr is not `extracted`, so it is rescuable — this is the case that
    would invalidate the pypdf screen."""
    comparison = _comparison(
        _side("pypdf", status=quality.STATUS_NEEDS_OCR, reason="no_text_layer"),
        _side("docling"),
    )
    assert comparison.category == "docling_rescued_pypdf"


def test_a_rescue_needs_the_other_side_to_be_usable_not_merely_different():
    """suspicious vs needs_ocr is a disagreement, not a rescue: neither engine
    produced text anyone should index."""
    comparison = _comparison(
        _side("pypdf", status=quality.STATUS_SUSPICIOUS,
              reason="legacy_font_suspected"),
        _side("docling", status=quality.STATUS_NEEDS_OCR, reason="no_text_layer"),
    )
    assert comparison.category == "disagreed_neither_usable"


def test_agreement_on_a_status_that_is_neither_extracted_nor_suspicious():
    comparison = _comparison(
        _side("pypdf", status=quality.STATUS_NEEDS_OCR, reason="no_text_layer"),
        _side("docling", status=quality.STATUS_NEEDS_OCR, reason="no_text_layer"),
    )
    assert comparison.category == "agreed_other"


def test_every_category_the_model_can_produce_is_declared():
    """A category the report has no column for would silently vanish from it."""
    statuses = quality.STATUSES
    produced = {
        _comparison(_side("pypdf", status=a), _side("docling", status=b)).category
        for a in statuses
        for b in statuses
    }
    assert produced <= set(calibrate.CATEGORIES)


# --------------------------------------------------------------------------- #
# F. One engine failing does not erase the other's measurement
# --------------------------------------------------------------------------- #
def test_a_docling_failure_leaves_the_pypdf_side_intact():
    comparison = _comparison(
        _side("pypdf"),
        _side("docling", status=quality.STATUS_FAILED, reason="parser_error",
              error="RuntimeError", char_count=0, preview=""),
    )
    assert comparison.native.char_count == 3600
    assert comparison.docling.error == "RuntimeError"
    assert comparison.category == "pypdf_rescued_docling"


# --------------------------------------------------------------------------- #
# G/H. One classifier, one threshold, both engines
# --------------------------------------------------------------------------- #
LEGACY_PAGE = "\n".join(
    ["k|fKt ug{ ljQLo ;+:yfx? ;Da4 4{i-4;f ug'{ kg]{ ljifodf ;"] * 12
)
ENGLISH_PAGE = "\n".join(
    ["Nepal Rastra Bank shall require that every licensed bank reports it."] * 12
)


@pytest.mark.parametrize("pages", [[ENGLISH_PAGE] * 3, [LEGACY_PAGE] * 3, ["", "", ""]])
def test_both_engines_are_scored_by_the_same_classifier(pages):
    """The comparison is only meaningful if a disagreement comes from what the
    engines READ, never from how their output was judged."""
    native = extraction.result_from_pages(pages, parser="pypdf")
    docling = extraction.result_from_pages(pages, parser="docling")
    assert (native.status, native.reason) == (docling.status, docling.reason)
    assert native.metrics == docling.metrics
    assert native.parser == "pypdf" and docling.parser == "docling"


def test_the_legacy_font_threshold_is_the_classifiers_own_and_is_still_0_20():
    assert quality.LEGACY_LINE_RATIO == 0.20
    assert report.LEGACY_BANDS[2][1] == quality.LEGACY_LINE_RATIO


def test_a_legacy_font_page_is_suspicious_through_the_shared_path():
    result = extraction.result_from_pages([LEGACY_PAGE] * 3, parser="docling")
    assert result.status == quality.STATUS_SUSPICIOUS
    assert result.reason == "legacy_font_suspected"


# --------------------------------------------------------------------------- #
# I. Bounded preview
# --------------------------------------------------------------------------- #
def test_the_preview_stays_within_the_database_contract():
    result = extraction.result_from_pages(["word " * 5000], parser="docling")
    assert len(result.preview) <= extraction.PREVIEW_CHARS
    assert result.char_count > extraction.PREVIEW_CHARS


def test_the_report_never_widens_a_preview():
    long_preview = "x" * extraction.PREVIEW_CHARS
    summary = report.summarize_calibration(
        _result([
            _comparison(_side("pypdf", preview=long_preview,
                              status=quality.STATUS_SUSPICIOUS,
                              reason="legacy_font_suspected"),
                        _side("docling", preview=long_preview)),
        ])
    )
    for row in summary["disagreements"]:
        for engine in ("pypdf", "docling"):
            assert len(row[engine]["preview"]) <= report.PREVIEW_CHARS


# --------------------------------------------------------------------------- #
# K. The report is deterministic
# --------------------------------------------------------------------------- #
def _mixed_comparisons():
    return [
        _comparison(sha=1, keys=("a",)),
        _comparison(_side("pypdf", status=quality.STATUS_SUSPICIOUS,
                          reason="legacy_font_suspected", legacy_line_ratio=0.94,
                          char_count=900, devanagari_ratio=0.0),
                    _side("docling", char_count=4100, devanagari_ratio=0.55,
                          duration_ms=9000),
                    sha=2, keys=("b", "c")),
        _comparison(_side("pypdf", status=quality.STATUS_NEEDS_OCR,
                          reason="no_text_layer", char_count=0),
                    _side("docling", status=quality.STATUS_NEEDS_OCR,
                          reason="no_text_layer", char_count=0, duration_ms=7000),
                    sha=3, keys=("d",)),
    ]


def test_the_report_does_not_depend_on_the_order_the_comparisons_arrive_in():
    comparisons = _mixed_comparisons()
    shuffled = list(comparisons)
    random.Random(3).shuffle(shuffled)
    assert report.summarize_calibration(_result(comparisons)) == \
        report.summarize_calibration(_result(shuffled))


def test_the_report_counts_agreement_over_the_comparisons_actually_run():
    summary = report.summarize_calibration(_result(_mixed_comparisons()))
    assert summary["agreement"]["compared"] == 3
    assert summary["agreement"]["status_agreed"] == 2
    assert summary["agreement"]["status_agreement_rate"] == round(2 / 3, 4)


def test_the_report_separates_the_two_rescue_directions():
    summary = report.summarize_calibration(_result(_mixed_comparisons()))
    assert summary["rescues"]["docling_rescued_pypdf"] == 1
    assert summary["rescues"]["pypdf_rescued_docling"] == 0
    assert summary["rescues"]["both_extracted"] == 1


def test_the_report_names_the_explicit_extracted_versus_suspicious_pairs():
    summary = report.summarize_calibration(_result(_mixed_comparisons()))
    assert summary["rescues"]["docling_extracted_pypdf_suspicious"] == 1
    assert summary["rescues"]["pypdf_extracted_docling_suspicious"] == 0


def test_the_report_keeps_the_two_engines_metrics_apart():
    summary = report.summarize_calibration(_result(_mixed_comparisons()))
    assert summary["pypdf"]["by_status"] != summary["docling"]["by_status"]
    assert summary["pypdf"]["total_chars"] == 3600 + 900 + 0
    assert summary["docling"]["total_chars"] == 3600 + 4100 + 0


def test_the_report_records_the_status_transitions_both_ways():
    summary = report.summarize_calibration(_result(_mixed_comparisons()))
    transitions = summary["pairwise"]["status_transitions"]
    assert transitions["suspicious->extracted"] == 1
    assert transitions["extracted->extracted"] == 1


def test_the_report_measures_the_char_count_ratio_only_where_it_means_something():
    """A blob pypdf read nothing from has no ratio — 4100/0 is not `infinitely
    better`, it is a different question (a rescue), and it is counted as one."""
    summary = report.summarize_calibration(_result(_mixed_comparisons()))
    assert summary["pairwise"]["char_ratio"]["n"] == 2
    assert summary["pairwise"]["pypdf_zero_chars"] == 1


def test_the_report_states_the_speed_difference_both_engines_produced():
    summary = report.summarize_calibration(
        _result(_mixed_comparisons(), docling_init_seconds=31.5)
    )
    assert summary["speed"]["docling_init_seconds"] == 31.5
    assert summary["speed"]["pypdf_seconds"] == round(360 / 1000, 3)
    assert summary["speed"]["docling_seconds"] == round(16120 / 1000, 3)
    assert summary["speed"]["slowdown"] > 1


def test_the_identity_block_carries_both_fingerprints():
    summary = report.summarize_calibration(_result(_mixed_comparisons()))
    assert summary["calibration"]["parent_selection_sha256"] == "p" * 64
    assert summary["calibration"]["subset_selection_sha256"] == "s" * 64


def test_a_dry_run_reports_what_would_run_and_no_comparisons():
    summary = report.summarize_calibration(
        _result([], dry_run=True,
                counters={"subset_entries": 40, "blobs_selected": 12,
                          "comparisons_run": 0})
    )
    assert summary["calibration"]["dry_run"] is True
    assert summary["agreement"]["compared"] == 0
    assert summary["blobs"]["selected"] == 12


def test_the_report_renders_without_a_single_comparison():
    text = report.render_calibration(
        report.summarize_calibration(_result([], dry_run=True))
    )
    assert "DRY RUN" in text
    assert "(none)" in text


def test_the_report_renders_every_section_it_promises():
    text = report.render_calibration(
        report.summarize_calibration(_result(_mixed_comparisons()))
    )
    for heading in ("Agreement", "pypdf", "docling", "Rescues", "Speed",
                    "Disagreements"):
        assert heading in text


# --------------------------------------------------------------------------- #
# The comparison associates one blob with every cohort file that shares it
# --------------------------------------------------------------------------- #
def test_one_blob_reports_back_to_every_manifest_entry_that_names_it():
    comparison = _comparison(keys=("file-a", "file-b"))
    assert comparison.comparison_keys == ("file-a", "file-b")
    summary = report.summarize_calibration(_result([comparison]))
    assert summary["blobs"]["subset_files_represented"] == 2
    assert summary["blobs"]["compared"] == 1

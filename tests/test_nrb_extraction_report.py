"""The Phase 6A extraction profile. Pure — fed objects, returns a dict and a string.

The thing these tests exist to protect is the DENOMINATOR. A benchmark of 400
files, 380 of them downloaded, 375 distinct blobs and 20 already done is four
different numbers, and a report that quietly divides by whichever one flatters
the result is worse than no report — it reads as complete while describing a
smaller, self-selected cohort.
"""

from __future__ import annotations

import json
import random

from app.nrb import report
from app.nrb.extract import ExtractResult
from app.nrb.manifest import Manifest
from app.nrb.profile import BlobVerdict, Cohort, CohortKey


def _verdict(sha: str, **overrides) -> BlobVerdict:
    fields = dict(
        content_sha256=sha, parser="pypdf", media_family="pdf",
        status="suspicious", reason="legacy_font_suspected", warnings=(),
        page_count=4, pages_with_text=4, text_page_coverage=1.0,
        median_chars_per_text_page=800.0, char_count=3200, devanagari_ratio=0.0,
        legacy_line_ratio=0.61, legacy_lines=30, judged_lines=49,
        metrics={}, preview="ffihW", error=None, duration_ms=40,
    )
    fields.update(overrides)
    return BlobVerdict(**fields)


def _entry(key: str, *, year=2019, doc="circular", res="pdf", owner="bfr"):
    cohort = "2019" if year == 2019 else "2023-2026"
    return {
        "comparison_key": key, "year": year, "document_type": doc,
        "resource_type": res, "owner": owner,
        "sampling_stratum": f"{cohort}/{doc}/{res}",
    }


def _manifest(entries) -> Manifest:
    return Manifest(
        version="manifest-2", drawn_at="2026-08-15T00:00:00+00:00",
        requested=len(entries), shortfall=0,
        sampler={"floor": 2, "cohort_caps": {"2019": 120}},
        catalog_counts={}, strata=(), notes=(), entries=tuple(entries),
        algorithm_version="nrb-stratified-v1", seed="phase6a-v1",
        selected=len(entries), selection_sha256="deadbeef", diagnostics={},
    )


def _result(cohort: Cohort, **counters) -> ExtractResult:
    base = {
        "blobs_selected": 0, "blobs_attempted": 0, "blobs_persisted": 0,
        "blobs_failed": 0, "blobs_missing_on_disk": 0, "blobs_corrupt_on_disk": 0,
        "pages_read": 0,
    }
    base.update(counters)
    return ExtractResult(
        status="completed", dry_run=False, extractor_version="native-1",
        scope={"manifest_keys": cohort.requested}, counters=base,
        cohort=cohort.as_dict(), counts={"blobs_fetched": 3},
        notes={"failures": [], "failure_count": 0}, duration_seconds=1.0,
    )


# A cohort of 5 manifest files: 4 fetched (two of which share bytes), 1 not
# fetched. So 3 unique blobs, 2 of them extracted.
SHA_SHARED, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
KEYS = (
    CohortKey("k1", "fetched", SHA_SHARED),
    CohortKey("k2", "fetched", SHA_SHARED),
    CohortKey("k3", "fetched", SHA_B),
    CohortKey("k4", "fetched", SHA_C),
    CohortKey("k5", "pending", None),
)
ENTRIES = [
    _entry("k1"), _entry("k2"),
    _entry("k3", year=2024, doc="statistics", res="spreadsheet", owner="red"),
    _entry("k4", year=2024), _entry("k5"),
]


def _cohort(**overrides) -> Cohort:
    fields = dict(
        requested=5, duplicate_keys=0, keys=KEYS, missing=(),
        verdicts={
            SHA_SHARED: _verdict(SHA_SHARED),
            SHA_B: _verdict(SHA_B, status="extracted", reason="clean",
                            media_family="spreadsheet", page_count=None,
                            pages_with_text=None, text_page_coverage=None,
                            median_chars_per_text_page=None,
                            legacy_line_ratio=None, legacy_lines=None,
                            judged_lines=None, devanagari_ratio=0.71),
        },
        extractor_version="native-1",
    )
    fields.update(overrides)
    return Cohort(**fields)


def _summary(**overrides):
    cohort = _cohort(**overrides)
    return report.summarize_extraction(
        _result(cohort), cohort=cohort, manifest=_manifest(ENTRIES),
        manifest_path="docs/nrb/phase6a-manifest.json",
    )


# --------------------------------------------------------------------------- #
# J. Manifest files, fetched files and unique blobs stay distinct
# --------------------------------------------------------------------------- #
def test_the_three_populations_are_reported_separately():
    summary = _summary()
    source = summary["source_coverage"]
    blob = summary["blob_coverage"]
    assert source["requested"] == 5          # what the benchmark asked for
    assert source["fetched"] == 4            # what was acquired
    assert source["unfetched"] == 1
    assert blob["unique_fetched"] == 3       # what there was to extract
    assert blob["duplicates_collapsed"] == 1
    assert blob["already_extracted"] == 2
    assert blob["pending_extraction"] == 1


def test_an_unfetched_file_stays_in_the_manifest_denominator():
    """The failure this guards: 4 of 4 fetched files extracted reads as 100% of a
    400-file benchmark when 396 were never downloaded."""
    cells = _summary()["breakdowns"]["by_cohort"]
    assert cells["2019"]["manifest_files"] == 3
    assert cells["2019"]["fetched_files"] == 2
    assert sum(c["manifest_files"] for c in cells.values()) == 5


def test_every_breakdown_cell_accounts_for_all_of_its_fetched_files():
    """sum(by_status) + files_not_extracted == fetched_files, in every cell of
    every dimension. Nothing may fall out of the accounting silently."""
    summary = _summary()
    for dimension, cells in summary["breakdowns"].items():
        for label, cell in cells.items():
            assert sum(cell["by_status"].values()) + cell["files_not_extracted"] \
                == cell["fetched_files"], (dimension, label, cell)


def test_a_breakdown_counts_files_while_the_verdicts_count_blobs():
    """The shared blob is ONE verdict and TWO files. Both readings are correct;
    the report has to say which it is using."""
    summary = _summary()
    # Two 2019 files fetched, both resolving to the one shared blob.
    assert summary["breakdowns"]["by_cohort"]["2019"]["fetched_files"] == 2
    assert summary["breakdowns"]["by_cohort"]["2019"]["unique_blobs"] == 1
    assert summary["breakdowns"]["by_cohort"]["2019"]["by_status"] == {"suspicious": 2}
    # …but only one verdict exists for it.
    assert summary["by_status"]["suspicious"] == 1


def test_the_breakdowns_cover_every_agreed_dimension():
    summary = _summary()
    assert set(summary["breakdowns"]) == {
        "by_cohort", "by_year", "by_document_type", "by_resource_type", "by_owner",
    }
    assert set(summary["breakdowns"]["by_year"]) == {"2019", "2024"}
    assert set(summary["breakdowns"]["by_resource_type"]) == {"pdf", "spreadsheet"}


def test_metadata_comes_from_the_frozen_manifest_not_a_fresh_catalog_read():
    """A source re-typed by a later sync must not re-label a cohort that has
    already been profiled — so the entry's own stored metadata wins."""
    entries = [dict(e) for e in ENTRIES]
    entries[0]["document_type"] = "act"        # as frozen at draw time
    cohort = _cohort()
    summary = report.summarize_extraction(
        _result(cohort), cohort=cohort, manifest=_manifest(entries)
    )
    assert "act" in summary["breakdowns"]["by_document_type"]


# --------------------------------------------------------------------------- #
# Verdicts, metrics and legacy severity
# --------------------------------------------------------------------------- #
def test_verdicts_and_reasons_are_counted_per_blob():
    summary = _summary()
    assert summary["by_status"] == {"suspicious": 1, "extracted": 1}
    assert summary["by_reason"] == {"clean": 1, "legacy_font_suspected": 1}


def test_the_legacy_bands_use_the_classifiers_own_threshold():
    """0.20 is `quality.LEGACY_LINE_RATIO`. A band edge drifting away from it
    would silently change what the published profile means."""
    from app.nrb.quality import LEGACY_LINE_RATIO

    assert report.LEGACY_BANDS[2][1] == LEGACY_LINE_RATIO == 0.20
    assert _summary()["legacy"]["threshold"] == 0.20


def test_a_ratio_lands_in_exactly_one_band():
    cases = {
        0.0: "0", 0.001: ">0-<0.20", 0.19: ">0-<0.20", 0.20: "0.20-<0.50",
        0.49: "0.20-<0.50", 0.50: "0.50-<0.80", 0.79: "0.50-<0.80",
        0.80: ">=0.80", 1.0: ">=0.80",
    }
    for value, band in cases.items():
        assert report._band_for(value) == band, value


def test_blobs_with_no_legacy_measurement_are_not_banded_as_zero():
    """A spreadsheet has no line structure to judge. Counting it as ratio 0 would
    inflate the "clean" band with documents that were never assessed."""
    summary = _summary()
    assert sum(summary["legacy"]["bands"].values()) == 1     # the PDF only
    assert summary["legacy"]["bands"]["0.50-<0.80"] == 1


def test_the_metric_distributions_cover_the_agreed_measures():
    metrics = _summary()["metrics"]
    assert set(metrics) == {
        "char_count", "devanagari_ratio", "legacy_line_ratio",
        "text_page_coverage", "median_chars_per_text_page",
    }
    assert metrics["char_count"]["n"] == 2
    assert metrics["legacy_line_ratio"]["n"] == 1       # only the PDF has one


def test_a_distribution_is_independent_of_the_order_values_arrive_in():
    values = [3, 1, 4, 1, 5, 9, 2, 6]
    first = report._distribution(values)
    for seed in range(5):
        shuffled = list(values)
        random.Random(seed).shuffle(shuffled)
        assert report._distribution(shuffled) == first


def test_an_empty_distribution_is_zeros_and_nulls_not_a_crash():
    assert report._distribution([])["n"] == 0
    assert report._distribution([])["median"] is None


def test_page_metrics_separate_pages_from_documents():
    summary = _summary()
    assert summary["pages"]["blobs_with_pages"] == 1
    assert summary["pages"]["total_pages"] == 4
    assert summary["pages"]["pages_with_text"] == 4
    assert summary["pages"]["pages_without_text"] == 0


def test_a_document_with_pages_but_no_text_is_counted_explicitly():
    cohort = _cohort(verdicts={
        SHA_SHARED: _verdict(SHA_SHARED, pages_with_text=0, text_page_coverage=0.0,
                             status="needs_ocr", reason="no_text_layer"),
    })
    summary = report.summarize_extraction(
        _result(cohort), cohort=cohort, manifest=_manifest(ENTRIES)
    )
    assert summary["pages"]["documents_with_no_native_text"] == 1
    assert summary["pages"]["pages_without_text"] == 4


def test_warnings_are_reported_without_becoming_statuses():
    cohort = _cohort(verdicts={
        SHA_SHARED: _verdict(SHA_SHARED, warnings=("partial_text_coverage",)),
    })
    summary = report.summarize_extraction(
        _result(cohort), cohort=cohort, manifest=_manifest(ENTRIES)
    )
    assert summary["warnings"] == {"partial_text_coverage": 1}
    assert "partial_text_coverage" not in summary["by_status"]


# --------------------------------------------------------------------------- #
# Pass identity and shape
# --------------------------------------------------------------------------- #
def test_the_report_names_the_cohort_it_describes():
    """A profile of the wrong 400 files is worse than no profile."""
    identity = _summary()["pass"]
    assert identity["manifest_path"] == "docs/nrb/phase6a-manifest.json"
    assert identity["selection_sha256"] == "deadbeef"
    assert identity["extractor_version"] == "native-1"
    assert identity["manifest_entries"] == 5


def test_missing_catalog_keys_are_reported_not_absorbed():
    """Test H. A key the catalog does not know is the one real defect a cohort
    can have, so it must never be quietly dropped from the denominator."""
    cohort = _cohort(requested=6, missing=("k-gone",))
    summary = report.summarize_extraction(
        _result(cohort), cohort=cohort, manifest=_manifest(ENTRIES)
    )
    assert summary["source_coverage"]["missing_from_catalog"] == 1
    assert summary["source_coverage"]["requested"] == 6
    assert summary["source_coverage"]["in_catalog"] == 5


def test_the_summary_is_json_safe_and_stable():
    summary = _summary()
    assert json.loads(json.dumps(summary, default=str))
    assert summary == _summary()


def test_the_rendered_report_keeps_the_two_coverages_visibly_apart():
    text = report.render_extraction(_summary())
    assert "Source coverage (MANIFEST FILES" in text
    assert "Blob coverage (UNIQUE content_sha256" in text
    assert "not fetched yet:" in text
    assert "duplicates:" in text


def test_the_report_renders_without_a_manifest_scope():
    """`--section circular --limit 25` is a legitimate non-benchmark pass."""
    result = ExtractResult(
        status="completed", dry_run=True, extractor_version="native-1",
        scope={"sections": ["circular"]},
        counters={"blobs_selected": 25, "blobs_attempted": 0, "blobs_persisted": 0,
                  "blobs_failed": 0, "blobs_missing_on_disk": 0,
                  "blobs_corrupt_on_disk": 0, "pages_read": 0},
        cohort=None, counts={}, notes={}, duration_seconds=0.2,
    )
    summary = report.summarize_extraction(result)
    assert summary["breakdowns"] == {}
    assert summary["blob_coverage"]["selected_this_pass"] == 25
    assert "no manifest scope" in report.render_extraction(summary)


def test_the_report_reads_only_generic_verdict_fields():
    """Task 16's constraint: the later Docling calibration writes rows of the same
    shape through the same classifier, so nothing here may branch on pypdf."""
    cohort = _cohort(verdicts={
        SHA_SHARED: _verdict(SHA_SHARED, parser="docling"),
        SHA_B: _verdict(SHA_B, parser="docling", status="extracted", reason="clean"),
    })
    summary = report.summarize_extraction(
        _result(cohort), cohort=cohort, manifest=_manifest(ENTRIES)
    )
    assert summary["by_status"] == {"extracted": 1, "suspicious": 1}
    assert summary["metrics"]["legacy_line_ratio"]["n"] == 2

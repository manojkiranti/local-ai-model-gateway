"""The calibration pass against real Postgres and real blobs on disk.

Reuses `test_nrb_extract_integration`'s `blobs` fixture — four cohort files over
three states, two of them byte-identical — because the case this pass most has to
get right is the same one: 40 FILES resolving to fewer BLOBS.

Both engines are injected as counting fakes. Docling is minutes per dozen files
and downloads its layout models on first use; the real engine is exercised by the
opt-in smoke tests in `tests/test_nrb_extraction.py`. What is under test here is
the ORCHESTRATION — what gets selected, how often each blob is parsed, what is
reported, and the fact that nothing is written.
"""

from __future__ import annotations

import asyncio

import pytest

from app.nrb import calibrate, calibration, extraction, quality, report
from tests.test_nrb_extract_integration import _engine, _rows, blobs  # noqa: F401
from tests.test_nrb_extract_integration import _cohort_keys

VERSION = "native-1"


def _subset(keys, *, resource_type="pdf") -> calibration.CalibrationSubset:
    """A frozen subset naming exactly these files. Hand-built, because the point
    is what the PASS does with a subset — the draw has its own suite."""
    entries = tuple(
        {
            "comparison_key": key,
            "subset_rank": rank,
            "parent_rank": rank,
            "year": 2019,
            "cohort": "2019",
            "document_type": "circular",
            "resource_type": resource_type,
            "owner": "bfr",
        }
        for rank, key in enumerate(keys)
    )
    return calibration.CalibrationSubset(
        version=calibration.SUBSET_VERSION,
        purpose=calibration.PURPOSE,
        subset_algorithm_version=calibration.SUBSET_ALGORITHM_VERSION,
        parent_manifest_path="docs/nrb/phase6a-manifest.json",
        parent_selection_sha256="p" * 64,
        resource_type=resource_type,
        requested_size=len(entries),
        selected_size=len(entries),
        subset_selection_sha256="s" * 64,
        generated_at="2026-08-15T00:00:00+00:00",
        entries=entries,
    )


class _CountingEngine:
    """A Docling stand-in that records every path it was handed."""

    def __init__(self, *, status=quality.STATUS_EXTRACTED, fail_on=None):
        self.paths: list[str] = []
        self.opened = 0
        self.closed = 0
        self.init_seconds = 0.25
        self._status = status
        self._fail_on = fail_on or ()

    def open(self):
        self.opened += 1
        return True, "fake engine"

    def extract(self, path):
        self.paths.append(str(path))
        if any(token in str(path) for token in self._fail_on):
            return extraction.extract_file(
                path.parent / "does-not-exist.pdf", family="pdf", extension="pdf"
            )
        return extraction.result_from_pages(
            ["Nepal Rastra Bank circular for all licensed institutions today. " * 8],
            parser="docling",
        )

    def close(self):
        self.closed += 1


def _native_counter():
    calls: list[str] = []

    def parse(path, family, extension):
        calls.append(str(path))
        return extraction.extract_file(path, family=family, extension=extension)

    return calls, parse


def _run(blobs, keys, **kwargs):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def go():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            return await calibrate.run_calibration(
                subset=_subset(keys),
                session_factory=factory,
                base_dir=blobs["base"],
                extractor_version=VERSION,
                **kwargs,
            )
        finally:
            await engine.dispose()

    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# Scope: the frozen subset, and nothing else
# --------------------------------------------------------------------------- #
def test_a_fetched_file_outside_the_subset_is_never_compared(blobs):
    """The calibration cannot wander off the benchmark. `other` is fetched, sits
    in the same catalog and is simply not in the subset."""
    calls, native = _native_counter()
    docling = _CountingEngine()
    result = _run(blobs, _cohort_keys(blobs, "shared_a"),
                  native_extract=native, docling_engine=docling)

    assert result.counters["comparisons_run"] == 1
    assert len(calls) == 1
    assert blobs["other_sha"] not in " ".join(calls)
    assert [c.content_sha256 for c in result.comparisons] == [blobs["shared_sha"]]


def test_two_subset_files_sharing_bytes_are_compared_once_by_each_engine(blobs):
    """The expensive half of the phase. Two logical benchmark entries, identical
    bytes: one comparison, reported against both keys."""
    calls, native = _native_counter()
    docling = _CountingEngine()
    result = _run(blobs, _cohort_keys(blobs, "shared_a", "shared_b"),
                  native_extract=native, docling_engine=docling)

    assert len(result.comparisons) == 1
    assert len(calls) == 1
    assert len(docling.paths) == 1
    assert set(result.comparisons[0].comparison_keys) == \
        set(_cohort_keys(blobs, "shared_a", "shared_b"))


def test_both_manifest_entries_survive_the_report_denominator(blobs):
    calls, native = _native_counter()
    result = _run(blobs, _cohort_keys(blobs, "shared_a", "shared_b"),
                  native_extract=native, docling_engine=_CountingEngine())
    summary = report.summarize_calibration(result)

    assert summary["source"]["subset_entries"] == 2
    assert summary["blobs"]["compared"] == 1
    assert summary["blobs"]["subset_files_represented"] == 2
    assert summary["blobs"]["duplicates_collapsed"] == 1


def test_an_unfetched_subset_member_is_reported_and_not_substituted(blobs):
    result = _run(blobs, _cohort_keys(blobs, "shared_a", "unfetched"),
                  docling_engine=_CountingEngine())
    summary = report.summarize_calibration(result)

    assert summary["source"]["subset_entries"] == 2
    assert summary["source"]["fetched"] == 1
    assert summary["source"]["unfetched"] == 1
    assert summary["blobs"]["compared"] == 1


# --------------------------------------------------------------------------- #
# Deterministic --limit
# --------------------------------------------------------------------------- #
def test_limit_takes_the_first_blobs_in_subset_order_every_time(blobs):
    keys = _cohort_keys(blobs, "other", "shared_a", "shared_b")
    first = _run(blobs, keys, limit=1, docling_engine=_CountingEngine())
    again = _run(blobs, keys, limit=1, docling_engine=_CountingEngine())

    assert [c.content_sha256 for c in first.comparisons] == [blobs["other_sha"]]
    assert [c.content_sha256 for c in first.comparisons] == \
        [c.content_sha256 for c in again.comparisons]


def test_reversing_the_subset_order_reverses_which_blob_a_limit_takes(blobs):
    """`--limit` is the first n OF THE FROZEN SUBSET, not the first n of a SQL
    scan — so it moves when, and only when, the subset order moves."""
    keys = _cohort_keys(blobs, "shared_a", "other")
    result = _run(blobs, keys, limit=1, docling_engine=_CountingEngine())
    assert [c.content_sha256 for c in result.comparisons] == [blobs["shared_sha"]]


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #
def test_a_dry_run_calls_neither_parser(blobs):
    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("a dry run must not parse anything")

    class Exploding:
        init_seconds = 0.0

        def open(self):
            raise AssertionError("a dry run must not build a Docling converter")

        extract = explode
        close = explode

    result = _run(blobs, _cohort_keys(blobs), dry_run=True,
                  native_extract=explode, docling_engine=Exploding())

    assert result.dry_run
    assert result.comparisons == ()
    assert result.counters["blobs_selected"] == 2      # shared + other
    assert result.counters["comparisons_run"] == 0


def test_a_dry_run_still_reports_the_whole_subset_accounting(blobs):
    result = _run(blobs, _cohort_keys(blobs), dry_run=True)
    summary = report.summarize_calibration(result)

    assert summary["source"]["subset_entries"] == 4
    assert summary["source"]["fetched"] == 3
    assert summary["blobs"]["unique_fetched"] == 2
    assert summary["blobs"]["selected"] == 2


def test_a_dry_run_makes_no_http_request(blobs, monkeypatch):
    import httpx

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("Phase 6A makes no HTTP request at all")

    for name in ("get", "request", "stream", "post", "head"):
        monkeypatch.setattr(httpx, name, explode, raising=False)
    monkeypatch.setattr(httpx, "Client", explode)
    monkeypatch.setattr(httpx, "AsyncClient", explode)

    assert _run(blobs, _cohort_keys(blobs), dry_run=True).ok


def test_a_real_pass_makes_no_http_request(blobs, monkeypatch):
    import httpx

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("Phase 6A makes no HTTP request at all")

    for name in ("get", "request", "stream", "post", "head"):
        monkeypatch.setattr(httpx, name, explode, raising=False)
    monkeypatch.setattr(httpx, "Client", explode)
    monkeypatch.setattr(httpx, "AsyncClient", explode)

    result = _run(blobs, _cohort_keys(blobs), docling_engine=_CountingEngine())
    assert result.counters["comparisons_run"] == 2


# --------------------------------------------------------------------------- #
# Nothing is persisted
# --------------------------------------------------------------------------- #
def test_a_calibration_pass_writes_no_extraction_row(blobs):
    """`nrb_extractions` is the canonical screen at one extractor version. The
    parser comparison is experimental calibration data and must not enter it."""
    before = _rows([blobs["shared_sha"], blobs["other_sha"]])
    result = _run(blobs, _cohort_keys(blobs), docling_engine=_CountingEngine())
    after = _rows([blobs["shared_sha"], blobs["other_sha"]])

    assert result.counters["comparisons_run"] == 2
    assert before == after == []


def test_the_docling_converter_is_opened_once_and_closed(blobs):
    docling = _CountingEngine()
    _run(blobs, _cohort_keys(blobs), docling_engine=docling)

    assert docling.opened == 1
    assert docling.closed == 1
    assert len(docling.paths) == 2


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #
def test_one_docling_failure_does_not_stop_the_rest_of_the_pass(blobs):
    docling = _CountingEngine(fail_on=(blobs["other_sha"][:8],))
    result = _run(blobs, _cohort_keys(blobs), docling_engine=docling)

    assert result.counters["comparisons_run"] == 2
    assert result.counters["docling_failed"] == 1
    assert result.counters["pypdf_failed"] == 0
    failed = [c for c in result.comparisons
              if c.docling.status == quality.STATUS_FAILED]
    assert len(failed) == 1
    # The pypdf side of the failed pair is intact — that is the whole point.
    assert failed[0].native.char_count > 0
    assert failed[0].category == "pypdf_rescued_docling"


def test_a_blob_missing_from_disk_is_counted_and_the_pass_continues(blobs):
    from app.nrb import filestore

    path = blobs["base"] / filestore.storage_key_for(blobs["other_sha"], "pdf")
    path.unlink()
    result = _run(blobs, _cohort_keys(blobs), docling_engine=_CountingEngine())

    assert result.counters["blobs_missing_on_disk"] == 1
    assert result.counters["comparisons_run"] == 1


def test_a_corrupt_blob_is_never_compared(blobs):
    """The path IS the checksum. A truncated PDF produces two plausible partial
    texts and an agreement that means nothing."""
    from app.nrb import filestore

    path = blobs["base"] / filestore.storage_key_for(blobs["other_sha"], "pdf")
    path.write_bytes(b"%PDF-1.4 truncated")
    result = _run(blobs, _cohort_keys(blobs), docling_engine=_CountingEngine())

    assert result.counters["blobs_corrupt_on_disk"] == 1
    assert result.counters["comparisons_run"] == 1
    assert blobs["other_sha"] not in [c.content_sha256 for c in result.comparisons]


# --------------------------------------------------------------------------- #
# The report over a real pass
# --------------------------------------------------------------------------- #
def test_the_report_renders_a_real_pass(blobs):
    result = _run(blobs, _cohort_keys(blobs), docling_engine=_CountingEngine())
    text = report.render_calibration(report.summarize_calibration(result))

    assert "DOCLING RESCUED PYPDF" in text
    assert "unique fetched" in text

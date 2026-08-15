"""The extraction pass's pure parts. No DB, no network, no parser.

What lives here is everything about the pass that is a function of its inputs:
the deterministic work order that makes `--limit` reproducible, the blob
verification that keeps a corrupt file out of the numbers, the row builders that
must not lose a metric, and the CLI's scope rule.

The DB-backed half — selection, persistence, failure isolation across a real
pass — is `tests/test_nrb_extract_integration.py`.
"""

from __future__ import annotations

import hashlib
import random

import pytest

from app.nrb import extract as extract_mod
from app.nrb.catalog import ExtractTarget
from app.nrb.extraction import ExtractionResult
from app.nrb.profile import Cohort, CohortKey
from app.nrb.quality import STATUS_EXTRACTED, STATUS_FAILED


def _target(sha: str, *, file_id: int = 1, mime: str = "application/pdf"):
    return ExtractTarget(
        file_id=file_id,
        content_sha256=sha,
        storage_key=f"{sha[:2]}/{sha}.pdf",
        extension="pdf",
        sniffed_mime=mime,
        resource_type="pdf",
        content_length=1000,
    )


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Deterministic work order — what makes --limit mean something
# --------------------------------------------------------------------------- #
def test_without_a_manifest_targets_are_ordered_by_sha():
    targets = [_target(_sha(str(i)), file_id=i) for i in range(8)]
    shuffled = list(targets)
    random.Random(1).shuffle(shuffled)
    assert [t.content_sha256 for t in extract_mod.order_targets(shuffled)] == sorted(
        t.content_sha256 for t in targets
    )


def test_with_a_manifest_targets_follow_the_frozen_cohort_order():
    """`--limit 10` must mean "the first ten of the benchmark", not "the first ten
    rows the database returned". The rank is the position of the earliest cohort
    entry that resolves to each blob."""
    shas = [_sha(str(i)) for i in range(5)]
    rank = {sha: position for position, sha in enumerate(shas)}
    targets = [_target(sha) for sha in reversed(shas)]
    ordered = extract_mod.order_targets(targets, rank=rank)
    assert [t.content_sha256 for t in ordered] == shas


def test_the_limited_target_list_is_identical_however_the_rows_arrive():
    """Test I. Shuffle the selection; the first N must not move."""
    shas = [_sha(str(i)) for i in range(20)]
    rank = {sha: position for position, sha in enumerate(shas)}
    first = None
    for seed in range(5):
        targets = [_target(sha) for sha in shas]
        random.Random(seed).shuffle(targets)
        limited = extract_mod.order_targets(targets, rank=rank)[:6]
        keys = [t.content_sha256 for t in limited]
        assert first is None or keys == first
        first = keys
    assert first == shas[:6]


def test_a_blob_with_no_rank_sorts_after_the_ranked_ones_and_stays_stable():
    """A blob can be in scope without being in the manifest (another selector was
    also given). It must not jump the cohort, and it must not move between runs."""
    ranked = _sha("in-cohort")
    unranked_a, unranked_b = _sha("extra-a"), _sha("extra-b")
    ordered = extract_mod.order_targets(
        [_target(unranked_b), _target(unranked_a), _target(ranked)],
        rank={ranked: 0},
    )
    assert ordered[0].content_sha256 == ranked
    assert [t.content_sha256 for t in ordered[1:]] == sorted([unranked_a, unranked_b])


def test_the_manifest_rank_uses_the_first_entry_that_resolves_to_each_blob():
    """Two cohort files sharing bytes are one blob. It takes the rank of whichever
    of them the manifest names first, so the shared blob does not sink to the
    position of the later entry."""
    sha = _sha("shared")
    cohort = Cohort(
        requested=3,
        duplicate_keys=0,
        keys=(
            CohortKey("k-first", "fetched", sha),
            CohortKey("k-other", "fetched", _sha("other")),
            CohortKey("k-second", "fetched", sha),
        ),
        missing=(),
        verdicts={},
        extractor_version="native-1",
    )
    assert extract_mod._manifest_rank(cohort)[sha] == 0


def test_unfetched_cohort_keys_contribute_no_rank():
    cohort = Cohort(
        requested=2, duplicate_keys=0,
        keys=(CohortKey("a", "pending", None),
              CohortKey("b", "fetched", _sha("b"))),
        missing=(), verdicts={}, extractor_version="native-1",
    )
    assert extract_mod._manifest_rank(cohort) == {_sha("b"): 1}


# --------------------------------------------------------------------------- #
# Blob verification — the path IS the checksum
# --------------------------------------------------------------------------- #
def test_a_blob_whose_bytes_match_its_own_name_verifies(tmp_path):
    body = b"%PDF-1.4 hello"
    path = tmp_path / "blob"
    path.write_bytes(body)
    assert extract_mod._verify_blob(path, hashlib.sha256(body).hexdigest()) is None


def test_a_truncated_blob_is_caught_before_it_is_parsed(tmp_path):
    """A truncated PDF still parses, and produces plausible partial text. Catching
    it here is much cheaper than finding it in the numbers."""
    path = tmp_path / "blob"
    path.write_bytes(b"%PDF-1.4 hel")
    problem = extract_mod._verify_blob(
        path, hashlib.sha256(b"%PDF-1.4 hello").hexdigest()
    )
    assert problem == "blob does not hash to its own storage key"


def test_a_missing_blob_is_distinguishable_from_a_corrupt_one(tmp_path):
    problem = extract_mod._verify_blob(tmp_path / "absent", _sha("x"))
    assert problem is not None
    assert "missing" in problem


def test_a_verification_problem_never_carries_the_path(tmp_path):
    """These strings reach the database. Same rule `app/files/documents.py` follows."""
    path = tmp_path / "absent"
    problem = extract_mod._verify_blob(path, _sha("x")) or ""
    assert str(tmp_path) not in problem


# --------------------------------------------------------------------------- #
# Row building — no metric may be lost on the way to the table
# --------------------------------------------------------------------------- #
def _result(**overrides) -> ExtractionResult:
    fields = dict(
        parser="pypdf", family="pdf", status=STATUS_EXTRACTED, reason="clean",
        warnings=(), text="hello", page_count=3, pages_with_text=3, char_count=120,
        devanagari_ratio=0.8, text_page_coverage=1.0,
        metrics={
            "legacy_line_ratio": 0.42, "legacy_lines": 21, "judged_lines": 50,
            "median_chars_per_page": 40.0, "median_chars_per_text_page": 40.0,
            "char_count": 120,
        },
        preview="hello", error=None, duration_ms=12,
    )
    fields.update(overrides)
    return ExtractionResult(**fields)


def test_the_promoted_severity_columns_come_from_the_metrics_the_classifier_wrote():
    row = extract_mod._row_for(
        _target(_sha("a")), _result(), extractor_version="native-1", now="NOW"
    )
    assert row["legacy_line_ratio"] == 0.42
    assert row["legacy_lines"] == 21
    assert row["judged_lines"] == 50
    assert row["median_chars_per_text_page"] == 40.0
    assert row["text_page_coverage"] == 1.0
    assert row["devanagari_ratio"] == 0.8


def test_every_extraction_column_is_written_so_an_upsert_can_replace_all_of_them():
    """Test G's other half. `record_extractions` derives its conflict-update set
    from the table by subtraction, so a column the writer never supplies would be
    updated to NULL — or, worse, keep a stale value if the set were hand-listed.
    The writer therefore has to supply every non-identity column."""
    from app.nrb.models import NRBExtraction

    row = extract_mod._row_for(
        _target(_sha("a")), _result(), extractor_version="native-1", now="NOW"
    )
    server_managed = {"id", "created_at", "updated_at"}
    columns = {c.name for c in NRBExtraction.__table__.columns} - server_managed
    assert columns == set(row), columns ^ set(row)


def test_a_failed_row_says_why_and_carries_no_measurements():
    row = extract_mod._failed_row(
        _target(_sha("a")), "blob is missing from the store",
        extractor_version="native-1", now="NOW",
    )
    assert row["status"] == STATUS_FAILED
    assert row["error"] == "blob is missing from the store"
    assert row["char_count"] == 0
    assert row["legacy_line_ratio"] is None
    assert row["metrics"] == {}


def test_a_failed_row_has_the_same_shape_as_a_successful_one():
    """Both go through the same upsert, so a missing key would set a column to
    NULL on one path and not the other."""
    ok = extract_mod._row_for(
        _target(_sha("a")), _result(), extractor_version="native-1", now="NOW"
    )
    bad = extract_mod._failed_row(
        _target(_sha("a")), "boom", extractor_version="native-1", now="NOW"
    )
    assert set(ok) == set(bad)


def test_a_preview_is_never_stored_as_an_empty_string():
    """The column is nullable and CHECK-bounded; '' and NULL both mean "nothing to
    show", and one representation is easier to query than two."""
    row = extract_mod._row_for(
        _target(_sha("a")), _result(preview=""),
        extractor_version="native-1", now="NOW",
    )
    assert row["preview"] is None


def test_the_pass_records_the_extractor_version_it_was_told_to():
    row = extract_mod._row_for(
        _target(_sha("a")), _result(), extractor_version="docling-9", now="NOW"
    )
    assert row["extractor_version"] == "docling-9"


# --------------------------------------------------------------------------- #
# The CLI's scope rule
# --------------------------------------------------------------------------- #
def _script():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "nrb_extract.py"
    spec = importlib.util.spec_from_file_location("nrb_extract_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_command_refuses_to_run_with_no_scope():
    """Extraction is CPU-bound over 18.3k files. "No flags" must never mean
    "everything"."""
    script = _script()
    assert script._scope_given(script._parse_args([])) is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--manifest", "m.json"], ["--core"], ["--all"], ["--limit", "5"],
        ["--section", "circular"], ["--owner", "bfr"], ["--type", "pdf"],
        ["--year", "2019"],
    ],
)
def test_every_scope_flag_counts_as_a_scope(argv):
    """A scope flag missing from `_scope_given` silently becomes a whole-corpus
    extraction."""
    script = _script()
    assert script._scope_given(script._parse_args(argv)) is True


def test_a_bare_invocation_exits_two_without_touching_the_database():
    import asyncio

    module = _script()

    async def no_catalog(**kwargs):     # pragma: no cover - must never run
        raise AssertionError("the pass started without a scope")

    module.run_extract = no_catalog
    assert asyncio.run(module.main([])) == 2


def test_the_extractor_version_defaults_to_the_current_one_and_is_overridable():
    from app.nrb.extraction import EXTRACTOR_VERSION

    script = _script()
    assert script._parse_args([]).extractor_version == EXTRACTOR_VERSION
    assert script._parse_args(
        ["--extractor-version", "native-2"]
    ).extractor_version == "native-2"


def test_the_core_sections_match_the_fetch_commands_definition():
    """`--core` must mean the same set of documents in both commands, or "extract
    the core" would silently profile a different cohort than "fetch the core"."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "nrb_fetch.py"
    spec = importlib.util.spec_from_file_location("nrb_fetch_script_2", path)
    fetch_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch_script)
    assert _script().CORE_SECTIONS == fetch_script.CORE_SECTIONS

"""Deterministic stratified benchmark sampling. Pure — no DB, no network, no files.

The requirement these tests encode is representativeness AND reproducibility.
`rows[:400]` satisfies neither: the catalog's id order follows the order REST
paged the post types, so the first 400 rows are one post type from one
department, and any order that depends on the database's row order cannot be
re-run.

The allocator tests are the ones that matter most. A benchmark that quietly
returns 350 files when 400 were asked for — because a cohort cap trimmed the
excess and nobody handed the slots back — reads downstream as "we profiled 400
files". Every number in the published profile would then be computed over a
cohort nobody chose.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from app.nrb import sampling


# --------------------------------------------------------------------------- #
# Fixtures. `_rows` builds catalog-shaped dicts — the shape
# `catalog.load_sample_rows` returns, one row per (file, source) association.
# --------------------------------------------------------------------------- #
def _rows(n, *, year, doc_type, resource_type="pdf", owner="bfr", prefix="k"):
    return [
        {
            "comparison_key": f"{prefix}-{year}-{doc_type}-{resource_type}-{i}",
            "resource_type": resource_type,
            "fetch_status": "pending",
            "content_sha256": None,
            "document_type": doc_type,
            "owner": owner,
            "year": year,
        }
        for i in range(n)
    ]


def _by_key(rows):
    return {row["comparison_key"]: row for row in rows}


def _cohorts(sample, rows):
    index = _by_key(rows)
    return Counter(sampling.year_cohort(index[k]["year"]) for k in sample.keys)


CORPUS = (
    _rows(9000, year=2019, doc_type=None, prefix="a")
    + _rows(400, year=2019, doc_type="circular", prefix="b")
    + _rows(600, year=2021, doc_type="circular", prefix="c")
    + _rows(700, year=2025, doc_type="circular", prefix="d")
    + _rows(300, year=2025, doc_type="statistics", resource_type="spreadsheet", prefix="e")
    + _rows(90, year=2015, doc_type="act", prefix="f")
    + _rows(12, year=2024, doc_type="monetary_policy", prefix="g")
    + _rows(3, year=2024, doc_type="rule_bylaw", prefix="h")
)


# --------------------------------------------------------------------------- #
# Cohorts and candidate canonicalization
# --------------------------------------------------------------------------- #
def test_year_cohorts_cover_the_measured_distribution():
    assert sampling.year_cohort(2007) == "<=2018"
    assert sampling.year_cohort(2018) == "<=2018"
    assert sampling.year_cohort(2019) == "2019"
    assert sampling.year_cohort(2021) == "2020-2022"
    assert sampling.year_cohort(2026) == "2023-2026"
    assert sampling.year_cohort(None) == "unknown"


def test_one_comparison_key_with_several_sources_is_one_candidate():
    """Test I. A file NRB publishes from two pages is ONE file, one download and
    one extraction — never two chances of being drawn."""
    rows = [
        {"comparison_key": "https://x/a.pdf", "resource_type": "pdf",
         "document_type": "circular", "owner": "bfr", "year": 2024},
        {"comparison_key": "https://x/a.pdf", "resource_type": "pdf",
         "document_type": "act", "owner": "psd", "year": 2019},
    ]
    candidates = sampling.build_candidates(rows)
    assert len(candidates) == 1
    assert candidates[0].comparison_key == "https://x/a.pdf"
    assert candidates[0].source_rows == 2


def test_the_canonical_document_type_follows_the_catalogs_own_priority_order():
    """`classify.SECTIONS` is ordered regulatory-first, and `documents.Taxonomy`
    already picks a post's primary section with it. The same rule is applied
    here, so the stratum a file lands in does not depend on which source row the
    database happened to return."""
    rows = [
        {"comparison_key": "k", "resource_type": "pdf", "document_type": "act",
         "owner": "b", "year": 2024},
        {"comparison_key": "k", "resource_type": "pdf", "document_type": "circular",
         "owner": "a", "year": 2024},
    ]
    forward = sampling.build_candidates(rows)[0]
    backward = sampling.build_candidates(list(reversed(rows)))[0]
    # `circular` precedes `act` in SECTIONS, so it wins whichever row came first.
    assert forward.document_type == backward.document_type == "circular"


def test_the_canonical_year_is_the_earliest_a_source_published_the_file():
    rows = [
        {"comparison_key": "k", "resource_type": "pdf", "document_type": "circular",
         "owner": "a", "year": 2024},
        {"comparison_key": "k", "resource_type": "pdf", "document_type": "circular",
         "owner": "a", "year": 2019},
    ]
    candidate = sampling.build_candidates(rows)[0]
    assert candidate.year == 2019
    assert candidate.cohort == "2019"


def test_every_owner_is_kept_and_the_primary_one_is_deterministic():
    rows = [
        {"comparison_key": "k", "resource_type": "pdf", "document_type": "circular",
         "owner": "psd", "year": 2024},
        {"comparison_key": "k", "resource_type": "pdf", "document_type": "circular",
         "owner": "bfr", "year": 2024},
    ]
    candidate = sampling.build_candidates(rows)[0]
    assert candidate.owners == ("bfr", "psd")
    assert candidate.owner == "bfr"


def test_missing_metadata_becomes_an_explicit_bucket_never_a_dropped_row():
    rows = [{"comparison_key": "k", "resource_type": None,
             "document_type": None, "owner": None, "year": None}]
    candidate = sampling.build_candidates(rows)[0]
    assert candidate.document_type == "untyped"
    assert candidate.resource_type == "unknown"
    assert candidate.cohort == "unknown"
    assert candidate.owner == "unknown"


def test_building_candidates_is_independent_of_row_order():
    shuffled = list(CORPUS)
    random.Random(7).shuffle(shuffled)
    assert sampling.build_candidates(CORPUS) == sampling.build_candidates(shuffled)


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def test_the_rank_is_a_sha256_of_algorithm_seed_and_key():
    import hashlib

    expected = hashlib.sha256(
        b"nrb-stratified-v1\x1fphase6a-v1\x1fhttps://x/a.pdf"
    ).hexdigest()
    assert sampling.rank_for("nrb-stratified-v1", "phase6a-v1",
                             "https://x/a.pdf") == expected


def test_the_rank_is_stable_across_processes_unlike_pythons_hash():
    """`hash()` is salted per process (PYTHONHASHSEED), so a sample ordered by it
    would be a different sample on every run."""
    a = sampling.rank_for("v", "s", "k")
    b = sampling.rank_for("v", "s", "k")
    assert a == b == sampling.rank_for("v", "s", "k")
    assert sampling.rank_for("v", "s2", "k") != a


# --------------------------------------------------------------------------- #
# Shape of the draw
# --------------------------------------------------------------------------- #
def test_the_sample_is_bounded_by_the_requested_size():
    assert len(sampling.stratified_sample(CORPUS, size=400).keys) <= 400
    assert len(sampling.stratified_sample(CORPUS, size=37).keys) <= 37


def test_the_sample_is_identical_across_calls():
    a = sampling.stratified_sample(CORPUS, size=400)
    b = sampling.stratified_sample(CORPUS, size=400)
    assert a.keys == b.keys


def test_selection_is_not_the_first_n_rows():
    keys = set(sampling.stratified_sample(CORPUS, size=400).keys)
    assert keys != {r["comparison_key"] for r in CORPUS[:400]}


def test_every_year_cohort_present_in_the_corpus_is_represented():
    sample = sampling.stratified_sample(CORPUS, size=400)
    assert {"<=2018", "2019", "2020-2022", "2023-2026"} <= set(_cohorts(sample, CORPUS))


def test_multiple_document_types_and_formats_are_represented():
    sample = sampling.stratified_sample(CORPUS, size=400)
    index = _by_key(CORPUS)
    types = {index[k]["document_type"] for k in sample.keys}
    formats = {index[k]["resource_type"] for k in sample.keys}
    assert {"circular", "act", "statistics"} <= types
    assert {"pdf", "spreadsheet"} <= formats


def test_a_sparse_stratum_is_not_padded_beyond_what_exists():
    sample = sampling.stratified_sample(CORPUS, size=400)
    rule = [s for s in sample.strata if s.document_type == "rule_bylaw"][0]
    assert rule.available == 3
    assert rule.selected == 3


def test_a_weak_stratum_is_flagged_rather_than_silently_included():
    sample = sampling.stratified_sample(CORPUS, size=400)
    weak = {s.document_type for s in sample.strata if s.weak and s.selected}
    assert "rule_bylaw" in weak       # n=3
    assert "circular" not in weak     # plenty


def test_a_rare_type_is_not_oversampled_to_force_parity():
    sample = sampling.stratified_sample(CORPUS, size=400)
    index = _by_key(CORPUS)
    by_type = Counter(index[k]["document_type"] for k in sample.keys)
    assert by_type["circular"] > by_type["monetary_policy"]


def test_keys_are_unique():
    keys = sampling.stratified_sample(CORPUS, size=400).keys
    assert len(keys) == len(set(keys))


def test_an_empty_corpus_returns_an_empty_sample():
    sample = sampling.stratified_sample([], size=400)
    assert sample.keys == ()
    assert sample.strata == ()
    assert sample.diagnostics.unfillable_slots == 400


# --------------------------------------------------------------------------- #
# A. Exact size when feasible
# --------------------------------------------------------------------------- #
def test_a_feasible_request_returns_exactly_the_requested_size():
    """THE question this task exists to answer: 400 asked for, 400 returned.

    The cap trims 2019; it must not shrink the sample. `CORPUS` has 9,400 of
    11,105 rows in 2019, so proportional allocation alone would spend 85% of the
    budget there and the 30% cap removes hundreds of slots. Every one of them
    comes back.
    """
    sample = sampling.stratified_sample(CORPUS, size=400)
    assert len(sample.keys) == 400
    assert len(set(sample.keys)) == 400
    assert sample.shortfall == 0
    assert sample.diagnostics.selected == 400
    assert sample.diagnostics.unfillable_slots == 0
    assert sample.diagnostics.complete is True


@pytest.mark.parametrize("size", [1, 7, 40, 120, 250, 400, 700])
def test_any_feasible_size_is_delivered_exactly(size):
    sample = sampling.stratified_sample(CORPUS, size=size, max_cohort_share=0.30)
    # Legal capacity under the 30% cap: 0.30*size per cohort across 4 cohorts,
    # bounded by what each cohort actually holds. For every size here that is
    # comfortably >= size.
    assert len(sample.keys) == size


# --------------------------------------------------------------------------- #
# B. 2019 cap redistribution
# --------------------------------------------------------------------------- #
def test_the_2019_cap_frees_slots_and_every_one_is_redistributed():
    sample = sampling.stratified_sample(CORPUS, size=400, cohort_caps={"2019": 80},
                                        max_cohort_share=1.0)
    diag = sample.diagnostics
    before = diag.pre_cap_allocation_by_cohort["2019"]
    assert before > 80, "proportional allocation should overshoot the cap here"
    assert diag.slots_removed_by_cap == before - 80
    assert diag.allocation_by_cohort["2019"] == 80
    assert _cohorts(sample, CORPUS)["2019"] == 80
    assert len(sample.keys) == 400
    assert diag.slots_redistributed >= diag.slots_removed_by_cap


def test_cap_trimmed_slots_are_redistributed_rather_than_lost():
    capped = sampling.stratified_sample(CORPUS, size=400, max_cohort_share=0.30)
    uncapped = sampling.stratified_sample(CORPUS, size=400, max_cohort_share=1.0)
    assert len(capped.keys) == len(uncapped.keys) == 400


# --------------------------------------------------------------------------- #
# C. Redistribution requiring multiple rounds
# --------------------------------------------------------------------------- #
def test_redistribution_continues_after_the_first_recipient_runs_out():
    """One redistribution pass is not enough. The first stratum it reaches has a
    single spare row; the freed slots must keep flowing to the next, and the
    next, until the budget is filled or nothing has headroom."""
    corpus = _rows(2000, year=2019, doc_type="circular", prefix="big")
    # Eight small non-2019 strata. Round-robin hands out at most one slot per
    # stratum per round, so filling the freed slots needs several rounds, and
    # each stratum drops out as it exhausts.
    for i in range(8):
        corpus += _rows(14, year=2021, doc_type=f"type{i:02d}", prefix=f"s{i}")
    sample = sampling.stratified_sample(
        corpus, size=120, floor=1, cohort_caps={"2019": 20}, max_cohort_share=1.0
    )
    diag = sample.diagnostics
    assert diag.allocation_by_cohort["2019"] == 20
    assert diag.redistribution_rounds >= 2
    # 20 from 2019 + every one of the 112 non-2019 rows = 132 legal capacity.
    assert len(sample.keys) == 120
    assert diag.slots_removed_by_cap > 0


# --------------------------------------------------------------------------- #
# D. The hard cap always holds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("floor", [0, 1, 5, 40])
@pytest.mark.parametrize("size", [50, 200, 400])
def test_the_cohort_cap_is_never_exceeded_whatever_the_floor_or_size(floor, size):
    sample = sampling.stratified_sample(
        CORPUS, size=size, floor=floor, max_cohort_share=0.30
    )
    cap = int(size * 0.30)
    for cohort, n in _cohorts(sample, CORPUS).items():
        assert n <= cap, (cohort, n, cap)


def test_the_cohort_cap_still_holds_after_redistribution():
    sample = sampling.stratified_sample(CORPUS, size=400, max_cohort_share=0.30)
    for cohort, n in _cohorts(sample, CORPUS).items():
        assert n <= int(400 * 0.30), (cohort, n)


def test_an_explicit_cohort_cap_beats_a_looser_share():
    sample = sampling.stratified_sample(
        CORPUS, size=400, max_cohort_share=1.0, cohort_caps={"2019": 25}
    )
    assert _cohorts(sample, CORPUS)["2019"] == 25


# --------------------------------------------------------------------------- #
# E. Impossible size
# --------------------------------------------------------------------------- #
def test_an_infeasible_request_reports_its_shortfall_instead_of_pretending():
    only_2019 = _rows(5000, year=2019, doc_type="circular")
    sample = sampling.stratified_sample(only_2019, size=400, max_cohort_share=0.30)
    assert len(sample.keys) == 120
    assert sample.shortfall == 280
    assert sample.diagnostics.unfillable_slots == 280
    assert sample.diagnostics.complete is False
    assert sample.diagnostics.incomplete_reason
    assert sample.notes


def test_a_corpus_smaller_than_the_request_reports_a_shortfall():
    sample = sampling.stratified_sample(_rows(7, year=2021, doc_type="act"), size=400)
    assert len(sample.keys) == 7
    assert sample.shortfall == 393
    assert sample.diagnostics.incomplete_reason == "corpus_exhausted"
    assert any("exhausted" in note for note in sample.notes)


def test_an_infeasible_request_still_never_breaches_a_cap():
    only_2019 = _rows(5000, year=2019, doc_type="circular")
    sample = sampling.stratified_sample(only_2019, size=400, cohort_caps={"2019": 61},
                                        max_cohort_share=1.0)
    assert len(sample.keys) == 61
    assert sample.diagnostics.incomplete_reason == "cohort_caps_reached"


# --------------------------------------------------------------------------- #
# F. Floor overflow
# --------------------------------------------------------------------------- #
def _ten_strata():
    corpus = []
    for i in range(10):
        corpus += _rows(20, year=2021, doc_type=f"type{i:02d}", prefix=f"t{i}")
    return corpus


def test_a_floor_larger_than_the_budget_spreads_instead_of_favouring_early_strata():
    """10 strata, floor 5, budget 12. Round-robin gives every stratum one slot and
    then two of them a second. A `for key in sorted(...): take = min(floor, ...)`
    loop gives 5 + 5 + 2 — three strata represented and seven invisible, chosen by
    nothing but their names."""
    corpus = _ten_strata()
    sample = sampling.stratified_sample(corpus, size=12, floor=5, max_cohort_share=1.0)
    index = _by_key(corpus)
    represented = {index[k]["document_type"] for k in sample.keys}
    assert len(sample.keys) == 12
    assert len(represented) == 10


def test_an_unsatisfiable_floor_is_reported_rather_than_silently_dropped():
    sample = sampling.stratified_sample(_ten_strata(), size=12, floor=5,
                                        max_cohort_share=1.0)
    diag = sample.diagnostics
    assert diag.floor == 5
    assert diag.floor_requested_slots == 50      # 10 strata x floor 5
    assert diag.floor_allocated_slots == 12
    assert diag.floor_shortfall_slots == 38
    assert diag.floor_short_strata_count == 10


def test_the_floor_pass_does_not_privilege_lexicographically_early_strata():
    """Every stratum holds 20 rows, so nothing but the algorithm decides who gets
    the two spare slots. If they always went to `type00`/`type01` the floor pass
    would be a sorted walk wearing a round-robin's clothes."""
    corpus = _ten_strata()
    index = _by_key(corpus)
    extras = set()
    for seed in ("s1", "s2", "s3", "s4", "s5", "s6"):
        sample = sampling.stratified_sample(corpus, size=12, floor=5, seed=seed,
                                            max_cohort_share=1.0)
        counts = Counter(index[k]["document_type"] for k in sample.keys)
        extras |= {t for t, n in counts.items() if n == 2}
    assert len(extras) > 2, extras


def test_the_floor_overflow_result_is_the_same_however_the_rows_arrive():
    corpus = _ten_strata()
    shuffled = list(corpus)
    random.Random(11).shuffle(shuffled)
    a = sampling.stratified_sample(corpus, size=12, floor=5, max_cohort_share=1.0)
    b = sampling.stratified_sample(shuffled, size=12, floor=5, max_cohort_share=1.0)
    assert a.keys == b.keys
    assert [(s.label, s.selected) for s in a.strata] == \
           [(s.label, s.selected) for s in b.strata]


# --------------------------------------------------------------------------- #
# G. Floor versus cap
# --------------------------------------------------------------------------- #
def test_when_the_floor_and_the_cohort_cap_conflict_the_cap_wins():
    """2019 holds 20 strata. A floor of 5 wants 100 slots there; the cap allows
    30. A floor is a representation preference, a cap is a hard constraint —
    satisfying the floor by breaching the cap would make the 2019 comparison the
    very thing the cap exists to prevent."""
    corpus = _rows(500, year=2024, doc_type="circular", prefix="new")
    for i in range(20):
        corpus += _rows(20, year=2019, doc_type=f"type{i:02d}", prefix=f"m{i}")
    sample = sampling.stratified_sample(
        corpus, size=200, floor=5, cohort_caps={"2019": 30}, max_cohort_share=1.0
    )
    diag = sample.diagnostics
    assert diag.allocation_by_cohort["2019"] == 30
    assert diag.floor_shortfall_slots > 0
    assert diag.floor_short_strata_count > 0
    assert "2019" in diag.capped_cohorts
    assert len(sample.keys) == 200          # the rest of the budget still lands


def test_the_cap_trims_depth_before_it_trims_representation():
    """The 2019 cap of 24 is big enough for all four of its strata to keep the
    floor of 5, so all four must survive it. What the cap takes is DEPTH: the
    large stratum was allocated ~78 slots proportionally and comes back level
    with the small ones, rather than the small ones being deleted to protect it.
    """
    corpus = _rows(500, year=2024, doc_type="circular", prefix="new")
    corpus += _rows(400, year=2019, doc_type="circular", prefix="big")
    for i in range(3):
        corpus += _rows(20, year=2019, doc_type=f"small{i}", prefix=f"s{i}")
    sample = sampling.stratified_sample(
        corpus, size=200, floor=5, cohort_caps={"2019": 24}, max_cohort_share=1.0
    )
    selected = {s.label: s.selected for s in sample.strata if s.cohort == "2019"}
    assert sum(selected.values()) == 24
    assert len(selected) == 4                       # nothing was deleted outright
    assert all(n >= 5 for n in selected.values()), selected   # every floor kept
    big = next(n for label, n in selected.items() if "circular" in label)
    assert big <= min(selected.values()) + 1        # depth, not breadth, was cut
    assert sample.diagnostics.floor_shortfall_slots == 0


# --------------------------------------------------------------------------- #
# H. Stratum capacity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size", [50, 400, 5000])
def test_no_stratum_is_ever_allocated_more_than_it_holds(size):
    sample = sampling.stratified_sample(CORPUS, size=size, max_cohort_share=1.0)
    for stratum in sample.strata:
        assert stratum.selected <= stratum.available
        assert stratum.selected == stratum.allocated


def test_requesting_more_than_exists_returns_everything_once():
    small = _rows(7, year=2021, doc_type="act")
    sample = sampling.stratified_sample(small, size=400)
    assert len(sample.keys) == len(set(sample.keys)) == 7


# --------------------------------------------------------------------------- #
# J. Input ordering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed_for_shuffle", [1, 2, 3])
def test_shuffled_input_produces_an_identical_sample(seed_for_shuffle):
    shuffled = list(CORPUS)
    random.Random(seed_for_shuffle).shuffle(shuffled)
    a = sampling.stratified_sample(CORPUS, size=400)
    b = sampling.stratified_sample(shuffled, size=400)
    assert a.keys == b.keys
    assert [(s.label, s.selected) for s in a.strata] == \
           [(s.label, s.selected) for s in b.strata]
    assert a.diagnostics.as_dict() == b.diagnostics.as_dict()
    assert a.fingerprint_payload() == b.fingerprint_payload()


def test_reversing_the_input_produces_an_identical_sample():
    a = sampling.stratified_sample(CORPUS, size=400)
    b = sampling.stratified_sample(list(reversed(CORPUS)), size=400)
    assert a.keys == b.keys


# --------------------------------------------------------------------------- #
# K. Seed behaviour
# --------------------------------------------------------------------------- #
def test_a_different_seed_draws_a_different_cohort_of_the_same_shape():
    a = sampling.stratified_sample(CORPUS, size=400, seed="seed-a")
    b = sampling.stratified_sample(CORPUS, size=400, seed="seed-b")
    assert a.keys != b.keys                       # the seed is actually used
    assert len(a.keys) == len(b.keys) == 400      # the constraints are unchanged
    assert _cohorts(a, CORPUS)["2019"] <= 120
    assert _cohorts(b, CORPUS)["2019"] <= 120


def test_the_seed_is_recorded_on_the_sample():
    sample = sampling.stratified_sample(CORPUS, size=40, seed="seed-a")
    assert sample.seed == "seed-a"
    assert sample.algorithm_version == sampling.ALGORITHM_VERSION


# --------------------------------------------------------------------------- #
# Canonical ordering
# --------------------------------------------------------------------------- #
def test_entries_are_written_in_canonical_rank_order_not_database_order():
    sample = sampling.stratified_sample(CORPUS, size=400)
    ranks = [
        sampling.rank_for(sample.algorithm_version, sample.seed, key)
        for key in sample.keys
    ]
    assert ranks == sorted(ranks)


def test_selected_entries_line_up_with_the_selected_keys():
    sample = sampling.stratified_sample(CORPUS, size=40)
    assert tuple(entry.comparison_key for entry in sample.entries) == sample.keys


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def test_the_diagnostics_explain_a_short_sample_without_reading_the_code():
    only_2019 = _rows(5000, year=2019, doc_type="circular")
    diag = sampling.stratified_sample(only_2019, size=400,
                                      max_cohort_share=0.30).diagnostics
    payload = diag.as_dict()
    for field in (
        "requested", "selected", "candidates", "strata", "allocation_by_stratum",
        "allocation_by_cohort", "candidates_by_cohort", "capped_cohort",
        "capped_cohort_candidates", "capped_cohort_selected", "cohort_caps",
        "floor", "floor_requested_slots", "floor_allocated_slots",
        "floor_shortfall_slots", "floor_short_strata", "slots_removed_by_cap",
        "slots_redistributed", "unfillable_slots", "exhausted_strata",
        "capped_cohorts", "complete", "incomplete_reason",
    ):
        assert field in payload, field
    assert payload["capped_cohort_candidates"] == 5000
    assert payload["capped_cohort_selected"] == 120
    assert payload["unfillable_slots"] == 280


def test_the_diagnostics_are_json_safe():
    import json

    diag = sampling.stratified_sample(CORPUS, size=400).diagnostics
    assert json.loads(json.dumps(diag.as_dict())) == diag.as_dict()


def test_the_allocation_totals_agree_with_the_keys_that_came_back():
    sample = sampling.stratified_sample(CORPUS, size=400)
    diag = sample.diagnostics
    assert sum(diag.allocation_by_cohort.values()) == len(sample.keys)
    assert sum(diag.allocation_by_stratum.values()) == len(sample.keys)
    assert sum(s.selected for s in sample.strata) == len(sample.keys)

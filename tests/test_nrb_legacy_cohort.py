"""The Phase 6B evaluation cohort: deterministic, frozen, drawn before conversion.

Pure — `legacy_eval`'s selection and fingerprinting take plain objects. The DB
reads and blob reads in that module are exercised by the live pass, not here.

The property under test is the one that makes the evaluation trustworthy at all:
the cohort is a function of the frozen benchmark's identity and the blobs' content
hashes, so it cannot have been influenced by how conversion turned out.
"""

import random

from app.nrb import legacy_eval

PARENT = "1ae297dba1c33c7db9976f817806f6666371695a31e1f424d046993d581a1312"


def _ref(sha: str, ratio: float) -> legacy_eval.BlobRef:
    return legacy_eval.BlobRef(
        content_sha256=sha,
        storage_key=f"{sha[:2]}/{sha}.pdf",
        extension="pdf",
        sniffed_mime="application/pdf",
        status="suspicious",
        reason="legacy_font_suspected",
        metrics={"legacy_line_ratio": ratio, "devanagari_ratio": 0.0},
        comparison_keys=(f"https://www.nrb.org.np/{sha}.pdf",),
    )


def _population(n: int = 60) -> list[legacy_eval.BlobRef]:
    """Spread across all three severity bands."""
    ratios = [0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.92, 1.0]
    return [
        _ref(f"{i:064x}", ratios[i % len(ratios)])
        for i in range(n)
    ]


def test_bands_follow_the_phase_6a_severity_bands():
    assert legacy_eval.band_for(0.19) == "below-0.20"
    assert legacy_eval.band_for(0.20) == "0.20-0.50"
    assert legacy_eval.band_for(0.50) == "0.50-0.80"
    assert legacy_eval.band_for(0.80) == "0.80-1.00"
    assert legacy_eval.band_for(1.00) == "0.80-1.00"


def test_selection_is_stratified_across_bands():
    cohort = legacy_eval.select_cohort(
        _population(), parent_fingerprint=PARENT, per_band=5
    )
    bands = {}
    for entry in cohort:
        bands[entry.band] = bands.get(entry.band, 0) + 1
    assert bands == {"0.20-0.50": 5, "0.50-0.80": 5, "0.80-1.00": 5}


def test_shuffled_input_selects_the_same_identities():
    """The draw ranks on the blob's own content hash, not on its position, so the
    order rows come back from Postgres cannot change the cohort."""
    population = _population()
    first = legacy_eval.select_cohort(
        population, parent_fingerprint=PARENT, per_band=4
    )
    shuffled = list(population)
    random.Random(20260815).shuffle(shuffled)
    second = legacy_eval.select_cohort(
        shuffled, parent_fingerprint=PARENT, per_band=4
    )
    assert [e.content_sha256 for e in first] == [e.content_sha256 for e in second]


def test_the_cohort_is_reproducible_and_fingerprint_stable():
    a = legacy_eval.select_cohort(_population(), parent_fingerprint=PARENT, per_band=4)
    b = legacy_eval.select_cohort(_population(), parent_fingerprint=PARENT, per_band=4)
    fingerprint = legacy_eval.cohort_fingerprint(a, parent_fingerprint=PARENT)
    assert fingerprint == legacy_eval.cohort_fingerprint(b, parent_fingerprint=PARENT)
    assert len(fingerprint) == 64


def test_a_different_parent_benchmark_draws_a_different_cohort():
    """Binding to the parent fingerprint is what stops a cohort being silently
    reused against a benchmark it was not drawn from."""
    ours = legacy_eval.select_cohort(
        _population(), parent_fingerprint=PARENT, per_band=4
    )
    other = legacy_eval.select_cohort(
        _population(), parent_fingerprint="0" * 64, per_band=4
    )
    assert [e.content_sha256 for e in ours] != [e.content_sha256 for e in other]
    assert legacy_eval.cohort_fingerprint(ours, parent_fingerprint=PARENT) != \
        legacy_eval.cohort_fingerprint(other, parent_fingerprint="0" * 64)


def test_a_short_band_contributes_everything_it_has_and_is_not_topped_up():
    """Bands are different populations; borrowing from one to fill another would
    misrepresent the stratification."""
    population = [_ref(f"{i:064x}", 0.25) for i in range(3)] + \
                 [_ref(f"{i + 100:064x}", 0.95) for i in range(10)]
    cohort = legacy_eval.select_cohort(
        population, parent_fingerprint=PARENT, per_band=5
    )
    bands = {}
    for entry in cohort:
        bands[entry.band] = bands.get(entry.band, 0) + 1
    assert bands == {"0.20-0.50": 3, "0.80-1.00": 5}


def test_selection_records_the_evidence_a_reader_needs():
    entry = legacy_eval.select_cohort(
        _population(), parent_fingerprint=PARENT, per_band=1
    )[0]
    payload = entry.as_json()
    assert set(payload) >= {
        "content_sha256", "band", "legacy_line_ratio", "status", "reason",
        "family", "role", "rank", "comparison_keys",
    }
    assert payload["rank"]           # the draw position is reproducible evidence
    assert payload["comparison_keys"]


def test_the_fingerprint_covers_content_not_order():
    cohort = legacy_eval.select_cohort(
        _population(), parent_fingerprint=PARENT, per_band=4
    )
    reversed_cohort = list(reversed(cohort))
    assert legacy_eval.cohort_fingerprint(cohort, parent_fingerprint=PARENT) == \
        legacy_eval.cohort_fingerprint(reversed_cohort, parent_fingerprint=PARENT)

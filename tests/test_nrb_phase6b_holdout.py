"""The Phase 6B routing holdout is a FROZEN, INDEPENDENT cohort — these tests
guard its identity, not the classifier's behaviour.

A holdout is only independent evidence if no file it contains ever influenced the
thing it validates. The Phase 6A benchmark shaped native-1, native-2, the table
guards, the spreadsheet and minority-region rules, `MIN_JUDGED_FOR_RATIO`,
`MIN_LEGACY_ABSOLUTE` and the observed `>=0.80` band. So the one property that
must never silently break is: NOT ONE Phase 6A comparison_key may appear in the
holdout, and the holdout's fingerprint must prove which cohort it withheld.

These assertions are pure file reads — no database, no network — so they run in
CI without the scratch catalog. "Every key exists in the catalog" is a live check
performed once at draw time (it needs `local_ai_gateway_p4`) and recorded in the
profile, not encoded here, exactly as other catalog-dependent NRB checks are.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.nrb import sampling
from app.nrb.manifest import read_manifest, verify_manifest

_ROOT = Path(__file__).resolve().parent.parent
PHASE6A = _ROOT / "docs" / "nrb" / "phase6a-manifest.json"
HOLDOUT = _ROOT / "docs" / "nrb" / "phase6b-routing-holdout.json"

# The frozen identities. Written down here so a re-draw that changes either cohort
# fails loudly rather than sliding a new benchmark in under the old name.
PHASE6A_FINGERPRINT = (
    "1ae297dba1c33c7db9976f817806f6666371695a31e1f424d046993d581a1312"
)
HOLDOUT_FINGERPRINT = (
    "6344e674f788808ab02f46218e59a76c215c0644cb95abbbf8212d45d400a970"
)
HOLDOUT_SEED = "phase6b-routing-holdout-v1"
HOLDOUT_SIZE = 150


def _keys(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [e["comparison_key"] for e in payload["entries"]]


# --------------------------------------------------------------------------- #
# Identity and integrity of the holdout itself
# --------------------------------------------------------------------------- #
def test_the_holdout_has_exactly_150_unique_keys():
    keys = _keys(HOLDOUT)
    assert len(keys) == HOLDOUT_SIZE
    assert len(set(keys)) == HOLDOUT_SIZE


def test_the_holdout_fingerprint_is_frozen_and_self_consistent():
    manifest = read_manifest(HOLDOUT)
    assert manifest.selection_sha256 == HOLDOUT_FINGERPRINT
    result = verify_manifest(manifest)
    assert result.ok, result.reason


def test_the_holdout_was_drawn_with_the_frozen_parameters():
    manifest = read_manifest(HOLDOUT)
    assert manifest.seed == HOLDOUT_SEED
    assert manifest.algorithm_version == sampling.ALGORITHM_VERSION
    assert manifest.requested == HOLDOUT_SIZE
    assert manifest.selected == HOLDOUT_SIZE
    assert manifest.shortfall == 0
    # 2019 capped at 45 so the CMS-migration cohort cannot dominate the holdout.
    assert manifest.sampler["cohort_caps"] == {"2019": 45}


# --------------------------------------------------------------------------- #
# THE leakage guard — the reason this file exists
# --------------------------------------------------------------------------- #
def test_no_phase6a_key_leaks_into_the_holdout():
    """The single most important property of the whole task. If this ever fails,
    the holdout is development evidence, not independent validation."""
    intersection = set(_keys(PHASE6A)) & set(_keys(HOLDOUT))
    assert intersection == set(), sorted(intersection)


def test_the_exclusion_is_bound_to_the_exact_phase6a_cohort():
    """Not merely 'excluded 400 files' — excluded THESE 400. The recorded
    exclusion hash must equal the hash of Phase 6A's own keys, so a future edit to
    either cohort cannot leave the binding looking valid."""
    manifest = read_manifest(HOLDOUT)
    p6a_keys = set(_keys(PHASE6A))
    assert manifest.sampler["exclude_count"] == len(p6a_keys) == 400
    expected = hashlib.sha256(
        "\x1f".join(sorted(p6a_keys)).encode("utf-8")
    ).hexdigest()
    assert manifest.sampler["exclude_keys_sha256"] == expected


def test_the_holdout_records_its_phase6a_provenance():
    manifest = read_manifest(HOLDOUT)
    excluded = manifest.provenance["excluded_manifests"]
    assert len(excluded) == 1
    assert excluded[0]["path"] == "docs/nrb/phase6a-manifest.json"
    assert excluded[0]["selection_sha256"] == PHASE6A_FINGERPRINT
    assert excluded[0]["keys"] == 400


def test_the_holdout_seed_is_independent_of_phase6a():
    """A different seed (and the exclusion) is what makes this an independent draw
    rather than a prefix of the same ordering."""
    assert read_manifest(HOLDOUT).seed != read_manifest(PHASE6A).seed


# --------------------------------------------------------------------------- #
# Phase 6A must not have been touched to make room for the holdout
# --------------------------------------------------------------------------- #
def test_phase6a_manifest_is_unchanged():
    manifest = read_manifest(PHASE6A)
    assert manifest.selection_sha256 == PHASE6A_FINGERPRINT
    assert verify_manifest(manifest).ok
    assert len(manifest.keys()) == 400

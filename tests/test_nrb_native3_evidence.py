"""The native-3 cohort measurements' pure half (spec §6.3)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nrb_native3_evidence as E  # noqa: E402


def test_classification_is_equal_only_when_every_native2_field_is():
    v2 = {"status": "extracted", "reason": "clean", "metrics": {"a": 1, "b": 2}}
    v3 = {"status": "extracted", "reason": "clean", "metrics": {"a": 1, "b": 2, "native3": {}}}
    assert E.classification_equal(v2, v3)
    assert not E.classification_equal(v2, {**v3, "metrics": {"a": 1, "b": 3, "native3": {}}})
    assert not E.classification_equal(v2, {**v3, "reason": "legacy_font_suspected"})


def test_the_crosstab_splits_false_negative_and_false_positive_candidates():
    records = [
        {"sha": "a", "detected": True, "contradicts": True},
        {"sha": "b", "detected": False, "contradicts": True},
        {"sha": "c", "detected": True, "contradicts": False},
        {"sha": "d", "detected": False, "contradicts": False},
    ]
    out = E.crosstab(records)
    assert out["both"] == 1 and out["neither"] == 1
    assert out["false_negative_candidates"] == ["b"]
    assert out["false_positive_candidates"] == ["c"]


def test_wilson_interval_brackets_the_rate():
    lo, hi = E.wilson(5, 100)
    assert 0 < lo < 0.05 < hi < 0.15
    assert E.wilson(0, 0) == (0.0, 0.0)

"""The native-3 cohort: drawn from metadata alone, every spent key withheld (spec §6.2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import nrb_native3_cohort as C  # noqa: E402

from app.nrb import manifest, sampling  # noqa: E402


def _cand(key, doc_type="act", year=2020, resource="pdf"):
    return sampling.Candidate(key, year, sampling.year_cohort(year), doc_type, resource,
                              "lgd", ("lgd",), 1)


def test_every_spent_set_is_withheld():
    keys = C.spent_keys([REPO / p for p in C.SPENT_SETS])
    for path in C.SPENT_SETS:
        for entry in json.loads((REPO / path).read_text())["entries"]:
            assert entry["comparison_key"] in keys


def test_the_strata_are_metadata_only():
    assert C.stratum_of(_cand("a", "act", 2015)) == "enriched"
    assert C.stratum_of(_cand("a", "directive", 2023)) == "enriched"
    assert C.stratum_of(_cand("a", "act", 2014)) == "random"
    assert C.stratum_of(_cand("a", "circular", 2023)) == "random"
    assert C.stratum_of(_cand("a", "act", None)) == "random"


def test_the_draw_is_pdf_only_excludes_spent_keys_and_is_deterministic():
    pool = [_cand(f"k{i}", "act" if i % 2 else "circular") for i in range(40)]
    pool.append(_cand("xls", resource="spreadsheet"))
    first = C.draw(pool, excluded=frozenset({"k1"}), seed="s", sizes={"enriched": 5, "random": 5})
    again = C.draw(list(reversed(pool)), excluded=frozenset({"k1"}), seed="s",
                   sizes={"enriched": 5, "random": 5})
    keys = [c.comparison_key for _, c in first]
    assert keys == [c.comparison_key for _, c in again]
    assert "k1" not in keys and "xls" not in keys
    assert [s for s, _ in first].count("enriched") == 5


def test_the_top_up_takes_the_next_ranks_of_the_same_stratum():
    pool = [_cand(f"k{i}") for i in range(20)]
    first = C.draw(pool, excluded=frozenset(), seed="s", sizes={"enriched": 5, "random": 0})
    more = C.draw(pool, excluded=frozenset(), seed="s", sizes={"enriched": 5, "random": 0}, offset=5)
    assert not {c.comparison_key for _, c in first} & {c.comparison_key for _, c in more}


def test_the_manifest_binds_the_detector_and_verifies(tmp_path):
    pool = [_cand(f"k{i}") for i in range(12)]
    built = C.build(pool, excluded=frozenset({"zz"}), seed="s",
                    sizes={"enriched": 4, "random": 4}, offset=0, drawn_at="2026-10-02T00:00:00+05:45",
                    catalog_counts={}, excluded_sets=list(C.SPENT_SETS))
    assert built.sampler["detector"] == {"density": 0.001, "min_devanagari": 500}
    assert manifest.verify_manifest(built).ok
    out = tmp_path / "m.json"
    manifest.write_new_manifest(built, out)
    assert manifest.read_manifest(out).keys() == built.keys()

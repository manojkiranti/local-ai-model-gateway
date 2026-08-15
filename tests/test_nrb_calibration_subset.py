"""The frozen Docling calibration subset — drawn from the benchmark, not the DB.

The subset answers one question: *which* PDFs will pypdf and Docling be compared
on. It is frozen for the same reason the parent cohort is — a comparison run over
whichever files happened to be on disk measures the disk — and it is drawn from
the parent manifest's own entries, so a calibration can never run over a file the
screen never saw.

The tests below are mostly about what must NOT be able to move the selection:
fetch state, extraction verdicts, entry order, or a parent that has been edited.

No database, no network, no Docling: this is a JSON file naming catalog keys.
"""

from __future__ import annotations

import json
import random

import pytest

from app.nrb import calibration
from app.nrb import manifest as manifest_module

PDF = "https://www.nrb.org.np/contents/uploads/2026/08/circular-{}.pdf"
XLSX = "https://www.nrb.org.np/contents/uploads/2026/08/table-{}.xlsx"


def _entry(key: str, resource_type: str = "pdf", **overrides) -> dict:
    entry = {
        "comparison_key": key,
        "year": 2024,
        "document_type": "circular",
        "resource_type": resource_type,
        "owner": "bfr",
        "owners": ["bfr"],
        "sampling_stratum": f"2023-2026/circular/{resource_type}",
    }
    entry.update(overrides)
    return entry


def _parent(
    *,
    pdfs: int = 60,
    others: int = 5,
    entries: list[dict] | None = None,
    seed: str = "phase6a-v1",
) -> manifest_module.Manifest:
    """A parent manifest whose own fingerprint verifies, like the frozen one."""
    if entries is None:
        entries = [_entry(PDF.format(i)) for i in range(pdfs)]
        entries += [
            _entry(XLSX.format(i), resource_type="spreadsheet") for i in range(others)
        ]
    sampler = {"size": len(entries), "floor": 2, "algorithm_version": "test-v1",
               "seed": seed}
    keys = tuple(dict.fromkeys(e["comparison_key"] for e in entries))
    return manifest_module.Manifest(
        version=manifest_module.MANIFEST_VERSION,
        drawn_at="2026-08-15T00:00:00+00:00",
        algorithm_version="test-v1",
        seed=seed,
        requested=len(entries),
        selected=len(entries),
        shortfall=0,
        sampler=sampler,
        catalog_counts={},
        strata=(),
        notes=(),
        entries=tuple(entries),
        selection_sha256=manifest_module.compute_selection_sha256(
            manifest_version=manifest_module.MANIFEST_VERSION,
            algorithm_version="test-v1",
            seed=seed,
            parameters=sampler,
            keys=keys,
        ),
    )


def _subset(parent=None, **overrides) -> calibration.CalibrationSubset:
    fields = dict(
        parent_manifest_path="docs/nrb/phase6a-manifest.json",
        size=40,
        generated_at="2026-08-15T00:00:00+00:00",
    )
    fields.update(overrides)
    return calibration.build_subset(parent or _parent(), **fields)


# --------------------------------------------------------------------------- #
# A. The candidate universe is the parent manifest, and nothing else
# --------------------------------------------------------------------------- #
def test_every_selected_key_comes_from_the_parent_manifest():
    parent = _parent()
    subset = _subset(parent)
    assert set(subset.keys()) <= set(parent.keys())


def test_building_a_subset_needs_nothing_but_a_manifest():
    """No session, no engine, no catalog: there is no path by which a key outside
    the frozen benchmark could enter the calibration."""
    import inspect

    parameters = inspect.signature(calibration.build_subset).parameters
    assert not {"session", "engine", "session_factory"} & set(parameters)


# --------------------------------------------------------------------------- #
# B. Only PDFs are eligible
# --------------------------------------------------------------------------- #
def test_only_pdf_entries_are_eligible():
    subset = _subset()
    assert {e["resource_type"] for e in subset.entries} == {"pdf"}
    assert all(key.endswith(".pdf") for key in subset.keys())


def test_the_restriction_is_recorded_in_the_artifact():
    assert _subset().resource_type == "pdf"


def test_a_parent_with_too_few_pdfs_reports_a_short_subset_rather_than_padding():
    subset = _subset(_parent(pdfs=7, others=30))
    assert subset.requested_size == 40
    assert subset.selected_size == 7
    assert {e["resource_type"] for e in subset.entries} == {"pdf"}


# --------------------------------------------------------------------------- #
# C/D. Exactly 40, and order-independent
# --------------------------------------------------------------------------- #
def test_exactly_forty_are_selected_when_the_parent_holds_more():
    subset = _subset()
    assert subset.selected_size == 40
    assert len(subset.entries) == 40
    assert len(set(subset.keys())) == 40


def test_the_selection_is_unchanged_when_the_parent_entries_are_shuffled():
    """One benchmark, its entries listed in a different order, draws the same 40.

    The rank is a per-key hash, so nothing about the order the candidates arrive
    in reaches it. The manifest keeps its identity here (the fingerprint is left
    alone) because the parent's canonical order IS part of what that fingerprint
    binds — a re-ordered cohort is a different cohort, and the test below says so.
    """
    parent = _parent()
    shuffled = list(parent.entries)
    random.Random(7).shuffle(shuffled)
    reordered = _parent(entries=shuffled)
    object.__setattr__(reordered, "selection_sha256", parent.selection_sha256)

    def picked(m):
        return [e["comparison_key"]
                for e in calibration.select_calibration_entries(m, size=40)]

    assert len(picked(parent)) == 40
    assert picked(parent) == picked(reordered)


def test_the_ranks_are_a_contiguous_zero_based_ordering():
    subset = _subset()
    assert [e["subset_rank"] for e in subset.entries] == list(range(40))


def test_each_entry_keeps_its_position_in_the_parent_cohort():
    parent = _parent()
    positions = {e["comparison_key"]: i for i, e in enumerate(parent.entries)}
    for entry in _subset(parent).entries:
        assert entry["parent_rank"] == positions[entry["comparison_key"]]


# --------------------------------------------------------------------------- #
# E/F. Nothing about the file's STATE may move the selection
# --------------------------------------------------------------------------- #
def test_fetch_state_on_the_parent_entries_cannot_alter_the_selection():
    plain = _parent()
    stateful = _parent(
        entries=[
            {**e, "fetch_status": "fetched" if i % 3 else "pending",
             "content_sha256": f"{i:064x}"}
            for i, e in enumerate(plain.entries)
        ]
    )
    assert _subset(stateful).keys() == _subset(plain).keys()


def test_quality_verdicts_on_the_parent_entries_cannot_alter_the_selection():
    plain = _parent()
    judged = _parent(
        entries=[
            {**e, "status": "suspicious", "legacy_line_ratio": i / 100,
             "char_count": i * 1000}
            for i, e in enumerate(plain.entries)
        ]
    )
    assert _subset(judged).keys() == _subset(plain).keys()


def test_the_rank_is_bound_to_the_parent_fingerprint_not_a_free_seed():
    """Two cohorts cannot share a calibration ordering: the parent fingerprint is
    in the pre-image, so a different benchmark draws a different 40.

    Same candidate files on both sides, drawn under a different sampler seed — so
    the only thing that moved is the parent's identity.
    """
    a = _parent(seed="phase6a-v1")
    b = _parent(seed="phase6a-v2")
    assert a.keys() == b.keys()
    assert a.selection_sha256 != b.selection_sha256
    assert _subset(a).keys() != _subset(b).keys()


# --------------------------------------------------------------------------- #
# G. A parent that has moved is refused
# --------------------------------------------------------------------------- #
def test_a_parent_whose_own_fingerprint_does_not_verify_is_refused():
    broken = _parent()
    object.__setattr__(broken, "selection_sha256", "0" * 64)
    with pytest.raises(ValueError, match="parent"):
        _subset(broken)


def test_a_parent_that_is_not_the_expected_one_is_refused():
    with pytest.raises(ValueError, match="parent"):
        _subset(expect_parent_sha256="1" * 64)


def test_the_expected_parent_fingerprint_is_accepted_when_it_matches():
    parent = _parent()
    subset = _subset(parent, expect_parent_sha256=parent.selection_sha256)
    assert subset.parent_selection_sha256 == parent.selection_sha256


# --------------------------------------------------------------------------- #
# H. Duplicates collapse
# --------------------------------------------------------------------------- #
def test_a_key_named_twice_by_the_parent_enters_the_subset_once():
    entries = [_entry(PDF.format(i)) for i in range(50)]
    entries.append(_entry(PDF.format(0)))          # the same file again
    subset = _subset(_parent(entries=entries))
    assert len(subset.keys()) == len(set(subset.keys())) == 40


# --------------------------------------------------------------------------- #
# I/J. The fingerprint
# --------------------------------------------------------------------------- #
def test_the_fingerprint_is_stable_across_volatile_values():
    parent = _parent()
    a = _subset(parent, generated_at="2026-08-15T00:00:00+00:00")
    b = _subset(parent, generated_at="2027-01-01T12:34:56+00:00")
    assert a.subset_selection_sha256 == b.subset_selection_sha256


def test_the_fingerprint_verifies_against_the_subsets_own_contents():
    verification = calibration.verify_subset(_subset())
    assert verification.ok, verification


def test_changing_one_selected_key_changes_the_fingerprint():
    subset = _subset()
    edited = list(subset.entries)
    edited[3] = {**edited[3], "comparison_key": PDF.format(999)}
    object.__setattr__(subset, "entries", tuple(edited))
    verification = calibration.verify_subset(subset)
    assert not verification.ok
    assert verification.reason == "fingerprint_mismatch"


def test_a_subset_with_no_fingerprint_recorded_does_not_verify():
    subset = _subset()
    object.__setattr__(subset, "subset_selection_sha256", "")
    assert calibration.verify_subset(subset).reason == "no_fingerprint_recorded"


def test_the_parent_fingerprint_is_part_of_the_subset_fingerprint():
    a = _parent()
    b = _parent(entries=list(a.entries) + [_entry(PDF.format(500))])
    assert _subset(a).subset_selection_sha256 != _subset(b).subset_selection_sha256


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def test_it_round_trips_through_disk_unchanged(tmp_path):
    original = _subset()
    path = tmp_path / "calibration.json"
    calibration.write_subset(original, path)
    assert calibration.read_subset(path) == original


def test_an_unknown_schema_version_is_refused_rather_than_half_read(tmp_path):
    path = tmp_path / "calibration.json"
    calibration.write_subset(_subset(), path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = "calibration-99"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="calibration-99"):
        calibration.read_subset(path)


def test_every_entry_carries_the_metadata_the_report_breaks_down_by():
    for entry in _subset().entries:
        assert set(entry) >= {
            "comparison_key", "subset_rank", "parent_rank", "year", "cohort",
            "document_type", "resource_type", "owner",
        }


def test_the_cohort_comes_from_the_parents_own_stratum():
    assert {e["cohort"] for e in _subset().entries} == {"2023-2026"}


# --------------------------------------------------------------------------- #
# K. Freeze protection
# --------------------------------------------------------------------------- #
def test_writing_over_an_existing_subset_is_refused(tmp_path):
    path = tmp_path / "frozen.json"
    calibration.write_new_subset(_subset(), path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="ONCE"):
        calibration.write_new_subset(_subset(_parent(pdfs=45)), path)
    assert path.read_text(encoding="utf-8") == before


def test_overwriting_deliberately_reports_the_fingerprint_it_replaced(tmp_path):
    path = tmp_path / "frozen.json"
    first = _subset()
    calibration.write_new_subset(first, path)
    second = _subset(_parent(pdfs=45))
    previous = calibration.write_new_subset(second, path, overwrite=True)
    assert previous == first.subset_selection_sha256
    assert calibration.read_subset(path).subset_selection_sha256 == \
        second.subset_selection_sha256


# --------------------------------------------------------------------------- #
# No network
# --------------------------------------------------------------------------- #
def test_freezing_a_calibration_subset_makes_no_http_request(tmp_path, monkeypatch):
    import httpx

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("the calibration subset is drawn from a JSON file")

    for name in ("get", "request", "stream", "post", "head"):
        monkeypatch.setattr(httpx, name, explode, raising=False)
    monkeypatch.setattr(httpx, "Client", explode)
    monkeypatch.setattr(httpx, "AsyncClient", explode)

    calibration.write_new_subset(_subset(), tmp_path / "frozen.json")


# --------------------------------------------------------------------------- #
# The command that freezes and verifies it
# --------------------------------------------------------------------------- #
def _script():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "nrb_calibrate.py"
    spec = importlib.util.spec_from_file_location("nrb_calibrate_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cli(argv):
    import asyncio

    return asyncio.run(_script().main(argv))


@pytest.fixture()
def parent_file(tmp_path):
    path = tmp_path / "manifest.json"
    manifest_module.write_manifest(_parent(), path)
    return path


def test_the_command_refuses_to_run_without_a_mode():
    assert _cli([]) == 2


def test_the_command_refuses_two_modes_at_once(tmp_path, parent_file):
    assert _cli(["--freeze", "--subset", str(tmp_path / "s.json")]) == 2


def test_freezing_writes_the_subset_and_prints_both_fingerprints(
    tmp_path, parent_file, capsys
):
    out = tmp_path / "cal.json"
    assert _cli(["--freeze", "--manifest", str(parent_file), "--out", str(out)]) == 0

    subset = calibration.read_subset(out)
    assert subset.selected_size == 40
    printed = capsys.readouterr().out
    assert subset.parent_selection_sha256 in printed
    assert subset.subset_selection_sha256 in printed


def test_freezing_refuses_to_replace_a_committed_subset(tmp_path, parent_file):
    out = tmp_path / "cal.json"
    argv = ["--freeze", "--manifest", str(parent_file), "--out", str(out)]
    assert _cli(argv) == 0
    before = out.read_text(encoding="utf-8")
    assert _cli(argv) == 2
    assert out.read_text(encoding="utf-8") == before


def test_freezing_against_the_wrong_parent_is_refused(tmp_path, parent_file):
    assert _cli([
        "--freeze", "--manifest", str(parent_file),
        "--out", str(tmp_path / "cal.json"), "--expect-parent", "9" * 64,
    ]) == 2
    assert not (tmp_path / "cal.json").exists()


def test_verifying_a_frozen_subset_checks_it_and_its_parent(tmp_path, parent_file):
    out = tmp_path / "cal.json"
    assert _cli(["--freeze", "--manifest", str(parent_file), "--out", str(out)]) == 0
    assert _cli(["--verify", str(out), "--manifest", str(parent_file)]) == 0


def test_verifying_an_edited_subset_fails(tmp_path, parent_file, capsys):
    out = tmp_path / "cal.json"
    _cli(["--freeze", "--manifest", str(parent_file), "--out", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["entries"][0]["comparison_key"] = PDF.format(4242)
    out.write_text(json.dumps(payload), encoding="utf-8")

    assert _cli(["--verify", str(out), "--manifest", str(parent_file)]) == 1
    assert "fingerprint_mismatch" in capsys.readouterr().out


def test_verifying_a_subset_drawn_from_another_benchmark_fails(tmp_path, parent_file):
    out = tmp_path / "cal.json"
    _cli(["--freeze", "--manifest", str(parent_file), "--out", str(out)])
    other = tmp_path / "other.json"
    manifest_module.write_manifest(_parent(seed="somebody-elses"), other)

    assert _cli(["--verify", str(out), "--manifest", str(other)]) == 1


# --------------------------------------------------------------------------- #
# The committed artifact
# --------------------------------------------------------------------------- #
def _canonical_paths():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "docs" / "nrb"
    return root / "phase6a-docling-calibration.json", root / "phase6a-manifest.json"


def test_the_committed_calibration_subset_is_intact():
    """The frozen calibration slice, guarded as a file rather than as a memory.

    Every pypdf-vs-Docling number Phase 6A publishes is computed over exactly
    these 40 keys. An edit to the file — or a change to the selection rule's
    canonical serialization — must fail here, not be discovered when two runs stop
    agreeing. Needs no database, no network and no Docling.
    """
    subset_path, manifest_path = _canonical_paths()
    subset = calibration.read_subset(subset_path)
    parent = manifest_module.read_manifest(manifest_path)

    assert subset.version == "calibration-1"
    assert subset.purpose == "docling-calibration"
    assert subset.subset_algorithm_version == "docling-calibration-v1"
    assert subset.resource_type == "pdf"
    assert subset.requested_size == 40
    assert subset.selected_size == 40
    assert len(set(subset.keys())) == 40

    assert subset.parent_selection_sha256 == (
        "1ae297dba1c33c7db9976f817806f6666371695a31e1f424d046993d581a1312"
    )
    assert subset.subset_selection_sha256 == (
        "81d5979ffeee6fbede375917fa6e3de09cb8f0475a397a21b7ad52fa233d90f5"
    )
    assert calibration.verify_subset(subset).ok
    assert calibration.verify_against_parent(subset, parent).ok


def test_the_committed_subset_would_be_drawn_again_from_the_committed_benchmark():
    """Reproducible from the parent alone — no database, no fetch state.

    This is the property that lets someone else re-derive the calibration slice
    from the two committed files and get the same 40.
    """
    subset_path, manifest_path = _canonical_paths()
    frozen = calibration.read_subset(subset_path)
    redrawn = calibration.build_subset(
        manifest_module.read_manifest(manifest_path),
        parent_manifest_path=str(manifest_path),
        size=40,
        generated_at="whenever",
    )
    assert redrawn.subset_selection_sha256 == frozen.subset_selection_sha256
    assert redrawn.keys() == frozen.keys()


def test_every_committed_subset_entry_is_a_pdf_from_the_benchmark():
    subset_path, manifest_path = _canonical_paths()
    subset = calibration.read_subset(subset_path)
    parent = manifest_module.read_manifest(manifest_path)
    by_key = {e["comparison_key"]: e for e in parent.entries}

    for entry in subset.entries:
        parent_entry = by_key[entry["comparison_key"]]
        assert parent_entry["resource_type"] == "pdf"
        assert entry["year"] == parent_entry["year"]
        assert entry["document_type"] == parent_entry["document_type"]

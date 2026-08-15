"""The benchmark manifest — the format, the draw, the fingerprint, the freeze.

Three groups, in the order they were built:

* the FILE FORMAT (read, write, round-trip, refuse) — shipped with the fetch
  path, which had to be able to *read* a manifest before anything could draw one;
* the DRAW — `build_manifest`, the canonical entry order and `selection_sha256`,
  the fingerprint that says whether two people are profiling the same 400 files;
* the FREEZE — `scripts/nrb_sample.py`, which refuses to overwrite a cohort that
  already exists and prints both fingerprints when it is told to anyway.

No database, no network: a manifest is a JSON file naming catalog keys, and the
tests assert that by making any HTTP call explode.
"""

from __future__ import annotations

import json

import pytest

from app.nrb import manifest as manifest_module

KEY = "https://www.nrb.org.np/contents/uploads/2026/08/circular-{}.pdf"


def _manifest(count: int = 3, **overrides) -> manifest_module.Manifest:
    fields: dict = dict(
        version=manifest_module.MANIFEST_VERSION,
        drawn_at="2026-08-15T00:00:00+00:00",
        requested=count,
        shortfall=0,
        sampler={"size": count, "floor": 5, "max_cohort_share": 0.30},
        catalog_counts={"files": 18266},
        strata=({"cohort": "2023-2026", "selected": count},),
        notes=(),
        entries=tuple(
            {
                "comparison_key": KEY.format(i),
                "year": 2024,
                "document_type": "circular",
                "resource_type": "pdf",
                "owner": "bfr",
                "stratum": "2023-2026/circular/pdf",
            }
            for i in range(count)
        ),
    )
    fields.update(overrides)
    return manifest_module.Manifest(**fields)


def test_keys_are_the_exact_comparison_keys_in_entry_order():
    assert _manifest().keys() == tuple(KEY.format(i) for i in range(3))


def test_it_round_trips_through_disk_unchanged(tmp_path):
    original = _manifest()
    path = tmp_path / "manifest.json"
    manifest_module.write_manifest(original, path)
    assert manifest_module.read_manifest(path) == original


def test_the_file_is_human_readable_and_rewrites_byte_identically(tmp_path):
    path = tmp_path / "manifest.json"
    manifest_module.write_manifest(_manifest(), path)
    first = path.read_text(encoding="utf-8")
    manifest_module.write_manifest(_manifest(), path)
    assert path.read_text(encoding="utf-8") == first
    assert json.loads(first)["version"] == manifest_module.MANIFEST_VERSION


def test_devanagari_keys_survive_the_round_trip(tmp_path):
    """NRB's filenames are Devanagari. A manifest full of \\uXXXX escapes is a
    benchmark definition nobody can read, and the key must come back byte-exact —
    it is matched against `nrb_files.comparison_key`."""
    key = "https://www.nrb.org.np/contents/uploads/आगलागी-२०७४.pdf"
    original = _manifest(1, entries=({"comparison_key": key, "year": 2024},))
    path = tmp_path / "m.json"
    manifest_module.write_manifest(original, path)
    assert "आगलागी" in path.read_text(encoding="utf-8")
    assert manifest_module.read_manifest(path).keys() == (key,)


def test_reading_a_manifest_with_an_unknown_version_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": "manifest-99", "entries": []}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        manifest_module.read_manifest(path)


def test_reading_a_manifest_over_the_cap_is_refused(tmp_path):
    """A manifest is a benchmark cohort, not a way to smuggle `--all` past the
    scope-is-required rule."""
    path = tmp_path / "huge.json"
    path.write_text(
        json.dumps(
            {
                "version": manifest_module.MANIFEST_VERSION,
                "entries": [
                    {"comparison_key": KEY.format(i)}
                    for i in range(manifest_module.MANIFEST_MAX_KEYS + 1)
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cap"):
        manifest_module.read_manifest(path)


def test_an_entry_without_a_key_is_refused(tmp_path):
    """Half-reading a benchmark definition silently redefines the benchmark."""
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps(
            {
                "version": manifest_module.MANIFEST_VERSION,
                "entries": [{"comparison_key": KEY.format(0)}, {"year": 2024}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="comparison_key"):
        manifest_module.read_manifest(path)


def test_duplicate_keys_are_reported_but_collapse_to_one_selection(tmp_path):
    """A hand-edited manifest can name the same file twice. It is one file: the
    keys are deduplicated (order-stable) so nothing is downloaded twice, and the
    duplicate count is kept so the discrepancy is visible rather than silent."""
    original = _manifest(1, entries=(
        {"comparison_key": KEY.format(0)},
        {"comparison_key": KEY.format(0)},
        {"comparison_key": KEY.format(1)},
    ))
    assert original.keys() == (KEY.format(0), KEY.format(1))
    assert original.duplicate_entries == 1


def test_the_cap_matches_the_one_the_catalog_enforces():
    """Two modules, one bound. A manifest the format accepts and the query then
    refuses would fail at the end of a load rather than at the start."""
    from app.nrb import catalog

    assert manifest_module.MANIFEST_MAX_KEYS == catalog.MANIFEST_MAX_KEYS


# --------------------------------------------------------------------------- #
# Drawing a manifest — the cohort is frozen here, before anything is fetched
# --------------------------------------------------------------------------- #
from app.nrb import sampling  # noqa: E402

ROWS = [
    {
        "comparison_key": f"https://www.nrb.org.np/uploads/doc-{i}.pdf",
        "resource_type": "pdf" if i % 3 else "spreadsheet",
        "fetch_status": "pending",
        "content_sha256": None,
        "document_type": "circular" if i % 2 else "directive",
        "owner": "bfr" if i % 4 else "red",
        # All four year cohorts, so a 40-file draw is feasible under the default
        # 30% cohort cap (4 x 12 = 48). 2019 is deliberately the largest.
        "year": (2019, 2024, 2019, 2021, 2015)[i % 5],
    }
    for i in range(200)
]


def _drawn(size=40, rows=None, **kwargs):
    rows = ROWS if rows is None else rows
    sample = sampling.stratified_sample(rows, size=size, **kwargs)
    return manifest_module.build_manifest(
        sample,
        drawn_at="2026-08-15T00:00:00+00:00",
        catalog_counts={"files": len(rows), "fetched": 0},
    )


def test_every_entry_carries_the_exact_key_and_its_stratum():
    entry = _drawn().entries[0]
    assert entry["comparison_key"].startswith("https://")
    for field in ("year", "document_type", "resource_type", "owner",
                  "sampling_stratum"):
        assert field in entry


def test_the_manifest_records_exactly_how_it_was_drawn():
    m = _drawn(seed="phase6a-v1", floor=5)
    assert m.requested == 40
    assert m.selected == len(m.entries) == 40
    assert m.algorithm_version == sampling.ALGORITHM_VERSION
    assert m.seed == "phase6a-v1"
    assert m.sampler["floor"] == 5
    assert m.sampler["max_cohort_share"] == "3/10"
    assert m.sampler["algorithm_version"] == sampling.ALGORITHM_VERSION
    assert m.drawn_at == "2026-08-15T00:00:00+00:00"
    assert m.catalog_counts["files"] == 200
    assert m.diagnostics["selected"] == 40


def test_the_entries_are_written_in_canonical_rank_order():
    """Not database order, not stratum order. The order the fingerprint is taken
    over has to be a property of the cohort itself."""
    m = _drawn()
    ranks = [
        sampling.rank_for(m.algorithm_version, m.seed, key) for key in m.keys()
    ]
    assert ranks == sorted(ranks)


def test_a_drawn_manifest_round_trips_through_disk_unchanged(tmp_path):
    """Test M. Keys, parameters, strata and fingerprint all survive."""
    original = _drawn()
    path = tmp_path / "manifest.json"
    manifest_module.write_manifest(original, path)
    loaded = manifest_module.read_manifest(path)
    assert loaded == original
    assert loaded.keys() == original.keys()
    assert loaded.sampler == original.sampler
    assert loaded.strata == original.strata
    assert loaded.selection_sha256 == original.selection_sha256
    assert manifest_module.verify_manifest(loaded).ok


def test_a_drawn_manifest_over_the_cap_is_refused():
    rows = [
        {"comparison_key": f"https://www.nrb.org.np/uploads/{i}.pdf",
         "resource_type": "pdf", "document_type": "circular",
         "owner": "bfr", "year": 2024}
        for i in range(manifest_module.MANIFEST_MAX_KEYS + 10)
    ]
    sample = sampling.stratified_sample(
        rows, size=manifest_module.MANIFEST_MAX_KEYS + 10, max_cohort_share=1.0
    )
    with pytest.raises(ValueError, match="cap"):
        manifest_module.build_manifest(sample, drawn_at="x", catalog_counts={})


def test_a_shortfall_and_its_notes_are_carried_into_the_manifest():
    m = _drawn(size=5000)
    assert m.shortfall > 0
    assert m.notes
    assert m.diagnostics["incomplete_reason"]


def test_devanagari_keys_survive_a_drawn_manifest(tmp_path):
    rows = [{
        "comparison_key": "https://www.nrb.org.np/uploads/आगलागी-२०७४.pdf",
        "resource_type": "pdf", "document_type": "circular",
        "owner": "bfr", "year": 2024,
    }]
    m = _drawn(size=1, rows=rows)
    path = tmp_path / "m.json"
    manifest_module.write_manifest(m, path)
    assert "आगलागी" in path.read_text(encoding="utf-8")
    reloaded = manifest_module.read_manifest(path)
    assert reloaded.keys() == m.keys()
    assert manifest_module.verify_manifest(reloaded).ok


# --------------------------------------------------------------------------- #
# L. Fingerprint stability
# --------------------------------------------------------------------------- #
def test_the_same_logical_sample_fingerprints_identically():
    assert _drawn().selection_sha256 == _drawn().selection_sha256


def test_shuffled_input_rows_fingerprint_identically():
    import random

    shuffled = list(ROWS)
    random.Random(3).shuffle(shuffled)
    assert _drawn().selection_sha256 == _drawn(rows=shuffled).selection_sha256


def test_changing_one_comparison_key_changes_the_fingerprint():
    original = _drawn()
    keys = list(original.keys())
    keys[0] = keys[0] + "-edited"
    tampered = manifest_module.compute_selection_sha256(
        manifest_version=original.version,
        algorithm_version=original.algorithm_version,
        seed=original.seed,
        parameters=original.sampler,
        keys=keys,
    )
    assert tampered != original.selection_sha256


def test_the_fingerprint_ignores_the_timestamp_and_the_catalog_counts():
    a = _drawn()
    b = manifest_module.build_manifest(
        sampling.stratified_sample(ROWS, size=40),
        drawn_at="2099-01-01T00:00:00+00:00",
        catalog_counts={"files": 999999},
    )
    assert a.selection_sha256 == b.selection_sha256


def test_a_different_seed_fingerprints_differently():
    assert _drawn(seed="a").selection_sha256 != _drawn(seed="b").selection_sha256


def test_a_different_sampler_parameter_fingerprints_differently():
    assert _drawn(floor=5).selection_sha256 != _drawn(floor=1).selection_sha256


def test_an_edited_manifest_fails_verification(tmp_path):
    m = _drawn()
    path = tmp_path / "m.json"
    manifest_module.write_manifest(m, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"][0]["comparison_key"] += "-tampered"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = manifest_module.verify_manifest(manifest_module.read_manifest(path))
    assert result.ok is False
    assert result.reason == "fingerprint_mismatch"


def test_a_manifest_with_no_recorded_fingerprint_says_so_rather_than_passing():
    result = manifest_module.verify_manifest(_manifest())
    assert result.ok is False
    assert result.reason == "no_fingerprint_recorded"


# --------------------------------------------------------------------------- #
# N. The freeze guard
# --------------------------------------------------------------------------- #
def test_writing_over_an_existing_manifest_is_refused(tmp_path):
    path = tmp_path / "frozen.json"
    first = _drawn(seed="a")
    manifest_module.write_new_manifest(first, path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="ONCE"):
        manifest_module.write_new_manifest(_drawn(seed="b"), path)
    assert path.read_text(encoding="utf-8") == before      # untouched


def test_overwriting_deliberately_reports_the_fingerprint_it_replaced(tmp_path):
    path = tmp_path / "frozen.json"
    first = _drawn(seed="a")
    manifest_module.write_new_manifest(first, path)
    second = _drawn(seed="b")
    previous = manifest_module.write_new_manifest(second, path, overwrite=True)
    assert previous == first.selection_sha256
    assert manifest_module.read_manifest(path).selection_sha256 == \
        second.selection_sha256


def test_writing_a_new_manifest_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "m.json"
    assert manifest_module.write_new_manifest(_drawn(), path) is None
    assert path.exists()


# --------------------------------------------------------------------------- #
# O. No network
# --------------------------------------------------------------------------- #
def test_drawing_and_writing_a_manifest_makes_no_http_request(tmp_path, monkeypatch):
    """Task 7 is catalog-only. Nothing here may reach NRB — the cohort is frozen
    BEFORE a single byte is downloaded, which is the whole point of drawing it
    from the catalog rather than from what happens to be on disk."""
    import httpx

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("Task 7 made an HTTP request")

    monkeypatch.setattr(httpx, "AsyncClient", explode)
    monkeypatch.setattr(httpx, "Client", explode)
    monkeypatch.setattr(httpx, "get", explode)
    monkeypatch.setattr(httpx, "request", explode)

    m = _drawn()
    path = tmp_path / "m.json"
    manifest_module.write_new_manifest(m, path)
    assert manifest_module.verify_manifest(
        manifest_module.read_manifest(path)
    ).ok


# --------------------------------------------------------------------------- #
# The command that freezes a cohort
# --------------------------------------------------------------------------- #
def _script():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "nrb_sample.py"
    spec = importlib.util.spec_from_file_location("nrb_sample_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_cli(module, argv, monkeypatch, rows=None):
    """Drive `main` with the catalog stubbed out. The database is not this
    command's subject — the allocation and the freeze guard are."""
    import asyncio

    async def fake_load(args):
        return (ROWS if rows is None else rows), {"files": 200}

    monkeypatch.setattr(module, "_load_catalog", fake_load)
    return asyncio.run(module.main(argv))


def test_the_defaults_are_the_documented_provisional_policy():
    args = _script()._parse_args([])
    assert args.size == 400
    assert args.seed == sampling.DEFAULT_SEED
    assert args.floor == sampling.DEFAULT_FLOOR
    assert args.max_cohort_share == "0.30"
    assert args.year_2019_cap is None


def test_the_2019_cap_and_the_generic_cohort_cap_both_reach_the_sampler():
    script = _script()
    args = script._parse_args(["--year-2019-cap", "80", "--cohort-cap", "<=2018=40"])
    assert script._cohort_caps(args) == {"2019": 80, "<=2018": 40}


def test_a_malformed_cohort_cap_is_refused_rather_than_ignored():
    script = _script()
    args = script._parse_args(["--cohort-cap", "2019"])
    with pytest.raises(ValueError, match="COHORT=N"):
        script._cohort_caps(args)


def test_the_command_refuses_to_run_without_somewhere_to_write(monkeypatch):
    assert _run_cli(_script(), ["--size", "20"], monkeypatch) == 2


def test_a_dry_run_draws_and_writes_nothing(tmp_path, monkeypatch, capsys):
    module = _script()
    assert _run_cli(module, ["--size", "20", "--dry-run"], monkeypatch) == 0
    assert list(tmp_path.iterdir()) == []
    assert "benchmark cohort" in capsys.readouterr().out


def test_writing_a_cohort_freezes_it_and_a_second_run_is_refused(tmp_path, monkeypatch):
    """Test N. The existing manifest must survive the refused run byte-for-byte."""
    module = _script()
    out = tmp_path / "cohort.json"
    assert _run_cli(module, ["--size", "20", "--out", str(out)], monkeypatch) == 0
    frozen = out.read_text(encoding="utf-8")

    assert _run_cli(
        module, ["--size", "20", "--seed", "other", "--out", str(out)], monkeypatch
    ) == 2
    assert out.read_text(encoding="utf-8") == frozen


def test_overwriting_is_possible_but_never_silent(tmp_path, monkeypatch, capsys):
    module = _script()
    out = tmp_path / "cohort.json"
    _run_cli(module, ["--size", "20", "--out", str(out)], monkeypatch)
    first = manifest_module.read_manifest(out).selection_sha256
    capsys.readouterr()

    assert _run_cli(
        module,
        ["--size", "20", "--seed", "other", "--out", str(out), "--overwrite"],
        monkeypatch,
    ) == 0
    second = manifest_module.read_manifest(out).selection_sha256
    err = capsys.readouterr().err
    assert first != second
    assert first in err and second in err          # both printed, side by side


def test_a_written_cohort_verifies_and_a_tampered_one_does_not(tmp_path, monkeypatch):
    module = _script()
    out = tmp_path / "cohort.json"
    _run_cli(module, ["--size", "20", "--out", str(out)], monkeypatch)

    import asyncio

    assert asyncio.run(module.main(["--verify", str(out)])) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["entries"] = payload["entries"][:-1]
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert asyncio.run(module.main(["--verify", str(out)])) == 1


def test_verification_needs_no_catalog_and_no_network(tmp_path, monkeypatch):
    """`--verify` answers "is this file internally consistent", which is a
    question about the file. It must not resample, so it must not need a
    database — that is what makes it runnable in CI."""
    import asyncio

    import httpx

    module = _script()
    out = tmp_path / "cohort.json"
    _run_cli(module, ["--size", "20", "--out", str(out)], monkeypatch)

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("verification touched the network")

    monkeypatch.setattr(httpx, "AsyncClient", explode)
    monkeypatch.setattr(httpx, "Client", explode)

    async def no_catalog(args):      # pragma: no cover - must never run
        raise AssertionError("verification read the catalog")

    monkeypatch.setattr(module, "_load_catalog", no_catalog)
    assert asyncio.run(module.main(["--verify", str(out)])) == 0


def test_a_short_cohort_exits_nonzero_so_it_cannot_pass_unnoticed(tmp_path,
                                                                 monkeypatch):
    module = _script()
    out = tmp_path / "cohort.json"
    assert _run_cli(module, ["--size", "5000", "--out", str(out)], monkeypatch) == 1
    assert manifest_module.read_manifest(out).shortfall > 0


def test_the_cli_makes_no_http_request_at_all(tmp_path, monkeypatch):
    import httpx

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("nrb_sample.py made an HTTP request")

    for name in ("AsyncClient", "Client", "get", "request", "stream"):
        monkeypatch.setattr(httpx, name, explode)

    module = _script()
    out = tmp_path / "cohort.json"
    assert _run_cli(module, ["--size", "20", "--out", str(out)], monkeypatch) == 0
    assert manifest_module.read_manifest(out).selected == 20


# --------------------------------------------------------------------------- #
# The committed cohort itself
# --------------------------------------------------------------------------- #
def _canonical_path():
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[1]
            / "docs" / "nrb" / "phase6a-manifest.json")


@pytest.mark.skipif(not _canonical_path().exists(),
                    reason="the canonical cohort has not been frozen yet")
def test_the_committed_phase6a_cohort_is_intact():
    """The frozen benchmark, guarded as a file rather than as a memory.

    `docs/nrb/phase6a-manifest.json` is now the definition of the Phase 6A
    cohort: every extraction number, the Docling calibration subset and the
    published profile are all computed over exactly these 400 keys. An edit to it
    — or a change to the sampler's canonical serialization — must fail here and
    not be discovered when two runs' numbers stop agreeing.

    Needs no database and no network: the fingerprint is recomputed from the
    file's own contents.
    """
    m = manifest_module.read_manifest(_canonical_path())

    assert m.version == "manifest-2"
    assert m.algorithm_version == sampling.ALGORITHM_VERSION == "nrb-stratified-v1"
    assert m.seed == "phase6a-v1"
    assert m.requested == 400
    assert m.selected == 400
    assert len(m.entries) == 400
    assert len(set(m.keys())) == 400
    assert m.duplicate_entries == 0
    assert m.shortfall == 0

    # The approved policy, spelled out so a silent re-draw under other parameters
    # cannot keep the same filename.
    assert m.sampler["floor"] == 2
    assert m.sampler["cohort_caps"] == {"2019": 120}

    verification = manifest_module.verify_manifest(m)
    assert verification.ok, verification

    # Canonical order is what the fingerprint is taken over.
    ranks = [sampling.rank_for(m.algorithm_version, m.seed, k) for k in m.keys()]
    assert ranks == sorted(ranks)

    # The hard historical cap, re-checked against the entries themselves rather
    # than trusted from the diagnostics block.
    from collections import Counter

    assert Counter(e["year"] for e in m.entries)[2019] <= 120

    # A manifest can only ever name NRB's own host — it selects from the catalog.
    assert {key.split("/")[2] for key in m.keys()} == {"www.nrb.org.np"}

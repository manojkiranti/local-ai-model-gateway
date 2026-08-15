"""The benchmark manifest FILE FORMAT — read, write, round-trip, refuse.

Only the format lives here. Drawing a cohort (`build_manifest`) needs the sampler
and arrives with it in Task 7A; the fetch path needs to *read* a manifest before
anything can draw one, which is why the two halves land separately.

No database, no network: a manifest is a JSON file naming catalog keys.
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

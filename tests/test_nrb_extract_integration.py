"""Phase 6A against real Postgres: which blobs get extracted, and what is stored.

Same harness as `test_nrb_fetch_integration`'s selection tests — every test runs
inside a savepoint-joined transaction that is rolled back, with the nrb_* tables
cleared INSIDE it. The catalog is global with no department to scope a fixture to,
so a test that really committed would rewrite a developer's whole catalog.

Nothing here parses a file: these are the queries around the extraction, and the
blobs they name need not exist on disk.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.nrb import catalog, sampling
from app.nrb.models import FETCH_BLOCKED_HOST, FETCH_FAILED, FETCH_FETCHED
from tests.test_nrb_fetch_integration import UPLOADS, _seed
from tests.test_nrb_sync_integration import _run

VERSION = "native-1"


async def _fetched(
    session, *, name: str, sha: str, owner: str = "bfr",
    section: str = "circular", published=None, **file_fields,
):
    """Seed one file already downloaded, with `sha` as its content hash.

    Status and the content columns move together — `ck_nrb_files_fetched_is_complete`
    refuses a `fetched` row that cannot name its bytes — so they are set in one
    statement right after the insert rather than drifting apart.
    """
    from sqlalchemy import text

    ids = await _seed(
        session, owner=owner, slug=name, section=section, published=published,
        files=[{"name": name, "url": f"{UPLOADS}/{name}.pdf", **file_fields}],
    )
    await session.execute(
        text(
            "UPDATE nrb_files SET fetch_status='fetched', content_sha256=:h,"
            " content_length=1024, storage_key=:k, sniffed_mime='application/pdf'"
            " WHERE id = :i"
        ),
        {"h": sha, "k": f"{sha[:2]}/{sha}.pdf", "i": ids[name]},
    )
    await session.flush()
    return ids[name]


def _row(sha: str, *, status="extracted", reason="clean", version=VERSION, **over):
    row = {
        "content_sha256": sha,
        "extractor_version": version,
        "parser": "pypdf",
        "media_family": "pdf",
        "status": status,
        "reason": reason,
        "warnings": [],
        "page_count": 4,
        "pages_with_text": 4,
        "text_page_coverage": 1.0,
        "median_chars_per_page": 900.0,
        "median_chars_per_text_page": 900.0,
        "char_count": 3600,
        "devanagari_ratio": 0.61,
        "legacy_line_ratio": 0.0,
        "legacy_lines": 0,
        "judged_lines": 80,
        "metrics": {"char_count": 3600},
        "preview": "Nepal Rastra Bank circular",
        "error": None,
        "duration_ms": 120,
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_only_fetched_blobs_are_selected():
    """`pending`, `failed` and `blocked_host` rows have no bytes on disk. They are
    excluded by the status column, not by a WHERE clause someone could forget."""
    async def go(session):
        await _fetched(session, name="done", sha="a" * 64)
        await _seed(session, owner="bfr", slug="p",
                    files=[{"name": "p", "url": f"{UPLOADS}/p.pdf"}])
        await _seed(session, owner="bfr", slug="f", files=[
            {"name": "f", "url": f"{UPLOADS}/f.pdf", "status": FETCH_FAILED}])
        await _seed(session, owner="bfr", slug="b", files=[
            {"name": "b", "url": "http://uat.nrb.org.np/b.pdf",
             "status": FETCH_BLOCKED_HOST, "blocked_reason": "plain http"}])
        return await catalog.select_extract_targets(session, extractor_version=VERSION)

    targets = _run(go)
    assert [t.content_sha256 for t in targets] == ["a" * 64]
    assert targets[0].storage_key == f"aa/{'a' * 64}.pdf"


def test_two_file_rows_sharing_bytes_are_one_extraction():
    """The whole reason `nrb_extractions` is keyed on the hash. Extracting both
    would parse the same PDF twice and write two rows the unique index rejects."""
    async def go(session):
        await _fetched(session, name="one", sha="b" * 64)
        await _fetched(session, name="two", sha="b" * 64)
        return await catalog.select_extract_targets(session, extractor_version=VERSION)

    assert len(_run(go)) == 1


def test_a_blob_already_extracted_at_this_version_is_not_selected_again():
    async def go(session):
        await _fetched(session, name="done", sha="c" * 64)
        await catalog.record_extractions(session, [_row("c" * 64)])
        await session.flush()
        return (
            await catalog.select_extract_targets(session, extractor_version=VERSION),
            await catalog.select_extract_targets(session, extractor_version=VERSION,
                                                 force=True),
            await catalog.select_extract_targets(session, extractor_version="native-2"),
        )

    again, forced, next_version = _run(go)
    assert again == []
    assert len(forced) == 1        # a rule changed but the version has not moved
    assert len(next_version) == 1  # bumping the version is the honest invalidation


def test_selection_can_be_scoped_by_cohort_keys_year_section_and_type():
    async def go(session):
        await _fetched(session, name="circ", sha="d" * 64, section="circular",
                       published=datetime(2019, 3, 1, tzinfo=timezone.utc))
        await _fetched(session, name="stat", sha="e" * 64, section="statistics",
                       published=datetime(2024, 3, 1, tzinfo=timezone.utc),
                       resource_type="spreadsheet", extension="xlsx")
        select = catalog.select_extract_targets
        return {
            "keys": await select(session, extractor_version=VERSION,
                                 keys=[f"{UPLOADS}/stat.pdf"]),
            "year": await select(session, extractor_version=VERSION, years=[2019]),
            "section": await select(session, extractor_version=VERSION,
                                    sections=["statistics"]),
            "type": await select(session, extractor_version=VERSION,
                                 resource_types=["spreadsheet"]),
            "both": await select(session, extractor_version=VERSION,
                                 keys=[f"{UPLOADS}/stat.pdf"], years=[2019]),
        }

    got = _run(go)
    assert [t.content_sha256 for t in got["keys"]] == ["e" * 64]
    assert [t.content_sha256 for t in got["year"]] == ["d" * 64]
    assert [t.content_sha256 for t in got["section"]] == ["e" * 64]
    assert [t.content_sha256 for t in got["type"]] == ["e" * 64]
    # Filters compose (AND), so a cohort key from 2024 asked for as 2019 is nothing.
    assert got["both"] == []


def test_an_oversized_cohort_is_refused_here_too():
    async def go(session):
        keys = [f"{UPLOADS}/{n}.pdf" for n in range(catalog.MANIFEST_MAX_KEYS + 1)]
        with pytest.raises(ValueError, match="cap"):
            await catalog.select_extract_targets(
                session, extractor_version=VERSION, keys=keys
            )
        return True

    assert _run(go) is True


def test_selection_is_ordered_stably_so_a_resumed_pass_continues():
    async def go(session):
        for n, letter in enumerate("0123456789abcdef"):
            await _fetched(session, name=f"f{n}", sha=letter * 64)
        first = await catalog.select_extract_targets(
            session, extractor_version=VERSION, limit=4)
        second = await catalog.select_extract_targets(
            session, extractor_version=VERSION, limit=4)
        return [t.content_sha256 for t in first], [t.content_sha256 for t in second]

    first, second = _run(go)
    assert first == second == sorted(first)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_recording_the_same_blob_twice_replaces_the_verdict_rather_than_failing():
    """A `--force` re-extraction must overwrite its previous answer, and an
    interrupted pass re-recording a blob must be a no-op rather than an error."""
    async def go(session):
        await _fetched(session, name="x", sha="f" * 64)
        await catalog.record_extractions(session, [_row("f" * 64)])
        await catalog.record_extractions(session, [
            _row("f" * 64, status="suspicious", reason="legacy_font_suspected",
                 legacy_line_ratio=0.94, legacy_lines=75, judged_lines=80,
                 devanagari_ratio=0.0, median_chars_per_text_page=880.0)
        ])
        await session.flush()
        from sqlalchemy import text
        return (await session.execute(text(
            "SELECT status, reason, legacy_line_ratio, legacy_lines,"
            " median_chars_per_text_page, count(*) OVER () FROM nrb_extractions"
        ))).all()

    rows = _run(go)
    assert len(rows) == 1                       # replaced, not duplicated
    status, reason, ratio, lines, median, total = rows[0]
    assert (status, reason) == ("suspicious", "legacy_font_suspected")
    # Every measure moves, not just the ones an upsert happened to list: a stale
    # severity column under a new verdict would misreport the OCR cohort's size.
    assert ratio == pytest.approx(0.94) and lines == 75
    assert median == pytest.approx(880.0)


def test_a_second_version_of_the_same_blob_is_a_second_row():
    async def go(session):
        await _fetched(session, name="x", sha="1" * 64)
        await catalog.record_extractions(session, [
            _row("1" * 64), _row("1" * 64, version="native-2", status="needs_ocr",
                                 reason="no_text_layer"),
        ])
        await session.flush()
        return await catalog.extraction_counts(session, extractor_version=VERSION)

    counts = _run(go)
    assert counts["blobs_extracted"] == 1        # at THIS version
    assert counts["stale"] == 1                  # the other version's row
    assert counts["extracted"] == 1


def test_the_counts_report_the_work_by_blob_and_by_verdict():
    async def go(session):
        await _fetched(session, name="a", sha="2" * 64)
        await _fetched(session, name="b", sha="3" * 64)
        await _fetched(session, name="dup", sha="3" * 64)     # same bytes
        await catalog.record_extractions(session, [
            _row("2" * 64),
            _row("3" * 64, status="needs_ocr", reason="no_text_layer"),
        ])
        await session.flush()
        return await catalog.extraction_counts(session, extractor_version=VERSION)

    counts = _run(go)
    assert counts["blobs_fetched"] == 2          # 3 rows, 2 distinct blobs
    assert counts["blobs_extracted"] == 2
    assert counts["extracted"] == 1 and counts["needs_ocr"] == 1


def test_count_unfetched_says_how_much_of_a_cohort_is_still_missing():
    """Every percentage in the profile is over what WAS extracted, so a cohort of
    400 that is really 380 has to say so."""
    async def go(session):
        await _fetched(session, name="here", sha="4" * 64)
        await _seed(session, owner="bfr", slug="later",
                    files=[{"name": "later", "url": f"{UPLOADS}/later.pdf"}])
        keys = [f"{UPLOADS}/here.pdf", f"{UPLOADS}/later.pdf",
                "https://www.nrb.org.np/never-synced.pdf"]
        return (
            await catalog.count_unfetched(session, keys),
            await catalog.count_unfetched(session, []),
        )

    missing, empty = _run(go)
    assert missing == 2      # one pending, one the catalog has never seen
    assert empty == 0


# --------------------------------------------------------------------------- #
# The sampler's input
# --------------------------------------------------------------------------- #
def test_sample_rows_carry_the_stratification_keys_for_unfetched_files():
    """Sampling happens BEFORE anything is fetched, so the identity is the
    comparison key and `content_sha256` is still NULL."""
    async def go(session):
        await _seed(session, owner="bfr", slug="a", section="circular",
                    published=datetime(2019, 5, 1, tzinfo=timezone.utc),
                    files=[{"name": "a", "url": f"{UPLOADS}/a.pdf"}])
        return await catalog.load_sample_rows(session)

    rows = _run(go)
    assert len(rows) == 1
    row = rows[0]
    assert row["comparison_key"] == f"{UPLOADS}/a.pdf"
    assert row["year"] == 2019
    assert row["document_type"] == "circular"
    assert row["resource_type"] == "pdf"
    assert row["owner"] == "bfr"
    assert row["content_sha256"] is None
    assert row["fetch_status"] == "pending"


def test_a_file_published_by_two_sources_is_one_sampling_candidate():
    """Otherwise the 42 multiply-referenced files would be twice as likely to be
    drawn as any other.

    The loader returns one row per (file, active source) — the two sources here
    disagree about the owner, and both disagreeing values have to reach the
    sampler for it to resolve them by rule. Collapsing in SQL would mean
    collapsing by `min(source_id)`, i.e. by the order REST paged the post types.
    `sampling.build_candidates` is what makes it one candidate.
    """
    async def go(session):
        from sqlalchemy import text

        ids = await _seed(session, owner="bfr", slug="one",
                          files=[{"name": "shared", "url": f"{UPLOADS}/shared.pdf"}])
        other = (await session.execute(text(
            "INSERT INTO nrb_sources (page_url, url_key, metadata_status,"
            " metadata_hash, owner, document_type) VALUES"
            " ('https://www.nrb.org.np/psd/p/', 'https://www.nrb.org.np/psd/p',"
            " 'rest', :h, 'psd', 'circular') RETURNING id"), {"h": "1" * 64})
        ).scalar_one()
        await session.execute(text(
            "INSERT INTO nrb_source_files (source_id, file_id, ordinal,"
            " relationship_type) VALUES (:s, :f, 0, 'primary')"),
            {"s": other, "f": ids["shared"]})
        await session.flush()
        return await catalog.load_sample_rows(session)

    rows = _run(go)
    assert len(rows) == 2                     # one per (file, source) association
    assert {row["comparison_key"] for row in rows} == {f"{UPLOADS}/shared.pdf"}

    candidates = sampling.build_candidates(rows)
    assert len(candidates) == 1               # …but ONE candidate, one download
    assert candidates[0].source_rows == 2
    assert candidates[0].owners == ("bfr", "psd")   # every owner kept, none guessed


def test_a_file_only_reachable_from_a_deactivated_source_is_not_sampled():
    async def go(session):
        await _seed(session, owner="bfr", slug="gone", active=False,
                    files=[{"name": "gone", "url": f"{UPLOADS}/gone.pdf"}])
        return await catalog.load_sample_rows(session)

    assert _run(go) == []

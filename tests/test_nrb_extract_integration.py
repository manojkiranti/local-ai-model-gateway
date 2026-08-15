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


# --------------------------------------------------------------------------- #
# A whole extraction pass (real commits, owner-scoped, cleaned up)
#
# `run_extract` manages its own sessions and really commits, so these tests do
# NOT use the rolled-back harness above. They scope to a unique owner code and a
# unique blob store, and delete only their own rows — the same isolation rule
# `test_nrb_fetch_integration`'s pass tests follow, and for the same reason: a
# test that selected a developer's real catalog would write extraction rows over
# it.
# --------------------------------------------------------------------------- #
import asyncio
import hashlib
import uuid

from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.nrb import extract as extract_mod
from app.nrb import extraction as extraction_mod
from app.nrb import filestore, profile, report
from app.nrb.quality import STATUS_FAILED
from tests.test_nrb_sync_integration import _engine, _skip_if_no_db


def _pdf_bytes(lines):
    """A real PDF with real text, built in-process — no committed binaries."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    for line in lines:
        pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


ENGLISH_PDF = _pdf_bytes([
    "Nepal Rastra Bank issued a circular to all licensed institutions today.",
    "The circular requires that every bank shall report its exposure to the",
    "central bank within thirty days of the end of the quarter.",
])
OTHER_PDF = _pdf_bytes([
    "Bank Supervision Department annual report for the fiscal year.",
    "All licensed institutions shall submit the statement within the period.",
])


@pytest.fixture()
def blobs(tmp_path):
    """Four cohort files over three states, with real bytes on disk.

    * `shared_a` and `shared_b` are two catalog files with IDENTICAL bytes — the
      case the whole source-vs-blob distinction exists for.
    * `other` is a second, distinct blob.
    * `unfetched` is a cohort member that has not been downloaded, so the pass
      has to report it rather than substitute something else.
    """
    _skip_if_no_db()
    owner = f"zz{uuid.uuid4().hex[:8]}"
    tag = uuid.uuid4().hex[:8]
    shared_sha = hashlib.sha256(ENGLISH_PDF).hexdigest()
    other_sha = hashlib.sha256(OTHER_PDF).hexdigest()

    for body, sha in ((ENGLISH_PDF, shared_sha), (OTHER_PDF, other_sha)):
        path = tmp_path / filestore.storage_key_for(sha, "pdf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    files = {
        "shared_a": (f"{UPLOADS}/{tag}-a.pdf", shared_sha, len(ENGLISH_PDF)),
        "shared_b": (f"{UPLOADS}/{tag}-b.pdf", shared_sha, len(ENGLISH_PDF)),
        "other": (f"{UPLOADS}/{tag}-c.pdf", other_sha, len(OTHER_PDF)),
        "unfetched": (f"{UPLOADS}/{tag}-d.pdf", None, None),
    }

    async def setup():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                ids = await _seed(
                    session, owner=owner, section="circular",
                    published=datetime(2019, 5, 1, tzinfo=timezone.utc),
                    files=[{"name": name, "url": url}
                           for name, (url, _s, _l) in files.items()],
                )
                for name, (_url, sha, length) in files.items():
                    if sha is None:
                        continue
                    await session.execute(_text(
                        "UPDATE nrb_files SET fetch_status = 'fetched',"
                        " content_sha256 = :sha, content_length = :len,"
                        " storage_key = :key, sniffed_mime = 'application/pdf'"
                        " WHERE id = :id"),
                        {"sha": sha, "len": length, "id": ids[name],
                         "key": filestore.storage_key_for(sha, "pdf")})
                await session.commit()
                return ids
        finally:
            await engine.dispose()

    ids = asyncio.run(setup())
    yield {
        "owner": owner, "ids": ids, "base": tmp_path,
        "keys": {name: url for name, (url, _s, _l) in files.items()},
        "shared_sha": shared_sha, "other_sha": other_sha,
    }

    async def teardown():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.execute(_text(
                    "DELETE FROM nrb_extractions WHERE content_sha256 = ANY(:s)"),
                    {"s": [shared_sha, other_sha]})
                await conn.execute(_text(
                    "DELETE FROM nrb_source_files WHERE file_id = ANY(:ids)"),
                    {"ids": list(ids.values())})
                await conn.execute(_text(
                    "DELETE FROM nrb_files WHERE id = ANY(:ids)"),
                    {"ids": list(ids.values())})
                await conn.execute(_text(
                    "DELETE FROM nrb_sources WHERE owner = :o"), {"o": owner})
        finally:
            await engine.dispose()

    asyncio.run(teardown())


def _cohort_keys(blobs, *names):
    names = names or ("shared_a", "shared_b", "other", "unfetched")
    return [blobs["keys"][name] for name in names]


def _pass(blobs, **kwargs):
    """A real `run_extract` over the fixture's blob store."""
    async def go():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            return await extract_mod.run_extract(
                engine=engine, session_factory=factory, base_dir=blobs["base"],
                **kwargs,
            )
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _rows(shas):
    async def go():
        engine = _engine()
        try:
            async with engine.connect() as conn:
                result = await conn.execute(_text(
                    "SELECT content_sha256, extractor_version, status, reason,"
                    " char_count, legacy_line_ratio, page_count, error, preview"
                    " FROM nrb_extractions WHERE content_sha256 = ANY(:s)"
                    " ORDER BY content_sha256, extractor_version"), {"s": list(shas)})
                return result.mappings().all()
        finally:
            await engine.dispose()

    return asyncio.run(go())


# --- A. exact manifest scope ------------------------------------------------ #
def test_only_the_cohorts_own_blobs_are_ever_extracted(blobs):
    """A file outside the manifest can never become a target, whatever else is in
    the catalog."""
    result = _pass(blobs, keys=_cohort_keys(blobs, "other"))
    assert result.counters["blobs_attempted"] == 1
    rows = _rows([blobs["shared_sha"], blobs["other_sha"]])
    assert [row["content_sha256"] for row in rows] == [blobs["other_sha"]]


# --- B. source-vs-blob identity --------------------------------------------- #
def test_two_cohort_files_sharing_bytes_are_one_extraction(blobs):
    """The case the whole design turns on. Two logical benchmark files, identical
    content: one attempt, one row, and the report can still speak for both."""
    result = _pass(blobs, keys=_cohort_keys(blobs, "shared_a", "shared_b"))
    assert result.cohort["source"]["requested"] == 2
    assert result.cohort["source"]["fetched"] == 2
    assert result.cohort["blob"]["unique_fetched"] == 1
    assert result.cohort["blob"]["duplicates_collapsed"] == 1
    assert result.counters["blobs_attempted"] == 1
    assert len(_rows([blobs["shared_sha"]])) == 1


def test_the_report_associates_one_verdict_with_both_manifest_entries(blobs):
    _pass(blobs, keys=_cohort_keys(blobs, "shared_a", "shared_b"))

    async def go():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return await profile.load_cohort(
                    session, keys=_cohort_keys(blobs, "shared_a", "shared_b"),
                    extractor_version=extraction_mod.EXTRACTOR_VERSION,
                )
        finally:
            await engine.dispose()

    cohort = asyncio.run(go())
    assert len(cohort.verdicts) == 1
    assert set(cohort.keys_for(blobs["shared_sha"])) == set(
        _cohort_keys(blobs, "shared_a", "shared_b")
    )


# --- C/D. extractor-version semantics --------------------------------------- #
def test_a_blob_already_extracted_at_this_version_is_skipped(blobs):
    first = _pass(blobs, keys=_cohort_keys(blobs))
    assert first.counters["blobs_attempted"] == 2      # two unique blobs

    second = _pass(blobs, keys=_cohort_keys(blobs))
    assert second.counters["blobs_selected"] == 0
    assert second.counters["blobs_attempted"] == 0
    assert second.cohort["blob"]["already_extracted"] == 2
    assert second.cohort["blob"]["pending_extraction"] == 0


def test_an_older_extraction_does_not_make_a_blob_current(blobs):
    """The skip is an EXACT (content_sha256, extractor_version) match. A result
    from an older extractor is a previous answer to a question the current rules
    would answer differently — it must not suppress the current work."""
    _pass(blobs, keys=_cohort_keys(blobs), extractor_version="native-0")
    assert len(_rows([blobs["shared_sha"]])) == 1

    current = _pass(blobs, keys=_cohort_keys(blobs))
    assert current.counters["blobs_attempted"] == 2
    versions = {row["extractor_version"] for row in _rows([blobs["shared_sha"]])}
    assert versions == {"native-0", extraction_mod.EXTRACTOR_VERSION}


def test_force_re_extracts_a_blob_recorded_at_this_version(blobs):
    _pass(blobs, keys=_cohort_keys(blobs))
    again = _pass(blobs, keys=_cohort_keys(blobs), force=True)
    assert again.counters["blobs_attempted"] == 2
    # Still one row per (blob, version): the upsert replaced, it did not duplicate.
    assert len(_rows([blobs["shared_sha"]])) == 1


# --- E. dry run -------------------------------------------------------------- #
def test_a_dry_run_calls_no_parser_and_writes_no_row(blobs, monkeypatch):
    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("a dry run called the parser")

    monkeypatch.setattr(extract_mod, "extract_file", explode)
    result = _pass(blobs, keys=_cohort_keys(blobs), dry_run=True)

    assert result.dry_run is True
    assert result.counters["blobs_selected"] == 2      # it still says what it would do
    assert result.counters["blobs_attempted"] == 0
    assert result.counters["blobs_persisted"] == 0
    assert _rows([blobs["shared_sha"], blobs["other_sha"]]) == []


def test_a_dry_run_still_reports_the_full_cohort_accounting(blobs):
    result = _pass(blobs, keys=_cohort_keys(blobs), dry_run=True)
    assert result.cohort["source"]["requested"] == 4
    assert result.cohort["source"]["fetched"] == 3
    assert result.cohort["source"]["unfetched"] == 1
    assert result.cohort["blob"]["unique_fetched"] == 2
    assert result.cohort["blob"]["duplicates_collapsed"] == 1
    assert result.cohort["blob"]["pending_extraction"] == 2


def test_a_dry_run_makes_no_http_request(blobs, monkeypatch):
    import httpx as _httpx

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("the extraction pass made an HTTP request")

    for name in ("AsyncClient", "Client", "get", "request", "stream"):
        monkeypatch.setattr(_httpx, name, explode)
    assert _pass(blobs, keys=_cohort_keys(blobs), dry_run=True).ok


def test_a_real_pass_makes_no_http_request_either(blobs, monkeypatch):
    """Phase 6A reads local blobs. There is no network in this path at all."""
    import httpx as _httpx

    def explode(*args, **kwargs):    # pragma: no cover - must never run
        raise AssertionError("the extraction pass made an HTTP request")

    for name in ("AsyncClient", "Client", "get", "request", "stream"):
        monkeypatch.setattr(_httpx, name, explode)
    assert _pass(blobs, keys=_cohort_keys(blobs)).counters["blobs_persisted"] == 2


# --- F. failure isolation ---------------------------------------------------- #
def test_a_missing_blob_is_recorded_and_the_pass_continues(blobs):
    """One unreadable file must not cost the other 399 measurements."""
    (blobs["base"] / filestore.storage_key_for(blobs["shared_sha"], "pdf")).unlink()

    result = _pass(blobs, keys=_cohort_keys(blobs))
    assert result.counters["blobs_attempted"] == 2
    assert result.counters["blobs_persisted"] == 2
    assert result.counters["blobs_failed"] == 1
    assert result.counters["blobs_missing_on_disk"] == 1

    by_sha = {row["content_sha256"]: row for row in
              _rows([blobs["shared_sha"], blobs["other_sha"]])}
    assert by_sha[blobs["shared_sha"]]["status"] == STATUS_FAILED
    assert "missing" in by_sha[blobs["shared_sha"]]["error"]
    assert by_sha[blobs["other_sha"]]["status"] != STATUS_FAILED


def test_a_corrupt_blob_is_caught_before_it_is_parsed(blobs):
    """The path IS the checksum, so bytes that no longer hash to their own name
    are corrupt — and a truncated PDF is exactly the input that yields
    plausible-looking partial text."""
    path = blobs["base"] / filestore.storage_key_for(blobs["shared_sha"], "pdf")
    path.write_bytes(ENGLISH_PDF[: len(ENGLISH_PDF) // 2])

    result = _pass(blobs, keys=_cohort_keys(blobs))
    assert result.counters["blobs_corrupt_on_disk"] == 1
    row = {r["content_sha256"]: r for r in _rows([blobs["shared_sha"]])}
    assert row[blobs["shared_sha"]]["status"] == STATUS_FAILED
    assert "hash" in row[blobs["shared_sha"]]["error"]


def test_a_failure_never_carries_a_filesystem_path_into_the_database(blobs):
    (blobs["base"] / filestore.storage_key_for(blobs["other_sha"], "pdf")).unlink()
    _pass(blobs, keys=_cohort_keys(blobs))
    for row in _rows([blobs["other_sha"]]):
        assert str(blobs["base"]) not in (row["error"] or "")


# --- G. the upsert replaces everything --------------------------------------- #
def test_re_extraction_cannot_leave_a_stale_severity_metric(blobs):
    """`record_extractions` derives its conflict-update set by subtraction from
    the table, so a metric column added later is refreshed rather than keeping its
    first value. This proves it end to end: a row written with one set of numbers
    is fully replaced, not partially patched."""
    async def poison():
        engine = _engine()
        try:
            async with engine.begin() as conn:
                await conn.execute(_text(
                    "INSERT INTO nrb_extractions (content_sha256, extractor_version,"
                    " parser, media_family, status, reason, warnings, char_count,"
                    " legacy_line_ratio, legacy_lines, judged_lines, page_count,"
                    " metrics, preview)"
                    " VALUES (:sha, :v, 'pypdf', 'pdf', 'suspicious',"
                    " 'legacy_font_suspected', '[\"stale\"]'::jsonb, 999999,"
                    " 0.99, 99, 100, 42, '{\"stale\": true}'::jsonb, 'STALE')"),
                    {"sha": blobs["shared_sha"],
                     "v": extraction_mod.EXTRACTOR_VERSION})
        finally:
            await engine.dispose()

    asyncio.run(poison())
    _pass(blobs, keys=_cohort_keys(blobs), force=True)

    row = {r["content_sha256"]: r for r in _rows([blobs["shared_sha"]])}[
        blobs["shared_sha"]
    ]
    assert row["char_count"] != 999999
    assert row["legacy_line_ratio"] != 0.99
    assert row["page_count"] == 1
    assert row["preview"] != "STALE"
    assert row["status"] != "suspicious" or row["reason"] != "legacy_font_suspected"


# --- H. missing and unfetched entries ---------------------------------------- #
def test_an_unfetched_cohort_file_is_reported_and_never_substituted(blobs):
    result = _pass(blobs, keys=_cohort_keys(blobs))
    assert result.cohort["source"]["unfetched"] == 1
    assert result.cohort["source"]["by_fetch_status"]["pending"] == 1
    assert result.counters["blobs_attempted"] == 2      # not 3, and not backfilled


def test_a_cohort_key_the_catalog_does_not_know_is_reported_missing(blobs):
    keys = _cohort_keys(blobs) + ["https://www.nrb.org.np/contents/uploads/nope.pdf"]
    result = _pass(blobs, keys=keys, dry_run=True)
    assert result.cohort["source"]["requested"] == 5
    assert result.cohort["source"]["in_catalog"] == 4
    assert result.cohort["source"]["missing_from_catalog"] == 1
    assert result.cohort["blob"]["unique_fetched"] == 2      # nothing invented


# --- I. deterministic --limit ------------------------------------------------ #
def test_limit_takes_the_first_blobs_of_the_cohort_in_manifest_order(blobs):
    """`--limit 1` over a cohort whose first entry is `other` must take `other`,
    whatever order the query returned — that is what makes a bounded developer
    run reproducible."""
    first = _pass(
        blobs, keys=_cohort_keys(blobs, "other", "shared_a"), limit=1, dry_run=True
    )
    assert first.counters["blobs_selected"] == 1

    result = _pass(
        blobs, keys=_cohort_keys(blobs, "other", "shared_a"), limit=1
    )
    assert [row["content_sha256"] for row in
            _rows([blobs["shared_sha"], blobs["other_sha"]])] == [blobs["other_sha"]]
    assert result.counters["blobs_attempted"] == 1


def test_limit_is_applied_after_deduplication_not_to_raw_rows(blobs):
    """Two cohort files, one blob: `--limit 2` cannot manufacture a second
    extraction out of the duplicate."""
    result = _pass(
        blobs, keys=_cohort_keys(blobs, "shared_a", "shared_b"), limit=2,
        dry_run=True,
    )
    assert result.counters["blobs_selected"] == 1


# --- the whole thing, through the report ------------------------------------- #
def test_the_pass_and_the_report_agree_on_every_population(blobs):
    result = _pass(blobs, keys=_cohort_keys(blobs))

    async def go():
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return await profile.load_cohort(
                    session, keys=_cohort_keys(blobs),
                    extractor_version=extraction_mod.EXTRACTOR_VERSION,
                )
        finally:
            await engine.dispose()

    cohort = asyncio.run(go())
    summary = report.summarize_extraction(result, cohort=cohort)
    assert summary["source_coverage"]["requested"] == 4
    assert summary["source_coverage"]["fetched"] == 3
    assert summary["source_coverage"]["unfetched"] == 1
    assert summary["blob_coverage"]["unique_fetched"] == 2
    assert summary["blob_coverage"]["duplicates_collapsed"] == 1
    assert summary["blob_coverage"]["already_extracted"] == 2
    assert sum(summary["by_status"].values()) == 2       # per BLOB, not per file
    assert report.render_extraction(summary)


def test_a_pass_with_no_cohort_still_reports_its_verdicts(blobs):
    """`--section circular` and `--all` are legitimate non-benchmark passes, and
    they have no manifest to carry verdicts. A report that said "2 blobs
    persisted" and showed no statuses at all would be worse than no report — the
    numbers would look like a successful pass that measured nothing."""
    result = _pass(blobs, owners=[blobs["owner"]])
    assert result.counters["blobs_persisted"] == 2
    assert result.cohort is None
    assert len(result.verdicts) == 2

    summary = report.summarize_extraction(result)
    assert sum(summary["by_status"].values()) == 2
    assert summary["by_reason"]
    assert summary["metrics"]["char_count"]["n"] == 2
    assert "no manifest scope" in report.render_extraction(summary)


def test_a_dry_pass_with_no_cohort_reports_no_verdicts(blobs):
    """Nothing was extracted, so there is nothing to report — and the selected
    blobs are by definition NOT extracted at this version."""
    result = _pass(blobs, owners=[blobs["owner"]], dry_run=True)
    assert result.counters["blobs_selected"] == 2
    assert result.verdicts == {}
    assert report.summarize_extraction(result)["by_status"] == {}

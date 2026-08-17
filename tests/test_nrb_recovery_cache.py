"""Phase 7 step 2 — the versioned recovery cache.

WHAT THESE TESTS ARE FOR, AND HOW THEY PROVE IT
    Reuse is proved by CALL COUNTERS, not by equal output. A converter and an
    OCR engine that count their invocations make "the second pass ran npttf2utf
    zero times" an assertion; comparing text would pass just as happily if the
    converter had run again and produced the same answer, which is exactly the
    expensive failure this task exists to prevent.

    The stub converter and OCR engine are the same shape `test_nrb_recovery.py`
    uses, plus a counter and a settable version. Nothing here needs npttf2utf,
    rapidocr or onnxruntime — the point is which functions get CALLED.

    Most tests are pure and need no database: `resolve` takes a `CachedRecovery`
    and returns a `RecoveredDocument`, so the whole reuse/staleness matrix is
    testable in memory. The persistence round-trip has its own section, against
    real Postgres, rolled back.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.nrb import legacy_convert, provenance, recovery, recovery_cache
from app.nrb import rag as nrb_rag
from app.nrb.recovery import (
    PageText,
    RecoveredDocument,
    ROUTE_LEGACY,
    ROUTE_NATIVE,
    ROUTE_OCR,
)
from app.nrb.recovery_cache import CachedRecovery, CachedUnit


# --------------------------------------------------------------------------- #
# Counting stubs.
# --------------------------------------------------------------------------- #
class CountingConverter:
    """A legacy-font converter that records every call.

    `convert` maps each character to one Devanagari letter, preserving length
    and producing no conjuncts, so `legacy_convert`'s validation accepts it —
    a fixed-length Devanagari string would fail the shrinkage check and every
    unit would come back `rejected`, which would make "the converter did not
    run" and "the converter ran and was refused" indistinguishable. Mapping
    CORRECTNESS is `test_nrb_legacy_convert.py`'s business, not this file's.
    """

    _ALPHABET = "अआइईउऊएऐओऔकखगघङचछजझञ"

    def __init__(self, version: str = "0.3.7", mapping: str = "Preeti") -> None:
        self.name = "npttf2utf"
        self.version = version
        self.mapping = mapping
        self.calls = 0

    def convert(self, text_in: str) -> str:
        self.calls += 1
        return "".join(
            c if c.isspace() else self._ALPHABET[ord(c) % len(self._ALPHABET)]
            for c in text_in
        )


class CountingOcr:
    def __init__(self, version: str = "rapidocr 1.4.4") -> None:
        self.name = "docling-rapidocr"
        self.model = "PP-OCRv5"
        self.lang = "devanagari"
        self.backend = "onnxruntime"
        self.version = version
        self.calls = 0

    def ocr_page(self, path, page_number: int) -> str:
        self.calls += 1
        return f"ओसीआर पृष्ठ {page_number}"


@dataclass(frozen=True)
class StubLexicon:
    """Only `fingerprint` is read by the cache; the guards are stubbed out."""

    fingerprint: str = "abc123def456"
    version: str = "test"
    english: frozenset = frozenset({"the", "and", "amount"})
    nepali: frozenset = frozenset()


def _engines(converter=None, lexicon=None, ocr=None):
    return recovery_cache.engine_versions(
        converter=converter, lexicon=lexicon, ocr=ocr
    )


def _unit(number, route, *, engine, text_="त", ok=True, reason="embedded_font",
          label=None, detail=None, error=None) -> CachedUnit:
    return CachedUnit(
        unit_number=number, label=label, route=route, reason=reason,
        engine_version=engine, ok=ok, text=text_, error=error,
        detail=detail or {},
    )


def _cached(units, *, family="pdf", plan=recovery.PLAN_PAGES, gate=1.0,
            base=None, sha="a" * 64) -> CachedRecovery:
    return CachedRecovery(
        content_sha256=sha,
        base_version=base or recovery_cache.base_version(),
        family=family,
        plan=plan,
        plan_reason="legacy_font_suspected",
        gate_ratio=gate,
        warnings=(),
        units=tuple(units),
    )


def _never_cold():
    def _boom():
        raise AssertionError("a cold recovery ran when the cache should have hit")

    return _boom


# --------------------------------------------------------------------------- #
# 1-4. Reuse: the converter and the OCR engine do not run a second time.
# --------------------------------------------------------------------------- #
def test_a_first_conversion_runs_the_converter_and_its_result_is_cacheable(tmp_path):
    """The cold pass really does call npttf2utf, so 'zero calls' means something."""
    converter, lexicon = CountingConverter(), StubLexicon()
    page = recovery.convert_unit(
        1, "kfg\tt/", reason="embedded_font", converter=converter,
        lexicon=lexicon, document_legacy_ratio=1.0,
    )
    assert converter.calls >= 1
    assert page.route == ROUTE_LEGACY and page.indexable


def test_a_second_identical_recovery_does_not_call_the_converter(tmp_path):
    """THE property. A warm hit runs no converter and opens no file."""
    converter, lexicon = CountingConverter(), StubLexicon()
    engines = _engines(converter, lexicon)
    cached = _cached([
        _unit(1, ROUTE_LEGACY, engine=engines.legacy_conversion),
        _unit(2, ROUTE_LEGACY, engine=engines.legacy_conversion),
    ])

    recovered, report = recovery_cache.resolve(
        tmp_path / "missing.pdf",           # never opened, so it need not exist
        cached=cached, converter=converter, lexicon=lexicon,
        cold=_never_cold(),
    )
    assert converter.calls == 0
    assert report.outcome == "warm"
    assert (report.units_reused, report.units_recovered) == (2, 0)
    assert report.converter_units == 0
    assert [p.page_number for p in recovered.pages] == [1, 2]


def test_a_second_identical_recovery_does_not_call_ocr(tmp_path):
    ocr = CountingOcr()
    engines = _engines(ocr=ocr)
    cached = _cached([_unit(1, ROUTE_OCR, engine=engines.ocr,
                            reason="no_font_scan_backed")])

    _, report = recovery_cache.resolve(
        tmp_path / "missing.pdf", cached=cached, ocr=ocr, cold=_never_cold()
    )
    assert ocr.calls == 0
    assert report.outcome == "warm" and report.ocr_units == 0


# --------------------------------------------------------------------------- #
# 5, 7, 8. Selective invalidation — the reason there are two version domains.
# --------------------------------------------------------------------------- #
def _mixed_pdf(tmp_path: Path) -> Path:
    """A two-page PDF whose pages really are readable, for the partial path.

    `resolve` re-reads page text on a partial refresh (a conversion needs its
    input), so this cannot be a stub path.
    """
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    path = tmp_path / "mixed.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def test_an_ocr_version_bump_invalidates_only_the_ocr_unit(tmp_path):
    """`e08988860534`'s shape: page 1 OCR, page 2 conversion, one document.

    A new OCR model must re-run page 1 and leave page 2 exactly as it was. This
    is the whole justification for versioning the engine per unit instead of
    keying the cache on one string.
    """
    path = _mixed_pdf(tmp_path)
    converter, lexicon = CountingConverter(), StubLexicon()
    old_ocr = CountingOcr(version="rapidocr 1.4.4")
    engines = _engines(converter, lexicon, old_ocr)
    cached = _cached([
        _unit(1, ROUTE_OCR, engine=engines.ocr, reason="no_font_scan_backed",
              text_="पुरानो ओसीआर"),
        _unit(2, ROUTE_LEGACY, engine=engines.legacy_conversion,
              text_="पुरानो रूपान्तरण"),
    ])

    new_ocr = CountingOcr(version="rapidocr 2.0.0")
    recovered, report = recovery_cache.resolve(
        path, cached=cached, converter=converter, lexicon=lexicon, ocr=new_ocr,
        cold=_never_cold(),
    )

    assert report.outcome == "partial"
    assert (report.units_reused, report.units_recovered) == (1, 1)
    assert new_ocr.calls == 1            # page 1 re-OCR'd
    assert converter.calls == 0          # page 2 NOT reconverted
    assert report.converter_units == 0 and report.ocr_units == 1
    assert recovered.pages[1].text == "पुरानो रूपान्तरण"
    assert recovered.pages[0].route == ROUTE_OCR


def test_a_converter_version_bump_invalidates_only_the_conversion_unit(tmp_path):
    path = _mixed_pdf(tmp_path)
    old_converter, lexicon = CountingConverter(version="0.3.7"), StubLexicon()
    ocr = CountingOcr()
    engines = _engines(old_converter, lexicon, ocr)
    cached = _cached([
        _unit(1, ROUTE_OCR, engine=engines.ocr, reason="no_font_scan_backed",
              text_="पुरानो ओसीआर"),
        _unit(2, ROUTE_LEGACY, engine=engines.legacy_conversion,
              text_="पुरानो रूपान्तरण"),
    ])

    new_converter = CountingConverter(version="0.4.0")
    recovered, report = recovery_cache.resolve(
        path, cached=cached, converter=new_converter, lexicon=lexicon, ocr=ocr,
        cold=_never_cold(),
    )

    assert report.outcome == "partial"
    assert ocr.calls == 0                     # the scan is NOT re-OCR'd
    assert report.ocr_units == 0 and report.converter_units == 1
    assert recovered.pages[0].text == "पुरानो ओसीआर"


def test_a_lexicon_change_invalidates_conversion_and_not_ocr(tmp_path):
    """The lexicon is a conversion GUARD, so it versions the conversion engine.

    It is deliberately not part of the base version: changing what counts as an
    English word changes what conversion produces, never where a page is routed.
    """
    path = _mixed_pdf(tmp_path)
    converter = CountingConverter()
    ocr = CountingOcr()
    engines = _engines(converter, StubLexicon(fingerprint="old000000000"), ocr)
    cached = _cached([
        _unit(1, ROUTE_OCR, engine=engines.ocr, reason="no_font_scan_backed"),
        _unit(2, ROUTE_LEGACY, engine=engines.legacy_conversion),
    ])

    _, report = recovery_cache.resolve(
        path, cached=cached, converter=converter,
        lexicon=StubLexicon(fingerprint="new111111111"), ocr=ocr,
        cold=_never_cold(),
    )
    assert report.converter_units == 1 and report.ocr_units == 0


def test_installing_the_converter_invalidates_only_the_pages_it_could_not_do(
    tmp_path,
):
    """`unavailable` is a version, not an absence — and that is load-bearing.

    A page recorded `conversion_unavailable` on a deployment without npttf2utf
    (§18's most dangerous failure: it looks like a clean deployment) must be
    re-run once npttf2utf is installed, while its native and OCR'd neighbours
    are reused. If absence were simply excluded from the key, that page would
    stay withheld forever.
    """
    path = _mixed_pdf(tmp_path)
    ocr = CountingOcr()
    without = _engines(None, None, ocr)
    assert without.legacy_conversion == recovery_cache.UNAVAILABLE
    cached = _cached([
        _unit(1, ROUTE_OCR, engine=without.ocr, reason="no_font_scan_backed"),
        _unit(2, ROUTE_LEGACY, engine=without.legacy_conversion, ok=False,
              text_="", reason="conversion_unavailable",
              error="legacy font converter unavailable"),
    ])

    converter, lexicon = CountingConverter(), StubLexicon()
    recovered, report = recovery_cache.resolve(
        path, cached=cached, converter=converter, lexicon=lexicon, ocr=ocr,
        cold=_never_cold(),
    )
    assert report.outcome == "partial"
    assert ocr.calls == 0
    assert report.converter_units == 1
    assert recovered.pages[1].route == ROUTE_LEGACY


# --------------------------------------------------------------------------- #
# 6. Unresolved outcomes are cached, so they do not re-run forever.
# --------------------------------------------------------------------------- #
def test_an_unresolved_unit_is_reused_and_does_not_re_run_its_engine(tmp_path):
    """A deterministically unrecoverable page is a cached ANSWER.

    Caching only the successes is the failure this test exists to prevent:
    every withheld page would re-run OCR (2-4 s) on every ingest, forever, to
    reach the same conclusion. The reason travels with it, and the page is
    still not indexable.
    """
    ocr = CountingOcr()
    engines = _engines(ocr=ocr)
    cached = _cached([
        _unit(1, ROUTE_OCR, engine=engines.ocr, ok=False, text_="",
              reason="no_font_scan_backed", error="ocr engine unavailable"),
    ])

    recovered, report = recovery_cache.resolve(
        tmp_path / "missing.pdf", cached=cached, ocr=ocr, cold=_never_cold()
    )
    assert ocr.calls == 0 and report.outcome == "warm"
    page = recovered.pages[0]
    assert page.ok is False and page.indexable is False
    assert page.error == "ocr engine unavailable"
    assert recovered.indexable_pages == ()


# --------------------------------------------------------------------------- #
# 9-11. Base-version invalidation, and what must NOT invalidate.
# --------------------------------------------------------------------------- #
def test_a_routing_version_bump_invalidates_the_whole_document(tmp_path):
    """A routing change makes every cached ROUTE the answer to a stale question.

    Nothing is salvaged unit by unit, deliberately: if `route_page` now sends a
    page to OCR that used to be converted, reusing the converted text would
    serve output the current rules would never produce.
    """
    ran: list[str] = []

    def cold():
        ran.append("cold")
        return RecoveredDocument("pdf", recovery.PLAN_PAGES, "r", 1.0,
                                 (PageText(1, ROUTE_NATIVE, "r", "fresh"),))

    cached = _cached([_unit(1, ROUTE_NATIVE, engine=_engines().native)],
                     base="native-2|recovery-0|prov-1|gate=0.8|unjudged=0.8")
    _, report = recovery_cache.resolve(tmp_path / "x.pdf", cached=cached, cold=cold)
    assert ran == ["cold"]
    assert report.outcome == "cold" and report.reason == "base_version_changed"


def test_every_routing_input_is_in_the_base_version(monkeypatch):
    """The base version must move when any routing input does.

    Asserted term by term rather than trusting the format string: a future
    reader adding a routing rule needs the test to fail if they forget the
    version, and the two GATE constants are read live for exactly that reason.
    """
    baseline = recovery_cache.base_version()

    monkeypatch.setattr(recovery, "RECOVERY_ROUTING_VERSION", "recovery-99")
    assert recovery_cache.base_version() != baseline
    monkeypatch.undo()

    monkeypatch.setattr(provenance, "PAGE_PROVENANCE_VERSION", "prov-99")
    assert recovery_cache.base_version() != baseline
    monkeypatch.undo()

    monkeypatch.setattr(recovery, "CONVERSION_GATE", 0.75)
    assert recovery_cache.base_version() != baseline
    monkeypatch.undo()

    monkeypatch.setattr(legacy_convert, "UNJUDGED_MIN_LEGACY_RATIO", 0.75)
    assert recovery_cache.base_version() != baseline
    monkeypatch.undo()

    assert recovery_cache.base_version("native-3") != baseline
    assert recovery_cache.base_version() == baseline


def test_a_chunking_or_embedding_change_does_not_invalidate_the_recovery(tmp_path):
    """The cache stores TEXT, never chunks, so re-chunking is free.

    This is the end-to-end property the task is for: a re-ingest triggered by a
    chunker or embedding-model change must reuse the recovered text. Same
    cached recovery, two chunk sizes, zero converter calls either time.
    """
    converter, lexicon = CountingConverter(), StubLexicon()
    engines = _engines(converter, lexicon)
    body = "नेपाल राष्ट्र बैंक " * 40
    cached = _cached([_unit(1, ROUTE_LEGACY, engine=engines.legacy_conversion,
                            text_=body)])

    small = nrb_rag.recover_and_chunk(
        tmp_path / "missing.pdf", max_chars=120, overlap_chars=10,
        cached=cached, converter=converter, lexicon=lexicon,
    )
    large = nrb_rag.recover_and_chunk(
        tmp_path / "missing.pdf", max_chars=2000, overlap_chars=10,
        cached=cached, converter=converter, lexicon=lexicon,
    )

    assert converter.calls == 0
    assert small.report.outcome == large.report.outcome == "warm"
    assert len(small.chunks) > len(large.chunks)   # the chunker really changed
    assert all(c.meta["route"] == ROUTE_LEGACY for c in small.chunks)


def test_different_bytes_never_reuse_a_prior_cache_entry():
    """Identity is sha256(bytes), so republished bytes cannot collide.

    Asserted at the lookup key rather than through the DB: the row is keyed on
    `(content_sha256, base_version)` and `documents.content_hash` is the same
    number, so a changed file is a different row by construction.
    """
    a = hashlib.sha256(b"circular v1").hexdigest()
    b = hashlib.sha256(b"circular v2").hexdigest()
    assert a != b
    cached = _cached([_unit(1, ROUTE_NATIVE, engine=_engines().native)], sha=a)
    assert cached.content_sha256 != b


# --------------------------------------------------------------------------- #
# 12-13. Order and provenance survive reuse.
# --------------------------------------------------------------------------- #
def test_a_cached_recovery_preserves_page_order_and_route_provenance(tmp_path):
    """A reused chunk must cite exactly as a fresh one does.

    Page number, route, converter identity and the OCR `authoritative: false`
    caveat all travel in `document_chunks.metadata`; if any of them were lost on
    reuse, a warm ingest would silently degrade every citation it produced.
    """
    converter, lexicon, ocr = CountingConverter(), StubLexicon(), CountingOcr()
    engines = _engines(converter, lexicon, ocr)
    cached = _cached([
        _unit(1, ROUTE_OCR, engine=engines.ocr, reason="no_font_scan_backed",
              text_="स्क्यान गरिएको पृष्ठ",
              detail={"engine": "docling-rapidocr", "model": "PP-OCRv5",
                      "version": "rapidocr 1.4.4", "authoritative": False}),
        _unit(2, ROUTE_LEGACY, engine=engines.legacy_conversion,
              text_="रूपान्तरित पृष्ठ",
              detail={"converter": "npttf2utf 0.3.7", "mapping": "Preeti",
                      "converted_units": 12, "unresolved_units": 0}),
        _unit(3, ROUTE_NATIVE, engine=engines.native, reason="no_font_provenance",
              text_="native text"),
    ])

    out = nrb_rag.recover_and_chunk(
        tmp_path / "missing.pdf", max_chars=4000, overlap_chars=100,
        cached=cached, converter=converter, lexicon=lexicon, ocr=ocr,
    )

    assert [c.page_number for c in out.chunks] == [1, 2, 3]
    assert [c.meta["route"] for c in out.chunks] == [
        ROUTE_OCR, ROUTE_LEGACY, ROUTE_NATIVE
    ]
    assert out.chunks[0].meta["ocr_model"] == "PP-OCRv5"
    assert out.chunks[0].meta["authoritative"] is False
    assert out.chunks[1].meta["converter"] == "npttf2utf 0.3.7"
    assert out.chunks[1].meta["mapping"] == "Preeti"
    assert all(c.meta["extractor_version"] == "native-2" for c in out.chunks)


def test_a_spreadsheet_keeps_its_sheet_identity_and_is_all_or_nothing(tmp_path):
    """Non-PDF units are SHEETS, not fake page numbers.

    `document_chunks.page_number` would be a lie otherwise. And because every
    unit of a workbook shares one route, a stale engine invalidates the whole
    document rather than a sheet — there is no per-sheet partial to do.
    """
    converter, lexicon = CountingConverter(), StubLexicon()
    engines = _engines(converter, lexicon)
    cached = _cached(
        [
            _unit(1, ROUTE_LEGACY, engine=engines.legacy_conversion,
                  label="अनुसूची १", text_="क | ख"),
            _unit(2, ROUTE_LEGACY, engine=engines.legacy_conversion,
                  label="Sheet2", text_="ग | घ"),
        ],
        family="spreadsheet", plan=recovery.PLAN_CONVERT,
    )

    out = nrb_rag.recover_and_chunk(
        tmp_path / "book.xlsx", max_chars=4000, overlap_chars=100,
        cached=cached, converter=converter, lexicon=lexicon,
    )
    assert converter.calls == 0
    assert [c.section for c in out.chunks] == ["अनुसूची १", "Sheet2"]
    assert all(c.element_type == "table" for c in out.chunks)

    ran: list[str] = []

    def cold():
        ran.append("cold")
        return RecoveredDocument("spreadsheet", recovery.PLAN_CONVERT, "r", 1.0,
                                 (PageText(1, ROUTE_LEGACY, "r", "fresh"),))

    _, report = recovery_cache.resolve(
        tmp_path / "book.xlsx", cached=cached,
        converter=CountingConverter(version="9.9.9"), lexicon=lexicon, cold=cold,
    )
    assert ran == ["cold"] and report.reason == "non_pdf_engine_changed"


# --------------------------------------------------------------------------- #
# 14. Fail-closed, unchanged.
# --------------------------------------------------------------------------- #
def test_the_cache_never_stores_the_withheld_original(tmp_path):
    """The strongest of the fail-closed guarantees: the junk is not in the row.

    `recovery._withhold` blanks an unresolved unit before a `RecoveredDocument`
    exists, so what `save` writes is already the post-withholding text. There is
    no filter here to forget, and no column from which the glyph-mapped original
    could be recovered.
    """
    class RefusingConverter(CountingConverter):
        def convert(self, text_in):
            self.calls += 1
            return "!!!"          # fails validation → REJECTED_LINE

    converter, lexicon = RefusingConverter(), StubLexicon()
    original = "kfg\tt/ hrn"
    page = recovery.convert_unit(
        1, original, reason="embedded_font", converter=converter,
        lexicon=lexicon, document_legacy_ratio=1.0,
    )
    assert original.strip() not in page.text
    assert page.ok is False

    # And a round trip through the cache's own dataclasses cannot reintroduce it.
    unit = CachedUnit(
        unit_number=1, label=None, route=page.route, reason=page.reason,
        engine_version="x", ok=page.ok, text=page.text, error=page.error,
        detail=page.detail,
    )
    assert original.strip() not in unit.as_page().text
    assert unit.as_page().indexable is False


def test_indexable_is_recomputed_not_stored(tmp_path):
    """A cache row cannot assert a trust state the current rules would refuse.

    `ok=False` with text present is a real state (`conversion_unresolved` keeps
    whatever the guards kept), and it must stay unindexable on reuse. If
    `indexable` were a column, a row written under an older rule could smuggle
    that text into the index.
    """
    unit = _unit(1, ROUTE_LEGACY, engine="x", ok=False,
                 text_="कही केही पाठ", reason="conversion_unresolved",
                 error="no unit converted; 4 unresolved")
    page = unit.as_page()
    assert page.text and page.ok is False and page.indexable is False
    assert not hasattr(recovery_cache.CachedUnit, "indexable")


def test_a_cached_document_with_no_indexable_unit_still_raises(tmp_path):
    """A warm hit on an unrecoverable blob fails the job, it does not index it.

    The message names the routing outcome, as the cold path's does — an ingest
    that silently succeeded with zero chunks is exactly the "every failure looks
    like a clean deployment" mode §18 is about.
    """
    cached = _cached([
        _unit(1, ROUTE_LEGACY, engine=_engines().legacy_conversion, ok=False,
              text_="", reason="conversion_unavailable",
              error="legacy font converter unavailable"),
    ])
    with pytest.raises(nrb_rag.NrbParseError) as excinfo:
        # The three Nones are explicit: with NO dependency kwargs at all,
        # `resolve_dependencies` falls back to the process-wide triple, which on
        # a developer's machine has a real converter — and the cached
        # `unavailable` unit would then be stale rather than warm.
        nrb_rag.recover_and_chunk(
            tmp_path / "missing.pdf", max_chars=4000, overlap_chars=100,
            cached=cached, converter=None, lexicon=None, ocr=None,
        )
    message = str(excinfo.value)
    assert "conversion_unavailable" in message and "route_pages" in message


def test_a_half_written_cache_entry_is_treated_as_a_miss():
    """`unit_count` disagreeing with the rows is not a partial document.

    Serving 40 of a 50-page circular as if it were the whole thing is worse than
    re-running the recovery, so the mismatch reports a MISS and the row is
    rewritten from scratch.
    """
    sha = hashlib.sha256(b"half-written").hexdigest()

    async def body(session, Session):
        await recovery_cache.save(
            session, content_sha256=sha, engines=_engines(),
            recovered=_document([
                PageText(i, ROUTE_NATIVE, "r", f"page {i}") for i in (1, 2, 3)
            ]),
        )
        await session.flush()
        assert await recovery_cache.load(
            session, sha, recovery_cache.base_version()
        ) is not None

        await session.execute(
            text(
                "DELETE FROM nrb_recovery_units u USING nrb_recoveries r "
                " WHERE u.recovery_id = r.id AND r.content_sha256 = :sha "
                "   AND u.unit_number = 3"
            ),
            {"sha": sha},
        )
        await session.flush()
        assert await recovery_cache.load(
            session, sha, recovery_cache.base_version()
        ) is None

    _run(body)


# --------------------------------------------------------------------------- #
# 15. The generic RAG path is untouched.
# --------------------------------------------------------------------------- #
def test_the_generic_parser_never_reaches_the_recovery_cache(monkeypatch, tmp_path):
    """A department's ordinary PDF must not acquire a cache lookup.

    `_load_chunks` branches on `origin == "nrb"` and nothing else; a non-NRB
    document goes straight to `parse_to_chunks` in a thread, with no session
    opened and no NRB module imported.
    """
    from app.rag import worker

    seen: list[str] = []

    def fake_generic(path, file_type, **kw):
        seen.append("generic")
        from app.rag.chunking import Chunk

        return [Chunk(content="x", chunk_index=0)]

    async def forbidden(*a, **kw):
        raise AssertionError("the generic path reached the NRB recovery cache")

    monkeypatch.setattr(worker, "parse_to_chunks", fake_generic)
    monkeypatch.setattr(recovery_cache, "chunks_for_blob", forbidden)

    settings = get_settings()
    stored = Path(settings.rag_docs_dir) / "cache-generic.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"%PDF-1.4\n")
    try:
        snap = worker.DocSnapshot(
            id="g1", department_id=1, file_type="pdf",
            storage_key="cache-generic.pdf", status="pending",
            content_hash="b" * 64, meta={},
        )
        chunks = asyncio.run(worker._load_chunks(None, snap, settings))
    finally:
        stored.unlink(missing_ok=True)
    assert seen == ["generic"] and len(chunks) == 1


def test_the_cache_never_consults_the_extraction_evidence_table():
    """`nrb_extractions` stays off the ingestion path (§19, §20.1).

    The same AST-level guard the corpus driver carries, applied to the module
    that now IS the production reuse boundary. The temptation is different here
    and stronger — `nrb_extractions` already holds a classification — and the
    answer is the same: that table is written by a measurement pass an operator
    may never have run.
    """
    import ast

    tree = ast.parse(Path("app/nrb/recovery_cache.py").read_text())
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "nrb_extractions" not in code
    assert "NRBExtraction" not in code


def test_the_cache_vocabularies_match_recovery():
    """`models.py` restates the route/plan vocabularies; keep the copies equal.

    They are restated rather than imported so a CHECK constraint cannot drag
    docling and the OCR stack into a migration, which means this test is the
    only thing keeping them in step.
    """
    from app.nrb import models

    assert models.RECOVERY_ROUTES == recovery.ROUTES
    assert models.RECOVERY_PLANS == recovery.PLANS


# --------------------------------------------------------------------------- #
# Persistence. Real Postgres, always rolled back.
# --------------------------------------------------------------------------- #
def _skip_if_no_db() -> None:
    async def probe():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


def _run(fn):
    """Run `fn(session, Session)` inside one transaction that is always rolled back."""
    _skip_if_no_db()

    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                outer = await connection.begin()
                Session = async_sessionmaker(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                )
                session = AsyncSession(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                )
                try:
                    return await fn(session, Session)
                finally:
                    await session.close()
                    await outer.rollback()
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _document(pages) -> RecoveredDocument:
    return RecoveredDocument("pdf", recovery.PLAN_PAGES, "legacy_font_suspected",
                             0.97, tuple(pages), ("some_warning",))


def test_a_saved_recovery_round_trips_exactly():
    """Every field a citation or a refresh needs survives the database.

    `gate_ratio` in particular: `convert_unit` takes it as
    `document_legacy_ratio`, so losing it would silently regate every refreshed
    page on its own content — the mistake §16 rule 4 names.
    """
    sha = hashlib.sha256(b"round-trip").hexdigest()
    engines = _engines(CountingConverter(), StubLexicon(), CountingOcr())
    doc = _document([
        PageText(1, ROUTE_OCR, "no_font_scan_backed", "ओसीआर",
                 detail={"model": "PP-OCRv5", "authoritative": False}),
        PageText(2, ROUTE_LEGACY, "embedded_font", "रूपान्तरित",
                 detail={"converter": "npttf2utf 0.3.7", "mapping": "Preeti"}),
        PageText(3, ROUTE_LEGACY, "conversion_unresolved", "", ok=False,
                 error="no unit converted; 3 unresolved"),
    ])

    async def body(session, Session):
        await recovery_cache.save(
            session, content_sha256=sha, recovered=doc, engines=engines
        )
        await session.flush()
        loaded = await recovery_cache.load(
            session, sha, recovery_cache.base_version()
        )
        assert loaded is not None
        assert loaded.gate_ratio == pytest.approx(0.97)
        assert loaded.plan == recovery.PLAN_PAGES
        assert loaded.warnings == ("some_warning",)

        rebuilt = loaded.as_document()
        assert [p.page_number for p in rebuilt.pages] == [1, 2, 3]
        assert [p.route for p in rebuilt.pages] == [
            ROUTE_OCR, ROUTE_LEGACY, ROUTE_LEGACY
        ]
        assert rebuilt.pages[0].detail["authoritative"] is False
        assert rebuilt.pages[2].ok is False
        assert rebuilt.pages[2].error.startswith("no unit converted")
        # The unresolved page is stored AND is still not indexable.
        assert len(rebuilt.indexable_pages) == 2

        stale = loaded.stale(_engines(CountingConverter("9.9.9"), StubLexicon(),
                                      CountingOcr()))
        assert stale == (2, 3)   # the two conversions, not the OCR page

    _run(body)


def test_saving_twice_replaces_the_unit_set_rather_than_appending():
    """A re-save must not leave an orphan unit claiming to be page 3."""
    sha = hashlib.sha256(b"replace").hexdigest()
    engines = _engines()

    async def body(session, Session):
        await recovery_cache.save(
            session, content_sha256=sha, engines=engines,
            recovered=_document([
                PageText(i, ROUTE_NATIVE, "r", f"p{i}") for i in (1, 2, 3)
            ]),
        )
        await session.flush()
        await recovery_cache.save(
            session, content_sha256=sha, engines=engines,
            recovered=_document([PageText(1, ROUTE_NATIVE, "r", "only")]),
        )
        await session.flush()
        loaded = await recovery_cache.load(
            session, sha, recovery_cache.base_version()
        )
        assert loaded is not None and len(loaded.units) == 1
        assert loaded.units[0].text == "only"

    _run(body)


def test_rows_under_another_base_version_sit_side_by_side():
    """Superseded routing versions are KEPT, exactly as native-1/native-2 are.

    A cache row is also the record of what was indexed at the time, so removing
    one is an explicit operator action (`purge`), never a side effect of a write.
    """
    sha = hashlib.sha256(b"side-by-side").hexdigest()
    engines = _engines()
    doc = _document([PageText(1, ROUTE_NATIVE, "r", "text")])

    async def body(session, Session):
        await recovery_cache.save(
            session, content_sha256=sha, recovered=doc, engines=engines,
            extractor_version="native-2",
        )
        await recovery_cache.save(
            session, content_sha256=sha, recovered=doc, engines=engines,
            extractor_version="native-3",
        )
        await session.flush()
        assert await recovery_cache.load(
            session, sha, recovery_cache.base_version("native-2")
        ) is not None
        assert await recovery_cache.load(
            session, sha, recovery_cache.base_version("native-3")
        ) is not None

        removed = await recovery_cache.purge(
            session, content_sha256=sha, keep_current=True,
            extractor_version="native-2",
        )
        assert removed == 1
        assert await recovery_cache.load(
            session, sha, recovery_cache.base_version("native-2")
        ) is not None

    _run(body)


def test_a_document_with_no_recoverable_units_is_still_cached():
    """The OLE2 case. "Nothing this pipeline can do" is an answer worth keeping.

    Otherwise the cohort's unsupported file re-derives the same verdict on every
    pass. It is a warm hit that produces no chunks and the same failure.
    """
    sha = hashlib.sha256(b"ole2").hexdigest()

    async def body(session, Session):
        await recovery_cache.save(
            session, content_sha256=sha, engines=_engines(),
            recovered=RecoveredDocument(
                "spreadsheet", recovery.PLAN_NONE, "no_native_parser", None, ()
            ),
        )
        await session.flush()
        loaded = await recovery_cache.load(
            session, sha, recovery_cache.base_version()
        )
        assert loaded is not None and loaded.units == ()
        _, report = recovery_cache.resolve(
            Path("/nonexistent.xls"), cached=loaded, cold=_never_cold()
        )
        assert report.outcome == "warm" and report.units_total == 0

    _run(body)

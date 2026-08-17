"""The NRB → RAG ingestion boundary: what may be indexed, and with what provenance.

Offline. The recovery layer is exercised through real (tiny) PDFs assembled in
`test_nrb_recovery.py`'s style, and the converter and OCR engine are stubs — the
question here is not whether Preeti converts (that is
`test_nrb_legacy_conversion.py`) but whether only TRUSTWORTHY text reaches a
chunk, and whether a chunk can still be cited afterwards.

Two failure modes these tests exist to prevent, both silent:

  * a page whose conversion was unresolved becoming an ordinary chunk, so a
    citation renders `g]kfn /fi6« a}+s` as the text of a directive;
  * a chunk that spans two PDF pages, which cannot be cited to either.
"""

from __future__ import annotations

import pathlib

import pytest

from app.nrb import rag as nrb_rag
from app.nrb import recovery
from app.rag.chunking import Chunk

from .test_nrb_recovery import (  # reuse the stated-provenance PDF builder
    ENGLISH,
    PREETI,
    SCAN_JUNK,
    StubConverter,
    StubOcr,
    UNICODE_NEPALI,
    UselessConverter,
    _write_pdf,
)

MAX_CHARS = 2000
OVERLAP = 200


def _parse(path, **injected):
    return nrb_rag.parse_nrb_to_chunks(
        path, max_chars=MAX_CHARS, overlap_chars=OVERLAP, **injected
    )


@pytest.fixture()
def lexicon():
    from .test_nrb_recovery import lexicon as _fixture

    # The module-scoped fixture from the routing suite, materialised here.
    from app.nrb import lexicon as LX

    english = frozenset(
        """the bank shall issue circular all licensed institutions and every
        report its exposure within thirty days quarter end""".split()
    )
    nepali = frozenset("नेपाल राष्ट्र बैंक बैंकको प्रयोजनको लागि मात्र".split())
    return LX.Lexicon(
        version=LX.LEXICON_VERSION,
        english=english,
        nepali=nepali,
        fingerprint=LX.lexicon_fingerprint(LX.LEXICON_VERSION, english, nepali),
        provenance={"source": "test fixture"},
    )


# --------------------------------------------------------------------------- #
# 1. Provenance survives into the chunk.
# --------------------------------------------------------------------------- #
def test_a_mixed_document_chunks_per_page_with_its_route(tmp_path, lexicon):
    """`e08988860534`'s shape: an OCR'd page and converted pages in one document.

    Every chunk names its source page and the instrument that produced its text.
    """
    path = _write_pdf(
        tmp_path, "mixed.pdf",
        [{"font": "Helvetica", "image": True},
         {"font": "ABCDEE+Preeti", "embedded": True}],
    )
    result, recovered = nrb_rag.recover_blob(
        path, converter=StubConverter(), lexicon=lexicon, ocr=StubOcr()
    )
    # The recovery layer is fed the real page texts by re-reading the PDF, which
    # for this synthetic file is empty — so drive the chunker from a recovery
    # result built on stated page text instead.
    recovered = recovery.recover(
        path,
        _nrb_result(text="\n".join([SCAN_JUNK, PREETI])),
        converter=StubConverter(), lexicon=lexicon, ocr=StubOcr(),
        pages=[SCAN_JUNK, PREETI],
    )
    chunks = nrb_rag.chunks_from_recovery(
        recovered, max_chars=MAX_CHARS, overlap_chars=OVERLAP
    )

    assert [c.page_number for c in chunks] == [1, 2]
    assert [c.meta["route"] for c in chunks] == [
        recovery.ROUTE_OCR, recovery.ROUTE_LEGACY
    ]
    ocr_chunk, converted = chunks
    assert ocr_chunk.meta["ocr_model"] == "PP-OCRv5"
    # Stated on the chunk, not only in the docs: OCR text is retrieval material,
    # never an authoritative figure or contact detail on a degraded scan.
    assert ocr_chunk.meta["authoritative"] is False
    assert converted.meta["mapping"] == "Preeti"
    assert converted.meta["converted_units"] >= 1
    assert all(c.meta["origin"] == "nrb" for c in chunks)
    assert all(c.meta["extractor_version"] == "native-2" for c in chunks)


def test_chunk_indices_are_contiguous_across_pages(tmp_path, lexicon):
    """Per-page chunking must still produce one contiguous index sequence —
    `document_chunks` has UNIQUE (document_id, chunk_index)."""
    long_page = " ".join([UNICODE_NEPALI] * 60)
    path = _write_pdf(
        tmp_path, "long.pdf",
        [{"font": "Arial", "embedded": True}, {"font": "Arial", "embedded": True}],
    )
    recovered = recovery.recover(
        path, _nrb_result(status="extracted", reason="clean", ratio=0.0,
                          text=long_page),
        converter=StubConverter(), lexicon=lexicon, ocr=StubOcr(),
        pages=[long_page, long_page],
    )
    chunks = nrb_rag.chunks_from_recovery(
        recovered, max_chars=500, overlap_chars=50
    )
    assert len(chunks) > 2
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    # No chunk mixes pages: each carries exactly one page number, and page 1's
    # chunks all precede page 2's.
    pages = [c.page_number for c in chunks]
    assert pages == sorted(pages) and set(pages) == {1, 2}


# --------------------------------------------------------------------------- #
# 2. Only trustworthy text is indexable.
# --------------------------------------------------------------------------- #
def test_an_unresolved_conversion_contributes_no_chunk(tmp_path, lexicon):
    """The whole point of the fail-closed rule, at the ingestion boundary."""
    path = _write_pdf(
        tmp_path, "two.pdf",
        [{"font": "ABCDEE+Preeti", "embedded": True},
         {"font": "ABCDEE+Preeti", "embedded": True}],
    )

    class OnlySecondPage(StubConverter):
        def convert(self, text: str) -> str:
            self.calls.append(text)
            if text == PREETI:
                return ""                     # page 1: rejected → unresolved
            return "".join(c if c.isspace() else "क" for c in text)

    recovered = recovery.recover(
        path, _nrb_result(text=PREETI), converter=OnlySecondPage(), lexicon=lexicon,
        ocr=StubOcr(), pages=[PREETI, "g]kfn /fi6« a}+ssf] nflu dfq xf]"],
    )
    chunks = nrb_rag.chunks_from_recovery(
        recovered, max_chars=MAX_CHARS, overlap_chars=OVERLAP
    )
    assert [c.page_number for c in chunks] == [2]
    assert PREETI not in "".join(c.content for c in chunks)


# A synthetic PDF that the REAL classifier calls `legacy_font_suspected` at unit
# ratio 1.0 — four Preeti lines drawn with a Preeti font, which is exactly what
# an NRB circular is. The two tests below run the whole path (sniff → native-2 →
# route → convert → chunk) rather than a hand-built recovery result, because the
# thing they assert is what happens when that path finds nothing it can trust.
PREETI_PAGE = [
    PREETI,
    ";~rfns ;ldltn] b]xfosf ljlgodx? agfPsf] 5 .",
    "g]kfn /fi6« a}+ssf] k|of]hgsf] nflu dfq xf]",
    "clwsf/ k|of]u u/L b]xfosf lgodx? agfPsf] 5",
]


def test_a_document_with_nothing_indexable_raises_with_the_routing_outcome(
    tmp_path, lexicon
):
    """A recorded gap, never a document indexed with its glyph-mapped original.
    The message has to name WHY, or an operator cannot act on it."""
    path = _write_pdf(
        tmp_path, "all-bad.pdf",
        [{"font": "ABCDEE+Preeti", "embedded": True, "lines": PREETI_PAGE}],
    )
    with pytest.raises(nrb_rag.NrbParseError) as exc:
        _parse(path, converter=UselessConverter(), lexicon=lexicon, ocr=StubOcr())
    message = str(exc.value)
    assert "conversion_unresolved" in message
    assert "legacy_font_suspected" in message


def test_a_missing_converter_yields_no_chunks_rather_than_legacy_text(
    tmp_path, lexicon
):
    """npttf2utf is GPL-3. A deployment without it must produce an explicit
    unresolved extraction — not an index full of glyph-mapped ASCII."""
    path = _write_pdf(
        tmp_path, "preeti.pdf",
        [{"font": "ABCDEE+Preeti", "embedded": True, "lines": PREETI_PAGE}],
    )
    with pytest.raises(nrb_rag.NrbParseError) as exc:
        _parse(path, converter=None, lexicon=None, ocr=StubOcr())
    assert "conversion_unavailable" in str(exc.value)


def test_the_real_path_indexes_converted_text_and_not_its_input(tmp_path, lexicon):
    """The positive control for the two tests above: same file, a converter that
    works. Chunks appear, they carry the conversion route, and the glyph-mapped
    input is nowhere in them."""
    path = _write_pdf(
        tmp_path, "preeti.pdf",
        [{"font": "ABCDEE+Preeti", "embedded": True, "lines": PREETI_PAGE}],
    )
    chunks = _parse(path, converter=StubConverter(), lexicon=lexicon, ocr=StubOcr())
    assert chunks
    assert all(c.meta["route"] == recovery.ROUTE_LEGACY for c in chunks)
    assert all(c.page_number == 1 for c in chunks)
    body = "".join(c.content for c in chunks)
    assert PREETI not in body


def test_a_failed_ocr_page_contributes_no_chunk(tmp_path, lexicon):
    path = _write_pdf(
        tmp_path, "scan.pdf",
        [{"font": "Helvetica", "image": True}, {"font": "ABCDEE+Preeti", "embedded": True}],
    )
    recovered = recovery.recover(
        path, _nrb_result(text=SCAN_JUNK), converter=StubConverter(), lexicon=lexicon,
        ocr=StubOcr(fail=True), pages=[SCAN_JUNK, PREETI],
    )
    chunks = nrb_rag.chunks_from_recovery(
        recovered, max_chars=MAX_CHARS, overlap_chars=OVERLAP
    )
    assert [c.page_number for c in chunks] == [2]
    assert SCAN_JUNK not in "".join(c.content for c in chunks)


# --------------------------------------------------------------------------- #
# 3. The generic path is untouched.
# --------------------------------------------------------------------------- #
def test_a_generic_chunk_carries_no_metadata():
    """`Chunk.meta` is additive: every non-NRB path leaves it None, which
    `replace_chunks` stores as `{}` — the column's own default."""
    from app.rag.chunking import chunk_text

    chunks = chunk_text("a plain paragraph of text", max_chars=100, overlap_chars=10)
    assert chunks and all(c.meta is None for c in chunks)


def test_only_an_nrb_marked_document_takes_the_nrb_branch(tmp_path, monkeypatch):
    """The worker's single branch, asserted in both directions. A department's
    ordinary PDF must keep parsing exactly as it did.

    The branch moved from `_load_chunks_sync` to the async `_load_chunks` when
    the recovery cache landed — the NRB side needs a session, which cannot be
    opened inside `asyncio.to_thread`. The property is unchanged and so is the
    generic path: a non-NRB document still goes straight to `parse_to_chunks`
    in a thread, with no session and no NRB import.
    """
    import asyncio
    import hashlib

    from app.config import get_settings
    from app.nrb import filestore, recovery_cache
    from app.rag import worker

    called: list[str] = []

    def fake_generic(path, file_type, **kw):
        called.append("generic")
        return [Chunk(content="x", chunk_index=0)]

    async def fake_nrb(Session, path, **kw):
        called.append("nrb")
        return [Chunk(content="y", chunk_index=0, meta={"origin": "nrb"})], None

    monkeypatch.setattr(worker, "parse_to_chunks", fake_generic)
    monkeypatch.setattr(recovery_cache, "chunks_for_blob", fake_nrb)

    settings = get_settings()
    # Generic doc: a real file under RAG_DOCS_DIR, exactly as an upload has.
    stored = pathlib.Path(settings.rag_docs_dir) / "test-branch.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"%PDF-1.4\n")
    # NRB doc: NO rag copy (§28). Its bytes live in the filestore, resolved by
    # content hash — so point the filestore at tmp and put the blob there.
    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path)
    body = b"%PDF-1.4\nnrb\n"
    sha = hashlib.sha256(body).hexdigest()
    blob = tmp_path / filestore.storage_key_for(sha, "pdf")
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(body)
    try:
        plain = worker.DocSnapshot(
            id="d1", department_id=1, file_type="pdf",
            storage_key="test-branch.pdf", status="pending", meta={},
        )
        nrb = worker.DocSnapshot(
            id="d2", department_id=1, file_type="pdf",
            storage_key=f"{sha[:2]}/{sha}.pdf", status="pending",
            content_hash=sha, meta={"origin": "nrb"},
        )
        asyncio.run(worker._load_chunks(None, plain, settings))
        asyncio.run(worker._load_chunks(None, nrb, settings))
    finally:
        stored.unlink(missing_ok=True)

    assert called == ["generic", "nrb"]


def _nrb_result(*, family="pdf", status="suspicious", reason="legacy_font_suspected",
                text="", ratio=1.0):
    from .test_nrb_recovery import _result

    return _result(family=family, status=status, reason=reason, text=text, ratio=ratio)

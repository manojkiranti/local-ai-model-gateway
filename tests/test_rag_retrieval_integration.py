"""Hybrid retrieval against real Postgres. Skips if the DB is unreachable.

No Ollama needed: query vectors are supplied directly, so this exercises the SQL
(RRF fusion, department isolation, the ready guard) rather than the model.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db.session import engine as app_engine
from app.rag.retrieval import search_chunks

DIM = 1536


def _unit(slot: int) -> list[float]:
    """A unit vector pointing along one axis — distinct DIRECTION per slot, which
    is what cosine distance actually orders on."""
    vec = [0.0] * DIM
    vec[slot % DIM] = 1.0
    return vec


def _sql(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


def _vec_literal(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


@pytest.fixture()
def corpus():
    """Two departments, each with a ready document; plus a non-ready document in
    the first so the ready guard has something to exclude."""
    _skip_if_no_db()
    tag = uuid.uuid4().hex[:8]
    hr_doc, fin_doc, pending_doc = (uuid.uuid4().hex for _ in range(3))

    async def setup(conn):
        hr = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'HR') RETURNING id"),
            {"c": f"rhr{tag}"})).scalar_one()
        fin = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'FIN') RETURNING id"),
            {"c": f"rfin{tag}"})).scalar_one()

        async def add_doc(doc_id, dept, title, status, h):
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i,:d,:t,'upload','pdf',:h,:s)"),
                {"i": doc_id, "d": dept, "t": title, "h": h, "s": status})

        await add_doc(hr_doc, hr, "HR Leave Policy", "ready", "1" * 64)
        await add_doc(fin_doc, fin, "Finance Treasury Policy", "ready", "2" * 64)
        await add_doc(pending_doc, hr, "HR Draft", "pending", "3" * 64)

        async def add_chunk(doc_id, dept, idx, content, slot):
            await conn.execute(text(
                "INSERT INTO document_chunks (document_id, department_id, chunk_index,"
                " content, embedding, page_number, section)"
                " VALUES (:d,:dep,:i,:c, CAST(:v AS vector), :p, :s)"),
                {"d": doc_id, "dep": dept, "i": idx, "c": content,
                 "v": _vec_literal(_unit(slot)), "p": idx + 1, "s": "Leave Policy"})

        # HR: chunk 0 is the lexical+dense match for "annual leave"
        await add_chunk(hr_doc, hr, 0, "Annual leave accrues monthly for staff.", 0)
        await add_chunk(hr_doc, hr, 1, "Sick leave requires a medical certificate.", 5)
        await add_chunk(hr_doc, hr, 2, "Parking permits are issued yearly.", 9)
        # Finance: a deliberately similar sentence in ANOTHER department
        await add_chunk(fin_doc, fin, 0, "Annual leave accrues monthly for staff.", 0)
        # A chunk whose document is NOT ready must never surface. Written
        # directly here to construct the state; slice 2's pipeline cannot.
        await add_chunk(pending_doc, hr, 0, "Annual leave draft text.", 0)

        return hr, fin

    hr, fin = _sql(setup)
    yield {"hr": hr, "fin": fin, "hr_doc": hr_doc, "fin_doc": fin_doc}

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id IN (:a,:b)"),
                           {"a": hr, "b": fin})
        await conn.execute(text("DELETE FROM departments WHERE id IN (:a,:b)"),
                           {"a": hr, "b": fin})
    _sql(teardown)


def _search(dept, qtext, qvec, limit=10):
    """`search_chunks` uses the app's module-level SessionLocal — correct in
    production, where everything shares one event loop. Here each `asyncio.run`
    creates a new loop, so the pooled connections from the previous test belong
    to a closed one. Dispose between calls (see CLAUDE.md)."""
    s = get_settings()

    async def go():
        try:
            return await search_chunks(
                department_id=dept, query_text=qtext, query_vector=qvec,
                limit=limit, candidate_pool=s.rag_candidate_pool,
                rrf_k=s.rag_rrf_k, ef_search=s.rag_hnsw_ef_search,
            )
        finally:
            await app_engine.dispose()

    return asyncio.run(go())


def test_returns_results_for_the_requested_department(corpus):
    hits = _search(corpus["hr"], "annual leave", _unit(0))
    assert hits
    assert all(h.document_id == corpus["hr_doc"] for h in hits)


def test_department_isolation_finance_content_never_leaks_into_hr(corpus):
    """The Finance chunk is byte-identical to an HR chunk and has the same
    vector, so only the department filter can keep it out."""
    hits = _search(corpus["hr"], "annual leave", _unit(0))
    assert corpus["fin_doc"] not in {h.document_id for h in hits}

    other = _search(corpus["fin"], "annual leave", _unit(0))
    assert other and all(h.document_id == corpus["fin_doc"] for h in other)


def test_a_department_with_no_matching_corpus_returns_nothing(corpus):
    """A department id with no chunks yields an empty list, not an error."""
    assert _search(-1, "annual leave", _unit(0)) == []


def test_non_ready_documents_are_excluded(corpus):
    """Slice 2 makes chunks-exist imply ready; this is the belt-and-braces guard."""
    hits = _search(corpus["hr"], "annual leave draft", _unit(0))
    titles = {h.title for h in hits}
    assert "HR Draft" not in titles


def test_lexical_channel_finds_a_term_the_vector_misses(corpus):
    """The query vector points at slot 9 (the parking chunk's direction is 9, but
    we use an unrelated slot) — a hit on 'medical certificate' can then only come
    from the full-text side."""
    hits = _search(corpus["hr"], "medical certificate", _unit(400))
    assert any("medical certificate" in h.content for h in hits)


def test_dense_channel_finds_a_chunk_with_no_lexical_overlap(corpus):
    """Query text shares no stem with the parking chunk, so it can only be
    reached by vector similarity."""
    hits = _search(corpus["hr"], "zzzznomatchterm", _unit(9))
    assert any("Parking permits" in h.content for h in hits)


def test_results_are_ordered_by_descending_rrf_score(corpus):
    hits = _search(corpus["hr"], "annual leave", _unit(0))
    scores = [h.rrf_score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_rrf_score_matches_the_reciprocal_rank_formula(corpus):
    """A chunk that ranks #1 on BOTH channels scores 2/(k+1)."""
    s = get_settings()
    hits = _search(corpus["hr"], "annual leave accrues monthly", _unit(0))
    top = hits[0]
    assert top.dense_distance is not None and top.lexical_score is not None
    expected = 2.0 / (s.rag_rrf_k + 1)
    assert top.rrf_score == pytest.approx(expected, rel=1e-6)


def test_diagnostics_are_carried_through(corpus):
    hits = _search(corpus["hr"], "annual leave", _unit(0))
    top = hits[0]
    assert top.dense_distance is not None      # dense channel contributed
    assert top.lexical_score is not None       # lexical channel contributed
    assert 0.0 <= top.rrf_score <= 1.0


def test_citation_fields_are_populated(corpus):
    hits = _search(corpus["hr"], "annual leave", _unit(0))
    top = hits[0]
    assert top.title == "HR Leave Policy"
    assert top.page_number is not None
    assert top.section == "Leave Policy"
    assert top.document_id == corpus["hr_doc"]


def test_limit_is_respected(corpus):
    assert len(_search(corpus["hr"], "leave", _unit(0), limit=1)) == 1


def test_a_stopword_only_query_does_not_raise(corpus):
    """websearch_to_tsquery yields an empty query; the dense channel carries it."""
    hits = _search(corpus["hr"], "the of and", _unit(0))
    assert all(h.lexical_score is None for h in hits)  # lexical contributed nothing
    assert hits                                        # dense still returned


def test_punctuation_heavy_query_does_not_raise(corpus):
    """to_tsquery would throw on this; websearch_to_tsquery must not."""
    assert _search(corpus["hr"], "what is the & leave || policy ???", _unit(0)) is not None


def test_results_carry_the_rank_from_each_channel(corpus):
    """Diagnostics: when retrieval returns the wrong passage, these say WHICH
    channel surfaced it. Both ranks are already computed to drive RRF; before
    this they were never selected out, so diagnosing a bad result meant
    reproducing the query by hand."""
    rows = _search(corpus["hr"], "annual leave", _unit(0))
    assert rows
    for r in rows:
        # RRF only returns a row if at least one channel found it.
        assert r.dense_rank is not None or r.lexical_rank is not None
        assert r.dense_rank is None or r.dense_rank >= 1
        assert r.lexical_rank is None or r.lexical_rank >= 1


def test_a_chunk_only_one_channel_found_has_none_for_the_other(corpus):
    """A dense-only hit (no lexical overlap) must not be reported as if the
    lexical channel ranked it too — that would make the diagnostics lie about
    attribution. Reuses the same scenario as
    test_dense_channel_finds_a_chunk_with_no_lexical_overlap, which guarantees
    at least one genuinely dense-only row, so this isn't a vacuous check."""
    hits = _search(corpus["hr"], "zzzznomatchterm", _unit(9))
    dense_only = [h for h in hits if h.lexical_score is None]
    assert dense_only  # the parking chunk has no lexical overlap with the query
    for h in dense_only:
        assert h.dense_rank is not None
        assert h.lexical_rank is None
    for r in hits:
        if r.lexical_score is None:
            assert r.lexical_rank is None
        if r.dense_distance is None:
            assert r.dense_rank is None

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


def _search(dept, qtext, qvec, limit=10, pool=None):
    """`search_chunks` uses the app's module-level SessionLocal — correct in
    production, where everything shares one event loop. Here each `asyncio.run`
    creates a new loop, so the pooled connections from the previous test belong
    to a closed one. Dispose between calls (see CLAUDE.md)."""
    s = get_settings()

    async def go():
        try:
            return await search_chunks(
                department_id=dept, query_text=qtext, query_vector=qvec,
                limit=limit, candidate_pool=pool or s.rag_candidate_pool,
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
    """Every hit scores the sum of 1/(k + rank) over the channels that ranked
    it. The top chunk ranks #1 on all three here — dense, lexical, and title
    ("leave" is in "HR Leave Policy") — so it scores 3/(k+1)."""
    k = get_settings().rag_rrf_k
    hits = _search(corpus["hr"], "annual leave accrues monthly", _unit(0))
    top = hits[0]
    assert (top.dense_rank, top.lexical_rank, top.title_rank) == (1, 1, 1)
    assert top.rrf_score == pytest.approx(3.0 / (k + 1), rel=1e-6)
    for h in hits:
        ranks = [r for r in (h.dense_rank, h.lexical_rank, h.title_rank) if r is not None]
        assert h.rrf_score == pytest.approx(sum(1.0 / (k + r) for r in ranks), rel=1e-6)


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
    assert all(h.title_rank is None for h in hits)     # nor did the title channel
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
        assert (r.dense_rank, r.lexical_rank, r.title_rank) != (None, None, None)
        assert r.dense_rank is None or r.dense_rank >= 1
        assert r.lexical_rank is None or r.lexical_rank >= 1
        assert r.title_rank is None or r.title_rank >= 1


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


# --- The title channel (§9.10 design A) -------------------------------------
#
# Production's Circular No. 2 of FY 2083/84 never reached the top 12: its text
# differs from No. 1's only in a zero-padded `०२`, so the vectors cannot tell them
# apart, and the keyword channel ANDs every word of a natural question and so
# matches nothing. Only the TITLE says which circular is which.

NO1_TITLE = "परिपत्र नं. १ (क, ख, ग) २०८३/८४: एकीकृत निर्देशन, २०८२ मा संशोधन"
NO2_TITLE = "परिपत्र नं. २ (क, ख, ग) २०८३/८४: एकीकृत निर्देशन, २०८२ मा संशोधन"
# Identical in both documents, and it lacks "परिपत्र", so the keyword channel's
# AND of a natural question matches neither — as in production.
TWIN_TEXT = "एकीकृत निर्देशन, २०८२ को बुँदा ०२ मा संशोधन गरिएको छ ।"


@pytest.fixture()
def make_docs():
    """`make_docs([(title, content, slot), ...])` -> (department id, [doc ids]).

    Every document gets ONE ready chunk. Give two documents the same content and
    slot and nothing but the title can tell them apart. Each call makes its own
    department, so a test can build two to check isolation."""
    _skip_if_no_db()
    made = []

    def make(docs):
        tag = uuid.uuid4().hex[:8]
        ids = [uuid.uuid4().hex for _ in docs]

        async def setup(conn):
            dept = (await conn.execute(text(
                "INSERT INTO departments (code, name) VALUES (:c, 'Titles')"
                " RETURNING id"), {"c": f"rti{tag}"})).scalar_one()
            for doc_id, (title, content, slot) in zip(ids, docs):
                await conn.execute(text(
                    "INSERT INTO documents (id, department_id, title, source,"
                    " file_type, content_hash, status)"
                    " VALUES (:i, :d, :t, 'upload', 'pdf', :h, 'ready')"),
                    {"i": doc_id, "d": dept, "t": title, "h": doc_id + doc_id})
                await conn.execute(text(
                    "INSERT INTO document_chunks (document_id, department_id,"
                    " chunk_index, content, embedding, page_number)"
                    " VALUES (:d, :dep, 0, :c, CAST(:v AS vector), 1)"),
                    {"d": doc_id, "dep": dept, "c": content,
                     "v": _vec_literal(_unit(slot))})
            return dept

        dept = _sql(setup)
        made.append(dept)
        return dept, ids

    yield make

    async def teardown(conn):
        for dept in made:
            await conn.execute(text("DELETE FROM documents WHERE department_id = :d"),
                               {"d": dept})
            await conn.execute(text("DELETE FROM departments WHERE id = :d"),
                               {"d": dept})
    _sql(teardown)


def _twins(make_docs):
    dept, (no1, no2) = make_docs([(NO1_TITLE, TWIN_TEXT, 0), (NO2_TITLE, TWIN_TEXT, 0)])
    return dept, {"no1": no1, "no2": no2}


def _assert_first(hits, doc_id):
    """`doc_id` is first AND strictly ahead: a tie would mean the order was left
    to whichever row the database happened to emit first."""
    assert hits, "nothing retrieved"
    assert hits[0].document_id == doc_id, [h.title for h in hits]
    runner_up = next(h.rrf_score for h in hits if h.document_id != doc_id)
    assert hits[0].rrf_score > runner_up


@pytest.mark.parametrize("wanted, question", [
    ("no2", "परिपत्र नं. २ ले एकीकृत निर्देशन २०८२ मा के संशोधन गरेको छ?"),
    # The mirror image. The query VECTOR is the same for both questions, so an
    # order that ignores titles can satisfy at most one of the pair.
    ("no1", "परिपत्र नं. १ ले एकीकृत निर्देशन २०८२ मा के संशोधन गरेको छ?"),
])
def test_the_title_decides_between_documents_with_identical_text(make_docs, wanted, question):
    dept, twins = _twins(make_docs)
    _assert_first(_search(dept, question, _unit(0)), twins[wanted])


def test_identical_vectors_share_a_dense_rank(make_docs):
    """Tied distances get tied ranks. ROW_NUMBER() would hand one twin rank 1
    and the other rank 2 on nothing but row order, and under RRF that invented
    lead exactly cancels a title channel's real one."""
    dept, twins = _twins(make_docs)
    hits = _search(dept, "zzzznomatchterm", _unit(0))
    assert [h.dense_rank for h in hits] == [1, 1]


def test_identical_text_shares_a_lexical_rank(make_docs):
    dept, twins = _twins(make_docs)
    hits = _search(dept, "बुँदा", _unit(400))
    assert [h.lexical_rank for h in hits] == [1, 1]


def test_the_title_channel_reaches_a_document_the_dense_pool_missed(make_docs):
    """Production's case: Circular No. 2 was not even among the dense
    candidates. With a one-chunk pool, the decoy (nearest to the query vector)
    fills the dense channel, so No. 2 can only arrive through its title."""
    dept, (decoy, no1, no2) = make_docs([
        ("सूचना: ब्याजदर करिडोर", TWIN_TEXT, 3),
        (NO1_TITLE, TWIN_TEXT, 0),
        (NO2_TITLE, TWIN_TEXT, 0),
    ])
    hits = _search(dept, "परिपत्र नं. २ ले के संशोधन गरेको छ?", _unit(3), pool=1)
    by_doc = {h.document_id: h for h in hits}
    assert no2 in by_doc
    reached = by_doc[no2]
    assert (reached.dense_rank, reached.lexical_rank, reached.title_rank) == (None, None, 1)


@pytest.mark.parametrize("titles, question", [
    # An English question with ASCII digits finds a Nepali title's Devanagari
    # digits — the one thing an English question shares with a Nepali title.
    ((NO1_TITLE, NO2_TITLE), "What does Circular No. 2 of 2083/84 amend?"),
    # And the reverse: an English title's ASCII digits, a Nepali question.
    (("Circular No. 1 of 2083/84", "Circular No. 2 of 2083/84"),
     "परिपत्र नं. २ २०८३/८४ ले के संशोधन गरेको छ?"),
], ids=["ascii-question", "ascii-title"])
def test_title_digits_match_across_scripts(make_docs, titles, question):
    dept, (no1, no2) = make_docs([(titles[0], TWIN_TEXT, 0), (titles[1], TWIN_TEXT, 0)])
    _assert_first(_search(dept, question, _unit(0)), no2)


@pytest.mark.parametrize("titles, question", [
    ((NO1_TITLE, NO2_TITLE), "परिपत्र नं. ०२ ले के संशोधन गरेको छ?"),
    ((NO1_TITLE.replace("नं. १", "नं. ०१"), NO2_TITLE.replace("नं. २", "नं. ०२")),
     "परिपत्र नं. २ ले के संशोधन गरेको छ?"),
], ids=["padded-question", "padded-title"])
def test_title_numbers_ignore_zero_padding(make_docs, titles, question):
    """NRB writes `नं. ०७` in one title and `नं. ७` in the next; a question may
    use either. A number is its value, not its spelling."""
    dept, (no1, no2) = make_docs([(titles[0], TWIN_TEXT, 0), (titles[1], TWIN_TEXT, 0)])
    _assert_first(_search(dept, question, _unit(0)), no2)


def test_function_words_do_not_count_as_title_matches(make_docs):
    """A natural Nepali question is mostly grammar (के, हो, र, कसरी, गर्छ…). If
    those counted, a title that merely shares the grammar would outrank the one
    that names the subject."""
    dept, (grammar, corridor) = make_docs([
        ("यो के हो र यसले कसरी काम गर्छ", TWIN_TEXT, 0),
        ("ब्याजदर करिडोर", TWIN_TEXT, 0),
    ])
    hits = _search(dept, "ब्याजदर करिडोर भनेको के हो र यसले कसरी काम गर्छ?", _unit(0))
    _assert_first(hits, corridor)


# The question names a term (`बैंकलाई`) that the twins' titles lack and the
# rival titles below carry, so a rival counted in the ranking takes rank 1 from
# No. 2.
RIVAL_QUESTION = "परिपत्र नं. २ ले बैंकलाई के संशोधन गरेको छ?"
RIVAL_TITLE = "परिपत्र नं. २: बैंकलाई संशोधन"


def test_the_title_channel_never_crosses_departments(make_docs):
    """Another department's document, titled closer to the question, neither
    appears nor takes a title rank from this department's documents."""
    dept, twins = _twins(make_docs)
    _, (foreign,) = make_docs([(RIVAL_TITLE, "अर्को विभागको पाठ", 0)])
    hits = _search(dept, RIVAL_QUESTION, _unit(0))
    assert foreign not in {h.document_id for h in hits}
    assert {h.document_id: h.title_rank for h in hits}[twins["no2"]] == 1


def test_documents_that_are_not_ready_take_no_title_rank(make_docs):
    """A pending, failed or archived document keeps its title but has no chunks,
    so ranking it would spend a title rank on a document with nothing to give."""
    dept, (no1, no2, pending) = make_docs([
        (NO1_TITLE, TWIN_TEXT, 0), (NO2_TITLE, TWIN_TEXT, 0), (RIVAL_TITLE, TWIN_TEXT, 0),
    ])

    async def unready(conn):
        await conn.execute(text("DELETE FROM document_chunks WHERE document_id = :d"),
                           {"d": pending})
        await conn.execute(text("UPDATE documents SET status = 'pending' WHERE id = :d"),
                           {"d": pending})
    _sql(unready)

    hits = _search(dept, RIVAL_QUESTION, _unit(0))
    assert {h.document_id: h.title_rank for h in hits}[no2] == 1


def test_a_function_word_only_question_leaves_the_title_channel_empty(make_docs):
    dept, _ = _twins(make_docs)
    hits = _search(dept, "के हो?", _unit(0))
    assert hits                                    # dense still returned
    assert all(h.title_rank is None for h in hits)


def test_the_banks_abbreviation_is_not_a_title_match(make_docs):
    """§9.10's English corridor question fell from rank 2 to 16 on the first
    build: "NRB" is in dozens of English titles, so every one of them tied at
    title rank 1 on that single word and outvoted the corridor document, whose
    title is Nepali and so matches nothing an English question says."""
    dept, (plan, corridor) = make_docs([
        ("NRB Strategic Plan 2017-2021", TWIN_TEXT, 1),
        ("ब्याजदर करिडोर", TWIN_TEXT, 0),
    ])
    hits = _search(dept, "What is NRB's interest rate corridor and how does it work?", _unit(0))
    _assert_first(hits, corridor)

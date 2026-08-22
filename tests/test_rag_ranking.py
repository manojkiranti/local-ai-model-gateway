"""The abstention decision. No database, no GPU — `decide` is pure.

These tests are the guard on a user-visible refusal, so they are exhaustive
about the boundary rather than representative.
"""

import pytest

from app.rag.ranking import NO_SIGNAL_SCORE, Ranking, decide
from app.rag.retrieval import RetrievedChunk


def chunk(chunk_id: int, content: str = "text") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=f"doc{chunk_id}",
        title=f"Title {chunk_id}",
        content=content,
        page_number=None,
        section=None,
        element_type="text",
        rrf_score=1.0 / chunk_id,
        dense_distance=None,
        lexical_score=None,
        dense_rank=None,
        lexical_rank=None,
    )


def test_passages_above_the_threshold_are_kept():
    result = decide([chunk(1), chunk(2)], [0.9, 0.8], threshold=0.7, top_k=10)
    assert [c.chunk_id for c in result.kept] == [1, 2]
    assert result.abstained is False
    assert result.degraded is False


def test_everything_below_the_threshold_abstains():
    result = decide([chunk(1), chunk(2)], [0.2, 0.1], threshold=0.7, top_k=10)
    assert result.kept == []
    assert result.abstained is True


def test_only_the_passages_above_the_threshold_survive():
    result = decide([chunk(1), chunk(2), chunk(3)], [0.9, 0.3, 0.8],
                    threshold=0.7, top_k=10)
    assert [c.chunk_id for c in result.kept] == [1, 3]
    assert result.abstained is False


def test_a_score_exactly_on_the_threshold_is_kept():
    # `>=`, not `>`. A boundary that excluded its own value would make the
    # swept threshold mean something different from the number in the table.
    result = decide([chunk(1)], [0.7], threshold=0.7, top_k=10)
    assert [c.chunk_id for c in result.kept] == [1]


def test_results_are_reordered_by_relevance_not_by_rrf():
    # The whole point of a reranker: RRF order is 1,2,3; relevance says 3,1,2.
    result = decide([chunk(1), chunk(2), chunk(3)], [0.8, 0.75, 0.95],
                    threshold=0.7, top_k=10)
    assert [c.chunk_id for c in result.kept] == [3, 1, 2]


def test_top_k_truncates_after_ranking():
    result = decide([chunk(1), chunk(2), chunk(3)], [0.8, 0.9, 0.85],
                    threshold=0.1, top_k=2)
    assert [c.chunk_id for c in result.kept] == [2, 3]


def test_every_score_is_reported_even_for_rejected_passages():
    # Diagnostics must cover what was DROPPED — that is the interesting half
    # when someone asks why the assistant refused.
    result = decide([chunk(1), chunk(2)], [0.9, 0.1], threshold=0.7, top_k=10)
    assert result.scores == {1: 0.9, 2: 0.1}


def test_no_candidates_is_not_an_abstention():
    # Zero retrieved chunks is the tool's pre-existing "no matching passages"
    # branch, decided before ranking. `abstained` means "we had candidates and
    # rejected them all", which is a different fact and a different message.
    result = decide([], [], threshold=0.7, top_k=10)
    assert result.kept == []
    assert result.abstained is False


def test_mismatched_score_count_is_a_programming_error():
    with pytest.raises(ValueError):
        decide([chunk(1), chunk(2)], [0.9], threshold=0.7, top_k=10)


def test_the_no_signal_score_is_named_so_a_threshold_cannot_land_on_it():
    assert NO_SIGNAL_SCORE == 0.5

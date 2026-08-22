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


def test_a_threshold_equal_to_the_no_signal_score_ADMITS_the_no_opinion_passage():
    """The trap, pinned: at threshold == NO_SIGNAL_SCORE the boundary sits exactly
    on the sentinel `score_from_logprobs` returns when the reranker answered
    neither "yes" nor "no". Because `decide` compares with `>=`, that passage is
    KEPT and presented to the model as relevant — it is NOT refused.

    Documentation got this backwards once, in four files. The direction matters to
    whoever picks the operating point: at 0.5 you silently trust passages the
    reranker had no opinion about, and one notch higher you discard them. Either
    way the least informative case is decided by the comparison operator rather
    than by evidence, which is why 0.5 is disqualified as a threshold.
    """
    at = decide([chunk(1)], [NO_SIGNAL_SCORE], threshold=NO_SIGNAL_SCORE, top_k=10)
    assert [c.chunk_id for c in at.kept] == [1], "at the sentinel: admitted"
    assert at.abstained is False

    above = decide([chunk(1)], [NO_SIGNAL_SCORE], threshold=NO_SIGNAL_SCORE + 0.1, top_k=10)
    assert above.kept == [], "one notch above the sentinel: dropped"
    assert above.abstained is True


def test_a_zero_top_k_still_returns_one_passage_rather_than_abstaining():
    # A config typo (RAG_TOP_K=0) must not turn into "the bank has no policy on
    # anything". Degrading to one passage is the safe failure; abstaining is not.
    result = decide([chunk(1), chunk(2)], [0.9, 0.8], threshold=0.7, top_k=0)
    assert len(result.kept) == 1
    assert result.abstained is False


import asyncio
from dataclasses import dataclass as _dc

from app.rag.ranking import apply as apply_ranking


@_dc
class FakeSettings:
    rag_rerank_enabled: bool = True
    rag_rerank_model: str = "qwen3-reranker:4b"
    rag_relevance_threshold: float = 0.7
    rag_top_k: int = 10


class ScriptedClient:
    """Answers each rerank call with a queued yes-logprob. Records concurrency."""

    def __init__(self, yes_logprobs):
        self._queue = list(yes_logprobs)
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def chat(self, payload):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)  # yield, so overlap is observable
        self.in_flight -= 1
        logprob = self._queue.pop(0)
        return {
            "choices": [
                {"logprobs": {"content": [
                    {"top_logprobs": [{"token": "yes", "logprob": logprob}]}
                ]}}
            ]
        }


class BrokenClient:
    async def chat(self, payload):
        raise RuntimeError("GPU is on fire")


def test_scoring_runs_concurrently_not_one_at_a_time():
    # 20 sequential round trips was ~3s of added latency per search.
    client = ScriptedClient([0.0] * 4)
    asyncio.run(apply_ranking(client, "q", [chunk(i) for i in range(1, 5)],
                              settings=FakeSettings()))
    assert client.calls == 4
    assert client.max_in_flight > 1, "calls must overlap"


def test_a_failing_reranker_falls_back_to_rrf_order_and_does_not_abstain():
    # The rule this test exists to protect: an infrastructure failure must never
    # become a false statement about the corpus.
    chunks = [chunk(1), chunk(2)]
    result = asyncio.run(apply_ranking(BrokenClient(), "q", chunks,
                                       settings=FakeSettings()))
    assert result.degraded is True
    assert result.abstained is False
    assert [c.chunk_id for c in result.kept] == [1, 2]


def test_reranking_disabled_behaves_exactly_like_today():
    chunks = [chunk(1), chunk(2), chunk(3)]
    result = asyncio.run(apply_ranking(
        BrokenClient(), "q", chunks,
        settings=FakeSettings(rag_rerank_enabled=False)))
    assert result.degraded is True
    assert result.abstained is False
    assert [c.chunk_id for c in result.kept] == [1, 2, 3]


def test_degraded_still_respects_top_k():
    chunks = [chunk(i) for i in range(1, 6)]
    result = asyncio.run(apply_ranking(
        BrokenClient(), "q", chunks,
        settings=FakeSettings(rag_rerank_enabled=False, rag_top_k=2)))
    assert [c.chunk_id for c in result.kept] == [1, 2]


def test_no_candidates_makes_no_rerank_calls():
    client = ScriptedClient([])
    result = asyncio.run(apply_ranking(client, "q", [], settings=FakeSettings()))
    assert client.calls == 0
    assert result.abstained is False
    assert result.degraded is False


def test_an_abstention_is_logged_with_the_scores_that_caused_it(caplog):
    client = ScriptedClient([-9.0, -9.0])  # exp(-9) ~ 0.0001 -> far below 0.7
    with caplog.at_level("INFO", logger="app.rag.ranking"):
        result = asyncio.run(apply_ranking(
            client, "pension scheme", [chunk(1), chunk(2)],
            settings=FakeSettings()))
    assert result.abstained is True
    assert any("abstained" in r.getMessage() for r in caplog.records), caplog.text


def test_a_degraded_ranking_is_logged_as_a_warning(caplog):
    # A silently un-reranked deployment looks exactly like a working one. This
    # log line is the only thing that distinguishes them at runtime.
    with caplog.at_level("WARNING", logger="app.rag.ranking"):
        asyncio.run(apply_ranking(BrokenClient(), "q", [chunk(1)],
                                  settings=FakeSettings()))
    assert any(r.levelname == "WARNING" for r in caplog.records)


class MidPoolFailureClient:
    """Fails on one passage; the rest take a turn of the loop to finish.

    Models the real hazard: a bare `gather` re-raises immediately, the caller's
    `finally` closes the shared client, and the still-running siblings blow up on
    a closed client — noise on the very log an operator reads to diagnose a
    degraded deployment.
    """

    def __init__(self, fail_on: int = 2):
        self.fail_on = fail_on
        self.started = 0
        self.settled = 0

    async def chat(self, payload):
        self.started += 1
        mine = self.started
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if mine == self.fail_on:
                raise RuntimeError("GPU is on fire")
            return {
                "choices": [
                    {"logprobs": {"content": [
                        {"top_logprobs": [{"token": "yes", "logprob": 0.0}]}
                    ]}}
                ]
            }
        finally:
            self.settled += 1


def test_a_mid_pool_failure_settles_every_task_and_still_raises():
    from app.rag.rerank import rerank

    client = MidPoolFailureClient(fail_on=2)
    with pytest.raises(RuntimeError):
        asyncio.run(rerank(client, "q", ["a", "b", "c", "d"], model="m"))
    assert client.started == 4
    assert client.settled == 4, "a sibling was still in flight when we unwound"


def test_a_mid_pool_failure_still_degrades_rather_than_abstaining():
    client = MidPoolFailureClient(fail_on=2)
    result = asyncio.run(apply_ranking(
        client, "q", [chunk(i) for i in range(1, 5)], settings=FakeSettings()))
    assert result.degraded is True and result.abstained is False
    assert client.settled == 4


def test_the_disabled_path_says_why_it_degraded(caplog):
    # The fail-open path warns; the config path used to say nothing at all, so
    # "off by configuration" and "the reranker broke" were indistinguishable.
    # DEBUG, not INFO: this is the steady state today and would flood the log.
    with caplog.at_level("DEBUG", logger="app.rag.ranking"):
        asyncio.run(apply_ranking(
            BrokenClient(), "q", [chunk(1)],
            settings=FakeSettings(rag_rerank_enabled=False)))
    assert any(
        "RAG_RERANK_ENABLED" in r.getMessage() and r.levelname == "DEBUG"
        for r in caplog.records
    ), caplog.text


def test_the_score_distribution_is_logged_not_only_the_best(caplog):
    # This log line is the only data a future threshold refit will have, since
    # per-turn persistence is deferred.
    client = ScriptedClient([0.0, -1.0, -9.0])
    with caplog.at_level("INFO", logger="app.rag.ranking"):
        asyncio.run(apply_ranking(
            client, "q", [chunk(1), chunk(2), chunk(3)],
            settings=FakeSettings(rag_relevance_threshold=0.2)))
    text = caplog.text
    assert "min=" in text and "median=" in text and "max=" in text
    assert "query_chars=1" in text  # length only, never the query itself

"""Calibrated relevance and the abstention decision.

Fusion cannot support abstention. An RRF score is rank-derived, so the top hit
in a department containing nothing on the topic scores exactly like a perfect
match — `retrieval.py` says so and rightly refuses to threshold on it. A
cross-encoder produces a per-PAIR score, which is the quantity a threshold
needs.

The split here mirrors `permissions.py` / `access.py`: `decide` is pure and
`apply` does the IO. That matters because `decide` is the code that produces a
user-visible refusal, and it should be provable without a GPU.

**This module fails OPEN, deliberately inverting the rule used throughout
`app/nrb/`.** There, a failed recovery withholds its input, because publishing
machine-garbled text as authoritative is worse than publishing nothing. Here,
withholding an answer *asserts something false about the bank's own policies* —
a GPU hiccup rendered as "we have no policy on that" is a worse outcome than an
unranked but honest answer. So an unavailable reranker means RRF order,
`degraded=True`, and never an abstention.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from .rerank import rerank
from .retrieval import RetrievedChunk

# What `rerank.score_from_logprobs` returns when neither "yes" nor "no" appeared
# in the top logprobs — deliberately uninformative rather than confidently
# wrong. Named here because it disqualifies itself as a threshold: at 0.5 the
# least informative case sits exactly on the boundary.
NO_SIGNAL_SCORE = 0.5

# A floor of 1, and it is a fail-safe rather than a nicety. With top_k=0 and no
# floor, `kept` is empty, so `abstained` becomes True and a single misconfigured
# RAG_TOP_K=0 would refuse EVERY question in EVERY department while reporting it
# as "not in these documents" -- a false statement about the corpus, delivered
# universally, from a config typo. Degrading to one passage is the safe failure.
MIN_KEPT = 1


@dataclass(frozen=True)
class Ranking:
    """The outcome of ranking one search's candidates.

    `scores` covers every candidate, including the rejected ones — those are the
    interesting half when someone asks why the assistant refused to answer.
    """

    kept: list[RetrievedChunk]
    scores: dict[int, float]
    # True only when there WERE candidates and none cleared the threshold. Zero
    # candidates is a different fact, handled before ranking.
    abstained: bool
    # True when the score is not trustworthy (reranker off, absent or failing)
    # and `kept` is therefore RRF order. Recorded so a silently un-reranked
    # deployment is detectable rather than looking like a working one.
    degraded: bool


logger = logging.getLogger(__name__)


def decide(
    chunks: Sequence[RetrievedChunk],
    scores: Sequence[float],
    *,
    threshold: float,
    top_k: int,
) -> Ranking:
    """Keep the candidates at or above `threshold`, best first, capped at `top_k`.

    `>=` rather than `>` so the swept threshold means the same number in the
    table as it does here.
    """
    if len(chunks) != len(scores):
        raise ValueError(
            f"got {len(scores)} scores for {len(chunks)} chunks — "
            "the reranker must return one score per candidate"
        )
    if not chunks:
        return Ranking(kept=[], scores={}, abstained=False, degraded=False)

    by_id = {c.chunk_id: float(s) for c, s in zip(chunks, scores)}
    # Stable sort: equal scores keep the order fusion gave them, so a tie is
    # broken by RRF rather than arbitrarily.
    ordered = sorted(chunks, key=lambda c: by_id[c.chunk_id], reverse=True)
    kept = [c for c in ordered if by_id[c.chunk_id] >= threshold][: max(MIN_KEPT, top_k)]

    return Ranking(
        kept=kept,
        scores=by_id,
        abstained=not kept,
        degraded=False,
    )


def _degraded(chunks: Sequence[RetrievedChunk], top_k: int) -> Ranking:
    """RRF order, no abstention, flagged. The fail-open outcome."""
    return Ranking(
        kept=list(chunks[: max(MIN_KEPT, top_k)]),
        scores={},
        abstained=False,
        degraded=True,
    )


async def apply(
    client,
    query: str,
    chunks: Sequence[RetrievedChunk],
    *,
    settings,
) -> Ranking:
    """Score `chunks` against `query` and decide what to keep.

    `client` is anything satisfying `rerank.ChatClient` — passed in rather than
    constructed so this module needs no httpx import and no knowledge of the
    backend.
    """
    if not chunks:
        return Ranking(kept=[], scores={}, abstained=False, degraded=False)

    if not settings.rag_rerank_enabled:
        # Not an error path: an untuned deployment runs exactly as it does today.
        return _degraded(chunks, settings.rag_top_k)

    try:
        scores = await rerank(
            client,
            query,
            [c.content for c in chunks],
            model=settings.rag_rerank_model,
        )
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. Fail-open is the whole point: a new exception type
        # out of httpx, a timeout, a model that was evicted — none of them may
        # become a refusal, which the user would read as "the bank has no policy
        # on this". Narrowing this is a regression, not a tidy-up.
        logger.warning(
            "rerank unavailable (%s); falling back to RRF order without abstention",
            type(exc).__name__,
        )
        return _degraded(chunks, settings.rag_top_k)

    result = decide(
        chunks,
        scores,
        threshold=settings.rag_relevance_threshold,
        top_k=settings.rag_top_k,
    )
    # Diagnostics, deliberately at INFO for the ordinary case and carrying the
    # DROPPED scores too — those are the interesting half when someone asks why
    # the assistant refused. No query text: it is user input and may carry
    # confidential detail, so only its length is recorded.
    ranked = sorted(result.scores.values(), reverse=True)
    logger.info(
        "ranked %d candidates (query_chars=%d threshold=%.2f top=%s) -> "
        "kept %d%s",
        len(chunks),
        len(query),
        settings.rag_relevance_threshold,
        f"{ranked[0]:.3f}" if ranked else "n/a",
        len(result.kept),
        ", abstained" if result.abstained else "",
    )
    return result

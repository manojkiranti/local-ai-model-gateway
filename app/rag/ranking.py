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
    # How many candidates the reranker returned NO signal for. Reported so that
    # partial breakage is visible: these are kept (fail open) and excluded from
    # `scores`, so without this they would leave no trace at all.
    unscored: int = 0


logger = logging.getLogger(__name__)


def decide(
    chunks: Sequence[RetrievedChunk],
    scores: Sequence[float | None],
    *,
    threshold: float,
    top_k: int,
) -> Ranking:
    """Keep the candidates at or above `threshold`, best first, capped at `top_k`.

    `>=` rather than `>` so the swept threshold means the same number in the
    table as it does here.

    A score of **None means the reranker gave no signal** for that passage, and
    it is NOT a number to compare — it is kept, because this module fails open:
    withholding an answer asserts something false about the bank's own policies,
    and silence from the reranker is not evidence of irrelevance. It is also
    excluded from `scores`, so it cannot masquerade as a measurement in the
    distribution a threshold refit reads.

    If NOTHING scored, the reranker itself is broken and the result is
    `degraded` — the case that previously looked like a working reranker
    reporting uniform 0.5s.
    """
    if len(chunks) != len(scores):
        raise ValueError(
            f"got {len(scores)} scores for {len(chunks)} chunks — "
            "the reranker must return one score per candidate"
        )
    if not chunks:
        return Ranking(kept=[], scores={}, abstained=False, degraded=False)

    by_id = {
        c.chunk_id: float(s) for c, s in zip(chunks, scores) if s is not None
    }
    unscored = [c for c in chunks if c.chunk_id not in by_id]

    # Nothing scored at all: the reranker produced no usable signal, so there is
    # nothing to rank BY. Fall back rather than present a confident-looking
    # ordering built out of silence.
    if not by_id:
        return _degraded(chunks, top_k, unscored=len(unscored))

    # Stable sort: equal scores keep the order fusion gave them, so a tie is
    # broken by RRF rather than arbitrarily.
    scored = [c for c in chunks if c.chunk_id in by_id]
    ordered = sorted(scored, key=lambda c: by_id[c.chunk_id], reverse=True)
    # Scored survivors first (we have evidence for them), then the unscored in
    # the order fusion gave them.
    kept = ([c for c in ordered if by_id[c.chunk_id] >= threshold] + unscored)[
        : max(MIN_KEPT, top_k)
    ]

    return Ranking(
        kept=kept,
        scores=by_id,
        abstained=not kept,
        degraded=False,
        unscored=len(unscored),
    )


def _degraded(
    chunks: Sequence[RetrievedChunk], top_k: int, *, unscored: int = 0
) -> Ranking:
    """RRF order, no abstention, flagged. The fail-open outcome."""
    return Ranking(
        kept=list(chunks[: max(MIN_KEPT, top_k)]),
        scores={},
        abstained=False,
        degraded=True,
        unscored=unscored,
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
        # Logged all the same, because `degraded=True` otherwise cannot be told
        # apart from a reranker that broke — and DEBUG rather than the fail-open
        # path's WARNING, since this is today's steady state and INFO would emit a
        # line per search forever.
        logger.debug(
            "ranking degraded by configuration: RAG_RERANK_ENABLED is false, "
            "so %d candidates keep RRF order and abstention is impossible",
            len(chunks),
        )
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
    # Diagnostics, at INFO for the ordinary case. Per-turn score persistence is
    # deferred, so this line is the ONLY data a future threshold refit will have —
    # hence the whole DISTRIBUTION (min / median / max over every candidate,
    # rejected ones included) rather than just the best score. Emitted fields:
    # candidate count, query LENGTH, the threshold in force, min/median/max score,
    # how many were kept, and whether it abstained. Never the query text: it is
    # user input and may carry confidential detail.
    ranked = sorted(result.scores.values(), reverse=True)
    if ranked:
        mid = len(ranked) // 2
        median = (
            ranked[mid]
            if len(ranked) % 2
            else (ranked[mid - 1] + ranked[mid]) / 2
        )
        spread = f"min={ranked[-1]:.3f} median={median:.3f} max={ranked[0]:.3f}"
    else:
        spread = "min=n/a median=n/a max=n/a"
    logger.info(
        "ranked %d candidates (query_chars=%d threshold=%.2f %s%s) -> kept %d%s",
        len(chunks),
        len(query),
        settings.rag_relevance_threshold,
        spread,
        # Partial silence leaves no other trace: unscored candidates are kept
        # but excluded from the distribution, so without this an operator cannot
        # tell a confident reranker from one answering half the time.
        f" unscored={result.unscored}" if result.unscored else "",
        len(result.kept),
        ", abstained" if result.abstained else "",
    )
    return result

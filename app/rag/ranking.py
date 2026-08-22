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

from dataclasses import dataclass
from typing import Sequence

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

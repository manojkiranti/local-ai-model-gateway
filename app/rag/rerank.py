"""Optional cross-encoder reranking. **Disabled by default in this slice.**

Why it exists at all: RRF scores are rank-derived, so the fused output carries no
absolute relevance signal — the best hit in a department that contains nothing on
the topic scores exactly like a perfect match. Abstention ("I couldn't find this
in the HR documents") therefore cannot be driven by RRF, and needs a calibrated
per-pair score. That is what a reranker provides.

Why it is off: `qwen3-reranker` is not pulled, and this slice ships RRF ordering
as the baseline. **The consequence is explicit — with reranking disabled there is
no calibrated relevance score and therefore no abstention.** The tool reports how
many passages it found and lets the grounding prompt carry the "only answer from
these" instruction; it does not claim a passage is relevant.

Serving note, measured against the live Ollama in slice 2: there is **no**
`/rerank` endpoint (404 on `/api/rerank`, `/v1/rerank`, `/rerank`), but
`/v1/chat/completions` does return `logprobs` with `top_logprobs`. Qwen3-Reranker
is natively a yes/no logit read, so it runs as a one-token completion and the
score is `softmax(logprob_yes, logprob_no)`. That keeps the wire format inside
`app/ollama/client.py` and swaps to vLLM's native `/rerank` later untouched.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Protocol, Sequence

RERANK_PROMPT = (
    "Judge whether the passage contains information that answers the query.\n"
    "Answer with exactly one word: yes or no.\n\n"
    "Query: {query}\n\nPassage: {passage}\n\nAnswer:"
)


class ChatClient(Protocol):
    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def score_from_logprobs(top_logprobs: Sequence[dict[str, Any]]) -> float | None:
    """P(relevant) from the first token's alternatives, or None for NO SIGNAL.

    Softmax over just the yes/no mass, case-insensitively — the model may emit
    'Yes', 'yes' or ' yes' depending on tokenizer and prompt.

    **None, not 0.5, when neither token appears.** An earlier version returned
    0.5, which made silence indistinguishable from a genuine 50/50 and, worse,
    let it enter `decide`'s `>=` comparison — so the least informative case was
    settled by the comparison operator rather than by evidence. It also hid the
    failure that matters: a reranker producing nothing usable (wrong model,
    changed prompt format, unexpected tokenizer) scored EVERY passage 0.5, kept
    them all, and logged `min=0.500 median=0.500 max=0.500` — identical to a
    working reranker that was uncertain, while quietly poisoning the score
    distribution that a threshold refit depends on. A real tie still returns
    0.5: that is a measurement and stays one.
    """
    yes = no = None
    for item in top_logprobs:
        token = str(item.get("token", "")).strip().lower()
        logprob = item.get("logprob")
        if logprob is None:
            continue
        if token == "yes" and yes is None:
            yes = float(logprob)
        elif token == "no" and no is None:
            no = float(logprob)

    if yes is None and no is None:
        return None
    if yes is None:
        return 1.0 - math.exp(float(no))
    if no is None:
        return math.exp(yes)

    ey, en = math.exp(yes), math.exp(no)
    total = ey + en
    # Both masses underflowed to zero: arithmetically undefined, so it is
    # silence, not a tie.
    return None if total == 0 else ey / total


async def rerank(
    client: ChatClient,
    query: str,
    passages: Sequence[str],
    *,
    model: str,
) -> list[float | None]:
    """Score each passage against the query. One forward pass per passage, all
    issued CONCURRENTLY.

    Sequential scoring cost one round trip per candidate — at a pool of 20 and
    150 ms per call that was ~3 s of serial latency added to every search.
    `gather` lets the backend batch them, and preserves input order, which
    `ranking.decide` relies on to pair a score with its chunk.
    """

    async def one(passage: str) -> float | None:
        response = await client.chat(
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": RERANK_PROMPT.format(query=query, passage=passage),
                    }
                ],
                "max_tokens": 1,
                "temperature": 0.0,
                "logprobs": True,
                "top_logprobs": 5,
            }
        )
        choice = (response.get("choices") or [{}])[0]
        content = ((choice.get("logprobs") or {}).get("content") or [{}])[0]
        return score_from_logprobs(content.get("top_logprobs") or [])

    # Order is preserved by gather, and it is load-bearing: decide() zips these
    # against the chunk list.
    #
    # `return_exceptions=True` is NOT about swallowing failures — the first
    # exception is still re-raised below, so `ranking.apply` still degrades. It is
    # about *when*: a bare gather propagates immediately while up to pool-1 sibling
    # requests are still in flight, and the caller's `finally` then closes the
    # shared client under them ("Cannot send a request, as the client has been
    # closed" plus "Task exception was never retrieved" noise) — corrupting exactly
    # the log an operator reads to diagnose a degraded deployment. Collecting
    # settles every task before we unwind.
    results = await asyncio.gather(*(one(p) for p in passages), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return list(results)

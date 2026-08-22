# RAG Abstention & Retrieval Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give department RAG a calibrated per-passage relevance score so the assistant can say "that isn't in these documents" when true, and prove with a frozen labelled cohort that it does not say so when false.

**Architecture:** A new `app/rag/ranking.py` sits between fusion and formatting. Its decision is pure (`decide()`); only scoring does IO (`apply()`), calling the existing `app/rag/rerank.py` cross-encoder in parallel. Retrieval starts fetching the *rerank pool* rather than `top_k`, so the reranker can rescue a passage RRF ranked low. Ranking **fails open** — reranker unavailable means RRF order and no abstention. A frozen, hashed question cohort plus a sweep script fit the threshold; the threshold is not chosen by taste and is not enabled until the sweep has run.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy async + asyncpg, Postgres 17 + pgvector, httpx against Ollama's OpenAI-compatible surface, pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md`

## Global Constraints

- **Use this project's venv for everything:** `.venv/bin/python`, `.venv/bin/pytest`. Python 3.10. Never a sibling project's venv.
- **Never add the `openai` or `ollama` SDK.** The model server is reached over its OpenAI-compatible REST surface with httpx, and the wire format lives only in `app/ollama/client.py`.
- **`app/rag/ranking.py` must not import Postgres or `httpx`.** It takes a client through its parameters, exactly as `app/rag/rerank.py` does, so the decision logic stays unit-testable with no database and no GPU.
- **Ranking fails OPEN.** A reranker error, timeout, or absence yields RRF ordering with `degraded=True` and `abstained=False`. This deliberately inverts the fail-closed rule used throughout `app/nrb/`: there, withholding text prevents publishing machine-garbled text as authoritative; here, withholding an answer asserts something false about the bank's own policies. Any change that lets an infrastructure failure produce a refusal is a regression.
- **`0.5` is disqualified as a threshold value.** `rerank.score_from_logprobs` returns exactly `0.5` when neither "yes" nor "no" appears in the top logprobs — its deliberate "no signal" value. A threshold of `0.5` puts the least informative case exactly on the boundary.
- **`RAG_RERANK_ENABLED=false` must remain a working configuration** and must behave exactly as the system does today. It takes the same `degraded` path as the fail-open branch, so there is no second untested mode.
- **The eval cohort is frozen before tuning.** `docs/rag/retrieval-eval-cohort.json` carries a `parameters.sha256` over its own `questions` array and is committed before any threshold is fitted. Same discipline as the NRB cohorts: a cohort that can be redrawn after seeing results is not evidence.
- **Do not flip `RAG_RERANK_ENABLED` to true before Task 8.** The threshold is unfitted until the sweep has run; enabling abstention on the placeholder `0.5` would ship refusals nobody measured.
- Commit messages end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## Prerequisites (operator, not tasks)

Tasks 6-8 need a department with ingested `ready` documents. Ingestion is out of this plan's scope and uses the existing path — one call per file, then the worker drains the queue:

```bash
# per file, as a department editor
curl -H "Authorization: Bearer $TOKEN" -F file=@"<path>" \
     http://localhost:8000/v1/departments/<code>/documents
# then, in its own process
.venv/bin/python -m app.rag.worker
```

Tasks 1-5 need neither a corpus nor a GPU.

## Deviation from the spec (recorded, not silent)

The spec's §2.4(f) and §4 say rerank scores reach `chat_messages.trace`. On inspection that is awkward: the trace is a `list[dict]` with **one entry per agent-loop iteration**, and `chat/router._trace_if_tools` inspects entries for a `tool_calls` key to decide whether to persist anything at all. Threading tool-internal data into that list needs a new contextvar plumbing pass through `agent/loop.py`, for marginal gain over a log line — and a log line is what you actually alert on for the failure this is meant to catch (a silently degraded deployment, the §18 lesson).

**Task 5 therefore implements structured logging instead.** The honest cost: logs are less durable than a JSONB column, so the spec's "feedback capture" claim is weaker than written — accumulating per-turn scores for later threshold refitting remains a follow-up. Task 5's final step amends the spec to say so.

## File Structure

| File | Responsibility |
|---|---|
| `app/rag/ranking.py` *(new)* | `Ranking` result, pure `decide()`, IO-only `apply()`. The one place a refusal is decided. |
| `app/rag/rerank.py` *(modify)* | `rerank()` becomes parallel. Scoring only; no policy. |
| `app/rag/eval_metrics.py` *(new)* | Pure metrics: recall@k, MRR, abstention recall, false-refusal rate. No IO. |
| `app/tools/local/search_department_docs.py` *(modify)* | Fetches the pool, calls ranking, renders either passages or the abstain message. |
| `app/rag/sources.py` *(modify)* | Records a search that found nothing, so `sources: []` becomes reachable. |
| `app/config.py` *(modify)* | Pool size, threshold, enable flag. |
| `scripts/rag_eval_build_cohort.py` *(new)* | Generates cohort candidates from ingested chunks for human review; freezes with a hash. |
| `scripts/rag_eval_sweep.py` *(new)* | Runs the cohort across thresholds and pool sizes, prints the markdown table. |
| `tests/test_rag_ranking.py` *(new)* | `decide()` exhaustively + `apply()` fail-open, no DB, no GPU. |
| `tests/test_rag_eval_metrics.py` *(new)* | The metrics themselves, on a hand-built fixture. |
| `tests/test_rag_retrieval_eval.py` *(new)* | Runs the frozen cohort; skips without DB/corpus. |

---

### Task 1: The pure abstention decision

**Files:**
- Create: `app/rag/ranking.py`
- Test: `tests/test_rag_ranking.py`

**Interfaces:**
- Consumes: `app.rag.retrieval.RetrievedChunk` (existing frozen dataclass; fields used here are `chunk_id: int`, `content: str`).
- Produces:
  - `Ranking` frozen dataclass: `kept: list[RetrievedChunk]`, `scores: dict[int, float]`, `abstained: bool`, `degraded: bool`
  - `decide(chunks: Sequence[RetrievedChunk], scores: Sequence[float], *, threshold: float, top_k: int) -> Ranking`
  - `NO_SIGNAL_SCORE: float = 0.5`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rag_ranking.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rag_ranking.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.ranking'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/ranking.py`:

```python
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
    kept = [c for c in ordered if by_id[c.chunk_id] >= threshold][: max(1, top_k)]

    return Ranking(
        kept=kept,
        scores=by_id,
        abstained=not kept,
        degraded=False,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_ranking.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add app/rag/ranking.py tests/test_rag_ranking.py
git commit -m "$(cat <<'MSG'
feat(rag): the pure abstention decision

An RRF score is rank-derived, so it cannot support a relevance threshold --
retrieval.py says so and refuses to try. decide() takes per-pair scores from a
cross-encoder instead and is pure, so the code that produces a user-visible
refusal is provable without a GPU.

abstained means "there were candidates and none cleared the bar", never "there
were no candidates" -- the latter is the tool's existing branch and a different
message. NO_SIGNAL_SCORE names 0.5 to disqualify it as a threshold: that is
what score_from_logprobs returns when neither yes nor no appeared.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 2: Parallel scoring that fails open

**Files:**
- Modify: `app/rag/rerank.py` (the `rerank` function body)
- Modify: `app/rag/ranking.py` (add `apply`)
- Test: `tests/test_rag_ranking.py` (append)

**Interfaces:**
- Consumes: `Ranking`, `decide` (Task 1); `app.rag.rerank.rerank`, `app.rag.rerank.ChatClient` (existing Protocol with `async def chat(self, payload: dict) -> dict`).
- Produces: `async def apply(client, query: str, chunks: Sequence[RetrievedChunk], *, settings) -> Ranking`. `settings` is `app.config.Settings`; the fields read are `rag_rerank_enabled`, `rag_rerank_model`, `rag_relevance_threshold`, `rag_top_k`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rag_ranking.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rag_ranking.py -v -k "concurrently or falling or disabled or degraded or no_candidates"`
Expected: FAIL — `ImportError: cannot import name 'apply' from 'app.rag.ranking'`

- [ ] **Step 3: Make `rerank` parallel**

In `app/rag/rerank.py`, add `import asyncio` to the imports, and replace the body of `rerank` (the `scores: list[float] = []` loop) with:

```python
async def rerank(
    client: ChatClient,
    query: str,
    passages: Sequence[str],
    *,
    model: str,
) -> list[float]:
    """Score each passage against the query. One forward pass per passage, all
    issued CONCURRENTLY.

    Sequential scoring cost one round trip per candidate — at a pool of 20 and
    150 ms per call that was ~3 s of serial latency added to every search.
    `gather` lets the backend batch them, and preserves input order, which
    `ranking.decide` relies on to pair a score with its chunk.
    """

    async def one(passage: str) -> float:
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
    return list(await asyncio.gather(*(one(p) for p in passages)))
```

- [ ] **Step 4: Add `apply` to `app/rag/ranking.py`**

Add these imports at the top of `app/rag/ranking.py`:

```python
import logging

from .rerank import rerank
```

and append:

```python
logger = logging.getLogger(__name__)


def _degraded(chunks: Sequence[RetrievedChunk], top_k: int) -> Ranking:
    """RRF order, no abstention, flagged. The fail-open outcome."""
    return Ranking(
        kept=list(chunks[: max(1, top_k)]),
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

    return decide(
        chunks,
        scores,
        threshold=settings.rag_relevance_threshold,
        top_k=settings.rag_top_k,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_ranking.py -v`
Expected: PASS, 15 tests

- [ ] **Step 6: Check nothing else used the sequential rerank**

Run: `grep -rn "rerank(" app/ --include=*.py`
Expected: only the definition in `app/rag/rerank.py` and the call in `app/rag/ranking.py`. There were no other call sites before this plan.

- [ ] **Step 7: Commit**

```bash
git add app/rag/rerank.py app/rag/ranking.py tests/test_rag_ranking.py
git commit -m "$(cat <<'MSG'
feat(rag): score candidates concurrently, and fail open when scoring breaks

rerank() issued one HTTP round trip per candidate, serially -- ~3s of added
latency per search at a pool of 20. gather() lets the backend batch them and
preserves input order, which decide() relies on to pair a score with its chunk.

apply() catches Exception deliberately broadly. Fail-open is the point: a
timeout, an evicted model or a new httpx error must never become a refusal,
because a refusal asserts the bank has no policy on the subject. That inverts
the fail-closed rule in app/nrb/ for the reason written in the module docstring.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 3: Wire ranking into the retrieval tool

**Files:**
- Modify: `app/config.py:182` (`rag_rerank_pool`) and the comment block at `app/config.py:195-200`
- Modify: `app/tools/local/search_department_docs.py` (imports, the `_search_department_docs` body)
- Test: `tests/test_rag_search_tool_ranking.py` *(new)*

**Interfaces:**
- Consumes: `ranking.apply`, `ranking.Ranking` (Task 2).
- Produces: module constant `ABSTAIN` in `app/tools/local/search_department_docs.py`.

**Two traps in this task:**

1. **The Ollama client is currently closed before ranking could run.** The existing body does `client = OllamaClient(...)`, embeds inside `try`, and closes in `finally`. Ranking needs the same client, so the `finally` must move to cover both. Get this wrong and every search raises on a closed client.
2. **`search_chunks` is currently called with `limit=top_k`.** It must become `limit=settings.rag_rerank_pool`. This is the change that makes the reranker more than a reorderer, and it is invisible in the output — hence a dedicated test.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_search_tool_ranking.py`:

```python
"""The retrieval tool's ranking wiring.

Patched at the seams — no Postgres, no GPU. What is asserted here is the wiring
itself: that retrieval is asked for the POOL, that an abstention produces its own
message, and that the client survives long enough to rerank.
"""

import asyncio
from dataclasses import dataclass

import pytest

import app.tools.local.search_department_docs as tool
from app.rag.ranking import Ranking
from app.rag.retrieval import RetrievedChunk


def chunk(chunk_id: int, content: str = "Annual leave accrues monthly.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=f"doc{chunk_id}", title=f"Doc {chunk_id}",
        content=content, page_number=1, section=None, element_type="text",
        rrf_score=1.0 / chunk_id, dense_distance=None, lexical_score=None,
        dense_rank=None, lexical_rank=None,
    )


@dataclass
class FakeDept:
    id: int = 1
    code: str = "hr"


@pytest.fixture()
def wired(monkeypatch):
    """Patch everything outside the tool: department, embedding, retrieval, client."""
    seen = {}

    monkeypatch.setattr(tool, "current_department", lambda: FakeDept())

    async def fake_embed(client, texts, **kw):
        return [[0.0] * 8]

    monkeypatch.setattr(tool, "embed_texts", fake_embed)

    class FakeClient:
        def __init__(self, *a, **kw):
            self.closed = False

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(tool, "OllamaClient", FakeClient)

    async def fake_search(**kwargs):
        seen.update(kwargs)
        return seen["_returns"] if "_returns" in seen else [chunk(1), chunk(2)]

    monkeypatch.setattr(tool, "search_chunks", fake_search)
    return seen


def test_retrieval_is_asked_for_the_rerank_pool_not_top_k(wired, monkeypatch):
    # The point of a reranker is rescuing a passage RRF ranked low. Handed only
    # top_k it can do nothing but reorder. Invisible in output, so asserted here.
    from app.config import get_settings

    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=list(chunks), scores={}, abstained=False, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    asyncio.run(tool._search_department_docs({"query": "annual leave"}))
    assert wired["limit"] == get_settings().rag_rerank_pool


def test_an_abstention_returns_its_own_message(wired, monkeypatch):
    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=[], scores={}, abstained=True, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    out = asyncio.run(tool._search_department_docs({"query": "pension scheme"}))
    assert out == tool.ABSTAIN.format(code="hr")
    assert "Do NOT answer from general knowledge" in out


def test_the_abstain_message_is_distinct_from_the_no_results_message(wired, monkeypatch):
    # Both tell the model the same thing, but they are different diagnoses:
    # "retrieved nothing" vs "retrieved and rejected everything".
    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=[], scores={}, abstained=True, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    abstained = asyncio.run(tool._search_department_docs({"query": "q"}))
    wired["_returns"] = []
    nothing = asyncio.run(tool._search_department_docs({"query": "q"}))
    assert abstained != nothing


def test_kept_passages_are_what_gets_formatted(wired, monkeypatch):
    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=[chunks[1]], scores={}, abstained=False, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    out = asyncio.run(tool._search_department_docs({"query": "q"}))
    assert "Doc 2" in out
    assert "Doc 1" not in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rag_search_tool_ranking.py -v`
Expected: FAIL — `AttributeError: module 'app.tools.local.search_department_docs' has no attribute 'ranking'`

- [ ] **Step 3: Change the config**

In `app/config.py`, replace line 182 and the comment block at lines 195-200 with:

```python
    # Fused candidates the reranker scores. This is ALSO what retrieval fetches
    # (`search_chunks(limit=...)`) — handed only `rag_top_k`, a reranker could do
    # nothing but reorder what fusion already liked. 10 rather than 20 because
    # the calls are concurrent but not free; the eval sweep measures both.
    rag_rerank_pool: int = 10
```

```python
    # Reranking supplies the calibrated per-pair score that RRF cannot, and is
    # therefore what makes abstention possible. Keep it FALSE until the eval
    # sweep has fitted a threshold: enabling it on the placeholder below would
    # ship refusals nobody measured. False is a supported configuration and
    # behaves exactly as the system did before ranking existed.
    rag_rerank_enabled: bool = False
    rag_rerank_model: str = "qwen3-reranker:4b"
    # PLACEHOLDER until `scripts/rag_eval_sweep.py` fits it. 0.5 is also
    # disqualified on principle: it is exactly what `rerank.score_from_logprobs`
    # returns for "no signal", so at 0.5 the least informative case sits on the
    # boundary.
    rag_relevance_threshold: float = 0.5
```

- [ ] **Step 4: Wire the tool**

In `app/tools/local/search_department_docs.py`, add to the imports:

```python
from ...rag import ranking
```

Add this constant next to `NO_DEPARTMENT`:

```python
# Retrieval found candidates and ranking rejected all of them. Deliberately
# shaped like the zero-results message: an empty or vague result reads to the
# model as an unremarkable outcome and invites an answer from its own
# parameters, which is the exact failure abstention exists to prevent.
ABSTAIN = (
    "No sufficiently relevant passages were found in the {code} department's "
    "documents. Tell the user you could not find this in the {code} documents. "
    "Do NOT answer from general knowledge."
)
```

Then replace the body from `client = OllamaClient(...)` through `return text` with:

```python
    # ONE client for the whole call: embedding and reranking both use it, so it
    # is closed once, after ranking. Closing it after embedding (as an earlier
    # version did) makes every rerank raise on a closed client.
    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
    try:
        try:
            vectors = await embed_texts(
                client,
                [query],
                mode="query",  # asymmetric: queries carry the instruction prefix
                model=settings.rag_embed_model,
                dim=settings.rag_embed_dim,
                batch_size=1,
            )
        except (EmbeddingError, OllamaError) as exc:
            return f"ERROR: could not embed the query ({exc})."

        chunks = await search_chunks(
            department_id=department.id,
            query_text=query,
            query_vector=vectors[0],
            # The POOL, not top_k: ranking does the final cut, and a reranker
            # handed only what fusion liked can do nothing but reorder it.
            limit=settings.rag_rerank_pool,
            candidate_pool=settings.rag_candidate_pool,
            rrf_k=settings.rag_rrf_k,
            ef_search=settings.rag_hnsw_ef_search,
        )

        if not chunks:
            # Explicit, not an empty list: an empty result reads to the model as
            # an unremarkable outcome and invites an answer from its own
            # parameters.
            return (
                f"No matching passages were found in the {department.code} "
                f"department's documents. Tell the user you could not find this "
                f"in the {department.code} documents. Do NOT answer from "
                f"general knowledge."
            )

        result = await ranking.apply(client, query, chunks, settings=settings)
    finally:
        await client.aclose()

    if result.abstained:
        # A searched-but-empty result, recorded so the turn's `sources` can say
        # "searched, nothing relevant" ([]) rather than "never searched" (null).
        record_search(department.code, [])
        return ABSTAIN.format(code=department.code)

    text, presented = _format(
        # Sliced AGAIN, and not redundantly: ranking cut at `settings.rag_top_k`
        # while `top_k` is what the MODEL asked for, clamped. When the model asks
        # for fewer, this is the cut that honours it. Removing it silently
        # ignores the tool's own parameter.
        result.kept[:top_k],
        department_code=department.code,
        budget=settings.rag_tool_result_max_chars,
    )
    # Structured provenance for the turn's `sources`. The tool's own return value
    # is a string with nowhere to put it, so it goes out of band on a contextvar
    # — a no-op when nobody installed a collector.
    record_search(department.code, [_source_chunk(c) for c in presented])
    return text
```

- [ ] **Step 5: Run the new tests**

Run: `.venv/bin/pytest tests/test_rag_search_tool_ranking.py -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Run the existing RAG suite for regressions**

Run: `.venv/bin/pytest tests/ -k "rag or citation" -q`
Expected: no NEW failures. Two known pre-existing conditions: `tests/test_rag_reingest_integration.py::test_department_filter_restricts_the_set` fails on any developer database holding real data (documented in CLAUDE.md), and integration tests skip without Postgres. **Compare the skip count as well as the pass count** — the `_auth` helpers skip on auth failure, so a break can hide as silent skips.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/tools/local/search_department_docs.py tests/test_rag_search_tool_ranking.py
git commit -m "$(cat <<'MSG'
feat(rag): the retrieval tool ranks its candidates and can abstain

Retrieval now fetches RAG_RERANK_POOL rather than RAG_TOP_K. That is the change
that makes a reranker more than a reorderer -- handed only what fusion already
liked it cannot rescue the passage RRF ranked low -- and it is invisible in the
output, so it has its own test.

Abstention gets its own message rather than an empty result, for the reason the
zero-results branch already documents: a vague result reads to the model as
unremarkable and invites an answer from its own parameters.

RAG_RERANK_ENABLED stays false. The threshold is a placeholder until the sweep
fits it, and 0.5 is disqualified anyway -- it is score_from_logprobs' own
"no signal" value.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 4: Make `sources: []` reachable

**Files:**
- Modify: `app/rag/sources.py` (`SourceCollector.record`, `record_search` docstring)
- Modify: `tests/test_rag_sources.py:155-159` (replace `test_collector_ignores_empty_chunk_lists`)

**Interfaces:**
- Consumes: `record_search(department_code, [])` as called by Task 3's abstain path.
- Produces: no signature change. `SourceCollector.records` may now hold a `SearchRecord` with `chunks == []`.

**This changes deliberate existing behaviour**, so it gets its own task. `SourceCollector.record` currently has an `if chunks:` guard, and `tests/test_rag_sources.py::test_collector_ignores_empty_chunk_lists` pins it. The consequence today: an abstention (or a zero-result search) records nothing, `resolve_sources` sees `not records` and returns `None` — indistinguishable from a turn that never touched the corpus. The spec's `null` vs `[]` distinction exists in the docstring but is unreachable in practice.

- [ ] **Step 1: Write the failing tests**

In `tests/test_rag_sources.py`, replace `test_collector_ignores_empty_chunk_lists` (lines 155-159) with:

```python
def test_collector_records_a_search_that_found_nothing():
    # "searched, nothing relevant" and "never searched" are different facts and
    # resolve_sources renders them differently ([] vs null). Dropping the empty
    # record collapsed them, which made abstention indistinguishable from a
    # general chat that never touched the corpus.
    collector = SourceCollector()
    with source_scope(collector):
        record_search("hr", [])
    assert len(collector.records) == 1
    assert collector.records[0].department_code == "hr"
    assert collector.records[0].chunks == []


def test_a_search_that_found_nothing_resolves_to_empty_not_null():
    collector = SourceCollector()
    with source_scope(collector):
        record_search("hr", [])
    assert resolve_sources(collector.records, "I could not find that.") == []


def test_no_search_at_all_still_resolves_to_null():
    assert resolve_sources([], "A general answer.") is None
```

Ensure `resolve_sources` is imported in that module (it is already, per `tests/test_rag_sources.py:43`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rag_sources.py -v -k "found_nothing or no_search_at_all"`
Expected: FAIL — the first two fail (`len(collector.records) == 1` gets 0), the third passes already.

- [ ] **Step 3: Drop the guard**

In `app/rag/sources.py`, replace `SourceCollector.record` with:

```python
    def record(self, department_code: str, chunks: list[SourceChunk]) -> None:
        """Record one search, INCLUDING one that presented nothing.

        An empty record is not noise — it is the difference between "a corpus was
        searched and held nothing relevant" (`sources: []`) and "no corpus was
        searched at all" (`sources: null`). `resolve_sources` renders those
        differently, and an abstention is exactly the first case. Dropping empty
        records collapsed the two and made abstention look like a general chat.
        """
        self.records.append(
            SearchRecord(department_code=department_code, chunks=list(chunks))
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_sources.py -v`
Expected: PASS, all tests

- [ ] **Step 5: Check for callers that assumed empty records vanish**

Run: `.venv/bin/pytest tests/ -k "sources or citation" -q`
Expected: PASS. If a multi-search test now sees an extra record, that is this change working — verify the assertion's intent before editing it.

- [ ] **Step 6: Commit**

```bash
git add app/rag/sources.py tests/test_rag_sources.py
git commit -m "$(cat <<'MSG'
fix(rag): record a search that found nothing, so sources [] is reachable

SourceCollector.record dropped empty chunk lists, so resolve_sources saw no
records and returned null -- the same answer as a turn that never touched the
corpus. The null-vs-[] distinction was documented but unreachable, and
abstention is precisely the case that needs it.

Replaces test_collector_ignores_empty_chunk_lists, which pinned the old intent.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 5: Log ranking diagnostics

**Files:**
- Modify: `app/rag/ranking.py` (log in `apply`)
- Modify: `docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md` (§2.4(f), §4)
- Test: `tests/test_rag_ranking.py` (append)

**Interfaces:** no new public names. `apply` gains a log call.

See "Deviation from the spec" above: this replaces the spec's `chat_messages.trace` plan with structured logging, and amends the spec accordingly.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rag_ranking.py`:

```python
def test_an_abstention_is_logged_with_the_scores_that_caused_it(caplog):
    client = ScriptedClient([-9.0, -9.0])  # exp(-9) ~ 0.0001 -> far below 0.7
    with caplog.at_level("INFO", logger="app.rag.ranking"):
        result = asyncio.run(apply_ranking(
            client, "pension scheme", [chunk(1), chunk(2)],
            settings=FakeSettings()))
    assert result.abstained is True
    assert any("abstained" in r.message % r.args if r.args else "abstained" in r.message
               for r in caplog.records), caplog.text


def test_a_degraded_ranking_is_logged_as_a_warning(caplog):
    # A silently un-reranked deployment looks exactly like a working one. This
    # log line is the only thing that distinguishes them at runtime.
    with caplog.at_level("WARNING", logger="app.rag.ranking"):
        asyncio.run(apply_ranking(BrokenClient(), "q", [chunk(1)],
                                  settings=FakeSettings()))
    assert any(r.levelname == "WARNING" for r in caplog.records)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rag_ranking.py -v -k "logged"`
Expected: FAIL on the abstention test (no INFO line emitted); the WARNING test already passes from Task 2.

- [ ] **Step 3: Add the log line**

In `app/rag/ranking.py`, replace the final `return decide(...)` in `apply` with:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_ranking.py -v`
Expected: PASS, 17 tests

- [ ] **Step 5: Amend the spec**

In `docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md`, replace the §2.4(f) paragraph's final sentence (`rerank_score` joins them... "No migration: the trace is JSONB.") with:

```markdown
`rerank_score` is emitted as a structured log line rather than persisted into
`chat_messages.trace`. The trace is a per-iteration `list[dict]` whose shape
`chat/router._trace_if_tools` inspects, so threading tool-internal data into it
needs a new contextvar pass through `agent/loop.py` for marginal gain over a log
line — and a log line is what an operator alerts on for a silently degraded
deployment. **Cost, recorded honestly: logs are less durable than a JSONB
column, so accumulating per-turn scores for a later threshold refit is a
follow-up, not something this design delivers.**
```

And in §4's **Feedback capture** paragraph, replace it with:

```markdown
**Feedback capture.** Ranking emits a structured log line per search carrying
the candidate count, the top score, the threshold in force, and whether it
abstained or ran degraded. That makes a degraded deployment detectable — the §18
lesson that every way this breaks looks like it is working — and makes refusals
auditable after the fact. It is weaker than per-turn persistence: correlating a
specific user's rejected answer with its score needs the durable capture noted in
§2.4(f), which remains a follow-up.
```

- [ ] **Step 6: Commit**

```bash
git add app/rag/ranking.py tests/test_rag_ranking.py docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md
git commit -m "$(cat <<'MSG'
feat(rag): log ranking diagnostics, and amend the spec to match

A silently un-reranked deployment looks exactly like a working one, so the
degraded path warns and the ordinary path logs the top score, the threshold and
whether it abstained. Dropped scores are included: they are the interesting half
when someone asks why the assistant refused. Query text is not logged -- only its
length -- since it is user input.

This replaces the spec's plan to put scores in chat_messages.trace. The trace is
a per-iteration list whose shape _trace_if_tools inspects, so threading
tool-internal data through it needs a contextvar pass for marginal gain. The
spec is amended rather than quietly diverged from, including the honest cost:
durable per-turn score capture is now a follow-up.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 6: Generate the eval cohort for review

**Files:**
- Create: `scripts/rag_eval_build_cohort.py`
- Create: `docs/rag/retrieval-eval-cohort.json` (generated, then human-reviewed, then committed)

**Interfaces:**
- Produces: the cohort JSON contract that Tasks 7 and 8 read:
  - `parameters`: `{generated_at, department, document_count, chunk_count, sha256}`
  - `questions[]`: `{id, kind: "answerable"|"unanswerable", question, expect_document_id?, expect_section?, why?}`
  - `sha256` is over `json.dumps(questions, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()`

- [ ] **Step 1: Write the generator**

Create `scripts/rag_eval_build_cohort.py`:

```python
"""Build eval-cohort CANDIDATES from a department's ingested chunks.

Answerable questions are generated from chunks, which gives a free gold label:
the chunk's document is the expected document. Unanswerable negatives are real
questions built from a DIFFERENT department's chunks — real questions about real
documents that this department genuinely does not hold.

**Output is candidates, not a cohort.** A human reviews and edits, then re-runs
with --freeze to stamp the hash. Two limits, restated in the file it writes:

1. Chunk-derived questions reuse their source document's vocabulary, so they
   flatter the lexical channel. The answerable set measures an UPPER BOUND on
   recall; the negatives carry the trustworthy signal.
2. One department's corpus supports no population claim.

Usage:
  DATABASE_URL=... .venv/bin/python scripts/rag_eval_build_cohort.py \
      --department hr --negatives-from finance --answerable 40 --negatives 10 \
      --out docs/rag/retrieval-eval-cohort.json
  # human edits the file, then:
  DATABASE_URL=... .venv/bin/python scripts/rag_eval_build_cohort.py \
      --freeze docs/rag/retrieval-eval-cohort.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

# Long enough to carry a real claim, short enough that a generated question is
# about one thing.
MIN_CHUNK_CHARS = 300


def questions_hash(questions: list[dict]) -> str:
    payload = json.dumps(
        questions, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def _sample(engine, department_code: str, limit: int) -> list[dict]:
    sql = text(
        """
        SELECT c.content, c.section, c.document_id, d.title
          FROM document_chunks c
          JOIN documents   d ON d.id = c.document_id
          JOIN departments dep ON dep.id = c.department_id
         WHERE dep.code = :code
           AND d.status = 'ready'
           AND length(c.content) >= :min_chars
         ORDER BY c.document_id, c.chunk_index
        """
    )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(sql, {"code": department_code, "min_chars": MIN_CHUNK_CHARS})
        ).mappings().all()

    # Spread across documents rather than taking the first N chunks of the first
    # document — otherwise a 24-document corpus is measured on two of them.
    by_doc: dict[str, list[dict]] = {}
    for row in rows:
        by_doc.setdefault(row["document_id"], []).append(dict(row))
    rng = random.Random(20260822)  # fixed: regenerating must not reshuffle
    picked: list[dict] = []
    while len(picked) < limit and any(by_doc.values()):
        for doc_id in list(by_doc):
            bucket = by_doc[doc_id]
            if not bucket:
                del by_doc[doc_id]
                continue
            picked.append(bucket.pop(rng.randrange(len(bucket))))
            if len(picked) == limit:
                break
    return picked


def _draft_question(row: dict) -> str:
    """A placeholder a human rewrites. Deliberately NOT model-generated here:
    the generator must run with no GPU, and a human is reviewing every line
    anyway."""
    head = " ".join(row["content"].split())[:160]
    section = row["section"] or row["title"]
    return f"[REVIEW — rewrite as a user question about: {section}] {head}"


async def build(args) -> dict:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        positives = await _sample(engine, args.department, args.answerable)
        negatives = await _sample(engine, args.negatives_from, args.negatives)
        async with engine.connect() as conn:
            doc_count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM documents d JOIN departments dep"
                        " ON dep.id = d.department_id"
                        " WHERE dep.code = :c AND d.status = 'ready'"
                    ),
                    {"c": args.department},
                )
            ).scalar_one()
            chunk_count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM document_chunks c JOIN departments dep"
                        " ON dep.id = c.department_id WHERE dep.code = :c"
                    ),
                    {"c": args.department},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    if not positives:
        raise SystemExit(
            f"no ready documents with chunks >= {MIN_CHUNK_CHARS} chars in "
            f"department '{args.department}' — ingest the corpus first"
        )

    questions = [
        {
            "id": f"q{i:03d}",
            "kind": "answerable",
            "question": _draft_question(row),
            "expect_document_id": row["document_id"],
            "expect_section": row["section"],
        }
        for i, row in enumerate(positives, start=1)
    ] + [
        {
            "id": f"n{i:03d}",
            "kind": "unanswerable",
            "question": _draft_question(row),
            "why": f"drawn from the '{args.negatives_from}' corpus, not '{args.department}'",
        }
        for i, row in enumerate(negatives, start=1)
    ]

    return {
        "parameters": {
            "generated_at": date.today().isoformat(),
            "department": args.department,
            "negatives_from": args.negatives_from,
            "document_count": int(doc_count),
            "chunk_count": int(chunk_count),
            "sha256": None,  # stamped by --freeze, AFTER human review
        },
        "limitations": [
            "Chunk-derived questions reuse their source document's vocabulary, "
            "so they flatter the lexical channel. The answerable set measures an "
            "upper bound on recall; the negatives carry the trustworthy signal.",
            "One department's corpus supports no population claim. Re-sweep for a "
            "corpus that differs in size or character.",
            "expect_document_id is DOCUMENT granularity: the right document via "
            "the wrong passage scores as a hit. expect_section is diagnostic only.",
        ],
        "questions": questions,
    }


def freeze(path: Path) -> None:
    cohort = json.loads(path.read_text())
    unreviewed = [q["id"] for q in cohort["questions"] if "[REVIEW" in q["question"]]
    if unreviewed:
        raise SystemExit(
            f"{len(unreviewed)} question(s) still carry the REVIEW marker "
            f"({', '.join(unreviewed[:5])}…). A cohort is evidence only if a "
            "human wrote its questions."
        )
    cohort["parameters"]["sha256"] = questions_hash(cohort["questions"])
    path.write_text(json.dumps(cohort, indent=2, ensure_ascii=False) + "\n")
    print(f"frozen: {cohort['parameters']['sha256']}")
    print(f"{len(cohort['questions'])} questions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--department")
    parser.add_argument("--negatives-from")
    parser.add_argument("--answerable", type=int, default=40)
    parser.add_argument("--negatives", type=int, default=10)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--freeze", type=Path)
    args = parser.parse_args()

    if args.freeze:
        freeze(args.freeze)
        return
    if not (args.department and args.negatives_from and args.out):
        raise SystemExit("--department, --negatives-from and --out are required")

    cohort = asyncio.run(build(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(cohort, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.out} — {len(cohort['questions'])} CANDIDATES")
    print("Review every question, then re-run with --freeze to stamp the hash.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it refuses to freeze unreviewed candidates**

```bash
mkdir -p /tmp/ce && cat > /tmp/ce/c.json <<'EOF'
{"parameters":{"sha256":null},"questions":[{"id":"q001","kind":"answerable","question":"[REVIEW — rewrite] x"}]}
EOF
.venv/bin/python scripts/rag_eval_build_cohort.py --freeze /tmp/ce/c.json
```
Expected: exits non-zero with "still carry the REVIEW marker".

- [ ] **Step 3: Verify the hash is stable and content-sensitive**

```bash
.venv/bin/python -c "
from scripts.rag_eval_build_cohort import questions_hash
a = [{'id':'q001','question':'x'}]
b = [{'id':'q001','question':'y'}]
assert questions_hash(a) == questions_hash(a)
assert questions_hash(a) != questions_hash(b)
print('hash ok')
"
```
Expected: `hash ok`

- [ ] **Step 4: Generate candidates against the real corpus**

```bash
DATABASE_URL=<url> .venv/bin/python scripts/rag_eval_build_cohort.py \
  --department <code> --negatives-from <other-code> \
  --answerable 40 --negatives 10 \
  --out docs/rag/retrieval-eval-cohort.json
```
Expected: `wrote docs/rag/retrieval-eval-cohort.json — 50 CANDIDATES`

- [ ] **Step 5: STOP — hand the file to the human reviewer**

Every question carries a `[REVIEW …]` marker and must be rewritten as a question a real user would ask. `--freeze` refuses until they are all gone. **Do not invent the questions and freeze them yourself: a cohort is evidence only because a human vouched for its labels.**

- [ ] **Step 6: Freeze and commit the reviewed cohort**

```bash
.venv/bin/python scripts/rag_eval_build_cohort.py --freeze docs/rag/retrieval-eval-cohort.json
git add scripts/rag_eval_build_cohort.py docs/rag/retrieval-eval-cohort.json
git commit -m "$(cat <<'MSG'
test(rag): freeze the retrieval eval cohort

Answerable questions come from ingested chunks, which gives a free gold label
(the chunk's document). Negatives are real questions drawn from ANOTHER
department's corpus -- genuinely unanswerable here, and not phrased in this
corpus's vocabulary either way, which is why they carry the trustworthy signal.

The generator writes CANDIDATES and refuses to freeze while any still carries
its REVIEW marker: a cohort is evidence only because a human vouched for the
labels. Frozen before any threshold is fitted, so it cannot be redrawn to
flatter a result.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 7: The metrics, and the scorer that runs the cohort

**Files:**
- Create: `app/rag/eval_metrics.py`
- Create: `tests/test_rag_eval_metrics.py`
- Create: `tests/test_rag_retrieval_eval.py`

**Interfaces:**
- Consumes: the cohort contract (Task 6); `ranking.Ranking` (Task 1).
- Produces:
  - `@dataclass(frozen=True) Outcome`: `question_id: str`, `answerable: bool`, `abstained: bool`, `returned_document_ids: list[str]`, `expected_document_id: str | None`
  - `@dataclass(frozen=True) Report`: `recall_at_k: float`, `mrr: float`, `abstention_recall: float`, `false_refusal_rate: float`, `answerable: int`, `unanswerable: int`
  - `score(outcomes: Sequence[Outcome]) -> Report`

- [ ] **Step 1: Write the failing metric tests**

Create `tests/test_rag_eval_metrics.py`:

```python
"""The metrics themselves, on hand-built outcomes with known answers.

A broken metric reports a false pass, so the scorer is tested before it is
trusted to judge retrieval.
"""

import pytest

from app.rag.eval_metrics import Outcome, score


def ans(qid, expected, returned, abstained=False):
    return Outcome(question_id=qid, answerable=True, abstained=abstained,
                   returned_document_ids=returned, expected_document_id=expected)


def neg(qid, abstained):
    return Outcome(question_id=qid, answerable=False, abstained=abstained,
                   returned_document_ids=[], expected_document_id=None)


def test_recall_counts_the_expected_document_anywhere_in_the_results():
    r = score([ans("q1", "A", ["B", "A"]), ans("q2", "C", ["D"])])
    assert r.recall_at_k == 0.5


def test_mrr_rewards_a_higher_rank():
    first = score([ans("q1", "A", ["A", "B"])]).mrr
    second = score([ans("q1", "A", ["B", "A"])]).mrr
    assert first == 1.0
    assert second == 0.5


def test_a_miss_contributes_zero_to_mrr():
    assert score([ans("q1", "A", ["B", "C"])]).mrr == 0.0


def test_abstention_recall_is_over_the_unanswerable_questions_only():
    r = score([neg("n1", True), neg("n2", False), ans("q1", "A", ["A"])])
    assert r.abstention_recall == 0.5


def test_false_refusal_rate_is_over_the_answerable_questions_only():
    # The number that governs the operating point: refusing a question the
    # corpus answers is worse than answering it imperfectly.
    r = score([ans("q1", "A", [], abstained=True), ans("q2", "B", ["B"]),
               neg("n1", False)])
    assert r.false_refusal_rate == 0.5


def test_an_abstained_answerable_question_is_not_also_counted_as_recall():
    r = score([ans("q1", "A", [], abstained=True)])
    assert r.recall_at_k == 0.0
    assert r.false_refusal_rate == 1.0


def test_counts_are_reported_so_a_rate_can_be_read_in_context():
    r = score([ans("q1", "A", ["A"]), neg("n1", True), neg("n2", True)])
    assert (r.answerable, r.unanswerable) == (1, 2)


def test_no_questions_of_a_kind_yields_zero_not_a_crash():
    r = score([ans("q1", "A", ["A"])])
    assert r.abstention_recall == 0.0
    assert r.unanswerable == 0


def test_an_empty_outcome_set_is_rejected():
    with pytest.raises(ValueError):
        score([])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_rag_eval_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.eval_metrics'`

- [ ] **Step 3: Write the metrics**

Create `app/rag/eval_metrics.py`:

```python
"""Retrieval eval metrics. Pure — no database, no model, no cohort loading.

Four numbers, and they are not equally important. **False-refusal rate governs
the operating point**: an assistant that refuses questions the corpus answers
reads to users as broken, and that is worse than the over-confidence it
replaces — over-confidence yields a wrong answer the user may catch, a false
refusal denies a correct answer the corpus contained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Outcome:
    """What one cohort question actually produced."""

    question_id: str
    answerable: bool
    abstained: bool
    # Document ids in the order the tool presented them, best first.
    returned_document_ids: list[str]
    expected_document_id: Optional[str]


@dataclass(frozen=True)
class Report:
    recall_at_k: float
    mrr: float
    abstention_recall: float
    false_refusal_rate: float
    answerable: int
    unanswerable: int


def _ratio(hits: int, total: int) -> float:
    # A kind with no questions is 0.0, not a division error: a sweep row that
    # crashed would be read as "no data" anyway, and a crash loses the other
    # three numbers with it.
    return (hits / total) if total else 0.0


def score(outcomes: Sequence[Outcome]) -> Report:
    if not outcomes:
        raise ValueError("no outcomes to score — the cohort ran zero questions")

    answerable = [o for o in outcomes if o.answerable]
    unanswerable = [o for o in outcomes if not o.answerable]

    recall_hits = 0
    reciprocal = 0.0
    for outcome in answerable:
        if outcome.abstained or outcome.expected_document_id is None:
            # An abstention retrieved nothing the model could use, so it cannot
            # also count as a retrieval success.
            continue
        ids = outcome.returned_document_ids
        if outcome.expected_document_id in ids:
            recall_hits += 1
            reciprocal += 1.0 / (ids.index(outcome.expected_document_id) + 1)

    return Report(
        recall_at_k=_ratio(recall_hits, len(answerable)),
        mrr=(reciprocal / len(answerable)) if answerable else 0.0,
        abstention_recall=_ratio(
            sum(1 for o in unanswerable if o.abstained), len(unanswerable)
        ),
        false_refusal_rate=_ratio(
            sum(1 for o in answerable if o.abstained), len(answerable)
        ),
        answerable=len(answerable),
        unanswerable=len(unanswerable),
    )
```

- [ ] **Step 4: Run to verify the metric tests pass**

Run: `.venv/bin/pytest tests/test_rag_eval_metrics.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Write the cohort runner**

Create `tests/test_rag_retrieval_eval.py`:

```python
"""Run the frozen cohort end to end and report the four metrics.

Skips unless the cohort exists, Postgres is reachable, and the embedding model
answers — this is a measurement, not a unit test, and a skipped measurement is
honest where a fabricated one is not.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.db.session import engine as app_engine
from app.rag import ranking
from app.rag.embedding import embed_texts
from app.rag.eval_metrics import Outcome, score
from app.rag.retrieval import search_chunks
from app.ollama.client import OllamaClient
from scripts.rag_eval_build_cohort import questions_hash

COHORT = Path("docs/rag/retrieval-eval-cohort.json")


def _load():
    if not COHORT.exists():
        pytest.skip(f"{COHORT} not present — run scripts/rag_eval_build_cohort.py")
    cohort = json.loads(COHORT.read_text())
    stamped = cohort["parameters"].get("sha256")
    if not stamped:
        pytest.skip("cohort is not frozen — run --freeze after human review")
    actual = questions_hash(cohort["questions"])
    assert actual == stamped, (
        f"cohort questions changed since freezing ({actual} != {stamped}). "
        "A cohort edited after results were seen is no longer evidence — "
        "re-freeze deliberately and say so in the spec."
    )
    return cohort


async def _run_one(client, dept_id: int, question: str, settings) -> Outcome:
    vectors = await embed_texts(
        client, [question], mode="query", model=settings.rag_embed_model,
        dim=settings.rag_embed_dim, batch_size=1,
    )
    chunks = await search_chunks(
        department_id=dept_id, query_text=question, query_vector=vectors[0],
        limit=settings.rag_rerank_pool, candidate_pool=settings.rag_candidate_pool,
        rrf_k=settings.rag_rrf_k, ef_search=settings.rag_hnsw_ef_search,
    )
    result = await ranking.apply(client, question, chunks, settings=settings)
    seen: list[str] = []
    for chunk in result.kept:
        if chunk.document_id not in seen:
            seen.append(chunk.document_id)
    return Outcome(
        question_id="", answerable=False, abstained=result.abstained,
        returned_document_ids=seen, expected_document_id=None,
    )


def test_the_frozen_cohort_reports_its_metrics(capsys):
    cohort = _load()
    settings = get_settings()

    async def go():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                dept_id = (
                    await conn.execute(
                        text("SELECT id FROM departments WHERE code = :c"),
                        {"c": cohort["parameters"]["department"]},
                    )
                ).scalar_one_or_none()
        finally:
            await engine.dispose()
        if dept_id is None:
            pytest.skip("cohort's department is not in this database")

        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        outcomes = []
        try:
            for q in cohort["questions"]:
                base = await _run_one(client, dept_id, q["question"], settings)
                outcomes.append(
                    Outcome(
                        question_id=q["id"],
                        answerable=q["kind"] == "answerable",
                        abstained=base.abstained,
                        returned_document_ids=base.returned_document_ids,
                        expected_document_id=q.get("expect_document_id"),
                    )
                )
        finally:
            await client.aclose()
            await app_engine.dispose()
        return outcomes

    try:
        outcomes = asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"eval prerequisites unavailable: {type(exc).__name__}: {exc}")

    report = score(outcomes)
    with capsys.disabled():
        print(
            f"\nthreshold={settings.rag_relevance_threshold} "
            f"pool={settings.rag_rerank_pool} "
            f"rerank={'on' if settings.rag_rerank_enabled else 'OFF (degraded)'}\n"
            f"  recall@k            {report.recall_at_k:.3f}\n"
            f"  MRR                 {report.mrr:.3f}\n"
            f"  abstention recall   {report.abstention_recall:.3f}"
            f"  ({report.unanswerable} negatives)\n"
            f"  FALSE REFUSAL RATE  {report.false_refusal_rate:.3f}"
            f"  ({report.answerable} answerable)\n"
        )

    # Not a quality gate — the gate is the human reading the sweep table. This
    # only catches a harness that measured nothing.
    assert report.answerable > 0
```

- [ ] **Step 6: Run it**

Run: `.venv/bin/pytest tests/test_rag_retrieval_eval.py -v -s`
Expected: either the four metrics printed, or a clear skip naming the missing prerequisite. Both are acceptable outcomes of this step.

- [ ] **Step 7: Commit**

```bash
git add app/rag/eval_metrics.py tests/test_rag_eval_metrics.py tests/test_rag_retrieval_eval.py
git commit -m "$(cat <<'MSG'
test(rag): measure retrieval -- recall@k, MRR, abstention, false refusals

Retrieval accuracy was the one quality-critical surface here with no eval: the
existing integration tests cover fusion mechanics and department isolation, not
whether the right passage comes back. No threshold could be chosen and no
regression detected.

false_refusal_rate governs the operating point. Refusing a question the corpus
answers is worse than the over-confidence it replaces: over-confidence yields a
wrong answer the user may catch, a false refusal denies a correct one.

The runner verifies the cohort hash before trusting it -- a cohort edited after
results were seen is no longer evidence -- and skips rather than fabricating when
Postgres or the embedding model is unavailable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

### Task 8: Fit the threshold and turn abstention on

**Files:**
- Create: `scripts/rag_eval_sweep.py`
- Modify: `app/config.py` (`rag_relevance_threshold`, `rag_rerank_enabled`, `rag_rerank_pool`)
- Modify: `docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md` (§3.3 table)

**Interfaces:**
- Consumes: the cohort (Task 6), `eval_metrics.score` / `Outcome` (Task 7), `ranking.decide` (Task 1).
- Produces: no new importable names. Output is a markdown table and a fitted config value.

**Prerequisite:** `ollama pull qwen3-reranker:4b` on the model host, and `RAG_RERANK_ENABLED=true` **in the sweep's environment only** — not committed until Step 5.

- [ ] **Step 1: Write the sweep**

Create `scripts/rag_eval_sweep.py`:

```python
"""Sweep the abstention threshold and pool size over the frozen cohort.

Scores each question ONCE per pool size and reuses those scores across every
threshold — the reranker is the expensive part and a threshold is just a
comparison. 9 thresholds x 2 pools over 50 questions costs 2 scoring passes,
not 18.

Usage:
  DATABASE_URL=... RAG_RERANK_ENABLED=true \
    .venv/bin/python scripts/rag_eval_sweep.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db.session import engine as app_engine
from app.ollama.client import OllamaClient
from app.rag.embedding import embed_texts
from app.rag.eval_metrics import Outcome, score
from app.rag.ranking import NO_SIGNAL_SCORE, decide
from app.rag.rerank import rerank
from app.rag.retrieval import search_chunks
from scripts.rag_eval_build_cohort import questions_hash

COHORT = Path("docs/rag/retrieval-eval-cohort.json")
THRESHOLDS = [round(0.1 * i, 1) for i in range(1, 10)]
POOLS = [10, 20]


async def _scored(client, dept_id, cohort, pool, settings):
    """(question, chunks, scores) for every cohort question at this pool size."""
    out = []
    for q in cohort["questions"]:
        vectors = await embed_texts(
            client, [q["question"]], mode="query", model=settings.rag_embed_model,
            dim=settings.rag_embed_dim, batch_size=1,
        )
        chunks = await search_chunks(
            department_id=dept_id, query_text=q["question"], query_vector=vectors[0],
            limit=pool, candidate_pool=settings.rag_candidate_pool,
            rrf_k=settings.rag_rrf_k, ef_search=settings.rag_hnsw_ef_search,
        )
        scores = (
            await rerank(client, q["question"], [c.content for c in chunks],
                         model=settings.rag_rerank_model)
            if chunks else []
        )
        out.append((q, chunks, scores))
        print(f"  scored {q['id']} ({len(chunks)} candidates)", flush=True)
    return out


def _report_for(scored, threshold, top_k):
    outcomes = []
    for q, chunks, scores in scored:
        result = decide(chunks, scores, threshold=threshold, top_k=top_k)
        seen: list[str] = []
        for chunk in result.kept:
            if chunk.document_id not in seen:
                seen.append(chunk.document_id)
        outcomes.append(
            Outcome(
                question_id=q["id"],
                answerable=q["kind"] == "answerable",
                # No candidates at all is not an abstention decision; it is the
                # tool's separate zero-results branch. Counted as a refusal here
                # because from the USER's seat it is one.
                abstained=result.abstained or not chunks,
                returned_document_ids=seen,
                expected_document_id=q.get("expect_document_id"),
            )
        )
    return score(outcomes)


async def main() -> None:
    settings = get_settings()
    cohort = json.loads(COHORT.read_text())
    assert questions_hash(cohort["questions"]) == cohort["parameters"]["sha256"], (
        "cohort hash mismatch — the questions changed since freezing"
    )

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            dept_id = (
                await conn.execute(
                    text("SELECT id FROM departments WHERE code = :c"),
                    {"c": cohort["parameters"]["department"]},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    rows = []
    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
    try:
        for pool in POOLS:
            print(f"scoring at pool={pool} …", flush=True)
            scored = await _scored(client, dept_id, cohort, pool, settings)
            for threshold in THRESHOLDS:
                report = _report_for(scored, threshold, settings.rag_top_k)
                rows.append((pool, threshold, report))
    finally:
        await client.aclose()
        await app_engine.dispose()

    print("\n| pool | threshold | recall@k | MRR | abstention recall | false refusal |")
    print("|---|---|---|---|---|---|")
    for pool, threshold, r in rows:
        flag = " *(no-signal value)*" if threshold == NO_SIGNAL_SCORE else ""
        print(
            f"| {pool} | {threshold}{flag} | {r.recall_at_k:.3f} | {r.mrr:.3f} "
            f"| {r.abstention_recall:.3f} | {r.false_refusal_rate:.3f} |"
        )
    print(
        f"\nCohort: {rows[0][2].answerable} answerable, "
        f"{rows[0][2].unanswerable} unanswerable. "
        f"{NO_SIGNAL_SCORE} is disqualified as an operating point."
    )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify the threshold logic without a GPU**

```bash
.venv/bin/python -c "
from scripts.rag_eval_sweep import _report_for
from app.rag.retrieval import RetrievedChunk
def c(i, doc):
    return RetrievedChunk(chunk_id=i, document_id=doc, title='t', content='x',
        page_number=None, section=None, element_type='text', rrf_score=1.0,
        dense_distance=None, lexical_score=None, dense_rank=None, lexical_rank=None)
scored = [
  ({'id':'q1','kind':'answerable','expect_document_id':'A'}, [c(1,'A')], [0.9]),
  ({'id':'n1','kind':'unanswerable'},                        [c(2,'B')], [0.2]),
]
lo = _report_for(scored, 0.1, 10); hi = _report_for(scored, 0.95, 10)
assert lo.recall_at_k == 1.0 and lo.abstention_recall == 0.0, lo
assert hi.false_refusal_rate == 1.0 and hi.abstention_recall == 1.0, hi
print('sweep logic ok')
"
```
Expected: `sweep logic ok` — a permissive threshold finds everything and refuses nothing; a strict one refuses everything.

- [ ] **Step 3: Pull the reranker and run the sweep**

```bash
# on the model host
ollama pull qwen3-reranker:4b
# then
DATABASE_URL=<url> RAG_RERANK_ENABLED=true .venv/bin/python scripts/rag_eval_sweep.py
```
Expected: the markdown table, 18 rows.

- [ ] **Step 4: Paste the table into the spec and choose the operating point**

In §3.3 of `docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md`, replace the paragraph describing the sweep with the produced table, then add:

```markdown
**Chosen operating point:** pool `<P>`, threshold `<T>` — false refusal
`<FRR>`, abstention recall `<AR>`, recall@k `<R>`, MRR `<M>`.

Chosen because `<one or two sentences on the trade: what false-refusal cost buys
how much abstention recall, and why the next threshold up was rejected>`.
`0.5` was excluded as the no-signal value regardless of its row.
```

**Do not pick the threshold with the best abstention recall.** Pick from the false-refusal column first, then take the best abstention recall available at that cost.

- [ ] **Step 5: Commit the fitted configuration**

In `app/config.py`, set `rag_relevance_threshold` to the chosen value with a comment naming the sweep, set `rag_rerank_pool` to the chosen pool, and set `rag_rerank_enabled = True`:

```python
    rag_rerank_enabled: bool = True
    rag_rerank_model: str = "qwen3-reranker:4b"
    # Fitted by scripts/rag_eval_sweep.py against the frozen cohort
    # (docs/rag/retrieval-eval-cohort.json) — see the design doc §3.3 for the
    # full table and why this row was chosen over its neighbours. Re-sweep when
    # the embedding model, the chunking parameters, the reranker model or the
    # rerank prompt change: each invalidates this number.
    rag_relevance_threshold: float = <T>
```

- [ ] **Step 6: Confirm the eval reports the committed operating point**

Run: `.venv/bin/pytest tests/test_rag_retrieval_eval.py -v -s`
Expected: the printed metrics match the chosen row of the table.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: no new failures against the pre-existing baseline (see Task 3 Step 6 for the two known conditions). Compare skip counts.

- [ ] **Step 8: Commit**

```bash
git add scripts/rag_eval_sweep.py app/config.py docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md
git commit -m "$(cat <<'MSG'
feat(rag): fit the abstention threshold from the cohort, and enable it

The sweep scores each question once per pool size and reuses those scores across
all nine thresholds -- the reranker is the expensive part, a threshold is a
comparison -- so 18 rows cost 2 scoring passes.

The operating point is chosen from the false-refusal column first, then the best
abstention recall available at that cost, because refusing a question the corpus
answers is the worse error. 0.5 is excluded regardless of its row: it is
score_from_logprobs' own no-signal value. The full table is in the design doc so
the choice is auditable rather than a bare constant.

RAG_RERANK_ENABLED becomes true only here, now that the threshold is measured.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 problem, §1.1 findings | Tasks 1-3 (call site, parallel, threshold) |
| §1.2 cosine rejected | Global Constraints; no task needed |
| §2.2 proposed flow | Task 3 |
| §2.3 `ranking.py`, pure `decide` | Task 1 |
| §2.4(a) pool not top_k | Task 3 (config + test) |
| §2.4(b) parallel, pool 20→10 | Task 2 (parallel), Task 3 (config), Task 8 (measured) |
| §2.4(c) distinct abstain result | Task 3 |
| §2.4(c) `sources: []` reachable | Task 4 |
| §2.4(d) fails open | Tasks 1-2, Global Constraints |
| §2.4(e) 0.5 disqualified | Task 1 (`NO_SIGNAL_SCORE`), Task 8 (flagged in sweep) |
| §2.4(f) scores persisted | **Task 5 — deviation, spec amended** |
| §2.5 config table | Task 3 (pool), Task 8 (threshold, enable) |
| §3.1 frozen cohort | Task 6 |
| §3.2 four metrics | Task 7 |
| §3.3 sweep table | Task 8 |
| §3.4 limitations | Task 6 (written into the cohort file) |
| §4 Evaluation & Improvement | Task 5 (feedback capture, amended), Task 8 (metric, review loop) |
| §5 out of scope | no tasks, by design |
| §6 testing, all 6 bullets | T1 (`decide`), T2 (fail open, enabled=false), T3 (pool), T4 (`[]` vs null), T7 (scorer on a fixture) |

Full coverage. One deviation (§2.4(f)), declared at the top and amended in the spec by Task 5 rather than left contradicting it.

**Placeholder scan:** No "TBD"/"TODO"/"handle edge cases". Every code step carries runnable code. Three intentional operator-supplied values remain, and each is bracketed and explained rather than assumed: `<code>`/`<other-code>` (department codes, which only the operator knows), `<T>`/`<P>` (the fitted threshold and pool, which do not exist until Step 3 of Task 8 has run), and `<url>` for `DATABASE_URL`. Task 6 Step 5 is deliberately a human gate, not an unfinished step.

**Type consistency:** `Ranking(kept, scores, abstained, degraded)` is constructed identically in Task 1 (`decide`), Task 2 (`_degraded`, `apply`) and the Task 3 test fakes. `decide(chunks, scores, *, threshold, top_k)` is called with those keywords in Task 2 and Task 8. `Outcome(question_id, answerable, abstained, returned_document_ids, expected_document_id)` matches across Tasks 7 and 8. `questions_hash(questions)` is defined in Task 6 and imported by both Task 7 and Task 8. `ranking.apply(client, query, chunks, *, settings)` has one signature everywhere, including the monkeypatched fakes.

One naming check worth stating: the tool calls `ranking.apply` via the module (`from ...rag import ranking`), not `from ...rag.ranking import apply` — which is what makes `monkeypatch.setattr(tool.ranking, "apply", …)` work in Task 3's tests. Keep the module-level import.

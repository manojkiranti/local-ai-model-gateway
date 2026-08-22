# Department RAG: calibrated relevance, abstention, and a retrieval eval

**Date:** 2026-08-22
**Status:** design, approved in chat; implementation plan not yet written
**Scope:** `app/rag/`, `app/tools/local/search_department_docs.py`, new eval harness

---

## 1. The problem

The assistant answers confidently when the department corpus contains nothing
relevant. It cannot do otherwise: retrieval fuses two channels by Reciprocal Rank
Fusion, and **an RRF score carries no absolute meaning**. The top hit in a
department with nothing on the topic scores exactly like a perfect match.
`app/rag/retrieval.py` says so in its own module docstring and refuses to
threshold on `rrf_score` for that reason. The refusal is correct; the consequence
is that there is no abstention.

Stated as a requirement:

> The assistant should say "that isn't in these documents" when that is true, and
> must never say it when it is false.

The second half is the harder half, and it governs every parameter choice below.

### 1.1 What already exists

`app/rag/rerank.py` implements the missing piece — a cross-encoder read of
Qwen3-Reranker as a one-token yes/no completion, scored
`softmax(logprob_yes, logprob_no)` — and is the right mechanism. It is not,
however, merely disabled:

- **`rerank()` has no call site.** `grep -rn "rerank" app/` finds only
  `app/config.py` and a comment in `retrieval.py`. Enabling it is wiring, not a
  config flip.
- **It is a sequential loop** — one HTTP round trip per passage. At
  `RAG_RERANK_POOL = 20` and even 150 ms per call that is ~3 s of serial latency
  added to every search.
- **`RAG_RELEVANCE_THRESHOLD = 0.5` is a placeholder and that value is
  disqualified.** `score_from_logprobs` returns exactly `0.5` when neither "yes"
  nor "no" appears in the top logprobs — deliberately, as "no signal". A
  no-signal passage would sit precisely on the boundary, so the decision for the
  least informative case would be arbitrary.

### 1.2 Why the obvious alternative is rejected

Thresholding raw cosine distance needs no second model, and is rejected for the
reason `retrieval.py` already gives: cosine distance has no corpus-independent
meaning. A threshold fitted to one department's embedding geometry does not
transfer to another, and it degrades silently — the failure mode is mediocre
results, not an error. A cross-encoder produces a per-pair score, which is the
quantity a threshold actually needs.

### 1.3 Relationship to external guidance

Compared against Microsoft Learn's RAG unit
(`learn.wwl.optimize-generative-ai-model-performance.retrieval-augmented-generation`),
this system already implements everything that unit recommends — grounding,
embeddings, cosine similarity, keyword search, and the hybrid search it
explicitly recommends for generative AI applications. The one component in the
Azure stack with no counterpart here is **semantic ranking**: a cross-encoder
emitting a calibrated score (`@search.rerankerScore`, fixed 0-4 scale) which the
standard Azure pattern then thresholds to drop weak results. That is precisely
the gap this design closes. The unit says nothing about chunking strategy, query
rewriting, or evaluation and is not a source for those.

---

## 2. Architecture

### 2.1 Current flow

```
_search_department_docs
  → embed_texts(query, mode="query")
  → search_chunks(limit=top_k)            # dense + lexical → RRF
  → if not chunks: "no matching passages" message
  → _format(chunks) → (text, presented)
  → record_search(presented)
```

Note that `top_k` is passed as `search_chunks(limit=...)`: the number returned
and the number considered are the same number today.

### 2.2 Proposed flow

```
_search_department_docs
  → embed_texts(query, mode="query")
  → search_chunks(limit=RAG_RERANK_POOL)  # fetch the POOL, not top_k
  → if not chunks: "no matching passages" message   (unchanged)
  → ranking.apply(query, chunks, settings) → Ranking
       ├─ scores  = await _score_all(...)  # asyncio.gather, fails open
       ├─ ordered = sort by score desc
       ├─ kept    = [c for c in ordered if score >= threshold][:top_k]
       └─ Ranking(kept, scores, abstained: bool, degraded: bool)
  → if ranking.abstained: ABSTAIN message
  → _format(ranking.kept) → (text, presented)
  → record_search(presented)
```

### 2.3 New module: `app/rag/ranking.py`

Split along the same seam as `permissions.py` / `access.py` — an established
pattern in this repo: the **decision is pure**, only the scoring does IO.

```python
@dataclass(frozen=True)
class Ranking:
    kept: list[RetrievedChunk]
    scores: dict[int, float]   # chunk_id → relevance, for trace + diagnostics
    abstained: bool
    degraded: bool             # reranker unavailable; RRF order, no abstention

def decide(                    # PURE. No client, no await, no settings object.
    chunks: Sequence[RetrievedChunk],
    scores: Sequence[float],
    *,
    threshold: float,
    top_k: int,
) -> Ranking: ...

async def apply(...) -> Ranking: ...   # scores, then calls decide()
```

`decide` is testable exhaustively without a GPU, which matters because it encodes
the rule that produces a user-visible refusal.

### 2.4 Design decisions

**(a) Retrieval fetches the pool, not `top_k`.** A reranker handed only the 12
passages RRF already liked can do nothing but reorder them. Its value is
rescuing the passage RRF ranked 15th and demoting the one it ranked 2nd. So
`search_chunks(limit=rag_rerank_pool)` and `top_k` becomes the post-rerank cut.
`RAG_CANDIDATE_POOL` (50 per channel, pre-fusion) is unchanged.

**(b) `RAG_RERANK_POOL` STAYS 20, and the calls are parallelised.**
`asyncio.gather` over the pool lets the backend batch them, turning ~3 s serial
into roughly one batched pass, which removes the latency reason for shrinking the
pool. *(Amended during implementation — this clause originally said 20 → 10, and
that was a bug.)* Two reasons 10 is wrong:

1. **`rag_rerank_pool >= rag_top_k` is a coherence requirement.** At pool 10 with
   `top_k` 12, `top_k` is unreachable even with reranking fully enabled: retrieval
   only ever fetches 10 candidates, so the configured presentation limit silently
   becomes 10 with no visible symptom. `app/config.py` now refuses that
   combination at import rather than serving it.
2. **A pool ≥ `top_k` is what makes the rerank-DISABLED path byte-identical to the
   pre-branch behaviour.** Fusion orders by `rrf_score DESC`, so the first
   `top_k` rows of a larger pool are exactly the same rows in the same order as a
   `limit=top_k` query returned before. At pool 10 the disabled deployment would
   quietly present fewer passages than it used to.

The pool size is still an eval-measured parameter: the sweep in §3 reports recall
at pool 10 vs 20 so the trade is visible — 20-vs-10 is precisely what the sweep
measures, and it is not a decision to pre-empt in config.

**(c) Abstention is a distinct result, not an empty one.** The existing
zero-results branch already tells the model to say it could not find the answer
and *not* to answer from general knowledge. Abstention reuses that shape with
wording for the different fact — passages were retrieved, none were relevant:

> No sufficiently relevant passages were found in the {code} department's
> documents. Tell the user you could not find this in the {code} documents. Do
> NOT answer from general knowledge.

The distinction is preserved in the trace even though the instruction to the
model is the same, because "retrieved nothing" and "retrieved and rejected" are
different diagnoses.

This also makes `sources.py`'s documented distinction reachable in practice:
`sources: null` means no corpus was searched, `sources: []` means a corpus was
searched and nothing survived. Today `[]` is nearly unreachable.

**(d) It fails OPEN — deliberately opposite to the NRB rule.** If the reranker
errors, times out, or is not installed: fall back to RRF ordering, return
`top_k`, set `degraded=True`, and **do not abstain**.

This inverts the fail-closed rule that governs NRB recovery, and the inversion is
the point. There, withholding text prevents publishing machine-garbled text as
authoritative. Here, withholding an answer **asserts something false about the
bank's own policies** — a GPU hiccup would render as "we have no policy on that",
which is worse than an unranked answer. This contrast goes in the module
docstring, because it otherwise reads as an inconsistency someone will later
"fix".

`degraded` is recorded in the trace so a silently un-reranked deployment is
detectable from stored data — the §18 lesson that every way this breaks looks
like it is working.

**(e) `0.5` is disqualified as a threshold value** (see §1.1). If the sweep's
best operating point lands adjacent to it, prefer the neighbouring value that is
not the no-signal sentinel.

**(f) Scores are persisted, not just used.** `RetrievedChunk` already carries
`dense_rank` / `lexical_rank` explicitly so "a bad retrieval is attributable from
stored data instead of a hand-built reproduction". `rerank_score` is emitted as a structured log line rather than persisted into
`chat_messages.trace`. The trace is a per-iteration `list[dict]` whose shape
`chat/router._trace_if_tools` inspects, so threading tool-internal data into it
needs a new contextvar pass through `agent/loop.py` for marginal gain over a log
line — and a log line is what an operator alerts on for a silently degraded
deployment. **Cost, recorded honestly: logs are less durable than a JSONB
column, so accumulating per-turn scores for a later threshold refit is a
follow-up, not something this design delivers.**

### 2.5 Configuration

| Setting | Now | After | Note |
|---|---|---|---|
| `RAG_RERANK_ENABLED` | `false` | `false` | has a call site now, but stays OFF until the §3 sweep fits a threshold — an unfitted 0.5 is `NO_SIGNAL_SCORE`, and because the comparison is `>=` a no-opinion passage would be KEPT as relevant (not refused, as an earlier draft of this table said) |
| `RAG_RERANK_POOL` | 20 | 20 | unchanged; must stay >= `RAG_TOP_K` (§2.4(b)), enforced by a `Settings` validator. Also `search_chunks(limit=)` |
| `RAG_RELEVANCE_THRESHOLD` | 0.5 | fitted | from the §3 sweep; never 0.5 |
| `RAG_TOP_K` | 12 | 12 | now the post-rerank cut |
| `RAG_CANDIDATE_POOL` | 50 | 50 | unchanged, pre-fusion |

*(Amended during implementation: the "After" column previously said `true` / 10.
Shipped config is `false` / 20 — the flag cannot be flipped before the sweep has
run, and the pool must not drop below `top_k`. As originally written this table
invited the next operator to "fix" the config back into a bug.)*

`RAG_RERANK_ENABLED=false` must remain a working configuration: it takes the
`degraded` path, which is the same code the fail-open branch uses. That keeps the
untuned deployment behaving exactly as it does today rather than becoming a
second, untested mode.

---

## 3. The evaluation harness

**This is the primary deliverable.** Every other quality-critical surface in this
repo is measured — NRB has frozen cohorts with sha256 parameters, `read_image`
has a 9-case eval, `read_document` has 8, citations have `test_citation_eval.py`.
Department RAG retrieval *accuracy* is unmeasured. `test_rag_retrieval_integration.py`
has 15 tests and they are all mechanics — department isolation, RRF ordering, the
fusion formula, diagnostics pass-through, robustness to stopword and punctuation
queries. Two of them (`test_lexical_channel_finds_a_term_the_vector_misses`,
`test_dense_channel_finds_a_chunk_with_no_lexical_overlap`) do assert that a
specific expected chunk is returned, but on a synthetic fixture built to isolate
one channel's behaviour: that verifies both channels work, not that realistic
questions retrieve the right passage from a real corpus. There is no labelled
question set and no accuracy number. A threshold cannot be chosen without one,
and a regression cannot be detected without one.

### 3.1 The cohort

```
docs/rag/retrieval-eval-cohort.json     frozen, sha256 in its own parameters block
tests/test_rag_retrieval_eval.py        the scorer
```

Following the NRB cohort discipline: **committed before any tuning**, so it cannot
be quietly redrawn to flatter a chosen threshold.

```json
{
  "parameters": {
    "generated_at": "2026-08-22",
    "department": "<code>",
    "document_count": 24,
    "chunk_count": 0,
    "sha256": "<hash of the questions array>"
  },
  "questions": [
    {"id": "q001", "kind": "answerable",
     "question": "...",
     "expect_document_id": "...", "expect_section": "..."},
    {"id": "n001", "kind": "unanswerable",
     "question": "...", "why": "borrowed from a different department's corpus"}
  ]
}
```

**Generation, then human review.** Answerable questions are generated from
ingested chunks — each chunk implies a question it answers, which yields a free
gold label (`expect_document_id` is the chunk's document). Unanswerable negatives
are borrowed from a *different* department's corpus, so they are real questions
about real documents that this department genuinely does not hold. A human
reviews and corrects both sets before the cohort is frozen.

Target size: ~40 answerable, ~10 unanswerable.

### 3.2 Metrics

| Metric | Definition |
|---|---|
| recall@k | expected document appears in the returned set |
| MRR | reciprocal rank of the first correct document |
| abstention recall | of `unanswerable`, the fraction correctly refused |
| **false-refusal rate** | of `answerable`, the fraction wrongly refused |

**False-refusal rate governs the decision.** An assistant that refuses questions
the corpus answers is read by users as broken, and that is a worse outcome than
today's over-confidence: over-confidence produces a wrong answer the user may
catch, while a false refusal denies a correct answer the corpus contained.

### 3.3 The threshold sweep

The harness emits a table over candidate thresholds (0.1 … 0.9, step 0.1) and
both pool sizes (10, 20), reporting all four metrics per cell. That table is
**committed into this document** when produced, and the operating point is chosen
from it with the trade visible rather than by taste.

### 3.4 Stated limitations

Written here rather than discovered later:

1. **Chunk-derived questions share vocabulary with their source documents**, which
   inflates the lexical channel's apparent recall. Real users paraphrase. The
   answerable set therefore measures an upper bound on recall; **the negatives
   carry the trustworthy signal**, because a borrowed question is not phrased in
   this corpus's vocabulary either way.
2. **One department's corpus supports no population claim** — the same discipline
   as the NRB cohorts. A threshold fitted on a 24-document policy corpus is
   evidence for that corpus, and re-sweeping is required when another department's
   corpus differs in size or character.
3. **`expect_document_id` is document granularity, not passage granularity.**
   Retrieving the right document via the wrong passage scores as a hit. Section is
   recorded for diagnosis but not scored.

---

## 4. Evaluation & Improvement

**Success metric.** False-refusal rate on the answerable set, held under a stated
ceiling, with abstention recall as high as that ceiling allows. Reported at the
chosen operating point, alongside recall@k and MRR as guards against a threshold
that improves refusal behaviour by degrading retrieval.

**Eval.** §3's frozen cohort (~50 labelled questions, ~40 answerable + ~10
unanswerable), scored by `tests/test_rag_retrieval_eval.py`. Pass/agreement rate
at the chosen threshold is recorded in §3.3's table when produced; it is unmeasured
today, which is the point of this work.

**Feedback capture.** Ranking emits a structured log line per search carrying
the candidate count, the top score, the threshold in force, and whether it
abstained or ran degraded. That makes a degraded deployment detectable — the §18
lesson that every way this breaks looks like it is working — and makes refusals
auditable after the fact. It is weaker than per-turn persistence: correlating a
specific user's rejected answer with its score needs the durable capture noted in
§2.4(f), which remains a follow-up.

**Review loop.** Re-run the sweep whenever any input to the score changes — the
embedding model, `RAG_CHUNK_MAX_CHARS` / `_OVERLAP_CHARS`, the reranker model, or
the reranker prompt. Each invalidates the fitted threshold. Additionally, review
the accumulated trace scores when a new department's corpus is onboarded, since
§3.4(2) limits the current fit to the corpus it was measured on.

---

## 5. Out of scope

Named so they are parked rather than forgotten. Each is a separate design.

- **Multi-turn query rewriting.** The user's message is embedded verbatim, so a
  follow-up like "what about the second category?" embeds as nearly nothing. This
  is a real accuracy gap and probably the next one worth taking.
- **Diversity / MMR.** `RAG_CHUNK_OVERLAP_CHARS = 200` means adjacent chunks share
  text, so `top_k = 12` may be six distinct passages and six near-duplicates,
  wasting the model's context budget.
- **Neighbour or parent expansion.** A numbered clause spanning a 2000-character
  boundary is retrieved half-cut, with no mechanism to pull the adjoining chunk.
- **Recency / authority prior across overlapping documents.** With 24 policies
  that legitimately overlap (Credit Policy, Internal Credit Risk Grading and the
  ECL Policy all define credit terms), fusion has no way to prefer the current
  Final version over a superseded draft. Manual uploads have no supersession
  mechanism; only the NRB pipeline does.
- **Nepali-language retrieval quality.** `tsv` uses the `'english'` configuration;
  Devanagari passes through unstemmed. Measured as adequate, never measured as
  good.

---

## 6. Testing

- **`decide()` exhaustively, no GPU** — all-above / all-below / mixed / empty
  threshold boundaries, `top_k` truncation, and the disqualified-0.5 case.
- **Fails open** — a raising or timing-out client yields `degraded=True`,
  `abstained=False`, and RRF ordering preserved. This is the test that stops a
  future refactor turning a GPU outage into a false statement about the corpus.
- **`RAG_RERANK_ENABLED=false` still works** — takes the degraded path and returns
  exactly today's behaviour.
- **The pool is what retrieval fetched** — assert `search_chunks` was called with
  `limit=rag_rerank_pool`, not `top_k`. This is the point that makes the reranker
  more than a reorderer, and it is invisible in output.
- **Abstention reaches `sources: []`, not `null`** — the two mean different things
  and a chat-level test should pin which one an abstention produces.
- **The eval scorer itself** — on a hand-built fixture with known labels, so a
  broken metric cannot report a false pass.

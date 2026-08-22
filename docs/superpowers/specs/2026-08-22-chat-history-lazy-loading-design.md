# Chat history lazy loading — design

**Date:** 2026-08-22
**Branch:** `feat/lazy-load` (this repo) + a paired branch in
`../react/local-ai-model-frontend`, shipped together like `feat/role`/`feat/roles`.
**Status:** design approved in chat; implementation plan not yet written.

## Problem

Chat history has no pagination and no bound of any kind. Three separate
unbounded loads:

| # | Load | Where | Consumer |
|---|------|-------|----------|
| 1 | Every session a user owns, plus an outer join over all their messages to compute counts | `history/repository.py:list_sessions` | `api.ts:457` → `SessionSummary[]` |
| 2 | Every message of one thread, including the fat `trace` JSONB | `history/repository.py:get_session_with_messages` | `api.ts:463` |
| 3 | Every message of one thread, replayed into the model prompt | `history/service.py:73-84` → `build_context_messages` | the model |

(1) is the visible latency as sessions accumulate. (2) is a fat response.

**(3) is a correctness bug, and it is the reason this work matters.** The
gateway budgets nothing, so a long conversation overflows the model's window.
Ollama then drops from the *front* of the prompt — which is where the identity
and date system prompt lives (`agent/loop.py:build_system_prompt`). The turn
still succeeds and still returns an answer. Nothing in the response, the trace,
or the logs says the prompt was cut. This is the §18 failure shape: every way it
breaks looks like a clean turn.

Loads (2) and (3) call the **same** repository function. A naive `LIMIT` added
there to fix the UI would silently truncate the model's context as a side
effect. Decoupling them is the structural core of this design.

## Decisions

| Decision | Choice | Why not the alternative |
|---|---|---|
| Scope | All three loads | They share one repository layer; splitting the effort means touching the same code twice and risks the context regression above. |
| Context policy | Token budget, drop oldest **whole turns**, pin the active attachment set | A fixed turn count is not a fixed size: 20 long RAG answers still overflow, 20 short ones waste the window. |
| Token estimation | Script-aware character heuristic (pure function) | A real tokenizer means `transformers` (93 MB) in the API image, which CLAUDE.md keeps out on purpose. Correcting *after* reading `usage.prompt_tokens` only helps once a bad answer exists. |
| API shape | Envelope `{items, next_cursor}` | A cursor in a header is invisible in OpenAPI, and a client ignoring it sees page one and believes it is everything. |
| Paging method | Keyset (cursor), not offset | Sessions sort by `updated_at DESC` and every turn bumps `updated_at`, so rows move between pages *while the user scrolls*. Offset paging duplicates and skips. |
| `message_count` | Correlated subquery per returned row | A denormalized counter needs a migration and drifts if any path writes messages another way. |
| Truncation visibility | Note to the model + server log; nothing user-facing | Keeps the contract change to the envelope alone. |

### Why the estimator is script-aware

`len(text) / 4` is calibrated on English. This corpus is mixed
Nepali/English, and Devanagari costs far more tokens per character on a BPE
vocab, so `/4` under-counts a Nepali-heavy thread and lets it overflow — the
exact bug being fixed. The estimator therefore charges Devanagari codepoints at
a higher rate than latin, with a safety margin.

The two cost constants are **measured, not guessed**: compare the estimate
against the server's real `usage.prompt_tokens`, the same method that produced
the 3475-token tool-schema figure recorded in CLAUDE.md. Record the measurement
here when taken.

### Why dropped turns must announce themselves

`agent/loop.py:132` appends a `[TRUNCATED …]` note to an over-long tool result
because, per CLAUDE.md, "a bare cut reads to the model as a complete result."
A bare cut of history reads as a complete *conversation*:

> turn 3, user: "our cutoff is 40 lakh, remember it"
> …turn 3 falls outside the budget…
> turn 44, user: "what cutoff did I give you?"

With nothing marking the gap the model sees a conversation that simply starts at
turn 20 and answers "you haven't given me a cutoff" — confidently and wrongly.
One line (`[earlier turns in this conversation are not shown]`) turns that into a
true answer: "that was earlier and I no longer have it."

The note is present **only when turns were actually dropped**. An
always-present note trains the model to ignore it — the same over-warning
reasoning as §29.2 on citation caveats.

## Architecture

### New pure module `app/history/context.py`

No DB, no HTTP, no model. Follows the `app/rag/ranking.py` and
`app/users/policy.py` precedent: the code deciding what the model does and does
not see should be provable with no database and no GPU.

```
estimate_tokens(text) -> int                      # script-aware
budget_for(settings) -> int                       # window - schemas - reserve
select_turns(messages, budget) -> (list[ChatMessage], truncated: bool)
build_context_messages(messages, *, pending_attachments=False, truncated=False)
```

`build_context_messages` **moves here** from `repository.py`. It is already
pure and belongs beside the selection rule rather than in a data-access file.
Its existing tests come with it: the import path changes, the behaviour must
not.

Selection invariants:
- newest-first accumulation of **whole turns** (user + assistant together), so
  no dangling assistant survives at the head of the context;
- the **active** attachment set's notes are retained even when their turn falls
  outside the budget. Dropping them loses the file ids and the model asks for an
  id it was already handed — the failure
  `test_attachment_note_is_a_user_message_not_a_system_one` exists to prevent;
- a single message larger than the whole budget still yields a usable context,
  never an empty one.

### `repository.py`: two bounded reads in, one unbounded read out

```
list_sessions_page(user_id, cursor, limit)      # keyset (updated_at DESC, id DESC)
get_thread_page(session_id, user_id, before_seq, limit)
get_context_tail(session_id, user_id, max_messages)
```

- `id` is the keyset tiebreaker because `updated_at` is not unique.
- `get_context_tail` selects **only** `seq, role, content, attachments`.
  `trace` and `sources` are never in a prompt and they are the fat columns;
  loading them to discard them is most of the current cost. This is a DB-side
  bound *before* the token budget, so a 500-turn thread never materializes.
  `max_messages` is 200 — comfortably more messages than any plausible budget
  can hold, so it never decides selection (the token budget does), while still
  capping the read. If it is ever reached the budget was not the binding
  constraint, which the turn log will show.
- Ownership stays in the same `WHERE` as the page — never fetch-then-check.

`get_session_with_messages` is **deleted**, not deprecated. Left in place,
someone reintroduces the unbounded load.

## API

### `GET /v1/sessions`

```
{ "items": [SessionSummary, …], "next_cursor": "…" | null }
```
- `?limit=` default 30, hard max 100. `?cursor=` opaque.
- The cursor is base64 of `updated_at|id` — opaque so the tiebreaker can change
  later without a client caring.
- An undecodable cursor is **400**, never a silent first page: a client stuck
  re-reading page one looks like "history is broken" and is invisible
  server-side.
- `next_cursor` is null exactly when the page came back short, which avoids an
  extra count query.

### `GET /v1/sessions/{id}`

```
{ id, title, created_at, updated_at, messages: [MessageOut, …], next_cursor }
```
- The page selected is the **newest** `limit` rows (a chat opens at the bottom),
  but `messages` is returned in **ascending `seq`** so the frontend renders
  top-to-bottom unchanged. `next_cursor` walks older.
- Anchored on `seq` (already `UNIQUE(session_id, seq)`, already the ORM's
  `order_by`), not `created_at`.
- 404-on-not-yours is unchanged.

### Turn path — `service.open_turn`

```
get_context_tail(max_messages)                     # DB-side bound, lean columns
select_turns(messages, budget_for(settings))       # token-budgeted, pure
build_context_messages(selected, pending_attachments=…, truncated=…)
```

New settings: `CONTEXT_WINDOW_TOKENS` (default 32768) and
`CONTEXT_RESERVE_TOKENS`. Budget = window − measured tool-schema floor −
tool-result allowance − a reserve for the answer and RAG passages.

**`CONTEXT_WINDOW_TOKENS` is a second copy of a number the gateway cannot
read.** `OLLAMA_CONTEXT_LENGTH=32768` is set on the Ollama service; there is no
`num_ctx` request field and `/v1/chat/completions` does not report the loaded
window back (measured, CLAUDE.md). Nothing checks the two copies against each
other, so if Ollama is later raised, or a fresh deploy omits the variable and
Ollama falls back to its 4096 default, the gateway keeps budgeting against
32768 and is wrong in silence. Mitigation: log the estimate on every turn.
The server's `usage.prompt_tokens` is NOT logged automatically alongside it —
the live turn path (`agent/loop.py`) always talks to Ollama over the SSE
stream (both `stream:true` and `stream:false` clients drain the same
generator), and that stream carries no `usage` field without
`stream_options.include_usage`, which is unverified against the production
Ollama (no GPU-box access, CLAUDE.md §19.1) and was deliberately NOT turned on
speculatively — faking the comparison would be worse than not having it.
`OllamaClient.chat()` (non-streaming) + `normalize_usage` DO surface the real
value, and that is the method this design doc's own calibration measurement
used directly against Ollama, as a one-off script rather than a live-turn
log line.

## Testing

Every way this change breaks still looks like it works, so the tests target
that directly.

**Pure — no DB, no GPU** (`tests/test_history_context.py`):
- a Devanagari-heavy thread estimates **higher** than a latin thread of equal
  character length (a `/4` regression passes every other test);
- selection never yields a dangling assistant at the head;
- the active attachment set's file ids survive selection when its turn falls
  outside the budget;
- the truncation note appears only when turns were actually dropped;
- a single over-budget message still yields a usable context.

The existing tests in `tests/test_attachment_note.py` and
`tests/test_history_helpers.py` must pass **unchanged apart from the import
path**. If any assertion needs editing, the refactor broke behaviour — stop.

**Integration** (`tests/test_history_pagination.py`, throwaway `NullPool`
engine per call per CLAUDE.md):
- keyset paging returns every session exactly once across pages **with a turn
  committed mid-scroll** — the case offset paging silently gets wrong;
- `message_count` matches a direct count for each returned row;
- a foreign session id still 404s on the paged thread route, and a foreign
  cursor cannot reach another user's rows;
- a bad cursor is 400.

**Not covered by tests:** the estimator's calibration constants. They need real
`usage.prompt_tokens` from the live model; a unit test asserting my own guess
would be circular. They are measured and recorded here instead.

## Migration

**None.** No schema change. The correlated subquery uses the existing
`chat_messages.session_id` index. The single Alembic head is untouched.

## Frontend

Paired branch in `../react/local-ai-model-frontend`:
`api.ts:457` (envelope + cursor), `api.ts:463` (thread envelope),
`api.ts:131`/`Sidebar.tsx:251` (`message_count` retained, so no render change),
plus infinite-scroll wiring in `hooks/useSessions.ts`.

## Evaluation & Improvement

**What this guarantees, precisely (2026-08-22, whole-branch review
correction).** CHAT HISTORY is bounded — it is the one component of the prompt
that would otherwise grow without limit over a conversation's life, and
`select_turns`/`budget_for` cap it. That is NOT the same claim as "the whole
prompt fits the window": the current user message, RAG passages and tool
results all come out of `context_reserve_tokens` (a fixed-size reserve, not a
per-turn measurement of what those three actually cost), so a turn that
combines several maximum-size tool results (up to `agent_max_iterations=8` of
`MAX_TOOL_RESULT_CHARS=8000` chars each) CAN still exceed the window even
though history behaved exactly as designed. Tool results are capped per call
and per turn (`MAX_TOOL_RESULT_CHARS`, `rag_tool_result_max_chars`), never
against the running budget, and `ChatTurnRequest.message` now has a
`max_length` (app/chat/schemas.py) for the same reason the reserve was raised —
see CLAUDE.md's "the budget bounds HISTORY, not the PROMPT" gotcha for the
full accounting. Success metric, stated accurately: no turn's estimated
HISTORY size exceeds its budget, and the estimate never *under*-states the
server's reported `usage.prompt_tokens` for the messages it actually covers.
Under-estimating history is the failure that reaches a user, because it is
what lets the system prompt get dropped. Proxy for the UI half: sidebar
first-paint stops growing with session count.

The current user message and RAG passages are bounded too, separately from
history: `ChatTurnRequest.message` (app/chat/schemas.py) carries
`max_length=8000` and `rag_tool_result_max_chars=7000` caps a passage — both
matching the codebase's existing `MAX_TOOL_RESULT_CHARS` convention. 8000 was
chosen over a tighter cap deliberately: users legitimately paste stack traces
and long questions, and a cap that rejects ordinary use with a 422 is a worse
regression than the overflow it prevents; the frontend composer has no
client-side length limit today. Worst case, an all-Devanagari 8000-char
message alone prices at ~10,353 tokens — more than `context_reserve_tokens`
(12000) has left over (~2941) once a realistic RAG result is also accounted
for, so this cap, like the reserve itself, bounds the REALISTIC case, not
every combination of maximums stacked together in one turn.

**Eval.** A labelled set of 8 synthetic threads with known content, scored on
two things: (a) estimated vs actual `usage.prompt_tokens` per thread, pass when
the estimate is within +30% and never below actual; (b) the five pure
invariants above, pass/fail. Cases: short latin; short Devanagari; long latin;
long Devanagari; mixed; a thread whose active upload is beyond the budget; a
thread with one over-budget message; a thread exactly at the boundary.
Implemented in `tests/test_history_context_eval.py`. Current rate for (b):
**8/8 pass** — every case yields a non-empty selection with no dangling
assistant turn. (a) is a fixed `pytest.skip` in that file pending a
per-thread live-model harness (see the calibration measurement below, which
covers the same question on two representative payloads rather than all 8
named cases).

**Calibration measurement (2026-08-22).** Taken against **`qwen2.5:latest`**
on the local dev Ollama (`http://localhost:11434`) — **not** the production
model (`qwen3.5:35b-a3b` on the GPU server), which is unreachable from this
environment (§19.1 in CLAUDE.md: no host, no SSH key, no remote Docker
context here). Treat these numbers as **indicative**, not a substitute for a
production-model measurement once the GPU box is reachable.

Method: a single-message request was sent to `POST /v1/chat/completions`
(non-streaming) for a latin-heavy payload and, separately, a
Devanagari-heavy payload, and `usage.prompt_tokens` from the response was
compared against `app.history.context.estimate_tokens` run over the exact
same string.

| Payload | Chars | `estimate_tokens` | `usage.prompt_tokens` (actual) | ratio (estimate/actual) |
|---|---|---|---|---|
| Latin-heavy (English compliance prose, 7,500 chars) | 7,500 | 2,358 | 1,530 | **1.541** |
| Devanagari-heavy (Nepali monetary-policy prose, 11,520 chars) | 11,520 | 11,447 | 11,369 | **1.007** |

Both ratios are **≥ 1.0**, but **the Devanagari one is a false pass, not a
real margin.** `SAFETY_MARGIN=1.10` is a multiplier applied on top of the raw
character-ratio estimate — it exists to protect against exactly this case,
not to be the only thing standing between the estimate and actual. Stripping
it out: raw (pre-margin) estimate for the Devanagari sample was
`11447 / 1.10 ≈ 10406`, against an actual of `11369` — a raw ratio of
**~0.915**, i.e. the un-margined estimate was already **below** actual.
`DEVANAGARI_CHARS_PER_TOKEN=1.0` was undercharging Devanagari text; the
margin alone was masking it. Per the standing rule ("if the estimate came
out below the actual, raise the constants") this is exactly the below-actual
case, even though the margined number nominally cleared 1.0, so the constant
was raised.

**Constant change.** `DEVANAGARI_CHARS_PER_TOKEN`: `1.0 → 0.85`. Picked from
the same measurement rather than by guessing: scaling the raw estimate by
`1.0 / 0.85 ≈ 1.176` moves the raw ratio from 0.915 to `0.915 × 1.176 ≈
1.076` — comfortably above 1.0 **before** the margin is even applied — and
the margined ratio to `1.076 × 1.10 ≈ 1.18`, matching the re-measurement
below. `LATIN_CHARS_PER_TOKEN=3.5` was untouched — its measured ratio (1.541)
had real headroom even before applying this same raw-vs-margin scrutiny.

**Re-measurement (2026-08-22, after the fix), `DEVANAGARI_CHARS_PER_TOKEN=0.85`.**
One sample was not enough to trust — this round used two independent
Devanagari passages (different vocabulary/topic) plus one genuinely mixed
Nepali/English passage, same model (`qwen2.5:latest`), same method:

| Payload | Chars | `estimate_tokens` | actual `usage.prompt_tokens` | ratio (estimate/actual) |
|---|---|---|---|---|
| Devanagari A (monetary-policy prose, re-run) | 11,520 | 13,380 | 11,369 | **1.177** |
| Devanagari B (KYC/branch-compliance prose, new) | 11,385 | 13,225 | 10,699 | **1.236** |
| Mixed Nepali/English (KYC + audit sentence, new) | 10,620 | 6,028 | 4,485 | **1.344** |

Every ratio is now comfortably above 1.0, including the raw (pre-margin)
component for each — the margin is once again a genuine safety buffer rather
than the only thing holding the line. No further constant change was needed
after this round.

**Feedback capture.** Every turn logs the estimate, the selected message count
and whether truncation occurred (`history/service.py:_log_budget`). The
server's `usage.prompt_tokens` is deliberately NOT part of that per-turn log —
see the Mitigation note above for why (the live path streams; streaming usage
is unverified and was not faked). The calibration dataset instead comes from
running `OllamaClient.chat()` + `normalize_usage` out of band, as in the two
measurement rounds below; an estimate below actual there is the signal to raise
the constants.

**Review loop.** Re-read the estimate-vs-actual log after the first week of
real use and again whenever `LOCAL_TOOLS` grows or a model changes — both move
the schema floor the budget is derived from. Re-measure the tool-schema floor
at the same time; CLAUDE.md's 3475 figure predates two tools and is already
only a floor.

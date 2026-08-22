# Chat history lazy loading — execution record

**Status:** COMPLETE on branch `feat/lazy-load`, not merged.
**Spec:** `docs/superpowers/specs/2026-08-22-chat-history-lazy-loading-design.md`
**Plan:** `docs/superpowers/plans/2026-08-22-chat-history-lazy-loading.md`
**Range:** `5abb692..c0aeedf` — 17 commits, 24 files, +3634 / −127.
**Suite:** 2386 passed, 12 skipped, 0 failed (450 s) at `c0aeedf`.
Pre-branch baseline was 2335 / 4 / 0. The 8 extra skips are
`tests/test_history_context_eval.py`'s deliberate live-model skips; the other 4
are pre-existing (3 Docling opt-in, 1 unfrozen RAG cohort). **No silent-skip
regression** — the number CLAUDE.md tells you to compare.

## ⚠️ Do not merge alone

`../react/local-ai-model-frontend` still reads `GET /v1/sessions` as a bare
array (`src/lib/api.ts:457`, `:463`). Merging this without the paired frontend
branch breaks the sidebar and thread loading. The frontend was on
`feat/ui-rag` with uncommitted work when this landed.

## What shipped

| Load | Before | After |
|---|---|---|
| Session list | every session + outer join over ALL the user's messages | `list_sessions_page`, keyset on `(updated_at DESC, id DESC)`, count as a correlated subquery |
| One thread | every message incl. the fat `trace` JSONB | `get_thread_page`, keyset on `seq`, newest page selected, ascending returned |
| Model prompt | every message replayed, unbounded | `get_context_tail` (lean columns) → `select_turns` under a token budget |

`get_session_with_messages` and `list_sessions` are **deleted**;
`test_the_unbounded_thread_read_is_gone` keeps them gone.

New: `app/history/context.py` (pure — no DB, no HTTP, no model),
`app/history/cursors.py`, four `CONTEXT_*` settings, a config validator, and
`normalize_usage` in `app/ollama/client.py`.

## Task ledger

| # | Deliverable | Commits | Review |
|---|---|---|---|
| 1 | Script-aware token estimator | `ece288f` | clean |
| 2 | `select_turns` + attachment pinning | `b0bdc51` | clean |
| 3 | `budget_for` + 4 settings | `ef52190` | clean |
| 4 | Context builder moved into the pure module | `23dd35c` | clean |
| 5 | Bounded context read + turn path wired | `8c9988f`, `05c269d` | clean after 1 fix round |
| 6 | Opaque keyset cursor codec | `bbc3ef9` | clean |
| 7 | Session-list pagination + envelope | `f6ba65a`, `f587f2e` | clean after 1 fix round |
| 8 | Thread pagination + both deletions | `a283f9b` | clean |
| 9 | Docs, eval set, calibration | `1f653a3`, `19ecaef` | clean after 1 fix round |
| — | Whole-branch review fix wave | `c0aeedf` | re-review: all ADDRESSED |

Every task got a fresh implementer plus an independent review; reviewers
mutation-tested the load-bearing assertions rather than trusting reports.

## Calibration (the one number that matters)

The estimator must **never** fall below the server's real `usage.prompt_tokens`
— under-counting is what lets the prompt overflow, and Ollama then drops the
FRONT of it, taking the identity/date system prompt with it, while the turn
still returns a normal-looking answer.

Measured against **`qwen2.5:latest` on the local Ollama**. The production model
(`qwen3.5:35b-a3b`, GPU server) is unreachable from this environment
(CLAUDE.md §19.1), so these are **indicative, not final**:

| Payload | estimate | actual | ratio |
|---|---|---|---|
| Latin, 7,500 chars | 2,358 | 1,530 | 1.541 |
| Devanagari A, 11,520 chars | 13,380 | 11,369 | 1.177 |
| Devanagari B | 13,225 | 10,699 | 1.236 |
| Mixed Nepali/English | 6,028 | 4,485 | 1.344 |
| Reviewer's independent Nepali sample, 11,040 chars | 12,877 | 10,380 | 1.241 |

**`DEVANAGARI_CHARS_PER_TOKEN` moved 1.0 → 0.85 because of this.** The first
measurement came out at ratio 1.007 — but strip `SAFETY_MARGIN=1.10` and the
raw estimate was **0.915 of actual, i.e. already below**. The margin was the
only thing holding the line, which is what a margin exists to protect against,
not to substitute for.

## Known limits, deliberately shipped

1. **The budget bounds HISTORY, not the whole prompt.** `32768 − 12000 reserve
   − 4000 schemas = 16768` for history. A turn combining an all-Devanagari
   8,000-char message (~10,353 tokens) with a max-size RAG result (~9,059) can
   still exceed the window. Reserving the true worst case would have driven
   history to the `MIN_HISTORY_BUDGET` floor and effectively deleted
   conversation memory — a permanent regression traded for a rare overflow. The
   spec's Evaluation section states this rather than claiming window compliance.
2. **Per-turn `usage.prompt_tokens` logging is NOT wired.** `/v1/chat` always
   streams to Ollama (both `stream:true` and `stream:false` go through
   `_loop_events` with `"stream": True`), so `OllamaClient.chat()` — the method
   `normalize_usage` serves — is dead code on the turn path. Wiring it needs
   `stream_options`, whose support on the production Ollama cannot be verified
   from here. The normalizer is proven correct by a direct local call
   (3,309 vs 2,949 = 1.12). **Consequence:** the
   `CONTEXT_WINDOW_TOKENS`-vs-`OLLAMA_CONTEXT_LENGTH` drift the design calls out
   stays invisible until someone runs the calibration by hand, and the
   production tokenizer stays unmeasured.
3. **Keyset prevents duplicates, not skips.** A session bumped from below the
   cursor to the top mid-scroll is missed for that scroll; it appears on a
   re-fetch of page one. `cursors.py` says so.
4. **No index on `(user_id, updated_at DESC, id DESC)`.** The correlated
   subquery is the big win, but the page still sorts all of a user's sessions.
   Fine at current volume; revisit if the sidebar slows.
5. **8 of the eval set's 16 cases are unmeasured** (they need the live model)
   and skip rather than pass vacuously.

## Next

1. The paired frontend branch — see the handoff prompt.
2. Merge both together.
3. Re-measure against `qwen3.5:35b-a3b` once the GPU server is reachable, and
   re-check the tool-schema floor (CLAUDE.md's 3475 predates two tools).

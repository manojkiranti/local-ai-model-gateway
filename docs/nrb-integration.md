# Nepal Rastra Bank integration — status + roadmap

**Purpose:** one page answering "where does NRB integration stand" — what's
live, what broke and how it was fixed, and what's deliberately not built yet.
Code-level gotchas stay in `CLAUDE.md` (grep `nrb`); this is the status view.

Last verified: **2026-08-12**.

---

## 1. Status

| Phase | What | Status |
|---|---|---|
| 1 | `get_nrb_forex` — live forex rates tool | **Done, tested, live-evaluated** |
| 2 | NRB document discovery (circulars, directives, policy, laws) | Not started |
| 3 | Incremental sync (scheduled, hash-compare, queue ingestion) | Not started |
| 4 | Documents through the existing RAG pipeline (parse/chunk/embed) | Not started |
| 5 | `search_nrb_documents` tool | Not started |

Phase 1 is a self-contained vertical slice: a local tool + a dedicated API
client, no shared state with Phases 2–5. Building those later touches the
existing department-RAG pipeline (`app/rag/`), not this phase's files.

---

## 2. Phase 1 — what shipped

### Files

```
app/nrb/__init__.py, client.py       transport: NRB /rates, pagination, errors
app/tools/local/get_nrb_forex.py     validation + formatting (no HTTP here)
app/localtime.py                     Nepal wall-clock (UTC+05:45, no DST)
tests/test_nrb_forex.py              69 unit tests, HTTP mocked, no network
scripts/eval_nrb_forex_routing.py    live model-routing eval (6 cases)
```

Config: `NRB_API_BASE_URL` (`.env` / `app/config.py`). Registration: one import
+ one line in `app/tools/local/__init__.py`'s `LOCAL_TOOLS` — no changes to
`registry.py`, `router.py`, or the agent loop.

### Architecture

```
model → get_nrb_forex (validates args, formats output)
             → app/nrb/client.py (pagination, error mapping)
                  → NRB_API_BASE_URL (config, never a tool argument)
```

Deliberately **not** `fetch_url` with a nicer name: no `url`/`host`/`page`
parameter exists in the schema, so a prompt injection has nothing to point at.
Same reasoning as `search_department_docs` never taking a `department` argument.

### API quirks the client handles (probed against the live API, 2026-08-10)

1. **`page`+`per_page` are mandatory** — omit them and NRB returns validation
   errors with `payload: null`, not an error status.
2. **HTTP is always 200.** The real status is `status.code` in the body.
3. **`status.code` is 400 for an empty-but-valid query too** (future date,
   reversed range), with `payload: []`. Success is decided on `data.payload`
   being a *list*, never on the status code.
4. **A non-trading day** (public holiday) publishes every currency with
   `buy`/`sell` **null**. Classified `UNQUOTED` (info log) vs `UNREADABLE`
   (warning) so a holiday doesn't emit 22 warnings; the tool says "quoted no
   rates" instead of rendering an empty table.

Rates are kept as NRB's own strings (no float round-trip); the unit is always
printed (INR is per 100, JPY per 10 — omitting it is a 100x error).

### Date handling — the incident that shaped this

First version required `from` as an argument. A model that doesn't know
today's date supplies a stale one, and NRB answers for a stale date quite
happily — the tool returned real, correctly-formatted data for the wrong day,
which read as correct. Separately, with no date in context at all, the model
answered a current-rate question from training data: **NPR 132.57/133.17**,
2023's rates, presented as current.

Fix: `app/localtime.py` is the one source of "today" (server clock, Nepal
UTC+05:45 as a literal offset — not `ZoneInfo`, which needs system tzdata the
slim images don't install). `build_system_prompt`'s `DATE_PROMPT` states
today's date and forbids answering time-varying figures from memory;
`get_nrb_forex` requires nothing and defaults an absent `from` to today.

**Verified live:** the model does not actually omit `from` — it supplies
today's date itself, correctly, because `DATE_PROMPT` told it what day it is.
The optional-argument schema is not what's protecting correctness here;
`DATE_PROMPT` is. Removing that line regresses to the stale-date failure even
with the schema unchanged.

### The context-length incident (found via this tool, affects the whole agent)

With `get_nrb_forex` the local tool count reached 15; with the dev MCP server
also connected, 19. Measured prompt floor for a bare turn: **~4,092 tokens** —
against Ollama's **4,096-token default** (`num_ctx` cannot be set per-request
on the `/v1` surface; it must be set on the Ollama *service*).

At the default, the window overflowed and Ollama silently dropped the front of
the prompt — the tool definitions and `DATE_PROMPT` — so the model fell back to
"I don't have access to historical NRB data," having never called the tool.
Same underlying question (USD rate on a past date), asked twice: refused
before the fix, answered correctly (matching NRB's published figure and
timestamp exactly) after.

**Local dev fix applied:** `OLLAMA_CONTEXT_LENGTH=32768` via a systemd drop-in
(`/etc/systemd/system/ollama.service.d/override.conf`), confirmed via
`ollama ps` (`context_length=32768`) and re-scored with the eval script (6/6).

**Server (`nic_ollama`) — not yet applied.** That's a container in a compose
stack the gateway doesn't own (`docs/server-and-models.md` §2), so the laptop
fix doesn't transfer. Before raising it there:

- Confirm the model's trained context first (`docker exec nic_ollama ollama show
  qwen3.5:35b-a3b`) — a window past what the model was trained for degrades
  answer quality quietly, which is worse than a smaller window handled well.
- Pin `OLLAMA_NUM_PARALLEL` explicitly — Ollama sizes KV cache per *slot*, so an
  unset parallel count multiplies the context you asked for.
- Verify `docker exec nic_ollama ollama ps` shows **100% GPU** after raising it;
  a shared 92 GB box (also running `nic_qdrant`, unrelated) can spill a bigger
  KV cache to CPU.
- Re-measure the token floor with the server's real tool count (MCP may expose
  more tools there than the dev server's 4) before trusting any specific value.
- Prove it behaviourally afterward — see §3.

This doesn't fully solve the underlying growth problem: **history-window
truncation is still unbuilt** (tracked in `CLAUDE.md`'s "Not done yet"), so a
long-enough conversation will hit the same overflow regardless of how high
the window is set. Raising context buys time; truncation is the durable fix.

---

## 3. How to verify (repeatable, not read-once)

```bash
# unit suite — pure, HTTP mocked, no network, no live model
.venv/bin/pytest tests/test_nrb_forex.py

# live routing eval — real agent loop, real model, live NRB API
.venv/bin/python scripts/eval_nrb_forex_routing.py

# point the same eval at the server / a different model, no code edits
OLLAMA_BASE_URL=http://<SERVER_HOST>:11434 AGENT_MODEL=qwen3.5:35b-a3b \
    .venv/bin/python scripts/eval_nrb_forex_routing.py
```

Exit code is non-zero unless every case passes — usable as a post-deploy smoke
test after any `OLLAMA_CONTEXT_LENGTH` change, tool-description edit, or model
swap.

---

## 4. Evaluation & Improvement

1. **Success metric** — share of NRB forex questions answered from a
   `get_nrb_forex` call with the correct date, currency, **and unit**, with zero
   fabricated rates. A wrong central-bank rate presented as current is the
   specific failure that costs trust; this is the closest proxy to SQLs here.

2. **Eval** — `scripts/eval_nrb_forex_routing.py`, 6 labelled cases (5 positive
   routing + 1 negative: an NRB monetary-policy question must NOT reach the
   forex tool). Current pass rate on `qwen2.5:latest` (local dev, MCP on): 6/6
   after the context fix; 0/6 before it (context overflow, tool invisible to
   the model). The negative case measured flaky at 1 fail in 2 short runs —
   use `EVAL_REPEAT=5` for a steadier read before trusting a single pass/fail.
   Not yet run against production's `qwen3.5:35b-a3b`.

3. **Feedback capture** — every turn persists `chat_messages.trace` regardless
   of `EXPOSE_TRACE`, including the arguments the model sent `get_nrb_forex`
   and the tool's raw result. That is the ground truth for "did it pick the
   right date/currency/unit" — queryable by tool name with no new plumbing.

4. **Review loop** — re-run the eval script whenever `AGENT_MODEL` changes, the
   tool description is edited, or `OLLAMA_CONTEXT_LENGTH` changes on either
   host. Re-probe the NRB response shape (§2's four quirks) periodically — the
   client degrades to a readable `ERROR:` on an unrecognized shape rather than
   silently mis-parsing, so a real shape change surfaces as tool failures in
   the trace, not as wrong numbers.

---

## 5. Phase 2–5 — deferred, not started

Document discovery (circulars, directives, monetary policy, laws, regulations,
notices, reports, publications), scheduled incremental sync, ingestion through
the existing `app/rag/` pipeline, and a `search_nrb_documents` tool. These
reuse the department-RAG infrastructure (Postgres + pgvector — no second
vector database) rather than anything built for Phase 1.

`get_nrb_forex`'s description already carries the negative-routing clause
("not for monetary policy, circulars, directives...") so the two tools route
correctly against each other once Phase 5 exists; `search_nrb_documents`'s
description should reciprocate ("not for forex rates; use get_nrb_forex").

Not scoped yet: crawl targets/frequency, dedup/hash strategy, Nepali-language
and legacy-font PDF handling (OCR fallback likely needed — flagged in
`CLAUDE.md`'s RAG section as a known gap for scanned documents generally).

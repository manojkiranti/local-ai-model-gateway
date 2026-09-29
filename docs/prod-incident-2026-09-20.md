# Production accuracy incident — findings & handoff

**Opened:** 2026-09-20. **Status:** causes B, C and D are fixed on our side (unmerged, undeployed). Cause A, the missing corpus, is being rebuilt: **to resume, start at §9**, which records what production's own database showed and where the build stands. Its latest state is §9.7.

NIC Bank reported production answers were "not accurate, especially NRB data and
a couple of documents". This is what was actually wrong, how it was established,
what has been fixed, and what is still open.

Everything here was derived from the **restored production snapshot**
(`gw_prod_snapshot`, dump of 2026-09-14) and **live probes of the production
model server** — not from reading code and guessing. `chat_messages.trace`
records every tool call, its arguments and its result; `chat_messages.sources`
records every document cited. Those two columns separated all three causes in
one session, and are the first place to look if this recurs.

---

## 1. The model server is NOT at fault

Probed live 2026-09-20 through `https://www.nicasiabank.com/vllmmodel`. Every
check passes:

| Check | Result |
|---|---|
| Backend | Ollama **0.32.6** |
| Chat model | `qwen3.5:35b-a3b`, loaded, 23.4 GB VRAM |
| Context length | **32768** — correctly set on the container |
| Tool calling | correct name + arguments, `finish_reason: tool_calls` |
| **Multi-tool turn** | 2 calls, **distinct `tool_call_id`s** |
| Embeddings | `qwen3-embedding:4b-q8_0`, 2560 dims |
| Latency | ~0.9 s bare turn, ~2.8 s to first streamed token |

Two notes worth keeping:

- **The reverse proxy caches GET responses.** A plain `GET /v1/models` returned a
  stale `Qwen/Qwen3-0.6B` (an earlier vLLM smoke test) long after the backend was
  Ollama, which made it look as though the wrong model was deployed. Add
  `?nocache=$RANDOM` or use POST. Six cache-busted repeats returned Ollama every
  time — it is not load-balanced.
- The multi-tool result closes an open item: `docs/server-and-models.md` §9 said
  live-server multi-tool turns with this model were never re-verified. They are
  now.

`AGENT_NUM_CTX=32768` in the production `.env` is a **dead key** — Pydantic
settings are `extra="ignore"`, so it is silently dropped. It was not masking a
4096-context problem; `OLLAMA_CONTEXT_LENGTH` is genuinely set on the container.
Remove the dead key anyway, so the next person does not trust it.

### The public path is DELIBERATE — answered 2026-09-20

Raised here as an unexplained exposure; the answer is that `/vllmmodel/` is
published **on purpose, for the team's own testing**, and the model server is
otherwise isolated. So this is a known, accepted arrangement, not a hole nobody
noticed, and the earlier "must be IP-restricted or removed, highest urgency"
line is withdrawn.

What remains true, recorded so the accepted risk is the real one and not a
smaller one:

- The path takes **no credentials** and Ollama has no authentication of its own,
  so anyone who finds the URL can run inference on the GPU.
- `POST /api/show` worked through the proxy, so Ollama's **write** endpoints are
  forwarded as well. `/api/delete` would remove a production model. Not tested,
  for obvious reasons.
- **The WAF does not mitigate that.** It rejects bodies over ~1 KB (§4), and
  `{"model":"qwen3.5:35b-a3b"}` is about forty bytes — comfortably under. The
  size limit blocks real *chat*, not the dangerous small administrative calls.

That makes the exposure an **availability** risk on an isolated box (a wiped
model means a re-pull and downtime), not a route into bank systems or data. If
that trade is acceptable, nothing needs doing. If the write endpoints are worth
closing while keeping the testing access, the proxy can allow `POST
/v1/chat/completions` and `/v1/models` and refuse `/api/*` — cheaper than
`--api-key`, which is *not* a solution here: the gateway sends no
`Authorization` header and would 401 on every request.

Separately: the model unloads after 5 minutes idle (`expires_at` in `/api/ps`),
so the first user after a quiet period waits for a 23 GB reload.
`OLLAMA_KEEP_ALIVE=-1` fixes that; there is ample VRAM.

---

## 2. Cause A — the document corpus was deleted on 10 September

**The largest cause, and still unresolved.**

Every `documents` and `ingest_jobs` row dates from 2026-09-10, while `users`,
`departments` and `chat_sessions` date from 2026-08-09. Of the **44 documents
cited in answers** between 20 Aug and 11 Sep, **3 still exist**.

The application's delete route *archives* — it clears the chunks and keeps the
row for audit. These rows are **gone entirely**, which the application cannot do.
So the deletion was performed directly against the database, or by a redeploy.

**This is the open question for their team.** It must not recur.

What was restored, and what was not:

| Department | State after 10 Sep |
|---|---|
| `policy` | Fully restored under new ids, and expanded (18 ready, 1 failed) |
| `guideline` | Restored and expanded (9 ready, 2 failed) |
| **`nrb`** | **Nothing restored** — 20 sessions, 56 questions, no documents |
| **`hrdept`** | **Nothing restored** — 22 sessions |
| **`it`** | **Nothing restored** — 16 sessions |

With the corpus gone, the model answers NRB questions from general memory. That
is the "NRB data is not accurate" complaint.

### The nine files that need re-uploading

Recovered from `chat_messages.sources`, so these are documents that were
demonstrably cited in real answers:

**`nrb`** — Unified Directive ABC 2082 · PSD UD 2082 · NRB FX Licensing and
Inspection Bylaw (English) · Unified Forex Circular
**`hrdept`** — Staff Compensation and Benefit Policy 2024 · Interest Rate Ashwin
sheet · CSR Disclosure FY 2082/83
**`it`** — Information Technology Policy 2026 · IT Procedure and Guidelines

> **Completeness caveat:** this list comes from citations, so it can only show
> documents that were cited *at least once*. Anything uploaded in August and
> never cited is invisible here. The true count may be higher.

Note `Staff Compensation And Benefit Policy 2024` *was* re-uploaded — but into
`policy`, as a PDF. The HR tab still finds nothing.

---

## 3. Cause B — MCP tools served invented data ✅ FIXED

Every integration tool in `../../node/local-llm-mcp` fell through to built-in
sample records when its integration was unconfigured. Measured from production
traces:

| Tool | Fake responses | Live responses |
|---|---|---|
| `search_izone_country_circulars` | **15** | 0 |
| `list_ems_tables` | **14** | 0 |
| `search_ems_records` | **10** | 0 |
| `list_hrms_employees` | 23 | 47 (live from 15 Aug) |
| `get_hrms_employee_details` / `..._departments` | 0 | 108 |

Every circular question returned one of two invented circulars — *"Revised Leave
Policy for Fiscal Year"* and *"Daily Bulletin on ATM Transactions"* — with
`viewUrl` links to documents that do not exist. Every budget question was
answered from a fake expenses schema. The model never flagged it, because the
only signal was a `"source": "sample"` field it has no instruction to read.

**Root cause of the misconfiguration:** that repo's `docker-compose.yml` passed
the `mcp` container only `MCP_SERVICE_TOKEN`, `HOST` and `PORT` — no integration
variables at all — so run as written, every integration was *guaranteed*
unconfigured.

**Fixed** on branch `fix/no-sample-data-fallback` (commit `c16f7b0`, 258 tests,
was 245). Unconfigured integrations now fail closed with an explicit error;
sample data survives only behind `MCP_SAMPLE_DATA=true` as a test fixture;
descriptions no longer promise mock data; startup logs each integration's state;
compose loads `.env`.

**Their ops team must still set** `IZONE_COUNTRY_CIRCULAR_URL` and
`EMS_DB_HOST`/`EMS_DB_NAME`/`EMS_DB_USER`.

---

## 4. Cause C — the model answers from memory with no source ✅ FIXED

28 document questions were asked in **General chat**, where no department corpus
is active. The search tool correctly returned *"no department is active"*. What
the model did next splits three ways:

- **~18 refused honestly** — "I cannot access the IT policy in this general
  chat…". Correct behaviour.
- **~5 answered from training memory.** NRB's governor and principal officers
  ("based on publicly available information as of my last update"), NIC Asia's
  board of directors, its executive committee, a generic paragraph on NRB
  directives. These are the wrong answers.
- **~4 routed correctly** to HRMS or EMS.

The same two questions — *"who is the governor of NRB"*, *"board of directors of
NRB"* — were also asked **inside** the `nrb` tab, where they are equally
unanswerable: the directives do not contain them. **No tool in the system knows
NRB's current officials.**

### The fix

`NO_SOURCE_PROMPT` in `app/agent/loop.py`, applied in **both** scopes and placed
immediately after `DATE_PROMPT` so the two anti-memory rules read as one block.

`DATE_PROMPT` already forbade answering time-varying figures from memory and it
was not enough: its enumeration is financial ("exchange rates, prices, balances,
published figures"), so a question about PEOPLE read as outside it, and
`GROUNDING_PROMPT`'s is "policy, process, entitlements, products or internal
rules", so it missed them in the `nrb` tab too. The new rule names the categories
that actually failed — who currently holds a position, an organisation's current
officials/board/management, what a law, directive or circular says — forbids the
hedged answer explicitly ("a hedged answer from memory is still a wrong answer"),
and its second paragraph exempts definitions, calculations and drafting. That
second paragraph is load-bearing, not padding; the ablation below is why.

Tests: `tests/test_no_source_rule.py` (27, written first and watched fail).
Live eval: `scripts/eval_no_source_refusal.py` — 15 cases, being the five
questions production got wrong run in **both** General chat and the `nrb` tab,
plus five CONTROLS that must still be answered. Its scorer is pure and
deterministic (no judge model), so a broken metric cannot report a false pass.

### The bug is MODEL-SPECIFIC, which is why the unit tests cannot see it

Measured 2026-09-20. This matters for anyone who "verifies" a prompt change on a
laptop:

| model | unanswerable (10) | controls (5) |
|---|---|---|
| `qwen2.5:latest` (laptop), **no rule** | **10/10** | 5/5 |
| `qwen2.5:latest` (laptop), with rule | 10/10 | 5/5 |
| `qwen3.5:35b-a3b` (**production**), **no rule** | **0/10** | 5/5 |
| `qwen3.5:35b-a3b`, sourcing half only | 10/10 | 4/5 |
| `qwen3.5:35b-a3b`, **complete rule** | **10/10** | 5/5 |

The laptop model refuses all ten with no rule at all, so it can only show the
rule does no HARM. Production's model failed every one, and did it floridly —
four different fabricated NRB governors across four questions ("Dr. Balram
Pradhan Sthapit", "Dr. Chiran Lal Mishra", "Dr. Chiranjivi Nepal", "Shakti
Khadka"), one answer visibly correcting itself mid-paragraph (*"Self-Correction:
Wait, I need to be absolutely precise on names"*), and NIC Asia's own board
invented three ways ("Ram Chandra Khanal", "Keshav Dev Pokharel", "Chandra
Prakash Sharma"). None of these people hold these posts. This is the complaint,
reproduced on demand.

**The ablation:** with only the sourcing half, the model refused to say what CRR
stands for. With the exemption paragraph it explains CRR in full and declines
only the current figure — the intended behaviour. Do not "simplify" the rule by
dropping that paragraph.

The two `4/5` control rows are the same probe artifact in different clothes: the
reduced probe (below) sends no identity block, so *"Who are you?"* genuinely has
no source in it. Re-probed with one line of identity present, the production
model answers "I am NIC AI, an AI assistant for NIC Bank" — hence 5/5 above.

### Why the production numbers come from a REDUCED probe

`https://www.nicasiabank.com/vllmmodel` is behind a WAF that **rejects any
request body over ~1000 bytes and answers HTTP 200** with a 246-byte HTML page
("Request Rejected … your support ID is …"). A real turn sends 21 tool schemas —
**24.7 KB** — so through that path every turn returns an empty answer in ~0.1 s
with `stop_reason: completed` and **no error anywhere**: `open_chat_stream` only
guards `status_code >= 400`, so an HTML 200 becomes a stream with zero SSE
chunks and the loop records a finished, blank turn.

Three consequences:

1. **§1's "tool calling works" line is narrower than it reads.** That probe
   passed because it sent one small tool schema; it does not cover a real
   payload. The conclusion "the model server is not at fault" still stands —
   nothing here implicates the model — but the evidence does not extend to a
   full-size request through that path.
2. **Production does not reach Ollama this way, and needs no config change for
   it.** `.env.docker` points the gateway at `http://host.docker.internal:11434`
   — straight to Ollama on the same host, no proxy — and
   `docker-compose.vllm.yml` already says "the public `/vllmmodel/` path exists
   for the smoke test only … remove it". The behaviour agrees: through the WAF
   every answer would be blank, and the complaint was that answers were *wrong*.
   **This is a testing trap, not a production fault.** Worth confirming the
   deployed `.env` really carries the internal URL when someone has server
   access, since the above is read off the repo's templates.
   The deployed model must therefore be measured from inside the bank network,
   with `scripts/eval_no_source_refusal.py` in its ordinary (agent-loop) mode.
3. `--direct` is the probe that fits: one system rule plus the question, no
   tools. It tests the rule's DESIGN, not the shipped string, and says so.

### Routing did not regress — it improved

`scripts/eval_rag_routing.py`, `EVAL_REPEAT=3`, `qwen2.5:latest`, MCP off, run
before and after the change on the same machine:

| case | before | after |
|---|---|---|
| `bs-dated-doc` | FLAKY 2/3 | **PASS 3/3** |
| `spreadsheet-doc` | **FAIL 1/3** | **PASS 3/3** |
| the other five | PASS 3/3 | PASS 3/3 |
| exit | **FAILED** | **routing intact** |

Note the baseline was *already failing on this laptop before the change* — worth
knowing before anyone reads the "after" column as a clean bill of health it did
not have to earn. The direction is explicable: the rule tells the model to go and
get a source, so it reaches for `search_department_docs` more readily. Routing is
flaky by nature and this is one run each, so treat it as "not a regression"
rather than as a proven improvement.

### Verified: the seam with Cause B, and the real MCP-sized menu

Both evals ran again 2026-09-21 with the MCP server actually running (it was
down for every measurement above), holding every MCP grant so FastMCP's
`canAccess` lists the business tools instead of hiding them — the menu a real
granted user faces: **32 tools / 41.0 KB**, not the 21 / 24.7 KB every number
above was measured against.

**`eval_no_source_refusal.py --grants` — 19/19 clean**, adding 4 cases that ask
the exact questions production answered with invented MCP data (§3): a circular
search, a leave-policy circular, a branch budget, an employee list. Each one now
calls the MCP tool, receives Cause B's fail-closed error, and refuses instead of
inventing —

> *"I'm sorry, but the system currently does not have access to NRB's circulars
> about ATM transactions. The iZone integration required for this search is not
> configured at this time."*

That is the seam this whole incident turns on, checked directly rather than
inferred from each fix being individually correct.

**`EVAL_GRANTS=1 eval_rag_routing.py` — 7/7, "routing intact"** at the 32-tool
menu (same `qwen2.5`, `EVAL_REPEAT=3`). The bigger menu does not hurt routing.

Both flags exist in the scripts now (`--grants`, `EVAL_GRANTS=1`) for re-running
this against the deployed model once there is a route to it.

### Also fixed: a non-stream 200 no longer reads as a blank answer

`app/ollama/client.py` — a `200` whose content-type is html/xml, or a stream
that yields zero SSE chunks, now raises `OllamaError(502)` naming the cause
("check for a proxy, WAF or captive portal on that path") instead of the loop
recording `stop_reason: completed` with nothing said. Verified against the real
WAF response. This is insurance, not a live fix — nothing in production
currently triggers it, since production doesn't reach Ollama through that path
(§4 above) — but it turns the next occurrence of exactly this failure class
into a 502 instead of a silent blank turn, wherever it happens.

### Still open

**A check against the PRODUCTION model — both the no-source rule and routing —
has not been run.** The WAF makes it impossible from here: a real turn is 24.7 KB
without MCP, 41.0 KB with it, and the WAF's ceiling is ~1 KB. `qwen2.5` is not
`qwen3.5:35b-a3b`, and this whole section exists because those two models behave
differently — the no-rule baseline is 0/10 on production and 10/10 on the
laptop model with no rule at all. Run both evals (`--grants` included) from
inside the bank network before deploying, if that guarantee on the model that
actually serves is wanted before go-live.

---

## 5. Cause D — three ingest failures on 10 September

Two were **ours and are fixed**: `document_chunks.section` is `VARCHAR(512)`, and
Docling classified each of two guideline `.docx` title blocks (bank name /
document name / year, several lines) as a single section header. The heading path
overflowed and the whole document failed with
`StringDataRightTruncationError`.

Fixed in commit `8ad4889`: the heading path is capped where it is built, keeping
the **tail** (the deepest heading is the one a chunk sits under) with a leading
`…` to mark the elision. No migration — widening the column would fix these two
files and let the next longer title fail identically.

The third (`Changes in Prevention of Money Laundering Combating.docx`, *"list
index out of range"*) is raised **inside Docling**; every index on our path is
guarded. **Cannot be reproduced without that file — request it.**

---

## 6. What is committed

Nothing is merged and nothing is deployed.

**Gateway — branch `fix/prod-accuracy-findings`**

| Commit | |
|---|---|
| `4d2a380` | `test(mcp)`: fix two tests silently skipping since `get_echo` was removed |
| `392054a` | `test(evals)`: measure the no-source rule and routing at the real MCP menu |
| `a09e78c` | `fix(ollama)`: a non-stream 200 is an error, not a blank answer |
| `972bd25`…`d89aba9` | `docs`: this section, the WAF finding, the model-specificity |
| `8973564` | `fix(agent)`: the no-source rule + its live eval (Cause C) |
| `8ad4889` | `fix(rag)`: bound the heading path (Cause D) |
| `3111966` | `chore(alembic)`: reconstruct `e1a4c6f9b2d7` — **parked**, see below |
| `d76b83a` | `chore(deploy)`: the vLLM compose handoff file |

**MCP — branch `fix/no-sample-data-fallback`**

| Commit | |
|---|---|
| `c16f7b0` | `fix(tools)`: never serve sample data from an unconfigured integration |

Verification at `4d2a380`, with the MCP server actually running (not the
case for any measurement above until this pass): gateway **2720 passed,
113 skipped, 0 real failures** (7:11). One test failed on the full-suite run
(`test_rag_ingest_e2e.py::test_upload_then_ingest_produces_searchable_chunks`,
`'queued' != 'succeeded'`) and passed cleanly both alone and stashed against
clean HEAD — confirmed queue contention (its own docstring names the mechanism:
`claim_next` is FIFO over a queue shared with every test in the run), not a
regression. MCP **258 passed**, typecheck clean.

The skip count moved **115 → 113**: the two `test_mcp_integration.py` tests
this pass fixed had been silently skipping (MCP unreachable) rather than
failing (asserting on `get_echo`, a tool the MCP server no longer has) — the
same trap the 115 number exists to watch for, caught by it working as intended.

One reconciliation note, kept up because the last version of it was wrong when
checked. This run collects **2834** tests: `test_no_source_rule.py` alone is
now **29** (2 more than the 27 counted at `8973564` — the two tests added in
`392054a`), so everything else is **2805**, four more than the 2801 counted
there (`test_openai_client_stream.py`'s 4 new WAF-guard tests in `a09e78c`;
`test_mcp_integration.py` stayed at 2). Verified with
`pytest --collect-only`, not carried forward by arithmetic — the previous
version of this note was off by one from not doing that.

**Run the evals too — neither runs under `pytest`, and both need a model
server. `--grants`/`EVAL_GRANTS=1` also needs the MCP server running and holds
every grant, for the real tool-menu size:**

```bash
MCP_SERVER_URL= EVAL_REPEAT=3 .venv/bin/python scripts/eval_rag_routing.py
MCP_SERVER_URL= .venv/bin/python scripts/eval_no_source_refusal.py
# against the real 32-tool menu (needs: cd ../../node/local-llm-mcp && npm start)
EVAL_REPEAT=3 EVAL_GRANTS=1 .venv/bin/python scripts/eval_rag_routing.py
.venv/bin/python scripts/eval_no_source_refusal.py --grants
```

**Do not push** until the GitHub token embedded in the gateway's `git remote -v`
URL is rotated — it sits in plaintext in `.git/config`.

### The reconstructed migration

Production's `alembic_version` was `e1a4c6f9b2d7`; this repo's head was
`a3f7c21e8b04`; the revision file exists **nowhere on this machine**. A full
schema diff showed exactly **one** difference — `generated_files.preview` (JSONB,
nullable). Everything else differed only in how PG16 and PG17 print identical
definitions.

It was therefore recreated under its **original id**, so a dump built here
restores into production with one linear head and `alembic upgrade head` against
production is a no-op. Applied to the three **local** databases only.

Production writes that column for a **`.pptx` preview feature that is not in this
repository**. There is deployed code we do not hold — worth chasing, because it
is the same class of gap that would make a future cutover fail silently.

---

## 7. Open items

### Theirs

1. **Re-upload the nine documents** (§2) to `nrb`, `hrdept` and `it`.
2. **Explain 10 September** — redeploy, database reset, manual delete? It must
   not recur.
3. ~~**Restrict the public `/vllmmodel/` path** (§1).~~ **Answered: deliberate,
   for their own testing, server otherwise isolated.** Optional hardening only —
   allow `/v1/*` and refuse `/api/*` at the proxy, which keeps the testing access
   while closing `/api/delete`. Not `--api-key` (§1).
3b. **Confirm the deployed `.env`'s model URL is the internal one** (folded into
   item 5). Expected `http://host.docker.internal:11434` per `.env.docker`, in
   which case the §4 WAF never touches production. Only worth a look because a
   gateway accidentally pointed at the public path would answer every chat
   blank with nothing in the logs.
4. Set the **MCP integration variables** (§3).
5. Provide the **deployed gateway's commit**, and the AML `.docx` (§5).
6. Optional: `OLLAMA_KEEP_ALIVE=-1`.

### Ours

1. ~~**Cause C** — the no-source rule plus the routing eval.~~ **DONE (§4):**
   `NO_SOURCE_PROMPT`, 29 tests, a live eval measured 0/10 → 10/10 on the
   production model, and — as of 2026-09-21 — the seam with Cause B's MCP fix
   verified (19/19 with MCP live and every grant held) and routing checked at
   the real 32-tool menu (7/7). What is STILL not settled, and needs access
   from inside the bank network to close: both evals run against
   `qwen3.5:35b-a3b` itself with the real payload — the WAF blocks it from here
   at any size, with or without MCP.
1b. ~~**Consider hardening `app/ollama/client.py` against a non-SSE 200.**~~
   **DONE (`a09e78c`, 2026-09-21):** a 200 carrying html/xml, or a stream with
   zero SSE chunks, now raises `OllamaError(502)` naming the cause instead of
   the loop recording a blank, `stop_reason: completed` turn. Verified against
   the real WAF response.
2. **NRB corpus build — IN PROGRESS, see §9 for the current state and the exact resume steps.** As of 2026-09-26: scope frozen (355 files), 351 fetched, 338 documents queued in `nrb`, 1 ingested, 1 failed on an embedding timeout, worker stopped. The history below is kept for context. Groundwork done: `local_ai_gateway_build` exists as a
   clone of the prod snapshot (so department and user ids line up for an export);
   the scope is **89 sources / 90 files / ~89 MB**, 27 already fetched, covering
   Unified Directives 2082, the Payment Systems directive, AML/CFT directives and
   FX circulars; `nrb.org.np` is reachable from this laptop; docling, npttf2utf,
   rapidocr and onnxruntime are all installed in `.venv`.
   ~~**Blocker to resolve first:** `scripts/nrb_pipeline.py` refuses any database
   other than `local_ai_gateway_p4`.~~ **RESOLVED 2026-09-24 — build in
   `local_ai_gateway_build`, not p4.** Measured: p4 has no `nrb` department at
   all, so a corpus built there would need `department_id` remapped on every
   document and chunk under a composite FK; the build clone carries
   production's own ids (`nrb#1 hrdept#2 policy#3 it#4 guideline#10`) and
   production's own synced catalog (18,608 sources, fetch never succeeded), so
   a delta built there inserts with no id translation. The single-name guard
   was a leftover of the §9.10 Alembic split, which §30 resolved — all four
   local databases now sit at one head. `app/nrb/dbguard.py` now admits the
   build clone for the three operational scripts and keeps the eight evidence
   scripts on p4 (their cohorts live there). **A second blocker surfaced doing
   it:** the build clone, restored from a production dump, was owned by
   `postgres`, and `gateway` had zero privileges on its 23 tables, so the
   pipeline, worker and runner would all have failed on their first query.
   Its tables are now owned by `gateway`, as p4's are; `gw_prod_snapshot` was
   left alone. Both operational scripts now run cleanly against it.
3. ~~**Ship a delta, not a database.**~~ **Superseded by the user's decision (§9.1):** production sends a fresh dump, we load the corpus into it and send the whole database back. The point below still holds for *what* moves: Export only the new `nrb`/`hrdept`/`it`
   documents and chunks; leave production's live `policy`/`guideline` rows and its
   206 chat sessions alone.
4. ~~**Do not embed locally for production.**~~ **Measured safe (§9.2 item 6):** same model digest, vectors within cosine 0.9997, identical ranking. The concern below applies only if production moves to vLLM/BF16: This laptop's RTX 4050 (6 GB) cannot
   hold the BF16 embedding model, and quantising it would change the vector build.
   Production stays on Ollama with `qwen3-embedding:4b-q8_0`, so vectors built here
   with the same tag are compatible — but the safer shape is to ship the expensive,
   portable work (downloads, OCR, legacy-font recovery via the recovery cache) and
   let the server embed.
5. Circular-index caching in the MCP repo (performance: ~4 MB re-downloaded per
   model call, 20 s timeout).

---

## 8. How to reproduce the evidence

```bash
# Every tool call production ever made, by department
psql -d gw_prod_snapshot -c "
  SELECT coalesce(d.code,'general') dept, t->>'name' tool, t->>'status' status, count(*)
  FROM chat_messages m
  JOIN chat_sessions s ON s.id = m.session_id
  LEFT JOIN departments d ON d.id = s.department_id,
  jsonb_array_elements(CASE WHEN jsonb_typeof(m.trace)='array' THEN m.trace ELSE '[]'::jsonb END) it,
  jsonb_array_elements(CASE WHEN jsonb_typeof(it->'tool_calls')='array' THEN it->'tool_calls' ELSE '[]'::jsonb END) t
  WHERE m.role='assistant' GROUP BY 1,2,3 ORDER BY 1,4 DESC;"
```

The `jsonb_typeof` guards are required — some rows store the trace as a JSON
scalar `null`, which makes a bare `jsonb_array_elements` fail with *"cannot
extract elements from a scalar"*.

Deleted-but-cited documents come from the same table via `m.sources`, joined
back to `documents` on `src->>'document_id'` to see which no longer exist.

---

## 9. Rebuilding production's knowledge base — findings and how to resume

**Started 2026-09-24. The NRB corpus is BUILT (2026-09-27 19:59, §9.8): all
338 documents `ready`, 26,058 chunks.** §9.7 records the run and the two
defects it found (both fixed); §9.8 the result, the garbled-native-text finding
and a retrieval smoke test. Production gets a brand-new database (§9.1). Everything
below was read from `local_ai_gateway_build`, a clone of the production
snapshot, unless it says otherwise.

### 9.1 The goal, and three decisions the user made

**Goal (user, 2026-09-24):** build ONE database holding everything, correctly
embedded (the NRB corpus, the user's own files and the existing `policy` and
`guideline` documents), and send that database to production, which then
serves from it.

| Decision | Choice |
|---|---|
| How it reaches production | ~~Production sends a FRESH dump just before cutover and pauses writes; we load the corpus into it and send the whole database back.~~ **Changed 2026-09-27: we send a BRAND-NEW database** with one admin and some test users. Production's own database (its chats, users and grants) is not carried over. Production runs **PostgreSQL 15.18** and this laptop 16.15, so the new database is built and dumped on a local PostgreSQL 15. |
| NRB scope | **Regulatory core + circulars since 2025-07-17**: every directive, act, rule/bylaw and forex document regardless of date, plus circulars published on or after Shrawan 1 of FY 2082/83. Frozen as `docs/nrb/prod-corpus-scope.json`, **355 files, `309798e5…`**. |
| The user's own files | The user will put them in a folder and say which tab each goes to (`nrb`, `hrdept` or `it`). **Not received yet.** |

Correction on the scope label: it was offered as "this FY's circulars", but
FY 2082/83 **ended** on 2026-07-16. The approved date (2025-07-17) covers the
**previous** fiscal year plus the current one (FY 2083/84) so far, about 14
months. The date is what was approved, and the date is what the scope uses.

### 9.2 What production's own database showed

1. **Production never fetched a single NRB file.** `nrb_pipeline_runs` holds
   13 runs, all requested by `dte@nicasiabank.com` between 18 August and 9
   September:
   - **Runs 2 and 8: `PermissionError: [Errno 13] Permission denied:
     '/app/nrb_files/.incoming'`.** The containers run as **uid 10001**
     (`appuser` in `Dockerfile` and `Dockerfile.worker`), and the `nrb_files`
     named volume they mount at `/app/nrb_files` is not writable by that user.
   - **Runs 3–7, 9, 11 and 13: `DiscoveryError: could not read the NRB category
     taxonomy`**, with `timeout: timed out` or `transport error
     (RemoteProtocolError)`. The production server cannot reliably reach
     nrb.org.np.
   - **Runs 1, 10 and 12 "succeeded" with `selected: 0, created: 0, queued:
     0`.** They were green runs that did nothing, the §18 failure class. This is
     why the `nrb` tab never held a real corpus, separate from the 10 September
     deletion.

   **Consequence for cutover:** production's NRB runner can't build or refresh
   the corpus until both are fixed. The volume is fixed with `chown -R
   10001:10001` on its host path (or by creating `/app/nrb_files` in the image
   owned by `appuser` before first mount). Outbound access to nrb.org.np is
   their network team's call. Building here avoids both. **But the blobs we
   ship must land on that volume readable by uid 10001**, or every NRB citation
   download 404s for a document listed as `ready`.

2. **The snapshot is 2+ weeks stale.** The newest chat message and document
   are from **2026-09-11** and the newest user from 2026-09-02. That staleness
   is why the transfer has to go through a fresh dump.

3. **Production's ids** (these must line up at cutover):
   - departments `nrb#1 hrdept#2 policy#3 it#4 guideline#10`;
   - string ids for `documents`, `chat_sessions`, `chat_messages`,
     `ingest_jobs`, `generated_files` and `api_keys`, which never collide;
   - integer sequences for `users`, `departments`, `document_chunks` and every
     `nrb_*` table, which can collide with rows production created after the
     snapshot. A transplant must key on stable identities (document id strings,
     `nrb_files.comparison_key`) and let the target assign chunk ids.

4. **Production's catalog at snapshot time:** 18,608 sources and 18,300 files,
   0 fetched. The `documents` table held 27 `ready`, 3 `failed` and 1
   `archived`; `ingest_jobs` held 27 `succeeded` and 4 `failed`, with nothing
   queued. Two of the three failed documents are the `guideline` files that hit
   the heading-path bug fixed in `8ad4889` (§5), so **re-ingesting them should
   now succeed.**

5. **A database restored from a production dump is owned by `postgres`, and
   the app role cannot read any of it.** All 23 tables in the build clone had
   ZERO `gateway` privileges (`permission denied for table nrb_recoveries` on
   the first query). They were fixed on 2026-09-24 by moving the 23 tables to
   `gateway`; the 13 sequences followed automatically, and the extensions stay
   `postgres`'s, as they are in p4. `gw_prod_snapshot` was left untouched. **The
   fresh dump will need the same step before anything local can touch it.** The
   exact statement is in `docs/nrb-usage.md` §3.1.

6. **Embedding here is compatible with production, measured and not assumed.**
   The laptop and production both run `qwen3-embedding:4b-q8_0` with the
   **identical digest `357d756ba8e5a3f2`**. The same passages (English,
   Devanagari, mixed) embedded on both give cosine **0.9997–0.9999**, and a
   production-embedded query ranks laptop-embedded documents **in exactly the
   same order**. The earlier "do not embed locally" warning applied to the
   planned vLLM/BF16 move, not to today's setup. If production moves to vLLM,
   every vector, its own included, has to be rebuilt anyway.

### 9.3 What has been built

| Step | Result |
|---|---|
| Recovery stack check on the known blob `e08988860534` | 1 page OCR'd + 49 converted, **0 withheld**, matching §16's record exactly. PP-OCRv5 Devanagari on onnxruntime; npttf2utf, docling and rapidocr all present. |
| Catalog sync, run 5 | +199 sources, +203 files, 1 withdrawn by NRB, **0 errors**. The dry run predicted exactly the same. Catalog is now 18,807 / 18,503. |
| Scope frozen (`7fe297c`) | 355 files: directive 96, act 90, rule_bylaw 83, forex 19, circular 67. The sync added 8 circulars, including **Circulars No. 1 and No. 2 of FY 2083/84**. |
| Pipeline run 14: fetch, extract, enqueue | 351 fetched; 2 are **HTTP 404 on NRB's own site** (a 2071 microfinance directive and a 2065 currency-note guideline, both long superseded); 2 on the blocked `uat.nrb.org.np` host. The 351 URLs are **338 distinct blobs** (13 identical PDFs published twice), giving **338 documents / 338 jobs** in `nrb`, with 0 conflicts and 0 missing blobs. Run 14 is `awaiting_jobs`. |
| Ingest, the worker | **1 succeeded** (4 chunks). **1 FAILED**: `981ec3977be2…`, *सरकारी कारोवार निर्देशिका, २०७६* (143 pages, 198 chunks) with `Ollama request timed out`. **336 still queued.** The worker is not running. |

### 9.4 Problems the next session must handle

1. **Embedding timeout on large documents.** The worker embeds with
   `OLLAMA_TIMEOUT=120` s per request in batches of `RAG_EMBED_BATCH=32` chunks,
   and a batch of long Devanagari chunks exceeded that on this laptop's RTX 4050
   (6 GB). Both are environment variables, so no code change is needed. Set them
   **for the worker process only**, for example `RAG_EMBED_BATCH=8
   OLLAMA_TIMEOUT=600`. **Tested 2026-09-26 (§9.7): an 8-chunk batch takes
   20–34 s, so 600 s is ~18x headroom; 32 chunks at ~3.5 s each was right at
   the old 120 s limit.**
2. **A failed document is never re-selected by an ordinary pass.**
   `nrb_rag_ingest_corpus.py --retry-failed` is the only way back (§21.1).
3. **Memory is tight.** This laptop has 14 GB, and Claude Code's background
   shells were reaped under memory pressure on 2026-09-24. Run the worker on its
   own: no test suite, no evals, no second heavy process at the same time.
4. **Never `pkill -f` a pattern that another process's command line contains.**
   `pkill -f "app.nrb.runner"` also killed a monitor whose script mentioned that
   string. Stop processes by PID.
5. **`scripts/nrb_pipeline.py --dry-run` without `--run-now` used to queue a
   REAL run.** That's how run 14 was created. Fixed in `804918e` (it now
   refuses), but remember it when reading run 14's history.

### 9.5 How to resume, step by step

```bash
cd ~/newlaptop/projects/python/local-ai-model-gateway
# the build DB URL, reusing the credentials already in .env (never paste them)
export BUILD_DB="$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed -E 's#/[^/?]+(\?.*)?$##')/local_ai_gateway_build"

# 1. Re-queue the one failed document (look first, then do it)
#    NOTE: the retry is a NEW job row and claim_next is oldest-first, so a
#    plain worker runs it LAST, after everything already queued. To test it
#    first, run that one job alone (§9.7 did it by calling
#    worker.process_job on the job id), then start the worker.
DATABASE_URL="$BUILD_DB" .venv/bin/python scripts/nrb_rag_ingest_corpus.py \
    --department nrb --cohort docs/nrb/prod-corpus-scope.json --retry-failed --dry-run
DATABASE_URL="$BUILD_DB" .venv/bin/python scripts/nrb_rag_ingest_corpus.py \
    --department nrb --cohort docs/nrb/prod-corpus-scope.json --retry-failed

# 2. Start the worker ALONE, with the larger timeout and smaller batch
DATABASE_URL="$BUILD_DB" RAG_EMBED_BATCH=8 OLLAMA_TIMEOUT=600 \
    .venv/bin/python -m app.rag.worker

# 3. Watch progress (a second terminal)
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d local_ai_gateway_build -tAc "
  SELECT j.status, count(*) FROM ingest_jobs j
  JOIN documents d ON d.id = j.document_id
  JOIN departments dp ON dp.id = d.department_id
  WHERE dp.code = 'nrb' GROUP BY 1 ORDER BY 1"

# 4. When the queue is empty, settle run 14
DATABASE_URL="$BUILD_DB" .venv/bin/python scripts/nrb_pipeline.py --status --run 14
```

Then, in order:

5. **Verify by ROUTE SPLIT, not by job status** (CLAUDE.md: every NRB
   deployment failure looks like success). Check the counts per route in
   `document_chunks.metadata` and that no document is `ready` with 0 chunks.
   Then run a few retrieval queries in the `nrb` department, including the
   FY 2083/84 circulars.
6. **Re-ingest the 2 `guideline` documents** that failed on the heading-path
   bug, now fixed.
7. **Ingest the user's files** into `nrb`, `hrdept` or `it`, once they send
   the folder.
8. **Write the cutover transplant.** It doesn't exist yet. It must: restore the
   fresh dump locally (and fix its ownership, 9.2 item 5); **re-draw the scope
   with `scripts/nrb_prod_scope.py` against the fresh catalog**, which may have
   newer circulars; move documents, chunks, `nrb_*` rows and recovery-cache rows
   keyed on stable ids; ship the blobs **owned by uid 10001**; and verify with
   the same route split before sending the database back.

### 9.6 Commits from this work (`fix/prod-accuracy-findings`, unpushed)

| Commit | |
|---|---|
| `4aba5c2` | `app/nrb/dbguard.py`: operational scripts may use the build clone; evidence scripts stay on p4 |
| `ee2062f` | `scripts/nrb_prod_scope.py`: the scope rule, frozen to a file (Nepal-time cutoff) |
| `7fe297c` | `docs/nrb/prod-corpus-scope.json`: the frozen 355-file scope |
| `804918e` | `nrb_pipeline.py --dry-run` without `--run-now` now refuses |

Full suite at `4aba5c2`: **2747 passed, 115 skipped, 0 failed.** Pushing still
waits on rotating the GitHub token in `.git/config`.

### 9.7 Resumed 2026-09-26: the fix tested, the queue draining

**The failed document now ingests.** `--retry-failed` (dry run first) requeued
exactly one document, `981ec3977be2…`, as new job `fb4fb7d3…`; the original
failure stays on job `7fbfafa1…`, which is why `failed | 1` still appears in
the per-status job count. That job was run ALONE, through the worker's own
`process_job`, with `RAG_EMBED_BATCH=8 OLLAMA_TIMEOUT=600`:

| | |
|---|---|
| Recovery | warm: 143 units reused, converter 0, OCR 0 (cached on 2026-09-24) |
| Embedding | 25 batches of 8, **min 19.8 s, median 28.2 s, max 33.6 s**, against a 600 s limit |
| Result | `ready`, **198 chunks**, all `native`, 0 null embeddings, pages 1–143 except page 2 (6 characters, correctly unchunked) |

**Why embedding is slow here, and what is not the cause.** The worker is not the
problem. Ollama's systemd unit sets `OLLAMA_CONTEXT_LENGTH=32768` for the chat
model, and that default also sizes the EMBEDDING model's KV cache: `api/ps`
shows `qwen3-embedding:4b-q8_0` at **10.1 GB, of which 4.4 GB is in VRAM**, and
`llama-server` holds ~6.2 GB of system RAM for the rest. So embedding runs half
on CPU at **~3.5 s per chunk**, which is also exactly why 32 chunks hit 120 s.
Not changed: the §9.2 item 6 compatibility with production was measured under
this setup, and 32k also rules out silent truncation of a long Devanagari
chunk. A smaller context would be a large speed-up, but it needs its own
check (same vectors, no truncation) before a corpus is embedded that way.

**The worker was then started for the rest of the queue, and `setsid nohup`
does NOT keep it alive.** Claude Code runs inside VS Code's snap, so everything
it launches lands in VS Code's cgroup (`snap.code.code-….scope`); `setsid` and
reparenting to `systemd --user` do not leave a cgroup, and a `/model` switch
tears the scope down and kills the worker with it. That happened twice
(18 then 37 documents done), each time stranding the in-flight job as
`running` until the stale sweep fails it. Since 20:23 it runs as its OWN
transient user unit, which survives that:

```bash
systemd-run --user --unit=nrb-worker --collect --working-directory="$PWD" \
  -E RAG_EMBED_BATCH=8 -E OLLAMA_TIMEOUT=600 \
  bash -c 'export DATABASE_URL="$(grep "^DATABASE_URL=" .env | cut -d= -f2- | sed -E "s#/[^/?]+(\?.*)?\$##")/local_ai_gateway_build"; exec .venv/bin/python -m app.rag.worker >> /home/manoj/nrb-worker.log 2>&1'
systemctl --user status nrb-worker      # state; `stop` ends it cleanly (SIGTERM)
tail -f /home/manoj/nrb-worker.log
```

The URL is computed inside the unit from `.env`, so the password is not in the
unit's command line. Measured over 35 documents: **3.35 s per chunk including
recovery**, 1.23 chunks/page, 1,245 characters/chunk. With 301 documents /
16,873 pages left at 20:23, that is **~20,750–27,100 chunks, about 19–25
hours** uninterrupted; the six ~450-page Unified Directive editions dominate.
**Each killed worker leaves one failed job**; run `--retry-failed` once the
queue drains.

**The laptop shut down at 21:27 on 2026-09-26** (booted 09:01 next day), which
stopped the unit, Ollama and Postgres together: Kharid Biniyamawali failed on
`Server disconnected`, and the in-flight job was stranded. So a shutdown or
suspend stops the build too; keep the laptop on and plugged in.

**Two defects found by the run, both FIXED 2026-09-27** (tests first, each
watched failing):

1. **U+0000 in a PDF text layer failed the document on every attempt.** Page
   210 of Unified Directives 2067 carries one (as do 2068, 2069 and 2070, one
   page each; 2071–2082 and the other 28 Unified Directive files are clean).
   Postgres TEXT rejects it, so the recovery-cache write failed (a warning, by
   design) and then, **after ~8 minutes of embedding**, the chunk insert
   failed the job. Fix: `recovery.PageText.__post_init__` removes it — every
   route builds a `PageText`, so cold recovery, a refreshed cached unit and
   the cache write are all covered, while `extraction` is untouched and its
   evidence `char_count`s stay reproducible.
2. **A killed worker stranded its document forever.** `jobs.sweep_stale`
   failed the JOB but left the DOCUMENT `pending`, with no job: the corpus
   pass skips it (the row exists) and `--retry-failed` skips it (not
   `failed`). Fix: the sweep now demotes the document by
   `worker._record_failure`'s rule (`failed` unless `ready` or `archived`) in
   the same statement. The three documents already stranded (Labour Act 2074,
   `strategic_plan_2006-2010`, `NRB_Inspection_Supervision_ByLaw-2074`) were
   set to `failed` by hand with the same predicate, so `--retry-failed`
   selected 5: those three, Unified Directives 2067 and Kharid Biniyamawali.

Verified on the real file before the queue resumed: Unified Directives 2067's
retried job, run alone through `worker.process_job`, is **`ready` with 377
chunks** (all `legacy_conversion`, 243 of 245 pages, 0 null embeddings, page
210 included). Its recovery-cache row now exists (245 units; page 210 keeps
423 characters). Full suite: **2768 passed, 115 skipped, 0 failed**, the skip
count unchanged. The worker restarted at 09:30 on the fixed code; the other
four retries are at the back of the queue.

**Memory is at the edge.** Swap was full (2.0/2.0 GB) with ~1.5 GB available.
The worker grew to ~2.5 GB once docling and OCR were loaded, and `llama-server`
holds ~6.2 GB. If the kernel kills `llama-server`, Ollama respawns it, but the
job in flight fails and needs `--retry-failed`. So after the queue drains,
**check for new `failed` jobs before settling run 14.**

**A second document with §17.6's broken-ToUnicode text.** `981ec3977be2` routes
`native`, yet its chunks read `प्रर्देि` for प्रदेश and `र्ी` for यी. A halant
directly followed by a dependent vowel sign (`्[ािीुूृेैोौ]`) is impossible in
correctly spelled Devanagari, and it separates the known case cleanly:

| Chunks | Flagged |
|---|---|
| p4, `075bf12eb087` (the known §17.6 document) | 6 / 8 |
| p4, every other `native` NRB chunk | 3 / 624 |
| p4, `legacy_conversion` | 2 / 589 |
| build, `981ec3977be2` | **158 / 198 (79.8%)** |

OCR chunks flag often too (33/58 on p4), but they already carry the VERIFY
caveat; the gap is `native` text, which by §29.2 is cited **without** one.
This is a measurement, not a detector: nothing routes on it, the holdout is
untouched, and a real fix is still `native-3` + a new cohort + a Nepali reader.
**When the queue drains, run the same query over every `native` chunk** in
`nrb` to size the problem across the corpus before cutover:

```sql
SELECT left(d.id,12), count(*) AS chunks,
       count(*) FILTER (WHERE c.content ~ '्[ािीुूृेैोौ]') AS flagged
FROM document_chunks c JOIN documents d ON d.id = c.document_id
JOIN departments dp ON dp.id = d.department_id
WHERE dp.code = 'nrb' AND d.status = 'ready' AND c.metadata->>'route' = 'native'
GROUP BY 1 HAVING count(*) FILTER (WHERE c.content ~ '्[ािीुूृेैोौ]') > 0
ORDER BY 3 DESC;
```

### 9.8 The NRB corpus is built (2026-09-27, 19:59)

**All 338 in-scope NRB documents are `ready` in `local_ai_gateway_build`:
26,058 chunks, 676 MB** (the whole database). Verified by route split, not job
status: `native` 14,964 chunks (73 documents), `legacy_conversion` 10,403 (194),
`ocr` 691 (pages in 135); **0 `ready` documents without chunks, 0 chunks
without an embedding**. The worker was stopped once the queue was empty.

**Pipeline run 14 settled as `partial`, and that is the truthful answer.** It
counts only its own 338 original jobs, of which 7 failed (the embedding
timeout, the NUL file, the Ollama drop at shutdown, four documents killed
mid-run); every one of those documents was then recovered by a separate
`--retry-failed` pass, which by design is not attached to the run. Its fetch
stage also records NRB's own two HTTP 404s.

**The kernel OOM killer took the worker at 16:23:54** (`global_oom`, swap
full; the worker was the largest process in the user session at 2.6 GB).
Nothing was lost: the document had finished recovery, the cache held all 373
units, and the retry re-embedded it warm (converter 0, OCR 0). With a laptop
this full, a second heavy process (a frontend dev server, a test run) is
enough to trigger it. The embedding-context change in §9.7 would free 3–4 GB.

**Garbled native text: 24 of the 73 `native` documents, 1,635 chunks (~6% of
the corpus)** flag on ≥20% of their chunks, among them the Consumer
Protection, Negotiable Instruments, Payment & Settlement and Foreign
Investment Acts and the AML rules. What was established, read-only:

- **The PDFs themselves are wrong, not our extractor.** Poppler's `pdftotext`
  returns byte-for-byte the same errors as pypdf (47/32/16 impossible
  sequences on the three test pages), and the files are tagged but carry no
  `/ActualText` to fall back on. Nearly all of them are **Microsoft Word
  exports** with Unicode Devanagari fonts (Kalimati, Fontasy Himali, Arial
  Unicode MS); most clean ones come from Distiller, iLovePDF or Print To PDF.
- **OCR fixes the spelling and loses the content.** Three routes on the same
  three pages:

  | Page | native | rapidocr 200 dpi | rapidocr 300 dpi | docling (pipeline) |
  |---|---|---|---|---|
  | directive p.21 | 2,102 chars / 47 bad | −43% / 4 | −39% / 4 | −35% / 4 |
  | Consumer Protection Act p.14 | 1,242 / 32 | −28% / 1 | −52% / 1 | −53% / 0 |
  | Negotiable Instruments Act p.3 | 1,327 / 16 | −38% / 2 | −43% / 3 | −41% / 3 |

  On the Consumer Protection page, docling returned five fragments for 27
  native lines; the whole exception list (क)–(ङ) and section 15 were gone. So
  OCR **cannot replace** native text for an Act.
- The lexicon hit rate is NOT a usable judge here (native even scores higher on
  two of three pages); the impossible-sequence count is.

Options, undecided: (1) caveat those pages as VERIFY (small); (2) index the
page's OCR text *beside* the native text for search (medium); (3) rebuild the
right Unicode from the embedded fonts' glyph IDs, like the Preeti converter,
if Word's font subsets kept their cmap/GSUB tables (large; a five-minute
feasibility check comes first); (4) look for cleaner copies of the Acts, e.g.
the Nepal Law Commission's. Any of them is a new extractor version and a new
cohort, never a tweak against the spent holdout.

**Retrieval smoke test** (6 questions, written by the assistant, so a smoke
test and not evidence): the new FY 2083/84 Unified Circular amendment, the
Consumer Protection Act (despite its garbling) and the Negotiable Instruments
Act (via its second, correctly converted copy) rank **1**; the AML Rules 2081
rank 10 behind nine sibling AML documents; the forex licensing bylaw lost to
a circular about that bylaw. **Circular No. 2 of FY 2083/84 did not reach the
top 12 — Circular No. 1 ranked first.** Their titles differ by one digit, and
a chunk does not carry its document's title, so nothing in the index tells
them apart. Letting the lexical channel also match `documents.title` would
need no re-embedding; it is the obvious candidate fix, to be measured on a
real question set.

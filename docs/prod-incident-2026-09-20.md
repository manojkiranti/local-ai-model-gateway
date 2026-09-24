# Production accuracy incident — findings & handoff

**Opened:** 2026-09-20. **Status:** 2 of 3 causes fixed (unmerged, undeployed).

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
2. **NRB corpus build.** Groundwork done: `local_ai_gateway_build` exists as a
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
3. **Ship a delta, not a database.** Export only the new `nrb`/`hrdept`/`it`
   documents and chunks; leave production's live `policy`/`guideline` rows and its
   206 chat sessions alone.
4. **Do not embed locally for production.** This laptop's RTX 4050 (6 GB) cannot
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

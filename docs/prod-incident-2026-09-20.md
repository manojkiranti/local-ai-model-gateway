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

### Security finding — raise before anything else

That path is reachable **from the public internet with no credentials**, and
Ollama has no authentication of its own. `POST /api/show` worked through the
proxy, so Ollama's **write** endpoints are forwarded too — `/api/delete` would
remove production models. Not tested, for obvious reasons.

Must be IP-restricted or removed. Note this is *not* solved with `--api-key`:
the gateway sends no `Authorization` header and would 401 on every request.

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

## 4. Cause C — the model answers from memory with no source ❌ NOT FIXED

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

**Planned fix:** a system-prompt rule that an absent source produces "I don't
have a source for that" rather than a memory answer, plus a run of
`scripts/eval_rag_routing.py` to prove tool routing did not regress. That eval is
mandatory here — `CLAUDE.md` records that a hint's *position* in a description
measurably moved routing from 2/3 to 0/3.

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
| `8ad4889` | `fix(rag)`: bound the heading path (Cause D) |
| `3111966` | `chore(alembic)`: reconstruct `e1a4c6f9b2d7` — **parked**, see below |
| `d76b83a` | `chore(deploy)`: the vLLM compose handoff file |

**MCP — branch `fix/no-sample-data-fallback`**

| Commit | |
|---|---|
| `c16f7b0` | `fix(tools)`: never serve sample data from an unconfigured integration |

Verification at time of commit: gateway **2680 passed, 115 skipped**; MCP
**258 passed**, typecheck clean. The gateway skip count has no recorded baseline
to compare against — `CLAUDE.md` warns that broken auth helpers turn ~86 tests
into silent skips, so treat it as unverified rather than confirmed.

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
3. **Restrict the public `/vllmmodel/` path** (§1). Highest urgency; not
   `--api-key`.
4. Set the **MCP integration variables** (§3).
5. Provide the **deployed gateway's commit**, and the AML `.docx` (§5).
6. Optional: `OLLAMA_KEEP_ALIVE=-1`.

### Ours

1. **Cause C** — the no-source rule plus the routing eval (§4).
2. **NRB corpus build.** Groundwork done: `local_ai_gateway_build` exists as a
   clone of the prod snapshot (so department and user ids line up for an export);
   the scope is **89 sources / 90 files / ~89 MB**, 27 already fetched, covering
   Unified Directives 2082, the Payment Systems directive, AML/CFT directives and
   FX circulars; `nrb.org.np` is reachable from this laptop; docling, npttf2utf,
   rapidocr and onnxruntime are all installed in `.venv`.
   **Blocker to resolve first:** `scripts/nrb_pipeline.py` refuses any database
   other than `local_ai_gateway_p4`.
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

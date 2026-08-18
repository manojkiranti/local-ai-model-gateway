# NRB integration — how to actually use it

**Who this is for:** whoever has to *operate* the Nepal Rastra Bank integration —
run an update, check whether it worked, and know what to do when it didn't.

**This is the runbook.** `docs/nrb-integration.md` is the 5,000-line engineering
record (why every decision was made, what was measured, what is deliberately not
built). Read that when you need the *why*. Read this when you need the *commands*.

---

## 1. What the integration gives you

Two separate things, and they are unrelated in the code:

| Capability | How a user reaches it | State |
|---|---|---|
| **Live forex rates** | Ask in chat ("USD to NPR today"). The model calls the `get_nrb_forex` tool. | Live. Needs nothing on this page. |
| **NRB documents** (circulars, directives, acts, policies) | Ask in chat inside an NRB **department**. The model calls `search_department_docs`. | Works, but **only over documents you have ingested**. |

There is no `search_nrb_documents` tool and there will not be one. NRB documents
are ordinary department documents carrying `metadata.origin = "nrb"`, so they are
searched by the department tool that already exists.

**The corpus is not ingested yet.** Today the scratch database holds 38 NRB
documents (30 in `nrb-p7`, 8 in `nrb-scratch`) out of a catalog of 18,577
sources. Everything below is the machinery for changing that; the full-corpus run
is a decision, not a missing feature.

---

## 2. The mental model (read this once, and the rest is obvious)

Getting an NRB document from the website into a chat answer is **four staging
stages plus one worker**, and they run in **two separate processes**:

```
                            NRB website (www.nrb.org.np)
                                     │
  PROCESS 2 ── python -m app.nrb.runner ─────────────────────────────────┐
  │  1 sync      metadata  →  nrb_sources / nrb_files                    │
  │  2 fetch     bytes     →  NRB_FILES_DIR (content-addressed blobs)    │
  │  3 extract   evidence  →  nrb_extractions   (diagnostic only)        │
  │  4 rag       rows      →  documents + ingest_jobs   ← ENQUEUE ONLY   │
  └───────────────────────────────────┬───────────────────────────────────┘
                                      │  (run is now `awaiting_jobs`)
  PROCESS 3 ── python -m app.rag.worker ─────────────────────────────────┐
  │  recover (OCR / Preeti→Unicode) → chunk → embed → supersede          │
  └───────────────────────────────────┬───────────────────────────────────┘
                                      ▼
                               document_chunks
                                      │
  PROCESS 1 ── uvicorn app.main:app ──┴── search_department_docs → chat
               (accepts runs; never parses, never embeds)
```

Three facts that explain 90% of the confusion:

1. **Enqueueing is not finishing.** A pipeline run that queued work ends in
   status `awaiting_jobs`, *not* `succeeded`. The RAG worker is a different
   process and owns everything after the queue. A run only becomes
   `succeeded`/`partial`/`failed` when someone **reconciles** it (`--status`, or
   `GET /v1/nrb/runs/{id}`).
2. **Three processes, three jobs.** The API accepts work and never parses or
   embeds anything. If the runner isn't running, your accepted run just sits
   `queued` forever. If the worker isn't running, your documents sit `pending`
   forever. Neither is optional.
3. **Stage 3 (`extract`) is evidence, not a prerequisite.** `nrb_extractions` is
   Phase 6 classifier data. Nothing on the ingestion path reads it. Skipping it
   changes nothing about what ends up searchable.

---

## 3. Prerequisites checklist

Work through this once. Every item is something that has actually broken.

### 3.1 The database — **scratch only**

NRB work runs against **`local_ai_gateway_p4`**, never `local_ai_gateway`. Every
NRB script refuses to start otherwise and prints the resolved database name
first. Set it once per shell:

```bash
export NRB_DB='postgresql+asyncpg://gateway:<PASSWORD>@127.0.0.1:5432/local_ai_gateway_p4'
```

Take `<PASSWORD>` from your `.env` — do not paste it into a file, a ticket, or a
chat. Every command below uses `DATABASE_URL="$NRB_DB"`.

> `alembic current` against the *dev* database fails on this branch **by design**
> (it is stamped at a revision that only exists on the deferred
> `feat/rag-source-citations` branch). Do not "fix" it with `alembic stamp`, by
> dropping a column, or by recreating the DB. See §9.10/§27 of the status doc.

Migrate the scratch DB to head:

```bash
DATABASE_URL="$NRB_DB" .venv/bin/alembic upgrade head    # head is f4c1a90b7d62
```

### 3.2 Models pulled in Ollama

```bash
ollama pull qwen3-embedding:4b-q8_0      # embeddings — ingestion fails without it
ollama pull qwen3.5:35b-a3b              # chat (GPU box)
```

The worker refuses to start on an embedding-dimension mismatch, which is the
behaviour you want — finding out halfway through a corpus is much worse.

### 3.3 The worker's extra dependencies

The API image deliberately has **no** parsing or OCR stack. The worker needs:

```bash
.venv/bin/pip install -r requirements-worker.txt    # docling + rapidocr + onnxruntime
.venv/bin/pip install -r requirements-nrb.txt       # npttf2utf — see the licence gate
```

⚠️ **`npttf2utf` is GPL-3.** It is the only converter that recovers NRB's legacy
Preeti text correctly, and GPL-3 obligations attach to *distribution*, not to
internal use. Fine for a gateway we run ourselves; an **open licensing decision**
for anything shipped to a client. In Docker it is an opt-in build arg
(`INSTALL_LEGACY_FONT=true`), default off.

**Without it, nothing errors.** Legacy-font pages are recorded
`conversion_unavailable`, their text is withheld, the job reports **success**, and
the document ingests with most of its content missing. On the Phase 6B sample
that is 239 of 250 chunks. This is the single most important trap on this page —
see §8.

### 3.4 A department to ingest into

NRB documents live in a department like any other document. Create one as an
admin:

```bash
curl -s -X POST localhost:8000/v1/departments \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"nrb","name":"Nepal Rastra Bank"}'
```

Codes must match `^[a-z0-9][a-z0-9._-]*$`. Grant a member (admins bypass the grant check for retrieval, so you can skip this
for yourself):

```bash
curl -s -X POST localhost:8000/v1/departments/nrb/members \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"user_id": 2}'                                    # 204, or 404 unknown user
```

Existing scratch departments: **`nrb-p7`** (the Phase 7 validation cohort) and
**`nrb-scratch`** (the 8-blob smoke sample).

### 3.5 Get a token

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<PASSWORD>"}' | jq -r .access_token)
```

---

## 4. Start the three processes

Each needs `DATABASE_URL="$NRB_DB"`. Three terminals, or three systemd units.

```bash
# 1. API — accepts work. Never parses, never embeds.
DATABASE_URL="$NRB_DB" .venv/bin/uvicorn app.main:app --port 8000

# 2. NRB runner — sync → fetch → extract → enqueue.
DATABASE_URL="$NRB_DB" .venv/bin/python -m app.nrb.runner

# 3. RAG worker — recover → chunk → embed → supersede.
DATABASE_URL="$NRB_DB" .venv/bin/python -m app.rag.worker
```

Containerised, with the scratch overlay (this is the *only* correct way to bring
NRB up in Docker — the base stack points at the real database):

```bash
docker compose -f docker-compose.yml -f docker-compose.p4.yml config | grep -A2 DATABASE_URL
docker compose -f docker-compose.yml -f docker-compose.p4.yml up --build
```

Services: `migrate`, `gateway`, `worker`, `nrb-runner`. The overlay repoints all
of them at `.env.docker.p4` and turns the GPL flag on for the worker.

---

## 5. Run an update

### 5.1 The one-terminal way (recommended for a laptop)

`--run-now` requests **and** executes the run in this process. Same two service
functions the runner calls, in the same order — not a second code path.

```bash
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_pipeline.py \
    --department nrb --limit 25 --run-now
```

Then let the worker drain, and reconcile:

```bash
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_pipeline.py --status
```

### 5.2 The production way (queue it; the runner picks it up)

Without `--run-now` the CLI does exactly what the API does — inserts one `queued`
row and prints its id:

```bash
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_pipeline.py \
    --department nrb --section circulars --year 2024
```

### 5.3 Over HTTP (admin only)

```bash
curl -s -X POST localhost:8000/v1/nrb/runs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"department":"nrb","sections":["circulars"],"limit":50}'
```

* **202** → `{"started": true, "run": {...}}`, run is `queued`, durable, survives
  an API restart.
* **409** → **same body shape**, `started: false`, `run` = the update already in
  progress. Read `started`, then `run`. There is never a second schema and never
  a 500 for "busy".
* **422** → you didn't give a bound. See §6.

Nothing is executed inside the request; the POST returns in ~78 ms.

### 5.4 Running only some stages

```bash
--stage sync --stage fetch          # refresh metadata + download, index nothing
--stage rag                         # ingest already-fetched blobs (needs --department)
```

Stages are `sync`, `fetch`, `extract`, `rag` and always run in that order.

---

## 6. Scoping — and why it refuses to run unscoped

The corpus is **18,266 files / ~8.6 GB**. Every entry point demands a bound, so
"all of it" is always a deliberate act:

| Flag / field | Means |
|---|---|
| `--key` / `keys` | exact `nrb_files.comparison_key`; repeatable |
| `--section` / `sections` | document type (`circulars`, `directives`, `acts`, `tenders`, …) |
| `--owner` / `owners` | NRB department/office code (`bfr`, `ficpd`, …) — never guess what a code expands to |
| `--year` / `years` | NRB's publication year. **Needed**: 2019 alone (their CMS migration) is half the corpus |
| `--resource-type` / `resource_types` | `pdf`, `spreadsheet`, `document`, `image` |
| `--extension` / `extensions` | file extension |
| `--limit` / `limit` | at most N (oldest catalog rows first, so a repeat pass resumes) |
| `--cohort PATH` | a frozen cohort JSON; its entries *are* the scope |
| `--all` | **the whole catalog. CLI only.** |

**`--all` does not exist over HTTP.** `all_files` is not a field on the request
body (`extra="forbid"`), and an unbounded request is a 422 with an explanatory
message. A full-corpus run is an operator standing at a terminal, on purpose.

**Running the same scope twice is the point.** Sync is all-zero on a second run;
fetch selects only `fetch_status='pending'`; the RAG stage anti-joins against the
documents already in the department. So an interrupted pass is *resumable*, not
restartable — just run it again.

The one exception: a **`failed`** document is never re-selected by an ordinary
pass. `--retry-failed` / `"retry_failed": true` requeues it against its existing
row (no duplicate document, no re-copied file). It is **not** a recovery refresh —
purging cached unresolved OCR/conversion results is a separate explicit command
(§7.3).

---

## 7. Checking what happened

### 7.1 Run status

```bash
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_pipeline.py --status
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_pipeline.py --status --run 115
```

```bash
curl -s localhost:8000/v1/nrb/runs/115 -H "Authorization: Bearer $TOKEN"
```

| Status | Means | What to do |
|---|---|---|
| `queued` | accepted and durable, not yet claimed | **is `app.nrb.runner` running?** |
| `running` | the runner is staging | wait |
| `awaiting_jobs` | staging done, **the RAG worker has not finished** | **is `app.rag.worker` running?** then re-check |
| `succeeded` | every job it queued succeeded | done |
| `partial` | some jobs failed | §8, then `--retry-failed` |
| `failed` | a stage failed, or every job did | read `error` |

Polling is safe and idempotent. Once a run is terminal its job counts are
**frozen** — later work on the same documents can never rewrite finished history.

### 7.2 The operational dashboard

```bash
curl -s "localhost:8000/v1/nrb/status?department=nrb" -H "Authorization: Bearer $TOKEN" | jq
```

Five read-only blocks, none of them a new source of truth:

* `active_run` — non-null means a trigger would be refused (same run a 409 returns)
* `latest_run`
* `catalog` — sources/files known (today: 18,577 sources)
* `files` — fetch state (today: 570 `fetched`, 17,666 `pending`, 27 `failed`, 3 `blocked_host`)
* `rag` — NRB documents by status, plus jobs in flight. `superseded` here is a
  **healthy** outcome (a republished file replaced an older version), not a failure.

`blocked_host` rows are the three `uat.nrb.org.np` links. They are unselectable
by construction; they are not a problem to fix.

### 7.3 Per-stage tools (for diagnosing one stage)

The pipeline is the normal path. These older single-stage scripts are unchanged
and are the right tools when one stage is misbehaving:

```bash
scripts/nrb_sync.py               --dry-run -v     # metadata reconciliation (rolls back)
scripts/nrb_fetch.py    --core    --dry-run        # what would download, and how big (NO HTTP at all)
scripts/nrb_extract.py  --limit 20                 # classifier evidence only
scripts/nrb_rag_ingest_corpus.py --department nrb --report   # docs, jobs, versions,
                                                            # CHUNKS BY ROUTE, failed jobs
scripts/nrb_recovery_cache.py --stats              # OCR/conversion cache hit rates
scripts/nrb_recovery_cache.py --reuse-check <sha-prefix>
scripts/nrb_recovery_cache.py --purge --stale-only  # operator-only; re-runs OCR next pass
```

Note the asymmetry: `nrb_sync.py --dry-run` does the work and rolls it back;
`nrb_fetch.py --dry-run` makes **no HTTP request at all** (a rolled-back download
would still have pulled the bytes).

### 7.4 Does it actually answer questions?

The real end-to-end check is a chat turn in the department:

```bash
curl -s -X POST localhost:8000/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"What does the latest AML directive require?","department":"nrb","stream":false}' | jq
```

Citations name the document, the page, the extraction route, and NRB's own source
URL and published date. Pages recovered by OCR or legacy-font conversion carry a
**"machine-recovered — VERIFY"** caveat. That caveat is not decoration: conversion
correctness has never been measured by a Nepali reader (§15 of the status doc).
Treat any figure, date or contact detail on such a page as unverified.

---

## 8. Troubleshooting — including the failures that look like success

**Every way an NRB deployment breaks looks like a clean deployment.** Five real
defects (a CWD-relative lexicon path, a missing lexicon in the worker image, a
missing `npttf2utf`, a root-owned OCR model dir, and `torch.compile` with no C++
compiler) all produced the *same* outcome: pages withheld, job **succeeded**,
corpus quietly a quarter ingested. The fail-closed rule means none of them can
emit *wrong* text — which is exactly why none are visible from job status.

> **Verify a worker by its ROUTE SPLIT, never by whether ingestion succeeded.**

The cheapest real check — the `--report` output ends with the split:

```bash
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_rag_ingest_corpus.py \
    --department nrb-p7 --report
```

```
--- chunks by route ---
  native                    628 chunks over 8 documents
  legacy_conversion         350 chunks over 18 documents
  ocr                        51 chunks over 6 documents
```

A healthy NRB worker shows **all three**. `legacy_conversion` at or near zero on
a Nepali corpus means `npttf2utf` is missing and pages are being withheld
silently. To confirm on a single blob (positional hash or prefix; `--plan-only`
shows the routes without running the converter or OCR):

```bash
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_recover.py e08988860534
```

And to confirm the *engines* actually in use:

```bash
DATABASE_URL="$NRB_DB" .venv/bin/python scripts/nrb_recovery_cache.py --stats
#   legacy_conversion  npttf2utf 0.3.7/Preeti/lexicon cc1fec3f2808   ok  54
#   ocr                PP-OCRv5/devanagari/onnxruntime/…             ok   2
```

| Symptom | Cause | Fix |
|---|---|---|
| Run stuck `queued` | `app.nrb.runner` isn't running | start process 2 |
| Run stuck `awaiting_jobs` | `app.rag.worker` isn't running, or nobody reconciled | start process 3, then `--status` |
| Documents `pending` forever | same | as above |
| Job **succeeded** but the document has almost no chunks | `npttf2utf` missing → every legacy page withheld | install `requirements-nrb.txt` / build with `INSTALL_LEGACY_FONT=true` |
| Scanned PDFs come back empty in a container | `torch.compile` has no C++ compiler in slim | `TORCHDYNAMO_DISABLE=1` (already in `Dockerfile.worker`) |
| Container: `libxcb.so.1: cannot open shared object file` | OpenCV's native deps absent from slim | already handled in `Dockerfile.worker`; don't strip those apt packages |
| `POST /v1/nrb/runs` → 409 | an update is already `queued`/`running`/`awaiting_jobs` | read `run` from the body; wait or reconcile it |
| `POST /v1/nrb/runs` → 422 | no bounded scope given | add `sections`/`years`/`limit`/… (§6) |
| Script exits 2, "refusing to run" | `DATABASE_URL` isn't `local_ai_gateway_p4` | fix the URL; do not edit the guard |
| `alembic current` fails on the dev DB | by design (§3.1) | leave it alone |
| A run crashed and now nothing can start | a run left `running` occupies the single active slot | start the runner — it calls `recover_abandoned` **before** looking for work, unconditionally, for exactly this |
| Failed document never retried | the anti-join excludes `failed` | `--retry-failed` |
| Ingestion fails immediately at startup | embedding dimension mismatch | `ollama pull qwen3-embedding:4b-q8_0`; check `RAG_EMBED_DIM=1536` |

Two known issues, recorded and deliberately **not fixed**:

* `075bf12eb087` has a corrupt text layer at the codepoint level (a broken
  ToUnicode CMap — `कार्ाालर्` for कार्यालय). The classifier calls it clean because
  it asks "is this Devanagari", not "is it spelled right". Fixing it needs a new
  extractor version and a new evaluation cohort.
* Four English accounting templates are flagged as suspicious. They sit at ratio
  0.48–0.54, well below the 0.80 conversion gate, so they are never converted — a
  caveat, not a bug.

---

## 9. Configuration reference

Only the NRB-relevant keys; full list in `.env.example`.

| Key | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | **must** end in `local_ai_gateway_p4` for NRB work |
| `NRB_API_BASE_URL` | `https://www.nrb.org.np/api/forex/v1` | forex tool only; `/rates` is hardcoded |
| `NRB_SITE_BASE_URL` | `https://www.nrb.org.np` | the discovery **trust boundary** — anything on another host is reported and skipped, never fetched |
| `NRB_CRAWL_DELAY_SECONDS` | `0.25` | politeness pacing for a central bank's site |
| `NRB_FILES_DIR` | `nrb_files` | content-addressed blob store; relative paths anchor to the **repo root**. Sizing: ~8.6 GB full, ~1.5 GB for the regulatory core |
| `RAG_DOCS_DIR` | `rag_documents` | department corpus. NRB bytes are **resolved from the filestore, not copied here** (§28) |
| `RAG_EMBED_MODEL` / `RAG_EMBED_DIM` | `qwen3-embedding:4b-q8_0` / `1536` | changing the dimension is a schema change |
| `RAG_INGEST_HEARTBEAT_SECONDS` | `30` | keep well under `RAG_INGEST_STALE_MINUTES × 60` or long parses get swept |

---

## 10. What is not built yet

* **The full-corpus ingest.** Every structural gate is cleared; this is a run
  needing a go-ahead, not code.
* **Any scheduling.** No cron, no timer, no in-process scheduler. When it lands
  it must trigger *through* `pipeline.request_run` — it does not replace the
  runner.
* **The admin UI.** Lives in a separate frontend repo (`local-ai-model-frontend`)
  over `/v1/nrb/*`.
* **GPU-server deployment.** Blocked: this environment has no host, no SSH key
  and no remote Docker context. Server access is a prerequisite, not a step.
* **The Nepali semantic review** of recovered text. Until it happens, conversion
  correctness is unmeasured — hence the "VERIFY" caveat on citations.

---

## 11. Where to read more

| Question | File |
|---|---|
| Where does NRB stand, and why was X decided that way? | `docs/nrb-integration.md` (§ numbers below) |
| Code-level rules I must not break | `CLAUDE.md`, grep `nrb` |
| Which model runs where, hardware, ports | `docs/server-and-models.md` |
| Evidence packs, frozen cohorts, manual review | `docs/nrb/` |

Useful section numbers in the status doc: **§9** catalog/sync · **§10** fetch ·
**§11–17** extraction, legacy fonts, OCR, routing · **§18** container deployment
traps · **§19.1** why the GPU server is unreachable · **§20–22** corpus driver,
recovery cache, supersession · **§23–26** the pipeline service, admin API and
runner · **§27–29** Alembic lineage, `RAG_DOCS_DIR`, document search.

---

## 12. Keeping this document honest

* **Success metric:** a newcomer can go from a clean checkout to an NRB-grounded
  chat answer using only this page — no chat history, no reading
  `nrb-integration.md`.
* **Check it:** walk §3 → §4 → §5.1 → §7.4 on a scratch DB with a `--limit 5`
  scope. Every command must run as written (only `<PASSWORD>` substituted). Any
  command that needs an undocumented step is a defect in this file.
* **Feedback:** if you hit a symptom that isn't in §8, add the row — symptom,
  cause, fix. That table is the part of this page that earns its keep.
* **Review:** re-verify on any change to `app/nrb/pipeline.py`, `app/nrb/runner.py`,
  `app/nrb/router.py`, the compose files or the requirements files; otherwise
  quarterly. Update the "today" numbers in §1 and §7.2 when you do.

*Last verified against the code and the scratch database on 2026-08-18. Alembic
head `f4c1a90b7d62`.*

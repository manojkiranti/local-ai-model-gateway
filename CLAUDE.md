# CLAUDE.md — Local LLM Gateway

## System (Local LLM product)
```
Frontend  →  THIS GATEWAY (:8000)  →  Ollama LLM (:11434, OpenAI-compatible /v1)
                    |                    (inference only; NOT an MCP client)
                    ├─ Postgres (users, later chat history)
                    └─ remote MCP server (business tools)
```
This gateway is the **single authenticated front door**. Auth + ALL tool
execution live here (Pattern A). Ollama only runs the model and says which tool
to call; the gateway (the MCP client) actually calls it. The frontend talks ONLY
to this gateway, with a JWT bearer token.

Sibling project `../local-ai-model` is the original where this code was first
built/proven; code is being ported here. Don't edit it as part of gateway work.

**Server / models / DB facts live in `docs/server-and-models.md`** — GPU box
hardware, which model runs where (`qwen3.5:35b-a3b` chat, `qwen3-embedding:4b-q8_0`),
Postgres + pgvector layout, ports, RAG settings, what is not yet live. Read it
instead of guessing the environment; update it when any of it changes.

**NRB work runs against the SCRATCH database `local_ai_gateway_p4`, not
`local_ai_gateway`.** `DATABASE_URL=…/local_ai_gateway_p4` for every NRB sync,
fetch and DB test.

**The Alembic lineage is RESOLVED (2026-08-19, §30) — one linear head, nothing
stranded.** The old warning here (dev DB stamped at a revision that existed only on
a deferred branch, `alembic current` failing "by design") is **obsolete**: citations
was un-deferred, `d4a91f2c7b3e` was rebased onto the NRB head `f4c1a90b7d62` per
§27.4's runbook — same revision id, so the stamp stayed valid — and the dev DB was
reconciled by stamping back to the pre-fork baseline `c33c0fd56028`, upgrading
through the 7 NRB revisions, then stamping past citations (the `sources` column was
already there with identical DDL, so **no column was dropped and no chat row lost**).
`local_ai_gateway` and `local_ai_gateway_p4` are both at head. `alembic heads` must
stay **one** — `tests/test_alembic_lineage.py` fails if a second appears, and a new
migration must sit on the current head, never beside it.

**Image OCR in chat (`read_image`) lives in `docs/image-ocr.md`** — why not
docling, why not a VL model, the rapidocr config trap, the pixel-bomb guard, the
optional `INSTALL_OCR` build flag, and the 9-case eval.

**How to OPERATE the NRB integration lives in `docs/nrb-usage.md`** — the
runbook (prerequisites, the three processes, triggering/scoping a run, reading
status, troubleshooting). Point a human there instead of at this file or the
status doc.

**NRB integration status/roadmap lives in `docs/nrb-integration.md`** —
Phases 1–4 done and live-verified (forex tool; sitemap inventory; REST document
inventory; **persistent catalog + idempotent sync**, 18,577 sources / 18,266 files
reconciled, second run all-zero). Phase 5 (download + magic-byte
verification + SHA-256 + content-addressed storage) done and live-fetched.
**Phase 6A (native extraction + quality profiling) is done and live-profiled
2026-08-15**: a frozen 400-file benchmark (`docs/nrb/phase6a-manifest.json`,
`1ae297d…`), 381 fetched, all 381 extracted at `native-1`, and a frozen 40-PDF
pypdf-vs-Docling calibration (`81d5979…`) run over the 37 that fetched. Evidence
in `docs/nrb/phase6a-{profile,calibration}.txt`. **Phase 6B Task 1 (legacy-font conversion) is EVALUATED, not deployed
(2026-08-15, §12)**: Preeti recovers correctly above `legacy_line_ratio >= 0.80`
(7/10 blobs; one converts line-for-line identically to its rendered page), all 14
negative controls reconstruct byte-identically, and no routing is wired.
**Phase 6B Task 2 (`native-2` routing classifier) is MEASURED and recommended
(2026-08-15, §13)**: 381 native-2 rows sit beside native-1's, 7/7 English-table
false positives corrected, spreadsheets judged per CELL for the first time
(0 → 11 flagged), and 4 minority Preeti regions found inside Unicode-majority
documents — with `legacy_line_ratio >= 0.20` **unchanged**. Classification only:
it never invokes the converter.
**Phase 6B Task 3 (independent holdout validation) is DONE and native-2 is
VALIDATED (2026-08-16, §14)**: a frozen 150-file cohort
(`docs/nrb/phase6b-routing-holdout.json`, `6344e674…`) drawn with **zero
intersection** with Phase 6A, committed before any network access; 142 fetched
(8 honest 404s kept in the denominator), 142 native-2 rows, second pass 0 pending.
On files that never influenced it, native-2 flagged 67/142 — circular 9/9,
rule_bylaw 5/5, enforcement 4/4 — and the candidate gate `unit_legacy_ratio >= 0.80`
routed **56 blobs with 0 English false positives**, 52 recovering usable Unicode.
One new false-positive class was found (English accounting templates, 4 blobs) and
**reported, not fixed** — it sits at 0.48–0.54, entirely outside the gate, and its
fix is a `native-3` change needing a new cohort.
**Phase 6B Task 3B (evidence closure) is DONE (2026-08-16, §15)**: all 150 frozen
entries reconcile exactly (67 suspicious / 49 clean / 17 needs_ocr / 8 unsupported
/ 1 parser_error / 8 HTTP-404), no substitution, and the whole `>=0.80` queue —
all 56, not a sample — is laid out for a Nepali reader in
`docs/nrb/phase6b-routing-holdout-manual-review.md` with per-unit page numbers,
`Sheet!B27` cell coordinates, converted output and 61 rendered source pages. Three
corrections to §14 came out of it: the `unsupported` bucket is **8 OLE2 files
(6 `.xls` + 2 `.doc`)**, not 6; the false-positive class is **4 routed** documents
once clean documents are excluded from the definition; and of the three
false-negative candidates only **`a2077aa9b24d`** is real (the other two carry
English units native-2 was right not to route on) — it misses the minority-region
rule on `contested_legacy_ratio` 0.2857 alone. **Every semantic verdict is
`awaiting_nepali_review`; conversion CORRECTNESS is still unmeasured.**
**Phase 6B OCR strategy is DECIDED (2026-08-16, `faa9489`+`50edde6`, §16.6):
PP-OCRv5 Devanagari via docling/RapidOCR on the **onnxruntime** backend is the
fallback; **PP-OCRv4 is rejected** (it recovers the script but not the
orthography — halant/Devanagari char 0.0042 and mean word length 24.7 vs v5's
0.0798/5.4 against npttf2utf's own 0.0982/5.7); PaddleOCR-VL deferred.
**Phase 6B Task 4 (production extraction ROUTING) is IMPLEMENTED and tested
(2026-08-16, §16)**: `app/nrb/{provenance,ocr,recovery}.py` route each PDF PAGE
to native text, the guarded converter or OCR, and reconstruct in page order —
verified live on `e08988860534` (p1 OCR'd, p2-50 converted). The `>=0.80` gate is
unchanged and font provenance is consulted only INSIDE an eligible document.
**Phase 6B Task 5 (NRB text in department RAG) is DONE for a SMALL SAMPLE
(2026-08-16, §17)**: 8 named blobs → 250 chunks in the SCRATCH db, routes
`legacy_conversion` 239 / `ocr` 7 / `native` 4, all 8 jobs succeeded, and 7
retrieval queries returned the expected document (6 at rank 1) with page + route
intact. **No migration** — `document_chunks.page_number` + `metadata` JSONB
already carried citation provenance. It found a FOURTH text-trust failure mode
(§17.6): `075bf12eb087`'s own text layer is corrupt at the codepoint level
(`कार्ाालर्` for कार्यालय — a broken ToUnicode CMap), which native-2 calls clean
because it asks "is this Devanagari", not "is it spelled right". Recorded, not
fixed (that is a native-3 + new cohort).
**Deployment is validated in CONTAINERS ONLY (§18) — the GPU server has never
been reached (§19.1, re-checked 2026-08-17).** This working environment has no
SSH key, no `known_hosts`, no server address and no remote Docker context, so
`nic_ollama`/`nic_postgres`/the A40s and even whether `local_ai_gateway_p4`
exists *there* are all unverified. Server access is a **prerequisite**, not a
step: don't re-run laptop deployment testing in its place.
**Phase 7 step 1 (the corpus ingest DRIVER) is DONE and validated on 31
documents (2026-08-17, §20)**: `app/nrb/corpus.py` + `scripts/nrb_rag_ingest_corpus.py`
select from the catalog ONLY, refuse to run unscoped, skip what exists by
anti-join, and enqueue without draining — 31 created in 0.2 s, 30 ready / 1,029
chunks, the one OLE2 file failing mid-run without stopping the batch, a second
pass selecting 0, and all **8 anchors reproducing §18.7 exactly**. The cohort is
frozen at `docs/nrb/phase7-validation-cohort.json` (`f2d36b4c…`, 8 route-aware
anchors + 22 blind + 1 unsupported) and its pool is the 570 FETCHED blobs, so it
supports **no population claim**.
**Phase 7 step 1.1 (explicit failed-document retry) and step 2 (the VERSIONED
RECOVERY CACHE) are DONE (2026-08-17, §21).** `--retry-failed` requeues a
`failed` document against its EXISTING row (no second `documents` row, `ready`
/`pending`/`archived` unreachable, no NRB-less document adopted); and
`app/nrb/recovery_cache.py` + migration `714264eba2fd` make recovery reusable —
verified on 4 real blobs / 56 units at **0 npttf2utf calls and 0 PP-OCR calls**
on the second pass, chunk counts unchanged (1/2/9/75 = §18.7's), plus a real
worker re-ingest logging `warm … 4 reused, converter 0, ocr 0`.
**Phase 7 step 3 (SUPERSESSION) is DONE (2026-08-17, §22).** A republished NRB
file's new version becomes current only when its ingest SUCCEEDS, in the same
transaction that archives the old one (`app/nrb/supersession.py`,
`worker._activate`, migration `8f2d1c05a7b4`); a failed candidate never retires
the version that is serving. Proved by 19 tests and a four-revision real-data
exercise (`scripts/nrb_supersession_exercise.py`, all checks passed).
**Phase 8 is DONE (§29): NRB documents are searchable through the EXISTING
`search_department_docs` tool** — no separate `search_nrb_documents` (a
cross-department tool would fight the department-scope FK invariant; NRB docs are
department documents with `origin=nrb`). `RetrievedChunk` now carries chunk +
document metadata, and citations show the extraction route with a
"machine-recovered — VERIFY" caveat for OCR/legacy pages plus the NRB source URL +
published date. **The NRB CORPUS is still not ingested** (so this is proven on the
sample, not the corpus, and recovered-text CORRECTNESS is still §15's open Nepali
review — the caveat exists precisely because of that). The 6B gate and its
recommendations are §11.9, §12.10, §13.11, §14.7, §15.9, §16.10, §17.8, §19.5,
§20.9, §21.10 and §22.12. Phase 7's `RAG_DOCS_DIR` duplication gate is **DECIDED
and removed (§28): NRB bytes resolve from the filestore, no copy**, so nothing
structural blocks a full-corpus ingest. **Next unblocked: the actual corpus
ingest (a run, needs the go-ahead), then cron/systemd; the thin admin UI lives in
a separate frontend repo.**
**Phase 7 step 4 is DONE (2026-08-17, §23):** the §22.10 sync defect is FIXED
(state ownership — see the gotcha below), and `app/nrb/pipeline.py` is the ONE
orchestration path (`sync → fetch → extract → rag enqueue`) that the CLI, the
future admin API and a future schedule all call, with a durable
`nrb_pipeline_runs` identity, advisory-lock exclusion and an explicit run→job
relation. Migration `1fb5a0d183d6`. Run it via `scripts/nrb_pipeline.py`
(`--status` reconciles).
**Phase 7 step 5 is DONE (2026-08-17, §25): the thin admin API.**
`POST /v1/nrb/runs` (202 `{started, run}`, or **409 with the SAME envelope**
carrying the active run — both the advisory lock and the durable
`running`/`awaiting_jobs` gate mean one thing to a client),
`GET /v1/nrb/runs/{id}`, `GET /v1/nrb/status` (active/latest run +
`catalog_counts` + `fetch_counts` + `corpus.nrb_rag_counts`). Admin-only via the
existing `require_admin`; **the API cannot start a full-corpus run** — a bounded
scope is required (422) and `all_files` is not a field (`extra="forbid"`);
`--all` stays CLI-only. **Phase 7 step 6 (2026-08-17, §26): orchestration LEFT the HTTP request.**
`POST` calls `pipeline.request_run` → one `queued` row → 202 in ~78 ms; the
staging runs in `python -m app.nrb.runner` (`app/nrb/runner.py`, gateway image,
compose service `nrb-runner`) via `pipeline.execute_run`. `pipeline.start` is
just those two composed and is used by `scripts/nrb_pipeline.py --run-now` and
tests, NEVER by the API. Migration `f4c1a90b7d62` adds the `queued`
status/stage (three CHECK vocabularies forbade it) and
`ux_nrb_pipeline_runs_one_active` (UNIQUE over the constant `(true)` on the three
active statuses). **UI and cron are NOT implemented.**
The roadmap was renumbered when Phase 4 was scoped down to
persistence; read that doc before touching anything NRB-related instead of
re-deriving status from chat history.

## Environment / commands
- **Use THIS project's `.venv`** (`.venv/bin/python`, `.venv/bin/pip`, `.venv/bin/uvicorn`,
  `.venv/bin/alembic`, `.venv/bin/pytest`). Never install into a sibling's venv. Python 3.10.
- Run: `.venv/bin/uvicorn app.main:app --reload --port 8000`  (Swagger at `/docs`).
  **Port convention: this gateway = 8000** (front door the frontend targets);
  sibling `local-ai-model` = 8001. Never run both on the same port.
- Migrations: `.venv/bin/alembic revision --autogenerate -m "msg"` then `.venv/bin/alembic upgrade head`
- Tests: `.venv/bin/pytest`
- Config via `.env` (see `.env.example`). `DATABASE_URL` and `JWT_SECRET` are required.
- **Three long-running processes, three jobs.** The API accepts work; neither of
  the other two is optional if NRB is in use, and an accepted run just sits
  `queued` without them:
  ```
  .venv/bin/uvicorn app.main:app --port 8000    # accepts (never parses/embeds)
  .venv/bin/python -m app.nrb.runner            # NRB staging: sync→fetch→extract→enqueue
  .venv/bin/python -m app.rag.worker            # recovery→chunk→embed→supersession
  ```
  For NRB work every one of them needs `DATABASE_URL=…/local_ai_gateway_p4`.

## Postgres (local dev)
Local PG17 via TCP. Superuser: `postgres`/`postgres` on 127.0.0.1:5432 (peer auth
fails — no `manoj` role). App uses role `gateway` / db `local_ai_gateway` (creds
in `.env` only, never in code). Create with:
`psql -h 127.0.0.1 -U postgres -c "CREATE ROLE gateway LOGIN PASSWORD '...'; CREATE DATABASE local_ai_gateway OWNER gateway;"`

## Layout
`app/{config,main}`, `db/`, `auth/` (`security` = JWT+bcrypt, `dependencies` =
`get_current_user`/`require_admin`/`get_current_user_optional`, `directory` = the
ONLY file that knows the Active Directory HTTP shim exists, `throttle` =
per-identifier login limiting, `router` = `/auth/*`), `users/`
(`models`+`schemas`+`router`, `repository` = the shared lookup plus directory
provisioning), `ollama/` (client), `chat/`,
`mcp/` (client), `nrb/` (Nepal Rastra Bank: `client` = Forex API behind
`get_nrb_forex`; `http` = shared host guard + `FetchError`; `sitemap`+`classify`
= Phase 2 URL inventory; `wp_api`+`documents`+`attachments`+`page` = Phase 3
document discovery; `models`+`records`+`catalog`+`discovery`+`sync` = Phase 4
persistent catalog (`nrb_sources`/`nrb_files`/`nrb_source_files`/`nrb_sync_runs`;
`records` is pure, `catalog` is set-based data access, `sync` is the idempotent
reconciliation — no downloads); `sniff`+`filestore`+`fetch`+`locks` = Phase 5 file
download (`sniff` is pure magic-byte typing, `filestore` is the content-addressed
blob store, `fetch` is the resumable pass, `locks` is the advisory-lock rule shared
with `sync` — still no parsing); `quality`+`extraction`+`extract`+`profile`
+`sampling`+`manifest` = Phase 6A native extraction & the frozen 400-file
benchmark; `calibration`+`calibrate` = the frozen 40-PDF Docling calibration
subset and the pypdf-vs-Docling comparison pass (writes NOTHING; Docling imported
lazily); `legacy_font`+`devanagari`+`lexicon`+`legacy_convert`+`legacy_eval`
+`legacy_report` = Phase 6B Task 1 legacy-font→Unicode conversion **evaluation**
(`legacy_font` is the ONLY file that knows npttf2utf exists — GPL-3, lazy import,
`requirements-nrb.txt` which `Dockerfile` does not install; `devanagari`+`lexicon`
are the plausibility signals; `legacy_convert` is per-line/per-cell routing +
validation; writes NOTHING); `units`+`routing` = Phase 6B Task 2 `native-2`
(three-state judgment units — lines for text, CELLS for spreadsheets — and the
routing classifier; imports NOTHING from `legacy_font`, run via
`scripts/nrb_extract.py --extractor-version native-2` and compared by
`scripts/nrb_native2_compare.py`); `provenance`+`ocr`+`recovery` = Phase 6B Task 4
production extraction routing (`provenance` reads per-page fonts/images from the
PDF with **pypdf, no subprocess**; `ocr` is the ONLY file that knows docling's OCR
stage exists — PP-OCRv5/onnxruntime, lazy import, `requirements-worker.txt`;
`recovery` routes each page to native/legacy_conversion/ocr and rebuilds in page
order, persisting NOTHING itself, run via `scripts/nrb_recover.py`);
`recovery_cache` = Phase 7 step 2, the versioned recovery cache
(`nrb_recoveries`/`nrb_recovery_units`; the ONLY file that persists recovered
text, reached from `worker._load_chunks` via `chunks_for_blob`, operated by
`scripts/nrb_recovery_cache.py --stats/--reuse-check/--purge`);
`pipeline` = Phase 7 steps 4+6, the shared orchestration SERVICE
(`nrb_pipeline_runs` / `nrb_pipeline_run_jobs`; `request_run` admits a `queued`
run, `execute_run` claims and stages it, `start` = both composed, `reconcile`
/`recover_abandoned` are the recovery pair; calls the stage services, never a
subprocess); `runner` = the PROCESS that executes them
(`python -m app.nrb.runner`, gateway image, compose service `nrb-runner`; a poll
loop and nothing else — no locking, transitions or stage logic);
`router`+`schemas` = the thin admin API (`/v1/nrb/*`, admin-only via
`require_admin`; the ONLY model-facing-adjacent HTTP surface NRB has, and it
calls one service per handler);
`supersession` = Phase 7 step 3, which version of a logical NRB source is
searchable (logical identity = `documents.metadata.comparison_key`; called from
`worker._activate` INSIDE the replacement transaction; exercised by
`scripts/nrb_supersession_exercise.py`); `rag` = the ONLY
seam between NRB and department RAG (`parse_nrb_to_chunks`, reached from
`worker._load_chunks_sync` via `documents.metadata.origin == "nrb"`; chunks per
PAGE, route in `document_chunks.metadata`, exercised by
`scripts/nrb_rag_ingest.py`); `report` = all of them — everything but `client` is
**not** model-facing, run via
`scripts/nrb_{sitemap_inventory,document_inventory,sync,fetch,sample,extract,calibrate,build_lexicon,legacy_eval,native2_compare,holdout_validate,holdout_evidence}.py`), `tools/` (`registry.py` = engine; `local/` package = one module
per in-process tool, each exporting a `SPEC`, aggregated in `local/__init__.py`'s
`LOCAL_TOOLS`), `agent/` (hand-rolled loop; `loop.stream_turn` = async event
generator, `loop.run_turn` = collect for non-stream, `schemas` = trace types —
**no router**, it's driven by `/v1/chat`), `files/` (per-user generated AND
uploaded files: `models`=`generated_files` table (with `source`
`generated|uploaded`), `repository`=data access, `store`=`file_sink`+`file_source`
contextvars + async `save`/`resolve_file` + in-memory fallback, `sink`=
`PostgresFileSink` (owns its own commit), `source`=`PostgresFileSource`
(owner-scoped id→path resolver) + `turn_files` (installs sink+source together),
`readers`=xlsx/csv→`Table` normalizer (pure, no formula eval), `documents`=
pdf/docx/txt/md/json→flat lines normalizer (pure; PDF page markers,
scanned-vs-empty distinction), `images`=png/jpg/webp/tif/bmp→format+dimensions
(pure, **never OCRs** — the summary is recomputed every turn; owns the
decoded-PIXEL cap and the decoder allowlist), `image_ocr`=the ONLY file that
knows rapidocr exists (PP-OCRv5/onnxruntime, lazy import,
`requirements-ocr.txt`; imports no part of docling), `ingest`=extension→family
dispatch (spreadsheet vs document vs image) shared by the upload route and
turn-open, `router`=upload
`POST /v1/files` + `GET /v1/files` list + owner-scoped `/v1/files/{id}`; feeds
create_excel/html/chart/pdf/csv/docx and inspect_excel/read_excel/
read_document/read_image),
`history/` (chat-history: `models` = `chat_sessions`
+ `chat_messages`, `repository` = data access, `service.open_turn` = shared
turn-open used by chat, `router` = `/v1/sessions`),
`rag/` (department-scoped RAG: `models` = `departments` + `user_departments` +
`documents` + `document_chunks` (pgvector `vector(1536)` + generated `tsv`) +
`ingest_jobs`, `context` = `rag_context`/`current_department` contextvar,
`access.resolve_department` = the permission boundary, `repository`/`documents` =
data access, `storage` = `storage_key` minting + traversal-safe resolution,
`chunking`/`parsing` = content → `Chunk[]` (Docling lazily, worker only),
`embedding` = query/document-aware embed + 2560→1536 + normalize, `jobs` =
Postgres queue, `ingest` = atomic replacement, `worker` = the separate ingest
process, `ranking` = the ONE place a refusal is decided (pure `decide()` +
IO-only `apply()`, split like `permissions`/`access` so the code producing a
user-visible refusal is provable with no DB and no GPU; **fails OPEN**, inverting
`app/nrb/`'s rule — there withholding text stops garbage being published, here
withholding an answer asserts something false about the bank's own policies),
`eval_metrics` = the four retrieval metrics, pure so a broken metric cannot
report a false pass (`false_refusal_rate` is the one that governs the operating
point), `router`/`jobs_router` = `/v1/departments` + `/v1/ingest-jobs`).
`alembic/` for migrations. Retrieval eval lives in
`scripts/rag_eval_{build_cohort,sweep}.py` — the first generates cohort
CANDIDATES and REFUSES to freeze while any question still carries its `[REVIEW`
marker (a cohort is evidence only because a human wrote its questions), the
second sweeps threshold x pool over the frozen cohort and prints the table the
operating point is chosen from.
- **Adding a local tool:** new `app/tools/local/<name>.py` with `_fn` + `SPEC`,
  then add `<name>.SPEC` to `LOCAL_TOOLS`. The engine (`registry.py`) never changes.

## Endpoints
Public: `/health`, `POST /auth/login` (local password OR Active Directory,
decided by the user's own `auth_provider` — 401 rejected / 403 inactive /
**429 throttled, `Retry-After`** / **503 directory unreachable, which is NOT a
wrong password**).
Admin: `POST /auth/register` (creates a **local** account for service or
break-glass use; unauthenticated ONLY while the `users` table is empty, where it
mints the first admin).
Authed (JWT): `GET /users/me`, `GET /users` (admin; `?q=` = case-insensitive
email substring, LIKE wildcards literal), `PATCH /users/{id}` (admin,
`{is_active}` only — 409 on self-deactivation or the last active admin),
`POST /v1/chat`,
`POST /v1/nrb/runs` (admin, 202 `{started, run}` / 409 same shape when busy),
`GET /v1/nrb/runs/{id}` (admin), `GET /v1/nrb/status` (admin, `?department=`),
`GET /v1/tools`, `GET /v1/mcp/status`, `POST /v1/files` (upload .xlsx/.csv/
.pdf/.docx/.txt/.md/.json/.png/.jpg/.jpeg/.webp/.tif/.tiff/.bmp →
`generated_files` row `source=uploaded`; 400 bad
ext/corrupt/zip-bomb/pixel-bomb/non-allowlisted image format, 413 over size
cap), `GET /v1/files` (caller's files,
newest first; `?source=` filters),
`GET /v1/files/{id}` (owner-scoped download; 404 if not yours),
`DELETE /v1/files/{id}` (owner-scoped; 204, removes row + on-disk file),
`GET /v1/sessions` (keyset-paginated, `{items, next_cursor}`; `?limit=`
defaults 30/max 100, `?cursor=` continues the page, 400 on a malformed
cursor), `GET /v1/sessions/{id}` (newest page of that thread, ascending by
`seq`, same `{items, next_cursor}` envelope and `?limit=`/`?cursor=`/400
contract), `DELETE /v1/sessions/{id}`,
`POST /v1/departments` (admin), `GET /v1/departments` (admin → all; member →
granted+active, i.e. the frontend's tabs — **each row carries `role`, the
caller's own effective level**), `PATCH /v1/departments/{code}`
(admin), `GET|POST /v1/departments/{code}/members` (**dept owner** or admin; POST
takes `user_id` **XOR** `email` plus optional `role`, and is also the
promote/demote route — 403 if an owner asks for `owner`, 409 if the account is
deactivated, and an **omitted `role` keeps the level**),
`DELETE /v1/departments/{code}/members/{user_id}` (**dept owner** or admin; 403 if
an owner targets another owner — but their OWN row is theirs),
**all three member routes work on a soft-disabled department**, unlike every
document route,
`POST /v1/departments/{code}/documents` (**dept editor**, multipart → 202
`{document_id, job_id}`; 400 bad ext/empty, 409 duplicate content, 413 over cap),
`POST /v1/departments/{code}/documents/text` (**dept editor**, typed text →
`source=manual`),
`GET /v1/departments/{code}/documents` (dept **viewers** see `ready` only;
**editors** see non-archived, `?include_archived=` editor-only),
`DELETE /v1/departments/{code}/documents/{id}` (**dept editor**; archives — chunks
removed, row retained),
`GET /v1/departments/{code}/documents/{id}/download` (what a chat citation links
to — original bytes; 403 ungranted department, 404 at document granularity:
unknown id, another department's, non-`ready` for a viewer, or bytes missing),
`GET /v1/ingest-jobs/{id}` (**dept editor** of the job's document, or admin;
**404** — never 403 — when you may not see it, because a job id maps to a
document).
`GET /v1/mcp/status` is the UI's MCP-connection badge — **always 200**, health
is in the body (`configured/reachable/tools/error`), never a 502.
`POST /v1/chat` is the **single, unified** turn endpoint — **stateful**
(`{session_id?, message, model?, stream?, options?, file_ids?}`, server rebuilds
context + persists both rows) and **tool-capable** (runs the agent loop every
turn; the
model calls local/MCP tools when useful). `stream:false` → JSON
`{session_id, message, model, stop_reason, trace?, sources?}`; `stream:true` →
NDJSON typed events (`token`/`tool_call`/`tool_result`/`done`, with `sources` on
`done`) + the new id in the `X-Session-Id` header. **There is no `/v1/agent`** —
it was folded in.

## Conventions / gotchas
- **A chat answer's `sources` are collected out of band, and only the passages the
  model actually SAW may appear.** A tool returns a string, so `app/rag/sources.py`
  is the return channel: a per-turn collector on a contextvar, installed by the chat
  router beside `turn_files` and `_department_scope` — same streaming rule (set it
  INSIDE the generator Starlette iterates), and the collector object is constructed
  *outside* the `with` so the `finally` that writes the assistant row can still read
  it. Five things a rewrite must not lose: (1) `search_department_docs` records only
  the passages that survived its character budget (`_format` returns
  `(text, presented)`, both drop paths discard from the END so `[1..k]` still
  indexes the survivors) — a trimmed passage was never in context, and citing its
  document would invent provenance; (2) with **several** searches every presented
  document is `cited:false`, because each call restarts numbering at `[1]` and
  guessing would link the wrong file; (3) an out-of-range `[9]` is dropped, not an
  error, and `[2024]` in prose is not a citation (the marker regex is bounded to
  three digits); (4) `download_url` is **derived** at serialization from
  `department_code + document_id` and never stored, so rows survive a route change
  — safe only because `departments.code` is immutable (`PATCH` touches name and
  is_active alone); (5) `sources` is **never** gated by `EXPOSE_TRACE` — the trace
  is diagnostics, a citation is the product — and `null` (no corpus searched) is a
  different fact from `[]`.
- **The "machine-recovered — VERIFY …" sentence is ONE constant with TWO readers.**
  `sources.VERIFY_NOTE`/`RECOVERED_ROUTES` are rendered into the model's context by
  `search_department_docs._nrb_provenance` **and** published as a source's
  `verify_note`; a second copy would drift, and a UI badge contradicting the answer
  text leaves the reader unable to tell which to believe
  (`test_the_caveat_is_one_constant_with_two_readers`). A source's `routes` is the
  **union** over the pages the model saw, because an NRB PDF is routed per PAGE
  (§16) — reporting only the first route would hide the recovered page, which is
  exactly the page a reader must check. `authoritative: false` alone is enough to
  caveat a route we have not enumerated. Native NRB text carries the route WITHOUT
  the caveat (§29.2: over-warning trains a reader to ignore the warning).
- **The citation download reads TWO trees, and NRB's is not `RAG_DOCS_DIR`.**
  `rag/router._document_path` branches on `documents.metadata.origin`: an NRB
  document's bytes exist only in the content-addressed NRB filestore (§28 removed
  the copy), and the key is **reconstructed from the content hash** rather than read
  from `storage_key` — exactly as `worker._document_path` does — so a row minted
  under the old copy scheme still resolves with no migration
  (`metadata.blob_sha256` is the fallback digest). Consequences: an NRB row is
  legitimately without a usable `storage_key`, so that check lives inside the
  resolver, and **all three containers must mount the one `nrb_files` volume** —
  nrb-runner writes it, worker reads it during recovery, gateway serves it. A
  private per-container copy fails the §18 way: fetch reports success, the job
  succeeds with no text, the download 404s a document listed as `ready`.
- **A relative `RAG_DOCS_DIR` is anchored to the REPO ROOT, not the CWD**
  (`Settings.rag_docs_base`, mirroring `filestore.base_dir()`). Three processes read
  that tree now — API, worker, download route — and a CWD-relative resolve makes
  them disagree silently.
- Auth: JWT (PyJWT HS256) + bcrypt. User = email, `auth_provider` (`local`|`ad`),
  nullable password_hash, role admin|member. **First registered user → admin, and
  that rule is REGISTRATION-ONLY.**
- **`/auth/login` dispatches on `auth_provider`; it is never a fallback chain.** A
  `local` user is checked against bcrypt and Active Directory is not consulted; an
  `ad` user is checked against the directory and their (always NULL) hash is not.
  "Try AD then local" would let an offboarded employee keep signing in on a stale
  hash after AD disabled them; "try local first" would let a local password shadow
  an AD identity. `ck_users_credential` makes the illegal state unrepresentable —
  a local user MUST have a hash, an `ad` user MUST NOT — and
  `ck_users_auth_provider` closes the vocabulary because the dispatch reads it
  (adding a provider means editing the CHECK, same rule as `ck_documents_status`).
  With `AD_AUTH_ENABLED=false` an existing `ad` user gets **503, never a local
  fallback**. Six things a rewrite must not lose:
  (1) **the "first user → admin" rule must never reach the provisioning path** —
  `_resolve_role` counts rows and is reachable only from `register`, while
  `users_repo.resolve_directory_role` consults ADMIN_EMAILS alone; otherwise
  whoever signs in first on a fresh deploy owns the system (AST-locked in
  `tests/test_directory_provisioning_rules.py`);
  (2) **an unreachable directory is 503, a rejected credential is 401** — the shim
  answers only `Success`/`Failed`, so `DirectoryOutcome` is THREE states and an
  unrecognised body (empty, IIS error page, SOAP fault, redirect) is a malfunction,
  not a rejection: rendering an outage as "invalid password" sends the whole office
  to reset passwords that were never wrong;
  (3) **`UNAVAILABLE` must not consume a throttle attempt**, or an AD outage locks
  everyone out for the lockout period on top of the outage;
  (4) **the shim takes credentials in the QUERY STRING over plain HTTP, so the URL
  is a secret** — `directory.py` never logs the URL, the params, the response BODY
  (a shim echoing the query back would put the password in it) or the caught
  exception *object* (`httpx.HTTPStatusError.__str__` embeds the URL, so
  `f"failed: {exc}"` is a credential leak), and it pins the `httpx`/`httpcore`
  loggers at WARNING because they log request URLs at DEBUG;
  (5) **`is_active` is checked AFTER the credential**, as it always was — checking
  first would make a disabled account answer 403 to any password and confirm it
  exists;
  (6) `register` is admin-only because public registration let anyone pre-register
  a colleague's address as a `local` account and permanently shadow their AD
  identity — and on an empty table take admin with it.
- **`is_active=false` is the offboarding switch; disabling the AD account is
  NOT.** AD is consulted at the login boundary only, so an account disabled in AD
  keeps working through an already-issued JWT until it expires — up to
  `JWT_EXPIRE_MINUTES` (**24h** by default, and there is no refresh flow to
  shorten). `get_current_user` re-reads the user row every request, so clearing
  `is_active` takes effect on the holder's very next call. Anyone writing an
  offboarding runbook needs both steps, in that order of importance.
  `PATCH /users/{id}` is that lever, and its guards are a PURE function
  (`app/users/policy.py`) for a reason: refusing self-deactivation and refusing
  the last active admin are the two ways to lock everyone out of the deployment,
  and the last-admin branch cannot be safely rehearsed against a real database —
  proving it needs reducing that database to one admin. `count_active_admins`
  takes `FOR UPDATE` so two admins deactivating each other concurrently cannot
  both pass the guard. **`role` is deliberately NOT patchable** (`extra="forbid"`
  rejects it loudly rather than ignoring it): promotion is an escalation surface
  wanting its own guards, ADMIN_EMAILS designates admins at creation, and
  changing an existing user's role is still a SQL-only operation.
- **Login is throttled, and that is an AD requirement, not hygiene.** Once
  `/auth/login` forwards a credential to the domain, an unthrottled endpoint is a
  remote way to trip the **AD lockout counter on every account in the company**.
  `app/auth/throttle.py` limits per identifier (both providers), so keep
  `LOGIN_MAX_ATTEMPTS` below the domain's own threshold. Counters are **per
  process** — N uvicorn workers means N x the limit — and eviction under the size
  bound deliberately **prefers UNLOCKED entries**, because if a flood of distinct
  identifiers could evict a locked one, the flood would be a way to clear a
  lockout.
- **A test that creates a user must now supply a password_hash or an admin token.**
  Two consequences of the above that already bit once: a fixture inserting
  `auth_provider='local'` with a NULL hash violates `ck_users_credential` (it was
  creating a user who could never log in), and the `_auth` helpers duplicated
  across ~12 integration test modules had to gain `_ensure_user`, which logs in as
  the seeded admin before registering. Those helpers **skip on auth failure**, so
  breaking them turns 86 tests into silent skips that a green run hides — compare
  the skip count, not just the pass count.
- **AD has never been reached from a dev environment.** `10.10.0.66` is unroutable
  here, so UPN-vs-sAMAccountName and the real response envelope (ASMX usually
  wraps it `<string>Success</string>`) are **unverified against the live service**.
  The parser is tolerant of both, but if AD turns out to reject UPN the identifier
  mapping needs a nullable UNIQUE `ad_username` column — a schema change, not a
  tweak, because the shim returns no email to key on.
- Agent loop is **hand-rolled, no framework** — keep it readable/commented.
- **Never** use the `ollama` SDK, and don't add the `openai` SDK either — we call
  the model server's OpenAI-compatible REST surface (`/v1/chat/completions`,
  `/v1/models`, `/v1/embeddings`) with httpx. The `openai` SDK would not solve
  streamed tool-call fragment accumulation for us (only its *beta* stream helper
  accumulates) while displacing our `OllamaError` → HTTP-status mapping.
- **The wire format lives in ONE file:** `app/ollama/client.py`. `stream_chat`
  yields normalized events (`{"type":"content","text"}` /
  `{"type":"tool_calls","calls"}` / `{"type":"finish","reason"}`); the agent loop
  never sees SSE or `choices[0].delta`. Pointing `OLLAMA_BASE_URL` at vLLM /
  llama.cpp / LiteLLM should need no edits outside that file.
- **Tool-call streaming differs per backend:** Ollama's `/v1` shim sends each
  tool call whole in one delta; **vLLM fragments `arguments` across deltas**.
  `merge_tool_call_deltas` handles both. The fragmented path is covered by
  hand-authored fixtures in `tests/test_openai_stream_parsing.py` because our
  Ollama can't produce it — re-verify live when vLLM lands.
- **Tool results correlate on `tool_call_id`**, not Ollama's `tool_name`. Ids
  come from the server (`finalize_tool_calls` synthesises a fallback). Getting
  this wrong silently mismatches results in multi-tool turns.
- **`num_ctx` is NOT a request field** — the `/v1` surface has no `num_ctx`
  (Ollama's shim ignores a passthrough `options.num_ctx`; verified 0.32.5 —
  requested 8192, loaded 4096). Set context server-wide on the Ollama service:
  `OLLAMA_CONTEXT_LENGTH=32768`. Without it Ollama defaults to **4096**, which is
  too small — **15** local tool schemas alone measured **3475 tokens** on
  2026-08-11 (via `usage.prompt_tokens`, qwen2.5; a bare turn's prompt floor was
  3778, leaving ~300 of a 4096 window). `LOCAL_TOOLS` is now **17** (`read_document`
  then `read_image` landed after that measurement) — the token figure has not
  been re-measured since, so treat it as a floor, not the current count. Either way
  one 8000-char tool result overflows. This matches vLLM's `--max-model-len` (a launch flag),
  so it stays a config value across backends. See
  `docs/llm-transport-and-deployment.md`.
- Use `resp.aiter_lines()` for SSE — never `aiter_bytes()` with manual `\n\n`
  splitting, which truncates JSON across HTTP chunk boundaries under load and
  presents as a flaky model rather than a parser bug.
- **fetch_url SSRF rule:** the `fetch_url` tool (outbound HTTP GET) must keep its
  guards — http/https only, resolve the host and refuse if ANY IP is
  non-public (blocks localhost/private/link-local incl. 169.254.169.254 metadata),
  re-check every redirect hop, GET-only, timeout + byte cap. Never relax these to
  "make it work"; internal services (Ollama/PG/MCP on localhost) are reachable
  otherwise. Config: `FETCH_URL_ENABLED`, `FETCH_URL_ALLOWLIST`.
- **`get_nrb_forex` is NOT fetch_url with a nicer name.** The NRB host is
  `NRB_API_BASE_URL` and `/rates` is hardcoded in `app/nrb/client.py`; the tool's
  schema is `{from?, to?, currency?}` with **no `url`/`page`** — there is nothing
  for an injection to point at. Four things the live API does that defensive
  parsing must handle (probed 2026-08-10): (1) **`page`+`per_page` are mandatory**
  (omit → validation errors, `payload: null`); (2) **HTTP is always 200** — the
  real status is `status.code`; (3) **`status.code` is 400 for an empty-but-valid
  query too** (future date, reversed range) with `payload: []`, so success is
  decided on `data.payload` being a *list*, never on the status; (4) a
  **non-trading day** publishes every currency with `buy`/`sell` **null** — the
  client classifies that `UNQUOTED` (info log) vs `UNREADABLE` (warning) so a
  public holiday doesn't emit 22 warnings, and the tool says "quoted no rates"
  instead of rendering an empty table. Buy/sell stay NRB's **strings** (no float
  round-trip, no arithmetic — official figures), and the **unit is always
  printed** (INR is per 100, JPY per 10; dropping it is a 100x error). Range caps:
  31 days, or 3 without a `currency` (a full day is ~22 rows). NRB DOCUMENT search
  (policies/circulars/directives) is **`search_department_docs`, not a separate
  tool** (§29): NRB docs are department documents with `origin=nrb`, and a
  cross-department tool would fight the department-scope FK invariant. So this
  tool's negative-routing clause names `search_department_docs`, and that name is
  asserted by `test_description_routes_documents_away_from_this_tool` — keep the
  "not for policies/circulars/directives → search_department_docs" clause here.
- **NRB's sitemap does not tell you what a document IS.** Measured live
  2026-08-13: 19,480 URLs, of which 18,567 are `/{owner}/{slug}/` custom-post-type
  documents whose slug is a Devanagari title. The directive/circular/act
  vocabulary exists ONLY on the 359 `/category/…` archive pages, so **95.8% of
  URLs classify `section=unknown` and that is the correct answer**, not a broken
  classifier — hence `page_kind`, which says *why*. Also: the sitemap contains
  **zero PDF/attachment URLs** (they are linked from inside pages) and the WP REST
  API is 404, so Phase 3 cannot get section or attachments cheaply — it has to
  walk the category archives or the post pages. Two traps: the owner code is in
  the URL's first path segment, **except** `/federal-offices/<code>/<slug>/` where
  it is the second (385 URLs), and the sitemap *filename* disagrees with the path
  for those and for `ditty_news_ticker-sitemap.xml` → `/ticker/…`. Never expand a
  code (`bfr`, `ficpd`, `skt`) to a guessed name. `parse_sitemap` rejects any
  doctype/entity declaration before parsing — stdlib ElementTree expands internal
  entities, so the byte cap is not a billion-laughs guard. Every bound (depth,
  sitemap count, URL count) records itself in `inventory.truncated` and the CLI
  exits non-zero, because a silently truncated inventory reads as the whole site.
- **NRB's WordPress REST API is open at `/api/`, not `/wp-json/`** — the Phase 2
  note that it was "404, unavailable" was wrong; only the default prefix is
  disabled (every page advertises the real one as `<link rel='https://api.w.org/'>`).
  That is why Phase 3 is `wp_api.py` and not a page crawler: `/api/wp/v2/{type}`
  returns `acf.document_file` (url + WordPress's own `mime_type` + filesize),
  `secondary_file`, real `date`/`modified`, and `categories[]` in 100-post pages —
  **~190 requests for the whole corpus instead of 18,567**, and the rendered HTML
  page has *no date metadata at all*. Facts a rewrite must not lose: (1) a
  document post URL **302s to its file** (`acf.document_redirect_to_file`), so 95%
  of them are not HTML — `page.py` exists to *verify* that redirect, not to scrape
  it; (2) `acf` is `[]` on fieldless posts and an unset file field is `false`, not
  absent; (3) **`per_page` maxes at 100** and a page past the end is a 400, which
  is a terminator not an error; (4) REST returns attachment paths with literal
  Devanagari while the 302 `Location` percent-encodes them — the same file, so
  dedup uses `attachments.comparison_key` (decoded path), while `Attachment.url`
  keeps NRB's spelling; (5) `economic-review` + `er-article` (196 URLs) are in the
  sitemap but **not REST-registered**, a corpus gap reported separately from
  failures; (6) HTML is parsed with **stdlib `html.parser`** — bs4/lxml are in the
  venv only as docling (worker-only) transitive deps and must not enter the API
  image.
- **Document type comes from NRB's category ids, never from the slug.**
  `documents.Taxonomy.section_for` walks the category **parent chain** into Phase
  2's `CATEGORY_SECTIONS` because NRB files posts under children
  (`domestic-tenders` under `tenders`, `2082-83` under `circulars`). `sections` is
  a **tuple** — posts really are filed under several — and `primary_section` picks
  by `SECTIONS` order, which is why that tuple is ordered regulatory-first.
  Coverage measured over the full 18,370-document corpus is **71.6%**, but that
  single number is misleading and the report deliberately breaks it out by year:
  every year except 2019 runs **89–100%**, while 2019 alone (9,189 docs, NRB's CMS
  migration) is 47.5% because 5,052 of them sit in the catch-all `upload-files`
  category. Don't "fix" that by guessing from titles.
- **The NRB catalog's identity is a DECODED url, twice over.** Phase 4 persists
  the corpus in `nrb_sources` / `nrb_files` / `nrb_source_files` / `nrb_sync_runs`
  (`app/nrb/{models,records,catalog,discovery,sync}.py`, run by
  `scripts/nrb_sync.py`; **nothing is downloaded** — Phase 5 owns that).
  `nrb_files.comparison_key` is Phase 3's `attachments.comparison_key`;
  `nrb_sources.url_key` is that same function plus a trailing-slash strip, and it
  exists because the **sitemap percent-encodes Devanagari slugs while REST returns
  them literally** — matching raw strings makes all ~18,370 REST documents look
  absent from the sitemap and inserts each a second time as a `sitemap_only` row.
  `page_url`/`source_url` keep NRB's own spelling (that is what a fetcher must
  request); the `*_key` columns are only ever compared. Four more things a rewrite
  must not lose: (1) **`sitemap_only` rows are only created when the REST pass was
  complete** (`Discovery.rest_complete`) — on `--limit 300` the "gap" would name
  18,267 URLs REST serves fine; (2) **a known REST source is never downgraded to
  `sitemap_only`** — one post type dropping out of REST would otherwise strip the
  attachments off 5,400 sources while the run still called itself clean;
  (3) **`metadata_hash` excludes `sitemap_lastmod`** (Yoast derives it from
  `post_modified`, which IS hashed) and carries only the attachment
  `comparison_key`s, not their MIME/size — a file edit is `files_updated`, not a
  source update, and double-counting would break the second-run-is-zero
  invariant; (4) `published_at`/`modified_at` are parsed with the offset **derived
  per post from `date` − `date_gmt`**, never an assumed +05:45, because `modified`
  has no GMT twin and the raw strings are kept in `metadata` for audit.
- **Absence-based deactivation is gated three ways, and it is the most dangerous
  statement in the sync.** `deactivate_unseen` is one unqualified `UPDATE
  nrb_sources … WHERE last_sync_run_id <> :run` (that predicate is why it is not an
  18k-element `NOT IN`). It runs only when `Discovery.complete` (no fetch error, no
  truncating bound, sitemap read) **and** the run saw ≥90% of the known active
  corpus (`SHRINK_FLOOR`, only applied at ≥100 sources), and
  `ck_nrb_sync_runs_deactivation_needs_complete` makes the illegal combination
  unrecordable. Rows are never hard-deleted; `is_active=false` + `deactivated_at`
  is the only withdrawal path, and `first_seen_at` survives reactivation. A
  removed attachment deletes the **relationship** only — `nrb_files` rows are kept
  (`ON DELETE RESTRICT`), because another source may reference the same file (42
  duplicate references measured) and Phase 5 may already have fetched it.
  **Consequence for tests:** the catalog is global with no department to scope a
  fixture to, so `tests/test_nrb_sync_integration.py` runs every test inside a
  rolled-back transaction (`join_transaction_mode="create_savepoint"`) and clears
  the nrb_* tables *inside* it. A test that really committed would deactivate a
  developer's whole catalog.
- **A downloaded NRB file is only trusted if the BYTES agree with NRB's claim.**
  Phase 5 (`app/nrb/{sniff,filestore,fetch,locks}.py`, `scripts/nrb_fetch.py`)
  downloads files; it still parses nothing. **WordPress answers a missing file with
  a 200 and a themed ~100 KB HTML page**, so `sniff.py` types every body from magic
  bytes and a `web` body where a document was promised is recorded `failed` with
  nothing stored — a navigation menu indexed as the text of a circular is far worse
  than a recorded gap. Same for a `Content-Length` that disagrees with the body (a
  truncated PDF still parses). A *non*-HTML disagreement (claimed PDF, bytes are a
  spreadsheet) is **stored** with both values kept, because Phase 6 decides what it
  can parse; an unsniffable body is kept too. Storage is content-addressed —
  `<sha256[:2]>/<sha256>.<ext>` under `NRB_FILES_DIR` — so a blob verifies against
  its own filename, identical bytes republished under two URLs occupy one file (a
  duplicate turned up in the **first 25** live files), and no Devanagari name or
  `..` reaches the filesystem. Writes go to `.incoming/*.part` then `os.replace`,
  because the final name is not known until the body has been hashed. `sniff.py` is
  stdlib-only on purpose (libmagic must not enter the API image) and states its two
  limits: OLE2 is a *family* not a format, and a ZIP's flavour comes from its first
  4 KB.
- **`scripts/nrb_fetch.py` refuses to run without a scope, and the fetch is
  resumable rather than idempotent.** The corpus is 18,263 files / **8.6 GB**
  (`--core` = 1,804 / 1.5 GB), so a slice must be named — same convention as Phase
  3's `--all`. Selection is `fetch_status='pending'` in id order, committed every 25
  files, so a repeat pass takes the *next* files, not the last ones, and an
  interrupted pass keeps its progress; an exhausted scope selects 0. `blocked_host`
  rows (the three `uat.nrb.org.np` links) are unselectable **by construction** —
  the status list only ever holds `pending`/`failed`. `--dry-run` here makes **no
  HTTP request at all**, unlike the sync's dry run which does the work and rolls it
  back: a rolled-back download would still have pulled the bytes. `app/nrb/locks.py`
  holds the advisory-lock rule for both commands (distinct keys, `NRB_SYNC` /
  `NRB_FTCH`, each on a dedicated connection because an `AsyncSession` releases its
  connection — and the lock — at every commit).
- **Producing Devanagari is NOT succeeding — the Phase 6B guards run on the
  INPUT.** Measured 2026-08-15: the benchmark's known English table
  `Instruments Times Offer Amount` run through a Preeti converter becomes
  `mक्ष्लकतचगभलतक mत्ष्भक इााभच mब्यगलत` — **91% Devanagari**, `legacy_line_ratio`
  0.2632 → **0.0**, character count preserved. Every after-the-fact success signal
  fires on a destroyed table, so validating the output cannot work; `lexicon.
  is_confidently_english` vetoes the raw line first. Three more things a rewrite
  must not lose: (1) **`devanagari_ratio` is anti-correlated with correctness for
  mapping choice** — `@)^%` is `२०६५` under Preeti and nonsense `द्दण्टछ` under
  FONTASY, and the WRONG one scores higher (0.9808 vs 0.9796); (2) the converter is
  **not a no-op on correct Devanagari** (it turns `(मनी लाउन्डररङ)` into `९मनी
  लाउन्डररङ०` while raising the ratio), so the Unicode guard runs before the English
  one; (3) **`|` is a Preeti codepoint mapping to `्र`** and `extraction.py` joins
  cells with `" | "`, so spreadsheets convert per CELL — and **ASCII digits map to
  Devanagari digits** (`1,234.00` → `ज्ञ,द्दघद्ध।ण्ण्`, which passes every validation
  rule), so an unjudged unit must clear `quality.LEGACY_MIN_LATIN` first.
  `UNJUDGED_MIN_LEGACY_RATIO = 0.80` (6A's own top band) gates both the
  too-short-to-judge branch and ambiguous replacement; without it 5 of 7 English
  controls lost lines. Never lower it to raise recall — see `docs/nrb-integration.md`
  §12.2.
- **`native-2` fixed the detector; it did NOT move the threshold.** The version
  selects the CLASSIFIER only — same parser, same text, same `quality` metrics, so
  `native-1` rows stay reproducible and both sit in `nrb_extractions` side by side
  (identity is `(content_sha256, extractor_version)`). The English-table defect was
  located before it was fixed: over 355 flagged lines in the seven known tables the
  **intra-word-symbol rule fired on 89.3%**, the vowel-less rule on 2.5%. So three
  narrow signal corrections in `app/nrb/units.py` — symbols only in letter-bearing
  tokens, compounds (`FIU-Nepal`, `F/Y`) exempt, acronyms (`NRB`, `SLF`) out of the
  vowel test — and `legacy_line_ratio >= 0.20` **untouched**. Four things a rewrite
  must not lose: (1) units are three-state, and `unjudged` (blank/numeric/too-short)
  is in NEITHER half of the ratio — that dilution is how 57 Preeti lines hid in
  `84862ab6866a`; (2) that shrunken denominator needs `MIN_JUDGED_FOR_RATIO = 8` (or
  `MIN_LEGACY_ABSOLUTE = 4`), or a document flags on 1 legacy unit out of 3 —
  measured, six of them; (3) spreadsheets are judged per CELL because `|` is a
  Preeti codepoint, and structure no longer short-circuits the linguistic rules;
  (4) the minority-region rule needs all three of ≥10 units, run ≥3 and ≥50% of
  CONTESTED units (neither Unicode nor English) — and an `unjudged` unit must not
  break a run. `routing.py`/`units.py` import nothing from `legacy_font`: the
  converter is GPL-3, and a classifier that needed it would drag the licence gate
  into every deployment.
- **The Phase 6B holdout is SPENT EVIDENCE — never tune against it.** Every number
  in §14 is worth having only because `docs/nrb/phase6b-routing-holdout.json`
  (`6344e674…`) contains no file that shaped native-2. Independence is enforced by
  the sampler, not by hand: `sampling.stratified_sample(exclude_keys=…)` drops
  candidates **before stratification** (so they never touch allocation or strata)
  and hashes the excluded SET into the parameters as `exclude_keys_sha256` — a
  count alone would let someone swap which 400 keys were withheld. Two consequences
  a rewrite must not lose: (1) **an un-excluded draw records no exclusion key at
  all**, which is why Phase 6A's `1ae297db…` still re-verifies byte-identically;
  (2) **the moment the classifier changes in response to a holdout finding, that
  holdout becomes development evidence** — the change needs a new extractor version
  (`native-3`) and a NEW cohort. That is exactly why §14.3's English-accounting-
  template false positive (`Profit & Loss A/c`, `5.2.Pension & Gratuity Fund` — 14
  of 14 flagged units readable English, in four copies of one NRB template) is
  written down and left unfixed. It sits at unit ratio 0.48–0.54, and the
  conversion gate is 0.80, so it is a caveat rather than a blocker.
- **The conversion gate is `unit_legacy_ratio`, NOT `legacy_line_ratio`.** They are
  different quantities and the difference is the whole point of native-2: the three
  large research workbooks the holdout caught sit at unit ratio 0.969–0.993 while
  their `legacy_line_ratio` is 0.15–0.19, i.e. native-1 called them clean.
  Substituting the line metric in a future conversion router would route a
  different population and silently undo §13.4.
  `test_the_conversion_gate_reads_the_unit_metric_not_the_line_metric` locks it.
- **A flagged unit has no location until you re-parse for one, and both obvious
  ways of recovering it are wrong.** `nrb_extractions` persists no text (300-char
  `preview` cap) and the text native-2 scored is flat, so
  `scripts/nrb_holdout_evidence.py` re-parses each blob and **verifies the
  reconstruction against the stored `unit_total`** before publishing a coordinate.
  (1) **`str.splitlines()` is not the inverse of `"\n".join(pages)`** — it also
  breaks on form feeds and lone `\r`, which nine holdout PDFs contain, so counting
  lines per page drifts and a page ending in `\f` yields a line belonging to
  neither page; lines are recovered with character OFFSETS (`_LINE_BOUNDARY`, the
  exact boundary set) and mapped back to a page, asserted equal to
  `text.splitlines()`. (2) **A cell boundary comes from the workbook, never from
  splitting the rendered row on `" | "`** — that inverse only holds while no cell
  contains the sequence; origins are openpyxl's `min_row`/`min_column`, because
  `iter_rows()` starts at the first populated cell, not A1. Same file: **a false
  positive is a document that was ROUTED** whose flagged units are English — a
  *clean* document containing English-looking units was never routed and belongs in
  the false-negative section, and conflating the two reports correct calls as
  mistakes (it briefly did).
- **Font provenance NARROWS the conversion route; it never widens it.** Phase 6B
  Task 4 (`app/nrb/recovery.py`) routes per PDF PAGE, and the order of the two
  questions is the whole design: eligibility is still native-2's
  `status=suspicious/legacy_font_suspected` **and** `unit_legacy_ratio >= 0.80`,
  and only INSIDE an eligible document does provenance choose between the
  converter and OCR. A page that embeds Preeti inside a below-gate document is
  *not* converted — that would widen npttf2utf eligibility on font presence
  alone. Five things a rewrite must not lose: (1) **a stripped font name is not a
  scan** — `7820b1f49fc1`'s producer emitted `/CIDFont+F1…F6` and converts
  correctly, so eligibility reads embedded font OBJECTS and recognised names are
  supporting evidence only (they also catch a page that NAMES Preeti without
  embedding it); (2) `scan_backed` is "no font of its own AND pixels", never "has
  an image" — `268bcfe86d03` is an embedded-Preeti circular with a logo;
  (3) **a page routed to OCR is never handed to npttf2utf** — its hidden text
  layer is a scanner's latin guess, and the converter would turn it into fluent
  nonsense that passes every validation rule (§12.2 measured that on an English
  table), so OCR failure yields EMPTY text + `ok=False`, never the junk layer —
  and symmetrically **a conversion that does not succeed withholds its INPUT**
  (`recovery._withhold`): a missing npttf2utf (the GPL-3 gate), a broken backend
  and a rejected unit all end `ok=False`/blanked, never the glyph-mapped original
  published as recovered text, and such a page is NOT re-routed to OCR (v5 is
  worse than conversion on embedded-font pages). Withholding is per UNIT and
  lives in `recovery`, NOT in `legacy_convert` — that module's byte-exact
  reconstruction is what its negative controls assert on. `PageText.indexable`
  is the one question an ingestion boundary should ask;
  (4) the unjudged-unit gate uses the DOCUMENT's `unit_legacy_ratio`, not a
  per-page recomputation (`nrb_holdout_validate._doc_ratio`); (5) pages are
  re-read with `read_pdf_pages`, never recovered by splitting `result.text` —
  `splitlines()` is not the inverse of `"\n".join(pages)`. `CONVERSION_GATE` and
  `legacy_convert.UNJUDGED_MIN_LEGACY_RATIO` are both 0.80 and deliberately two
  constants; they decide different things.
- **PP-OCRv5 is retrieval text, not a transcription, and the BACKEND picks the
  model.** docling reaches PP-OCRv4 through torch and **PP-OCRv5 only through
  onnxruntime**, so `RapidOcrOptions(backend=...)` is load-bearing, not a
  preference — v4 is rejected for Nepali (no conjuncts, visual order). On a
  150 dpi scan v5 still drops letterheads, subject lines and whole paragraphs and
  mangles latin runs (`lc_visakhapatnam@nrb.org.np` → noise), so OCR output must
  never be treated as authoritative for a figure, date or contact detail — every
  OCR page records `authoritative: false`. There is deliberately no confidence
  score: the spike measured orthographic well-formedness, which is not a
  per-field correctness estimate. Conversion still BEATS OCR where a font is
  embedded (v5 renders `कारवाही` as `शदक`), which is why OCR is the narrow
  fallback and not the default. `rapidocr`+`onnxruntime` live in
  `requirements-worker.txt` only — `Dockerfile` installs `requirements.txt`
  alone, so the API image cannot acquire an OCR stack by accident.
- **Every way an NRB deployment breaks looks like a clean deployment.** Five
  defects found by actually running the images (§18, 2026-08-16) — a CWD-relative
  `LEXICON_PATH`, the lexicon absent from the worker image, `npttf2utf` absent
  from it, RapidOCR's model dir root-owned against a uid-10001 worker, and
  docling's layout model calling `torch.compile` with no C++ compiler in the slim
  runtime — produce the **same** outcome: pages recorded
  `conversion_unavailable`/`needs_ocr`, input withheld, job **succeeded**,
  corpus quietly a quarter ingested. The fail-closed rule holds throughout, so
  none of them can emit bad text; that is exactly why none of them are visible
  from job status. **Verify a worker image by its route split on a known blob,
  never by whether ingestion succeeded.** `TORCHDYNAMO_DISABLE=1` and the
  `chown` of `site-packages/rapidocr` are in `Dockerfile.worker` for this;
  `npttf2utf` is the opt-in `INSTALL_LEGACY_FONT` build ARG, default false,
  because it is GPL-3 and a default build must stay distributable.
- **`docker-compose.p4.yml` is the NRB overlay, and `migrate` is the reason it
  exists.** The base stack reads `.env.docker`, whose `DATABASE_URL` names the
  REAL database, and `migrate` runs `alembic upgrade head` against whatever it is
  handed. The overlay repoints **all three** services at `.env.docker.p4` so they
  cannot disagree about which database they mean, and flips the GPL flag on.
  Never bring the base stack up for NRB work.
- **"Is the NRB pipeline idempotent" has a different answer at every stage, and
  `nrb_extractions` is NOT an input to ingestion.** Sync is all-zero on a second
  run; fetch selects `fetch_status='pending'` only (excluded by the status
  column, not a `WHERE` someone can forget); extract selects blobs with no row at
  this `extractor_version` (`catalog.py:1059-1064`). Recovery **used to** re-run
  on every ingest; since §21 it is cached (see the next bullet). Running
  `nrb_extract.py` is still *not* a prerequisite for ingesting; the two paths
  agree because they run the same code, not because one consults the other.
  **Nothing is scheduled** — no cron, no timer, no in-process scheduler; stages
  1–3 are manual CLI passes and the only daemon is `app.rag.worker` polling
  `ingest_jobs`. Of §19.3's three gaps, two are now closed —
  `scripts/nrb_rag_ingest_corpus.py` (§20) and the recovery cache (§21) — and
  **the supersession link is not**: a republished NRB file still mints a SECOND
  `documents` row with nothing archiving the first (`metadata.blob_sha256` is
  written but never read back).
- **The recovery cache has TWO version domains, and collapsing them is the
  mistake it exists to avoid.** `nrb_recoveries.base_version` is the ROUTING
  identity (`native-2|recovery-1|prov-1|gate=0.8|unjudged=0.8` — the classifier,
  `recovery.RECOVERY_ROUTING_VERSION`, `provenance.PAGE_PROVENANCE_VERSION` and
  both gate constants, **read live** so editing a gate cannot be forgotten); a
  change there invalidates the whole document, because the routes themselves may
  now differ. `nrb_recovery_units.engine_version` is per unit and depends on the
  route (npttf2utf+mapping+lexicon fingerprint; PP-OCR model+backend+package
  versions; `passthrough/<extractor_version>`), so an OCR bump re-runs OCR pages
  only and a converter bump re-runs conversions only — measured on
  `e08988860534`: 1 of 50 pages, then 49 of 50, never both. **Never key it on
  `extractor_version` alone** (a converter upgrade would serve stale text) and
  never on one combined string (an OCR bump would re-run the whole corpus).
  An ABSENT dependency renders `unavailable`, which is a version like any other
  — that is what makes fail-closed and selectivity the same mechanism, and it is
  why installing npttf2utf invalidates exactly the pages it could not do. Three
  invariants a rewrite must not lose: only post-`_withhold` text is ever stored
  (the glyph-mapped original is not in the database and cannot be resurrected);
  `indexable` is **recomputed** from `(ok, text)`, never a column; and unresolved
  units are cached WITH their reason, or every withheld page re-runs OCR forever.
  The unit is whatever `recovery.py` returns — PDF page, spreadsheet SHEET (never
  a fake page number), or unit 1 — and non-PDF documents are all-or-nothing
  because their units share one route. `recovery.py` stays the semantic owner:
  a stale unit is refreshed by `recovery.convert_unit`/`ocr_unit`, the same
  functions a cold run calls. Superseded versions are kept side by side like
  native-1/native-2; `--purge` is the only removal and it is an operator command.
- **The corpus ingest driver skips by ANTI-JOIN and conflicts mean RACED.**
  `app/nrb/corpus.py` anti-joins the scope against `documents.content_hash` in
  the target department — the same number as `nrb_files.content_sha256`, both
  `sha256(bytes)`, asserted per file rather than assumed — so a repeat pass
  selects nothing in one query. `DocumentConflict` is still caught, but a nonzero
  count means **concurrency, not idempotence**, and the two are reported
  separately. The anti-join repeats `ux_documents_active_content`'s own
  `status <> 'archived'` predicate, because archiving must stay reversible;
  the side effect is that a **`failed` document is never re-selected** by the
  ordinary pass; `--retry-failed` (§21.1) is the only way past it, and it is a
  SEPARATE pair of functions that creates no document and copies no file — three
  exclusions (`status='failed'`, no active job, and a join to `nrb_files` so it
  cannot adopt a non-NRB upload) are each load-bearing. It is **enqueue-only by design** — draining
  in-process races the deployed worker, and `SKIP LOCKED` makes them split the
  scope rather than collide. And it must never learn to read `nrb_extractions`:
  `test_the_driver_never_consults_the_extraction_evidence_table` checks the
  module's AST (not its text, which explains the rule at length).
- **`app/nrb/catalog.py` uses Core statements, never `update(Model)`, and that is
  load-bearing.** `nrb_sources` maps the attribute `meta` onto the column named
  `metadata` (declarative reserves `metadata`). A Core insert wants the key
  `metadata`; an ORM bulk update wants `meta` — and handed `metadata` it **drops
  the key silently and leaves the column unchanged** (measured, no error). Core
  everywhere means one vocabulary; updates are
  `Table.update().where(id == bindparam("_id"))` executemany.
- **Dates come from the server clock, never from the model.** `app/localtime.py`
  is the one source of "today" (Nepal time as a literal **UTC+05:45** offset —
  `ZoneInfo` needs system tzdata the slim images don't install, and Nepal has no
  DST). Two consumers: `build_system_prompt`'s `DATE_PROMPT` states today's date
  and forbids answering time-varying figures from memory, and `get_nrb_forex`
  defaults an absent/blank `from` to today (**nothing is `required`** in its
  schema). Both exist because of one live failure: with no date in context the
  model answered "USD to NPR" with 2023's 132.57/133.17 as current. Requiring a
  date made it *supply* a stale one, and NRB answers for 2023 quite happily, so
  the result looked right. Don't reintroduce `required: ["from"]`, and don't
  derive today from UTC — after 18:15 UTC that's yesterday in Kathmandu.
- MCP: gateway is the MCP client (streamable HTTP). Set `MCP_SERVER_URL` to enable;
  blank = agent runs with local tools only. `mcp` SDK v2: fn is `streamable_http_client`,
  tool field is `input_schema`.
- File downloads are behind JWT — the frontend must fetch with the Bearer header
  and make a blob URL (an `<a href>` can't send the header). Files are **per-user**
  now: every generated file gets a `generated_files` row owned by the caller;
  `GET /v1/files/{id}` 404s unless you own it, `GET /v1/files` lists your files.
- **File-sink/source contextvar gotcha:** tools call `await file_store.save(...)`
  (write) / `await resolve_file(id)` (read) but never see the user; the chat
  router installs BOTH via `turn_files(user_id, session_id)` (= `file_sink(
  PostgresFileSink)` + `file_source(PostgresFileSource)`) for the turn. For
  streaming they MUST be set *inside* the async generator Starlette iterates
  (done in `chat/router.py`), else they're invisible while the loop runs (writes
  fall back to the unowned in-memory store; reads can't find owned files). A
  new file-producing tool needs nothing here — just `await file_store.save(...)`;
  a new file-reading tool just `await resolve_file(id)` (None ⇒ not owned/unknown).
- **Excel/CSV upload + read:** `POST /v1/files` ingests .xlsx/.csv (uuid on-disk
  name under the user folder, `source=uploaded`; guards: size cap→413, ext
  allowlist + xlsx zip-bomb + parse-check→400). `app/files/readers.py` normalizes
  both formats to a capped `Table` (**opens xlsx `data_only=True` — formulas are
  NEVER evaluated**; row/char caps bound context). Tools `inspect_excel` (every
  sheet's structure) + `read_excel` (one sheet, paged/projected, truncation tells
  the model how to page; multi-sheet-with-no-`sheet` reads the first AND names
  the others). Attach with `file_ids` on `/v1/chat`: `open_turn` verifies
  ownership (404 on foreign id), persists `{id,filename,summary}` on the user
  message (`chat_messages.attachments` JSONB), and `build_context_messages`
  re-emits the attachment note on later turns so ids survive without resending.
- **Image OCR is `read_image`, it is OPTIONAL, and it does NOT go through
  docling.** Full write-up in `docs/image-ocr.md`. `app/files/image_ocr.py` calls
  `rapidocr` directly — `RapidOCR.__call__` takes an image, so the docling path
  (`app/nrb/ocr.py`, which exists because ITS input is a PDF *page*) would add
  torch 1.1 G + transformers 93 M for nothing. What is reused is §16.6's model
  decision: PP-OCRv5 on onnxruntime, v4 rejected for Nepali. `rapidocr` +
  `onnxruntime` live in `requirements-ocr.txt`, installed only under
  `--build-arg INSTALL_OCR=true`; absent, `read_image` says "image OCR is not
  enabled on this deployment" and nothing else changes. Six things a rewrite must
  not lose: (1) **an omitted rapidocr param is not a default, it is Chinese
  PP-OCRv6** (`config.yaml` ships `lang_type: "ch"`, `ocr_version: "PP-OCRv6"`)
  and rapidocr *refuses* plain strings, so `ocr_config` declares all eight keys
  as strings — asserted in an environment WITHOUT rapidocr, where a silent
  fallback would go unnoticed — and `ocr_params` converts them to enums;
  (2) detection stays byte-identical to the measured NRB config (`ch`, v5,
  mobile) because a detector finds text BOXES not characters, so `lang` moves the
  recogniser only, and `devanagari` is the default because it reads English too
  (measured exact on `45,320.75`) while a latin-only model returns NOTHING for
  Nepali; (3) **rapidocr returns one box per WORD** — `Total Amount: 45,320.75`
  is three boxes on one row — so `group_lines` groups by geometry, never by the
  order rapidocr emitted, and a blank image returns `txts=None` not `()`;
  (4) `summarize_image` must **never** OCR (`history/service._resolve_attachments`
  re-summarizes every attached file on EVERY turn) — locked by an AST test;
  (5) the caveat lives in the HEADER because `agent/loop.py` cuts from the END,
  and confidence is reported but **never** compared to a threshold (§16.6 refuses
  to invent one) — locked by an AST test; (6) the API image can now *acquire* an
  OCR stack, so `tests/test_image_ocr_import_boundary.py` (a SUBPROCESS check)
  replaces "it isn't installed" as the guarantee that importing the app never
  loads rapidocr/onnxruntime/cv2. The Dockerfile's three OCR steps (native
  OpenCV libs, model pre-warm, `chown` of `site-packages/rapidocr`) are each a
  §18-class defect if skipped: the stack is present, the call "succeeds", and no
  text ever comes out.
- **Uploaded images have a PIXEL cap, and Pillow is not optional.** A ~200-byte
  PNG can declare 40000×40000 and sail past both the 10 MB wire cap and
  `router.py`'s zip guard (which only covers the OOXML paths).
  `images.MAX_IMAGE_PIXELS` is checked BEFORE any decode and does not rely on
  Pillow's own exception — Pillow only *raises* above 2× its limit and merely
  warns between 1× and 2×, so a 1.5× bomb would pass. `images._KINDS` doubles as
  a **decoder allowlist on the sniffed format**, so a GIF renamed `.png` never
  reaches the GIF decoder. Both are security controls on the upload path, so
  `Pillow` is in `requirements.txt` (previously an undeclared transitive dep) and
  uploads keep working with the OCR stack absent. Same file, different trap:
  **a multi-frame `.tif` loses pages silently** — a scanner's normal output, and
  the engine reads frame 1 only (measured: page 2's text just vanished) — so
  `ImageSummary.frames` is a reported fact and `read_image` emits a `PARTIAL:`
  line, exactly as `read_document` reports `pages_skipped`.
- **`read_document` reads ONE attached .pdf/.docx/.txt/.md/.json** by `file_id`
  (spreadsheets 400 with a pointer to `inspect_excel`/`read_excel`).
  `app/files/documents.py` normalizes every format to flat lines (`documents.py`
  is pure — no DB/HTTP — shared by this tool AND the upload route's summary via
  `app/files/ingest.py`'s extension→family dispatch); a PDF's page boundaries
  appear as `[page N]` marker lines inside that same line stream, so there's
  only one paging unit. Two deliberate departures from `read_excel`, both about
  truncation honesty: **metadata leads** (the header — total lines, and if
  truncated the `start_line=` to resume from — is put FIRST, because
  `agent/loop.py` cuts an oversized tool result from the END, which is exactly
  where `read_excel` puts its own continuation note); and **truncation is on
  WHOLE lines, done by the tool before the loop ever sees the result** — a line
  that would cross the budget is dropped entirely rather than sliced, so the
  promised resume point is exactly where the model's view actually stopped, not
  mid-line. The scanned-PDF seam: `documents.py` reports facts only (a scanned
  page comes back with `text_pages == 0`, no exception); the tool is what turns
  "a PDF with pages but zero pages of text" into a distinct
  `ERROR: ... OCR is not available yet` — separate from the ordinary per-page
  `(no extractable text — likely a scanned image)` marker a MIXED scan/text PDF
  gets, so a fully-scanned file and a mostly-readable one with a couple of
  scanned pages read differently to the model. Locked by
  `tests/test_document_eval.py` (8 deterministic cases, target 8/8) and the
  routing-description cross-reference test in `tests/test_excel_read_tools.py`.
- **Exactly ONE attachment set is active** — the newest. `build_context_messages`
  replays older sets with superseded wording and no summary; a turn that carries
  its own upload passes `pending_attachments=True` so every replayed set is
  demoted and only `open_turn`'s appended note is active. Identical notes per
  upload made a second file get ignored in favour of the first.
- **The attachment note is a `user` message, and that is load-bearing.**
  Measured against `qwen3.5:35b-a3b` with the 16 tool schemas loaded, the same
  note produced a tool call **3/12** times as `system` vs **12/12** as `user`.
  The model *reads* a system note fine (asked directly, it returns the id every
  time) but won't ACT on it once tools are in play — it asks the user for a
  file id it was already handed, which reads as "the assistant ignored my PDF".
  Stronger imperative wording barely helped (2/6); only the role did. Both
  emitters (`service.open_turn`, `context.build_context_messages`) use
  `user`; `test_attachment_note_is_a_user_message_not_a_system_one` locks it.
  Related, and the reason this was easy to get wrong: the agent's
  `SYSTEM_PROMPT` is now **always** message 0 — the old
  `base_messages[0].role != "system"` guard silently dropped it for any session
  that began with a file upload, back when the note was a system message.
- **Chat history is paginated by KEYSET, and the model's context is budgeted
  separately from either read.** Three loads used to be unbounded; two of them
  shared one repository function, so a `LIMIT` added for the UI would have
  silently truncated the model's prompt. They are now three bounded reads:
  `list_sessions_page` (keyset on `(updated_at DESC, id DESC)` — offset paging
  duplicates and skips, because every turn bumps `updated_at` and rows move
  between pages mid-scroll), `get_thread_page` (keyset on `seq`, newest page
  selected, ascending order returned), and `get_context_tail` (newest N,
  selecting only `seq/role/content/attachments` — `trace` and `sources` are
  never in a prompt and were most of the cost). `get_session_with_messages` and
  `list_sessions` are DELETED, and a test asserts they stay gone.
  `app/history/context.py` owns the budget and is pure — no DB, no model — for
  the `app/rag/ranking.py` reason: the rule deciding what the model sees should
  be provable without a database or a GPU. Five things a rewrite must not lose:
  (1) **the estimator is SCRIPT-AWARE** — `len/4` is an English ratio, and on
  this mixed Nepali/English corpus it under-counts Devanagari into the very
  overflow the budget prevents; the constants are pessimistic on purpose,
  because over-estimating wastes window while under-estimating lets Ollama drop
  the FRONT of the prompt, where the identity and date system prompt lives;
  (2) **selection keeps whole turns**, so no dangling assistant reply can head
  the context as if it had spoken unprompted; (3) **the active attachment set is
  PINNED** when its own turn falls outside the budget — losing the file ids
  makes the model ask for an id it was handed, the failure
  `test_attachment_note_is_a_user_message_not_a_system_one` exists for — and a
  pinned set demotes every surviving one, because two active sets leave the
  model guessing; (4) **dropped turns announce themselves** via
  `TRUNCATION_NOTE`, for the same reason `_for_model` appends `[TRUNCATED …]`:
  a bare cut of history reads as a COMPLETE conversation, so the model denies
  being told what it was told — and the note is absent when nothing was
  dropped, because an always-present warning trains the model to ignore it;
  (5) **`CONTEXT_WINDOW_TOKENS` is a second copy of `OLLAMA_CONTEXT_LENGTH`**
  that this process cannot read back (no `num_ctx` field, no reported window),
  so a mismatch is silent and every symptom looks like a healthy turn —
  `service._log_budget` logging the estimate is the only place it becomes
  visible, and it is the dataset the estimator is calibrated against.
- **`aggregate_excel` is the correct tool for ANY total** — sum/avg/min/max/count
  with an optional one-level `group_by` and AND-only filters, computed over
  EVERY row via `readers.open_sheet_rows` (uncapped streaming context manager,
  distinct from the ~200-row `load_table`). `read_excel`'s cap makes model-side
  arithmetic silently wrong on a bigger sheet; this removes that. Engine is
  `app/files/aggregate.py` (pure), numbers come from `app/files/numeric.py`
  (currency/commas/percent/accounting negatives → **Decimal**, never eval,
  rejects `"nan"`/`"Infinity"` which `Decimal()` would otherwise accept). Each
  cell is blank (absent), parsed, or unparseable (excluded but **counted and
  named** in the footer); a column where nothing parsed returns None, never 0.
  Caps: `MAX_SCAN_ROWS=200_000` (states a PARTIAL result rather than refusing),
  `MAX_GROUPS=50` (reports the true group total).
- **Assistant identity is deployment config**, not a constant: `build_system_prompt(
  settings)` in `agent/loop.py` prepends an identity block driven by
  `ASSISTANT_NAME`/`ASSISTANT_ORG` (defaults stay generic; NIC Bank sets them in
  `.env`). It exists because the model otherwise says "I am Qwen" — and because a
  model merely renamed will invent a training story, so the prompt explicitly
  forbids claiming the org trained it. **Branding, not a security boundary:** the
  real model id is still in the `/v1/chat` body and elicitable by prompting.
- **Tool descriptions are the routing prompt** — `inspect_excel` is read first,
  so it routes by question type (totals → `aggregate_excel`, rows →
  `read_excel`); `read_excel` names `aggregate_excel` in its cap warning. Locked
  by `test_descriptions_route_totals_to_aggregate_excel`. Don't drop those
  cross-references: without them the model commits to the capped path and totals
  come out wrong.
- **Truncation must announce itself.** Tool results over `MAX_TOOL_RESULT_CHARS`
  (8000) go through `_for_model` in `agent/loop.py`, which appends a `[TRUNCATED
  …]` note — a bare cut reads to the model as a complete result. Same reason the
  repeat-nudge only quotes a cached result back when the quote IS the whole
  result (`call_cache` holds the full text; the trace keeps the 600-char copy).
- **The trace is persisted always, exposed conditionally.** `EXPOSE_TRACE`
  (default true) gates whether the execution trace leaves the gateway:
  `/v1/chat` (JSON body AND the streamed `done` event) and `/v1/sessions/{id}`
  all send null when it's off, so a production UI has nothing to draw a "how it
  worked" panel from. `chat_messages.trace` is written either way — it's the
  audit record, not a display field. Live `tool_call`/`tool_result` stream
  events are deliberately NOT gated (that's the in-flight "using tool X"
  indicator). Both turn paths also run the trace through `_trace_if_tools`, so a
  tool-free turn sends null: the loop's raw trace has one entry per iteration
  even with zero tool calls, and streaming used to leak that as
  "1 iteration · 0 tool calls" on an ordinary answer.
- **Starlette 1.x gotcha:** `include_router` mounts as a lazy `_IncludedRouter`,
  so `app.routes` won't list child routes as `APIRoute`. Verify routes via
  TestClient or `/openapi.json`, not `isinstance` checks.
- **A department grant carries a LEVEL, and a global admin is not an owner.**
  `user_departments.role` is `viewer` < `editor` < `owner`
  (`ck_user_departments_role` closes the vocabulary, same rule as
  `ck_documents_status`), and every decision lives in the pure
  `app/rag/permissions.py`. Four things a rewrite must not lose: (1) **`allows()`
  takes a level while `grant_refusal()` takes `caller_is_global_admin`
  SEPARATELY** — a global admin is owner-equivalent for capabilities, but run them
  through the owner escalation rule and "an owner cannot grant owner" stops global
  admins creating owners too, at which point the feature is unusable and whoever
  debugs it deletes the guard (`test_a_global_admin_is_never_refused_a_membership_change`
  locks it); (2) **every gate fails closed** — `allows(None, …)` and
  `allows("<unknown>", …)` are both False, so a level that escaped the CHECK cannot
  compare as rank 0 and pass the viewer test; (3) `effective_level` takes the
  **MAXIMUM** of grant and admin-ness, so being granted a department never costs an
  admin a capability; (4) an owner delegates viewer/editor but **never `owner`**,
  and may neither demote nor evict an existing owner — that bounds the escalation
  chain at depth 1 and needs no last-owner guard, because global admin is always
  the backstop (unlike `LAST_ADMIN` in `app/users/policy.py`, which has none).
  `access.effective_department_level` is the ONE place a level is computed and
  `router._require_level` the ONE gate, ordering refusals **404** unknown/inactive
  → **403** no grant → **403** naming the level. Naming the level is safe only
  there: at document and ingest-job granularity the answer is **404**, because
  existence is the secret. The chat boundary admits **any** level — curation is
  what levels gate, while holding the grant at all is what "may ask a question
  here" means. `POST .../members` is also promote/demote, so
  `grant_department` upserts: `on_conflict_do_nothing` would report success while
  leaving the old level in place.
- **On `POST .../members`, an ABSENT `role` preserves the level; it does not mean
  viewer.** `GrantCreate.role` is `DepartmentRole | None = None`, and the upsert
  writes `role` only when it was supplied. Defaulting absence to `viewer` looks
  harmless and is not: the route upserts, so "field omitted" became
  indistinguishable from "set to viewer" and re-adding an existing owner **stripped
  them to viewer, answered 204, and overwrote `granted_at`** — measured, and missed
  by `test_granting_twice_is_idempotent` because that only exercises viewer→viewer
  (`test_omitting_role_on_a_RE_grant_does_not_demote` is the guard). A new grant
  with no `role` still lands on viewer, which is least privilege with no level to
  keep. Three related rules in the same neighbourhood: (1) **the read-then-write in
  `grant_member`/`revoke_member` is serialised by
  `repo.lock_department_for_members`** — without it a global admin promoting U to
  owner races a department owner demoting U, the owner's read sees the old level,
  the refusal is skipped and the fresh owner is overwritten; the lock is on the
  DEPARTMENT row, not the grant row, because the dangerous interleaving is two
  INSERTs and `FOR UPDATE` cannot hold a row that does not exist yet; (2) **an
  owner may act on their OWN row** (`grant_refusal(target_is_caller=True)`), or
  stepping down would need an admin — the parameter defaults to False so a caller
  that forgets it gets the strict answer; (3) **a deactivated account cannot be
  granted** (409) — the old admin-only user dropdown filtered them out and the
  email path cannot, so an offboarded account would return as a phantom member.
- **Membership survives soft-disable; the CORPUS does not.** The three member
  routes pass `require_active=False` to `_require_level`; every document route does
  not. Grants deliberately outlive `is_active=false`, so revoking a departing
  employee on a retired department must not require reactivating it — which would
  re-expose it as a live tab to every remaining member — revoking, then disabling
  it again. Raised independently by a code review and by the frontend author, after
  the first cut active-gated all of them. Accepted trade-off recorded at the same
  time: `_require_level` answers **404** for an unknown code and **403** for a real
  one, so an authenticated user can probe which department codes exist. That is the
  existing documented contract on every department-scoped route ("403 ungranted
  department") and the frontend branches on it; codes are org unit names, while the
  secret is the corpus, which stays 404 at document granularity.
- **Department access is a database invariant, not a convention.** A chunk's
  `department_id` is held to its document's by the composite FK
  `(document_id, department_id) → documents(id, department_id)`, so
  `WHERE department_id = ?` is enforced by Postgres rather than by application
  code behaving correctly. `documents` carries the otherwise-redundant
  `UNIQUE (id, department_id)` purely as that FK's target — don't "clean it up".
- **The department is NEVER a tool argument.** `resolve_department` validates the
  request's tab code against `user_departments` and installs it via
  `rag_context`, exactly like `file_sink`/`file_source`. Same streaming rule: set
  it INSIDE the async generator Starlette iterates. Retrieval tools take no
  `department` parameter, so a prompt injection has nothing to target. Contract:
  404 unknown/inactive, 404 foreign session (ownership is re-checked, not assumed
  of the caller), 403 ungranted, 409 department mismatch, 409 **existing general
  session given a department**. Admins bypass the grant check ONLY. **Omitting
  `department` on a BOUND session is not an error** — it continues in its own
  department, read from `chat_sessions.department_id` (`access.py`'s `code is None`
  branch). An earlier version of this line claimed a 400 there; no such 400 exists
  in the code, and a frontend that believed it would send the code defensively on
  every turn and treat a legitimate omission as a bug.
- **`chat_session is None` (new) ≠ `chat_session.department_id is None`
  (existing general chat).** Both look like "no department". Collapsing them lets
  an existing general conversation be relabelled HR on turn five, misrepresenting
  every prior turn as departmentally grounded. New sessions may open in a
  department; existing general ones get a 409.
- **Department authorization stays in Postgres — no JWT claims, no auth cache.**
  A session is bound to one department and retrieval uses the server-side
  `chat_sessions.department_id`, never a value read back from the request body.
  Slice 3 folds the grant check into `open_turn`'s existing session query, so it
  costs **zero additional round trips**. Measured, `resolve_department` is
  0.518 ms against a multi-second turn, and the request is DB-bound anyway
  (`get_current_user` selects the user row every request). Token claims would buy
  that back for a revocation delay — up to 24h, since there is no refresh flow —
  which is the wrong trade in a bank. Don't reintroduce without building refresh
  first.
- **Departments are never deleted.** `documents.department_id` and
  `chat_sessions.department_id` are both `ON DELETE RESTRICT` — deleting a
  department must not silently rewrite an old HR session into a general one.
  `departments.is_active = false` is the only retirement path.
- **Discovery owns upstream FACTS; the fetch stage owns operational state.**
  `records.file_record` always builds a candidate with `fetch_status='pending'`
  (the constructor default), so comparing that column in
  `catalog.FileState.differs_from` made every fetched row look changed and wrote
  `pending` back over it — a re-sync would have re-downloaded 8.6 GB and
  overwritten every `content_sha256` (fixed 2026-08-17, §23.1). `differs_from`
  now compares facts only, an UPDATE writes `_file_facts`, and
  `fetch_status`/`blocked_reason`/`fetch_error` move ONLY via
  `FileState.fetch_transition`, which names the upstream field: became blocked
  (but a **`fetched`** row is left alone — `ck_nrb_files_blocked_reason` makes
  "fetched + reason" unrepresentable and `select_ingest_targets` needs
  `fetched`), became unblocked, or **upstream replaced the bytes** (a different
  `filesize`/attachment id at the same `comparison_key` → `pending`, which is
  the §22 supersession TRIGGER). That last rule fires only when BOTH values are
  known — `None → 123456` is metadata arriving, not a new file — and never
  clears the content columns.
- **One orchestration path, and enqueueing is not finishing.** `pipeline.start`
  is what the CLI/API/cron all call; it takes `PIPELINE_LOCK_KEY` (advisory, so
  a crash frees it), sweeps any run left `running` — safe with **no timeout**
  because holding the lock proves no orchestrator is alive — runs the stage
  services in order, and records WHICH jobs it queued in
  `nrb_pipeline_run_jobs`. That relation is explicit because a time-window query
  would adopt the scratch DB's 190 unrelated `ri*` jobs. A run that queued work
  ends `awaiting_jobs`, NOT succeeded; `pipeline.reconcile` (callable from any
  process, after the orchestrator has exited) computes the terminal status, and
  **waiting beats every other signal**. A second trigger gets `PipelineBusy`
  carrying the active run rather than a duplicate — and `queued` (§26) as well as
  `awaiting_jobs` counts as ACTIVE (§24.3, reversing §23.5): the lock is released when orchestration
  returns while the jobs outlive it, so exclusion is the durable ROW, checked
  under the lock after `sweep_abandoned` and `settle_waiting`. That second call
  is not optional — without it a run whose jobs finished but which nobody polled
  would block every future trigger forever. **A terminal run's job counts are
  FROZEN into `counters['jobs']`** when it leaves the active states, so later
  work on the same documents cannot rewrite finished history (§24.2).
  **`pipeline.recover_abandoned` must be called before looking for work**, by
  every process that can orchestrate: a run left `running` by a killed runner
  occupies the singleton active slot, so nothing can be accepted AND no queued
  run can appear to trigger `execute_run`'s own sweep — one crash would wedge the
  pipeline permanently (§26.6). `retry_failed` defaults False
  and is NOT a recovery refresh — purging cached unresolved recoveries is a
  separate explicit command.
- **A failed replacement must never remove the last good version, and one
  transaction is what guarantees it.** NRB republishes; new bytes are a new
  `content_hash`, so `ux_documents_active_content` is perfectly happy with two
  versions and cannot express "which one is current". `ux_documents_nrb_current_source`
  — a PARTIAL UNIQUE index over `(department_id, metadata->>'comparison_key')`
  where `status='ready' AND origin='nrb'` — is what says only one may be
  SEARCHABLE. The logical identity is **`comparison_key`** (the catalog's own
  unique attachment URL), never `content_sha256` (that is the VERSION), never
  `page_url` (a post can carry a circular AND its annex — promoting one would
  archive the other), and never a title/filename/date. `worker._activate`
  archives the old version and activates the new one in ONE transaction, with
  the archive FIRST (the index would refuse two `ready` rows otherwise), so any
  failure rolls the archive back too. Ordering is `documents.created_at` +
  `id` — OUR observation order, because `nrb_files` overwrites `content_sha256`
  in place and keeps no version history (§22.5); job completion order is never
  used, so a document supersedes only strictly OLDER siblings and archives
  ITSELF if a newer one is already `ready`. A newer success also archives an
  older `failed` row, which is why **a superseded failure is not retryable**.
  Supersession touches `documents` only: `nrb_files`, blobs, `nrb_recoveries`
  and `nrb_extractions` are evidence and are never purged, and an archived
  version's recovery stays cached.
- **Both RAG unique indexes are PARTIAL, deliberately.**
  `ux_documents_active_content` excludes `archived` rows, or archiving a document
  (which deletes its chunks but keeps the row for audit) would permanently block
  re-uploading that file. `ux_ingest_jobs_active_document` covers only
  `queued|running`, because `FOR UPDATE SKIP LOCKED` guards a single row and does
  nothing about two active jobs for one document. Both surface as 409, not 500.
- **The status CHECK constraints are load-bearing, not hygiene.** Both partial
  indexes key off exact strings, so a typo'd status (`'runnning'`) would match no
  predicate and silently escape `ux_ingest_jobs_active_document` entirely.
  `ck_documents_status`, `ck_documents_source` and `ck_ingest_jobs_status` close
  the vocabularies. Adding a status value means editing the CHECK too.
- **`documents.storage_key` is a RELATIVE key under `RAG_DOCS_DIR`**, not an
  absolute path (unlike `generated_files.path`). Rows stay portable across hosts
  and the same value becomes the object-storage key later.
- **`metadata` is reserved by SQLAlchemy declarative** — the attribute is `meta`,
  the column keeps the name (`mapped_column("metadata", JSONB, ...)`).
- **`tsv` uses `'english'`, not `'simple'`** — measured: English stems
  (`loans`→`loan`) while Devanagari passes through untouched, so a mixed
  Nepali/English corpus gains recall and loses nothing. Changing it rewrites the
  table (it's a STORED generated column).
- **The HNSW/GIN indexes are declared on the model AND hand-written in the
  migration**, and excluded from autogenerate comparison via `_include_object` in
  `alembic/env.py` — Alembic cannot reflect an HNSW opclass or its
  `WITH (m, ef_construction)` options, so without the exclusion every drift check
  proposes dropping and recreating them.
- **RAG integration tests build a throwaway `NullPool` engine per call**, not the
  app's module-level `engine`: that one pools connections bound to the first
  event loop, and each `asyncio.run` makes a new one — the second test would die
  with "Event loop is closed".
- **Ingestion runs in a SEPARATE process:** `.venv/bin/python -m app.rag.worker`.
  It shares the repo and database but not the dependency set — Docling pulls ~90
  packages including torch and the CUDA stack, which must never enter the API
  image. `requirements-worker.txt` = `-r requirements.txt` + docling. Docling is
  imported INSIDE `parsing._parse_with_docling`, never at module scope, and
  `test_docling_is_not_imported_at_module_scope` (a SUBPROCESS check, because
  `sys.modules` is process-global) locks that.
- **The API never parses or embeds.** Upload writes a `documents` row + a queued
  `ingest_jobs` row and returns **202**. All slow work is the worker's.
- **The worker holds NO transaction while parsing/embedding.** Snapshot the doc
  (`DocSnapshot`), end the read, parse via `asyncio.to_thread` (Docling is sync
  and CPU-bound), embed, then ONE short atomic replacement. A background
  heartbeat task runs throughout so a long job isn't swept as stale, and is
  cancelled before the terminal status is written.
- **`worker.preflight` refuses to start on a dimension mismatch.** Finding out
  after half a corpus is inserted is far worse — `vector(1536)` would start
  rejecting inserts partway through.
- **A failed re-ingest of a `ready` document leaves it `ready`.** The
  replacement rolled back, so its previous chunks are intact and correct; only a
  document that never had a good version becomes `failed`. `replace_chunks` and
  `archive_document` both take `SELECT … FOR UPDATE` on the document row and
  re-check status under the lock, so an archive landing mid-ingest is not
  resurrected by the commit that follows.
- **`get_job`/`get_document`/`lock_document` re-read with `populate_existing`.**
  `claim_next`/`sweep_stale`/`replace_chunks` update via raw SQL the ORM can't
  synchronise, and sessions run `expire_on_commit=False`; a cached read would
  report a swept job as still running.
- **Upload compensates storage on failure.** The file is written before the DB
  work is known to succeed, so a duplicate-content 409 or a failed commit calls
  `storage.delete_document` — otherwise a rejected upload orphans a file.
- **Corpus spreadsheets are searchable but NOT aggregatable in v1.**
  `aggregate_excel` resolves through `resolve_file` → `generated_files` (per-user
  uploads); corpus documents live in `documents` under `RAG_DOCS_DIR`, so the
  resolvers are disjoint and it cannot reach them. Each corpus table chunk
  repeats its header row so a chunk retrieved alone is self-describing.
- **Qwen3-Embedding is asymmetric:** queries get an `Instruct:`/`Query:` prefix,
  documents do not. `embed_texts` requires an explicit `mode`; getting it wrong
  silently degrades retrieval. `/v1/embeddings` batch results are ordered by
  `index`, not array position — `embed_texts` re-sorts and validates the index
  set; don't "simplify" that away.
- **PDF/DOCX parsing preserves provenance** — `parsing._parse_with_docling`
  walks `iterate_items()` (real `prov[0].page_no`, heading path, element label)
  rather than dumping `export_to_markdown()`, and prepends the heading path to
  chunk *content* because `tsv` indexes content alone. Model prereq for a live
  run: `ollama pull qwen3-embedding:4b-q8_0` (not pulled by default; the
  embedding-live and e2e tests skip until it is).
- Test login: `admin@example.com` / `supersecret123` (persisted in Postgres).

## Not done yet
**The frontend EXISTS and is substantially built** — it lives in the separate repo
`../react/local-ai-model-frontend` (React 19 + Vite + Tailwind 4, vitest). Auth,
chat, files, department scoping, the NRB ops page, and citations
(`components/chat/SourcesPanel.tsx` + `OcrNotice.tsx` for the machine-recovered
`verify_note`) are all implemented. Do not assume it is unbuilt: check that repo
before claiming a contract is unconsumed. Its per-department roles UI is on branch
`feat/roles` and **ships together with this repo's `feat/role`** — a roles frontend
against a gateway without the level field receives no `role`, fails closed, and
silently hides every RAG admin control.

**Abstention is WIRED BUT INERT.** `search_department_docs` fetches
`RAG_RERANK_POOL` and routes candidates through `app/rag/ranking.py`, which can
abstain — but `RAG_RERANK_ENABLED=false` and `RAG_RELEVANCE_THRESHOLD` is an
unfitted placeholder, so the serving path is byte-identical to before: RRF order,
no abstention. **0.5 is disqualified as a threshold** — it is exactly what
`rerank.score_from_logprobs` returns for "no signal", and the decision boundary would sit exactly on the sentinel, and since the
comparison is `>=`, a passage the reranker had NO opinion about would be kept
and treated as relevant (verified: at 0.5 a no-signal passage is kept; at 0.6 it
is dropped). Either way the least informative case is decided by the comparison
operator rather than by evidence, which is why the value is disqualified. Fitting it needs three things in order: a human authors the 50
`[REVIEW` questions in `docs/rag/retrieval-eval-cohort.json` and freezes it,
`ollama pull qwen3-reranker:4b`, then `scripts/rag_eval_sweep.py`. Pick the
operating point from the false-refusal column FIRST — refusing a question the
corpus answers is worse than the over-confidence this replaces, because
over-confidence yields a wrong answer the user may catch while a false refusal
denies a correct one. Design + method: `docs/superpowers/specs/2026-08-22-rag-abstention-and-retrieval-eval-design.md`.

Still open here: history follow-ups (title rename, context-window truncation);
file follow-ups (pagination, orphan cleanup of root-level pre-scoping files);
client-side stream cancellation/abort. Deployment hardening (firewall internal
deps to the gateway IP) is deferred by the user for now.

**Known broken test, unrelated to any feature:**
`tests/test_rag_reingest_integration.py::test_department_filter_restricts_the_set`
asserts `every["total"] == 2` after seeding two documents, but `reingest` with no
department counts the WHOLE table, so it fails on any developer database with real
data (781 documents here). A test-isolation bug, not a `reingest` bug.

**NRB, in the order they became unblocked** (full reasoning in
`docs/nrb-integration.md` §26.11): the thin admin UI over `/v1/nrb/*` (built in a
SEPARATE frontend repo, `local-ai-model-frontend`); the actual **corpus ingest**
(a run needing the go-ahead — everything structural is now in place); then
cron/systemd (which triggers *through* `pipeline.request_run` — it does not
replace the runner); and GPU-server deployment, still blocked on §19.1 (no host,
no key, no SSH user in this environment). **Phase 8 is DONE (§29):** NRB documents
are searched via `search_department_docs` with route-aware, caveated citations —
no separate `search_nrb_documents`. The `RAG_DOCS_DIR` duplication decision is
**DONE (§28, resolve-from-filestore)** and the Alembic lineage is **DONE and
EXECUTED (§27 decided it, §30 carried it out: citations un-deferred, rebased onto
the NRB head, one linear head, both databases at head)**; neither gates a
full-corpus ingest. Two known-and-recorded, not fixed: `075bf12eb087`'s broken-ToUnicode text
layer (a `native-3` + new cohort, §17.6) and the Nepali semantic review of the
§15 pack — **conversion correctness is still unmeasured** (which is why §29's
citations carry the machine-recovered "VERIFY" caveat).

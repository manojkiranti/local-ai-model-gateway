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
`local_ai_gateway`.** The dev DB is stamped at `d4a91f2c7b3e`, a revision that
exists only on `feat/rag-source-citations` — which is **deferred, not abandoned**
(user's decision, `docs/nrb-integration.md` §9.10). So on this branch `alembic
current` against the dev DB fails by design. Do **not** "fix" it with `alembic
stamp`, by dropping `chat_messages.sources`, or by recreating the DB; the lineage
gets reconciled before NRB is merged, not before it is built.
`DATABASE_URL=…/local_ai_gateway_p4` for every NRB sync, fetch and DB test.

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
**NRB documents are now parsed
and classified, but still NOT chunked, embedded or searchable** — the rest of 6B
(OCR strategy), 7 (chunk+embed) and 8 (`search_nrb_documents`) are
not started; the 6B gate and its recommendation are §11.9 and §12.10. The roadmap was renumbered when Phase 4 was scoped down to
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

## Postgres (local dev)
Local PG17 via TCP. Superuser: `postgres`/`postgres` on 127.0.0.1:5432 (peer auth
fails — no `manoj` role). App uses role `gateway` / db `local_ai_gateway` (creds
in `.env` only, never in code). Create with:
`psql -h 127.0.0.1 -U postgres -c "CREATE ROLE gateway LOGIN PASSWORD '...'; CREATE DATABASE local_ai_gateway OWNER gateway;"`

## Layout
`app/{config,main}`, `db/`, `auth/`, `users/`, `ollama/` (client), `chat/`,
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
validation; writes NOTHING); `report` = all of them — everything but `client` is
**not** model-facing, run via
`scripts/nrb_{sitemap_inventory,document_inventory,sync,fetch,sample,extract,calibrate,build_lexicon,legacy_eval}.py`), `tools/` (`registry.py` = engine; `local/` package = one module
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
scanned-vs-empty distinction), `ingest`=extension→family dispatch (spreadsheet
vs document) shared by the upload route and turn-open, `router`=upload
`POST /v1/files` + `GET /v1/files` list + owner-scoped `/v1/files/{id}`; feeds
create_excel/html/chart/pdf/csv/docx and inspect_excel/read_excel/
read_document),
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
process, `router`/`jobs_router` = `/v1/departments` + `/v1/ingest-jobs`).
`alembic/` for migrations.
- **Adding a local tool:** new `app/tools/local/<name>.py` with `_fn` + `SPEC`,
  then add `<name>.SPEC` to `LOCAL_TOOLS`. The engine (`registry.py`) never changes.

## Endpoints
Public: `/health`, `POST /auth/register`, `POST /auth/login`.
Authed (JWT): `GET /users/me`, `GET /users` (admin), `POST /v1/chat`,
`GET /v1/tools`, `GET /v1/mcp/status`, `POST /v1/files` (upload .xlsx/.csv/
.pdf/.docx/.txt/.md/.json → `generated_files` row `source=uploaded`; 400 bad
ext/corrupt/zip-bomb, 413 over size cap), `GET /v1/files` (caller's files,
newest first; `?source=` filters),
`GET /v1/files/{id}` (owner-scoped download; 404 if not yours),
`DELETE /v1/files/{id}` (owner-scoped; 204, removes row + on-disk file),
`GET /v1/sessions`, `GET /v1/sessions/{id}`, `DELETE /v1/sessions/{id}`,
`POST /v1/departments` (admin), `GET /v1/departments` (admin → all; member →
granted+active, i.e. the frontend's tabs), `PATCH /v1/departments/{code}`
(admin), `GET|POST /v1/departments/{code}/members` (admin),
`DELETE /v1/departments/{code}/members/{user_id}` (admin),
`POST /v1/departments/{code}/documents` (admin, multipart → 202
`{document_id, job_id}`; 400 bad ext/empty, 409 duplicate content, 413 over cap),
`POST /v1/departments/{code}/documents/text` (admin, typed text → `source=manual`),
`GET /v1/departments/{code}/documents` (dept members see `ready` only; admins see
non-archived, `?include_archived=` admin-only),
`DELETE /v1/departments/{code}/documents/{id}` (admin; archives — chunks removed,
row retained), `GET /v1/ingest-jobs/{id}` (admin, progress).
`GET /v1/mcp/status` is the UI's MCP-connection badge — **always 200**, health
is in the body (`configured/reachable/tools/error`), never a 502.
`POST /v1/chat` is the **single, unified** turn endpoint — **stateful**
(`{session_id?, message, model?, stream?, options?, file_ids?}`, server rebuilds
context + persists both rows) and **tool-capable** (runs the agent loop every
turn; the
model calls local/MCP tools when useful). `stream:false` → JSON
`{session_id, message, model, stop_reason, trace?}`; `stream:true` → NDJSON typed
events (`token`/`tool_call`/`tool_result`/`done`) + the new id in the
`X-Session-Id` header. **There is no `/v1/agent`** — it was folded in.

## Conventions / gotchas
- Auth: JWT (PyJWT HS256) + bcrypt. Provider-agnostic User (email, auth_provider,
  nullable password_hash, role admin|member). **First registered user → admin.**
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
  3778, leaving ~300 of a 4096 window). `LOCAL_TOOLS` is now **16** (`read_document`
  landed after that measurement) — the token figure has not been re-measured
  since, so treat it as a floor, not the current count, until it is. Either way
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
  31 days, or 3 without a `currency` (a full day is ~22 rows). Future NRB document
  search (`search_nrb_documents`, Postgres/pgvector) is a separate tool — the
  descriptions cross-reference each other, so keep the "not for policies/
  circulars/directives" clause here.
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
  emitters (`service.open_turn`, `repository.build_context_messages`) use
  `user`; `test_attachment_note_is_a_user_message_not_a_system_one` locks it.
  Related, and the reason this was easy to get wrong: the agent's
  `SYSTEM_PROMPT` is now **always** message 0 — the old
  `base_messages[0].role != "system"` guard silently dropped it for any session
  that began with a file upload, back when the note was a system message.
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
  session given a department**, 400 bound session with no code. Admins bypass the
  grant check ONLY.
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
Frontend (unblocked now). History follow-ups (title rename, context-window
truncation). File follow-ups (pagination, orphan cleanup of root-level
pre-scoping files). Client-side stream cancellation/abort.
Deployment hardening (firewall internal deps to the gateway IP) is deferred by
the user for now.

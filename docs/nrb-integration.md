# Nepal Rastra Bank integration — status + roadmap

**Purpose:** one page answering "where does NRB integration stand" — what's
live, what broke and how it was fixed, and what's deliberately not built yet.
Code-level gotchas stay in `CLAUDE.md` (grep `nrb`); this is the status view.

Last verified: **2026-08-15** (Phases 1–5 live-verified 2026-08-14; Phase 6A is
build-only so far — see §11).

---

## 1. Status

| Phase | What | Status |
|---|---|---|
| 1 | `get_nrb_forex` — live forex rates tool | **Done, tested, live-evaluated** |
| 2 | Sitemap discovery + source inventory | **Done, live-run 2026-08-13** |
| 3 | Page/document-level discovery + attachment inventory | **Done, full-corpus run 2026-08-14** |
| 4 | Persistent catalog + idempotent metadata sync | **Done, live-run twice 2026-08-14** — §9 |
| 5 | Attachment download → MIME validation → SHA-256 → local storage | **Done, live-fetched 2026-08-14** — §10 |
| 6A | Native extraction + quality profiling (**no OCR**) | **Done, live-profiled 2026-08-15** — 400-file benchmark fetched, 381 extracted, pypdf-vs-Docling calibrated — §11 |
| 6B | OCR / legacy-font strategy, chosen from 6A's evidence | **Task 1 (conversion) evaluated, NOT deployed — §12. Task 2 (`native-2` routing classifier) MEASURED and recommended 2026-08-15 — §13.** Conversion routing + OCR strategy still not started |
| 7 | Chunk + embed into the existing `documents`/`document_chunks` pipeline | Not started |
| 8 | `search_nrb_documents` tool | Not started |

**The roadmap was renumbered when Phase 4 was specified.** It previously read
"Phase 4 = documents through the RAG pipeline (download/parse/chunk/embed), Phase
5 = search tool", i.e. one phase for everything after discovery. Phase 4 is now
only **persistence and reconciliation**, and what used to be inside it is Phases
5–7. So *"Phase 4/5 is done" does NOT mean any NRB document is searchable* — bytes
are on disk, but nothing has been parsed, chunked or embedded. §8 (the old gate) is
kept for the decisions it records; §9 is Phase 4 as built and §10 is Phase 5.

Phase 1 is a self-contained vertical slice: a local tool + a dedicated API
client, no shared state with the rest. **Phases 2 and 3 are both read-only
reconnaissance** — no tables, no cron, no persistence, nothing downloaded,
nothing registered in `LOCAL_TOOLS`; they exist so Phase 4 could be designed
against the real site instead of a guess. **Phase 4 adds four tables and a manual
sync command and still downloads nothing**; **Phase 5 adds the download and stores
raw bytes and still parses nothing.** Both stay deliberately separate from
`app/rag/` — Phase 7 is where that finally gets touched. Nothing through Phase 5 is
registered in `LOCAL_TOOLS`, reachable by the model, or exposed on any endpoint.

**Phase 6 was split.** 6A extracts text with the parsers already in the repo,
measures it, and classifies each blob as `extracted` / `suspicious` / `needs_ocr` /
`unsupported` / `failed` — so 6B chooses an OCR strategy from measured evidence
rather than from an assumption about a corpus that is 91% PDF and largely Nepali.
**6A runs no OCR of any kind and converts no legacy font**; that is a hard boundary,
not a sequencing convenience. It also chunks nothing, embeds nothing, and adds no
tool. See §11.

**6B Task 1 has now evaluated legacy-font conversion and NOT deployed it** (§12).
Preeti recovers correctly — one document converts line-for-line identically to its
rendered page — but only above `legacy_line_ratio >= 0.80`; below that the band is
mostly native-1's over-flagged English tables and the guards correctly decline.
All 14 negative controls reconstruct byte-identically. `quality.classify`, the 0.20
threshold and the 381 `native-1` rows are untouched, and no routing is wired.

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
# unit suites — pure, HTTP mocked, no network, no live model
.venv/bin/pytest tests/test_nrb_forex.py tests/test_nrb_sitemap.py tests/test_nrb_pages.py

# Phase 4 catalog suites (test_nrb_sync_integration needs Postgres; it skips
# without one, and every test rolls back — see the file's isolation note)
.venv/bin/pytest tests/test_nrb_catalog.py tests/test_nrb_sync_integration.py

# bounded live inventories (network, no model)
.venv/bin/python scripts/nrb_sitemap_inventory.py
.venv/bin/python scripts/nrb_document_inventory.py --limit 800 --verify 40

# the catalog sync (network + Postgres). Run it TWICE — the second run reporting
# all-zero is the acceptance test. --dry-run changes nothing.
.venv/bin/python scripts/nrb_sync.py --dry-run
.venv/bin/python scripts/nrb_sync.py -v
.venv/bin/python scripts/nrb_sync.py -v

# Phase 5 fetch suites
.venv/bin/pytest tests/test_nrb_fetch.py tests/test_nrb_fetch_integration.py

# downloading files (network + Postgres + disk). Scope is REQUIRED; --dry-run makes
# no HTTP request at all and prints how many files and bytes were selected.
.venv/bin/python scripts/nrb_fetch.py --core --dry-run
.venv/bin/python scripts/nrb_fetch.py --section circular --limit 25 -v

# Phase 6A suites (quality metrics, format dispatch, manifest format, and the
# catalog queries around extraction — the last needs Postgres and rolls back)
.venv/bin/pytest tests/test_nrb_quality.py tests/test_nrb_extraction.py \
    tests/test_nrb_manifest.py tests/test_nrb_sampling.py \
    tests/test_nrb_extract_pass.py tests/test_nrb_extraction_report.py \
    tests/test_nrb_extract_integration.py tests/test_files_documents_pdf_pages.py

# the extraction pass. Scope is REQUIRED; --dry-run opens no blob and parses
# nothing; NOTHING here makes a network request.
.venv/bin/python scripts/nrb_extract.py \
    --manifest docs/nrb/phase6a-manifest.json --dry-run
.venv/bin/python scripts/nrb_extract.py --section circular --limit 25 -v

# the benchmark cohort. Catalog-only: no HTTP, and no file is written without
# --out. It is already FROZEN — the write below is what was run once, on
# 2026-08-15, and re-running it is refused rather than silently re-drawing.
.venv/bin/python scripts/nrb_sample.py --size 400 --seed phase6a-v1 --floor 2 \
    --year-2019-cap 120 --max-cohort-share 1.0 --dry-run
.venv/bin/python scripts/nrb_sample.py --size 400 --seed phase6a-v1 --floor 2 \
    --year-2019-cap 120 --max-cohort-share 1.0 --out docs/nrb/phase6a-manifest.json
.venv/bin/python scripts/nrb_sample.py --verify docs/nrb/phase6a-manifest.json

# the exact benchmark cohort, and one publication year. Both dry, both zero HTTP.
.venv/bin/python scripts/nrb_fetch.py --manifest docs/nrb/phase6a-manifest.json --dry-run
.venv/bin/python scripts/nrb_fetch.py --section circular --year 2019 --dry-run

# EVERY NRB command and DB test on this branch needs the scratch database — the
# dev DB is stamped at a revision that exists only on the deferred citations
# branch, so `alembic current` against it fails BY DESIGN (§9.10). Do not "fix" it.
export DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4'

# forex unit suite alone
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

## 5. Phase 2 — what shipped, and what the live site turned out to be

### Files

```
app/nrb/sitemap.py                   host guard, bounded fetch, XML parsing, the walk
app/nrb/classify.py                  pure deterministic URL classifier
app/nrb/report.py                    pure aggregation + rendering
scripts/nrb_sitemap_inventory.py     the manual run (--json / --urls / --sample / --root)
tests/test_nrb_sitemap.py            109 unit tests, HTTP mocked, no network
```

Config: one new setting, `NRB_SITE_BASE_URL` (default `https://www.nrb.org.np`).
Separate from `NRB_API_BASE_URL` because that is a versioned API path; its **host
is also the discovery trust boundary**. Nothing was added to `LOCAL_TOOLS`.

```
scripts/nrb_sitemap_inventory.py
   → sitemap.discover()      probe root → walk index → fetch children (bounded)
        → parse_sitemap()    namespace-agnostic, doctype/entity-refused
        → normalize_url()    dedup key; original loc always retained
        → classify_url()     section / department / resource_type / page_kind
   → report.summarize() → report.render()
```

### Live inventory (2026-08-13)

Root: **`https://www.nrb.org.np/sitemap_index.xml`** (`/sitemap.xml` 301s to it).
**60** sitemaps fetched (index + 59 children, all `urlset` — depth 2, no nesting).
**19,480** URLs discovered, **19,480** unique (0 duplicates), 0 rejected, 0 errors.
`lastmod` on every entry, spanning 2019-12-05 → 2026-08-13.

| page_kind | count | | resource_type | count |
|---|---|---|---|---|
| `document_post` | 18,567 (95.3%) | | `html` | **19,480 (100%)** |
| `news_post` | 415 | | | |
| `taxonomy_archive` | 359 | | | |
| `page` / `post_type_archive` / `department_page` / `office_page` / root | 139 | | | |

Sections: `unknown` 18,666 (95.8%), `research` 230, `other` 193, `media` 177,
`faq` 35, `report` 31, `statistics` 31, `circular` 28, `notice` 18,
`guideline_manual` 12, `publication` 11, `license_registry` 9,
`monetary_operations` 8, `directive`/`monetary_policy`/`act`/`rule_bylaw`/
`enforcement_action` 4 each, `procurement` 5, `career` 3, `forex` 3.

Top owners (of 33 codes): `bfr` 5,400 · `pdm` 3,584 · `red` 2,299 · `ofg` 2,296 ·
`gsd` 951 · `fxm` 544 · `hrm` 538 · `psd` 433 · `fmd` 384 · `fiu` 208 · `mfd` 201.
1,202 URLs have no owner (categories, dated posts, standalone pages).

### The finding that matters

**The sitemap says who published a document, never what it is.** 18,567 documents
live at `/{owner}/{slug}/` with a Devanagari title slug; the
directive/circular/act vocabulary appears only on the 359 `/category/…` archive
pages. So the 95.8% `unknown` is the site's shape, not a weak classifier — which
is why `page_kind` exists as a separate field. Three consequences for Phase 3:

1. **There is no URL rule waiting to be found.** Section has to come from the
   category archives (359 pages, paginated) or from each post's own page.
2. **No attachment URLs are in the sitemap at all** — zero `.pdf`. PDFs are
   linked from inside the HTML pages, so document discovery is necessarily a
   page-level crawl, not a sitemap read.
3. ~~**The WordPress REST API is disabled** (`/wp-json/wp/v2/…` → 404), so the
   cheap route to per-post categories and attachments is closed.~~
   **This was wrong, and Phase 3 corrected it.** `/wp-json/` is disabled but NRB
   moved the REST prefix to **`/api/`**, which is fully open. Only the default
   path was tested. See §7 — it changed Phase 3's whole design.

Chronology is safe: every URL carries a `lastmod`, so the directive-plus-later-
circulars ordering that Phase 3+ needs is available without re-crawling.

### Surprises worth remembering

* Sitemap **filenames disagree with paths**: the eight office sitemaps publish
  `/federal-offices/<code>/<slug>/` (owner in the *second* segment, 385 URLs), and
  `ditty_news_ticker-sitemap.xml` publishes `/ticker/…`.
* NRB runs a **misspelled duplicate category** (`inforcement-actions-offsite-onsite`
  alongside `enforcement-actions-offsite-onsite`) with live posts in it. Kept.
* **`fepd` does not exist** on the site. The relevant codes are `fxm` and `ficpd`.
* A 404 returns a **~100 KB HTML page**, so "is this a sitemap" is decided on the
  parsed root element.
* Paged post types split at exactly 1000 URLs (`bfr-sitemap1..6`).
* All 18 remaining unrecognised path roots are single institutional pages (about,
  contact, organogram, privacy policy…). No unmapped `/category/` roots remain.
* `/departments/` has 29 pages but only 27 are owner codes — `statistics-division`
  and `statistics-data-links` are pages with no post type, so they own no
  documents and correctly get `department=null`.
* NRB publishes an **`/api-docs-v1/` page — it documents the Forex API only.**
  Checked, because an official document API would have removed the need to crawl
  at all. There is no *documented* one — but there is an undocumented one, the
  WordPress REST API at `/api/`, which Phase 3 found and now uses (§7).
* The site nav exposes NRB's own editorial hierarchy ("Laws, Policies &
  Guidelines → Acts / Rules and Bylaws / Guidelines and Manuals", "Regulations &
  Supervisions → Circulars", …), which is a third possible route to section
  membership and cheaper than either option in §6 — worth pricing before choosing.

### Bounds and trust

Host: exactly the `NRB_SITE_BASE_URL` host — no subdomains, no userinfo, https
required for anything fetched. Every child sitemap loc is re-checked, so NRB's own
sitemap cannot walk us off site. Redirects are not followed except **one hop at
root probing**, same-host and https (that 301 is real). Depth ≤ 3, ≤ 300 sitemaps,
≤ 200,000 URLs, ≤ 10 MB per response, 5 s connect / 30 s read, sequential
requests. Any bound that bites lands in `inventory.truncated`, prints a banner and
exits 1. `fetch_url`'s SSRF guards are neither reused nor relaxed.

### Evaluation & Improvement (Phase 2)

1. **Success metric** — share of discovered URLs carrying a *correct and useful*
   classification. Today: 100% carry an accurate `page_kind` and 95.3% carry an
   owner; only 4.2% carry a section, which is the ceiling the sitemap allows.
   Phase 3's own metric is what lifts the section number, so the honest Phase 2
   metric is **zero URLs silently lost or misfiled**: 0 rejected, 0 errors, 0
   unmapped categories, 18 unrecognised roots — all named in the report.
2. **Eval** — `tests/test_nrb_sitemap.py`, 109 tests, **109/109 passing**. The
   labelled set is 23 category→section cases plus 8 resource-type and 12
   page_kind/owner cases, all URL shapes copied verbatim from the live sitemap.
   Bounds, host rejection and XML safety are covered separately.
3. **Feedback capture** — the report itself: `unmapped_categories` and
   `unrecognised_path_roots` are the correction log, and every classification
   carries an `evidence` string naming the rule that fired, so a disputed label is
   traceable rather than arguable. `--json` output diffs cleanly between runs.
4. **Review loop** — re-run before any Phase 3 work and after any NRB site
   redesign; a new category or post type appears as a named to-do rather than a
   silent `other`. Both lists being empty and counts moving only upward is the
   pass condition.

---

## 6. How Phase 3 answered §5's open decision

§5 left one question: how a document gets its type, given the sitemap does not
carry one. It offered two shapes — crawl the 359 category archives, or fetch each
of the 18.5k post pages. **Neither was necessary.** Probing the site first found a
third route that neither option anticipated, and it is strictly better than both:
NRB's WordPress REST API, open at `/api/`, returns each post's category ids, its
dates and its attachment *as data*.

The lesson is worth keeping: the Phase 2 conclusion "the REST API is unavailable"
came from testing only the default `/wp-json/` path. One more probe would have
changed Phase 3's design a week earlier.

---

## 7. Phase 3 — what shipped, and what a document post really is

### Files

```
app/nrb/http.py                        shared host guard, URL normalization, FetchError
app/nrb/wp_api.py                      WordPress REST reader (bounded, paged, host-guarded)
app/nrb/attachments.py                 attachment extraction + typing (pure)
app/nrb/documents.py                   NRBDocument + category->section resolution (pure)
app/nrb/page.py                        bounded post-URL probe (the verification path)
app/nrb/report.py                      + summarize_documents / render_documents
scripts/nrb_document_inventory.py      the manual run
tests/test_nrb_pages.py                114 unit tests, HTTP mocked, no network
```

`sitemap.py` now imports its host guard from `http.py` rather than owning a
private copy — one trust boundary for every NRB integration. Its public surface
is unchanged (Phase 2's 109 tests still pass untouched).

Config: one new setting, `NRB_CRAWL_DELAY_SECONDS` (default 0.25). Byte caps,
timeouts and page bounds stay module constants, because they follow from the
site's measured shape; how hard we may lean on a central bank's website is an
operational judgement, so that one is configurable.

```
scripts/nrb_document_inventory.py
   -> wp_api.fetch_categories()      284 categories, 3 requests
   -> wp_api.fetch_post_types()      which types REST actually serves
   -> wp_api.fetch_posts(type)       100 posts/request, X-WP-Total paged
        -> documents.build_document()          pure
             -> attachments.extract_attachments()   acf fields, then body anchors
             -> Taxonomy.section_for()              category parent chain
   -> report.summarize_documents() -> render_documents()
   -> (--verify N) page.probe_page()  does the 302 land where REST said?
```

### The design decision, and why the brief was not followed

The brief specified a page-level HTML crawl: fetch each post page, scrape its
attachment anchors. Measuring the live site first contradicted every premise of
that plan:

| The brief assumed | The site does |
|---|---|
| post URLs render HTML to scrape | **104 of 110 answer 302 straight to the file** (97 PDF, 4 xlsx, 3 jpg) |
| attachments are anchors in the page | they are `acf.document_file`, a data field |
| page HTML carries dates | it carries **none** — no `article:published_time`, no JSON-LD |
| WP REST is unavailable | it is fully open at `/api/` |
| ~18,567 page fetches | **~190 REST requests** for the same corpus |

So REST is the data path and `page.py` is the *verification* path — it answers
"does the post URL really redirect to the file REST claims?", which is a real
question about trustworthiness. Measured: **60/60 probes agreed** on the full run.

### Live inventory — the FULL corpus (2026-08-14)

18,370 documents in **5m18s**, **zero fetch failures**, 60/60 probes agreeing.

| | |
|---|---|
| documents normalized | **18,370** |
| attachment links / unique | 18,298 / **18,256** (42 duplicate refs) |
| posts with **1** attachment | **18,032 (98.2%)** |
| posts with **0** | 205 (1.1%) |
| posts with **2** | 133 (0.7%) |
| PDF-looking | **16,593 (90.7%)** |
| non-PDF | 1,705 — spreadsheet 1,556, image 115, document 34 |
| type from WordPress's own MIME | 18,220 (99.6%); only 78 fell back to the extension |
| title present | 18,367 / 18,370 |
| published date present | **18,370 / 18,370** |
| canonical URL mismatches | **0** |

Attachment discovery: `acf:document_file` 18,105 · `acf:secondary_file` 115 ·
`body_link` 78. Extensions: pdf 16,593 · xlsx 1,252 · xls 304 · jpg 84 · doc 21 ·
docx 13 · png 11 · gif 10 · jpeg 10.

**Hosts: 18,295 of 18,298 on `www.nrb.org.np`. The other 3 are the finding** —
they point at `http://uat.nrb.org.np/wp-content/uploads/…`, a **UAT/staging host,
over plain http**. Live NRB documents linked to a staging server. Phase 4 must
decide explicitly what to do with them; they are currently refused by the host
guard and reported, which is the right default.

### Document type — one number would have lied

Blended coverage is **71.6%** (13,149 of 18,370). That figure is misleading, and
the report breaks it out by publication year because of it:

```
2003–2018     ~97–100%   (a few dozen to a few hundred per year)
2019           47.5%     9,189 documents  <-- the CMS migration
2020           96.4%     2021  95.9%   2022  97.2%   2023  89.3%
2024           95.0%     2025  96.5%   2026  96.5%
```

**5,052 of the 5,221 untyped documents sit in one category: `upload-files`**, a
WordPress catch-all, and almost all carry a 2019 date — NRB's bulk migration onto
this CMS. Only 160 documents have no categories at all. So type extraction is
~95% reliable for everything published since 2020 and the shortfall is a single,
named, one-off legacy backlog rather than a diffuse failure.

Sections (primary, full corpus): notice 3,050 · statistics 3,052 ·
monetary_operations 2,311 · circular 1,294 · media 990 · report 652 ·
procurement 314 · publication 340 · research 275 · license_registry 206 ·
monetary_policy 130 · guideline_manual 120 · enforcement_action 98 ·
directive 96 · act 90 · rule_bylaw 84 · career 25 · forex 19 · unknown 5,221.

Note the regulatory core is small and tractable: directives, circulars, acts,
rules, guidelines and monetary policy together are ~1,800 documents.

### What the page HTML exposes (for the record)

Sampled across owners; used only by `--verify`, never as the data path:

* `<link rel=canonical>`, `og:title` (with a ` - <og:site_name>` suffix to strip),
  `og:description`, the theme's single `.main-title`.
* **`<meta property="article:section">`** — the WordPress category, which is a
  useful cross-check on the REST category resolution.
* A Yoast breadcrumb, `Home » <owner name> » <title>`, which is the only place
  NRB spells out an owner code (`fmd` → "Financial Management Departments"). This
  is how `owner_label` gets populated without inventing an expansion.
* **No dates of any kind.** No JSON-LD anywhere.

### Special cases Phase 4 must handle

1. **Percent-encoding equivalence.** REST returns `…/आगलागी-२०७४.pdf` with literal
   UTF-8; the 302 `Location` returns the same file percent-encoded. Comparing raw
   strings reported phantom disagreements and would double-count the file.
   `attachments.comparison_key` (decoded path) is the identity;
   `Attachment.url` keeps NRB's own spelling because that is what a downloader
   should request.
2. **3 attachments on `uat.nrb.org.np` over http** (above).
3. **`economic-review` (49 URLs) and `er-article` (147)** are in the sitemap but
   **not REST-registered** — 196 documents REST cannot reach. Reported separately
   from failures; they need the page path or a different route.
4. **205 posts have no attachment at all** — some are genuinely empty stubs, some
   are duplicates of a sibling post (`economic-bulletin-2023-04-mid-april` and
   `…-2` both exist, one empty).
5. **133 posts carry two files** (`document_file` + `secondary_file`) — usually a
   circular plus its annex, so they are one document in two parts, not two
   documents. That is a Phase 4 modelling decision.
6. **A `mime_type` can disagree with the extension.** WordPress's value wins and
   both are kept (`resource_type`/`type_source` vs `extension`).
7. `acf` is `[]` on fieldless posts; an unset file field is `false`, not absent.
8. Useful deterministic ACF extras exist and are retained verbatim:
   `circular_number`, `fiscal_year`, `month`, `period`, `quarter`, `division`,
   `province`, and tender dates (`first_date_of_publication`,
   `last_date_of_submission`, `opening_date`).

### Acceptance criteria, answered from live evidence

1. **Can a document post be fetched and parsed reliably?** Yes — 18,370/18,370,
   zero failures, though via REST rather than the page.
2. **Authoritative title?** Yes — 18,367/18,370 (3 genuinely have none).
3. **Date/category metadata?** Dates **100%** (REST only; the page has none).
   Categories on 18,210 of 18,370.
4. **Real attachment URLs?** Yes — 18,256 unique, verified against the live 302 on
   60/60 probes.
5. **0 / 1 / many attachments?** 1.1% / 98.2% / 0.7%.
6. **PDF-looking?** 90.7% of links; 99.6% typed from WordPress's recorded MIME.
7. **Hosted where?** 18,295 on `www.nrb.org.np`; 3 on the `uat.` staging host.
8. **Type from page evidence?** 71.6% overall, **~95% for 2020 onward**; the
   shortfall is the 2019 `upload-files` migration batch.
9. **Edge cases?** Listed above.
10. **Deterministic enough to automate on?** Yes for everything except the legacy
    backlog: extraction is pure, ordered, and reproducible, and two runs over the
    same corpus produce byte-identical reports.

### Evaluation & Improvement (Phase 3)

1. **Success metric** — share of document posts yielding a *trustworthy* download
   target: a resolved attachment URL on the approved host whose type came from
   WordPress's own MIME. Currently **18,220 / 18,370 = 99.2%**; the complement is
   205 attachment-less posts, 78 body-link-only attachments and the 3 UAT ones.
   Type coverage (~95% post-2019) is tracked separately because it gates
   *routing*, not retrieval.
2. **Eval** — `tests/test_nrb_pages.py`, **114 tests, 114/114 passing**, plus
   Phase 2's 109 still green after the `http.py` refactor. The labelled set covers
   28 attachment cases, 18 normalization/classification cases and 16 fetch/
   security cases, with fixtures copied from live payloads. The live cross-check
   is `--verify`: **60/60** redirect targets matched REST.
3. **Feedback capture** — the report is the correction log: `unmapped_categories`,
   `post_types_not_served_by_rest`, `off_host_examples`, `untyped_examples`,
   `failures_by_kind` and `probe_disagreement_examples` each name a specific
   fixable gap, and every classification carries an `evidence` string. `--json`
   output is stable, so two runs diff cleanly.
4. **Review loop** — re-run `--limit 800 --verify 40` before any Phase 4 work and
   after any NRB site change; re-run `--all` when the corpus count moves
   materially. Pass condition: zero failures, zero probe disagreements, no new
   unmapped categories, and off-host attachments still countable on one hand.

---

## 8. Phase 4 — the gate

Phase 4 is `discovered attachment -> download -> validate MIME/content ->
hash/deduplicate -> PDF/text extraction -> chunk -> embed -> RAG ingestion`,
through the existing `app/rag/` pipeline (Postgres + pgvector — no second vector
database). None of it is built, and nothing in Phase 3 persists anything.

Decisions to make **before** writing the schema, all now answerable from §7:

* **Corpus scope.** The regulatory core (directives, circulars, acts, rules,
  guidelines, monetary policy) is ~1,800 documents — a far better first ingest
  than all 18,370, and it is exactly the set whose type is most reliable.
* **The 2019 `upload-files` backlog** (5,052 documents): ingest untyped, leave
  out, or classify from another signal. Do not guess from titles.
* **Two-file posts** (133): one document or two?
* **The 3 UAT/staging attachments** and the 196 REST-invisible posts: fetch by
  another route, or accept the gap and record it.
* **Identity and change detection.** `post_id` + `modified` + attachment
  `comparison_key` are all available; a content hash needs the download Phase 4
  introduces. Chronology for the directive/amendment problem is fully available
  (`date`, `modified`, plus `circular_number` where NRB publishes it).
* **Nepali / legacy-font PDF handling** — still unscoped, OCR fallback likely
  needed (a known gap for scanned documents generally, per `CLAUDE.md`'s RAG
  section). ~91% of the corpus is PDF, so this is the main technical risk.

Routing for the search tool is unchanged: `get_nrb_forex`'s description already
carries the negative clause ("not for monetary policy, circulars, directives…"),
and `search_nrb_documents` should reciprocate ("not for forex rates; use
get_nrb_forex").

---

## 9. Phase 4 — the persistent catalog

Discovery now has somewhere to live. Phase 4 reconciles NRB's published corpus
into Postgres on demand: what NRB publishes, which files each post points at, and
what changed since last time. **Nothing is downloaded** — no attachment is
fetched, no bytes are hashed, no text is extracted, nothing is embedded, no
`ingest_jobs` row is created, `LOCAL_TOOLS` is unchanged and no endpoint was
added.

### Files

```
app/nrb/models.py                nrb_sources / nrb_files / nrb_source_files / nrb_sync_runs
app/nrb/records.py               discovery -> rows, identity keys, the metadata hash (pure)
app/nrb/catalog.py               set-based data access (no commits)
app/nrb/discovery.py             one complete read of the corpus (REST + sitemap)
app/nrb/sync.py                  the idempotent reconciliation + advisory lock
app/nrb/report.py                + summarize_sync / render_sync
scripts/nrb_sync.py              the manual command (--dry-run / --limit / --json)
alembic/versions/9a1c4f7b2e05_add_nrb_catalog_tables.py
tests/test_nrb_catalog.py        71 pure tests (no DB, no network)
tests/test_nrb_sync_integration.py  55 tests against real Postgres
```

No new configuration. The host stays `NRB_SITE_BASE_URL`, pacing stays
`NRB_CRAWL_DELAY_SECONDS`, the database stays `DATABASE_URL`.

```
scripts/nrb_sync.py
   -> discovery.discover_corpus()            REST (~190 requests) + sitemap (60)
        -> wp_api.fetch_posts / documents.build_document   (Phase 3, unchanged)
        -> sitemap.discover()                              (Phase 2, unchanged)
   -> sync.run_sync()
        pg_try_advisory_lock  ->  refuse if another sync holds it
        -> catalog.create_run()
        -> sync.reconcile()   files -> sources -> relationships -> deactivation
        -> catalog.finish_run()
   -> report.render_sync()
```

### The schema, and the two identities that carry it

| Table | Rows | What it is |
|---|---|---|
| `nrb_sources` | 18,577 | one logical NRB post |
| `nrb_files` | 18,266 | one distinct external attachment |
| `nrb_source_files` | 18,308 | which files a post publishes, ordered |
| `nrb_sync_runs` | one per sync | counters + why deactivation was or was not allowed |

**Source identity** is `(wp_post_type, wp_post_id)` first — WordPress's own id,
enforced by a *partial* unique index (`WHERE wp_post_id IS NOT NULL`, because a
sitemap-only row has none and a plain UNIQUE would allow exactly one) — and
`url_key` as the fallback, enforced unique unconditionally. Never the title: NRB
publishes near-identical Devanagari titles across years and three documents have
no title at all.

**File identity** is `comparison_key`, Phase 3's `attachments.comparison_key`
reused rather than reimplemented.

`url_key` is the part the brief did not anticipate, and it is the difference
between 197 rows and 18,577. `comparison_key` was specified for files because REST
returns `…/आगलागी-२०७४.pdf` literally while the 302 percent-encodes it — **the
same is true of page URLs**: the sitemap percent-encodes Devanagari slugs, REST
does not. Matching them as raw strings makes every REST document look absent from
the sitemap, and each would be inserted a second time as a "sitemap only" stub.
So `url_key` = `comparison_key` + a trailing-slash strip (WordPress serves
`/bfr/slug/` and `/bfr/slug` as one page). `page_url` and `source_url` keep NRB's
own spelling, because that is the string a fetcher must request; the `*_key`
columns are only ever compared. Live proof: 18,380 REST documents, 18,577 sitemap
document URLs, and exactly 197 of the latter unmatched.

Other schema decisions worth not re-deriving:

* **`document_type` is nullable and 5,418 rows use it.** 5,221 are the untyped
  corpus from §7 (mostly the 2019 `upload-files` migration batch) plus the 197
  sitemap-only rows. A type guessed from a Devanagari title would be
  indistinguishable from a real one. `raw_taxonomy` keeps NRB's category ids,
  slugs, names and the per-category evidence so a future reclassification runs
  against Postgres instead of re-crawling.
* **`sections` is a JSONB array, not a scalar.** Posts really are filed under
  several; `document_type` is only the first by `classify.SECTIONS` order.
* **`owner`, not `department`.** In this codebase `department` is the RAG
  permission boundary; reusing the word here would read as access control.
* **The three UAT attachments are rows.** `fetch_status='blocked_host'` with the
  guard's own reason, decided by `http.check_url(..., require_https=True)` — the
  same function the fetchers use, not a second opinion. The catalog records that
  NRB referenced them; nothing can fetch them. A CHECK makes "blocked with no
  reason" unrepresentable.
* **No `content_sha256` / `content_length` / `downloaded_at`.** Phase 5 adds those
  with its own migration when it knows their shape. Nullable columns nothing
  writes are dead weight.
* **`metadata_hash` excludes `sitemap_lastmod`** (Yoast derives it from
  `post_modified`, which is hashed) and carries only the attachment
  `comparison_key`s, not their MIME or size. A file edit is `files_updated`; a
  post gaining or losing a file is `sources_updated`. Hashing both would
  double-count one upstream edit and break the second-run-is-zero invariant.
* **Timestamps are parsed with the offset derived per post from `date` −
  `date_gmt`.** `modified` has no GMT twin, so an assumed +05:45 would shift the
  chronology later phases need for amendment ordering by hours if NRB's WordPress
  site timezone were ever not what we guessed. The raw strings stay in `metadata`.

### Reconciliation semantics

| Situation | What happens |
|---|---|
| new | inserted; `first_seen_at` = this run |
| changed (`metadata_hash` moved) | updated in place; `first_seen_at` untouched |
| unchanged | `last_seen_at` + `last_sync_run_id` advance only — **not** counted as an update |
| missing, complete run | `is_active=false` + `deactivated_at`; never hard-deleted |
| missing, incomplete run | nothing; the run says why |
| reappears | reactivated, `first_seen_at` preserved |
| attachment removed | the **relationship** goes; the `nrb_files` row stays |
| attachment respelled (encoding only) | same file row, no change at all |
| attachment genuinely different | new file row; the old one is retained, unreferenced |
| REST stops returning a known post | stored REST metadata is kept, row stamped as seen, warning raised |

Three safety rules, in the order they bite:

1. **Absence-based deactivation requires a complete discovery** — every REST
   collection and the whole sitemap read with no error and no truncating bound.
   `--limit` and `--no-sitemap` are incomplete by construction.
2. **...and a 90% shrink floor** (only applied at ≥100 known sources). A
   "complete" run that suddenly sees 60% of the corpus is refused, because NRB
   serving empty REST collections would otherwise deactivate thousands of good
   rows in one statement. The run reports `partial` and names the reason.
3. **...and `ck_nrb_sync_runs_deactivation_needs_complete`** makes the illegal
   combination unrecordable even if a future caller tries.

Two guards that are not in the brief and were added because the failure they
prevent is silent:

* **A REST source is never downgraded to `sitemap_only`.** One post type dropping
  out of REST for a single run would otherwise strip the attachments off every
  source it owns — `bfr` alone is 5,400 — while the run still called itself clean.
* **`sitemap_only` rows are only created when the REST pass was complete**
  (`Discovery.rest_complete`, a separate question from `Discovery.complete`). On
  `--limit 300`, "in the sitemap but not in REST" would name ~18,267 URLs REST
  serves perfectly well. Verified: the bounded run creates zero and says so.

### Transactions, and what a crash leaves behind

Phases commit separately — files, then sources, then relationships (batched, but
always at a source boundary so a post's whole attachment set lands together), then
deactivation, then the run row. Every phase before deactivation is *additive or
corrective*, so a crash leaves a catalog that is **behind, never wrong**, and the
next run finishes the job. Deactivation is the only destructive-ish statement and
it runs last, gated as above. One 18k-row transaction was rejected deliberately:
it would either land whole or throw away a multi-minute run.

`--dry-run` runs the identical code path in one transaction and rolls it back, so
it predicts what the real run would do — including any constraint it would violate
— rather than approximating it. The run row is rolled back too. Verified live
against the populated catalog: `--limit 5 --no-sitemap --dry-run` reported 5
sources and 5 files *unchanged* and wrote nothing.

### Concurrency

A Postgres **session-level advisory lock** (`pg_try_advisory_lock`, key
`NRB_SYNC` as ASCII) held on a connection dedicated to it for the whole sync,
**taken before discovery rather than before reconciliation**. Both orderings refuse
correctly; the first version locked later, and a live check caught that a second
invocation would then spend ~190 requests and four minutes on a central bank's
website before finding out it could not proceed. Discovery is read-only, so this is
politeness, not correctness — which is exactly why it was easy to get wrong.
`tests/test_nrb_sync_integration.py` pins it by replacing `discover_corpus` with a
landmine while the lock is held. A
second sync refuses with `SyncBusy` rather than waiting, because two syncs would
interleave counters and race on the same rows. The lock is on its own connection
because an `AsyncSession` returns its connection to the pool at every commit,
which would silently release the lock at the first phase boundary and strand it on
a pooled connection. No lock table and no Redis: the lock dies with the
connection, so a killed sync leaves nothing to clean up. The `nrb_sync_runs` row
is a record, never a mutex — a crashed run's row stays `running` forever and
blocks nothing.

### Live runs (2026-08-14, full corpus)

Both against a clean database, back to back.

```
                      run #1        run #2
sources seen          18,577        18,577
  created             18,577             0
  updated                  0             0
  unchanged                0        18,577
  reactivated              0             0
  deactivated              0             0
  sitemap-only           197           197
files seen            18,266        18,266
  created             18,266             0
  updated                  0             0
  unchanged                0        18,266
  blocked                  3             3
relationships created 18,308             0
  removed                  0             0
status             completed     completed
deactivation applied    True          True
```

**Run #2 is the acceptance test and it is exactly zero.** Only `last_seen_at` and
`last_sync_run_id` moved.

Timing, and why the report prints the two halves separately: **discovery 258.3 s,
reconciliation 4.0 s.** Reading 18.5k documents over ~190 paced REST requests plus
60 sitemaps is the entire cost; the 18,577-source diff-and-write against Postgres
is four seconds. A blended number would make the sync look expensive and NRB's
site look fast, and would hide which half to look at when a run gets slower.

Database verification after run #2:

```
sources                       18,577   (active 18,577, inactive 0)
  from REST                   18,380
  sitemap-only                   197   <- economic-review + er-article + 1 ticker
  untyped (document_type NULL)  5,418
files                         18,266   (blocked 3, all uat.nrb.org.np over http)
source-file relationships     18,308
duplicate source identities        0
duplicate comparison keys          0
```

18,380 + 197 = 18,577 = the sitemap's document-URL count, which is the
cross-check that `url_key` matching works: every REST document was found in the
sitemap, and the 197 remainder is the known REST-invisible set. The one warning
was upstream's: two posts describe the same shared PDF with different metadata.

Sitemap URLs deliberately **not** persisted as sources (they are pages about
documents, not documents): 415 `news_post`, 359 `taxonomy_archive`, 60 `page`, 39
`post_type_archive`, 30 `department_page`, 9 `office_page`, 1 root. Counted in the
run's `notes` rather than silently dropped.

### Evaluation & Improvement (Phase 4)

1. **Success metric** — the share of NRB's published corpus that is present,
   correctly identified, and correctly attributed in the catalog, with **zero
   duplicate identities**. Today: 18,577 of 18,577 sitemap document URLs present
   (100%), 18,380 with full REST metadata (99.0%), 0 duplicate `url_key`s, 0
   duplicate `comparison_key`s, 3 known-unfetchable files recorded as such. The
   secondary metric is the one that makes the catalog trustworthy over time:
   **a second consecutive sync must report zero meaningful changes** (measured:
   0 created, 0 updated, 0 deactivated).
2. **Eval** — `tests/test_nrb_catalog.py` (71 pure) + `tests/test_nrb_sync_integration.py`
   (55 against Postgres) = **126 tests, 126/126 passing**, plus Phase 1–3's 296
   still green (**422** NRB tests total; full suite 1,060 passing with one
   pre-existing unrelated RAG failure, see §9.10). The labelled set covers the cases that would
   corrupt a catalog rather than merely annoy: encoding-only URL changes, the
   sitemap/REST identity match, reactivation, relationship removal without file
   deletion, the shrink floor, the incomplete-run deactivation ban, and every
   CHECK/unique constraint asserted against real Postgres. The live idempotency
   run is the end-to-end eval and is repeatable by anyone with network access.
3. **Feedback capture** — `nrb_sync_runs` **is** the feedback log: per-run
   counters plus `notes` (bounded samples of errors and warnings, the bounds that
   truncated discovery, the post types REST did not serve, the sitemap kinds
   skipped, whether deactivation was applied and if not why). Every source also
   carries `classification_source` (the rule that produced its type) and
   `last_sync_run_id`, so a disputed row is traceable to a run and a rule.
   `--json` output diffs cleanly between runs.
4. **Review loop** — run `scripts/nrb_sync.py` twice before any Phase 5 work and
   after any NRB site change. Pass condition: run #1 `completed`, run #2 all-zero,
   both duplicate counts 0, `inactive` moving only when NRB genuinely withdraws
   something, and `blocked_files` still countable on one hand. A jump in
   `sources_deactivated` or a `deactivation_skipped` note is the signal to look at
   NRB before looking at the code.

### 9.9 The Phase 5 gate

Phase 5 is `new/changed nrb_file -> safe download -> real MIME validation ->
SHA-256 -> local storage`. The catalog now answers the questions the old §8 could
not:

* **Corpus scope is a query, not a guess.** `SELECT count(*) FROM nrb_sources
  WHERE document_type IN ('directive','circular','act','rule_bylaw',
  'guideline_manual','monetary_policy')` is the ~1,800-document regulatory core,
  and `ix_nrb_sources_document_type` exists for exactly that.
* **The work queue is `nrb_files WHERE fetch_status = 'pending'`**, which by
  construction excludes the three blocked UAT files.
* **Change detection is already recorded** — `metadata_hash`, `modified_at`,
  `first_seen_at`/`last_seen_at` per file. What is still missing is *content*
  identity, which needs the download: Phase 5 adds `content_sha256`,
  `content_length`, `downloaded_at` and the `fetched`/`failed` values in
  `ck_nrb_files_fetch_status` (that CHECK must be edited, not bypassed).
* **Still undecided:** whether to ingest the 5,418 untyped sources, and Nepali /
  legacy-font PDF handling with an OCR fallback (~91% of the corpus is PDF, so
  this remains the main technical risk). Neither is a schema question any more.

### 9.10 Migration lineage: source citations are DEFERRED, not abandoned

**Decision, 2026-08-14 (the user's, recorded so nobody re-opens it as a bug):**
`feat/rag-source-citations` is **parked**. It is not superseded, not abandoned and
must not be deleted — source citations simply are not part of the current NRB
milestone. Nothing about it is being reconciled now.

Five facts this pins down:

1. **`feat/rag-source-citations` is intentionally deferred, not abandoned.** The
   branch stays as it is, local and on origin. It carries ~1,577 lines that exist
   nowhere else (`app/rag/sources.py`, the chat/history/tool provenance changes,
   four test files) plus a `rag_docs_base` fix `main` lacks. Not merged, not
   rebased, not cherry-picked, not deleted.
2. **`local_ai_gateway` (the real dev DB) remains tied to that branch's
   revision.** It is stamped at `d4a91f2c7b3e`, so on this branch `alembic
   current` fails with *"Can't locate revision identified by 'd4a91f2c7b3e'"* —
   expected, and left exactly that way. `chat_messages.sources` and its data stay
   untouched.
3. **NRB Phase 5+ development and testing use the Phase 4 scratch database**
   (`local_ai_gateway_p4`), where the NRB catalog migration and the full
   18,577-source catalog are already verified. Point `DATABASE_URL` at it; see the
   command block below.
4. **Before NRB is merged or deployed against the real dev/production database,
   the Alembic lineage must be revisited** and a decision made about citations.
   That is a gate on merging NRB, not on building it. The two candidate routes and
   the measured conflict analysis are kept below so the decision can be made from
   evidence rather than re-derived.
5. **No destructive database or migration-history operation is authorized** by
   this deferral: no `alembic stamp`, no dropping `chat_messages.sources`, no
   recreating or migrating `local_ai_gateway`, no editing `d4a91f2c7b3e`, no
   squashing history.

### 9.11 The lineage facts, for whoever revisits point 4

**The dev database cannot be migrated from this branch.** `local_ai_gateway` is
stamped at `d4a91f2c7b3e` (`add_chat_message_sources`), a revision that exists
only on the unmerged `feat/rag-source-citations` branch, so on `feat/nrb-sitemap`
even `alembic current` fails with *"Can't locate revision identified by
'd4a91f2c7b3e'"*. Phase 4's migration was therefore verified — upgrade, downgrade,
re-upgrade and `alembic check` (no drift) — plus both live syncs and the whole test
suite, against a scratch database created for it:

```bash
psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE local_ai_gateway_p4 OWNER gateway"
psql -h 127.0.0.1 -U postgres -d local_ai_gateway_p4 -c "CREATE EXTENSION vector"
DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \
    .venv/bin/alembic upgrade head
```

`9a1c4f7b2e05` revises `c33c0fd56028`, and `d4a91f2c7b3e` revises `c33c0fd56028`
too — so once both files coexist in one branch there are genuinely **two Alembic
heads** off a common ancestor. Nothing was changed about the dev DB's stamp.

**The preferred route when this is revisited (NOT yet done, and deferred by the
decision in §9.10) is a rebase, not an Alembic merge revision.**
`feat/rag-source-citations` lands on `main` first, in its own PR; then
`feat/nrb-sitemap` rebases onto `main` and `9a1c4f7b2e05`'s `down_revision` is
pointed at `d4a91f2c7b3e`. Rationale:

* Every branch in this repository is a linear ancestor of `main`. A merge DAG in
  `alembic/versions` would be the only non-linear thing in the codebase, and it
  would exist solely to record a coincidence of timing.
* `9a1c4f7b2e05` is **not yet committed**, so choosing its parent is *authoring*
  it, not rewriting published history. The only thing that has ever seen the old
  parent is the scratch database, which is disposable.
* The dev DB is already at `d4a91f2c7b3e`, so after the rebase
  `alembic upgrade head` applies **exactly one** migration and no merge revision
  is needed anywhere.
* Merging the citations branch into the NRB branch instead would put an unreviewed
  feature into the NRB PR and require resolving that feature's conflicts here.

Recipe, for when the citations branch has landed. **Nothing below has been run,
and none of it is authorized by the current deferral** — it is written down so the
decision in §9.10 point 4 costs an hour rather than a day:

```bash
git rebase main                      # onto a main that contains d4a91f2c7b3e
# then edit 9a1c4f7b2e05: down_revision = "d4a91f2c7b3e"
.venv/bin/alembic heads              # must print exactly one
.venv/bin/alembic check              # must print no new operations
.venv/bin/alembic upgrade head       # on the dev DB: applies 9a1c4f7b2e05 only
.venv/bin/python scripts/nrb_sync.py -v   # twice; the second must be all-zero
```

The five merge conflicts between the two branches were measured (`git merge-tree`,
no working tree touched) and are all mechanical, which is what makes landing the
citations branch on `main` cheap: `app/rag/parsing.py` and
`tests/test_rag_parsing_docling.py` are **duplicates of changes already on main**
(the CPU/no-OCR Docling pinning), `app/rag/retrieval.py` is additive on both sides
(`file_name`/`file_type` vs `dense_rank`/`lexical_rank`), `app/config.py` adds
`PROJECT_ROOT`/`rag_docs_base` which main **lacks**, and `.env.docker.example` is
two rewrites of the same comment block. The citations branch is therefore *not*
superseded — 1,577 lines of it (`app/rag/sources.py`, the chat/history/tool
changes) exist nowhere else, and its `rag_docs_base` fix is one main should have.

**One pre-existing test failure, unrelated.**
`tests/test_rag_reingest_integration.py::test_department_filter_restricts_the_set`
asserts an **unscoped** total of 2 documents, so any leftover `documents` rows from
an earlier test break it. It fails identically on a stashed tree with no Phase 4
code present (verified by `git stash push -u`), and it is the same dirty-database
failure noted during Phase 3. Not fixed here: the fix is to scope that assertion to
its own fixture, which is RAG's business.

---

## 10. Phase 5 — downloading the files

The catalog can now say *we have this file*, not merely *NRB publishes it*. For a
chosen slice of the corpus, `scripts/nrb_fetch.py` streams each file to disk under a
byte cap, hashes it, checks the bytes against NRB's claim about them, stores it
content-addressed, and records the result.

**Still nothing is parsed.** No PDF is opened, no OCR runs, no text is extracted, no
chunk or embedding exists, no `documents`/`ingest_jobs` row is created, `LOCAL_TOOLS`
is unchanged and no endpoint was added. A stored file is a raw artefact; what it
*says* is Phase 6's question.

### Files

```
app/nrb/sniff.py                 magic-byte typing (pure, stdlib only)
app/nrb/filestore.py             content-addressed blob store
app/nrb/locks.py                 the advisory-lock rule, now shared with the sync
app/nrb/fetch.py                 the downloader + one pass
app/nrb/catalog.py               + select_fetch_targets / record_fetch_outcomes / fetch runs
app/nrb/report.py                + summarize_fetch / render_fetch
scripts/nrb_fetch.py             the manual command
alembic/versions/2b7f5c9d1a34_add_nrb_file_download_columns.py
tests/test_nrb_fetch.py             56 pure tests (no DB, no network)
tests/test_nrb_fetch_integration.py 20 tests against real Postgres
```

One new setting, `NRB_FILES_DIR` (default `nrb_files`, gitignored). Byte caps and
timeouts stay module constants, as in Phases 2–3: they follow from the corpus's
measured shape (largest file live: 46 MB → `MAX_FILE_BYTES = 64 MB`), while pacing —
how hard we may lean on a central bank — remains `NRB_CRAWL_DELAY_SECONDS`.

### Scope is mandatory, and the numbers are why

The command **refuses to run without a scope**. Measured from the catalog:

| scope | files | reported size |
|---|---|---|
| everything (`--all`) | 18,263 | **8,793 MB** |
| PDFs only | 16,560 | 8,131 MB |
| spreadsheets | 1,554 | 619 MB |
| **the regulatory core (`--core`)** | **1,804** | **1,537 MB** |

`--core` is `circular, directive, act, rule_bylaw, guideline_manual,
monetary_policy` — the set whose document type is most reliable (~95% post-2019, vs
the 2019 `upload-files` backlog), and the obvious first ingest. 74 files report no
size at all, so every total above is a floor. `--dry-run` prints this without making
a single HTTP request.

### The three things that make a stored file trustworthy

1. **HTML where a document was promised is a failure, not a file.** WordPress
   answers a missing file with a **200 and a themed ~100 KB HTML page** (Phase 2
   measured that on this site). Storing one as `circular-15.pdf` would hand Phase 6 a
   navigation menu to index as the text of a regulatory circular — a wrong document
   that parses cleanly, which is far worse than a recorded gap. `app/nrb/sniff.py`
   identifies the body from magic bytes; a `web` body against a document promise is
   recorded as `failed` and nothing is written. (An HTML file that NRB *said* was
   HTML is fine — the rule is about the mismatch.)
2. **A `Content-Length` that disagrees with the body is a failure.** A truncated PDF
   still opens and still parses; its tail is simply missing.
3. **The path is the checksum.** `<sha256[:2]>/<sha256>.<ext>` under `NRB_FILES_DIR`,
   so a blob is self-verifying, identical bytes republished under two URLs occupy one
   file, and no Devanagari filename or `..` ever reaches the filesystem. Writes are
   atomic: bytes stream to `.incoming/<uuid>.part` and are `os.replace`d into place
   once the hash is known — the final name cannot be chosen up front.

Type disagreements that are *not* the HTML case (NRB says PDF, bytes are a
spreadsheet) are **stored and recorded**, not rejected: `sniffed_mime` sits beside
`reported_mime_type` and the disagreement goes in `fetch_error`, for Phase 6 to
judge. An unsniffable body (`application/octet-stream`) is also kept — rejecting it
would lose real files whose type nothing at the front identifies.

### Safety, unchanged from Phases 2–4

`http.check_url(..., require_https=True)` is re-checked **at the code that opens the
socket**, not trusted from the catalog row. Redirects are refused rather than
followed. Bodies stream with a hard cap, so nothing is buffered in memory. Requests
are sequential and paced. There is no retry inside a pass — a failure is recorded and
`--retry-failed` is an explicit later decision. The three `uat.nrb.org.np`
attachments cannot be selected at all: `select_fetch_targets` only ever asks for
`pending` (plus `failed` on request), so `blocked_host` is excluded by construction
rather than by a `WHERE` clause someone could forget.

### Resumable, not idempotent — and the difference matters

The sync is idempotent: run it twice, the second run changes nothing. A fetch is
**resumable**: selection is `pending`-only in id order, results commit every 25
files, so a second invocation with the same scope picks up the *next* unfetched
files rather than redoing the last ones. A repeat pass over an *exhausted* scope
selects zero and is a genuine no-op (covered by
`test_a_second_pass_has_nothing_left_to_do`).

Because storage is content-addressed, the worst an interruption can leave is an
unreferenced blob — the next attempt at that file hashes to the same key, finds it
present, and records it. There is no cleanup step to forget.

Concurrency is a second advisory lock (`NRB_FTCH`), taken **before selection**, so a
second pass refuses in milliseconds instead of racing on the same rows and doubling
the load on NRB. `app/nrb/locks.py` now holds that rule once for both commands,
including the reason it needs a dedicated connection (an `AsyncSession` returns its
connection to the pool at every commit, which would silently release the lock).

### Live evidence (2026-08-14)

Two bounded passes, `--section circular --limit 25` each, against the scratch DB:

```
                    pass #1     pass #2
selected                 25          25   <- the NEXT 25, not the same ones
fetched                  25          25
failed                    0           0
already on disk           1           0   <- byte-identical to a sibling
downloaded           15.5 MB     12.5 MB
newly stored         14.8 MB     13.2 MB
status            completed   completed
```

Verified afterwards: **24 blobs on disk for 25 rows** (one duplicate pair sharing a
`storage_key`), `sha256sum` of each blob equals its own filename, all 25 sniffed
`application/pdf` — NRB's recorded MIME agreed 25/25 — zero type disagreements, zero
`.part` files left behind, and 50 files `fetched` / 18,213 still `pending` after the
two passes.

The duplicate turning up **within the first 25 files** is the finding: content
addressing was not a hypothetical saving. Phase 3 had measured 42 duplicate
attachment *references*; identical bytes under *different* URLs is a separate and
apparently larger class, so the 8.6 GB reported total is an upper bound on disk.

### Evaluation & Improvement (Phase 5)

1. **Success metric** — the share of selected files that become a **trustworthy
   local artefact**: stored, hashed, and of the type NRB claimed. Live: **50/50
   (100%)** across two passes, 0 failures, 0 type disagreements, 100% of blobs
   verifying against their own filename. The metric that matters more as scale grows
   is the complement: **files stored that are not what they claim to be**, which the
   soft-404 rule is designed to hold at zero. That number is queryable
   (`fetch_status='fetched' AND fetch_error IS NOT NULL`), and it is 0.
2. **Eval** — `tests/test_nrb_fetch.py` (56 pure) + `tests/test_nrb_fetch_integration.py`
   (20 against Postgres) = **76 tests, 76/76 passing**; all NRB suites together are
   **498/498**. The labelled set is the failure catalogue rather than the happy path:
   the themed soft-404 in five spellings, a truncated transfer, an empty body, a
   redirect, an over-cap body, a timeout, a transport error, the UAT host, plain http,
   an off-host URL, a non-fatal type disagreement, an unsniffable body, and the
   duplicate-bytes case. Two of them were written because the implementation was
   wrong first: a leaked `.part` file on the cap path, and a sniffer that called
   control-character binary "text/plain".
3. **Feedback capture** — `nrb_fetch_runs` is the log: per-pass counters, the
   **recorded `scope`** (a fetch is always a slice, so the counters are meaningless
   without it), bounded samples of failures and type disagreements, and why a pass
   stopped early. Per file, `nrb_files` keeps `fetch_attempts`, `http_status`,
   `fetch_error`, `sniffed_mime` and `downloaded_at`, so a permanently broken URL is
   visible instead of being retried forever.
4. **Review loop** — before a large pass, run `--dry-run` and read the file count and
   size. After it, check `files_failed` and the failure samples: a cluster of soft-404s
   means NRB moved or withdrew files (re-run `nrb_sync.py` first), while a cluster of
   timeouts means back off the pacing. Re-check `distinct_blobs` against `fetched`
   occasionally — a widening gap is duplication, not a bug. Pass condition: no
   failures outside the known-explained set, and `duplicate comparison keys` still 0
   in the sync report.

### 10.9 The Phase 6 gate

Phase 6 is parsing: PDF/DOCX/spreadsheet text extraction for a Nepali corpus, with
legacy-font detection and an OCR fallback. What Phase 5 leaves it:

* A work queue that is a query — `nrb_files WHERE fetch_status = 'fetched'` — with
  `storage_key`, `content_length`, `sniffed_mime` and `resource_type` per row.
* Type answers it can trust as far as bytes go, and an honest limit where they do
  not: **OLE2 (`.xls`/`.doc`) is identified as a family, not a format**, because
  telling those apart means walking the OLE directory — which is a parser, i.e.
  Phase 6's job. A ZIP whose flavour is not in its first 4 KB likewise degrades to
  `application/zip`.
* Still undecided, and unchanged by Phase 5: **whether the 5,418 untyped sources get
  ingested**, and **how Nepali/legacy-font PDFs are handled** (~91% of the corpus is
  PDF, so this remains the main technical risk). Neither is a schema question.
* Not yet built, and deliberately: conditional re-download (no ETag/`Last-Modified`
  is stored, so a file NRB edits in place is not re-fetched until someone asks).

---

## 11. Phase 6A — native extraction + quality profiling (MEASURED)

**Design:** `docs/superpowers/specs/2026-08-15-nrb-phase-6a-extraction-quality-design.md`
**Plan:** `docs/superpowers/plans/2026-08-15-nrb-phase-6a-extraction-quality.md` (14 tasks)

**State as of 2026-08-15: Tasks 1–13 of 14 are done and the phase has been
measured on a live cohort.** The 400-file benchmark is frozen at
`docs/nrb/phase6a-manifest.json` (`1ae297d…`), 381 of its 400 files are
downloaded, all 381 are extracted at `native-1`, and the 40-PDF Docling
calibration slice (`docs/nrb/phase6a-docling-calibration.json`, `81d5979…`) has
been run over the 37 of it that fetched. The evidence is committed verbatim:

* **`docs/nrb/phase6a-profile.txt`** — the fetch accounting, the extraction
  profile, the resumability check, the by-eye validation and the throughput.
* **`docs/nrb/phase6a-calibration.txt`** — the pypdf-vs-Docling comparison and
  every one of its six disagreements in full.

Read those for the numbers; this section is the interpretation. Only Task 14
(this write-up) remained after them.

### Why the phase exists

Phase 5 left 49 blobs on disk and a probe over them found the failure mode that
shapes everything here: **text that parses cleanly and is wrong.** Those PDFs
extract without error, contain **zero Devanagari**, and read as ASCII rubbish
(`ffihW\ffifiHrz\reU=,.`) — Preeti/Kantipur legacy fonts, which map Devanagari
glyphs onto ASCII codepoints. A pipeline that only checks "did extraction raise"
would index all of them as English gibberish. So 6A measures the text and says
what is wrong with it, and only then does 6B decide what to do about it.

### What shipped (Tasks 1–13)

| Task | File | What it is |
|---|---|---|
| 1–2 | `app/nrb/quality.py` | Pure metrics + the classifier. No DB, no HTTP, no imports from `extraction.py` (evidence arrives via a neutral `Evidence` carrier). |
| 3 | `app/files/documents.py` | `read_pdf_pages` — the **single pypdf call site** in the repo, now shared by `read_document` and by NRB. Encryption handling, the 500-page cap and per-page failure isolation cannot drift between the two. |
| 4 | `app/nrb/extraction.py` | Format dispatch: pypdf / python-docx / openpyxl (`data_only=True`, formulas never evaluated) / text. **Never raises** — every failure is a recorded result. `.xls`/`.doc` are `unsupported`, not opened. |
| 5 | `app/nrb/models.py`, `alembic/…b1bea6ac36c5` | The `nrb_extractions` table. |
| 6 | `app/nrb/{manifest,catalog,fetch,report}.py`, `scripts/nrb_fetch.py` | The exact-cohort (benchmark manifest) fetch scope, `--year`, and extraction target selection. |
| 7 | `app/nrb/sampling.py` | The deterministic stratified sampler: candidate canonicalization, seeded ranking, four-pass allocation with cap redistribution, allocation diagnostics. Pure. |
| 7A | `app/nrb/manifest.py`, `app/nrb/report.py`, `scripts/nrb_sample.py` | `build_manifest`, the `selection_sha256` fingerprint, the freeze/verify guard, and the command that writes a cohort. |
| 8 | `app/nrb/extract.py`, `app/nrb/locks.py`, `scripts/nrb_extract.py` | The pass: manifest → catalog rows → unique blobs → extract → record. Advisory lock `NRB_XTRC`, batched commits, resumable, failure-isolated, zero network. |
| 9 | `app/nrb/profile.py`, `app/nrb/report.py` | The read-time cohort query and the deterministic profile: source/blob coverage, verdicts, metric distributions, legacy-severity bands, metadata breakdowns. |
| 10 | `app/nrb/calibration.py`, `app/nrb/calibrate.py`, `app/nrb/extraction.py`, `scripts/nrb_calibrate.py` | The frozen Docling calibration subset (40 PDFs drawn from the benchmark itself, own fingerprint), the Docling adapter behind a lazy import + reusable converter, the parser-neutral comparison model, and the deterministic agreement/rescue report. Writes nothing. |
| 13 | `docs/nrb/phase6a-profile.txt`, `docs/nrb/phase6a-calibration.txt` | The live profile: one fetch of the frozen 400 (381 acquired), the canonical `native-1` extraction of all 381, the resumability check, the by-eye validation, the throughput, and the pypdf-vs-Docling comparison over 37 of the frozen 40. Evidence, not code. |

### The legacy-font detector, and the measurement that rebuilt it

The first version classified per document and **missed 7 of 49 real circulars**.
Root cause: a `stopword_rate` gate. Glyph-mapped text is full of 1–2 character
ASCII tokens (`a`, `t`, `is`, `on`) that match short English stopwords by chance —
one file scored **0.248**, higher than genuine English prose. The deeper cause is
that those 7 are genuinely *mixed*: a real English annex (audit scope, a Basel
capital table) behind a Preeti-encoded Nepali covering note. The document average
is honestly English while the operative Nepali directive is unreadable.

Rejected on measurement: alpha-token denominators, stopwords of ≥3 and ≥4
characters (best margin only 1.7×), and per-page detection (caught 5 of 7).

What works is **per-LINE, shape-only** detection — vowel-less tokens, intra-word
symbols, intra-word case switches — with `legacy_line_ratio > 0.20`:

| corpus | ratio |
|---|---|
| English prose | 0.000 |
| Unicode Devanagari Nepali | 0.000 |
| the 49 live circulars | 0.281 – 1.000 |

**49/49 flagged, 0 false negatives.** `stopword_rate` is still reported as a
metric; it is no longer a gate. The threshold is calibrated on **one cohort** and
is frozen — Task 13's stratified sample is the agreed next calibration point, and
it must not be re-tuned against these 49 again.

### The table

`nrb_extractions`, migration **`b1bea6ac36c5`** (revises `2b7f5c9d1a34`).

Identity is **`(content_sha256, extractor_version)`**, not an `nrb_files.id`, and
there is deliberately **no foreign key**: storage is content-addressed and blobs
are shared (Phase 3 measured 42 duplicate attachment references), so per-file-row
extraction would parse the same bytes twice and store two answers to one question.
A file row being re-fetched must not orphan a valid extraction *of the same bytes*.

**Every column is a function of the bytes alone.** A source title is a useful
quality signal — a Devanagari title over zero-Devanagari text corroborates a
legacy-font verdict — but a blob referenced by one Devanagari-titled and one
English-titled source would persist a *different verdict depending on which source
the pass reached first*. That is non-deterministic persisted state and it would
break the second-run-is-identical invariant every earlier phase holds, so the
title-assisted signal lives in the read-time profile instead.

Five CHECKs, each proven against live Postgres rather than assumed: closed `status`
and `reason` vocabularies (a typo'd value would match no predicate and no query);
`failed` ⟺ an error is present; the legacy numerator and denominator travel
together and the numerator never exceeds the denominator; and **`preview ≤ 300`
characters**, which is the structural guarantee that this table never becomes a
document store. No extracted text is persisted — Phase 7 re-parses with Docling for
chunking anyway, and a text column that could hold a whole document is something a
later phase would eventually embed by accident.

Index note: `ux_nrb_extractions_content_version` serves lookup by blob and the join
from `nrb_files`. It does **not** serve a version-only staleness scan
(`WHERE extractor_version <> …`) — that column is second. Left as a scan on
purpose: it is an occasional operator query over one row per blob.

### The benchmark cohort is named, not approximated

Phase 5 selects `pending` rows in **id order** within a scope, and catalog id order
is the order REST paged the post types. So `--section circular --year 2019 --limit
60` returns the 60 *lowest ids*, and stratifying over that measures the id order
rather than the corpus — and is not reproducible, since any later fetch changes
what is on disk. The sample is therefore drawn **once** from the full catalog into
a committed manifest of exact `comparison_key`s, and fetch, extraction and
calibration all name that file.

`scripts/nrb_fetch.py --manifest <path>` fetches exactly it. The manifest holds
**catalog keys, not URLs**: keys are matched against `nrb_files.comparison_key`,
what gets requested is the matched row's own `source_url` through the same
`check_url` guard, and a key naming a host NRB never published matches no row and
is reported missing. There is no path by which a file on disk introduces a URL. The
key scope is purely additive, so a manifest still cannot select a `blocked_host`
file, and every cap, pacing rule and soft-404 check is unchanged.

Selection alone is not a report — it returns only the pending slice, so a cohort
already on disk would read as a cohort that had lost its files. Every pass
therefore accounts for **requested / already fetched / pending / previously failed
/ blocked / fetched this pass / failed this pass / not in the catalog**, in the dry
run as well as a real one. `--manifest --dry-run` makes **zero HTTP requests**.

### The sampler (Task 7) — and the two values still to decide

`app/nrb/sampling.py`, pure. Algorithm version **`nrb-stratified-v1`**, bound into
the manifest fingerprint, so changing what a stratum means or how slots are
allocated cannot silently redefine an existing benchmark.

**The unit is `comparison_key`**, canonicalized once per key before anything is
allocated. `catalog.load_sample_rows` returns one row per (file, *active source*)
association, so a file NRB publishes from two pages arrives as two rows that can
disagree about year, type and owner — 41 files in the live catalog do. Resolving
that in SQL means resolving it by `min(source_id)`, and source id order is REST
paging order, so a shared file's stratum would be decided by NRB's paging. The
rules instead: **earliest** year, `classify.SECTIONS` priority for the document
type (the catalog's own regulatory-first order, the one `Taxonomy.section_for`
already uses), every owner kept and the sorted-first reported. One key, one
candidate, one download, one extraction.

**Ranking is `sha256(algorithm_version ␟ seed ␟ comparison_key)`** — never
Python's `hash()`, which is salted per process, and never SQL order. The same
inputs in any order produce the same cohort; the tests shuffle the rows and assert
the keys, the per-stratum allocation and the fingerprint are all identical.

**Allocation is four passes, and the cap must not shrink the sample.** Floor
(round-robin, one slot at a time, in seeded-hash stratum order so a partial round
is not handed out alphabetically) → proportional (largest remainder, integer
arithmetic) → cohort cap → **redistribution, repeated**. The naive version —
allocate 400, trim 2019, return 350 — reads downstream as "we profiled 400 files".
Every slot a cap removes goes back into the pool and is handed out again, round
after round, dropping strata as they exhaust. **If 400 are requested and 400 can
legally be selected, exactly 400 come back**; if they cannot, the shortfall, the
constraint that bound it and every intermediate figure are in the diagnostics. A
cap is never breached to reach the number, and a floor is a preference the cap
outranks.

`selection_sha256` binds the schema version, the algorithm version, the seed,
every sampler parameter and the ordered keys — and nothing volatile, so the same
cohort drawn on another machine tomorrow hashes the same and one edited key does
not. `scripts/nrb_sample.py --verify <path>` recomputes it without a database or
a network.

Why floor 2 and not 5, which was the first draft: at 106 strata a floor of 5 wants
459 slots of a 400 budget, so the floor pass would consume the entire budget and
passes 2–4 would never run — the draw would be near-uniform across strata rather
than proportional, and the share 2019 received would be an accident of how many
strata it has. Floor 2 wants 197 slots, leaving 203 for proportional weighting and
letting the cap actually bind.

### The frozen cohort — `docs/nrb/phase6a-manifest.json`

**Drawn 2026-08-15 and committed. These 400 `comparison_key`s ARE the Phase 6A
benchmark.** Fetch, extraction, calibration and the published profile all name
this file; nothing downstream re-samples, and files already on disk are not
swapped out for fresh ones.

```
selection_sha256   1ae297dba1c33c7db9976f817806f6666371695a31e1f424d046993d581a1312
parameters         size 400 · seed phase6a-v1 · floor 2 · 2019 cap 120
                   algorithm nrb-stratified-v1 · no general cohort share cap
candidates         18,266 (41 with >1 source association) · 106 strata
selected           400 · unique 400 · shortfall 0 · unfillable 0
floor              197 requested / 197 allocated / 0 short
2019               159 allocated proportionally -> capped to 120
                   39 slots removed, 39 redistributed, 1 round
cohorts            2019 120 (AT CAP) · 2023-2026 131 · 2020-2022 109 · <=2018 40
formats            pdf 303 · spreadsheet 60 · image 22 · document 15
types              20 of 20 present; notice 67, statistics 60, untyped 35,
                   monetary_operations 33, circular 27, report 27, … faq 2
owners             25 of 33 codes; the 8 absent are <=0.33% of the corpus each
strata             106 of 106 represented, none empty
catalog resolution 400 known, 0 missing (3 already fetched, 397 pending)
```

The 2019 cap is the only hard cap. It is absolute (120 files), not a share, so it
does not move if the requested size ever changes — the point is a fixed ceiling on
a CMS-migration cohort that is half the corpus, not a proportion of a budget.

Structural review before freezing found no defects. The four things that look odd
and are not: `untyped` 35 is NRB's real `upload-files` catch-all (5,065
candidates), not a canonicalization failure — every one of those files still has a
year and an owner; 2003/2005/2006 are absent because they hold 6/2/2 files and
year is not a stratification key below the cohort; the 8 absent owner codes are
the smallest departments and owner is deliberately not stratified on; and rare
formats are over-represented against their corpus share (15 of the 34 `.docx`, 22
of the 115 images) because the floor exists precisely to make those cells
measurable.

Before reading any per-stratum number: with 400 files over 106 strata, **97 strata
are below the n<10 weak threshold**. Conclusions come from the cohort /
document-type / format breakdowns (n = 15–131), never from a single cell.

`tests/test_nrb_manifest.py::test_the_committed_phase6a_cohort_is_intact` guards
the committed file — parameters, count, canonical order, the 120 ceiling and the
fingerprint — with no database and no network.

### The extraction pass, and the two populations it must never merge

`app/nrb/extract.py` (`run_extract`), run by `scripts/nrb_extract.py`. Scope is
required — extraction is CPU-bound over 18.3k files — and the resolution runs one
way only:

```
manifest comparison_key -> nrb_files row -> content_sha256 + storage_key -> ONE target per sha
```

A URL in a manifest is never an input, nothing scans a directory, and a key the
catalog does not know is reported missing rather than substituted. **400 cohort
files is not 400 extractions**: some are not downloaded, and two cohort files with
identical bytes are one blob, one attempt and one verdict. `app/nrb/profile.py`
resolves both populations and the report prints them in separate blocks —
`source_coverage` denominated on the frozen manifest (so an unfetched file cannot
quietly leave the denominator and flatter every percentage), `blob_coverage` on
unique `content_sha256`.

"Current" is an exact `(content_sha256, extractor_version)` match. **A row written
by an older extractor never makes a blob current** — that is the invalidation
handle, and the pass selects by sha through the leading column of
`ux_nrb_extractions_content_version`, never by scanning `extractor_version <>`.
`--limit` applies *after* cohort resolution, content deduplication and
current-version filtering, to a list ordered by the manifest's own rank, so
`--limit 10` is the same ten blobs every run.

Every blob is hashed against the sha in its own filename before parsing: the path
IS the checksum, so bytes that no longer match are corrupt on disk, and a
truncated PDF is exactly the input that yields plausible-looking partial text.
Missing and corrupt are counted separately and recorded as `failed` rows rather
than skipped — a skipped blob would stay pending forever and never appear in a
status count. `--dry-run` opens no blob, calls no parser and writes no row.

**Live smoke over the 49 already-fetched circulars** (throwaway extractor version
`probe-6a-smoke`, rows deleted afterwards, so the benchmark's `native-1` state is
untouched): 49 blobs in 11.0 s, 0 failures, 380 pages.

```
suspicious / legacy_font_suspected            49 / 49   (100%)
devanagari_ratio                              0.0 at every percentile
legacy_line_ratio    min 0.281 · median 0.980 · max 1.000
  0.20-<0.50  3      0.50-<0.80  2      >=0.80  44
text_page_coverage   min 0.50  · median 0.929 · pages without text 31 of 380
```

That reproduces the finding the phase exists for, through the whole pass this
time: the text extracts cleanly and is wrong. The band split is the part a single
"49/49 suspicious" number hides — 44 are unusable throughout, 3 sit in 0.20–0.50
because a real English annex is bound behind a Preeti-encoded Nepali covering
note. `legacy_line_ratio > 0.20` is the classifier's own threshold and was **not**
re-tuned here; the report's bands read it rather than restating it.

### The Docling calibration — frozen subset, comparison harness (Task 10)

Phase 6A screens with pypdf because both engines read the same embedded text
layer, at ~41 pages/s against Docling's ~1–2 on CPU. That is a claim; this is the
instrument that turns it into a measurement.

**The subset is frozen too**, for the same reason the cohort is:
`docs/nrb/phase6a-docling-calibration.json`, **40 PDFs**,
`subset_selection_sha256 = 81d5979ffeee6fbede375917fa6e3de09cb8f0475a397a21b7ad52fa233d90f5`,
bound to parent `1ae297d…`. Drawn by `app/nrb/calibration.py` from the parent
manifest's **own entries** — `build_subset` takes a `Manifest` and nothing else,
no session and no engine, so there is no path by which a key outside the benchmark
can enter the comparison. Candidates are restricted to `resource_type == pdf` (303
of the 400): pypdf never reads a `.docx` or `.xlsx`, so including them would
compare two different pairs of parsers and average the results.

The rank is `sha256(subset_algorithm_version | parent_selection_sha256 |
comparison_key)`, key as tiebreak. **Nothing about a file's state may reach it** —
not fetch status, not what is on disk, not a pypdf verdict, not
`legacy_line_ratio`, not `char_count`, not row order. Picking the files pypdf
already found suspicious would guarantee a rescue rate and measure nothing.
Binding the *parent fingerprint* rather than a free-text seed means the subset is
re-derivable from the two committed files alone, and a different benchmark cannot
draw the same 40. Distribution: cohorts 2023-2026 14 / 2020-2022 13 / 2019 10 /
≤2018 3, across 14 document types and 15 owners; 0 of the 3 currently-fetched
benchmark files landed in it, and that was **not** grounds to redraw.

**The comparison is extraction vs extraction, never pipeline vs pipeline.**
`extraction.docling_extract` walks Docling's own `iterate_items()` stream with no
filtering — deliberately not `parsing.parse_to_chunks`, which layers
`merge_blocks`, `drop_small_blocks`, front-matter skipping and chunking on top, so
a disagreement there could come from RAG's filter rather than from what Docling
read off the page. Both engines' page lists go through one shared
`extraction.result_from_pages`, so `measure_text`, `measure_pages` and `classify`
run identically on both sides at the same thresholds; `legacy_line_ratio >= 0.20`
is untouched. `parser` is recorded as a fact and never branched on.

`app/nrb/calibrate.py` (`run_calibration`, run by `scripts/nrb_calibrate.py`)
resolves the subset the same one-way path the extraction pass uses, dedups to
unique blobs, and **writes nothing**: `nrb_extractions` is the canonical screen at
one `extractor_version`, and bounded experimental calibration data must not enter
it. No migration, no lock (nothing to serialise), no HTTP. One `DoclingEngine`
holds one converter for the whole run with `init_seconds` measured separately —
per-file construction would make the "how much slower" number mostly model
loading, in the direction that flatters pypdf.

**"A rescued B" means B's verdict is not usable and A's is** — `usable` is
`extracted` and nothing else, so `suspicious` and `needs_ocr` are both rescuable.
Not "A read more characters", not "A disagreed": only that one case would change
the choice of screen. The report counts both directions separately, because a
single agreement percentage hides both inside it.

**Adapter smoke, NOT the calibration** (two non-canonical fetched circulars,
offline, 2026-08-15): Docling returns real native text and the same page counts as
pypdf (2 and 14), and reads **more** of it — 1,272 vs 532 chars, 32,493 vs 28,500.
But `devanagari_ratio` is 0.0 on both sides and `legacy_line_ratio` is 1.000/1.000
and 0.9699/0.971, so both engines land on `suspicious/legacy_font_suspected` and
neither rescues the other. Docling cost 4.6 s and 18.5 s against pypdf's 61 ms and
328 ms (57–75×). Two files is an adapter check, not a finding; the finding needs
the frozen 40, and the frozen 40 need Task 13's acquisition.

### Live evidence (scratch DB `local_ai_gateway_p4`, 2026-08-15)

Full detail in `docs/nrb/phase6a-profile.txt` and `docs/nrb/phase6a-calibration.txt`.

**The one live fetch** — `nrb_fetch.py --manifest … --max-bytes 500000000`, run
id 315, 142.1s. 397 selected, **378 fetched, 19 failed** (every one an HTTP 404),
302.3 MB. With the 3 already on disk that is **381 of 400 benchmark files
(95.25%)**. The 19 are a *stated gap*: no substitute file was chosen, no second
scope was run, and neither frozen artifact was touched. 17 of the 19 are 2019
sources and 12 of those are Account-Block enforcement notices under
`/contents/uploads/2019/12/` — a cluster that shape means NRB moved the files.
The calibration slice came out **37 of 40**, and is reported as 37 everywhere.

**The extraction** — 381 fetched files → **381 unique blobs** (no two share
bytes in this cohort) → 381 rows at `native-1`, 0 pass failures. Re-running the
identical command selected **0** pending targets.

```
suspicious    179   47.0%    all legacy_font_suspected
extracted     126   33.1%
needs_ocr      51   13.4%    no_text_layer 28, image_file 22, sparse 1
unsupported    23    6.0%    legacy .doc/.xls — a PARSER gap, not an OCR one
failed          2    0.5%    PdfReadError, PdfStreamError

legacy_line_ratio bands   0:42  >0-<0.20:107  0.20-<0.50:41  0.50-<0.80:17  >=0.80:127
devanagari_ratio          > 0.5: 6 blobs (1.8%)   exactly 0.0: 321 (96.1%)
pages                     4,285 total, 4,136 with text, 28 docs with none
```

**The headline, now measured on a stratified benchmark rather than on 49
circulars: 321 of 334 blobs that produced text contain no Devanagari at all.**
Where the document *is* Nepali, the text arrives as latin codepoints carrying
Devanagari glyphs. 127 blobs sit at `legacy_line_ratio >= 0.80` — unusable
throughout, not merely doubtful.

**The Docling calibration** (37 PDFs, offline from cached models, one converter,
`do_ocr=False, device=cpu`): status and reason agreement **31/37 = 83.8%**,
**Docling rescued pypdf 6, pypdf rescued Docling 0**, both_suspicious 20,
both_extracted 6, both_failed 0. Docling read 40.7% more text and was **76.2×
slower** (2,354.9s vs 30.9s; p95 387.8s for one document).

**Throughput.** pypdf did 4,285 pages in 211.6s = **20.3 pages/s**. But the PDFs
are not the cost: **spreadsheets are 11.5% of the blobs and 79.2% of the CPU
time** (44 blobs, 808.3s; one 8.6 MB workbook took 262.6s alone). A corpus-wide
estimate is driven by workbook count and size, not by page count.

### Two defects the benchmark found, and neither was "fixed" here

**A false positive, and it is systematic.** `05fa82badf94` is a completely
readable *English* statistics table ("Liquidity Absorbing Instruments | Times |
Offer Amount…") classified `suspicious`. Only 19 of its lines were long enough
to judge and 5 tripped the intra-word-symbol rule on formatted numbers like
`2,123,180.00` and dash-filled cells. **This is the same defect the Docling
calibration measured**: all six of Docling's "rescues" are English tables where
pypdf lands just above 0.20 (0.2182, 0.2121, 0.2381, 0.2523, 0.2632, 0.5787) and
Docling just below (0.1675, 0.1250, 0.1961, 0.1517, 0.1579, 0.0214) on
substantially the same text, because Docling's markdown table rows break lines
differently. Read together they are one finding, not two: **native-1 over-flags
tables, and that is the entire measured gap between the two engines.** Docling
rescued *no* legacy-font Nepali document — on all 20 `both_suspicious` files the
engines agree the text is Preeti, and no parser can reverse a font mapping.

**A false negative, and it is worse.** `8df7b02f8a13` is a **spreadsheet** whose
content is Preeti-encoded Nepali (`legacy_line_ratio` 0.2204 over 345/1565
judged lines) — classified `extracted`/`clean`. Cause: `quality.classify` judges
spreadsheets **structurally** (are there cells?) and returns before any
linguistic rule runs, deliberately, because prose rules misfire on statistical
tables. The consequence is that **legacy-font Nepali inside a workbook is
invisible to the detector**: 60 of the 400 benchmark files are spreadsheets, 44
parsed, and all 44 came back `clean`. This is the dangerous direction, and it is
a rule gap rather than a threshold.

**`legacy_line_ratio >= 0.20` was NOT changed and no classifier edit was made.**
Both findings are the evidence for a `native-2` proposal; the plan requires that
to be a separate reviewed decision, re-run over this same frozen manifest with
native-1 and native-2 reported side by side.

### What is deliberately absent

No OCR of any kind (Tesseract, Paddle, EasyOCR, Docling OCR, vision or cloud) and
no legacy-font→Unicode conversion. No chunking, no embeddings, no pgvector writes,
no `documents`/`document_chunks`/`ingest_jobs` rows, no `search_nrb_documents`, no
`LOCAL_TOOLS` entry, no endpoint, no cron. No new runtime dependency —
`app/rag/parsing.py` is untouched (its CPU/no-OCR Docling pinning is load-bearing
for department RAG) and Docling is still never imported at module scope — the
calibration reuses `parsing._docling_converter()` rather than building a second
pipeline that could drift into enabling OCR, and `docling_pipeline_is_native()`
fails loudly if that pinning ever changes. That is a private dependency, kept
deliberately and guarded; promoting it to a public boundary is Phase 7's business.

**Exactly one network request was made in the whole of 6A**: Task 13's single
`nrb_fetch.py --manifest` pass. The extraction pass reads local blobs only, and
the Docling calibration ran with `HF_HUB_OFFLINE=1` against already-cached models
so it could not become a second one. Nothing in 6A wrote to
`documents`/`document_chunks`/`ingest_jobs`, and the calibration wrote nothing at
all — `nrb_extractions` is the canonical screen and bounded experimental
comparison data does not enter it.

### Remaining tasks

**None in Phase 6A.** Task 13 (the live profile) ran on 2026-08-15 and Task 14 is
this write-up. What is left is a *decision*, not a task: whether the two defects
above justify a `native-2` — see §11.9.

Task 10 (the CLI) and Task 12 (Postgres integration tests) landed with Tasks 8–9
rather than separately: the pass is not verifiable without both. The plan's Task
11 (Docling calibration) landed as Task 10 of the follow-up sequence, extended
with the frozen subset artifact and the deterministic comparison report;
`manifest.select_manifest_subset` was **removed** in the process — two subset
selectors that would draw two different 40s is a trap, and
`calibration.select_calibration_entries` is the one that is bound to the parent
fingerprint and restricted to PDFs.

### Evaluation & Improvement (Phase 6A)

1. **Success metric** — the share of extracted blobs whose *status is correct*,
   judged against a hand-labelled sample. Not "did extraction succeed": the whole
   phase exists because success and correctness came apart. **Now measured on the
   400-file benchmark, and the answer is directional.** In the PDF population the
   classifier is safe in the direction that matters — it passed no Preeti document
   as clean and correctly left real Unicode Devanagari alone — but it over-flags:
   **1 of 5 reviewed `suspicious` files was a readable English table**, and the
   Docling calibration puts a floor under that at **6 of 37 PDFs (16.2%)**. In the
   spreadsheet population it is not measured at all, because the rule never runs
   (see §11.9). The old 49-circular figure (49/49 flagged, 0 false negatives) still
   holds for circulars and is now the *narrowest* of the three numbers.
2. **Eval** — the labelled sets are the fixtures in `tests/test_nrb_quality.py`
   and `tests/test_nrb_extraction.py`, plus the by-eye validation Task 13 ran over
   the benchmark: 5 `extracted` + 5 `suspicious` + 5 `needs_ocr` (and, added
   because 23 blobs land there, 5 `unsupported` and both `failed`), chosen by
   lowest `content_sha256` within each status so the sample is reproducible rather
   than "whatever the query returned". Result: **1 false positive, 1 false
   negative**, both written up in §11.9. All NRB suites: **872 passing / 3
   skipped** (the 3 are the opt-in real-Docling smoke tests, `NRB_DOCLING_TESTS=1`).
3. **Feedback capture** — `nrb_extractions` is the log: status, the `reason` rule
   that fired, non-fatal `warnings`, the full metric set in JSONB, a bounded preview
   for eyeballing, and `duration_ms`. Because the row is keyed on the content hash,
   re-running the same version is a no-op and a **version bump re-opens every blob**
   without deleting anything, so a rule change is auditable against its predecessor.
4. **Review loop** — per profiling run: read the status split by year cohort and
   document type before believing any single overall percentage (Phase 3's 71.6%
   type coverage was a misleading average for exactly this reason — 2019 alone is
   47.5%). Compare pypdf against Docling on the same cohort and count rescues in
   both directions. Re-tune the legacy threshold only against a *new* cohort, never
   against the one it was fitted on — which is exactly why Task 13 stopped at
   *reporting* the two defects rather than fixing them: the benchmark that found
   them cannot also be the benchmark that validates the fix. A `native-2` re-runs
   over this same frozen manifest and reports both versions side by side.

### 11.9 The Phase 6B gate

**What 6A hands over.** The work queue is a query, not a document:
`SELECT * FROM nrb_extractions WHERE extractor_version = 'native-1' AND status IN
('needs_ocr','suspicious')`. On the benchmark that is **230 of 381 blobs (60.4%)**
— 179 legacy-font suspected and 51 needing pixels. A second, separate queue is
`status = 'unsupported'` (23 blobs, 6.0%), which needs a **parser**, not OCR.

**Three things 6B must not assume.**

1. **The `suspicious` count is an upper bound, not a measurement.** At least 6 of
   37 calibration PDFs (16.2%) and 1 of 5 hand-reviewed files are readable English
   tables that native-1 over-flagged. Sizing an OCR or font-conversion programme
   off 179 would over-buy.
2. **The `clean` count is not a lower bound for spreadsheets.** 44 benchmark
   spreadsheets were classified `extracted` by a rule that never reads their text,
   and at least one of them is Preeti-encoded Nepali. **Every spreadsheet in the
   corpus is currently unclassified with respect to legacy fonts**, and the query
   above silently excludes all of them. This is the single most consequential gap
   6A leaves.
3. **Docling is not the remedy for either.** It rescued 6 files and every one was
   a table pypdf mis-flagged; it rescued zero legacy-font documents, and it cannot
   — the glyph mapping lives in the embedded font. At 76.2× the cost it buys no
   recovery, so it stays a Phase 7 chunking dependency, not a screen.

**The recommendation, from the measurements only.** Before any OCR is priced,
do the cheap classifier work: a table-aware guard on the intra-word-symbol rule
and a linguistic path for spreadsheets, shipped as `native-2` and re-run over
this same frozen manifest. That directly addresses the one defect that inflates
the 6B queue and the one that hides work from it, and it costs a 17-minute
re-extraction. Only then is 179 (or whatever native-2 says) a number worth
buying OCR against. Legacy-font → Unicode conversion, not OCR, is the likely
remedy for most of it: these documents *have* a text layer, it is simply
mis-mapped, and 127 blobs sit at `legacy_line_ratio >= 0.80`.

**Still undecided, deliberately:** which OCR engine (if any), whether a Preeti
mapping table can be built or licensed, whether `.doc`/`.xls` justify
antiword/xlrd, and whether the 19 un-fetchable benchmark files indicate a
corpus-wide 404 rate worth a re-sync. None of those are answerable from 6A's
evidence, which is why 6A did not answer them.

### Known, and not caused by this work

Running the RAG suites against the scratch DB fails
`tests/test_rag_reingest_integration.py::test_department_filter_restricts_the_set`
(`assert 69 == 2`): the test assumes an empty `documents` table and
`local_ai_gateway_p4` holds 69 non-archived rows from earlier RAG work. It fails in
isolation too, so it is DB-state dependence rather than test-order pollution. Same
family as the migration-lineage situation in §9.10 — a consequence of NRB work
living on a scratch database, and resolved when that is reconciled.

## 12. Phase 6B Task 1 — legacy-font conversion, EVALUATED (not deployed)

**State as of 2026-08-15: evaluated on the frozen Phase 6A benchmark, with a
recommendation. Nothing is wired into production.** `quality.classify` is
unchanged, `legacy_line_ratio >= 0.20` is unchanged, the 381 `native-1` rows are
unchanged, no OCR ran, nothing was chunked or embedded, and the whole pass made
**zero network requests**.

Evidence, committed verbatim:

* **`docs/nrb/phase6b-legacy-conversion-evaluation.txt`** / **`.json`** — the
  generated report: cohort identity, per-mapping counters, before/after per
  document, negative controls, spreadsheets, performance.
* **`docs/nrb/phase6b-manual-validation.txt`** — the by-eye work: rendered pages
  compared against converted text, and the three corrections the controls forced.
* **`docs/nrb/phase6b-lexicon.json`** — the frozen vocabulary the guards use
  (`cc1fec3f…`, 6,655 English / 343 Nepali words).

### The five answers

**A. Can Preeti-encoded NRB text be deterministically recovered? YES.** On
`5e0ca4500f8f` the converted output is **line-for-line identical to the rendered
page**, Nepali dates and dandas included; `legacy_line_ratio` 1.0000 → 0.0000,
`devanagari_ratio` 0.0000 → 0.9740. Across the 30-PDF cohort, 8 recovered, 8
partial, 14 unresolved — but that split is almost entirely explained by severity
(below).

**B. Can a valid conversion be told apart from an English-table false positive?
YES, but not by any signal the original brief proposed.** All seven English-table
controls reconstruct **byte-identically, 0 lines converted, 0 preservation
failures**. Getting there took three measured corrections — see §12.2.

**C. Are there legacy documents Preeti cannot recover? YES, and one is
visually confirmed.** `9892625b8531` is a perfectly readable Nepali circular
whose text layer extracts as `\.qfr dqT frr+f,{ kt+r.r` — not a keystroke
encoding at all, and the same `ffi`-run shape §11 quoted. Preeti cannot touch it;
the validator refused every line rather than emitting confident nonsense. These
need an `unknown_legacy_encoding` state and OCR.

**D. Is line-level conversion safe for mixed English/Nepali PDFs? YES**, with
per-line routing, per-cell for spreadsheets, and byte-exact reconstruction of
everything not converted (line terminators included).

**E. Should 6B route native → convert → validate → OCR-on-failure?**
**Yes, but ONLY above `legacy_line_ratio >= 0.80`, and not below it.** That is
the one number this evaluation adds, and it is the recommendation.

### 12.1 What the severity split actually says

| band | recovered | partial | unresolved |
|---|---|---|---|
| 0.20–0.50 | 1 | 0 | 9 |
| 0.50–0.80 | 0 | 6 | 4 |
| **0.80–1.00** | **7** | 2 | 1 |

Conversion works where the document is unambiguously legacy and does almost
nothing where it is not — which is exactly what §11.9 predicted, from the other
direction: the 0.20–0.50 band is largely **native-1's over-flagged English
tables**, and the guards correctly decline to convert them. The band is not a
conversion failure; it is the classifier defect 6A already reported, seen through
a second instrument.

So the 6B queue is not 179 blobs. On the benchmark, **127 of 381 sit at >= 0.80**,
and that is the population deterministic conversion can serve today.

### 12.2 Three corrections the negative controls forced, and why they are the design

Each was measured, and each earlier state is recorded in the manual-validation
file because it is the evidence for the rule that replaced it.

1. **Output validation cannot work; the guard must run on the INPUT.**
   `Instruments Times Offer Amount` converts to `mक्ष्लकतचगभलतक mत्ष्भक इााभच
   mब्यगलत` — 91% Devanagari, `legacy_line_ratio` 0.2632 → 0.0, character count
   preserved. Every after-the-fact signal reports a successful recovery of a
   table that has just been destroyed. Hence `lexicon.is_confidently_english`,
   applied to the raw line before any converter sees it.
2. **`devanagari_ratio` is anti-correlated with correctness for mapping choice.**
   `@)^%` is `२०६५` under Preeti and the nonsense `द्दण्टछ` under
   FONTASY_HIMALI_TT — and the wrong one scores **0.9808 against 0.9796**. Hence
   `devanagari.py` (illegal clusters, latin residue) and a vocabulary score.
3. **An unconfirmable conversion is only trustworthy in a clearly legacy
   document.** Both the "line too short for the detector to judge" branch and the
   "structurally fine but vocabulary can't vouch" outcome are gated on
   `UNJUDGED_MIN_LEGACY_RATIO = 0.80` — 6A's own top severity band, not a value
   invented here. Without the gate, 5 of 7 English controls lost lines
   (`No.`, `Net`, `ago.`, `Reporting Stats`); with it on unjudged lines only, 4
   of 7 still lost numeric rows (`MachhapuchureBank Ltd. 3500.00`). It costs the
   0.50–0.80 band its recoveries, and that trade is deliberate: a missed heading
   is a gap, a converted English cell is corruption.

Two more traps, both caught by tests rather than by eye. `extraction.py` renders a
spreadsheet row as `" | ".join(cells)` and **`|` is a Preeti codepoint mapping to
`्र`**, so conversion is per CELL. And **Preeti maps ASCII digits to Devanagari
digits** — `1,234.00` → `ज्ञ,द्दघद्ध।ण्ण्`, which passes every validation rule while
destroying a number — so routing requires the detector's own `LEGACY_MIN_LATIN`
share before an unjudged unit is a candidate.

### 12.3 The mapping question

`Preeti` is the best mapping on **25 of 30** documents; Kantipur and Sagarmatha
are near-identical to it, and FONTASY_HIMALI_TT / PCS NEPALI are clearly wrong
here (acceptance 0.3727 / 0.3911 against Preeti's 0.5747). Every mapping is run
from the **same original text** and never chained — one mapping's corruption must
not become another's input.

**Automatic mapping identification is NOT yet feasible**, and the reason is
specific: the Nepali half of the lexicon is built from the only genuine Unicode
Devanagari in the benchmark — **6 blobs, 343 words** — so the measured separation
between a right and a wrong mapping on the same line is 0.125 against 0.100. Real,
repeatable, far too narrow to decide on. Vocabulary is therefore a *confirming*
signal here, never a veto, and the three documents where a non-Preeti mapping won
on score all sit in the low-severity band where the score means least. A richer
Nepali lexicon is the prerequisite for automatic mapping selection.

### 12.4 Spreadsheets, on their own denominator

Six benchmark workbooks, reported apart from the PDF cohort throughout. The known
Preeti workbook `8df7b02f8a13` recovers correctly per cell (`Plss[t jflif{s
cfly{s ultljlw @)&^÷&&` → `एकिकृत वार्षिक आर्थिक गतिविधि २०७६/७७`), 54 cells
accepted of 26,549. The other five are genuinely numeric and convert essentially
nothing, which is the correct answer.

This does **not** close §11.9's spreadsheet gap. It shows the converter works on
cell strings; the classifier still never reads them, so the corpus's spreadsheets
remain unclassified for legacy fonts. That is still `native-2`'s job.

### 12.5 A THIRD false-negative class, found here

`84862ab6866a` is `extracted`/`clean` with `devanagari_ratio` 0.6396 and
`legacy_line_ratio` 0.0444 — and 29 of its lines are real Preeti that convert
correctly (`kb M ;xfos lgb]]{zs` → `पद : सहायक निर्देशक`). It holds genuine
Unicode Devanagari and genuine Preeti **in one file**, and the Unicode majority
dilutes the Preeti minority below the 0.20 flag. Two of the six high-Devanagari
benchmark blobs are this shape. Reported, not fixed — the same discipline Task 13
followed, and more evidence for `native-2`.

### 12.6 Cost

**109,865 lines in 38.4s = 2,860 lines/s**, 3.9 document-mappings/s, for the
whole cohort against all five mappings. Against 6A's measured pypdf (4,285 pages
in 211.6s) and Docling (37 PDFs in 2,354.9s), conversion is cheap enough to sit
in front of OCR without being priced. **No OCR timing is claimed, because none
was measured.**

### 12.7 The dependency, and the licence gate

`npttf2utf==0.3.7`, declared in **`requirements-nrb.txt`** — which `Dockerfile`
does not install, so the API image cannot acquire it by accident (same structural
guarantee as docling and `requirements-worker.txt`). `app/nrb/legacy_font.py` is
our own adapter behind a `LegacyFontConverter` Protocol; the import is lazy and
absence raises `ConverterUnavailable` rather than silently no-oping. **Nothing
from the package is copied into this repository** — no mapping table, no rule
set, no code; `map.json` is read from the installed package at runtime.

**npttf2utf is GPL-3.0, and that is an OPEN GATE, not a resolved question.** It
was chosen because it is the only evaluated converter that is *correct*: the MIT
alternative, `preeti-unicode-converter` 0.1.1, mangles matra reordering
(`आर्थकि` for आर्थिक, `माैदि्रक` for मौद्रिक) and drags pymupdf + reportlab.
GPL-3 obligations attach to **distribution**, not internal use, so this is fine
for evaluation and for a gateway we operate — and it **must be resolved before
this gateway is distributed to a client**. The user's decision (2026-08-15): use
it now behind the adapter, do not vendor its tables, treat the licence as a
deployment gate, and consider an independently-derived converter repo later.

### 12.8 What is deliberately absent

No production routing (`quality.classify` is untouched and the pipeline is not
wired), no `native-2`, no threshold retune, no OCR of any kind, no chunking, no
embeddings, no pgvector writes, no `search_nrb_documents`, no `LOCAL_TOOLS`
entry, no endpoint, no migration, no `alembic stamp`, nothing on
`feat/rag-source-citations`, and no write to the real `local_ai_gateway`
database. Converted text is **not persisted anywhere** — not in Postgres, not on
disk beyond the bounded excerpts in the evidence files.

### 12.9 Evaluation & Improvement (Phase 6B Task 1)

1. **Success metric** — the share of legacy-suspected blobs converted to
   *correct* Unicode Nepali, with **zero** corruption of non-legacy content. Both
   halves are load-bearing: a converter that recovers Nepali and damages English
   tables is a net loss, because the corpus's English is currently fine.
   Measured: **7 of 10 blobs at `>= 0.80` recovered; 0 of 14 negative controls
   damaged.** Below 0.80, recovery is 1 of 20 — reported, not hidden.
2. **Eval** — 51 tests across `tests/test_nrb_legacy_{conversion,cohort}.py`,
   whose fixtures are real benchmark text with hand-verified Unicode, plus the
   30-PDF frozen cohort (`b977464d…`, drawn from the 400-file benchmark by
   content hash before any converter ran) and the 14 named negative controls.
   All NRB suites: **923 passing / 3 skipped** (the 3 are the opt-in real-Docling
   smoke tests), up from 872/3.
3. **Feedback capture** — the evaluation JSON is the log: per document, per
   mapping, every disposition counted, every validation reason recorded, and
   bounded before/after excerpts. Nothing is persisted to Postgres, deliberately
   — this is an experiment, and `nrb_extractions` is the canonical screen.
   Re-running the command over the same cohort reproduces the file byte for byte.
4. **Review loop** — before any production routing, (a) a Nepali reader confirms
   the four flagged manual cases; (b) the licence gate in §12.7 is resolved; (c)
   the 0.80 threshold is re-measured against a *different* cohort than the one it
   was chosen on — the same rule §11.9 sets for the legacy threshold, and it
   applies to this number too. Re-run whenever the lexicon or the converter pin
   changes, and compare the per-mapping table.

### 12.10 The gate to Phase 6B Task 2

**Recommended order, from these measurements only.** First `native-2`, still —
this evaluation strengthens §11.9's case rather than replacing it, and has now
added a third false-negative class (§12.5) to the spreadsheet gap. Then wire
conversion for `legacy_line_ratio >= 0.80` only, with everything below it and
every rejection falling through to the OCR queue. A richer Nepali lexicon is the
prerequisite for extending it further or for automatic mapping identification.

**Still undecided, deliberately:** which OCR engine, whether `unknown_legacy_
encoding` deserves its own status in the `nrb_extractions` vocabulary, whether
the 0.50–0.80 band is better served by a richer lexicon or by OCR, and the
licence gate. None is answerable from this evidence, which is why none is
answered here.

## 13. Phase 6B Task 2 — `native-2`, the routing classifier (MEASURED)

**State as of 2026-08-15: native-2 is implemented, re-run over the frozen Phase
6A benchmark, and recommended as the routing gate.** It **classifies only** — it
converts nothing, never invokes `npttf2utf`, and works on a machine where that
package was never installed. `native-1`'s 381 rows are untouched and sit beside
native-2's 381 for comparison.

Evidence, committed verbatim:

* **`docs/nrb/phase6b-native2-comparison.txt`** — the generated side-by-side:
  status and reason transition matrices, the seven English controls, the
  spreadsheet population, the minority regions, severity distributions, and every
  changed blob with the metrics that explain it.
* **`docs/nrb/phase6b-native2-manual-review.txt`** — the by-eye work, the
  independent cross-check against Task 1, and one defect native-2 introduced.

### The three answers

**A. Did native-2 eliminate the English-table false positives without hiding real
legacy Nepali? Yes, on both halves.** All **7 of 7** known English tables are
corrected (`legacy_line_ratio` 0.2121–0.5787 → unit ratio 0.0000–0.1242). And the
converse holds independently: of the 16 documents Phase 6B Task 1 recovered or
partially recovered with a real converter, **15 are still `legacy_font_suspected`**
— the one exception recovered 4% Devanagari, i.e. Task 1's label was generous.

**B. Can native-2 detect legacy text in spreadsheets? Yes — 0 → 11.** native-1
found legacy in **0 of 44** parsed benchmark workbooks because `quality.classify`
returns on structure alone. native-2 scores their **cells** and flags 11,
including Phase 6A's known false negative `8df7b02f8a13` (unit ratio 0.9663 over
623 judged cells).

**C. Can native-2 detect a minority Preeti region inside a Unicode document?
Yes — 4 cases, including the regression case.** `84862ab6866a` moves to
`suspicious` with the warning `minority_legacy_region`, and its
`legacy_line_ratio` is **still 0.0444** — the global threshold was not lowered.

**D. How many blobs would native-2 send to a future conversion stage?** 154 of
381 are `legacy_font_suspected`; **144 of them sit at unit ratio ≥ 0.80**, which
is the band Task 1 measured as reliably recoverable. 4 sit at 0.50–0.80, 2 at
0.20–0.50, and 4 arrive only through the region rule.

**E. Is native-2 trustworthy enough to be the production routing gate?** Yes, on
the evidence here, with the Nepali confirmation in §13.6 outstanding.

### 13.1 The transition matrix

| native-1 | → native-2 | blobs |
|---|---|---|
| `clean` | `clean` | 105 |
| `clean` | **`legacy_font_suspected`** | **15** |
| `legacy_font_suspected` | **`clean`** | **40** |
| `legacy_font_suspected` | `legacy_font_suspected` | 139 |
| everything else | unchanged | 76 |

Nothing moved into or out of `needs_ocr` (51), `unsupported` (23) or `failed` (2).
Those rules are native-1's, carried over unedited: re-deciding them would make the
comparison unreadable.

### 13.2 What actually changed, and why each change exists

**Not** a new heuristic. The shape signals are native-1's own — vowel-less tokens,
intra-word symbols, intra-word case switches, **at the same thresholds**. What
changed is the *signals* and the *unit*, each traced to a measured defect.

The English-table cause was measured before anything was written. Over 355 flagged
lines in the seven known tables, the **intra-word-symbol rule fired on 89.3%**
while the vowel-less rule fired on 2.5% and case switches on 10.4%. So the symbol
signal was the defect, and it got three narrow corrections:

1. **Symbols count only in tokens that contain letters.** `2,123,180.00` is a
   formatted number, not a glyph-mapped word.
2. **A well-formed compound is not glyph-mapped.** `FIU-Nepal`, `AML/CFT`, `F/Y`
   split into letter runs that are each pronounceable, an acronym, or a single
   capital. `q_fie(` and `4{i-4;f` do not.
3. **Acronyms are not judged on vowels.** `NRB`, `SLF`, `IRC` have none because
   that is what an acronym is; `iv. NRB Bond - - -` scored 0.50 against a 0.30
   threshold. All-caps tokens leave the vowel test's numerator *and* denominator.

None of the three can shelter Preeti: glyph-mapped text is relentlessly mixed-case
and vowel-poor across whole lines, not in isolated acronyms.

### 13.3 Three-state units, and why `unjudged` had to exist

`app/nrb/units.py`. Native-1's detector answers `True`/`False`/`None` but
`legacy_line_ratio` collapses that to two halves. Native-2 keeps three:

```
legacy_candidate    glyph-mapped shape; needs recovery
trusted_nonlegacy   positively identified as fine (english_like, unicode_devanagari)
unjudged            no linguistic evidence either way (empty, too_short, numeric)
```

A numeric row, a row of dashes and a page number are not evidence that a
document's Nepali is fine, and counting them as such is precisely how 57 Preeti
lines hid inside `84862ab6866a`'s Unicode majority.

**`english_like` is a positive identification made from ORTHOGRAPHY, never a word
list.** Phase 6A already proved the cheap version unsafe — a document-level
`stopword_rate` gate missed 7 of 49 real circulars, one scoring 0.248, *higher*
than real English prose, because glyph-mapped text is full of short ASCII tokens
that match stopwords by chance. So the rule is: enough real words, essentially all
vowel-bearing, no mid-word case switching. Preeti runs 0.43–0.54 vowel-less and
0.40–0.60 case-switching and cannot reach that by accident.
`test_a_few_accidental_english_stopwords_do_not_exempt_preeti` locks it.

### 13.4 Spreadsheets are judged per CELL, and the separator is why

`extraction.py` renders a row as `" | ".join(cells)` so there is something to
store — and **`|` is a Preeti codepoint that maps to `्र`**. A rendered row is
unsafe to score and unsafe to convert, so `_extract_spreadsheet` now collects the
individual cells alongside the rendered text and hands *those* to the classifier.
Cell identity is preserved so the future converter can work per cell too.

Structure is still checked first — an empty workbook is still `empty_spreadsheet`
— but it no longer *ends* the classification. Valid workbook structure and
trustworthy cell text are two different claims.

What this found is not scattered accidents: `156a7dab82ce`, `1ac8962b1214` and
`36661db8f086` are the **same Microfinance progress report from three different
quarters**, with byte-identical Preeti row labels
(`sfo{If]q ePsf] lhNnf ;+Vof` = कार्यक्षेत्र भएको जिल्ला संख्या). A recurring
statistical series, published quarterly, entirely invisible to native-1.

### 13.5 The minority-region rule, and what it is not

Three conditions, all required: **≥10 legacy units**, a **contiguous run ≥3**, and
**≥50% of the *contested* units** — those that are neither Unicode nor
positively-English. The contested denominator is the load-bearing part: it
measures a Preeti section against the other *latin* text rather than against a
whole Unicode document.

Each condition alone is nonsense on this corpus — a count alone flags any long
document with odd lines, a run alone flags a stray table fragment, and a ratio
alone is the global measure that already missed the case. An `unjudged` unit does
**not** break a run (blank lines sit inside real legacy regions constantly); a
positively-clean unit does.

**The global 0.20 threshold was NOT lowered**, and
`test_the_global_threshold_was_not_lowered_to_achieve_that` asserts it. Lowering
it would have traded this false negative for a flood of false positives and
destroyed the meaning of Task 1's severity evidence.

### 13.6 Manual review, and an independent instrument

Deterministic samples — the five lowest `content_sha256` in each direction:

* **5 of 5 `suspicious → clean` correct.** All readable English statistics tables
  (Treasury Bills ownership, Open Market Operations, Monetary Operation summary).
* **5 of 5 `clean → suspicious` correct.** All spreadsheets with genuine Preeti
  row labels. *Nepali reading provisional — see below.*
* **0 newly observed false positives, 0 newly observed false negatives.**

The strongest evidence is not a hand reading. **14 of the 40 `suspicious → clean`
blobs were also in Task 1's conversion cohort**, where a real Preeti converter was
run over them: all 14 recovered `devanagari_ratio ≤ 0.042` — there was no Preeti
in them to recover. **Agree 14, disagree 0.** A shape classifier and a font
converter are independent instruments and they concur.

**Outstanding:** a Nepali reader should confirm the five spreadsheet cases and the
Preeti reading of `8df7b02f8a13` before native-2 becomes the production gate.
Sections A, C and the cross-check are script-independent.

### 13.7 A defect native-2 introduced, found by the benchmark

Marking uninformative units `unjudged` is right, and it shrinks the denominator.
The first native-2 run flagged **six** documents on the strength of a *single*
legacy unit out of three or four judged — a ratio over four units is not a
measurement, and native-1 never saw this because its denominator included every
numeric row. Fixed with `MIN_JUDGED_FOR_RATIO = 8`, plus
`MIN_LEGACY_ABSOLUTE = 4` so a short *wholly*-Preeti document still flags. The
pass was deleted and re-run from scratch; all six are clean. Recorded rather than
quietly corrected — it is exactly the kind of defect a rule change introduces and
only a benchmark finds.

### 13.8 Version isolation

`EXTRACTOR_VERSION` is still `native-1` and still the default. The version selects
the **classifier only** — the same pypdf/python-docx/openpyxl call, the same text,
the same `quality` metrics — so a `native-2` row is the same bytes read the same
way and judged by different rules, which is the only thing that makes the two
comparable. Every native-1 metric is present and identical in a native-2 row
(`test_native2_keeps_every_native1_metric_and_adds_its_own`), and
`legacy_line_ratio` still means exactly what Task 1's severity evidence says it
means.

New metrics live in the existing `metrics` JSONB — **no migration** — namespaced
so a reader can tell which numbers are new: `unit_total`, `unit_judged`,
`unit_legacy_candidates`, `unit_trusted`, `unit_unjudged`, `unit_english`,
`unit_unicode`, `unit_numeric`, `unit_legacy_ratio`,
`unit_contested_legacy_ratio`, `unit_max_legacy_run`, `minority_legacy_detected`,
and `spreadsheet_text_cells`.

The status/reason vocabulary is unchanged. There is deliberately **no `preeti`
reason**: native-2 detects a legacy *candidate* and never claims to know the font
mapping. A minority-region verdict names itself in `warnings`, because a reader
seeing `suspicious` against a 0.0444 ratio would otherwise think the classifier
had misfired.

### 13.9 What is deliberately absent

No conversion of any kind and **no `npttf2utf` invocation** —
`test_no_legacy_converter_is_invoked_during_classification` monkeypatches the
whole adapter to explode, and `test_routing_does_not_import_the_converter_module`
parses the AST of both new modules to prove neither references it. That is not
hygiene: the converter is GPL-3 and excluded from the API image, and a classifier
that needed it would drag the licence gate into every deployment.

No `>= 0.80 → convert` rule, no `converted` status, no mutation of extracted text.
No OCR, no chunking, no embeddings, no pgvector, no `documents`/`document_chunks`/
`ingest_jobs` rows, no `search_nrb_documents`, no `LOCAL_TOOLS` entry, no
endpoint, no migration, no `alembic stamp`, nothing on
`feat/rag-source-citations`, and no write to the real `local_ai_gateway`. **Zero
HTTP requests** — the pass reads local blobs only, and the 19 benchmark 404s
remain a stated gap that was not retried and not substituted.

### 13.10 Evaluation & Improvement (Phase 6B Task 2)

1. **Success metric** — the share of blobs whose *status is correct*, in both
   directions, judged against hand-labelled cases. Measured: **7 of 7** English
   controls corrected, **15 of 16** Task-1-recoverable documents retained, **11**
   spreadsheets recovered from a population where native-1 scored 0 of 44, and
   **0** newly observed false positives or negatives in a deterministic 10-case
   review.
2. **Eval** — 50 tests in `tests/test_nrb_native2_routing.py`, whose fixtures are
   text *shapes* from the frozen benchmark rather than document identities (a
   test that special-cased `05fa82badf94` would pass while the rule stayed
   broken), plus the full 381-blob re-run and the independent Task 1 cross-check.
   All NRB suites: **973 passing / 3 skipped**, up from 923/3.
3. **Feedback capture** — both versions' rows coexist in `nrb_extractions`, keyed
   on `(content_sha256, extractor_version)`, so every transition is auditable
   against its predecessor forever and `nrb_native2_compare.py` reproduces the
   comparison byte for byte. The `unit_*` metrics are the routing explanation:
   any verdict can be traced to counts a human can check.
4. **Review loop** — before native-2 becomes the production gate, get the Nepali
   confirmation in §13.6. Re-run the comparison whenever a rule changes and read
   the transition matrix, not the headline. The rule §11.9 sets still binds: a
   threshold must not be re-tuned against the cohort that fitted it — so the
   0.80 conversion-queue figure below is a *report*, not a policy, and wants a
   fresh cohort before it becomes one.

### 13.11 The gate to the conversion-routing task

**Not decided here, deliberately.** Task 1's evidence pointed at
`legacy_line_ratio >= 0.80`, and native-2 changes which units are judged, so that
number had to be re-measured rather than promoted. Re-measured, over native-2's
own unit ratio, across the 154 blobs it calls legacy:

| band | blobs |
|---|---|
| ≥ 0.80 | **144** |
| 0.50–0.80 | 4 |
| 0.20–0.50 | 2 |
| via the region rule only | 4 |

The distribution is far more bimodal than native-1's (127 / 16 / 36), which is the
point: the middle band was mostly mis-flagged tables and they are gone. Whether
144 becomes the conversion queue is the next task's decision.

**Still undecided:** which OCR engine, whether `unknown_legacy_encoding` deserves
its own `reason` value, whether the 4 region-detected documents should be
converted per-region rather than per-document, and the GPL-3 licence gate from
§12.7. None is answerable from this evidence.

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

---

## 14. Phase 6B Task 3 — independent holdout validation (native-2 VALIDATED, conversion not wired)

**Date:** 2026-08-16. **Continues** commit `2a6b498`. **Commits:** `ddc5f2d` (the
frozen holdout, pre-network) and the evidence commit that follows it.

This phase answers one question: *does native-2 hold up on NRB files that never
influenced it, and is `unit_legacy_ratio >= 0.80` safe enough to become the
production conversion gate?*

Everything in §11–§13 was measured on the Phase 6A benchmark, and that benchmark
shaped native-1, native-2, the table guards, the spreadsheet rule, the
minority-region rule, `MIN_JUDGED_FOR_RATIO`, `MIN_LEGACY_ABSOLUTE` **and** the
`>=0.80` band itself. Validating any of that against the same 400 files would be
circular, so none of it was.

### The six answers

**A. Is native-2 still trustworthy on unseen files? Yes**, with one new
false-positive class named below. On 142 previously unseen blobs it flagged 67
`legacy_font_suspected`, and the document types it flagged are exactly the ones
NRB publishes in Nepali: **circular 9/9, rule_bylaw 5/5, enforcement_action 4/4,
forex 3/3, monetary_policy 3/3**. It did not flag a single image (0/6) or `.docx`
(0/3).

**B. Of the high-confidence queue, how much is genuinely legacy?** Of **56**
blobs at `unit_legacy_ratio >= 0.80`: **0 English false routes**, 52 confirmed
glyph-mapped with the converter producing Unicode, 4 unresolved. Measured the
strict way — by asking whether the units native-2 *itself* flagged are readable
English — the false-positive count in the queue is **0 of 56**.

**C. Of genuine Preeti at high confidence, how much does npttf2utf recover?**
Of the 56: **36 `recovered`, 16 `partial`, 4 `unresolved`** — 52/56 (92.9%)
produced usable Unicode, 36/56 (64.3%) cleanly. Recovery is not correctness; see
the reader caveat.

**D. Does `>=0.80` survive without tuning? Yes.** Not one threshold was moved
before, during or after this cohort. The band is *sharper* on unseen data than on
the benchmark: 56 of 67 flagged blobs sit at `>=0.80`, and the four false
positives found all sit at **0.483–0.538**, below the gate.

**E. Does anything force `native-3`? Not for the queue.** One real defect was
found (§14.3) and it is entirely outside the conversion gate. Fixing it is a
`native-3` change with a new cohort; shipping the gate does not wait on it.

**F. Enough evidence to build production conversion routing? Yes**, for the
`>=0.80` queue only, and with the Nepali-reader confirmation in §14.5 outstanding.

### 14.1 The cohort, and why it is independent

`docs/nrb/phase6b-routing-holdout.json`, fingerprint
`6344e674f788808ab02f46218e59a76c215c0644cb95abbbf8212d45d400a970`, 150 files,
seed `phase6b-routing-holdout-v1`, floor 1, 2019 cap 45, algorithm
`nrb-stratified-v1` unchanged.

Independence is enforced in the sampler, not by hand. `stratified_sample` gained
`exclude_keys`, which removes candidates **before stratification** — so the
withheld files never touch allocation or strata — and the excluded SET is hashed
into the sampler parameters as `exclude_keys_sha256`. That is stronger than
recording a count: swapping *which* 400 keys were withheld, at the same count,
changes the fingerprint. Drawn from 17,866 candidates = 18,266 catalog files
minus exactly the 400 Phase 6A keys.

`intersection(phase6a, holdout) == 0`, asserted in
`tests/test_nrb_phase6b_holdout.py` and re-asserted at run time by the validator,
which aborts rather than produce a contaminated report.

The manifest was **committed before any network access** (`ddc5f2d`), which is the
same discipline Phase 6A used and the reason a redraw would be visible.

One honest difference from §11: Phase 6A was drawn with `floor=2`,
`max_cohort_share=1/1`, 2019 cap 120. The holdout uses `floor=1`, share `3/10`,
cap 45. Different parameters, deliberately — a 150-file cohort needs a tighter cap
— so the two are comparable in *kind*, not parameter-for-parameter.

### 14.2 What the holdout measured

| | fetched | native-2 |
|---|---|---|
| requested | 150 | |
| fetched | 142 | 142 rows |
| HTTP 404 | 8 | in the denominator, never substituted |

The 8 failures are genuine NRB 404s (six 2019 account-block notices, one 2066/67
circular, one `notice.jpg`). Per the task's own rule they stay in the denominator.
A second native-2 pass selects **0** — the pass is idempotent.

| status | n | | reason | n |
|---|---|---|---|---|
| suspicious | 67 | | legacy_font_suspected | 67 |
| extracted | 49 | | clean | 49 |
| needs_ocr | 17 | | no_text_layer | 11 |
| unsupported | 8 | | image_file | 6 |
| failed | 1 | | no_native_parser | 8 |
| | | | parser_error | 1 |

Bands over `unit_legacy_ratio` — **native-2's own metric, not native-1's
`legacy_line_ratio`**, and the distinction is load-bearing because the two
genuinely differ on exactly the population this phase cares about:

| band | blobs |
|---|---|
| ≥ 0.80 | **56** |
| 0.50–0.80 | 9 |
| 0.20–0.50 | 1 |
| region rule only | 1 |

Compared descriptively with Phase 6A (144 / 4 / 2 / 4 of 154): the holdout is
legacy-denser (47% vs 40% of blobs flagged) and its middle band is proportionally
larger. That is corpus composition, and **nothing was changed because of it**.

The `.xls` gap is worth naming: 26 manifest spreadsheets are 20 `.xlsx` plus **6
legacy OLE2 `.xls`**, all `unsupported`/`no_native_parser`. Of the 20 parseable, 7
flagged legacy. That is a parser gap, not a classifier gap, and it is new
information about the corpus.

### 14.3 The defect the holdout found — English accounting templates

**Four spreadsheets are mis-routed**, and they are the same NRB financial-statement
template repeated: `690c193dc4a9`, `971aa739f844`, `c38524fc9404`, `ed3bd543c54a`.
Every one of their flagged units is readable English:

```
Profit & Loss A/c
5.2.Pension & Gratuity Fund
5.7.Payable to Cumulative leave of staff
2.2.in "A"Class Licensed Institution
```

**14 of 14 flagged units English, in all four.** The cause is the same
intra-word-symbol signal §13.2 corrected for prose tables, meeting a shape those
corrections do not cover: a numbered accounting label (`5.2.Pension`) is one token
containing letters and periods, and `A/c` is a two-run compound whose runs are a
single capital and a vowel-less pair. The rest of each sheet is numeric, so those
14 units land in a small judged denominator and drag the ratio to ~0.5.

**This is reported, not fixed.** Per the task's rule and §11.9's own logic, a
classifier change after seeing holdout results makes this cohort development
evidence. The fix belongs to `native-3` with a fresh cohort. Its likely shape:
exempt `<digit>.<digit>.<Word>` outline labels and treat `A/c`-style abbreviations
as compounds — both narrow orthographic corrections, both testable on shapes.

**It does not touch the conversion gate.** All four sit at 0.483–0.538, and the
high band contains **0** members of this class. That is the difference between a
caveat and a blocker.

### 14.4 What did NOT go wrong

* **Genuine Unicode is safe.** The Unicode control (`842ab02fb3fa`, 0.8439
  Devanagari) is `clean`, and the converter changed nothing in it.
* **Input guards hold.** Six negative controls with zero legacy units — five
  English/numeric, one Unicode — all reconstruct with **0 units converted**.
* **Spreadsheet detection works on unseen data.** 7 of 20 parseable workbooks
  flagged, including three large research workbooks at unit ratio 0.969–0.993
  whose `legacy_line_ratio` is 0.15–0.19 — i.e. **native-1 would have called all
  three clean**. This is §13.4's rule earning its place on files it never saw.
* **The small-denominator floors are doing their job.** No document was flagged on
  a handful of units; the four false positives have 14 flagged units each, so
  `MIN_JUDGED_FOR_RATIO = 8` / `MIN_LEGACY_ABSOLUTE = 4` were not the cause and
  neither is implicated.
* **The minority rule is not trigger-happy.** Exactly **1** blob was routed by the
  region rule alone (`da0c680d072d`, unit ratio 0.0089).

### 14.5 False negatives, and what a reader must confirm

Of 49 `clean` documents, **36 carry at least one legacy unit**. Almost all are
scattered singletons at ratios of 0.01–0.06 — noise the router was right to
ignore. Three deserve a look: `d74b592c894a` (59 units, run 14), `a2077aa9b24d`
(14 units, run 10), `7425cbd1d9ee` (31 units, run 5). A run of ≥10 is the shape
§13.5's minority rule exists to catch, and these fell just under its
`contested_legacy_ratio` requirement. Reported as candidates, not as confirmed
misses — they are in `docs/nrb/phase6b-routing-holdout-manual-review.txt`.

**Nothing here is labelled `confirmed_correct` for Nepali.** Every recovered
document is `awaiting_nepali_review`: the converter turned glyph-mapped input into
Devanagari, and whether that Devanagari *reads correctly* is a competent reader's
call, not a metric's. What IS confirmed, because it is script-independent: which
inputs were English (the four false positives), that the guards touched no clean
control, and that no image or Unicode document was routed to conversion.

### 14.6 Evaluation & Improvement (Phase 6B Task 3)

**Success metric** — routing precision of the `>=0.80` queue: the share of routed
blobs that are genuinely glyph-mapped rather than English/numeric/Unicode.
Observed **56/56 (100%)** on unseen files. Nearest SQL proxy: every false route is
a document that would be corrupted before it could be retrieved.

**Eval** — this frozen 150-file holdout, disjoint from Phase 6A by construction;
native-2 run unchanged at `2a6b498`; per-blob scoring by a signal independent of
the classifier (are the flagged units themselves English?), plus six zero-legacy
negative controls. Current rates: routing precision 56/56, converter recovery
52/56, false-positive class 4/67 (0 in the queue), false-negative candidates
36/49 mostly singletons.

**Feedback capture** — `docs/nrb/phase6b-routing-holdout-{profile,manual-review}.txt`
plus the JSON. Reader corrections and the `native-3` defect list accumulate there;
they feed a *future* cohort and must never retune native-2 against this one.

**Review loop** — re-validate on a fresh holdout whenever the classifier changes.
Any threshold or rule move forces a new extractor version and a new cohort; that
is what keeps this evidence worth having.

### 14.7 The gate to the conversion-routing task

**Recommended: proceed**, scoped to the `>=0.80` queue.

Against the task's nine acceptance conditions: English/table false positives in
the queue **0/56** (1); genuine Unicode never routed (2); spreadsheet legacy
detected, 7/20, including three native-1 would have missed (3); minority regions
still detectable, 1 routed by the rule alone (4); queue precision **100%** on
unseen files (5); Preeti recovers on 52/56, cleanly on 36 (6); guards accepted no
destructive English/numeric conversion, 6/6 controls untouched (7); Preeti failing
leaves text unresolved rather than guessing another mapping (8); one new
false-positive class, wholly outside the queue (9).

**Carry these forward:** the `native-3` fix for numbered English accounting labels
(with a new cohort); the 6 unparseable `.xls`; the three high-run false-negative
candidates; OCR for the 17 `needs_ocr` and the 4 unresolved high-band blobs; and
the GPL-3 distribution gate from §12.7, still **unresolved by design** — npttf2utf
was used here as an evaluation instrument only, and `requirements-nrb.txt` is
still not installed by `Dockerfile`.

**Not done, deliberately:** no conversion is wired, no `converted` status exists,
no extracted text was mutated, and no OCR was run.

## 15. Phase 6B Task 3B — holdout evidence closure + the review pack

Task 3 produced numbers. This produced the thing a Nepali reader can actually
sit down with, and closed the two accounting gaps §14 left open: what happened
to all 150 frozen entries (not just the 142 that were evaluated), and *where on
the page* each flagged unit came from.

Nothing here changes a classifier, a threshold, a guard or an extractor version.
`app/nrb/{units,routing,quality,extraction,legacy_*,lexicon,devanagari}.py` are
byte-identical to commit `2a6b498`. No migration, no OCR, no chunking, no
embedding, no pgvector write, no production conversion.

Command: `scripts/nrb_holdout_evidence.py` (read-only, offline). Artifacts:
`docs/nrb/phase6b-routing-holdout-manual-review.md`,
`docs/nrb/phase6b-routing-holdout-evidence.json`, and 61 rendered source pages
under `docs/nrb/holdout-pages/`.

### 15.1 All 150 entries reconcile, and none was substituted

| outcome | n |
| --- | ---: |
| `suspicious` (native-2 flagged legacy) | 67 |
| `extracted` (clean) | 49 |
| `needs_ocr` | 17 |
| `unsupported` (no parser) | 8 |
| `failed` (parser error) | 1 |
| never fetched — HTTP 404 | 8 |
| **total** | **150** |

Every frozen key resolves to exactly one `nrb_files` row (**0** keys with no
catalog row), the 142 fetched blobs are 142 *distinct* `content_sha256` (no two
holdout keys share bytes), and all 150 rows carry `last_fetch_run_id = 436` — one
pass, so nothing was already on disk and nothing was re-fetched after the
outcomes were visible. The manifest's fingerprint still verifies, so the cohort
committed at `ddc5f2d` before any network access is the cohort that was measured.

The eight absences are genuine: HTTP 404, one attempt each, and NRB's own ACF
metadata reports `filesize = 0` for all eight. Seven `.pdf` and one `.jpg`, all
from `/uploads/2019/12/` — the CMS-migration cohort §7 already found to be the
corpus's damaged quarter. **They stay in the denominator.**

Also worth recording: the holdout's 142 blobs share **zero bytes** with Phase
6A's 381. Independence was enforced on URL identity; it happens to hold on
content too.

### 15.2 The `unsupported` bucket is 8 OLE2 files, not 6 `.xls`

§14 said six legacy `.xls`. The full accounting says **eight pre-2007 Office
binaries**: 6 `.xls` and **2 `.doc`**, every one sniffed
`application/x-ole-storage`. `extraction.extract_file` refuses them by extension
*before* the sniffed family is consulted, which is why an OLE2 blob never reaches
openpyxl and never produces a misleading partial parse. NRB's `resource_type`
calls all 26 spreadsheet-typed files spreadsheets; only 20 are xlsx that openpyxl
can open, and one of those 20 is the single `parser_error`. A corpus/format gap,
reported, not fixed.

### 15.3 Location, and why it needed re-parsing

`nrb_extractions` persists no text (`preview` is capped at 300 chars), and the
text native-2 scored is flat — a PDF's pages are joined with `"\n"` and a
workbook's cells are rendered `" | "`-joined. Neither can say where a unit came
from. So the pack re-parses each blob with structure retained and **verifies the
reconstruction against the stored `unit_total`** before publishing a coordinate;
70 of 70 items verified.

Two traps, both hit:

1. **`str.splitlines()` is not the inverse of `"\n".join(pages)`.** It also breaks
   on form feeds and lone `\r`, which a PDF text layer really contains — nine
   holdout PDFs did. Counting lines per page and accumulating drifts, and a page
   ending in a form feed produces a line that belongs to neither page. The pack
   therefore recovers lines *with character offsets* (`_LINE_BOUNDARY`, the exact
   boundary set) and maps each offset back to a page, asserting the result equals
   `text.splitlines()`. With that, all nine attribute correctly.
2. **Cell boundaries must come from the workbook.** Task 3's validator recovered
   cells by splitting the rendered row back on `" | "` — a faithful inverse only
   while no cell contains that sequence. The pack re-reads the workbook, so a
   coordinate is a real `Sheet!B27`. Row/column origins come from openpyxl's
   `min_row`/`min_column`, because `iter_rows()` starts at the first populated
   cell, not at A1.

### 15.4 Three questions, still three answers

| question | evidence | result |
| --- | --- | --- |
| **Routing precision** — is the routed *input* legacy Nepali? | script-independent: are the units native-2 flagged readable English? | **56/56**, 0 false routes; highest English share in the band **8.9%** |
| **Conversion recovery** — did npttf2utf produce usable Unicode? | structural: acceptance rate + native-1 flag cleared | **52/56** (36 clean, 16 partial), 4 unresolved |
| **Conversion correctness** — is it *correct Nepali*? | a reader comparing the pack against the rendered page | **no result — 0 of 56 adjudicated** |

`52/56` is a recovery figure and the pack says so in as many words. It is not
confirmed semantic success and must not be quoted as one.

### 15.5 The English false-positive class, narrowed to four

Restricted to documents native-2 actually **routed** — a clean document that
merely contains English-looking units was not routed and is not a false positive.
That leaves exactly the four §14.3 named: `690c193dc4a9` (0.5385),
`971aa739f844` (0.5185), `c38524fc9404` (0.5185), `ed3bd543c54a` (0.4828), four
copies of one NRB *Sources and Uses of Microfinance* template, 14 of 14 flagged
units readable English, every instance below the `0.80` gate.

The pack now names the cells: `Sources & Uses!B39` = `Profit & Loss A/c`,
`!B27` = `5.2.Pension & Gratuity Fund`, `!B44` = `2.2.in "A"Class Licensed
Institution`. The mechanism is two typographic habits defeating §13.2's own
corrections — a `<digit>.<digit>.<Word>` outline label is a single token with an
intra-word symbol and is not a letter-bearing compound like `FIU-Nepal`, and
`A/c` is a two-letter vowel-less token carrying a symbol. Every other cell on the
sheet is numeric and therefore `unjudged`, so the denominator shrinks until 14
labels reach ~0.5.

**Still unfixed, deliberately.** This holdout has now exposed the defect and may
not be reused as independent validation for a classifier modified to correct it.
That fix is `native-3` plus a new cohort, drawn with `exclude_keys` covering
**both** Phase 6A and this holdout.

### 15.6 Only one of the three false-negative candidates survives

§14.5 listed three clean documents carrying long legacy runs. Asking whether
their flagged units are English — decidable without Nepali — settles two of them:

| blob | flagged units | reading as English | verdict |
| --- | ---: | ---: | --- |
| `7425cbd1d9ee` | 31 | 31 (100%) | **not a miss** — `(y-o-y)`, `91-day T-bills Rate`, `Broad Money (M2)` |
| `d74b592c894a` | 59 | 37 (63%) | **not a miss** — `2.1 Why are Banks Supervised?`, `Supervision By-laws 2002` |
| `a2077aa9b24d` | 14 | 1 (7%) | **genuine candidate miss** — real Preeti |

So the same defect that produces §15.5's false positives also produces most of
what looked like false negatives, and in those two cases native-2's *document*
call was right for the wrong reason.

`a2077aa9b24d` is the real one, and it missed by exactly one condition. The
minority-region rule needs all three of `legacy >= 10` (14 ✓),
`max_legacy_run >= 3` (10 ✓) and `contested_legacy_ratio >= 0.50` (**0.2857 ✗**).
Its `Summary` sheet carries a genuine Preeti block — `Summary!C16`
`Joj;flos s[lif tyf kz'kG5L shf{` → `व्यवसायिक कृषि तथा पशुपन्छी कर्जा` — inside
a workbook that is otherwise clean English and numbers. **Diagnostic only. Do not
lower `MINORITY_MIN_CONTESTED_RATIO` on the strength of one holdout document** —
that is the tuning-against-the-holdout move §14.7 forbids, and it belongs to
`native-3` with a new cohort.

### 15.7 What the reader has to do

All 56 queue items are in the pack with their flagged units (up to 10 in the
Markdown, 40 in the JSON, always in document order, with the true total stated),
the converted Unicode, the converter's disposition per unit, and — for the 53
PDFs — a rendered page at 90 dpi so the comparison needs no network. Every
semantic verdict reads `awaiting_nepali_review`; a test asserts the generator can
never write `confirmed_correct` itself.

What does **not** need a reader, because it is script-independent and already
settled: the four English false positives, the six input-guard controls, the
zero images and zero docx routed, and the `a2077aa9b24d` diagnosis above.

Development-set reviews (the five Task 2 spreadsheet cases, the Preeti reading of
`8df7b02f8a13`) are appended to the same pack under a heading that marks them
`development evidence — not Phase 6B holdout`, so one sitting clears the backlog
without contaminating the holdout statistics.

### 15.8 Evaluation & Improvement

**Success metric.** Share of `>=0.80`-routed blobs a Nepali reader marks
`confirmed_correct`. Proxy until reviews land: routing precision on unseen files,
currently 56/56.

**Eval.** The pack is the labelled set: 56 items, each with the flagged unit, the
converted output and the rendered page. Scored by reader verdict per item.
Current agreement rate **not yet measurable** — 0 of 56 adjudicated. Six
committed tests guard the pack itself (accounting reconciles to 150, no
substitution, no auto-confirmed verdict, whole queue covered, false positives
outside the gate, spreadsheet units are cells not rendered rows).

**Feedback capture.** The reader edits the verdict column in §3 and the per-item
line in §4 of the pack, in place, under version control. Routing disagreements
(the English column) and conversion disagreements are logged separately because
they have different fixes.

**Review loop.** On reader return, and at every extractor-version change. A
`native-3` addressing §15.5 or §15.6 invalidates this cohort as validation
evidence and requires a fresh draw.

### 15.9 The gate

Unchanged from §14.7, with two refinements: the false-positive class is four
routed documents rather than six candidates, and only one of the three
false-negative candidates is real.

**Independent holdout evidence strongly supports the native-2 `>=0.80`
high-confidence routing candidate, but semantic conversion correctness remains
pending Nepali human review. Native-2 also exposed a real lower-band English
false-positive class. No classifier change or production converter integration is
made in this task.**

## 16. Phase 6B Task 4 — production extraction routing (page-level; OCR fallback is PP-OCRv5)

**Date:** 2026-08-16. **Continues** commits `faa9489` and `50edde6` (the OCR
spike and its A/B). **What this task is:** the first production behaviour built
on Phase 6B's evidence — a router that decides, per page, whether text is kept,
deterministically converted, or read from pixels.

**What it is NOT:** an ingest. Nothing is chunked, embedded, persisted or
searchable, no corpus pass was run, and neither database was opened. The
classifier is untouched: no `native-3`, no new cohort, no threshold moved.

### 16.1 The routing, in one place

```
native-2 verdict
├─ extracted/clean ...................................... keep the native text
├─ suspicious/legacy_font_suspected
│   ├─ unit_legacy_ratio <  0.80 ....................... keep (below_conversion_gate)
│   └─ unit_legacy_ratio >= 0.80  (the validated queue)
│       ├─ PDF ....... per page:  font present → guarded npttf2utf
│       │                         no font + pixels → PP-OCRv5 OCR
│       ├─ XLSX ...... guarded npttf2utf, per CELL (unchanged from §13.4)
│       └─ DOCX/TXT .. guarded npttf2utf, per line
├─ needs_ocr (PDF) ...... per page: no usable text layer + pixels → PP-OCRv5 OCR
│                                   a real text layer → keep
└─ failed / unsupported / image ......................... no recovery
```

Code: `app/nrb/recovery.py` (the router), `app/nrb/provenance.py` (per-page font
and image facts), `app/nrb/ocr.py` (the OCR boundary). Exercisable on a named
blob with `scripts/nrb_recover.py`; there is deliberately no `--all`.

### 16.2 The gate did not move, and provenance cannot widen it

Eligibility is still `status == suspicious/legacy_font_suspected` **and**
`unit_legacy_ratio >= 0.80` — native-2's own unit metric, never native-1's
`legacy_line_ratio` (§13.4, §14.7). Font provenance is consulted **only inside**
an already-eligible document, and only to choose between the converter and OCR.
A page that embeds Preeti inside a document below the gate is not converted:
that would widen npttf2utf eligibility on font presence alone, which the
validated queue semantics forbid. `CONVERSION_GATE` is a separate constant from
`legacy_convert.UNJUDGED_MIN_LEGACY_RATIO` even though both are 0.80, because
they decide different things (which documents are eligible; which units inside
one may be converted unjudged) and tying them would let a change to either move
the other silently.

The document's `unit_legacy_ratio` — not a per-page recomputation — is what gates
unjudged units inside a page, exactly as `scripts/nrb_holdout_validate._doc_ratio`
did. Re-deriving it per page would gate page 1 of a 1.0-ratio document on its own
three headings.

### 16.3 Where page provenance comes from — pypdf, not a subprocess

The spike used `pdffonts`/`pdfimages` diagnostically. Production does not need
them: pypdf is already a `requirements.txt` dependency (every PDF in this repo is
read with it), and a page's `/Resources` answers both questions directly —
`/Font` entries whose descriptor carries `/FontFile`, `/FontFile2` or
`/FontFile3`, and `/XObject` entries of `/Subtype /Image`. Composite `/Type0`
fonts are followed to their descendant (that is where NRB's subsetted CID fonts
keep the descriptor) and `/Type3` counts as embedded by construction. Form
XObjects are recursed into, bounded and cycle-guarded, because a scanner
routinely wraps the page image in one.

Verified against the spike's own findings on the seven diagnostic blobs. No
system package, no subprocess timeout policy, no missing-binary failure mode.

Three rules this implements, each traced to a measured case:

* **A stripped font name is not a scan.** `7820b1f49fc1`'s producer emitted
  `/CIDFont+F1 … /CIDFont+F6` and its deterministic conversion is good, so
  eligibility reads embedded font **objects**. Recognisable family names are
  supporting evidence only (`provenance.is_legacy_font_name`) — and they also
  catch the opposite case, a page that names Preeti without embedding it, whose
  bytes are still glyph-mapped.
* **A logo is not a scan.** `scan_backed` is "no font of its own **and** pixels".
  `268bcfe86d03` is an embedded-Preeti circular with the bank's logo on it; the
  weaker rule would have sent it to OCR.
* **A page is not judged on its document.** `e08988860534` page 1 is a 300 dpi
  scan and pages 2–50 embed real Preeti. Run end to end, the router OCRs page 1
  into `इ.प्रा. परिपत्र संख्या ०७/२०८०-८९ …` and converts pages 2–50 into
  `नेपाल राष्ट्र बैंक विदेशी लगानी तथा विदेशी ऋण व्यवस्थापन विनियमावली, २०७८ …`,
  in order. That is the whole reason routing is per page.

**A known limit, stated rather than engineered around.** Some OCR software
embeds a subsetted font for the invisible text layer it stamps onto a scan. Such
a page would read as "font present" and go to the converter rather than to OCR.
None of the 56 queue documents does this — every scan in the cohort uses a
non-embedded `/Helvetica` or declares no font at all — so the case is recorded,
not handled. The failure mode is degraded rather than corrupting: the
conversion guards run on the INPUT, so an English-looking scanner layer is
vetoed by `lexicon.is_confidently_english` and the rest is rejected by
`validate_conversion`, leaving the original text. Finding one is a reason to add
a signal, on a new cohort — not to loosen this rule.

### 16.4 Fail-closed, in both directions

A page that goes to OCR is **never** handed to npttf2utf. Its hidden text layer
is a scanner's latin-alphabet guess (`Htqft Hfrqq aFrerr{ hrn`), not a glyph
mapping, and the converter would turn it into fluent Devanagari nonsense that
passes every validation rule the converter has (§12.2 measured exactly that on
an English table).

So when OCR is unavailable or fails, the page yields **empty text**, `ok=False`
and the reason — not the junk layer, and not a conversion. When provenance cannot
be read at all, the page keeps its native text: an unopenable resource dictionary
is not evidence of a scan, and both alternatives act on a guess.

**A conversion that does not succeed withholds its input.** The original of a
unit this router itself called a high-confidence legacy candidate is glyph-mapped
ASCII; indexed, it is unsearchable noise that a citation would present as the
text of a circular. Four failures, one answer:

| what happened | page reason | `ok` | text |
|---|---|---|---|
| npttf2utf absent (the GPL-3 gate) | `conversion_unavailable` | false | empty |
| the backend broke the page | `conversion_failed` | false | empty |
| every candidate rejected/failed | `conversion_unresolved` | false | guard-kept only |
| *some* candidates unresolved | the route reason | true | those units blanked |

The withholding is per UNIT and lives in `recovery._withhold`, not in
`legacy_convert` — that module's `LineOutcome.text` deliberately returns the
original, which is what makes its negative controls byte-exact, and changing it
would weaken the controls to fix a consumer's problem. Only units that were
CANDIDATES are withheld: a line kept by the Unicode guard, the English guard or
the detector was never legacy text, so a mixed page keeps its readable half and
loses only the line that stayed glyph-mapped. The line terminator survives, so an
unresolved unit becomes a blank line rather than fusing its neighbours.

**A failed conversion is never re-routed to OCR.** PP-OCRv5 measured worse than
deterministic conversion on exactly this class — embedded-font pages, where it
renders `कारवाही` as `शदक` (§16.6) — so the substitution would trade a recorded
gap for unvalidated text. `PageText.indexable` (`ok` and non-empty) is the single
question an ingestion boundary should ask.

### 16.5 Provenance for citations

Every page comes back as a `PageText` carrying its 1-indexed **source** page
number, its **route** (`native` / `legacy_conversion` / `ocr`), the reason that
route was chosen, and per-route detail — the converter mapping and version and
the per-disposition counts for a converted page, the engine, model and version
for an OCR'd one. Page identity is never merged away; `.text` reconstructs the
document in page order for measurement, and the pages remain addressable.

Spreadsheets carry the **sheet name** and are converted per cell, with the grid
re-read from the workbook. Recovering cells by splitting the stored
`" | "`-joined row is not the inverse of rendering it, and `|` is itself a Preeti
codepoint mapping to `्र` (§13.4).

**Nothing is persisted.** There is no recovery table and no migration in this
task — storage lands with Phase 7, which is also where the route belongs as a
citation field. Adding a schema now would mean an Alembic revision on a branch
whose lineage is deliberately unreconciled (§9.10).

### 16.6 The OCR decision, and what it does not claim

`docs/nrb/phase6b-ocr-spike.md` and `phase6b-ocr-spike-v5.md` hold the evidence.
In short:

| | halant per Devanagari char | mean word length |
|---|---:|---:|
| PP-OCRv4 (torch) | 0.0042 | 24.7 |
| **PP-OCRv5 (onnxruntime)** | **0.0798** | **5.4** |
| reference — npttf2utf over the 56-doc queue | 0.0982 | 5.7 |

PP-OCRv4 is **rejected** for Nepali: it recovers the script but not the
orthography — no conjuncts, visual rather than logical order. PP-OCRv5 is at the
reference on both signals, unanimously on all 14 spike pages. The backend is
load-bearing, not incidental: docling reaches v4 through torch and **v5 only
through onnxruntime**.

Three limits are stated rather than engineered around:

1. **OCR output is retrieval text, not a transcription.** On a 150 dpi scan v5
   drops letterheads, subject lines and whole body paragraphs, and it is
   unreliable on latin runs (`lc_visakhapatnam@nrb.org.np` came back as noise).
   It must never be treated as authoritative for a figure, a date, an account
   number or a contact detail. Every OCR page records `authoritative: false`.
2. **There is no confidence score.** The spike measured orthographic
   well-formedness, which is not a per-field correctness estimate; inventing a
   threshold from it would dress a guess as a measurement.
3. **Conversion still beats OCR where a font is embedded.** On the control blob
   v5 renders `कारवाही` as `शदक` and `२०६९।१।३१` as `२०६९।९।३१`. That is why OCR
   is the narrow fallback and not the default.

**PaddleOCR-VL remains deferred.** It was proposed to solve the collapse v5
closed; revisit only if reader review finds the low-DPI recall gap
disqualifying.

### 16.7 The dependency boundary

`rapidocr` and `onnxruntime` are declared in `requirements-worker.txt` and are
**not** in `requirements.txt`, which is the only file `Dockerfile` installs — so
the API image cannot acquire an OCR stack by accident. `app/nrb/ocr.py` is the
only importer and imports both inside a function; a subprocess test asserts that
importing the router pulls in none of docling, torch, onnxruntime, rapidocr or
npttf2utf. Same structural guarantee `requirements-nrb.txt` gives npttf2utf, for
a different reason (size, not licence).

**The npttf2utf GPL-3.0 gate is still OPEN and still unresolved.** Wiring the
converter into a routing path does not resolve it: obligations attach to
distribution, so any build shipped to a client still needs a licensing decision
or an independently-derived mapping table behind the same Protocol (§12).

### 16.8 What is still not done

Semantic correctness is **still unmeasured** — every verdict in the review pack
is `awaiting_nepali_review` (§15). The router decides *which instrument reads a
page*; whether what came out is right is a reader's judgement, and none of the
numbers here change that. Also open: the §14.3 / §15.5 English accounting-template
false positives (below the gate, so they are kept native, not converted); the
§15.6 false negative `a2077aa9b24d`; the 8 OLE2 `.xls`/`.doc` files with no
parser; image-only files (`image_ocr_not_enabled` — no image was in the measured
cohort); Phase 7 chunk+embed; Phase 8 `search_nrb_documents`.

### 16.9 Evaluation & Improvement (Phase 6B Task 4)

**Success metric.** Share of high-confidence-legacy PDF **pages** that come back
as usable Nepali text under the route the router chose. Proxy until reader
verdicts land: on the frozen 56-document queue, 8 documents embed no font at all
and hold all 4 of the converter's `unresolved` outcomes — routing those pages to
OCR instead is the specific gain this task exists to produce, and it is
measurable per page rather than per document.

**Eval.** 29 committed tests in `tests/test_nrb_recovery.py`, currently **29/29**.
They are a labelled set of routing decisions, not of Nepali text: each names the
real blob whose behaviour it encodes (`7820b1f49fc1` stripped names,
`268bcfe86d03` logo, `e08988860534` mixed, `3d2eca8b9f95` scan junk,
`05fa82badf94` English table, `c298efaf1f16` no text layer). The PDFs are
assembled byte by byte in the test file so provenance is stated, not inherited.

**Feedback capture.** `scripts/nrb_recover.py --json` writes the full per-page
record (route, reason, page number, converter mapping, OCR model, failures) for
any named blob. A route a reader disagrees with is reported against the page
number in that record. Reader verdicts on conversion continue to land in the
§15 pack; the two are logged separately because a wrong ROUTE and a wrong
CONVERSION have different fixes.

**Review loop.** On reader return, and before any corpus pass. A change to the
gate, to `plan_document` or to `route_page` is a change to what gets indexed and
requires a fresh cohort — the §14/§15 holdout is spent evidence and must not be
re-run as validation for it.

### 16.10 The gate to Phase 7

**Page-level routing is implemented and tested, and the OCR fallback is
PP-OCRv5 via docling/RapidOCR on the worker side only. No corpus was routed, no
route was persisted, and no document is searchable. Conversion and OCR
correctness both remain pending Nepali human review, and the npttf2utf GPL-3.0
distribution gate remains open.**

The next step is a SMALL scratch-DB exercise against `local_ai_gateway_p4` — a
handful of named blobs through route → chunk → embed → retrieve — to find out
what Phase 7 has to store. Not a corpus ingest.

## 17. Phase 6B Task 5 — NRB text in department RAG (8 documents, scratch DB)

**Date:** 2026-08-16. **Continues** `3ba1185` (routing) and `8bf703f` (the
fail-closed conversion fix). **Scope:** eight named blobs into
`local_ai_gateway_p4`, end to end, to find out whether recovered NRB text
survives chunking, embedding, storage and retrieval **with its page and route
intact**. It is a smoke test. Eight documents cannot measure retrieval quality
and nothing here computes an accuracy figure.

### 17.1 Where the two pipelines meet

One function and one branch:

```
worker._load_chunks_sync
  └─ documents.metadata.origin == "nrb"  →  app/nrb/rag.parse_nrb_to_chunks
                                             sniff → native-2 → recovery → chunk per page
     anything else                       →  rag.parsing.parse_to_chunks   (unchanged)
```

`app/nrb/rag.py` is the whole NRB side. The import inside the branch is local, so
`app.rag` acquires no dependency on `app.nrb`, and a document without the marker
takes a byte-identical path to the one it took before. Chunking itself is the
generic `chunk_text` — the paragraph/sentence/word boundary logic is shared, not
reimplemented.

**Classification is re-run rather than read from `nrb_extractions`.** A chunk must
be a function of the bytes on disk, not of a catalog row that an older extractor
version may have written. It is the same pypdf parse, and it is cheap next to
embedding.

### 17.2 The schema already carried it — no migration

Checked before writing anything. `document_chunks` has **`page_number`** (a real
column, already populated by the Docling path) and **`metadata` JSONB**;
`documents` has its own `metadata` JSONB. That is enough for citation provenance,
so no Alembic revision was created and the deferred lineage (§9.10) was not
touched.

| what | where | example |
|---|---|---|
| document identity | `documents.metadata` | `{"origin":"nrb","blob_sha256":"e0898886…","source_url":…,"comparison_key":…}` |
| page | `document_chunks.page_number` | `1` |
| sheet | `document_chunks.section` | `T1.1` |
| route | `document_chunks.metadata.route` | `native` \| `legacy_conversion` \| `ocr` |
| conversion provenance | same | `{"converter":"npttf2utf 0.3.7","mapping":"Preeti","converted_units":11,"unresolved_units":0}` |
| OCR provenance | same | `{"ocr_model":"PP-OCRv5","ocr_version":"docling 2.118.1; rapidocr 3.9.2; onnxruntime 1.23.2","authoritative":false}` |

Three small generic-side edits, all additive: `Chunk.meta` (None on every generic
path), `replace_chunks` writing `chunk.meta or {}` (which is the column's own
default), and `DocSnapshot.meta` so the worker can read the marker without an
extra query.

`authoritative: false` rides on **every OCR chunk**, not just in the docs, so a
future citation renderer can carry the caveat without re-deriving it.

### 17.3 A chunk never spans two pages

Page identity is the citation, so chunking runs per page and every NRB chunk
carries a `page_number`. Chunk indices are still contiguous across the document —
`document_chunks` has `UNIQUE (document_id, chunk_index)` — and page 1's chunks
all precede page 2's. A chunk merged across a page boundary could not be cited to
either page.

No route-based ranking. The route is provenance and a quality caveat, not a
calibrated relevance penalty; OCR'd and converted chunks entered retrieval on
identical terms. Nothing in top-k, RRF, HNSW or reranking was changed.

### 17.4 The sample, and what it did

Eight blobs, one distinct routing outcome each, all with behaviour Phase 6B had
already established.

| blob | what it is | chunks | ingest |
|---|---|---:|---:|
| `075bf12eb087` | clean native Unicode PDF, 2 pages | 4 | 12 s |
| `1a9b6321aa61` | embedded Preeti+Bishall, recovered | 1 | 1 s |
| `268bcfe86d03` | embedded Preeti circular 2007, partial | 1 | 1 s |
| `3d2eca8b9f95` | 300 dpi scan, no embedded font | 2 | 32 s |
| `c298efaf1f16` | no text layer at all, 3 pages | 4 | 21 s |
| `e08988860534` | the mixed document, 50 pages | 75 | 266 s |
| `7820b1f49fc1` | stripped font names, 4 pages | 9 | 32 s |
| `8df7b02f8a13` | Preeti-encoded workbook, per CELL | 154 | 522 s |
| **total** | | **250** | **897 s** |

Routes actually stored: **legacy_conversion 239** chunks (4 documents),
**ocr 7** (3 documents), **native 4** (1 document). Every job succeeded; no
document failed, and nothing was withheld — the four converted documents
resolved every candidate unit they attempted except one line in
`268bcfe86d03`, which was dropped rather than indexed.

**OCR cost**, the number this exercise existed to get: 5 OCR'd pages across 3
documents, and the two OCR-only documents took 32 s and 21 s wall clock
**including** model load, embedding and storage. Per page OCR is ~2–3 s, as the
spike measured. It is not the bottleneck here — **embedding is**: the 154-chunk
workbook spent ~8 of its 8.7 minutes embedding, and the 75-chunk mixed document
~4 minutes. So **no second OCR queue.** The existing worker is already
asynchronous and one job at a time; the operational question a corpus pass raises
is embedding throughput, not OCR.

### 17.5 Retrieval: seven queries, every route came back

Queries were taken from text visible in the selected documents, per route. Top
hits, with page and route as stored:

| query | top hit | page | route | expected doc? |
|---|---|---|---|---|
| विदेशी विनिमय व्यवस्थापन विभाग | `3d2eca8b9f95` | p1 | ocr | yes |
| सम्पत्ति शुद्धीकरण निवारण | `e08988860534` | p5 | legacy_conversion | **at rank 3** |
| विदेशी लगानी … विनियमावली | `e08988860534` | p3 | legacy_conversion | yes |
| इजाजतपत्रप्राप्त बैंक तथा वित्तीय संस्था | `7820b1f49fc1` | p4 | legacy_conversion | yes |
| लगानी सम्बन्धी सूचना | `c298efaf1f16` | p1 | ocr | yes |
| प्रमुख कृषि बालीले ढाकेको भू–क्षेत्र | `8df7b02f8a13` | p3 (T1.1) | legacy_conversion | yes |
| कारवाही फुकुवा भएका वित्त कम्पनी | `1a9b6321aa61` | p1 | legacy_conversion | yes |

Six of seven put the expected document first; the seventh retrieved it at rank 3.
Notably the mixed document's **OCR'd page 1 and its converted page 3 both surface
for the same query**, which is the case the whole page-level design exists for.
With 250 chunks from 8 documents and 75+154 of them from two files, rank order
here is a smoke signal and nothing more.

### 17.6 What the exercise found — a fourth text-trust failure mode

The one query that did not rank its expected document first is the interesting
result, and the reason is not ranking. `075bf12eb087` is the **native** document,
and its own text layer is corrupt at the codepoint level:

```
extracted : कम्पनी रजिष्ट्र ारको कार्ाालर् जिपुरेश्वर … जनदेशन , २०७९
should be : कम्पनी रजिष्ट्रारको कार्यालय … निर्देशन, २०७९
```

This is pypdf reading a broken `ToUnicode` CMap, and **the recovery path never
touched it** — the native route is passthrough by definition. Native-2 calls the
document `extracted`/`clean` because its rules ask *is this Devanagari*, and it
is; they cannot ask *is it spelled correctly*. So this is a genuinely new failure
class beside the three Phase 6B already names (glyph-mapped legacy text, scanner
junk, no text layer): **Unicode Devanagari that is systematically wrong**.

Recorded, **not fixed**. A detector for it is a classifier change — a `native-3`
and a new cohort (§15.9), not an edit here — and it needs a Nepali reader to
confirm the extent. Its practical effect today is a document that indexes and
retrieves but whose text a reader would find garbled.

Second, smaller observation: workbook chunks carry long runs of empty `|`
separators from sparse spreadsheet rows, which dilutes their embeddings. The
generic spreadsheet parser repeats a header row per chunk for exactly this kind
of reason; the NRB path does not yet. Not a correctness problem — the converted
Nepali is in there and retrieves — but it is the obvious first improvement if
workbook recall matters.

### 17.7 Evaluation & Improvement (Phase 6B Task 5)

**Success metric.** Share of ingested NRB pages that retrieve with a correct page
number and route. Proxy today: 7 of 7 queries returned a chunk whose stored page
and route matched the document it came from, and 6 of 7 ranked the expected
document first — on 8 documents, which is a smoke signal, not a rate.

**Eval.** 9 committed tests in `tests/test_nrb_rag_ingest.py` (currently 9/9)
covering the boundary rather than the corpus: route metadata on the chunk,
contiguous indices with no cross-page chunk, an unresolved conversion
contributing nothing, a missing converter raising with the routing outcome named,
a failed OCR page contributing nothing, a generic chunk carrying no metadata, and
the worker branch asserted in **both** directions. Plus the seven queries above,
reproducible with `scripts/nrb_rag_ingest.py --search`.

**Feedback capture.** `scripts/nrb_rag_ingest.py` reports per-document status,
route counts and timings, and the retrieval output prints page + route per hit. A
bad retrieval is attributable to a document, a page and an instrument from stored
data alone. Reader verdicts on the text itself continue to land in the §15 pack.

**Review loop.** Before any larger ingest, and on every extractor-version change.
The §17.6 finding is the first item for that review: it needs a reader's judgement
on how much of the corpus is affected before a `native-3` is worth drawing a
cohort for.

### 17.8 The gate

**Recovered NRB text ingests and retrieves with page and route provenance
intact, over 8 documents in the scratch database. No migration was needed, the
generic RAG path is unchanged, and no corpus ingest was run.** Still open, and
all of them predate this task: conversion and OCR correctness await Nepali
review (§15), the npttf2utf GPL-3.0 distribution gate is unresolved (§12), and
§17.6 names a new text-trust failure mode that no classifier currently detects.

## 18. Deployment readiness — container validation (NOT the GPU server)

**Date:** 2026-08-16. **Scope:** the pre-deployment inspection and container
build/run validation for a first NRB deployment. **What this is not: the GPU
server was never reached.** No SSH key, no SSH config, no remote Docker context
and no server address exists in this working environment — every host in `.env`
and `.env.docker` is `localhost`/`host.docker.internal`, and `AGENT_MODEL` is the
laptop's `qwen2.5:latest`. So `nic_ollama`, `nic_postgres`, the A40s, and
`OLLAMA_CONTEXT_LENGTH` on that box are all **unverified**. Everything below was
measured against the real container images on a laptop.

That still mattered: the inspection found **four** things that would each have
broken or silently degraded a server deployment, three of them invisible until
the images actually ran.

### 18.1 What was wrong

| # | Defect | Effect on a server deploy | Fix |
|---|---|---|---|
| 1 | `LEXICON_PATH` was CWD-relative | worker's CWD is `/app`; lexicon not found → no converter → **every** legacy page unresolved | resolve from `__file__` |
| 2 | lexicon not in the worker image (`.dockerignore` drops `docs/`) | same as #1, even with the path fixed | `COPY` it + a `.dockerignore` exception |
| 3 | `npttf2utf` in neither requirements file the worker installs | same as #1, even with #1 and #2 fixed | opt-in build ARG (GPL-3 gate) |
| 4 | RapidOCR model dir root-owned; worker runs as uid 10001 | re-downloads ~5 MB **per page**, never persists, returns nothing → readable scans recorded `needs_ocr` | `chown` it |
| 5 | docling's layout model calls `torch.compile`; no C++ compiler in the slim runtime | OCR pages produce no text — same symptom as #4 | `TORCHDYNAMO_DISABLE=1` |

Defects 1–3 all produce the *same* outcome and each masks the next, which is why
they are listed separately: fixing any one alone changes nothing observable.

**None of them could produce bad text.** Every one of them ends in
`conversion_unavailable` or `needs_ocr` with the input withheld — the fail-closed
rule from `8bf703f` holding under conditions it was never written for. The cost
of the whole class is a silent recall hole, not a corrupted corpus. That is the
design working, and it is also why these were invisible: a deployment with all
five defects boots clean, reports success, and indexes a quarter of the sample.

### 18.2 The GPL-3 gate is now a build flag

`Dockerfile.worker` takes `INSTALL_LEGACY_FONT` (default **false**). A default
build carries no GPL-3 code and is distributable; an NRB deployment opts in:

```bash
docker compose -f docker-compose.yml -f docker-compose.p4.yml build worker
```

Measured on `7820b1f49fc1` in a worker image built **without** it: 4 pages routed
`legacy_conversion`, **0 indexable**, every page `conversion_unavailable`, no page
text non-empty, and `parse_nrb_to_chunks` refusing with the routing outcome
named. The omission is safe and it is loud in the log — but it is silent in the
*corpus*, which is why `DOCKER.md` now states the chunk cost (239 of 250).

### 18.3 `docker-compose.p4.yml`

The base stack reads `.env.docker`, which names the real database, and `migrate`
runs `alembic upgrade head` against whatever it is handed. "Remember to edit the
env file" is not a control. The overlay repoints **all three** services at
`.env.docker.p4` so migrate, gateway and worker cannot disagree, and turns the
GPL flag on. `p4` is at `b1bea6ac36c5` = this branch's head, so that upgrade is a
verified no-op.

### 18.4 What the containers proved

| Check | Result |
|---|---|
| API image free of docling / rapidocr / onnxruntime / npttf2utf / torch / cv2 | **all absent**, 471 MB |
| worker image has pypdf, docling, rapidocr, onnxruntime, npttf2utf, lexicon | **all present**, 673 MB |
| gateway boots, `/health`, reaches Ollama | ok, container `healthy` |
| worker preflight | `qwen3-embedding:4b-q8_0 -> 2560 native dims, storing 1536` |
| conversion in-container (`7820b1f49fc1`) | 4/4 pages converted, 9 chunks, clean Devanagari |
| OCR in-container (`c298efaf1f16`) | 3 pages → 4 chunks, `route=ocr`, `authoritative=false` |
| generic (non-NRB) docling parse | unchanged, 4 chunks with the dynamo flag either way |

The last row is the one that keeps #5 honest: an ordinary text-layer PDF is fine
without the fix because docling falls back to the embedded text. Only
`force_full_page_ocr=True` has no fallback, so only OCR broke.

### 18.5 What is still unverified, and cannot be verified from here

- **The GPU box entirely** — `docker ps`, `nic_ollama`, `nic_postgres`,
  `nvidia-smi`, disk, and whether `local_ai_gateway_p4` exists *there* (it exists
  on the laptop). `OLLAMA_CONTEXT_LENGTH=32768` is confirmed on the **laptop's**
  systemd unit; the server's compose stack was not read and **must not be edited
  without authorisation**.
- **Tool calling — not validated, on any model.** `qwen3.5:35b-a3b` is not
  present locally, and the laptop could not stand in: `qwen2.5:latest` (4.7 GB)
  plus a 32k KV cache does not fit a 6 GB RTX 4050, so `llama-server` spent 43
  minutes at ~500% CPU without returning, and every turn hit `OLLAMA_TIMEOUT`.
  That is the exact spill `docs/server-and-models.md` §3 warns about ("step down
  to 16384 if so") and it is a laptop capacity limit, not a gateway defect — but
  it means **no turn completed**, so tool selection, argument validity,
  `tool_call_id` correlation and multi-tool behaviour are all still unverified.
  What the attempt *did* show is the error path behaving: the loop logged
  `iteration 1: model stream failed: Model server request timed out`, the
  endpoint returned **200 with `stop_reason: "error"`**, and neither container
  crashed or leaked a 500. Retrieval itself was validated directly instead
  (§18.7), which needs no chat model.
- **Embedding throughput.** The laptop's RTX 4050 holds 4.62 GB of a 10.6 GB
  model — **44% GPU, 56% CPU spill** — so local embedding timings are a floor
  artefact and say nothing about two A40s. Do not carry them over.
- **`search_nrb_documents` does not exist.** It is Phase 8. Retrieval is reached
  through `search_department_docs` against a department the NRB blobs were
  ingested into.

### 18.6 Evaluation & Improvement

**Success metric.** A deployment either recovers the sample's legacy and scanned
pages or it does not: `legacy_conversion` + `ocr` chunk counts matching the
known-good figures, with zero `conversion_unavailable` pages. Proxy for SQLs at
this stage; nothing here is user-facing yet.

**Eval.** The eight named Phase 6B blobs, re-run through the deployed worker and
compared per document against §17's chunk counts and route split. Current
agreement is reported in §18.7. Two negative controls carry their own weight: a
worker built without npttf2utf must produce 0 indexable pages on a legacy blob,
and the API image must import none of the five parser packages.

**Feedback capture.** `scripts/nrb_rag_ingest.py --ingest --enqueue-only` leaves
the drain to the deployed worker, so the worker's own log is the record; per-job
status, chunk totals and errors persist in `ingest_jobs`. Route and page land in
`document_chunks.metadata` as before.

**Review loop.** On every image rebuild and before any larger ingest. The five
defects above share a signature — silent recall loss with a healthy-looking
deployment — so the check that matters is the route split, never job success.

### 18.7 The containerised run (laptop, `local_ai_gateway_p4`)

`migrate` → `gateway` → `worker` from the repository images, all three on
`.env.docker.p4`. `alembic upgrade head` was a verified no-op. Gateway reported
`healthy`; worker preflight logged
`qwen3-embedding:4b-q8_0 -> 2560 native dims, storing 1536`. The eight Phase 6B
blobs were enqueued with `--enqueue-only` and drained **by the deployed worker**.

**8/8 succeeded, 250 chunks — every per-document count identical to §17.**

| blob | chunks | s | route |
|---|---:|---:|---|
| `075bf12eb087` | 4 | 18.1 | native p1–2 |
| `1a9b6321aa61` | 1 | 3.1 | legacy p1 |
| `268bcfe86d03` | 1 | 3.0 | legacy p1 |
| `3d2eca8b9f95` | 2 | 31.4 | ocr p1 |
| `c298efaf1f16` | 4 | 24.4 | ocr p1–3 |
| `e08988860534` | 75 | 279.0 | **ocr p1 + legacy p2–50** |
| `7820b1f49fc1` | 9 | 32.2 | legacy p1–4 |
| `8df7b02f8a13` | 154 | 325.4 | legacy p1–46 |

Totals `legacy_conversion` 239 / `ocr` 7 / `native` 4. The mixed document keeping
its OCR page 1 and its 49 converted pages, through a container, is the
page-level routing claim surviving deployment.

**Embedding dominates, and the reason is local.** Batch size 32; the workbook's
batches took 120 s, 110 s, 36 s, 33 s. The worker container sat at 0.01–0.30%
CPU throughout — all of it is in Ollama — the embed model stayed resident (no
reload per batch), and requests were not serialized unexpectedly. The cost is
VRAM: `qwen3-embedding:4b-q8_0` is 10.6 GB and the RTX 4050 held **4.62 GB, 44%
GPU / 56% CPU spill**. On two A40s it fits entirely. **Do not carry these
timings to the server, and do not build a second OCR queue** — OCR was ~2–4 s
per page and the two OCR-only documents finished in 31 s and 24 s *including*
model load.

**Retrieval** (5 scripted queries, `--limit 3`): 4 of 5 returned the expected
document at rank 1 — `3d2eca8b9f95` p1 `ocr`, `e08988860534` p3
`legacy_conversion` (with its own OCR'd p1 at rank 3), `7820b1f49fc1` p4
`legacy_conversion`, `c298efaf1f16` p1 `ocr`. The fifth,
`सम्पत्ति शुद्धीकरण निवारण`, put `075bf12eb087` p2 `native` at **rank 3** —
the same rank as §17, and §17.6 is visible in the hit itself
(`सम्पजि शुद्धीकरण`, `त्तनवारण`). Recorded, not fixed.

**Route is stored but not retrievable.** `document_chunks.metadata.route` is
correct on all 250 chunks, but `RetrievedChunk` (`app/rag/retrieval.py`) carries
no metadata field, so `search_department_docs` cites title + page + doc id and
**cannot cite the extraction route**. Nothing was changed here — surfacing it is
a retrieval-layer decision that belongs with Phase 8's citation format.

## 19. The GPU-server run, and how the pipeline re-runs

**Date:** 2026-08-17. **Scope:** two things, neither of which changed any code.
A second attempt at the live-server deployment, which **did not begin** because
its own precondition is unmet; and a read of the pipeline's re-run behaviour —
what a repeat pass skips, what it redoes, and what a corpus-scale run would
still need. Working tree clean at `ec780fa` throughout; nothing was committed
from the deployment attempt because nothing was changed.

### 19.1 The server was not reached, again — and this is now a hard prerequisite

The task was gated on *"this task begins only if the actual GPU server is
reachable."* It is not. Every access route was checked rather than assumed from
the previous attempt:

| Check | Result |
|---|---|
| `hostname` | `manoj-hp` — the laptop |
| `~/.ssh/` | **empty** — no keys, no `config`, no `known_hosts` |
| `ssh-add -l` | *"The agent has no identities"* |
| `docker context ls` | only `default` → local socket; `DOCKER_HOST` unset |
| VPN / mesh | none — `tailscale` not installed, only `wlo1` up |
| `/etc/hosts` | no server entry |
| `nvidia-smi` | **RTX 4050, 6 GB** — not 2× A40 |
| addresses in `.env*`, `docs/`, `DOCKER.md` | only `127.0.0.1` and the Docker bridge |

`known_hosts` being absent is the strongest single signal: this environment has
never contacted the server. **No laptop deployment testing was repeated** — §18
already covers what containers can prove here, and repeating it would add
nothing.

**What unblocks it:** a host address, an SSH key installed for it (the keypair
does not exist yet), and the SSH user. A `Host` entry in `~/.ssh/config` is the
right shape, since `docs/server-and-models.md` deliberately keeps the address
out of the repository. Failing that, the Step 1–3 inspection commands are pure
reads and can be run by hand with their output pasted back.

Two server facts are worth flagging *before* anyone runs it, because both have
been assumed and neither is evidenced: `local_ai_gateway_p4` is known to exist
**on the laptop** — there is no evidence it was ever created on the server, and
§18.7's expected revision came from the laptop database; and
`OLLAMA_CONTEXT_LENGTH=32768` is confirmed on the **laptop's** systemd unit, not
on `nic_ollama`.

### 19.2 What a repeat pass skips, and what it redoes

Read from the code, because "is it idempotent" has a different answer at each
stage and the differences are the operationally interesting part.

| Stage | Driver | Repeats work? |
|---|---|---|
| 1. Catalog sync | `scripts/nrb_sync.py` | **No** — second run all-zero |
| 2. Download | `scripts/nrb_fetch.py` | **No** — resumable |
| 3. Extract + classify | `scripts/nrb_extract.py` | **No** — per `(blob, version)` |
| 4. Recovery (convert/OCR) | inside the worker | **Yes — every ingest** |
| 5. RAG ingest | `ingest_jobs` + worker | **No, but it aborts rather than skips** |

**Nothing is scheduled.** There is no cron, systemd timer or in-process
scheduler anywhere in the repository. Stages 1–3 are manual CLI passes; the only
long-running process is `app.rag.worker`, which polls `ingest_jobs`. "Run weekly
and pick up what NRB published" is not built.

**Sync** re-reads the REST API every run — it must, that is how new documents
are discovered — but writes only rows whose `metadata_hash` changed.

**Fetch** selects `fetch_status = 'pending'` in id order
(`catalog.select_fetch_targets`, `catalog.py:765`). Fetched files are excluded
*by construction*: the status list only ever holds `pending`, plus `failed`
under `--retry-failed`. Commits every 25 files, so an interrupt keeps its
progress and the next pass takes the *next* files.

**Extract** selects fetched blobs with no extraction row at this
`extractor_version` — a `NOT EXISTS` at `catalog.py:1059-1064`, `DISTINCT ON
(content_sha256)`. Bumping `native-2` → `native-3` makes the corpus selectable
again, deliberately; `--force` is development-only.

**Recovery is the stage that repeats, and it is the expensive one.**
`rag.parse_nrb_to_chunks` (`app/nrb/rag.py:270`) calls `recover_blob`, which
calls `extraction.extract_file` **fresh** (`rag.py:180`) and re-runs conversion
and OCR from the blob on disk. It does not read `nrb_extractions` at all. Two
consequences, and the second is not obvious:

1. Re-ingesting a document re-runs OCR at ~2–4 s/page. Invisible over 8 blobs;
   not invisible over the corpus.
2. **`nrb_extractions` is evidence, not an input.** Running `nrb_extract.py` is
   *not* a prerequisite for ingestion and its rows are not consulted by it. The
   Phase 6A/6B tables measure the classifier; the ingest path re-derives the same
   judgment independently. They agree because they run the same code, not
   because one reads the other.

**Ingest is the only real job system.** `documents` + `ingest_jobs`, and
`worker.py:312` polls `jobs.claim_next` (`FOR UPDATE SKIP LOCKED`). A job exists
once per enqueue; finished documents are never re-scanned. Duplicate protection
is the partial unique index `ux_documents_active_content` on
`(department_id, content_hash)` excluding archived rows, so one blob cannot be
indexed twice in a department.

### 19.3 Three things Phase 7 has to build, found by reading the re-run path

None of these is a defect in what exists — they are the parts a corpus run
needs that an 8-blob smoke test never exercised.

1. **There is no corpus ingest driver.** `scripts/nrb_rag_ingest.py` is a smoke
   test: 8 hard-coded blobs, a guard refusing any database but
   `local_ai_gateway_p4`, no `--section`/`--all` scope. And it calls
   `create_document` (`scripts/nrb_rag_ingest.py:181`) without catching
   `DocumentConflict`, so a second `--ingest` without `--reset` **aborts on the
   first already-ingested blob** instead of skipping it. The database stays
   correct; the run just stops. A driver needs the same treatment fetch and
   extract already have — a scope argument, skip-what-exists, and resumability.
2. **Recovery output is not cached.** Deciding whether to persist recovered page
   text (a new table, or `nrb_extractions` columns) is a Phase 7 call. It is a
   real trade: caching makes re-ingest cheap but adds a second place where
   recovered text can go stale against a converter change, and the version
   discipline that protects `nrb_extractions` would have to cover it too.
3. **There is no supersession link.** If NRB republishes a circular, sync and
   fetch produce new bytes → a new `content_sha256` → a *new* `documents` row.
   Nothing archives the old one, because `documents.metadata.blob_sha256` is
   written by the script but never read back. Both versions would accumulate and
   retrieval would return either. The catalog already knows which source the file
   came from; the missing piece is a reconciliation between `nrb_files` and the
   `documents` rows minted from them.

### 19.4 Evaluation & Improvement

**Success metric.** For §19.1, binary: the server inspection either ran or it
did not, and it did not. For §19.2, the metric that matters at corpus scale is
**re-run cost** — a second pass over an unchanged corpus should approach zero
work at stages 1–3 and should not re-OCR anything at stages 4–5.

**Eval.** Stages 1–3 already have one and it passes: sync's second run is
all-zero, fetch's exhausted scope selects 0, extract's `NOT EXISTS` selects 0.
Stage 5 has no eval because the driver does not exist; the test to write with it
is *ingest the same scope twice, assert the second pass creates zero documents
and zero jobs and raises nothing*. That test currently fails by construction.

**Feedback capture.** `nrb_sync_runs` records every sync's counters;
`nrb_files.fetch_status` and `nrb_extractions` are the per-item record for
stages 2–3; `ingest_jobs` holds per-job status, error and timing for stage 5. No
new capture is proposed here.

**Review loop.** Before any corpus-scale ingest, and again after the first one —
re-run cost is only measurable once there is a corpus in the index.

### 19.5 The gate

Unchanged from §17.8 and §18.6, plus one addition: **server access is now a
stated prerequisite**, not a step. The deployment task cannot be attempted again
until a host, a key and a user exist in the working environment.

Still outstanding and untouched by this session: the Nepali semantic review of
the §15 pack (conversion *correctness* remains unmeasured), `075bf12eb087`'s
broken-ToUnicode native text (§17.6, a `native-3` + new cohort), the npttf2utf
GPL-3.0 distribution decision, and full-corpus retrieval quality.

## 20. Phase 7 step 1 — the corpus ingest driver (31 documents, scratch DB)

**Date:** 2026-08-17. **Scope:** the driver that turns catalog blobs into queued
ingest jobs, its frozen validation cohort, and one live run. **What this is
not:** a benchmark, a retrieval measurement, or a corpus ingest. It answers one
question — *does a scoped, resumable, enqueue-only driver put the right
documents in the index and stay a no-op on a second pass?* — and it is still the
laptop, still `local_ai_gateway_p4`.

Two decisions were taken before any code (user, 2026-08-17) and both are load
bearing:

1. **~30 documents covering native, legacy conversion, OCR, mixed PDFs and
   spreadsheets, plus exactly one unsupported OLE2 file** to prove failure
   isolation. Not to be expanded into a benchmark.
2. **`nrb_extractions` must not pre-filter the driver** — no join, no import, no
   query. Recovery reuse will come from a new *versioned recovery cache*, never
   from the Phase 6 evidence table.

### 20.1 The tension in decision 2, and how the cohort resolves it

Route coverage is knowledge that exists only in Phase 6 evidence: the catalog
knows filename, MIME and section, never what came out of the parser. So a cohort
that covers five routes cannot be drawn from the catalog alone.

The resolution is that cohort selection is a **one-time offline act, frozen into
a committed file**, and the driver reads only that file. `nrb_extractions` never
enters the driver's import graph, query path or runtime dependencies — it
informed which 31 keys were written down, once. Three roles, drawn three
different ways (`scripts/nrb_p7_cohort.py` → `docs/nrb/phase7-validation-cohort.json`,
`cohort_sha256 f2d36b4c…`):

| role | n | how drawn |
|---|---:|---|
| anchor | 8 | hand-picked: the §17/§18.7 blobs, the only route-aware part |
| unknown | 22 | **blind** — ranked by `sha256(seed + content_sha256)`, no extraction evidence consulted |
| unsupported | 1 | one OLE2 `.xls` (§15.2), to prove a failed job isolates |

Coverage is guaranteed by the anchors, not by the draw. That is the honest way
round: a blind draw cannot guarantee it, and a route-aware draw of all 31 would
put Phase 6 evidence at the centre of the cohort instead of its edge.

**"Blind" means blind to ROUTE, not unrestricted.** The draw is scoped to
`extension IN ('pdf','xlsx','docx')` — catalog data, what NRB served — because
without it a random draw pulls images and OLE2 files and "exactly one
unsupported file" stops being true. And the pool is the **570 blobs that happen
to be fetched**, assembled by the Phase 6A benchmark, the 6B holdout and the
core fetch. That is not a random sample of 18,266 files, so **this cohort
supports no population claim about NRB** and the route split below must not be
read as one. Most of its members do have extraction rows from those earlier
passes; *unknown* means this cohort did not look, not that nobody ever has.

**It is deliberately not a `manifest`.** `manifest.build_manifest` takes a
`Sample` and certifies *sampling reproducibility*, admitting no second path by
which a key can enter. A hand-picked cohort has no sampling provenance to
certify, so it gets a plain ordered key list with its own sha256 instead —
enough to prove the driver ran on the cohort that was committed.

### 20.2 The driver

`app/nrb/corpus.py` (logic) + `scripts/nrb_rag_ingest_corpus.py` (CLI). The
8-blob smoke test `scripts/nrb_rag_ingest.py` is untouched and still guards
§17/§18.7.

- **Refuses to run without a scope**, like `nrb_fetch.py`. `--cohort` / `--key` /
  `--section` / `--owner` / `--year` / `--extension` / `--limit` compose; `--all`
  is the explicit way to mean the whole fetched catalog.
- **Selection is catalog-only**: `fetch_status = 'fetched'`,
  `DISTINCT ON (content_sha256)` with the lowest id as representative, stable
  order. Two catalog keys sharing bytes are ONE document — selecting both would
  inflate the conflict count with something that is not a conflict.
- **Skip-what-exists is an anti-join**, not an exception handler:
  `documents.content_hash` is `sha256(bytes)` and so is `nrb_files.content_sha256`,
  so the ordinary "I ran this yesterday" case selects nothing in one query. The
  anti-join repeats `ux_documents_active_content`'s own `status <> 'archived'`
  predicate, because an archived document must stay re-ingestable — skipping it
  would make archiving permanent.
- **`DocumentConflict` is still caught, but it means RACED**, not "already
  done", and is counted separately. A nonzero conflict count is evidence of
  concurrency, not of idempotence. The file written before the failed insert is
  compensated exactly as the upload route does.
- **Enqueue-only, always.** No in-process drain: that races the deployed worker,
  and `SKIP LOCKED` means the two split the scope rather than collide — quietly
  measuring neither.
- **Resumable**: per-document sessions, batch logging every 25. An interrupt
  keeps its progress because the anti-join no longer selects what committed.

`tests/test_nrb_corpus_ingest.py` — 10 tests, all passing — locks the second-pass
property, the shared-bytes collapse, archived re-ingest, raced-conflict counting,
`fetched`-only selection, per-department dedup, missing-blob isolation, and a
source-level guard (via `ast`, so the module's own prose about the rule does not
trip it) that `nrb_extractions` / `NRBExtraction` / `extractor_version` never
appear in the driver's code.

### 20.3 The run

`--department nrb-p7` (a NEW department, so §17/§18.7's 250 chunks stay intact
as comparable evidence), cohort scope, drained by a real
`python -m app.rag.worker`. All five recovery dependencies verified present
first — npttf2utf, rapidocr, onnxruntime, docling, pypdf, plus the lexicon —
because §18's whole lesson is that their absence produces a *clean-looking*
deployment.

**31 keys → 31 blobs selected → 31 documents created in 0.2 s.** Zero conflicts,
zero missing blobs, zero hash mismatches.

| | docs | chunks |
|---|---:|---:|
| ready | 30 | 1,029 |
| failed | 1 | 0 |

| route | chunks | documents |
|---|---:|---:|
| `native` | 628 | 8 |
| `legacy_conversion` | 350 | 18 |
| `ocr` | 51 | 6 |

Wall clock **2,271 s (37.9 min)** for 31 documents; mean 73.3 s, max 983.3 s
(`e75f209d1db7`, *Annual Report 2067-68 (Nepali)*, 342 chunks — a third of the
whole cohort's output in one file). **These are laptop VRAM-spill figures
(§18.5) and are not a server estimate.**

Anchors accounted for 8 documents / 250 chunks; the 22 blind unknowns plus the
OLE2 file for 23 documents / 779 chunks.

### 20.4 The anchors reproduce §18.7 exactly — 8/8

| blob | §18.7 chunks | now | routes | pages |
|---|---:|---:|---|---|
| `075bf12eb087` | 4 | **4** | native | 1–2 |
| `1a9b6321aa61` | 1 | **1** | legacy_conversion | 1 |
| `268bcfe86d03` | 1 | **1** | legacy_conversion | 1 |
| `3d2eca8b9f95` | 2 | **2** | ocr | 1 |
| `c298efaf1f16` | 4 | **4** | ocr | 1–3 |
| `e08988860534` | 75 | **75** | legacy_conversion + ocr | 1–50 |
| `7820b1f49fc1` | 9 | **9** | legacy_conversion | 1–4 |
| `8df7b02f8a13` | 154 | **154** | legacy_conversion | 1–46 |

Every count, route and page range identical, through a different driver, into a
different department. The mixed document still keeps its OCR'd page 1 beside 49
converted pages.

### 20.5 The second pass is a no-op, live

```
scope names 31 catalog keys; 0 blobs selected (not already in nrb-p7)
nothing to do: every blob in scope is already ingested here.
```

Exit 0, nothing created, nothing raised — the property the 8-blob smoke test
does **not** have (§19.3: it calls `create_document` with no handler and aborts
on the first existing blob). This is what makes an interrupted corpus pass
resumable rather than restartable.

### 20.6 The failure case did its job

The OLE2 file failed **mid-run**, not last: at the 17-minute mark the queue
stood at 24 succeeded / 1 failed / 5 queued / 1 running, and the remaining
documents ingested normally afterwards. Its recorded reason is specific enough
to act on rather than a bare "parse failed":

```
no indexable text: unsupported/no_native_parser, plan no_recovery, 0 pages
```

Which is §16's fail-closed rule reaching the far end of the pipeline intact: no
parser, no recovery plan, no text, an explicit failed job, and **nothing
indexed**.

### 20.7 What this run found

1. **A `failed` document is never re-selected.** The anti-join excludes
   everything that is not `archived`, so the OLE2 file — and any transiently
   failed document — stays out of a later pass. For a permanently unparseable
   file that is right; for a transient failure it is not, and there is currently
   no `--retry-failed` (fetch has one). Recorded, not fixed: the fix is a
   deliberate scope decision, not a bug patch.
2. **`RAG_DOCS_DIR` duplication, now measured.** 31 documents cost **31 MB**
   copied out of a 455 MB blob store. Trivial here, ~8.6 GB at corpus scale, and
   still the open decision from §19/§4 of the plan — copy, symlink, or teach the
   NRB branch to resolve from `filestore` (which restructures
   `_load_chunks_sync`, since it resolves the path *before* the NRB branch).
3. **The route split on blindly-drawn documents is native-dominated** — 628
   native chunks against 350 converted and 51 OCR'd. The anchors are legacy-heavy
   *by construction*, so this is the first split measured on documents chosen
   without route knowledge. **It is not a corpus estimate** (§20.1's pool
   caveat) and must not be quoted as one.
4. **Pre-existing test debris in `local_ai_gateway_p4`.** 190 documents in `ri*`
   departments dating from 2026-08-14, with `storage_key = 'k'`, committed by the
   RAG re-ingest integration tests rather than rolled back. Four carried stale
   `queued` jobs, which this run's worker claimed and failed instantly. That
   debris is also why `tests/test_rag_reingest_integration.py::
   test_department_filter_restricts_the_set` fails on any non-empty database — it
   asserts an *unscoped* document count of exactly 2. Pre-existing, unrelated to
   NRB, reported and not fixed.

### 20.8 Evaluation & Improvement (Phase 7 step 1)

**Success metric.** Re-run cost, and correctness of what lands. A second pass
over an unchanged scope must create zero documents and zero jobs; the anchors
must reproduce §18.7 per document. Both hold. Still a proxy for SQLs — nothing
here is user-facing.

**Eval.** The 8 anchors are the labelled set, scored per document on chunk count
and route: **8/8 agreement**. The second-pass-zero property has an automated
test that failed by construction before this work and passes now. What is *not*
evaluated: whether the 779 chunks from the unknowns are correct — no Nepali
reader has seen them, and §15's semantic verdicts remain `awaiting_nepali_review`.

**Feedback capture.** `ingest_jobs` holds per-job status, error, attempts and
timing; `documents.chunk_count` and `document_chunks.metadata` hold route,
page and `extractor_version` per chunk. `--json` writes the enqueue counters.
`--report` re-reads all of it without touching the queue.

**Review loop.** Before any scope expansion, and again after the recovery cache
lands (which changes re-run cost, the metric above). The check that matters
stays §18's: read the route split on known blobs, never job success.

### 20.9 The gate

Step 1 is done. **Not started, and not to be started without a decision:** the
versioned recovery cache (step 2 — its key is a composite recovery version, NOT
`extractor_version`, and it must cache unresolved pages with their reason or
every withheld page re-runs OCR forever), the supersession link (§19.3), the
`RAG_DOCS_DIR` duplication decision, and any corpus-scale ingest.

Unchanged and untouched: the Nepali semantic review, §17.6's broken-ToUnicode
native text, the npttf2utf GPL-3.0 distribution decision, full-corpus retrieval
quality, and server access (§19.1) — this run is still the laptop.

## 21. Phase 7 step 1.1 + step 2 — explicit retry, and the versioned recovery cache

**Date:** 2026-08-17. **Scope:** two commits. A `--retry-failed` path for the
corpus driver, and the recovery cache that makes a re-ingest reuse recovered
text instead of re-running npttf2utf and PP-OCRv5. **What this is not:** a
corpus ingest, a cohort re-run, or a benchmark. The 31-document cohort was not
re-run; the real-data evidence below is four named blobs.

### 21.1 Step 1.1 — the retry, and the three things it cannot reach

§20.7 item 1 recorded the defect: the anti-join excludes every non-`archived`
document, so a `failed` one is never selected again. Right for the cohort's OLE2
file, wrong for a transient failure, and there was no way to say which.

`corpus.select_retry_targets` + `corpus.requeue_failed`, behind
`--retry-failed`. Deliberately a **separate pair of functions**, not a flag
threaded through the create path, because it does a different thing: it creates
no `documents` row, copies no file, and only enqueues a job against a document
that already exists.

Three exclusions, each covering something the others do not:

| exclusion | what it stops |
|---|---|
| `status = 'failed'` | a `ready` document being requeued while it is serving; a `pending` one being queued twice (so a second `--retry-failed` before the worker drains is a no-op *before* the job-conflict guard fires); an `archived` one, which the ordinary create path already handles |
| no active job | a swept `failed` document still holding a `queued` row getting a second worker |
| join to `nrb_files` | `--retry-failed` silently adopting a failed ordinary upload in the same department |

The document row is reused — same id, `content_hash`, `storage_key`, metadata —
and status goes back to `pending` so a queued document does not also claim to
have failed. The previous failure stays on its own `ingest_jobs` row with its
error, so `--report` still shows what went wrong the first time. One session per
target, as in `create_ingest_targets`, so a target that raises cannot roll back
the ones requeued beside it.

**No transient-vs-permanent classifier**, by decision. Which failures are worth
retrying is an operator's judgement for now, and the flag is explicit precisely
so it can be. Run against the real scratch database, `--dry-run` correctly named
the one failed document in the cohort scope (`c434f463d638`, the OLE2 progress
report) and changed nothing.

8 new tests in `tests/test_nrb_corpus_ingest.py` (17 total, all passing). Two of
them read `ingest_jobs` **scoped to the test department**: the scratch database
carries unrelated pre-existing jobs (§20.7 item 4) and an unscoped assertion
would be about someone else's debris, not about this code.

### 21.2 Step 2 — why the cache key is split in two

One monolithic cache version would work and would be wrong in a specific,
expensive way: bumping the OCR model would invalidate every deterministic legacy
conversion in the corpus, and bumping npttf2utf would re-run every scan. So the
key is split along the only line that matters — *did the ROUTE change, or did
what the route PRODUCES change?*

**`base_version`**, on `nrb_recoveries`, is the ROUTING identity:

```
native-2 | recovery-1 | prov-1 | gate=0.8 | unjudged=0.8
```

the native-2 classifier, `recovery.RECOVERY_ROUTING_VERSION` (the plan ordering
and `route_page`'s rules), `provenance.PAGE_PROVENANCE_VERSION`, and the two
gate constants **read live** so editing `CONVERSION_GATE` or
`legacy_convert.UNJUDGED_MIN_LEGACY_RATIO` changes the key whether or not
anyone remembered to bump a version string. A change here invalidates the whole
document, correctly: the routes themselves may now differ, so no unit's cached
answer is still about the right question.

**`engine_version`**, per unit on `nrb_recovery_units`, is the identity of
whatever produced that unit's text:

| route | engine version |
|---|---|
| `native` | `passthrough/native-2` — parser changes are what `EXTRACTOR_VERSION` is documented to be bumped for, so it is reused rather than tracking pypdf/python-docx/openpyxl separately (which would make an openpyxl release invalidate every PDF) |
| `legacy_conversion` | `npttf2utf 0.3.7/Preeti/lexicon cc1fec3f2808` |
| `ocr` | `PP-OCRv5/devanagari/onnxruntime/docling 2.118.1; rapidocr 3.9.2; onnxruntime 1.23.2` |

The lexicon lives in the ENGINE version, not the base: it is a conversion guard,
so it changes what conversion produces, never where a page is routed.

**An absent dependency renders as `unavailable`, which is a version like any
other.** That single decision makes fail-closed and selectivity the same
mechanism: a page recorded `conversion_unavailable` on a deployment without
npttf2utf — §18's most dangerous failure, because it looks like a clean
deployment — can never be served once npttf2utf is installed, while its OCR'd
and native neighbours in the same document are still reused.

Invalidation semantics, as built:

| change | effect |
|---|---|
| embedding model | none — recovery does not embed |
| chunker | none — the cache stores TEXT, not chunks |
| OCR engine/model | OCR units only |
| converter / mapping / lexicon | `legacy_conversion` units only |
| routing / native classifier / either gate | the whole document |
| blob bytes | a different `content_sha256`, so a different row |

### 21.3 Schema

Migration **`714264eba2fd`**, `down_revision` **`b1bea6ac36c5`** — this branch's
actual head. Two new tables, no change to any existing one; the autogenerate
drift check afterwards was empty. Applied and verified against
`local_ai_gateway_p4` only.

`nrb_recoveries` — one row per `(content_sha256, base_version)`: `family`,
`plan`, `plan_reason`, `gate_ratio`, `warnings`, `unit_count`.
`nrb_recovery_units` — one row per unit: `unit_number`, `label`, `route`,
`reason`, `engine_version`, `ok`, `content`, `error`, `detail` JSONB, with
`ON DELETE CASCADE` and a unique `(recovery_id, unit_number)`.

Three things worth stating rather than leaving to be discovered:

1. **This table is a document store, and it is the only NRB table that is.**
   `nrb_extractions` carries `ck_nrb_extractions_preview_is_bounded`
   specifically to stop it becoming one. Here that is the point — the value of
   the cache is not re-running 2–4 s/page OCR — so there is no such bound. What
   is stored is exactly the text `rag.chunks_from_recovery` would chunk:
   `recovery._withhold` has already run, so the glyph-mapped original of an
   unresolved unit is not in the database and cannot be resurrected from it.
2. It is **global and department-agnostic**, keyed by bytes like `nrb_files`.
   Two departments ingesting the same blob share one recovery. No embedding, no
   `tsv`, no vector index; retrieval cannot reach it.
3. Rows under a superseded `base_version` are **kept side by side**, exactly as
   native-1 and native-2 extraction rows are. Deleting is an explicit operator
   action (`scripts/nrb_recovery_cache.py --purge`), never a side effect of a
   write — a cache row is also the record of what was indexed at the time.

**Branch integration is not solved here and must not be.** When
`feat/rag-source-citations` (stamped `d4a91f2c7b3e`, deferred) and this branch
meet, Alembic will have two heads and a merge revision will be needed. Nothing
was stamped, dropped or reconciled.

### 21.4 Granularity — the unit is whatever `recovery.py` already returns

No granularity was invented. A `PageText` is a unit:

- **PDF → per PAGE**, 1-indexed, the same number `provenance`,
  `read_pdf_pages` and `ocr.ocr_page` use, so a citation needs no translation
  table. `e08988860534` is the case that makes this necessary: page 1 OCR,
  pages 2–50 conversion, one document.
- **XLSX → per SHEET**, in workbook order, sheet name in `label`. Fake page
  numbers on a spreadsheet would make `document_chunks.page_number` a lie.
- **DOCX/TXT → unit 1**, one stream.

Non-PDF documents are **all-or-nothing by construction**: every unit of a
workbook shares one route, so "some units stale" cannot arise there and a stale
engine falls back to a cold run. Only PDFs have a partial path.

### 21.5 How the lookup integrates, and what `rag.py` now trusts

`recovery.py` remains the semantic owner. Two of its per-unit executors became
public — `convert_unit` and `ocr_unit` (previously `_converted_page` /
`_ocr_page`) — so a cache refreshing ONE stale page calls the same function a
cold run calls, with the same withholding rules. There is no second recovery
implementation.

```
worker._load_chunks            (async; branches on metadata.origin == "nrb")
  └─ recovery_cache.chunks_for_blob
       ├─ load(session, sha, base_version)      one SELECT, session closed
       ├─ asyncio.to_thread(rag.recover_and_chunk, ..., cached=…)
       │    └─ recovery_cache.resolve  →  warm | partial | cold
       │         ├─ warm     : rebuild PageTexts from rows. Nothing opened.
       │         ├─ partial  : re-read page texts (pypdf), re-execute ONLY the
       │         │             stale units via recovery.convert_unit/ocr_unit.
       │         │             The route, the reason and gate_ratio come from
       │         │             the cached header — no re-classification.
       │         └─ cold     : rag.recover_blob, exactly as before
       │    └─ rag.chunks_from_recovery   ← ONE chunking path, two sources
       └─ save(...)  unless warm (a warm hit has nothing to write)
```

`rag.parse_nrb_to_chunks` is unchanged in behaviour and still the no-database
entry point; `recover_and_chunk` is the new one that accepts a cached recovery
and returns what produced the chunks. The generic RAG path is untouched: a
non-NRB document goes straight to `parse_to_chunks` in a thread, with no session
and no NRB import.

**What `rag.py` now trusts, stated explicitly**, because its docstring used to
say the classification is re-run rather than read from a stored row. That
reasoning is not abandoned — it is what the versioning satisfies. A chunk is
still a function of the bytes, but the function is now NAMED: the row is keyed
on `sha256(bytes)` AND on a routing version covering the classifier, the routing
rules, the provenance algorithm and both gates, and each unit additionally
carries its engine's identity. What is trusted is a **version match**, never the
row's own claim about itself — `PageText.indexable` is recomputed from
`(ok, text)` on read rather than stored, so a row cannot assert a trust state
the current rules would refuse.

`nrb_extractions` is still off the ingestion path. `recovery_cache.py` carries
the same AST-level guard `corpus.py` does, and the temptation there is stronger
(that table already holds a classification) — which is why the guard is on both.

### 21.6 Unresolved outcomes are cached, with their reason

`ok = false` with empty text is a first-class cached outcome, not an omission.
Caching only the successes means a deterministically unrecoverable page re-runs
OCR on every ingest forever to reach the same conclusion. The reason travels
with it (`conversion_unavailable`, `conversion_unresolved`, `ocr engine
unavailable`), the page is still not indexable, and a document with no
indexable unit still raises the same actionable `NrbParseError` naming the
routing outcome — a warm hit does not turn a failure into a silent empty
success.

An `error IS NULL OR ok = false` CHECK keeps a successful unit from carrying an
error. The converse is deliberately not an invariant: `conversion_unresolved`
fails *with* text (the guards kept some lines) while an unavailable engine fails
with none.

A unit whose engine errored **transiently** is cached under a version that has
not moved, so it is reused until someone decides otherwise. Deciding is
`scripts/nrb_recovery_cache.py --purge`, a command rather than a heuristic. No
retry scheduler was built.

### 21.7 The small real-data check — four blobs, two passes

`scripts/nrb_recovery_cache.py --reuse-check`, which wraps the REAL converter
and OCR engine in counters. Reuse is proved by **zero calls**, not by equal
output: a converter that ran again and produced the same answer would look
identical. All five recovery dependencies verified present first (npttf2utf
0.3.7, rapidocr 3.9.2, onnxruntime 1.23.2, docling 2.118.1, pypdf, lexicon
`cc1fec3f2808`) — §18's lesson is that their absence produces a *clean-looking*
run.

| blob | | outcome | units | reused | run | npttf2utf calls | OCR pages | chunks | s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| `1a9b6321aa61` legacy | pass 1 | cold | 1 | 0 | 1 | 10 | 0 | 1 | 0.1 |
| | pass 2 | **warm** | 1 | 1 | 0 | **0** | **0** | 1 | 0.0 |
| `3d2eca8b9f95` OCR | pass 1 | cold | 1 | 0 | 1 | 0 | 1 | 2 | 6.6 |
| | pass 2 | **warm** | 1 | 1 | 0 | **0** | **0** | 2 | 0.0 |
| `7820b1f49fc1` stripped font | pass 1 | cold | 4 | 0 | 4 | 201 | 0 | 9 | 1.1 |
| | pass 2 | **warm** | 4 | 4 | 0 | **0** | **0** | 9 | 0.0 |
| `e08988860534` mixed | pass 1 | cold | 50 | 0 | 50 | 1,473 | 1 | 75 | 6.8 |
| | pass 2 | **warm** | 50 | 50 | 0 | **0** | **0** | 75 | 0.0 |

Second pass: **0 npttf2utf calls, 0 PP-OCR calls**, every document `warm`, chunk
counts identical. Those counts (1 / 2 / 9 / 75) are also §18.7's and §20.4's for
these four anchors, unchanged.

**Selective invalidation, on the real mixed document.** `e08988860534`'s cached
units were aged one route at a time:

| aged | outcome | reused | re-run | npttf2utf calls | OCR pages | chunks |
|---|---|---:|---:|---:|---:|---:|
| the 1 OCR unit | partial | 49 | 1 | **0** | 1 | 75 |
| the 49 conversion units | partial | 1 | 49 | 1,473 | **0** | 75 |

An OCR model change re-runs one page of fifty and touches no conversion; a
converter change re-runs forty-nine and touches no scan. That is the property
the two version domains exist for, measured rather than argued.

**End to end through the real worker.** `7820b1f49fc1` was re-ingested into
`nrb-p7` by `app.rag.worker` itself: `nrb recovery warm for d696be4e6a9e… : 4
units (4 reused, 0 recovered; converter 0, ocr 0)`, job `succeeded`, chunk count
9 → 9. That is the main end-to-end property — a re-ingest driven by anything
downstream reuses the recovery.

**Timings are laptop VRAM-spill figures (§18.5) and are not server estimates.**
The 46.7 s of that worker run is almost entirely model load and embedding; the
recovery part of it was the 0.0 s warm hit.

### 21.8 Tests

`tests/test_nrb_recovery_cache.py` — 25 tests, all passing. Most are pure:
`resolve` takes a `CachedRecovery` and returns a `RecoveredDocument`, so the
whole staleness matrix is testable in memory with counting stubs; the
persistence round-trip runs against real Postgres inside a rolled-back
transaction.

They cover: the converter and OCR each running once and not twice; a mixed PDF
reusing its OCR and conversion pages independently; an OCR bump invalidating
only OCR; a converter bump and a lexicon change invalidating only conversion;
installing a missing converter invalidating only the pages it could not do; a
routing-version bump invalidating the whole document; **every routing input
moving the base version**, asserted term by term rather than trusting the format
string; chunk-size changes not invalidating anything; page order, sheet labels
and converter/OCR provenance surviving reuse; the withheld original never
reaching the database; `indexable` being recomputed rather than stored; a
half-written entry reading as a miss; the generic RAG path never reaching the
cache; and the AST guard on `nrb_extractions`.

Suites: NRB **1,081 passed, 3 skipped**; RAG regression **260 passed, 1 failed**
— `test_rag_reingest_integration.py::test_department_filter_restricts_the_set`,
the pre-existing §20.7 item 4 failure that asserts an unscoped document count of
exactly 2 against a database with 190 rows of unrelated `ri*` debris. Reproduced
unchanged, not fixed, not cleaned. Everything else: **425 passed, 2 skipped**.

### 21.9 Evaluation & Improvement (Phase 7 step 2)

**Success metric.** Re-run cost, which §19.4 named and could not yet measure at
this stage: a second recovery of an unchanged blob must execute zero converter
and zero OCR invocations. Measured above at 0 and 0 across four blobs and 56
units. Still a proxy for SQLs — nothing here is user-facing.

**Eval.** Two labelled sets. The 25 focused tests are the invalidation matrix,
scored pass/fail: 25/25. The four real blobs are the reuse set, scored on engine
calls and on chunk count against §18.7: 4/4 on both, plus 2/2 on the selective
invalidation directions. What is **not** evaluated: whether the cached text is
semantically correct — §15's verdicts remain `awaiting_nepali_review`, and the
cache faithfully preserves whatever conversion produced, right or wrong.

**Feedback capture.** `nrb_recovery_units` is itself the record: route, engine
version, `ok` and reason per unit, queryable as a GROUP BY
(`scripts/nrb_recovery_cache.py --stats`). The worker logs the outcome, reuse
split and engine-call counts per document at INFO. `ingest_jobs` still holds
per-job status and error.

**Review loop.** Before any corpus-scale ingest, and again after the first one —
`--stats` is the check, and the thing to read is the route/engine split, never
job success (§18).

### 21.10 The gate

Steps 1.1 and 2 are done. Recovery persistence **is** ready for supersession
work: a republished NRB file already produces a different `content_sha256` and
therefore a different cache row with no collision, so the supersession task is
purely about `documents` rows and never about invalidating recovered text.
Supersession was **not started**.

**Not started, and not to be started without a decision:** the supersession link
(§19.3 item 3), the `RAG_DOCS_DIR` duplication (§20.7 item 2, measured at 31 MB
for 31 documents and still open before full-corpus ingest), Phase 8's
`search_nrb_documents`, and any corpus-scale ingest.

Unchanged and untouched: the Nepali semantic review, §17.6's broken-ToUnicode
native text, the npttf2utf GPL-3.0 distribution decision, full-corpus retrieval
quality, native-2 (not modified; native-3 not started), the frozen Phase 6A/6B
evidence, the Phase 7 cohort (not re-run, not expanded), the `ri*` scratch-DB
debris, and server access (§19.1) — this is still the laptop.

## 22. Phase 7 step 3 — supersession: which version of an NRB document is current

**Date:** 2026-08-17. **Scope:** the lifecycle that lets a republished NRB file
replace the one currently being searched, without ever leaving the corpus with
nothing to search. **What this is not:** an API, a scheduler, a corpus ingest, or
a re-run of the 31-document cohort.

### 22.1 The rule, and why it is one transaction rather than two commits

A is serving, B is the candidate.

```
B fails at ANY stage   →  A is still searchable. Nothing was archived.
B succeeds             →  B is current, A is archived.
```

This is not achieved by ordering two commits carefully. It is achieved by doing
the archive of A and the activation of B in **one transaction** — the one
`ingest.replace_chunks` already owns — so a failure anywhere in it takes the
archive with it. `app/rag/worker._activate` is that transaction.

Promotion runs *before* `replace_chunks` inside it, which looks backwards and is
not: `replace_chunks` flips B to `ready`, and the new unique index would refuse
that while A is still `ready`. Archiving A first is what makes the flip legal,
inside a transaction where "first" is invisible to everyone else. And reaching
that transaction at all means B already recovered, chunked and embedded
successfully — the expensive, failure-prone work is behind it and the vectors
are in memory.

### 22.2 The logical source identity is `comparison_key`

`nrb_files.comparison_key` — the percent-decoded attachment URL, unique in the
catalog by `ux_nrb_files_comparison_key`, already written onto every NRB
document's `metadata` by both ingest drivers. It identifies the FILE across
versions of its bytes. No new field, no fuzzy matching, no migration for it.

| candidate | verdict |
|---|---|
| `content_sha256` / `content_hash` | identifies the **version**, not the source — the whole reason this phase exists |
| `page_url` / `nrb_sources.url_key` | **concrete collision:** a post can carry two attachments (a circular plus its annex — §3 measured 0.7% of posts). Both share one `page_url`, so promoting the circular would archive the annex |
| title / filename / date / text similarity | never. NRB publishes near-identical Devanagari titles across years and 3 documents have no title at all |

**Two catalog keys that share bytes.** Deduplication is by bytes:
`select_ingest_targets` does `DISTINCT ON (content_sha256)` keeping the lowest
`nrb_files.id`, so N aliases of one blob produce ONE document carrying the
representative's `comparison_key`. They therefore share one logical identity,
chosen deterministically rather than by whichever pass ran first. If those
aliases later diverge, each becomes its own logical source from that point and
neither supersedes the other — the honest outcome, since they are no longer the
same file. No duplicate recovery work either way: recovery is keyed on bytes.

### 22.3 Schema — one index, no new column

Migration **`8f2d1c05a7b4`**, `down_revision` **`714264eba2fd`** (this branch's
actual head; `d4a91f2c7b3e` untouched, nothing stamped, the future merge
revision still required and still not solved here).

```sql
CREATE UNIQUE INDEX ux_documents_nrb_current_source
    ON documents (department_id, ((metadata ->> 'comparison_key')))
 WHERE status = 'ready'
   AND metadata ->> 'origin' = 'nrb'
   AND metadata ->> 'comparison_key' IS NOT NULL
```

The logical identity, the version identity and the current/archived state all
already existed on `documents`. What JSONB alone could not do is **refuse** two
current versions of one source: `ux_documents_active_content` is keyed on
`content_hash`, and two versions of a republished circular have two different
hashes, so it is satisfied by precisely the state this phase exists to prevent.
Row locking in `supersession.py` serialises two promoting workers; the index is
what makes the invariant a property of the database rather than of that file
continuing to be correct — the same posture as the composite chunk FK and the
status CHECKs.

Partial and expression-based, so it touches nothing else: a row without the key
indexes as NULL and never conflicts, leaving ordinary uploads, typed text and
pre-Phase-7 NRB documents exactly as they were. Verified before creation against
`local_ai_gateway_p4`: 39 NRB documents, all carrying a `comparison_key`, zero
duplicate `(department, key)` pairs among `ready` rows. Declared on the model,
hand-written in the migration, and added to `_AUTOGEN_SKIP_INDEXES` — Alembic
reflects neither the expression nor the `WHERE`, so without the exclusion every
drift check proposes dropping it. Same treatment as the HNSW/GIN indexes.

### 22.4 Candidate creation, and what the driver now reports

Selection is **still** anti-joined on `content_hash`, and that is right: it is
the version identity and it is what makes a repeat pass free. What it cannot do
is express supersession — new bytes are a hash nobody has indexed either way. So
`corpus.summarise_scope` classifies the scope by logical key and the report names
the three cases separately:

```
scope names 31 catalog keys / 31 blobs
  already_current        31
  new_source             0
  replacement_candidate  0   (supersede their predecessor only if their ingest succeeds)
  retry_failed           1
```

`already_current` reuses the exact predicate the anti-join uses, so the two can
never disagree about the same blob. Nothing in the driver archives anything —
promotion happens at the end of a successful ingest, in the worker's own
transaction, precisely so a candidate that never succeeds cannot retire the
version that is serving.

### 22.5 Ordering, and what the catalog does not give us

**Reported explicitly, as asked.** `nrb_files` holds one `content_sha256` per key
and **overwrites it in place**. It keeps no history of prior versions, so there
is no catalog-side version number, sequence or timestamp to order B against C.

What exists is the order in which our own driver OBSERVED each version —
`documents.created_at`, tie-broken by `id` — and because the driver is the only
thing that mints these rows and the catalog only ever offers the current version,
that order faithfully records catalog succession. **It is our record, not NRB's.**
A stronger guarantee would need the catalog to retain superseded shas per key
(a `nrb_file_versions` table), which is a Phase 4 change and is not made here.

Job completion order is explicitly not used:

- a document promotes itself over strictly **older** siblings only;
- if a strictly **newer** sibling is already `ready`, it archives **itself**.

Both halves are needed. Without the first, a late-finishing B would archive C.
Without the second, B would go live after C and stay there.

A consequence worth stating: a newer successful version archives an older
`failed` one too, so **a superseded failure is no longer retryable**
(`select_retry_targets` requires `failed`, not `archived`). An operator cannot
resurrect a stale revision after the fact. That also means the self-archive
branch is defensive rather than routine — the normal flow archives the older
candidate before it could try. It is kept because the state is still reachable by
a hand-repaired database, and the alternative is an IntegrityError that reads as
a bug rather than as a decision.

### 22.6 Failure behaviour, proved

| failure | outcome |
|---|---|
| recovery raises on B | `_activate` is never reached; job `failed`, B `failed`, **A untouched and `ready`** |
| embedding raises on B | same — the failure is before the transaction |
| the activation transaction fails midway (after the archive, before the chunks) | **rollback un-archives A**; A `ready`, B `pending`, no `superseded_by` written |
| worker crashes before B is ready | the stale sweep fails the job; A untouched |
| B is archived mid-ingest by a newer C | `replace_chunks`' existing `DocumentGone` guard refuses; C stays current |
| B retried later and succeeds | only then is A archived |

The third row is the one that needed a test rather than an argument, and it has
one (`test_a_failure_inside_the_activation_transaction_rolls_the_archive_back`,
which forces a dimension mismatch between the archive and the chunk write).

### 22.7 Retrieval

Already correct, and confirmed rather than changed: `app/rag/retrieval.py:96`
filters `WHERE doc.status = 'ready'`, and `archive_document` deletes the chunks
outright. Two independent mechanisms, both of which already excluded an archived
version. No ranking change, no reranking, no `search_nrb_documents`. The
supersession tests assert it through the **production `_SEARCH_SQL`** rather than
a paraphrase.

### 22.8 Tests — 19, all passing

`tests/test_nrb_supersession.py`, in the existing rolled-back-transaction style
and scoped to a test-only department so the shared scratch database's debris
(§20.7 item 4) is irrelevant. Covers all 15 required properties: A active; the
unchanged second run zero-work; new bytes reported as a replacement candidate;
A serving while B is pending; recovery failure, activation failure and the
transaction rollback all leaving A active; successful promotion; retrieval
returning B and never A; `--retry-failed` promoting only on success; a post-
promotion run selecting nothing; the database refusing two current versions;
both B-then-C orders; a different logical source (the annex) never superseded;
catalog rows, blobs and recovery rows all surviving; and a non-NRB document's
lifecycle unchanged.

**One test is not transactional and says so:** the two-worker race needs two real
connections, so it commits into its own department and removes it in a `finally`.
Two workers activate B and C concurrently; exactly one version ends `ready`, it
is the newest, and the original is archived.

Suites: NRB **1,100 passed / 3 skipped**; RAG regression **260 passed, 1 failed**
— `test_department_filter_restricts_the_set`, the pre-existing §20.7 item 4
dirty-database assertion, reproduced unchanged and not worked around; everything
else **425 passed / 2 skipped**.

### 22.9 The controlled real-data exercise

`scripts/nrb_supersession_exercise.py` — four generated PDFs under one
`comparison_key`, driven by the real corpus driver, the real
`app.rag.worker` (real recovery, real embedding) and the production retrieval
SQL. Synthetic on purpose and it is the stronger choice: the whole exercise is
about version ORDER and no real catalog record has one, so replaying successive
versions would mean mutating real catalog evidence. **ALL CHECKS PASSED:**

| step | result |
|---|---|
| ALPHA ingested | `new_source`, current, retrievable |
| BRAVO published | `replacement_candidate` (not `new_source`); ALPHA still current |
| BRAVO's ingest FAILS | BRAVO `failed`; **ALPHA still current and still retrievable** |
| `--retry-failed` BRAVO | succeeds → BRAVO current, ALPHA archived + `superseded_by`, retrieval returns BRAVO only |
| CHARLIE FAILS | BRAVO still current and retrievable |
| DELTA succeeds | DELTA current; the still-failed CHARLIE archived by it and **no longer retryable**; retrieval returns DELTA only |
| history | all 4 blobs on disk, the catalog row intact, and the recovery rows of the archived versions kept |

The department, the catalog row and the jobs are removed at the end (and by
`--cleanup`). Timings are not reported: they are laptop VRAM-spill embedding
figures (§18.5) and mean nothing about a server.

### 22.10 A defect found while inspecting the catalog, and NOT fixed here

`records.file_from_attachment` always builds a `FileRecord` with
`fetch_status = FETCH_PENDING`, and `catalog.FileState.differs_from` compares
`fetch_status` — so **a re-sync after a fetch pass marks every already-fetched
file `changed` and writes `pending` back over it** (`_file_values` includes the
column; `update_files` applies it). Measured directly, not inferred.

Consequence: the next fetch pass would re-download the entire 8.6 GB corpus.
It is **not** a supersession-correctness problem — identical bytes hash
identically, so `content_sha256` does not move and the driver's anti-join still
skips them — but it makes "NRB republished this file" indistinguishable from "we
re-synced", and it must be fixed before any scheduled re-sync. The fix is to
stop `differs_from` comparing `fetch_status`/`blocked_reason` (they are OUR
state, not upstream facts) or to carry the existing state onto the record before
diffing. Left alone deliberately: it is a Phase 4/5 change with its own test
surface, and folding it into a supersession commit would hide it.

### 22.11 Evaluation & Improvement (Phase 7 step 3)

**Success metric.** Zero windows without a current version. Concretely: after
any sequence of publishes, failures and retries, exactly one `ready` document
exists per logical source and it is the newest successfully indexed one. The
unique index makes the first half unfalsifiable at the database level; the
second half is what the tests and the exercise measure.

**Eval.** Two labelled sets. 19 focused tests scored pass/fail: 19/19. The
four-revision real-data exercise scored on 17 assertions across six lifecycle
steps: 17/17. Not evaluated: retrieval QUALITY after a supersession — the
exercise proves the right *document* comes back, not that it ranks well, and
§15's semantic verdicts remain `awaiting_nepali_review`.

**Feedback capture.** `documents.metadata.superseded_by` / `superseded_at` are
the per-document record of what replaced what; `--report` shows current versus
superseded counts per department; the worker logs the promotion outcome
(`ready` / `promoted` / `superseded`) per job.

**Review loop.** Before the admin API is built, and again after the first real
republication is observed in production — the ordering caveat in §22.5 is the
thing to revisit then, because it is the only part of this design resting on our
own record rather than on NRB's.

### 22.12 The gate

Step 3 is done. **The Phase 7 backend state machine is now safe enough for the
admin API and run-status work**: discovery, fetch, recovery, ingest, retry and
supersession are all idempotent or explicitly opt-in, all failures are
fail-closed, and every transition is recorded in a queryable row. The API was
**not started**.

**Not started, and not to be started without a decision:** the admin trigger and
status API, any scheduler or cron, the `RAG_DOCS_DIR` duplication (§20.7 item 2 —
still required before full-corpus ingest), Phase 8's `search_nrb_documents`, any
corpus-scale ingest, and the §22.10 sync fix.

Unchanged and untouched: the Nepali semantic review, §17.6's broken-ToUnicode
native text, the npttf2utf GPL-3.0 distribution decision, full-corpus retrieval
quality, native-2 (not modified; native-3 not started), the recovery-cache
versioning, the frozen Phase 6A/6B evidence, the Phase 7 cohort (not re-run, not
expanded), the `ri*` scratch-DB debris, and server access (§19.1) — this is still
the laptop.

## 23. A sync-state fix, and the shared pipeline runner (Phase 7 step 4)

**Date:** 2026-08-17. **Scope:** two commits. The fix for the sync defect §22.10
recorded, and the one orchestration path the CLI, a future admin API and a
future schedule will all call. **What this is not:** an HTTP endpoint, a UI, a
scheduler, or a corpus ingest.

### 23.1 The sync fix — state ownership, not one fewer comparison

`records.file_record` always builds a candidate with `fetch_status='pending'` —
that is the constructor default, decided by the host guard before anything has
been downloaded. `FileState.differs_from` compared that column against the
stored one, so **every successfully fetched row read as "changed" on the next
sync and `_file_values` wrote `pending` back over it.** The next fetch pass
would then re-download the whole corpus and overwrite every `content_sha256`.

The second-sync-is-all-zero invariant held only while nothing had been fetched
yet, which is exactly the state Phase 4 was tested in.

The fix names the two owners:

| columns | owner | how they change |
|---|---|---|
| `source_url`, `filename`, `reported_mime_type`, `extension`, `resource_type`, `type_source`, `reported_bytes`, `wp_attachment_id`, `host` | discovery (upstream facts) | `differs_from` → `_file_facts` in the UPDATE |
| `fetch_status`, `blocked_reason`, `fetch_error` | the fetch stage (operational) | **only** `FileState.fetch_transition` |

`fetch_transition` returns `{}` — leave it alone — for the ordinary case, and
names the upstream field for each of the three exceptions:

1. **Became unfetchable.** → `blocked_host`, **unless already `fetched`**, which
   is left completely alone. Its bytes are verified, on disk and referenced by
   `documents` and the recovery cache; `select_ingest_targets` requires
   `fetched`, so flipping it would silently drop the file out of RAG selection.
   Recording the reason while keeping `fetched` is not a middle path either —
   `ck_nrb_files_blocked_reason` makes that state unrepresentable, which is the
   database agreeing that a blocked reason belongs to a blocked row.
2. **Became fetchable again.** → `pending`.
3. **Upstream REPLACED the resource** — a different `filesize` or a different
   attachment id at the same `comparison_key` → `pending`, so the fetcher
   downloads the new version, mints a new `content_sha256` and **triggers the
   §22 supersession lifecycle**. This is the trigger that was missing.

Two guards on rule 3. It fires only when BOTH values are known: `None → 123456`
is WordPress starting to report sizes, not a changed file, and treating it as a
replacement would re-download much of the corpus the first time that happened.
And the content columns are not cleared — those bytes are still on disk and
still referenced by the version currently being served, exactly as
`fetch._row_for` already reasons about a failed attempt.

`update_files` now groups rows by key set before `executemany` (as
`record_fetch_outcomes` does), because most rows write facts only and a few also
move the fetch state. New counter **`files_refetch_queued`** — the only file
counter with a cost attached, and a nonzero value on a routine sync is the
signal that NRB republished something.

**10 regression tests**, including the headline `sync → fetch → sync`, the
consequence at the selector (`select_fetch_targets` returns nothing), a failed
fetch not being silently promoted while `--retry-failed` still sees it, both
forms of the replacement trigger, the metadata-becoming-available guard, both
guard-verdict transitions, and a pure unit test of the rule.

### 23.2 The runner — one path, three future callers

`app/nrb/pipeline.py`. The CLI, the admin API and a schedule must not be three
implementations of the same sequence; they are three callers of `start`.
**Nothing shells out to a script and no stage is reimplemented** — `run_sync`,
`run_fetch` and `run_extract` were already application services with injectable
engines, and the only stage that lived solely in a script was the RAG one, which
became `corpus.run_rag_enqueue`. `scripts/nrb_rag_ingest_corpus.py` was then
rewritten to call it, so the two cannot drift.

```
start(scope)
  ├─ advisory lock (locks.PIPELINE_LOCK_KEY) — or PipelineBusy carrying the active run
  ├─ sweep any run left `running` by a dead orchestrator
  ├─ open an nrb_pipeline_runs row
  ├─ sync    → sync.run_sync            (nrb_sync_runs stays the detailed record)
  ├─ fetch   → fetch.run_fetch          (nrb_fetch_runs)
  ├─ extract → extract.run_extract      (nrb_extractions)
  ├─ rag     → corpus.run_rag_enqueue   (documents + ingest_jobs)
  │             …recording WHICH jobs, in nrb_pipeline_run_jobs
  └─ release the lock → status = awaiting_jobs
```

It does **not** recover, chunk, embed, archive, purge the recovery cache or
drain jobs. Recovery reuse stays the worker's through the versioned cache;
supersession stays the worker's activation transaction (§22) — an orchestrator
that archived could retire a version before its replacement succeeded.

### 23.3 Schema

Migration **`1fb5a0d183d6`**, `down_revision` **`8f2d1c05a7b4`** (this branch's
actual head; `d4a91f2c7b3e` untouched, nothing stamped, the merge revision still
required and still not solved). Two tables, no change to any existing one.

`nrb_pipeline_runs` — `trigger` (cli|api|schedule), `requested_by`, `status`,
`stage`, `department`, `scope` JSONB, `counters` JSONB, `error`, plus
`created_at`/`started_at`/`heartbeat_at`/`finished_at`. It is the orchestration
record and **not a second database**: `nrb_sync_runs`, `nrb_fetch_runs`,
`nrb_files`, `nrb_extractions`, `documents`, `ingest_jobs` and the recovery
cache remain the detailed truth for their own stages, and `counters` is a
bounded per-stage rollup of integers, never a per-document log.

`nrb_pipeline_run_jobs` — `(run_id, job_id, document_id, reason)`. A separate
table rather than a column on `ingest_jobs`, because that table is shared with
ordinary department uploads and has no business knowing NRB exists — and because
a `--retry-failed` job belongs to the run that retried it, not to the run that
first created the document (hence `reason` = `created` | `retried`).

`queued` is deliberately not a status: there is no queue in front of the
orchestrator, and inventing a state nothing can produce would make a future UI
show a status that never resolves.

### 23.4 Lifecycle, and why `awaiting_jobs` exists

```
running ──(a stage raised)──────────────────────────────► failed
   │
   └─(staging done)─┬─ queued nothing ──► succeeded | partial | failed
                    └─ queued N jobs ───► awaiting_jobs ──reconcile()──►
                                              succeeded | partial | failed
```

"The pipeline finished enqueueing" is not "the NRB update completed". The RAG
worker is a separate process by design, so a run that staged 400 documents is
not done. `reconcile(run_id)` recomputes the terminal status from **this run's
own jobs** and is callable from any process at any time — including long after
the orchestrator exited, which is exactly the crash case.

`resolve_status` is pure and its order matters: **waiting beats everything**
(one queued job means the run has not finished, whatever else went wrong), then
succeeded+failed → `partial`, only-failed → `failed`, otherwise `succeeded`.
Item-level stage failures count toward `partial` too — a fetch that lost one
file did not fully update the corpus — while a stage that RAISED is recorded as
`error` and ends the run `failed` immediately.

### 23.5 Locking and crash recovery

A Postgres advisory lock (`PIPELINE_LOCK_KEY = b"NRB_PIPE"`), the mechanism
`locks.py` already uses for sync, fetch and extract, for the reason given there:
**it dies with the connection**, so a killed orchestrator leaves nothing to
clean up. Not an in-process lock, and not a lock row.

A second trigger does not queue and does not wait — it raises `PipelineBusy`
**carrying the run in progress**, which is the answer an admin endpoint wants to
return. Each stage still takes its own lock underneath, so a manual
`nrb_sync.py` running alongside is refused by the sync's lock rather than
corrupting anything.

The run ROW is a record, not a mutex, so a crashed orchestrator leaves one stuck
in `running`. The next run sweeps it — safely and **with no timeout to tune**,
because holding the lock is proof that no orchestrator is alive. `awaiting_jobs`
runs are never swept: they hold no lock, they are legitimately unfinished, and
their jobs are still draining. `heartbeat_at` advances at each stage boundary
for observability and nothing depends on it.

**Revised by the §24 lifecycle review:** `awaiting_jobs` *does* block a second
trigger. The paragraph below as originally written ("does NOT block a new run")
was the wrong call — see §24.3.

### 23.6 Scoping and retry

`PipelineScope` carries every bound the stage services already accept — keys,
sections, owners, years, resource types, extensions, limit — plus `stages` (run
a slice; `sync` is the one stage that cannot be scoped, since it reads NRB's
whole REST corpus by nature) and `retry_failed`.

`retry_failed` **defaults False**. A routine update — including a future
scheduled one — must not keep re-attempting a permanently unparseable file. And
it is explicitly *not* a recovery refresh: unresolved recovery outcomes are
cached deliberately (§21.6), so a retry re-runs the ingest without re-running
OCR on a page the pipeline already decided it cannot read. Purging that is a
separate, explicitly-requested operation
(`scripts/nrb_recovery_cache.py --purge`) and this task did not build a schedule
for it.

The CLI **refuses to run unbounded without `--all`**. The code supports an
incremental complete update — every stage is idempotent — but that is a
different statement from "we have approved running it on ~19k files", and the
`RAG_DOCS_DIR` decision (§20.7 item 2) is still open.

### 23.7 CLI

`scripts/nrb_pipeline.py` — parses arguments, calls `pipeline.start`, prints the
run id, stage counters and job counts, exits non-zero on a failed run and 3 on
`PipelineBusy`. `--status [--run N]` reconciles and reports. It contains no
pipeline logic. The existing stage scripts are unchanged and remain the right
tools for diagnosing one stage.

### 23.8 Tests

`tests/test_nrb_pipeline.py` — **23 tests, all passing**. The three upstream
stages are stubbed (they reach a central bank's website, download gigabytes and
parse hundreds of documents; their own suites cover them) and everything below
is real: run rows, the RAG stage against a real catalog fixture, the job
association table, the advisory lock on a genuinely separate connection, and the
status arithmetic.

Covered: stage order; a second unchanged run doing no upstream work and queuing
nothing; a real second orchestrator getting `PipelineBusy` with zero stages run;
an abandoned `running` run swept while an `awaiting_jobs` one is not; scope,
trigger, `requested_by` and counters persisting; **a run counting only its own
jobs** while a stranger's job created in the same second is ignored; waiting
while any job is unfinished; succeeded / partial / failed; reconcile being
idempotent and never rewriting `finished_at`; a stage raising and staging
nothing; the rag stage refusing without a department; retry off by default and
on when asked (recorded as `retried`, not `created`); and the generic RAG flow
being untouched — `ingest_jobs` and `documents` gained no column.

Suites: NRB **1,133 passed / 3 skipped**; RAG regression **260 passed, 1 failed**
— `test_department_filter_restricts_the_set`, the pre-existing §20.7 item 4
dirty-database assertion, reproduced unchanged.

### 23.9 The bounded integration exercise

No corpus ingest and no cohort re-run. Two bounded passes against the existing
`nrb-p7` department:

1. `--stage rag` over the 31-key cohort → `already_current=31`, nothing queued,
   run **succeeded** immediately. That is the second-run-is-free property on
   real data.
2. `--stage rag --retry-failed --trigger api --requested-by admin@example.com`
   → queued exactly **1** job (the cohort's OLE2 file), run **`awaiting_jobs`**,
   `rag jobs queued=1`. `--status` before the worker still reported
   `awaiting_jobs`. One real `app.rag.worker` pass drained it; the file failed
   again for the right reason (`unsupported/no_native_parser, plan no_recovery`).
   `--status` then reconciled the run to **`failed`**, `rag jobs failed=1`.

The run counted **1** job, not the 30 other documents' historical jobs, which is
the explicit relation doing its work. Department state afterwards: 30 `ready` /
1 `failed` — exactly as before. No timings are reported: they would be laptop
VRAM-spill figures (§18.5).

### 23.10 Evaluation & Improvement (Phase 7 step 4)

**Success metric.** Two. For the fix: a sync following a fetch must leave zero
files selectable for download — measured, `select_fetch_targets` returns `[]`.
For the runner: every question a status view needs (is it running, who asked,
what scope, which stage, how many queued/ready/failed, did it finish) must be
answerable from one row plus its job relation, with no time-window guessing.

**Eval.** 10 sync regression tests and 23 pipeline tests, scored pass/fail:
33/33. The bounded exercise scored on 6 lifecycle assertions: 6/6. Not
evaluated: behaviour at corpus scale — every number here comes from fixtures and
a 31-key cohort, and the runner has never orchestrated more than one queued job.

**Feedback capture.** `nrb_pipeline_runs` (status, stage, counters, error,
timings) and `nrb_pipeline_run_jobs` (which jobs, created vs retried) are the
record; the stage tables keep their own detail. `--status` and `--json` expose
it without touching the queue.

**Review loop.** Before the admin API is exposed, and again after the first
multi-hundred-document run — the counters are designed for a UI that does not
exist yet, and the first real one will say whether they are the right ones.

### 23.11 The gate

Both tasks are done. **The backend is ready for `POST` admin trigger, `GET` run
status and `GET` NRB status**: there is one orchestration entry point taking a
scope and a trigger, a durable run identity that survives the process, DB-backed
exclusion that a crash cannot wedge, an explicit run→job relation, and a status
that distinguishes staging from waiting from terminal. The endpoints were **not
implemented**.

**Not started, and not to be started without a decision:** the HTTP endpoints,
any UI, any cron or systemd timer, the `RAG_DOCS_DIR` duplication (§20.7 item 2 —
still required before full-corpus ingest), recovery-refresh scheduling, Phase 8's
`search_nrb_documents`, and any corpus-scale ingest.

Unchanged and untouched: the Nepali semantic review, §17.6's broken-ToUnicode
native text, the npttf2utf GPL-3.0 distribution decision, full-corpus retrieval
quality, native-2 (not modified; native-3 not started), the recovery-cache
versioning, supersession semantics, the frozen Phase 6A/6B evidence, the Phase 7
cohort (not re-run, not expanded), the `ri*` scratch-DB debris, and server access
(§19.1) — this is still the laptop.

## 24. Pipeline lifecycle review — two invariants a status API will lean on

**Date:** 2026-08-17. **Scope:** one focused review of `app/nrb/pipeline.py` and
the run/job models, and the two smallest fixes it justified. No new table, no
migration, no HTTP.

### 24.1 What was already safe, and why that was not enough

The scenario the review started from — *`--retry-failed` reuses `ingest_jobs` row
J, J's status changes, and Run A's history changes with it* — **cannot happen
today, and the premise is worth stating precisely**: `jobs.enqueue` always
INSERTs, so a retry creates a NEW job against the existing document rather than
reviving the old one; `claim_next` only touches `queued` rows and `sweep_stale`
only `running` ones. A terminal job row is therefore never mutated.

So the invariant held — but **derivatively**, borrowed from `app/rag/jobs.py`
rather than held here, and `_job_counts` recomputed a terminal run's counts from
live rows on every read. Two ways that goes wrong:

1. A future in-place retry (or any `UPDATE ingest_jobs` by hand) would silently
   rewrite a finished run's history.
2. **Reachable today:** if anything raises *after* `_record_jobs` has associated
   the jobs — a database blip in `_mark_stage` — the run is recorded `failed`
   while its jobs are still `queued`. They then drain, and the run would keep
   `status = failed` while its job counts drifted to `succeeded: N`: a
   self-contradictory row for a UI to render.

### 24.2 The fix — freeze the counts when the run leaves the active states

`_freeze` is one JSONB key, `counters['jobs']`, stamped by both terminal paths
(`reconcile`'s transition out of `awaiting_jobs`, and `_finish` for the
stage-failure and queued-nothing paths). `_job_counts` returns the stamp for a
terminal run and queries live only while the run is `running`/`awaiting_jobs`.

No migration and no second table: `counters` is already the bounded per-stage
integer rollup, and `jobs` is the one key not contributed by a stage — hence its
name. Runs written before this change carry no stamp and fall back to the live
query, which is correct for them precisely because their jobs are terminal and
immutable.

`reconcile` on a terminal run remains a no-op that never rewrites `finished_at`;
a status endpoint will poll it on every request.

### 24.3 `awaiting_jobs` now blocks a second trigger — a reversed decision

§23.5 said an `awaiting_jobs` run "does NOT block a new run", on the grounds that
every stage is idempotent. That was wrong, and the review reversed it: a waiting
run's documents are mid-ingest, and a second orchestrator would stage more work
on top of a corpus state the first has not finished establishing.

**The advisory lock cannot express this.** It is released the moment
orchestration returns, while the jobs it queued outlive it by design. So
exclusion is now the durable row, and `start` does three things in a
load-bearing order, all while holding the lock:

1. `sweep_abandoned` — fail runs left `running` by a dead orchestrator. Safe with
   no timeout because holding the lock proves none is alive.
2. `settle_waiting` — reconcile every `awaiting_jobs` run. **This is what stops
   the new rule becoming a trap:** `reconcile` only advances a run when somebody
   reads it, so a run whose jobs had all finished but which nobody polled would
   otherwise block every future trigger forever. A stale wait now costs one
   query, not an operator.
3. Refuse with `PipelineBusy` if a run is *still* `running` or `awaiting_jobs`,
   carrying that run so a future endpoint can return it.

Holding the lock across check-then-insert is what makes this safe against a
concurrent starter; the lock is still the mechanism, just no longer the whole
answer.

### 24.4 Tests — 28 in `tests/test_nrb_pipeline.py`, all passing

Five added, two existing ones corrected for the reversed decision:

- a terminal run reports what happened *during it*: Run A fails, Run B retries
  the same document and succeeds, **and the old job row is then flipped to
  `succeeded` by hand** — Run A still reads `failed` / `{failed: 1}` on two
  successive reads, with `finished_at` unmoved;
- a run that goes terminal with jobs in flight freezes `{queued: 1}` and does not
  drift to `{succeeded: 1}` once the worker drains them;
- a waiting run blocks a second trigger with **no lock held**, returns itself in
  `PipelineBusy`, runs zero stages, and is not swept;
- a waiting run whose jobs all finished does not wedge the pipeline — the next
  `start` settles it to `succeeded` and proceeds;
- the active/terminal status split is asserted against `PIPELINE_STATUSES`, so
  adding a status without deciding which side of the gate it falls on fails here.

Corrected: the second-unchanged-run test now drains the first pass before
triggering the second (the honest sequence); the sweep test no longer plants an
`awaiting_jobs` row, because `settle_waiting` legitimately settles a waiting run
that has no jobs — a separate test now covers a *genuinely* waiting one.

Only `app/nrb/pipeline.py` changed, so only the pipeline suite was run, plus a
live CLI smoke: a `--stage rag` pass over the 31-key cohort reported
`already_current=31`, settled `succeeded` immediately, and stamped
`counters.jobs = {}`.

### 24.5 The gate

Unchanged from §23.11, with both invariants now held here rather than inherited:
**the lifecycle is safe enough for the thin admin API.** A terminal run is
immutable history, an active update — in either of its two states — refuses a
duplicate, and a crashed orchestrator is recoverable without a timeout. The
endpoints are still **not implemented**.

## 25. Phase 7 step 5 — the thin NRB admin API

**Date:** 2026-08-17. **Scope:** three admin endpoints over the pipeline service
built in §23–§24. **What this is not:** a UI, a scheduler, a dashboard framework,
or a second source of truth. No migration.

### 25.1 Routes and the conventions reused

| route | purpose |
|---|---|
| `POST /v1/nrb/runs` | trigger an update — 202 with the run, 409 with the active one |
| `GET /v1/nrb/runs/{id}` | one run, reconciled through the service if still waiting |
| `GET /v1/nrb/status` | operational state for the future admin UI |

`app/nrb/router.py` + `app/nrb/schemas.py`, mounted in `app/main.py` beside the
other routers. Everything is the repository's existing machinery, nothing
NRB-specific: `APIRouter(prefix="/v1/nrb", tags=["nrb"])`,
`Depends(require_admin)` from `app/auth/dependencies.py`, `Depends(get_session)`,
Pydantic v2 models with `ConfigDict`, `HTTPException` with `status.HTTP_*`, and
202 for accepted work as the document upload route already does. **No NRB auth
was invented.**

Authorization is therefore the existing two-layer one, asserted in both
directions: no credentials → **401** from the shared `HTTPBearer`; valid
credentials without the admin role → **403 "Admin privileges required"** from
`require_admin`. An ordinary member cannot trigger ingestion, and the refusal
happens before any stage runs.

### 25.2 Thin means thin

Each handler parses a request, calls **one** application service, and shapes the
answer. `pipeline.start` still owns the sequence, the advisory lock, the durable
run row, the active-run gate and the status arithmetic; `pipeline.reconcile` owns
the terminal verdict. Nothing shells out — the CLI and the router are two callers
of one implementation. A source-level test asserts the router never references
`subprocess`, `advisory_lock`, `run_sync`/`run_fetch`/`run_extract`,
`create_ingest_targets`, `requeue_failed`, `sweep_abandoned` or `resolve_status`,
because "thin" is a property that erodes quietly.

One service function was added and one was promoted:

- `corpus.nrb_rag_counts` — NRB's RAG readiness, counted from `documents` /
  `ingest_jobs` filtered to `metadata->>'origin' = 'nrb'`. It exists because the
  equivalent queries lived only in a script's `do_report`.
- `pipeline.active_run` — was `_active_run`; now public and defaulting to **both**
  non-terminal statuses, which is the "is an update in progress" question the
  status endpoint and the trigger gate both ask.

### 25.3 POST semantics

Request is a deliberate SUBSET of `PipelineScope`: `department`, `stages`, the
six bounds, `limit`, `retry_failed`. `extra="forbid"`, so an unknown field is a
422 rather than being silently dropped. `trigger` is recorded as `api` and
`requested_by` as the admin's email — the reason those columns exist.

Response is the same envelope either way, so a client needs one parser:

```json
{"started": true,  "run": { …RunOut… }}   // 202
{"started": false, "run": { …RunOut… }}   // 409, this is the run already active
```

**Staging is synchronous in the request, and that is a consequence of the
requirement that `PipelineBusy` be answerable in the response.** A `rag`-only
pass over a named cohort is sub-second; a `sync` is minutes, because it reads
~190 pages of NRB's REST API. What is *not* synchronous is recovery, chunking and
embedding — those remain the separate worker's, so the run comes back
`awaiting_jobs` and the client polls. **The API still never parses or embeds**,
and `import app.main` was re-verified to pull in none of
docling/torch/rapidocr/onnxruntime/npttf2utf.

Moving staging off-request belongs with the scheduler step and is not built.

### 25.4 `PipelineBusy` → 409, never 500

Both exclusion mechanisms produce one externally understandable meaning:

- another orchestrator holds the advisory lock (`running`), and
- a durable run is still `running` or `awaiting_jobs` — the lock is released the
  moment staging returns while its jobs outlive it (§24.3).

`pipeline.start` raises `PipelineBusy` carrying the run for both, so both become
**409** with `started: false` and that run in the body. A caller retries later or
polls the run; it never needs to know which fired. The one degenerate case — the
lock held but no durable row yet naming the holder, i.e. a run opening at that
instant — is the same 409 with a plain `detail`, because there is no run to hand
back.

### 25.5 Status payload

Four blocks, every number somebody else's. Live against the scratch database:

```json
{
  "active_run": null,
  "latest_run": { "id": 115, "trigger": "cli", "status": "succeeded", … },
  "catalog": { "sources": 18577, "active_sources": 18577, "files": 18266,
               "blocked_files": 600, "duplicate_comparison_keys": 0, … },
  "files":   { "pending": 17666, "fetched": 570, "failed": 27, "blocked": 3,
               "distinct_blobs": 569, "bytes_on_disk": 474782059 },
  "rag":     { "documents": {"ready": 38, "failed": 1},
               "jobs": {"succeeded": 39, "failed": 2},
               "ready": 38, "failed": 1, "superseded": 0,
               "chunks": 1279, "departments": 2 }
}
```

`catalog` and `files` are `catalog.catalog_counts` / `catalog.fetch_counts` — the
same numbers `nrb_sync.py` and `nrb_fetch.py` print. `rag` is
`corpus.nrb_rag_counts`. `active_run` is the field a UI leans on: non-null means
a trigger would be refused, and it is **the same run a 409 would return**, so the
UI can grey out its own button from the status poll. Waiting runs are settled
first (`pipeline.settle_waiting`), so an update whose jobs finished but which
nobody polled does not show as active forever. `?department=` narrows only the
`rag` block; the catalog is global.

**Deliberately absent: `nrb_extractions` counts.** That table is Phase 6
classifier evidence, nothing on the ingestion path reads it (§19.3, §20.1), and
putting it in an operational view would invite a reader to treat it as pipeline
state.

### 25.6 Full-corpus safety

Three layers, none of them a permission that could be granted:

1. `RunTriggerIn` **requires a bound** — keys, sections, owners, years,
   resource_types, extensions or limit. An unbounded request is a **422** naming
   what is missing, not a 403.
2. `all_files` is **not a field**, and `extra="forbid"` means sending it is a 422
   rather than a silently ignored parameter. The router passes `all_files=False`
   unconditionally.
3. `--all` remains **CLI-only**, where an operator at a terminal is making a
   considered decision. §20.7 item 2 (`RAG_DOCS_DIR` duplication) is still open
   and is still the thing that must be decided before any full-corpus run.

### 25.7 Tests

`tests/test_nrb_api.py` — **19 tests, all passing** (47 with the pipeline suite).
Real Postgres + `TestClient` + the real auth router, in the style of
`test_rag_documents_api.py`. The three upstream stages are stubbed (network,
gigabytes, CPU — and a `POST` stages synchronously); `app.nrb.pipeline` is **not**
stubbed, because calling it is the router's entire job.

Covered: member 403 with nothing run, anonymous 401, member barred from status;
202 shape with `trigger=api`/`requested_by`, stage order driven through the
service, and counters surfacing; `retry_failed` carried through and defaulting
off; a stage subset; the unbounded 422; `all_files` refused by `extra="forbid"`;
rag-without-department and unknown-stage 422s; 409 carrying the active run with
the identical body schema; **a second trigger refused while a run is
`awaiting_jobs` with no lock held**; run read-back shape; a terminal run read
twice returning byte-identical JSON including `finished_at`; 404 for an unknown
run; the status envelope and its four blocks; `?department=` narrowing only
`rag`; `active_run` reporting the run a trigger would be refused for; and the
source-level thinness guard.

Narrow affected tests also run: `test_nrb_corpus_ingest.py` (18 passed — the AST
guard still holds after adding `nrb_rag_counts`) and
`test_docling_is_not_imported_at_module_scope`. No full suite, no corpus, no live
sync, no OCR, no cohort re-ingest.

### 25.8 Evaluation & Improvement (Phase 7 step 5)

**Success metric.** An operator (later, a UI) can trigger a bounded update, learn
that one is already running, and read enough state to decide what to do next —
without SSH, without the CLI, and without any endpoint being able to start a
full-corpus run by accident.

**Eval.** 19 API tests scored pass/fail: 19/19, plus a live `GET /v1/nrb/status`
against the scratch database returning the payload above. Not evaluated: latency
under a real `sync` (the request would last minutes — measured only as a design
consequence, not a benchmark), and anything about concurrent HTTP callers beyond
the single-run gate the pipeline suite already covers.

**Feedback capture.** `nrb_pipeline_runs.trigger`/`requested_by` now record that
an API caller asked and who they were; the router logs a refused trigger with the
active run's id. Nothing new is stored.

**Review loop.** When the UI is built — the status payload's shape is a guess at
what a UI needs, and the first real one will say which fields are missing and
which are noise. And again before a scheduler, which is also when staging should
move off-request.

### 25.9 The gate

**Ready for the thin UI.** Three endpoints, one auth pattern, one envelope for
the trigger, an `active_run` a UI can poll to keep its own button honest, and a
terminal run whose JSON does not change under polling.

**Not started, and not to be started without a decision:** the UI itself, any
cron or systemd timer (and with it moving staging off-request), the
`RAG_DOCS_DIR` duplication (§20.7 item 2 — still required before full-corpus
ingest), recovery-refresh scheduling, Phase 8's `search_nrb_documents`, and any
corpus-scale ingest.

Unchanged and untouched: the Nepali semantic review, §17.6's broken-ToUnicode
native text, the npttf2utf GPL-3.0 distribution decision, full-corpus retrieval
quality, native-2 (not modified; native-3 not started), the recovery-cache
versioning, supersession semantics, the frozen Phase 6A/6B evidence, the Phase 7
cohort, the `ri*` scratch-DB debris, and server access (§19.1) — this is still
the laptop.

## 26. Phase 7 step 6 — orchestration leaves the HTTP request

**Date:** 2026-08-17. **Scope:** the execution boundary. `POST /v1/nrb/runs` now
durably accepts a request and returns; a dedicated process stages it. **What this
is not:** a UI, a scheduler, a task queue library, or any change to what the RAG
worker owns.

### 26.1 The problem §25 exposed

`POST` executed the whole orchestration inline, because that is what made
`PipelineBusy` answerable in the response. So a request including `sync` held an
HTTP connection open for minutes while it read ~190 pages of a central bank's
REST API — and worse than the latency, an accepted run lived only in the
gateway's memory until it finished. A restart lost it silently.

### 26.2 Admission and execution are now two service functions

```
pipeline.request_run(scope, trigger, requested_by)   →  a `queued` row. Returns.
pipeline.execute_run(run_id)                         →  claim, stage, record.
```

`POST` calls the first and nothing else — measured **78 ms** live against the
scratch database. `app/nrb/runner.py` calls the second. `pipeline.start` survives
as the explicit composition of the two, used by `scripts/nrb_pipeline.py
--run-now` and by tests, and **never by the API** — a source-level test fails if
the router so much as mentions it.

The RAG worker's half is untouched: recovery, the versioned recovery cache,
chunking, embedding and supersession remain `app.rag.worker`'s. Two processes,
two jobs, and that split is still why the API image needs neither Docling nor an
OCR stack.

### 26.3 The lifecycle, with a real `queued` state

```
queued ──(runner claims)──► running ──┬─ queued nothing ─► succeeded|partial|failed
   │                           │      └─ queued N jobs ──► awaiting_jobs ──►
   │                           │                             reconcile() ──► terminal
   └─(nothing runs it yet;     └─(process dies)─► swept `failed` by the next
      blocks new requests)         process that can prove none is alive
```

`queued` was deliberately absent in §23 — "a status nothing can produce would
make a UI show a state that never resolves" — and it earns its place now that
something really does sit in front of the orchestrator. `queued` is also a
**stage**, so an unclaimed run does not have to claim a stage it has not reached;
`stage='sync'` on a run nobody has started would read as a sync in progress.

### 26.4 Migration `f4c1a90b7d62` — required by existing constraints

`down_revision` `1fb5a0d183d6`. No column added, no data rewritten. It exists
because `nrb_pipeline_runs` carries three CHECKs that enumerate exact strings, and
all three forbid the new lifecycle: `ck_nrb_pipeline_runs_status` (the `queued`
status), `ck_nrb_pipeline_runs_stage` (the `queued` stage), and
`ck_nrb_pipeline_runs_finished`, whose predicate lists the statuses that must
have a NULL `finished_at`. Editing a CHECK's vocabulary is DDL or nothing — which
is exactly the rule CLAUDE.md already states.

It also adds **`ux_nrb_pipeline_runs_one_active`**: `UNIQUE` over the constant
`(true)` restricted to `('queued','running','awaiting_jobs')` — the singleton-row
idiom. Admission left the orchestrator, so `POST` inserts without taking the
advisory lock (it must return in milliseconds), and two simultaneous requests
could both pass a plain SELECT gate. The gate still runs, because it is what
produces the useful 409 body; the index is what makes the gate not have to be
correct. A lost race surfaces as `IntegrityError` → caught → answered with the
run that won. Same posture as `ux_documents_active_content`.

The deferred lineage (`d4a91f2c7b3e`) is untouched, nothing is stamped, and the
merge revision is still required and still not solved here.

### 26.5 The runner

`app/nrb/runner.py`, run as `python -m app.nrb.runner`, in `app.rag.worker`'s
restrained style: sweep, poll, execute, sleep, and signal handling. It contains
**no locking, no transitions and no stage logic** — every safety property belongs
to the service.

It runs on the **gateway image**, not the worker image: `sync` and `fetch` are
httpx, `extract` is pypdf/openpyxl/python-docx (all in `requirements.txt`), and
the RAG stage copies bytes and inserts rows. Re-verified that
`import app.nrb.runner, app.main` pulls in none of
docling/torch/rapidocr/onnxruntime/npttf2utf. Compose gains one `nrb-runner`
service — same image as `gateway`, different command — and the p4 overlay points
it at the scratch env file alongside the other three.

Exclusion is unchanged in mechanism: `execute_run` holds `PIPELINE_LOCK_KEY` for
the whole orchestration, and the claim itself is `SELECT … FOR UPDATE SKIP
LOCKED` on the one row, so two runners pass over each other rather than both
running it.

### 26.6 A deadlock the review caught, and `recover_abandoned`

Moving admission behind the singleton index introduced a failure the tests found
before the design settled: a run left `running` by a killed runner **occupies the
only active slot**, so nothing new can be accepted — and `execute_run`'s own
sweep cannot help, because it only runs when there is a `queued` run to execute,
and none can be created. One crash would have wedged the pipeline permanently.

`pipeline.recover_abandoned` is the fix: take the lock, sweep `running` corpses,
settle `awaiting_jobs` runs whose jobs have all finished, release. It is called
**unconditionally, before looking for work**, by every process that is able to
orchestrate — `runner.run_once` and `pipeline.start`. `LockBusy` means a runner
genuinely is orchestrating, so there is nothing abandoned to find: not an error,
not a wait. Holding the lock is still what makes the sweep sound with no timeout
to tune.

### 26.7 The normalized busy response

One envelope for every outcome, so a client branches on `started` alone:

```json
{"started": true,  "run": {…}, "detail": null}    // 202, a queued run
{"started": false, "run": {…}, "detail": "…"}     // 409, the active run
{"started": false, "run": null, "detail": "…"}    // 409, lock held, no row yet
```

The third line is the case §25 answered with a different body (`HTTPException`'s
`{"detail": …}`) — a second schema for a client to parse, over a window measured
in milliseconds. `RunOut | None` normalises it. The locking was **not** redesigned
to eliminate that window.

### 26.8 The CLI

`scripts/nrb_pipeline.py` now **queues by default** and prints the runner command;
`--run-now` requests and then executes in-process. That is `request_run` followed
by `execute_run` — the same two service functions in the same order the runner
uses, not a second orchestration path. `--status` is unchanged. The stage-specific
scripts are untouched and remain the right tools for diagnosing one stage.

### 26.9 Tests

**37 pipeline + 20 API = 57**, all passing, plus `test_nrb_corpus_ingest.py` (18)
and the Docling import guard as the two narrow neighbours this could cross.

New: requesting a run executes nothing (the stage recorder is empty and no
document is created); a queued run is readable through a session the requester
never touched and is exactly what `claim_next` finds; the runner claims it and
drives `queued → running → awaiting_jobs`; a second `_claim` returns None;
executing a run that is no longer queued is a no-op; a queued run refuses a
second request **and** the database refuses the state directly; a crashed runner
blocks admission until `sweep_abandoned` clears it, then admission works again; a
run that stages nothing is terminal on the spot with frozen job counts; and the
scope survives the process boundary (`scope['key_list']` — `as_dict` stores
`keys` as a count, right for a status view and useless for execution, and a
silent widening from one key to "everything matching the other bounds" is exactly
the scope creep the `--all` guard exists to prevent).

Updated for the deliberate contract change: the API tests now assert admission
rather than completion, and stand in for the runner with `_run_it(run_id)` when a
test needs a run to have happened. The busy tests cover the queued gate, the
awaiting gate and the null-run case.

Live: `POST` returned **202 in 78 ms**; a second `POST` returned **409** naming
run 373 as `queued`; `GET /v1/nrb/status` reported it as `active_run`; the real
runner claimed it, logged `run 373 -> succeeded`, and afterwards `active_run` was
null and a third `POST` was accepted. The exercise department was removed and
`nrb-p7` is unchanged at 30 ready / 1 failed.

### 26.10 Evaluation & Improvement (Phase 7 step 6)

**Success metric.** Two. `POST` returns in well under a second regardless of the
stages requested — measured at 78 ms for a four-stage request. And no accepted
run is lost: acceptance is a committed row before the response is written.

**Eval.** 57 focused tests, pass/fail: 57/57. The live sequence above scored on
five transitions: 5/5. Not evaluated: two runner replicas under real contention
(covered only by the lock and the SKIP LOCKED claim in tests), and behaviour when
a runner is absent for a long time — a queued run simply waits, which is correct
but means a UI must distinguish "queued" from "queued and nobody is running".

**Feedback capture.** `nrb_pipeline_runs` now records the whole lifecycle
including the accepted-but-unclaimed window; the runner logs each transition.
Nothing new is stored.

**Review loop.** With the UI, which is the first thing that will care whether
`queued` needs a "no runner detected" signal; and again before cron, which is
the caller that can queue faster than a runner drains.

### 26.11 The gate

**Ready for the thin UI.** Trigger is fast and durable, the lifecycle has a state
for every real condition, a crash cannot wedge admission, and one envelope covers
every trigger outcome.

**Not started, and not to be started without a decision:** the UI, cron/systemd
(the runner is the process it would trigger through, not replace), the
`RAG_DOCS_DIR` duplication (§20.7 item 2 — still required before full-corpus
ingest), recovery-refresh scheduling, Phase 8's `search_nrb_documents`, any
corpus-scale ingest, and GPU-server deployment.

Unchanged and untouched: the Nepali semantic review, §17.6's broken-ToUnicode
native text, the npttf2utf GPL-3.0 distribution decision, full-corpus retrieval
quality, native-2, the recovery-cache versioning, supersession semantics, the
frozen Phase 6A/6B evidence, the Phase 7 cohort, the `ri*` scratch-DB debris, and
server access (§19.1).

## 27. Alembic lineage — the §9.10 point-4 decision is made (no reconciliation needed on this branch)

**Date:** 2026-08-17. **Question §9.10 point 4 left open:** what happens to the
deferred citations lineage when NRB is merged. **Decision (the user's):** citations
**stays deferred** and NRB merges **first**. **Consequence:** there is nothing to
reconcile in the migration GRAPH on `feat/nrb-sitemap` — it is already a single
clean linear head on top of `main`, and the only divergence artifact is one stamped
database, which stays exactly as §9.10 point 5 requires.

### 27.1 The topology, re-measured (git only, no DB)

Both feature branches fork from the SAME commit — `main`'s head `c33c0fd56028`
(add rag tables):

```
main head = c33c0fd56028
     ├── feat/nrb-sitemap ──► 9a1c4f7b2e05 → 2b7f5c9d1a34 → b1bea6ac36c5
     │                        → 714264eba2fd → 8f2d1c05a7b4 → 1fb5a0d183d6
     │                        → f4c1a90b7d62 (HEAD)      [7 NRB migrations, linear]
     └── feat/rag-source-citations ──► d4a91f2c7b3e (chat_messages.sources)  [DEFERRED]
```

`9a1c4f7b2e05.down_revision = c33c0fd56028` and `d4a91f2c7b3e.down_revision =
c33c0fd56028` — the two are **siblings off `main`**, confirming §9.11. There is no
fork *on this branch*; `alembic heads` prints exactly one.

### 27.2 The decision, and how it revises §9.11

§9.11's *preferred* route was **citations-first**: land `feat/rag-source-citations`
on `main`, then rebase `feat/nrb-sitemap` and re-point `9a1c4f7b2e05` at
`d4a91f2c7b3e`. Its whole rationale was that the dev DB is already stamped at
`d4a91f2c7b3e`, so citations-first lets that database later apply exactly the NRB
migrations with **zero** special handling.

That route requires **un-deferring citations now**, which §9.10 declines. So the
order flips to **NRB-first**, which is the only route consistent with keeping
citations deferred. It agrees with §9.11 on the thing that matters — **no Alembic
merge revision, ever** (the graph stays linear) — and differs only in order and in
one accepted cost: the dev DB stays stranded at `d4a91f2c7b3e` until citations is
picked up. It already is stranded; NRB dev/test runs on `local_ai_gateway_p4`; the
dev DB carries **no** NRB schema. Nothing gets worse, and §9.10 point 5 still holds
in full — no stamp, no drop, no recreate, no editing `d4a91f2c7b3e`.

### 27.3 Proof the NRB-first merge is clean (run 2026-08-17)

* **Offline, script only.** `alembic upgrade head --sql` resolves `base → head`,
  emits all **12** revisions (main's 5 + NRB's 7) in order, exit 0, and references
  `d4a91f2c7b3e` **zero** times. The chain is self-contained.
* **On a real Postgres.** `local_ai_gateway_p4` sits at `f4c1a90b7d62`, reached via
  this exact chain, so every NRB migration has already applied cleanly on top of
  `main`'s baseline against a live database.
* **Schema disjointness.** p4 has **no** `chat_messages.sources` column — the
  deferred schema never leaked into NRB's database. The two lineages are disjoint in
  the graph AND on disk.

### 27.4 The runbook

**Merging NRB to `main` (citations deferred):** `main` is at `c33c0fd56028`;
`feat/nrb-sitemap` is 7 linear migrations ahead. The merge adds a single linear
head — **no merge revision, no rebase of `9a1c4f7b2e05`** (that rebase belongs to
§9.11's citations-first route, which this decision does not take). A database being
upgraded must be at `c33c0fd56028` or an earlier ancestor, **not** at
`d4a91f2c7b3e`; a database stamped at the sibling revision (the current dev DB) is
handled by the citations step below, not by the NRB merge. Verify a real DB reaches
`f4c1a90b7d62`, and — per §18 — verify a worker by its route split on a known blob,
never by migration success.

**Un-deferring citations later (the citations owner's, not NRB's):** rebase
`d4a91f2c7b3e` so its `down_revision` becomes the then-current `main` head, turning
the sibling into a descendant — no merge revision, because by then there is one
head to sit on. The dev DB, already at `d4a91f2c7b3e` with the `sources` column but
no NRB schema, is the only database that then needs care, and it is reconciled *at
that point* by whoever owns citations. This is the step §9.10 point 4 defers; NRB
does not touch it.

### 27.5 Evaluation & Improvement (Alembic lineage)

**Success metric.** Merging `feat/nrb-sitemap` to `main` yields exactly one Alembic
head, and a database at `main`'s baseline reaches it with `alembic upgrade head` and
no missing-revision error. Proxy for SQLs only; nothing here is user-facing.

**Eval.** Two checks, both 2026-08-17: offline `base→head` resolution (12/12
revisions, 0 references to `d4a91f2c7b3e`) and a live Postgres already at head via
the chain (p4). Both pass. Not evaluated, by design: the citations-side dev-DB
reconciliation, which is out of scope until citations is un-deferred.

**Feedback capture.** `alembic heads` (must stay 1) and `alembic history` are the
standing signals; a second head appearing off any point other than the current head
is the regression this section guards against. No new store.

**Review loop.** At the NRB→`main` merge (execute §27.4), and again if citations is
un-deferred. If a future branch adds a migration off a point other than the current
single head, §27.1's topology is stale and must be re-measured before merging.

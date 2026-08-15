# NRB Phase 6A — native extraction + quality profiling

**Date:** 2026-08-15 · **Branch:** `feat/nrb-sitemap` · **Scratch DB:** `local_ai_gateway_p4`

## 1. The question this phase answers

> Can the files NRB publishes be extracted into trustworthy text before we put
> anything into RAG?

Not "does a parser return text" — **does the text mean what the document says**.
Phase 6A ends at a measured classification. No OCR, no chunking, no embeddings,
no `documents`/`document_chunks` rows, no `ingest_jobs`, no
`search_nrb_documents`, no tool registration, no endpoint.

## 2. The evidence that shaped the design

Probed 2026-08-15 against the 49 blobs Phase 5 already fetched (all circulars,
all PDF, the regulatory core):

* `pypdf` extracted a text layer from **49/49**. 380 pages in **9.2 s** (~41
  pages/s), on base dependencies, no torch.
* **Devanagari characters found: 0.** Across every file.
* The text is latin-dominant garbage. Verbatim from the first page of one
  circular:

  ```
  ffihW\ffifiHrz\reU=,.
  iqrn rrq *+,
  qtq i.: YYlqqoYr/{
  Web Site: www.n6.org.np
  ```

Three things follow, and they are the whole design:

1. **The failure mode is not "no text".** A file with no text layer is trivially
   detectable. The dangerous case is text that *parses cleanly and is wrong* —
   which is what 49/49 of the regulatory core produced. The quality classifier is
   the deliverable, not a side-check.
2. **Genuine English fragments are interleaved** (`Web Site`, `www`). Any
   detector that asks "is this English?" on the whole document will be pulled
   toward yes by the letterhead. It has to measure *structure*, not presence.
3. **We cannot tell legacy-font from bad-embedded-OCR from the bytes**, and we do
   not need to: the remedy for both is re-extraction from the image. They share
   one status and one reason code.

## 3. Architecture

```
scripts/nrb_extract.py            CLI. Scope REQUIRED (the nrb_fetch.py rule).
  → app/nrb/sampling.py           deterministic stratified selection (pure)
  → app/nrb/extract.py            the pass: select → load blob → extract → classify → record
       → app/nrb/extraction.py    format dispatch → ExtractionResult
       → app/nrb/quality.py       pure metrics + deterministic classification
  → app/nrb/catalog.py            + select_extract_targets / record_extractions / extraction_counts
  → app/nrb/report.py             + summarize_extraction / render_extraction
alembic/versions/<rev>_add_nrb_extractions.py
```

Same layering as Phases 4–5: `quality.py` and `sampling.py` are pure and carry
the tests that matter; `catalog.py` is set-based data access that never commits;
`extract.py` owns the transaction and the advisory lock; `report.py` renders.

`app/rag/parsing.py` is **not modified.** Its Docling CPU/no-OCR pinning is
load-bearing for department RAG and stays exactly as it is.

### 3.1 Why pypdf screens and Docling only calibrates

The brief assumed profiling runs under the existing Docling parser. Measured,
that is 20–40× slower per page on CPU and reads *the same embedded text layer* to
answer *the same question*. Docling's value — layout analysis, table structure,
`prov[0].page_no` provenance — is what Phase 7 needs for chunking, and buys
nothing for "is this text trustworthy".

So: **pypdf is the screen**, and a bounded Docling pass over ~40 manifest files is
the **calibration**, whose output is an agreement rate between the two engines'
status verdicts. That converts "pypdf is a fair proxy" from an assertion into a
measurement, and it is also the honest answer to the phase question *"is native
Docling sufficient for a meaningful percentage of the corpus?"*

**The calibration compares EXTRACTION, not pipelines.** It does not call
`parse_to_chunks`: that applies `merge_blocks`, `drop_small_blocks` and
front-matter skipping on top of Docling, so a disagreement could come from RAG's
chunk filtering rather than from what Docling read off the page. Instead the
adapter reuses `parsing._docling_converter()` — the lowest-level existing helper,
and reused deliberately rather than reimplemented because it carries the CPU
pinning and `do_ocr=False`, and a hand-rolled copy could silently drift into
enabling OCR. It then walks `document.iterate_items()` itself to collect raw
text, and runs **the same `quality.measure_text` and `quality.classify`** over
both engines' output. Like is compared with like.

A guard test asserts the converter really is CPU/no-OCR, so a future change to
`app/rag/parsing.py` breaks the calibration loudly instead of quietly OCRing.

The report is not a single agreement number. It records, per file, both statuses
and reasons, `char_count`, `devanagari_ratio` and the three core legacy metrics
from each engine, plus bounded previews of both — and it counts the two
asymmetric cases explicitly: **Docling rescues pypdf** (pypdf `needs_ocr`/
`suspicious`, Docling `extracted`) and **pypdf rescues Docling**. The first is
the one that would invalidate the screen, and burying it inside an average is
exactly how it would be missed.

Docling stays a worker-only dependency, imported inside the function, never at
module scope — the existing subprocess test that pins this keeps passing.

### 3.2 The one refactor

`app/files/documents.py::_read_pdf` gets its pypdf call extracted into a
`read_pdf_pages(path) -> PdfPages` helper returning per-page text plus the
encrypted/page-cap handling it already has; `_read_pdf` then builds its line
stream from that. Behaviour is unchanged and the existing document tests guard
it. Result: **one pypdf call site in the repository**, and NRB gets per-page text
without a second PDF stack.

That is the whole reuse boundary. `.docx` reuses `app/files/documents.py`
unchanged; `.xlsx` reuses `app/files/readers.py` (`inspect_workbook` +
`open_sheet_rows`, `data_only=True`) exactly as `app/rag/parsing.py` does.

## 4. Format behaviour

| Format | Parser | Notes |
|---|---|---|
| **pdf** (16,563) | pypdf, per page | page count, per-page text, per-page char counts. Page cap 500, `pages_skipped` recorded. |
| **docx** (13) | `app/files/documents.py` | reused as-is; no page concept. |
| **xlsx** (1,251) | `app/files/readers.py` | structural extraction: sheets, rows, non-empty cells, header rows. **No formula evaluation** (`data_only=True`), no macros, no eval. Quality rules are structural, not linguistic. |
| **xls** (303) | none | `unsupported`. openpyxl cannot read OLE2. Counted and sized for Phase 6B. |
| **doc** (21) | none | `unsupported`. Nothing in the dependency set reads it. |
| **image** (115) | none | `needs_ocr` by construction — a valid file whose text is pixels. Never `failed`. |
| archive / web / unknown | none | `unsupported`, with the sniffed family recorded. |

Dispatch is on `nrb_files.sniffed_mime` (our own magic-byte determination) with
`extension` as the tiebreak — never on NRB's `reported_mime_type`, which is the
claim Phase 5 exists to check. `sniff.family_for` supplies the family.

## 5. Quality metrics

All computed in `app/nrb/quality.py`, pure, from the extracted text alone.
Persisted in `metrics` JSONB; the five that Phase 6B will filter on are also
columns.

**Volume** — `char_count`, `non_whitespace_chars`, `token_count`, `line_count`,
`non_empty_lines`.

**Integrity** — `printable_ratio`, `control_char_ratio`,
`replacement_char_count` and `replacement_char_ratio` (U+FFFD — a real mojibake
tell; the count is what a human reads, the ratio is what the rules threshold on).

**Script** — `devanagari_ratio`, `latin_letter_ratio`, `digit_ratio`,
`punctuation_ratio`, `whitespace_ratio`. Ratios are over non-whitespace
characters, so a document's indentation cannot move its script profile.

**Structure (PDF)** — `page_count`, `pages_with_text`, `text_page_coverage`,
`median_chars_per_page`. The median rather than the mean: one 40-page appendix of
scanned tables must not be averaged away by a text-rich cover.

**Language plausibility** — the four that carry the legacy-font detector:
`stopword_rate` (share of tokens in a fixed 30-word English list),
`vowelless_token_ratio` (alphabetic tokens ≥3 chars with no vowel),
`intraword_symbol_ratio`, `intraword_case_switch_ratio`.

**Structure (spreadsheet)** — `sheet_count`, `row_count`, `non_empty_cells`,
`populated_ratio`.

Nothing on the brief's list that these do not cover was dropped silently; the
brief's `ascii_ratio` is subsumed by `latin_letter_ratio` + `digit_ratio` +
`punctuation_ratio`, which say *what kind* of ASCII rather than how much.

## 6. Status rules

Vocabulary, CHECK-constrained per repo convention:

| status | meaning |
|---|---|
| `extracted` | native text appears usable |
| `suspicious` | text exists; signals say it must not enter RAG unreviewed |
| `needs_ocr` | native extraction is insufficient; visual extraction required |
| `unsupported` | valid file, no native parser implemented |
| `failed` | parser error, missing blob, corrupt file |

There is no `pending`: with a per-blob table, absence *is* pending.

Evaluated in order; the first match wins, and every status carries a `reason`
code so the cohort is queryable rather than arguable.

1. **`failed`** — blob missing, hash mismatch against the filename, parser
   exception. Reason carries the exception *type*, never a stack trace or a path.
2. **`unsupported`** — family has no parser (`xls`, `doc`, archive, web, unknown).
3. **`needs_ocr`** — an image; or a PDF with `page_count > 0` and
   `text_page_coverage < 0.10`; or `median_chars_per_page < 50`. A valid scanned
   PDF is never `failed`.
4. **`suspicious`** — any of:
   * `legacy_font_suspected` (§7);
   * `partial_text_coverage` — PDF coverage in [0.10, 0.60);
   * `replacement_char_ratio` above 0.5%, or `control_char_ratio` above 1%;
   * `printable_ratio` below 0.95;
   * a spreadsheet whose `populated_ratio` is near zero.
5. **`extracted`** — everything else. A warning (not a status change) is attached
   for coverage in [0.60, 0.90).

Ties break toward `suspicious`, never toward `extracted`: a wrong document that
parses is the failure this whole phase exists to prevent.

**No numeric quality score.** A score invites threshold-tuning without labels.
Explicit rules plus the raw metrics; the metrics are persisted so a future rule
change can be re-evaluated against stored data instead of a re-parse.

## 7. Legacy-font / gibberish detection

Fires **only** when latin letters genuinely dominate, so a digit-heavy
statistical table is exempt by construction. All four conditions:

```
devanagari_ratio       < 0.01     (essentially no Unicode Devanagari)
latin_letter_ratio     > 0.35     (it IS latin text, not a numeric table)
token_count            > 50       (enough to measure)
stopword_rate          < 0.02     (real English prose runs 0.15-0.25)
```

…and then at least one corroborating shape signal:
`vowelless_token_ratio > 0.30`, `intraword_symbol_ratio > 0.15`, or
`intraword_case_switch_ratio > 0.10`.

Rationale: Preeti/Kantipur map Devanagari glyphs onto ASCII codepoints, so the
output is ASCII that is not English — no stopwords, consonant clusters with no
vowels, punctuation inside words, case switching mid-token (`k|fKt`, `ljQLo`,
`ffihW\ffifiHrz`). The stopword rate is the discriminating signal and the shape
signals are the corroboration; the measured circular scores ~0 stopwords with
high vowel-less and intra-word-symbol rates.

**Stated limits.** This cannot distinguish legacy-font mapping from a bad
embedded OCR layer (and does not try — same remedy). It will not fire on a short
document (<50 tokens); those get an `insufficient_text` warning instead. A
genuine English document of pure tabular codes could in principle trip it, which
is why the outcome is `suspicious` and not `needs_ocr`. Unicode Devanagari is
never flagged: `devanagari_ratio` fails the first condition immediately.

## 8. Metadata-assisted detection — and why it is NOT persisted

A source title carrying Devanagari while its file's text carries none is much
stronger evidence than either fact alone. But an extraction row is keyed on
`content_sha256`, and **a blob is shared across sources** — Phase 3 measured 42
duplicate attachment references, and Phase 5 found byte-identical duplicates
within the first 25 files. If the title fed the persisted verdict, a blob
referenced by one Devanagari-titled and one English-titled source would store a
different answer depending on which source the pass reached first. That is
non-deterministic persisted state, and it breaks the second-run-is-identical
invariant every prior phase holds.

So: **`nrb_extractions` is content-intrinsic. Every column is a function of the
bytes alone.** The title signal lives in `report.py`, computed at report time by
joining `nrb_extractions → nrb_files → nrb_source_files → nrb_sources` and
aggregating over *all* referencing sources rather than an arbitrary winner. It
raises reported confidence (`legacy_font_suspected` → *corroborated by Devanagari
title*), and it is never the sole determinant of anything. An English-titled
document is not held to a Nepali expectation.

## 9. Persistence

One table, revising the current single head `2b7f5c9d1a34`.

```
nrb_extractions
  id                    bigserial pk
  content_sha256        char(64)     -- the extraction input, not a file row
  extractor_version     varchar(32)  -- e.g. "pypdf-1"; the invalidation handle
  parser                varchar(32)  -- pypdf | python-docx | openpyxl | none | docling
  media_family          varchar(16)  -- sniff.FAMILIES
  status                varchar(16)  -- CHECK, §6
  reason                varchar(64)  -- the rule that fired
  warnings              jsonb        -- non-status findings
  page_count            int null
  pages_with_text       int null
  char_count            bigint
  devanagari_ratio      numeric null
  text_page_coverage    numeric null
  metrics               jsonb        -- everything in §5
  preview               text null     -- <= 300 chars, for manual sanity checks
  error                 text null     -- exception TYPE + short message, no traces
  extracted_at          timestamptz
  duration_ms           int
  UNIQUE (content_sha256, extractor_version)
  INDEX (status)
```

As built there is no separate `INDEX (content_sha256)`: that column leads the
unique index, which already serves lookup by blob and the join from `nrb_files`.

**No extracted text is persisted.** Only a ≤300-char preview for the manual
inspection sample. Phase 7 re-parses with Docling for chunking anyway, and a
cached screening artifact on disk is something a future phase will eventually
embed by accident. Not existing is cheaper than documenting that it must not be
used.

`extractor_version` is a plain string bumped by hand when the parser or the rules
change. The unique constraint on `(content_sha256, extractor_version)` makes
re-running a no-op and makes "which blobs are stale" a query
(`WHERE extractor_version <> current`). No versioning framework. That query is a
sequential scan — `extractor_version` is the *second* index column, so the unique
index does not serve it — which is accepted rather than indexed: it is an
occasional operator query over one row per blob, not something the pass runs.

`nrb_files` gains **nothing**. Its business is acquisition; extraction is per
blob, and per-row columns would extract shared bytes twice and store two answers.
Joining `nrb_files.content_sha256 → nrb_extractions` costs nothing and there is
already an index on it.

## 10. Sampling

`app/nrb/sampling.py`, pure: given catalog rows, return a stratified sample.

Strata: **year cohort × document type × resource type**, with owner tracked and
reported but not stratified on (33 owner codes × the rest would shatter into
single-digit cells). Year cohorts, chosen from the measured distribution:
`≤2018` (886 files), **`2019` (9,182)**, `2020–2022` (3,095), `2023–2026`
(5,109).

Selection within a stratum is `sha256(comparison_key)` order — deterministic,
reproducible across runs and machines, and uncorrelated with insertion order,
publication date or department, which id-order selection is not.

Allocation follows the direction given: **representation over equal counts.**
Four passes, in order:

1. **Floor**, round-robin — one slot at a time across every non-empty stratum, in
   `(-available, key)` order, until each holds `floor` or the budget runs out.
   Round-robin rather than "walk the sorted list handing out 5 each until the
   budget dies": that second form is what a `for … break` loop does, and when the
   budget cannot cover every stratum it silently gives everything to the
   lexicographically early ones. One slot at a time means an insufficient budget
   costs every stratum its depth, not some strata their existence.
2. **Proportional** to remaining headroom, so a 700-file stratum is not
   represented as thinly as a 3-file one. Rare types (`act` 90, `rule_bylaw` 84,
   `monetary_policy` 130 sources) get their floor and are **not** oversampled to
   force parity.
3. **Cohort cap** — no year cohort exceeds `max_cohort_share` (~30%), so 2019,
   which is half the corpus, cannot become half the sample.
4. **Redistribution** — every slot the cap removed is handed back, deterministic
   round-robin, to strata in *non-capped* cohorts that still have headroom,
   repeating until the requested size is reached or no eligible headroom remains.
   Without this the cap silently shrinks a 400-file request to whatever was left
   after trimming, and the sample would be both smaller and differently shaped
   than the one that was asked for. The cap is re-checked after every grant, so
   redistribution can never breach it.

If the request is genuinely infeasible — the corpus is smaller than `size`, or
every non-capped cohort is exhausted — the `Sample` reports a `shortfall` and the
reason, and the report prints it. A short sample that says it is short is fine; a
short sample that reads as complete is not.

Strata that cannot fill their floor are reported as weak rather than padded, and
the report names every stratum whose n < 10 so no conclusion is drawn from it
silently.

### 10.1 The benchmark manifest

The sample is selected **exactly once, from the full catalog**, and written to a
durable manifest — `docs/nrb/phase6a-manifest.json`, committed — holding each
selected file's exact `comparison_key` plus its `year`, `document_type`,
`resource_type`, `owner` and stratum, along with the sampler parameters and the
catalog counts it was drawn from.

Everything downstream then operates on **that exact cohort**: the Phase 5 fetch
gains an exact-key scope and downloads those files and no others, extraction runs
over the manifest, and calibration draws its ~40 files from the same set.

The alternative — approximate the sample with broad `--section`/`--year`/`--limit`
fetches and then re-sample whatever landed — was in the first draft of this plan
and is wrong. Phase 5 selects in id order within a scope, so "circulars from
2019, limit 60" returns the 60 with the lowest catalog ids, which is REST paging
order. Stratifying over *that* measures the id order, not the corpus, and the
stratification would be decorative. It is also not reproducible: a later fetch
changes what is on disk and therefore what gets re-sampled.

The exact-key scope is additive and changes no safety property: the host guard,
HTTPS requirement, pacing, byte caps, redirect refusal, soft-404 rule and
`fetch_status`-based selection all still apply, and a manifest key that names a
`blocked_host` file still cannot be selected. The manifest is bounded (≤5,000
keys) so it cannot become a way to smuggle a whole-corpus fetch past the
scope-is-required rule.

## 11. CLI

`scripts/nrb_extract.py`, following `nrb_fetch.py`'s conventions exactly.

* **Scope is required.** A bare invocation prints usage and exits 2 — the corpus
  is 18,263 files and CPU extraction is not free.
* Selectors: `--core`, `--section`, `--owner`, `--type`, `--year`, `--status`,
  `--limit`, `--manifest PATH` (the benchmark cohort, §10.1), `--all`.
  `scripts/nrb_sample.py` is the separate one-shot command that *writes* a
  manifest; sampling and extraction are deliberately not the same command, so the
  cohort cannot be silently re-drawn on a second run.
* `--dry-run` reports what would be extracted, parsing nothing.
* `--force` re-extracts blobs already recorded at the current version.
* `--calibrate N` runs the bounded Docling comparison.
* `--json` for a diffable summary; `-v` for progress.
* Advisory lock `NRB_XTRC` via `app/nrb/locks.py`, dedicated connection, same
  rule as `NRB_SYNC`/`NRB_FTCH`.
* Results commit in batches, so an interrupted pass keeps its progress —
  **resumable, like the fetch, not idempotent**; a second pass over an exhausted
  scope selects zero.

Two small changes to Phase 5, both additive and neither touching a safety
mechanism (host guard, pacing, byte cap, redirect refusal, soft-404 rule, lock,
resumability all unchanged):

* `--year` / a `years` parameter on `catalog.select_fetch_targets`. Fetch
  selection is id-order and cannot deliberately reach the 2019 cohort, which is
  9,182 of 18,263 files and the one the report must isolate.
* `--manifest PATH` / a `keys` parameter taking exact `comparison_key` values, so
  the benchmark cohort of §10.1 is downloaded exactly rather than approximated.

## 12. Failure isolation and safety

One bad file never aborts a pass: each file is extracted inside a try, a failure
is recorded as `failed` with the exception *type* and a short message, and the
pass continues. Errors never carry a stack trace, an absolute path or a user id
into the database — the same rule `app/files/documents.py` already follows.

Phase 6A makes **no network request at all.** It reads local blobs by
`storage_key` through `filestore.resolve_path` (which refuses traversal), and
verifies each blob against the sha256 in its own filename before parsing. Every
downloaded document is untrusted input: no formula evaluation, no macros, no
embedded script execution, no shelling out. Memory is bounded — per-page text for
PDFs, streamed rows for spreadsheets, and a size cap above which a file is
recorded rather than loaded.

## 13. Tests

`tests/test_nrb_extraction.py` (pure — no DB, no network) and
`tests/test_nrb_extract_integration.py` (real Postgres, rolled back inside a
savepoint exactly as `test_nrb_sync_integration.py` does, because the catalog is
global with no department to scope a fixture to).

Coverage classes:

* **Metrics** — empty, English, Unicode Devanagari, mixed, control-heavy,
  replacement-char-heavy, symbol-heavy; each ratio computed against a hand-checked
  expectation.
* **Classification** — good English PDF → `extracted`; Unicode Nepali PDF →
  `extracted` (the false-positive test that matters most); empty/scanned PDF →
  `needs_ocr`; legacy-style output → `suspicious/legacy_font_suspected`; image →
  `needs_ocr`; `.xls` → `unsupported`; parser exception → `failed`.
* **Metadata assistance** — Devanagari title + healthy Devanagari text → no
  suspicion; Devanagari title + zero-Devanagari symbol-heavy text → corroborated;
  English title never forces a Nepali expectation; **and the persisted row is
  byte-identical regardless of which source is processed first** (§8's invariant).
* **File handling** — reads the content-addressed blob, verifies sha, missing
  blob reads cleanly, corrupt file reads cleanly, a batch continues past one
  failure.
* **Persistence** — status and metrics persisted; a repeat pass creates no
  duplicate row; an `extractor_version` bump marks the prior result stale;
  timestamps behave.
* **Sampling** — deterministic across runs and across input order, bounded,
  multiple years, multiple document types, 2019 present, weak strata reported not
  padded; **a feasible request of 400 returns exactly 400**, cap-trimmed slots are
  redistributed, the cohort cap still holds after redistribution, an infeasible
  request reports its shortfall, and a floor larger than the budget does not
  privilege lexicographically early strata.
* **Manifest** — written once, round-trips, carries the sampler parameters,
  bounded in size; the fetch and the extract select exactly its keys and nothing
  else; a manifest key naming a `blocked_host` file is still unselectable.
* **CLI** — bare invocation refuses; scope accepted; limit honoured; summary
  deterministic on fixture data.

Fixtures are generated in-process (fpdf2 and openpyxl are already dependencies)
rather than committed as binaries.

Existing RAG parsing tests are run unchanged as the regression gate, including
the subprocess check that Docling is not imported at module scope.

## 14. Live profile

1. **Draw the manifest once** (§10, §10.1) over the full catalog, ~400 files, and
   commit it. Every later step names this file; none of them re-samples.
2. Fetch exactly those files through Phase 5's paced/bounded/resumable path with
   the manifest scope.
3. Extract, classify, record — over the manifest cohort.
4. Calibrate ~40 PDFs **drawn from the same manifest** against Docling, comparing
   extraction to extraction (§3.1).
5. Report: status counts overall and by year cohort (**2019 always broken out,
   never inside a corpus average**), by document type, by file format; script
   profile; medians; throughput; and a bounded manual-inspection sample of ~10
   per status with title, year, type, format, key metrics, reason and a short
   preview.

Strata that came back weak are named with their n. Conclusions are not drawn from
cells below 10.

## 15. Explicitly out of scope

OCR of any kind (Tesseract, Paddle, EasyOCR, Docling OCR, vision, cloud);
legacy-font → Unicode conversion; chunking; embeddings; pgvector writes;
`documents`/`document_chunks`/`ingest_jobs`; `search_nrb_documents`; cron;
amendment resolution; reclassifying the 2019 `upload-files` cohort; source
citations; any change to `feat/rag-source-citations`, its migration, or the
`local_ai_gateway` database; `alembic stamp`; whole-corpus processing.

Phase 6B's recommendation will be made from §14's measurements, and only from
those.

## 16. Evaluation & Improvement

1. **Success metric** — the share of the fetched core corpus that native
   extraction turns into **trustworthy** text: `status='extracted'` with no
   suspicion reason, over files attempted. The complement is the number that
   actually matters to Phase 6B — files whose text exists and is wrong — because
   those are the ones that would silently poison RAG. Current evidence (49
   circulars) puts the trustworthy share at approximately **zero**, which is the
   finding, not a bug.
2. **Eval** — the pure test suite in §13 is the labelled set: hand-authored
   English, Unicode Nepali, legacy-style, scanned, mixed and corrupt inputs with
   an expected status each, scored as exact status agreement. Target 100% on the
   labelled set, plus the Docling calibration's agreement rate on ~40 real files
   as the out-of-sample check. Both numbers get reported; neither is claimed to be
   statistically calibrated.
3. **Feedback capture** — `nrb_extractions` is the log: every row keeps its
   `reason`, its full `metrics` and a `preview`, so a disputed verdict is
   re-checkable without re-parsing, and a rule change can be re-scored against
   stored metrics. The report's manual-inspection sample is the standing
   correction channel; false positives and negatives found there are recorded
   honestly rather than tuned away.
4. **Review loop** — re-run the profile when `extractor_version` changes, when a
   parser dependency is upgraded, or before Phase 6B commits to an OCR strategy.
   Pass condition: the labelled set still at 100%, no Unicode-Devanagari document
   classified `suspicious`, and the Docling agreement rate not falling. A drop in
   agreement means the two engines have diverged and the screen needs re-checking
   before its numbers are trusted again.

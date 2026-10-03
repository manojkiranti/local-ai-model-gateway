# native-3: repairing NRB text layers through their own fonts

**Date:** 2026-10-02
**Status:** design approved section by section in conversation (2026-10-01/02);
this written spec awaits review. No code exists yet. Phase 1 (§3–§6, §8, §9)
is one implementation plan; Phase 2 (§7) gets its own plan after Phase 1 ships.
**Scope:** `app/nrb/` (four new modules, small edits to `recovery`,
`recovery_cache`, `rag`, `extraction`), `app/rag/sources.py` and the two
retrieval tools, `app/config.py`, `requirements-worker.txt`, new scripts, a new
frozen cohort, a reviewer sheet, the build-database repair, and a data-only
update for the bank. **No migration.**
**Background:** `docs/prod-incident-2026-09-20.md` §9.8 and §9.10 (part B);
`docs/nrb-integration.md` §17.6.

---

## 0. Decisions taken

| # | decision | by |
|---|---|---|
| D1 | The repair is versioned as the **native route's engine** (option E), not as a new routing `base_version` (option B). §2 | user, on recommendation |
| D2 | Impossible-sequence density is the **detector only**. The **round-trip shaping check is the acceptance gate.** §4 | user |
| D3 | A page that is detected but not repaired **keeps today's native text, marked non-authoritative**. This is a **deliberate exception** to recovery's "withhold what did not convert" rule. §5 | user |
| D4 | Word 2016–365 pages (GSUB stripped) are **Phase 2**: a verified donor GSUB, built only after Word 2007–2013 works. §7 | user |
| D5 | uharfbuzz's licence was checked before adding it: **Apache-2.0**, bundling HarfBuzz under **"Old MIT"**. Both permissive. §3.4 | checked 2026-10-01 |
| D6 | Reader validation is a new sheet in `/home/manoj/nrb-cohort-review.xlsx`, beside the 50 retrieval questions, so one review session covers both. §6.4 | user |
| D7 | Deliveries to the bank are **data-only updates**, never full restores: one atomic transaction, copy-paste steps, their worker and nrb-runner stopped throughout. §8.3 | user |
| D8 | Success adds a re-run of the retrieval smoke questions on the repaired Acts. §8.2 | user |

---

## 1. The problem

**About 6% of the served NRB corpus is wrong in a way no current check sees.**
§9.8 measured 24 of the 73 `native` documents flagging on ≥20% of their chunks
(1,635 chunks of 26,058). They include the Consumer Protection, Negotiable
Instruments, Payment & Settlement and Foreign Investment Acts, and the AML
rules:

```
extracted : कम्पनी रजिष्ट्र ारको कार्ाालर् जिपुरेश्वर … जनदेशन , २०७९
should be : कम्पनी रजिष्ट्रारको कार्यालय … निर्देशन, २०७९
```

What was already established:

- **The PDFs themselves are wrong, not our extractor.** Poppler returns
  byte-for-byte the same errors as pypdf, and there is no `/ActualText` to fall
  back on.
- **The cause is systematic.** Microsoft Word writes wrong ToUnicode labels for
  Kalimati: gid 143 त is labelled `ि`, and gid 91 र् is labelled `ध`. The same
  gids are wrong in every Word-2013 file. The embedded font keeps `cmap`+`GSUB`,
  and 83/83 and 80/80 glyphs were recoverable on two Word-2013 pages (probe:
  `/home/manoj/ab-rule/font_check.py`).
- **OCR is not a substitute.** It fixes the spelling and loses 28–53% of the
  content of an Act.
- **native-2 cannot see it.** Its rules ask "is this Devanagari", not "is it
  spelled correctly" (§17.6).

### 1.1 Measured while designing (throwaway, read-only, NOT evidence)

These ran against `local_ai_gateway_build` from the session scratchpad. They
shaped the design, and that makes the production scope **development
evidence** (§6.1).

**Census of native documents by producer and embedded font.**
- **Word 2007/2010/2013, about 20 documents.** Kalimati (sometimes Mangal) is
  embedded as Type0/CIDFontType2 with Identity CIDToGIDMap, `cmap`+`GSUB`+`glyf`,
  and the **full glyph count (696 or 699)**, so Word keeps the gid numbering.
- **Word 2016/2019/LTSC/365, 5 documents.** Same Kalimati glyph counts, but
  `cmap`+`glyf` with **no GSUB**. One uses Kokila-Bold (725 glyphs).
- One Online2PDF file, and one Nirmala UI file (`075bf12eb087`, §17.6), each a
  different shape.

**Mechanism probe** (Consumer Protection Act, p.14).
- Method: install a corrected ToUnicode, derived from the font's own
  cmap+GSUB, with placeholder markers for the pre-base `ि` and the reph. Then
  extract with pypdf and reorder visual → logical.
- Result: illegal clusters fell from **100 to 4**, and most words came back
  right: `वस्िुको→वस्तुको`, `गदाध→गर्दा`, `बमोम्िम→बमोजिम`, `यकमसमको→किसिमको`.
- **But a misclassified rakar/reph produced `पर्कृति`, `अकोर्`, `पनेर्छ`: wrong,
  yet with zero illegal clusters.** That single observation is why density
  cannot be the acceptance gate (D2).

**Density separation** (illegal clusters per 1k Devanagari characters, per
document).
- Affected documents score 5.05–185. The worst clean document with real
  Devanagari scores 0.09.
- **About 30 documents exceed 5/1k**, so "24" is an artefact of the criterion
  (the Unified Directives 2075–2077 join the list). The real count is whatever
  the frozen detector says (§8.2b).
- Almost every clean native document is **English** (0 Devanagari characters),
  so the development set has **almost no clean Unicode-Devanagari controls**.
  The new cohort must supply them.

---

## 2. Versioning: the repair is the native route's ENGINE

The recovery cache has two version domains (`recovery_cache.py`). The routing
identity is `base_version`; a change there turns the whole document cold. The
per-unit `engine_version` is the identity of whatever produced the unit's text,
and a change there re-runs only that route's units.

| | **E — engine (chosen)** | B — new `base_version` |
|---|---|---|
| "native-3" means | A new **parser** identity: text is read through the font program when its ToUnicode is proven wrong. Classifier and routing stay native-2. | A new classifier: a `tounicode_suspected` reason, a new `font_rebuild` route, recovery-2 |
| re-runs | **native units only** (a cheap pypdf re-read); OCR and converter units reused | every document cold: all OCR pages and conversions |
| migration | **none** | two CHECK edits, so a new head |
| shipping | **data-only release at `e1a4c6f9b2d7`**, independent of the bank's blocked code deploy | waits for their code deploy |
| production cache | stays warm | needs a warm-up pass; the dump carries two cache generations |
| trap | — | warming re-runs nondeterministic OCR, so cache text **disagrees with indexed chunks** on pages never re-ingested |
| provenance | route stays `native`; the repair lives in `detail` and chunk `metadata` | a distinct route label |
| precedent | the lexicon: a guard that changes what a route produces belongs in the engine version | native-2, a classifier change |

**The condition that makes E honest, not just convenient.** Classification and
`route_page` run on the **unrepaired** text, so no plan and no route changes,
and `base_version` stays `native-2|recovery-1|prov-1|gate=0.8|unjudged=0.8`
truthfully. Today a single `extractor_version` argument feeds both the base and
the native engine (`recovery_cache.base_version` and `engine_versions`). It is
split into a **classifier version** (base) and a **native engine identity**.
Tests enforce the condition (§9.2):
- native-3 rows equal native-2 rows on status, reason and every existing metric;
- `plan_document` and `route_page` are byte-unchanged.

**Two accepted costs.**
1. `route: native` stops meaning "the extracted text, unchanged". It now means
   "the PDF's own text layer, read through its font program when its ToUnicode
   is proven wrong". `recovery.py`'s docstring and `docs/nrb-integration.md`
   say so.
2. Until the bank deploys this code, re-ingesting an affected document **on
   their side** reverts it to garbled text: their engine sees the unit as stale
   and refreshes it with passthrough. Re-ingest only happens on an explicit
   trigger or a republished file, and `UPDATE.md` says so (§8.3).

**Engine identity.**

| state | native engine version | effect |
|---|---|---|
| `NRB_NATIVE_REPAIR=false` (default until §6.4's gate passes) | `passthrough/native-2`, **exactly today's string** | no unit goes stale and no text changes, wherever the code is deployed |
| flag on, dependencies present | `native-3/repair-1/D=<D>/N=<N>/lang=ne/hb-<HarfBuzz version>`, with every term read live | every native unit goes stale and refreshes cheaply on its next ingest |
| flag on, fontTools or uharfbuzz missing | `native-3/repair-unavailable` | detected pages are served unrepaired and caveated; installing the dependency later invalidates exactly those units |

Non-PDF native units go stale too when the flag flips. Their cached recovery
is all-or-nothing, so they re-run cold. A cold native `.docx` costs nothing
much, and the repair never applies to them.

---

## 3. Components and data flow

### 3.1 New modules

Each is the **only** file that knows its library exists, imported lazily, so
the API image never loads fontTools or uharfbuzz. A subprocess test asserts
that importing `app.main` loads neither, in the shape of
`tests/test_image_ocr_import_boundary.py`.

| module | job | depends on |
|---|---|---|
| `app/nrb/glyphtable.py` | One embedded TrueType program → `GlyphTable`: gid → (text, class), with class ∈ `plain`/`prebase`/`reph`. See §3.3. Also the font-identity fingerprint (family without subset prefix, glyph count, sha256 of the full advance-width vector) that Phase 2 needs. | fontTools |
| `app/nrb/reorder.py` | Visual token stream → logical Unicode. A marker with no legal attachment is an **orphan**: counted and never guessed. Output NFC. | nothing (pure) |
| `app/nrb/shaping.py` | `available()`, `version()`, `shape(font_program, text, *, script="deva", lang="ne") → tuple[gid, …]` | uharfbuzz |
| `app/nrb/fontrepair.py` | One PDF page → `RepairOutcome(status, text, detail)` where status ∈ `repaired` / `unrepaired:<why>` / `not_applicable`. Finds suspect fonts (§4.2), installs a corrected ToUnicode with markers on an **in-memory** reader, extracts with **the same `extract_text` call** `documents.read_pdf_pages` makes, reorders, runs the gate (§4.3). Glyph tables are memoized by font-program digest within a process. | the three above, pypdf |

### 3.2 Edits to existing files

- **`recovery.py`** gains a public `native_unit(number, text, *, path,
  document_suspect, repair)`, the native counterpart of `convert_unit`/`ocr_unit`.
  It is reached from `_recover_pdf`'s native branch, the PDF branch of
  `PLAN_NATIVE`, and `recovery_cache._refresh_unit`, so recovery stays the
  semantic owner. **`plan_document` and `route_page` are not edited.**
  `document_suspect` comes from one pure function over the texts of the
  document's **native-route** pages. Cold runs and refreshes call the same
  function, and a test asserts they agree. In `PLAN_PAGES`, all pages are routed
  first and native units are executed second, because which pages are native is
  known only after routing.
- **`recovery_cache.py`**: `base_version(classifier="native-2")` and
  `engine_versions(…, native=<native engine identity>)`. `CacheReport` gains
  `repaired_units` and `unrepaired_units`, and `stats()` breaks native units
  down by `detail->>'repair'` (the route split alone can no longer show it).
- **`rag.py`**: `nrb_dependencies()` returns the repair engine as a fourth
  dependency (`None` when the flag is off, or when the libraries are missing),
  and `resolve_dependencies` carries it. `_chunk_meta` writes `text_repair`
  (`font_tables` | `unrepaired`), `repair_engine` and `authoritative: false` for
  every page of a detected document.
- **`extraction.py`**: `SUPPORTED_EXTRACTOR_VERSIONS` gains `native-3`. It is
  native-2's classification **on the unrepaired text**, plus repair metrics:
  `detected`, `density`, `pages_attempted`, `pages_repaired`, a histogram of
  unrepaired reasons, run agreement, and illegal clusters before/after. This is
  **evidence** for `nrb_extract.py --extractor-version native-3`. It never feeds
  routing.
- **`app/rag/worker.py`**: the worker log line gains `repaired N, unrepaired M`
  beside `converter …, ocr …`.
- **`app/config.py`**: `NRB_NATIVE_REPAIR: bool = False`.
- **`requirements-worker.txt`** lists **`fontTools`** (today it is there only
  through docling) and **`uharfbuzz`** explicitly. `requirements.txt` gets
  neither, and `Dockerfile` installs only `requirements.txt`.
- **`app/rag/sources.py`**: the shared predicate `is_machine_recovered(route,
  authoritative)`. See §5.2.

### 3.3 How a `GlyphTable` is built

1. **cmap.** Each codepoint → glyph gives gid → that codepoint. When a gid has
   several codepoints, the Devanagari-block one wins, deterministically. All of
   them are kept for §4.2's contradiction test.
2. **GSUB closure, iterated to a fixed point.**
   - Ligatures (type 4) concatenate their components' texts.
   - Single substitutions (type 1) inherit the source's text **and class**.
   - Multiple substitutions (type 2) are recorded, not inverted.
   - Extension lookups (type 7) are unwrapped.
   - Contextual lookups (5/6/8) contribute through the lookups they reference.
3. **Class comes from the GSUB feature that formed the glyph**, read from
   `FeatureList`:
   - `rphf` → `reph`;
   - the cmap glyph of U+093F, and everything substituted from it → `prebase`;
   - every other feature (`half`, `blwf`, `rkrf`, `pstf`, `akhn`, `pres`, `abvs`,
     `blws`, `psts`, `haln`, `cjct`, `locl`, …) → `plain`, with components
     already in logical order.
4. **Ambiguity is refused.** A gid reached by two derivations that give
   different text or class is **ambiguous**. So is a lookup that `rphf` shares
   with another feature. An ambiguous gid fails §4.3's coverage check. This is
   the probe's rakar bug turned into a rule.

### 3.4 Licences

- **fontTools** is MIT.
- **uharfbuzz 0.56.2** is Apache-2.0. It ships a `cp310-abi3` manylinux wheel
  (2.0 MB) and requires Python ≥ 3.10. It bundles **HarfBuzz**, under the "Old
  MIT" licence.
- Both are permissive. **There is no distribution gate** of the npttf2utf
  (GPL-3) kind.
- Obligations: keep uharfbuzz's Apache licence and NOTICE in the worker image,
  and read the wheel's bundled per-directory COPYING files when the dependency
  is added. Recorded beside §12's npttf2utf licence note.
- No font bytes or font tables enter the repository in either phase (§7).

### 3.5 Data flow

```
worker → chunks_for_blob → recovery_cache.resolve
  cold:    extract (native-2 verdict on UNREPAIRED text) → plan_document → route_page   (unchanged)
           document_suspect = detector(native-route page texts)
           native page → recovery.native_unit(page, path, document_suspect, repair)
             ├ flag off / not suspect / no suspect font  → passthrough, byte-identical to today
             └ suspect → fontrepair.repair_page
                   ├ gate passes → repaired text            detail.repair = font_tables
                   └ gate fails  → today's native text kept  detail.repair = unrepaired:<why>   (§5)
  partial: only native units are stale → the same native_unit; OCR/converter units reused (0 runs of either)
```

---

## 4. Detection and the round-trip gate

**The detector decides what to attempt. The gate decides what to serve.**
Neither can stand in for the other (§1.1).

### 4.1 Document detector: density

- **Signal.** `devanagari.illegal_cluster_count` over the unrepaired text of
  the document's native-route pages, divided by their Devanagari characters.
  These are the existing rules, with nothing added.
- **Fires when** density ≥ **D = 1.0 per 1,000** Devanagari characters and the
  document has ≥ **N = 500** Devanagari characters.
- **Why these values.** On the development set, positives score ≥ 5.05/1k and
  the worst clean document 0.09/1k. D sits 5× below the lowest positive and 11×
  above the cleanest negative. N keeps a 200-character page from flagging on
  two stray marks.
- **Frozen before the cohort is drawn** (§6.2), as module constants read live
  into the engine version.
- **Per document, not per page.** The bug belongs to the font. A page with few
  illegal clusters can still be full of legal-but-wrong words (`जनदेशन`).

### 4.2 Font narrowing: deterministic, no threshold

Inside a suspect document, a font is **SUSPECT** when all of these hold:
- it is Type0 over CIDFontType2 with an embedded `FontFile2`;
- it is encoded Identity-H, with an Identity or stream CIDToGIDMap (any other
  encoding is `not_applicable` in Phase 1);
- it carries a ToUnicode;
- that ToUnicode **contradicts the font's own cmap** on at least one glyph the
  page draws. "Contradicts" means the label is not among the codepoints the
  cmap maps to that gid. Ligature glyphs, which have no cmap entry, cannot
  contradict.

A page is attempted only if it draws text in a suspect font. Only suspect fonts
get the corrected map; Calibri, Times and a healthy Mangal on the same page pass
through untouched. This is the provenance rule again: the font **narrows**, it
never widens.

### 4.3 The gate: all four, per page

1. **Coverage.** Every glyph a suspect font draws on the page resolves in its
   `GlyphTable`: no unknown gid, no ambiguous gid.
2. **No orphans.** Reordering attaches every `prebase` and `reph` marker.
3. **Round trip.** A **run** is the consecutive text-showing operators (`Tj`,
   `TJ`, `'`, `"`) in one suspect font within one `BT…ET`. They are grouped so a
   syllable split across operators is not a false failure. For **every** run:
   - decode the gids to tokens, reorder, then shape with **that run's embedded
     font program** (HarfBuzz, script `deva`, language `ne`);
   - the gid sequence must **equal** the gids the run draws;
   - required for **100% of runs**. The language is part of the engine
     identity, because Nepali `locl` changes glyph choice.

   A development-set mismatch is a finding to fix in `glyphtable`/`reorder`,
   with a new repair version. It is **never** a percentage quietly accepted.
4. **Layout agreement.** The served text is pypdf's extraction through the
   corrected map, then reordered. With all whitespace removed, it must equal
   the round-tripped runs together with the other fonts' text, in content
   order, also with whitespace removed. This proves pypdf's layout only *added
   whitespace*: it never split a syllable or reordered runs. The intra-word
   spaces pypdf already inserts today (`आफू ले`) are unchanged and out of
   scope.

Recorded, **not gated**: illegal clusters before and after. Once the round trip
passes, any illegal sequence that remains is what the page itself draws, and
those pages are listed for the reader (§6.4).

**Missing dependencies fail closed.** With uharfbuzz or fontTools absent, a
detected page is `unrepaired:shaper_unavailable`. A repair is never applied
without its gate, by the same rule as "a converter without its guards is worse
than no converter" (§12.2).

---

## 5. The unrepaired-page exception

### 5.1 What happens, and why it is an exception

A page in a detected document that is not served repaired keeps **today's
native text**. That covers four cases:
- a gate failure;
- no suspect font on the page;
- `not_applicable` (an unsupported encoding);
- a missing shaper.

Its unit records `detail.repair = "unrepaired:<why>"`, and its chunks carry
`text_repair: "unrepaired"` and `authoritative: false`. Repaired pages carry
`text_repair: "font_tables"` and also `authoritative: false` until §9.4's
decision.

**This is a deliberate exception to recovery's rule 5, "a conversion that does
not succeed withholds its input"**, and `recovery.py` states it as a new rule 6:
- Rule 5 withholds because the input of a failed *conversion* is glyph-mapped
  ASCII. Nobody can read it, it is noise to search, and a citation would present
  it as a circular.
- The input here is **Unicode Devanagari that is mostly right**. The Consumer
  Protection Act still ranked 1 in §9.8's smoke test despite its garbling.
- Withholding would remove whole Acts (~6% of the corpus) from search, and the
  only other source of their text, OCR, loses 28–53% of it.
- So a failed repair leaves the page exactly as it is today, plus a caveat it
  lacks today. It is never worse, and it is more honest.

**The exception is narrow:**
- it covers native pages of detected documents only;
- `_withhold`, the legacy-conversion rules and the OCR rules are untouched;
- a test pins both halves: *a detected-but-unrepaired page keeps its native
  text AND is non-authoritative*, and *a failed legacy conversion still
  withholds*.

### 5.2 The caveat gap this exposes, fixed in the same change

Three readers decide whether to show the VERIFY caveat, and they disagree:

| reader | predicate today | sees `native` + `authoritative:false`? |
|---|---|---|
| `search_department_docs` (model context) | route ∈ RECOVERED or `authoritative is False` | yes |
| `app/rag/sources.py` (citation `verify_note`) | same | yes |
| `read_department_doc` (whole-document read) | route ∈ RECOVERED **only** | **no** |

Unfixed, `read_department_doc` would hand the model a garbled Act with no
caution while the citation badge says VERIFY. That is the contradiction the
one-constant rule exists to prevent. The fix: **one shared predicate**,
`sources.is_machine_recovered(route, authoritative)`, used by all three. The
existing `test_the_caveat_is_one_constant_with_two_readers` is extended to
cover it.

**The wording stays the single `VERIFY_NOTE`** ("machine-recovered — VERIFY
figures, dates and names against the source"). It is accurate for repaired
pages and a little loose for unrepaired ones, whose text is the PDF's own. A
second constant would make every reader choose between two notes, and the
frontend may key only on the flag. The looser wording is the accepted cost.

---

## 6. Cohort and the review workbook

### 6.1 Development evidence: spent by construction

The **production scope's native documents** (`docs/nrb/prod-corpus-scope.json`)
are development evidence. That is where the bug was found, where the probe ran,
and where D and N were read. They tune nothing further and prove nothing.

### 6.2 The new cohort: `docs/nrb/native3-cohort.json`

Frozen and **committed before any network access**.

- **Withheld:** all four used sets, with the exclusion semantics of
  `sampling.stratified_sample(exclude_keys=…)`: candidates are dropped
  **before** stratification, and `exclude_keys_sha256` is recorded:
  - `phase6a-manifest.json`
  - `phase6b-routing-holdout.json` (spent: never tune against it)
  - `phase7-validation-cohort.json`
  - `prod-corpus-scope.json`
- **Frame:** catalog files whose REST claim is PDF. That is the only
  population the repair can touch.
- **Two strata, defined on catalog metadata alone**, so nothing native-3
  computes can shape them:
  - **Enriched, 250:** primary section ∈ {`circular`, `guideline_manual`},
    published 2015 or later. That is where the Word exports appeared.
    **Amendment (2026-10-03, user decision, before any network access):** the
    original definition was primary section ∈ {`act`, `rule_bylaw`, `directive`},
    published 2015 or later. All 251 of its PDFs are in the withheld
    `prod-corpus-scope.json`, so the stratum and its top-up drew 0 in a dry draw
    that was never frozen. The unspent counts for the same window are circular
    975 and guideline_manual 102 (published 2015 or later). The redefinition uses
    catalog metadata only.
  - **Random, 250:** the rest of the PDF frame, untyped documents included.
    **Population claims (prevalence, false-positive rate) come only from this
    stratum.**
- **Ordering:** `sampling.rank_for` (namespace + seed + `comparison_key`)
  within each stratum. The algorithm is versioned as its own string
  (`nrb-native3-v1`), because a two-stratum design is not `nrb-stratified-v1`.
  It is written through `manifest.py`, so `nrb_fetch.py --manifest` can fetch
  exactly these keys.
- **One pre-registered top-up.** If the enriched stratum yields fewer than **20**
  detected documents, the next 250 ranks **of that same stratum** are added. A
  shortfall after that is reported, never padded.
- **Database:** `local_ai_gateway_p4`. The cohort and evidence scripts join
  `dbguard`'s EVIDENCE list, and `test_nrb_dbguard.py` covers them.

### 6.3 Measured automatically over every cohort document

- **Non-regression.** Every undetected document's native-3 text is
  **byte-identical** to native-2 on every page. Required: **100%**.
- **Classifier invariance.** native-3 rows equal native-2 rows on
  status/reason/metrics for every document (§2's condition).
- **Detector cross-tab.** The density detector against font contradiction
  (§4.2), as an independent second signal. A contradiction below D is a
  **false-negative candidate**; density without any contradiction is a
  **false-positive candidate**. Both lists go to the reader, the way §15 split
  them.
- **Gate.** Pass rate and run-level round-trip agreement, by font × Word version,
  with illegal clusters before/after on repaired pages.
- **Output:** `docs/nrb/native3-cohort-evidence.{json,txt}`.

### 6.4 The reader: sheet `native3-repair` in `/home/manoj/nrb-cohort-review.xlsx`

- **Cells:** font (Kalimati-696, Kalimati-699, Mangal, Nirmala UI) × Word version
  (2007 / 2010 / 2013) × risky cluster (reph, rakar/below-base र, pre-base `ि`,
  conjunct/half form, plus plain text).
- **Excerpts:**
  - **2 per populated cell**, each 5–8 lines chosen *because* they contain that
    cluster;
  - plus **6 clean native controls**, to confirm the reader calls clean text
    clean;
  - plus **6 unrepaired pages**, to show what is left caveated.

  That is about 50 excerpts, roughly an hour.
- **Two kinds of row.** Cohort excerpts are **evidence**. Excerpts from the
  production Acts are **shipping QA**, and each row says which. Gate numbers
  come from evidence rows only.
- **Columns:**
  - id, source (cohort | production), document, page, font, Word version,
    cluster class;
  - a cropped page image;
  - native text, repaired text with the target clusters in **bold**;
  - verdict (correct / N wrong words / wrong), the wrong words;
  - **"native also wrong?"** (§17.6's "confirm the extent");
  - notes.
- **Workbook safety.**
  - The builder appends a sheet and edits nothing else. It refuses if
    `native3-repair` already exists, and keeps a timestamped backup first.
  - openpyxl drops embedded images it did not create on a load→save, so **no
    script re-saves the workbook after the sheet is added**. The importer opens
    it read-only.
  - Verdicts are imported to `docs/nrb/native3-review.json`.
- **Pass criterion, pre-registered.** Repair-caused word error rate **≤ 0.5%**
  over the evidence excerpts, and **no cluster class wrong twice**. A repeat is a
  systematic bug, not noise.
- **If the reader finds a bug:**
  - the fix gets a new repair version;
  - the excerpts reviewed for it become development evidence;
  - the fix is validated on **fresh excerpts from cohort documents the reader
    has not yet seen**. The cohort is far larger than what is reviewed for
    exactly this reason.

---

## 7. Phase 2: donor GSUB for Word 2016–365

**Phase 1 already handles these pages honestly.** A GSUB-less Kalimati cannot
resolve its conjunct and reph glyphs, so the page is `unrepaired:coverage`, keeps
today's text, and gains the caveat. **Phase 2 starts only after Phase 1 passes
§6.4 and ships.**

- **Feasibility first, before building.** For each GSUB-less embed (the 5
  production documents, and the cohort's Word 2016+ documents): does a verified
  donor exist, and do coverage and round-trip agreement reach 100% after the
  transplant? If not, Phase 2 stops at that finding.
- **Donor identity: all of these must hold, or there is no donor.**
  - same family name without the subset prefix, and the same glyph count;
  - an **identical advance-width vector over every glyph** (Word's subsets keep
    `hmtx` whole);
  - identical decomposed outlines on every gid present in both fonts;
  - an identical cmap wherever the two overlap.
- **The registry: `docs/nrb/native3-glyph-donors.json`**, committed and
  fingerprinted.
  - Per font identity it names a **donor blob by `content_sha256`** and font
    object: e.g. `2b11bf653aa2` for Kalimati-696, and `5acd61c8e9a8` or
    `99f130e6f812` for Kalimati-699.
  - All of these are in the production scope, so the production filestore
    already holds them.
  - **No font bytes or tables are committed.** The donor is a document NRB
    itself publishes.
- **The transplant.** In memory, the donor's GSUB+GDEF go onto the recipient
  font (gid numbering is proven identical), and the page goes through
  **exactly §4's path and gate**. The round trip shapes with the transplanted
  font and must reproduce the page's own drawn gids. A Kokila-Bold-725 page stays
  unrepaired unless a GSUB-bearing donor turns up.
- **The one cost: a cross-document dependency**, which breaks "a chunk is a
  function of its own bytes plus versioned engines". It is bounded three ways:
  - the registry's fingerprint joins the native engine version (`repair-2`),
    so again only native units re-run;
  - the identity check is a function of both blobs' bytes;
  - a donor missing from the filestore yields `unrepaired:donor_unavailable`.
- **Rejected:** a partial rebuild from cmap alone, keeping the PDF's labels
  for the rest. The glyphs the cmap cannot explain are the conjuncts and the
  reph: exactly the ones whose labels are wrong. And with no GSUB there is
  nothing to round-trip against.
- **Evaluation** uses the Word 2016+ documents of the **same frozen cohort**,
  which Phase 1 never repaired and the reader never saw repaired. New cells:
  Kalimati-696/699 × {2016, 2019, LTSC, 365} × cluster classes.

---

## 8. Rollout

### 8.1 Principles

- The code ships **behaviour-neutral**: `NRB_NATIVE_REPAIR=false` keeps the
  engine string `passthrough/native-2` (§2). The default flips in a separate
  commit, after §6.4's gate passes.
- **The full test suite passes before every commit.**
- Every long job (fetch, evidence pass, worker, release build, dumps, the
  rehearsal) runs as a **`systemd-run --user` unit**, never from a Claude shell.

### 8.2 Order of operations

1. **Cohort, on `p4`.** Freeze and commit the manifest, then fetch
   (`nrb_fetch.py --manifest`), run the evidence pass, build the workbook sheet,
   collect the reader's verdicts, and record the gate decision in a new
   `docs/nrb-integration.md` **§31**.
2. **Repair, in `local_ai_gateway_build`:**
   - **a. Fingerprint before.** For all 338 documents: chunk count, a sha256
     over ordered chunk content, and the route split.
   - **b. Detect.** An OPERATIONAL script (admitted to `build` by `dbguard`)
     lists the documents the detector fires on. Its dry run is the real number.
   - **c. Re-ingest those document ids only**, through the existing
     `app/rag/reingest.py`, with the worker running with the flag on.
   - **d. Verify by the repair split, not by job success** (the §18 rule):
     - pages repaired vs unrepaired, per document;
     - the worker log reads `converter 0, ocr 0`;
     - **every other document's fingerprint is unchanged**;
     - the route split is identical;
     - zero `ready` documents without chunks, and zero chunks without an
       embedding.
   - **e. Re-run the retrieval smoke questions on the repaired Acts.** These
     are §9.8's six and §9.11's nine, with ranks before and after. It is a smoke
     test, not evidence; the yardstick stays the human-written cohort (§9.10 C).
3. **Release database.**
   - Run `build_release_db.py --target ai_gateway --replace`, with the schema
     unchanged at `e1a4c6f9b2d7`.
   - Do §9.9's PG15 path: restore into a `pgvector/pgvector:pg15` container,
     re-dump with PG15's own `pg_dump`, then restore into a fresh PG15 database
     with zero errors.
   - `nrb_files.tar` gains no blobs (the Phase 2 donors are already in scope),
     so it is verified by checksum and not re-shipped.
   - `--replace` gains a guard: it refuses if the target department holds a
     document id absent from the source.

### 8.3 The delivery: a data-only update of a database that now has users

**Since §9.9 the bank's database holds their users, grants and chats. A full
restore would erase them.** The delivery is therefore an **update**:

- **`update.sql`, one atomic transaction**, run with
  `psql -1 -v ON_ERROR_STOP=1`. It is all or nothing, so any failure leaves the
  database untouched. It has three parts:
  - **prologue:** the stop checks, as `DO` blocks that `RAISE`, then the
    deletes (the `nrb` department's chunks, jobs and documents) and the
    truncates (catalog and cache tables);
  - **data:** a PG15 `pg_dump --data-only --format=plain` of exactly the
    replaced tables from `ai_gateway`. That database holds only the `nrb`
    department, so the dump is precisely the replacement. `departments` is
    excluded, because they already have that row;
  - **epilogue:** the `setval`s, and the same verification `SELECT`s that
    `build_release_db.py` prints (route split, chunks without an embedding,
    `ready` documents without chunks) plus the repair split.
- **Stop checks**, each one halting with a message and changing nothing:
  - their `nrb` department's id differs from ours;
  - it holds a document id we did not ship (an admin's own upload);
  - their runner has written sync runs since the delivery;
  - a pipeline run is `queued` or `running` (a restarted runner would pick it
    up).
- **Document ids are preserved** (the re-ingest updates rows in place), so
  citations in their existing chats still resolve.
- **Foreign keys.** These references touch the replaced tables:
  - **within the set**, so COPY order matters:
    - `nrb_files → nrb_sync_runs` and `nrb_files → nrb_fetch_runs`
    - `nrb_sources → nrb_sync_runs`
    - `nrb_source_files → nrb_files` and `nrb_source_files → nrb_sources`
    - `nrb_recovery_units → nrb_recoveries`
    - `document_chunks → documents` and `ingest_jobs → documents`
  - **from tables we do not replace:**
    - `nrb_pipeline_run_jobs → ingest_jobs`, which cascades, so their run→job
      links for the `nrb` department's jobs go;
    - `chat_sessions → departments` and `user_departments → departments`,
      untouched because `departments` is not replaced.
- **The rehearsal, here, before anything is sent.**
  - **Setup:** a PG15 copy of the delivered release, seeded with fake users,
    grants, chats, a foreign upload in another department, and a pipeline run
    with job links.
  - **Proof:** the data-only load succeeds **with foreign keys enforced**
    (dependency-ordered COPY, no `--disable-triggers`); a **per-FK orphan
    count is zero**; the seeded users, grants, chats and foreign upload all
    survive; the NRB counts and the repair split match `ai_gateway`.
  - **Fallback:** `--disable-triggers`, run as `postgres`, only if pg_dump
    reports a circular foreign key. The orphan counts are then the proof.
  - **Stop checks:** each is triggered deliberately once, to show it halts
    with the database unchanged.
- **`UPDATE.md`: copy-paste steps, sent one at a time, each with its expected
  output.** Their server has no AI agent.
  1. Stop the worker and nrb-runner. The API stays up: the transaction means
     chats read the old corpus or the new one, never a mix.
  2. **Back up their live database** (`pg_dump -Fc`). This is the rollback path.
  3. Copy `update.sql` into the Postgres container.
  4. A read-only check run, which prints what the stop checks would say.
  5. The update.
  6. Verification.
  7. Start the services.
  8. A smoke question in the `nrb` tab.
- **Placeholders, each with where its value comes from:**
  - `<postgres-container>`: `docker ps --format '{{.Names}}\t{{.Image}}'`, the
    row whose image is pgvector/postgres;
  - `<worker-container>` and `<nrb-runner-container>`: the same listing;
  - `<nrb_files host path>`: `docker inspect <gateway-container> --format
    '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'`, the line
    ending `/app/nrb_files`. It is used only for a check that every shipped
    document's blob exists there; the update ships no files.
- **Known from `bank-production-server.md`, not assumed:**
  - PostgreSQL 15.18 with its client tools (they made the 2026-09-14 dump and
    restored ours);
  - DB commands run as `postgres`, most likely via `docker exec`;
  - containers run as uid 10001.
- `UPDATE.md` also carries §2's warning: until their code includes the repair,
  re-ingesting an affected document on their side reverts it to garbled text.

---

## 9. Evaluation & Improvement

### 9.1 Success metric

This is the bank's internal knowledge base, so there is no SQL tie; the
nearest proxy is answer accuracy on Acts.

**The single measure is the share of served chunks that still carry the
ToUnicode garbling.** It is read by SQL from chunk metadata:
`text_repair = 'unrepaired'`, plus detected documents not yet re-ingested.
- **Today:** about 1,635 / 26,058 ≈ **6.3%**.
- **Phase 1 target:** the Word 2016+ residue only.
- **Phase 2:** toward zero.

**The guard** is the reader's repair-caused word error rate **≤ 0.5%** (§6.4).
A repaired chunk counts only while the reader's sample says the repair is right.

### 9.2 Eval

- **Labelled cases** (`tests/test_nrb_fontrepair_eval.py`), 8 pages, each scored
  by outcome and, for a repair, an exact match against **reader-confirmed
  reference lines**:

  | # | case | expected |
  |---|---|---|
  | 1 | CPA p.14 (Word 2013, Kalimati-696) | repaired |
  | 2 | NI Act p.3 | repaired |
  | 3 | the directive p.21 measured in §9.8 (47 illegal clusters; the plan names its blob) | repaired |
  | 4 | `075bf12eb087` (Nirmala UI) | repaired, or a recorded finding |
  | 5 | a Word-365 page, in Phase 1 | `unrepaired:coverage` |
  | 6 | a clean Distiller Kalimati page | `not_applicable`, byte-identical |
  | 7 | an English native page | `not_applicable` |
  | 8 | a Preeti page | never reaches `native_unit` |

- **Current pass rate: unmeasured.** Nothing is built. The throwaway probe got
  case 1's direction right, but its output kept 4 illegal clusters and about 6
  legal-but-wrong words, which the gate would have rejected.
- **`reorder.py`** gets exhaustive hand-built cases: `ि` before a cluster,
  `ि` before a conjunct, reph after a matra, `ि` with reph (`र्ति`), rakar
  (`प्र`, the probe's bug), a half-form conjunct (`क्ष`), and orphans of each
  marker.
- **Invariants:**
  - classifier invariance (native-3 = native-2 on status/reason/metrics);
  - `plan_document`/`route_page` unchanged;
  - flag off ⇒ engine string `passthrough/native-2` and byte-identical output;
  - cold and refresh paths agree on `document_suspect`;
  - the import boundary (no fontTools or uharfbuzz in `app.main`);
  - the one-predicate caveat test;
  - "unrepaired keeps native text and is non-authoritative" alongside "a
    failed legacy conversion still withholds".
- **The cohort** (§6.3) and **the reader** (§6.4).

### 9.3 Feedback capture

- **Per page:** `nrb_recovery_units.detail` holds the repair status, the gate
  check that failed, and the counts (glyph uses, runs, run agreement, orphans,
  illegal clusters before/after).
- **Per chunk:** `text_repair`, `repair_engine` and `authoritative`.
- **The worker log** gains `repaired N, unrepaired M`, beside
  `converter 0, ocr 0`.
- **`scripts/nrb_recovery_cache.py --stats`** breaks native units down by repair
  status.
- **Reader verdicts** land in `docs/nrb/native3-review.json`.
- A bad answer is traceable from document to page to repair status using
  stored data alone.

### 9.4 Review loop

- **On every repair-version bump:** re-run the cohort evidence pass
  (non-regression must stay 100%), and give the reader one session on any new or
  changed cell.
- **Before every delivery to the bank, and monthly while their own files
  arrive:** run the detector over new documents, and report any new font ×
  Word-version cell for review.
- **Lifting `authoritative: false` on repaired pages** is decided once, after
  the reader passes the gate. It is a repair-version bump, so only native units
  re-run, and the decision is recorded in §31.

---

## 10. Not changed, and out of scope

- **Not changed:**
  - native-2's classifier, `quality.REASONS`, the route vocabulary,
    `plan_document`, `route_page`;
  - the conversion gate (0.80) and the unjudged gate;
  - `_withhold`;
  - OCR routing;
  - the Phase 6B holdout, which is not read;
  - every CHECK constraint;
  - the Alembic head.
- **Out of scope:**
  - the Online2PDF document and any non-Identity-H encoding (`not_applicable`,
    caveated);
  - pypdf's intra-word spaces;
  - pages where Word wrote correct labels but `/ActualText` would differ;
  - Preeti-converted text, whose correctness is still §15's open review;
  - the title channel, and the rest of retrieval.

## 11. Risks and open questions

1. **Word's shaping vs HarfBuzz.** Uniscribe/DirectWrite might choose a glyph
   HarfBuzz does not, through `locl`, contextual `ि` widths or reph position.
   The 100% run rule would then reject correct pages. That fails safe (the page
   stays as today), but it could make Phase 1 useless. **The development-set run
   agreement is measured first**, and a systematic mismatch is fixed or
   documented before the cohort is drawn.
2. **The layout check could reject pages** where pypdf drops invisible text
   (render mode 3). That also fails safe; its count is reported.
3. **The bank's build may lack the `authoritative: false` caveat path.**
   `sources.py` has it on our main since §29; the bank's fork is unverified. If
   it is missing, repaired text still serves, only without the badge.
4. **Their side re-ingesting before a code deploy** reverts affected documents
   (§2, accepted cost 2).
5. **The real number of affected documents is unknown until §8.2b.** "24" and
   "~30" both came from ad-hoc criteria.

## 12. Files

**New:**
- `app/nrb/glyphtable.py`, `app/nrb/reorder.py`, `app/nrb/shaping.py`,
  `app/nrb/fontrepair.py`;
- `scripts/nrb_native3_cohort.py` (draw + freeze, no network),
  `scripts/nrb_native3_evidence.py` (p4), `scripts/nrb_native3_detect.py`
  (operational, p4 + build), `scripts/nrb_native3_workbook.py`,
  `scripts/nrb_native3_review_import.py`, `scripts/build_nrb_update_sql.py`;
- `docs/nrb/native3-cohort.json`, `docs/nrb/native3-cohort-evidence.{json,txt}`,
  `docs/nrb/native3-review.json`; Phase 2: `docs/nrb/native3-glyph-donors.json`;
- `/home/manoj/nrb-release/UPDATE.md` and `update.sql`;
- tests: `test_nrb_glyphtable.py`, `test_nrb_reorder.py`, `test_nrb_fontrepair.py`,
  `test_nrb_fontrepair_eval.py`, `test_nrb_native_repair_invariants.py`,
  `test_nrb_repair_import_boundary.py`, plus extensions to `test_nrb_dbguard.py`
  and the caveat test.

**Edited:**
- `app/nrb/recovery.py` (`native_unit`, rule 6, the `route: native` meaning);
- `app/nrb/recovery_cache.py` (the version split, report and stats);
- `app/nrb/rag.py` (the fourth dependency, chunk metadata);
- `app/nrb/extraction.py` (`native-3` evidence);
- `app/nrb/dbguard.py`;
- `app/rag/worker.py` (the log line);
- `app/rag/sources.py`, `app/tools/local/search_department_docs.py`,
  `app/tools/local/read_department_doc.py` (the shared predicate);
- `app/config.py`, `.env.example`, `requirements-worker.txt`;
- `scripts/build_release_db.py` (the foreign-document guard);
- `docs/nrb-integration.md` (§31), `docs/nrb-usage.md`, CLAUDE.md (one bullet
  and the status paragraph).

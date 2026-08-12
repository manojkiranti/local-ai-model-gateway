# RAG Ingest Quality — Design

**Date:** 2026-08-12
**Status:** approved, ready to plan

## The failure this fixes

Asked *"areas of investment"* against the `nrb` department, the model answered
with a pointer — *"refer to Chapter 4"* — instead of the content. Reproduced
twice, deterministically. The model behaved **correctly**: given twelve
disconnected scraps it declined to invent content and cited where to look. The
retrieval underneath it failed.

Measured on `"Final V3 Investment Policy 2026"` (docx, doc
`890ec43b2c5f46dd85ea5575a148f5b3`) on 2026-08-12:

| | |
|---|---|
| Chunks for the one document | 559, averaging **181 chars** |
| Chunks whose body is under 60 chars | 278 (50%) |
| Chunks under a `Table of Contents` heading | 51 |
| Top-12 slots carrying substantive content | **2** |
| Top-12 slots that were Table-of-Contents fragments | **7** |
| Rank of the first Chapter 4 (the correct chapter) hit | 10 |

Element-type breakdown for that document:

```
text    306 chunks, avg body   77 chars
list    247 chunks, avg body  148 chars
table     6 chunks, avg body  981 chars
```

Two independent causes.

**1. No accumulation across elements.** `parsing._parse_with_docling` calls
`chunk_text` **inside** the per-element loop, so `rag_chunk_max_chars` (2000) is
only ever an upper bound on a single Docling element, never a target to fill. A
four-character layout fragment (`"2026"`, `"th"`, `"21"`) becomes a
four-character chunk with its own embedding. Chapter 4's seven investment
products are scattered across 29 fragments.

**2. Front matter is indexed and outranks real prose.** The Table of Contents
contains every heading in the document, so it matches nearly any structural
query, and `ts_rank_cd` favours short text — so TOC lines beat real paragraphs.
This is a **lexical-channel** failure: with AND-semantics the phrase "areas of
investment" appears in exactly one chunk in the whole document, a bare heading.

Simulating both fixes over the same candidate pool moves top-12 substantive
content from **2/12 to 12/12**, and Chapter 4 from one hit at rank 10 to three
hits inside the top 12.

Note the simulation is a *lower bound*, and its threshold is not the one this
design ships. It was run against the **existing unmerged** chunks, so it needed
a 120-char cut to clear the debris. The design applies its floor
(`rag_chunk_min_body_chars`, default 40) **after** merging, where the average
body is ~1481 chars — so it discards strictly less real content than the
simulation did while removing the same debris. The two numbers measure
different pipelines and are not in tension.

## Scope

**Docling path only** (`.pdf` / `.docx`). Verified: `.txt` already calls
`chunk_text` once over the whole body, and spreadsheets already buffer rows via
`chunk_table`. Neither fragments. Applying the tiny-body filter globally would
be actively harmful — a legitimately short `.txt` upload would filter to zero
chunks and raise `ParseError`, turning a valid document into a failed ingest.

Of 281 non-archived documents (280 `ready`, 1 `pending`), only **5 are
pdf/docx** — the rest are `.txt`/`.csv` test fixtures. Re-ingestion is therefore
cheap, which matters because Docling on CPU is the slow path.

## Decisions

1. **Merge, then filter — in that order.** Merging first rescues content that
   filtering alone would destroy. The 45-char body
   `"means Assets Liability Committee of the Bank."` is a real glossary
   definition orphaned from its term by cause #1; a filter-first pipeline
   deletes it. After merging, anything still tiny is genuinely orphaned debris.
2. **Merge boundary: heading path, page, table, or `max_chars`.** The heading
   path is the document's own semantic boundary, and `_with_context` already
   stamps exactly one path per chunk, so citations stay accurate.
3. **Drop front matter by exact normalized heading match**, not by structural
   shape. Structural TOC detection (dotted leaders, trailing page numbers) was
   rejected: false positives over a policy document full of numbered clauses and
   limit tables would delete exactly the content most worth indexing.
4. **Backfill via a re-ingest command**, reusing the existing worker and
   `replace_chunks`.
5. **Expose per-channel ranks** so a future retrieval failure is attributable to
   the dense or lexical channel without hand-instrumentation.

## Components

### `app/rag/chunking.py` (pure — no IO, no model calls)

- `@dataclass(frozen=True) Block{text, section, page_number, element_type}` — one
  Docling element before it becomes a `Chunk`.
- `merge_blocks(blocks, *, max_chars) -> list[Block]` — buffers consecutive
  blocks, flushing when **any** of:
  - `section` (heading path) changes
  - `page_number` changes
  - the block is, or follows, a **table**
  - adding the next block would exceed `max_chars`
- `drop_small_blocks(blocks, *, min_body_chars) -> list[Block]` — applied
  **after** `merge_blocks`. **Tables are exempt** regardless of size: a small
  table is real content, and a table's information density is not proportional
  to its character count.

#### Why flush on page change

`page_number` is citation-bearing: `search_department_docs._header:49` renders
`page {n}` into the citation the model is explicitly instructed to cite. A
merged `Block` carries one `page_number`, so merging across a page boundary
would attribute a clause to a page it is not on — in a bank policy, someone
looks up page 12 and the clause isn't there.

Measured: PDFs carry provenance on **248/248** chunks. DOCX carries it on
**0/1118** — correctly, because a `.docx` has no fixed pages, so Docling
supplies no `page_no`. The flush is therefore a no-op for DOCX (the value never
changes from `None`) and a correctness guard for PDF. It cost nothing on the
sample document: merging by section alone and by section+page both yield 43
chunks.

### `app/rag/parsing.py`

`_parse_with_docling` becomes: walk → collect `Block`s → merge → filter → chunk.

1. Walk `iterate_items()` building `Block`s. **Skip** an element when the
   **first segment** of its heading path, normalized (casefold, collapse
   internal whitespace, strip trailing punctuation), is in the configured skip
   set. First-segment-only is deliberate: it catches `Table of Contents` and
   `Table of Contents > 5.2.5 …` while leaving a legitimate
   `Chapter 3 > Index of Limits` indexed.
2. `merge_blocks(...)`
3. `drop_small_blocks(...)`
4. Per surviving block → existing `chunk_text` → existing `_with_context`

`renumber`, embedding, `replace_chunks` and retrieval are untouched.

**Table handling is preserved exactly as it is today.** Note for the record,
because it is easy to assume otherwise: a Docling table does **not** go through
`chunk_table`. It is exported via `item.export_to_markdown(document)` and then
passed to `chunk_text` like any other element (`parsing.py:213-231`).
`chunk_table` is spreadsheet-only — it takes `(headers, rows)`, which Docling's
markdown export does not provide. This design changes none of that; tables gain
only two properties: they act as merge boundaries, and they are exempt from the
tiny-body filter.

### `app/rag/retrieval.py`

`dense.rank` and `lexical.rank` are already computed in the CTEs to drive the
RRF fusion; they are simply never selected out. Add `dense_rank` and
`lexical_rank` to the fused CTE, the final `SELECT`, and `RetrievedChunk`,
alongside the existing `rrf_score` / `dense_distance` / `lexical_score`
diagnostics.

This is diagnostics only — no ranking behaviour changes, and the fields are not
rendered into the model-facing tool result. It exists so the next retrieval
failure can be attributed to a channel from stored data instead of being
reproduced by hand, which is how this bug had to be diagnosed.

### `app/rag/reingest.py` (new, small)

`python -m app.rag.reingest [--department CODE] [--dry-run]`

Selects non-archived documents (optionally one department), calls the existing
`jobs.enqueue` per document, and prints a summary. The worker does the real
work. Reuses the proven path: `replace_chunks` is already atomic and re-checks
status under a row lock, and a failed re-ingest of a `ready` document leaves it
`ready` with its previous chunks intact.

## Config

```python
rag_chunk_min_body_chars: int = 40
rag_skip_sections: str = "table of contents,contents,index"
```

`rag_skip_sections` follows the existing `fetch_url_allowlist` pattern — a
comma-separated string with a derived, normalized property.

**On the value 40:** this is an **empirically chosen default for this corpus,
not a universally safe threshold.** It was picked by sampling body-length bands
in the sample document: the smallest genuine content observed was a 45-char
glossary definition, and post-merge bodies average ~1481 chars, so 40 sits well
clear of real content while catching orphans. A corpus of terse tabular
documents, or a non-English corpus with different tokenization, could need a
different value — that is why it is configurable rather than a constant. Anyone
raising it should re-check the body-length bands for their own corpus first.

## Error handling

| Condition | Behaviour |
|---|---|
| Every block filtered away | `ParseError("document contained only front matter or fragments")` — deliberately **distinct** from the scanned-PDF message, so a scan and a TOC-only file do not look alike to an admin |
| Table export raises | Unchanged: caught, treated as empty, not fatal |
| A single element exceeds `max_chars` | Unchanged: `chunk_text` splits it with overlap |
| Re-ingest hits an active job | `JobConflict` caught → counted as skipped, command continues |
| Re-ingest of a `ready` document fails | Unchanged: stays `ready` with its old chunks |
| `--dry-run` | Enqueues nothing; prints what it would enqueue |

## Testing

All new logic is pure, so none of it requires Docling — the existing subprocess
import-guard test (`test_docling_is_not_imported_at_module_scope`) stays valid.

- **`tests/test_rag_chunking.py`** — `merge_blocks`: merges within a section;
  flushes on heading change, on `max_chars`, on a table, and on page change; a
  `None` page never triggers a flush (the DOCX case, else every DOCX block would
  flush). `drop_small_blocks`: runs post-merge; exempts tables; keeps a 45-char
  body at the default threshold.
- **`tests/test_rag_parsing.py`** — front matter skipped by normalized first
  segment; `Chapter 3 > Index of Limits` **not** skipped (the false-positive
  guard); an all-filtered document raises the distinct `ParseError`.
- **`tests/test_rag_retrieval_integration.py`** — `dense_rank` / `lexical_rank`
  are populated and consistent with the existing scores; a chunk found by only
  one channel has `None` for the other's rank.
- **`tests/test_rag_reingest_integration.py`** — enqueues for non-archived
  documents; `--dry-run` enqueues nothing; `--department` filters; `JobConflict`
  is counted, not raised.

## Evaluation & Improvement

**Success metric.** The share of top-`k` retrieved chunks that are substantive
content rather than front matter, and whether the expected section appears in
the top 5. This is the measure that moves the user-visible failure: an answer
that names a chapter instead of quoting it is a retrieval miss, not a model
miss.

**Baseline (measured 2026-08-12, before any change).** Query
*"areas of investment"*, `top_k=12`: **2/12 substantive**, 7/12 Table of
Contents, correct chapter first appearing at **rank 10**.

**Eval.** 6 labelled queries against the NIC investment policy, each with its
expected section, in `tests/test_rag_retrieval_eval.py`. It needs the embedding
model, so it skips when unavailable, mirroring
`tests/test_rag_embedding_live.py`. **Target: ≥10/12 substantive and the
expected section within the top 5.** Because per-channel ranks are now exposed,
a failing case reports which channel surfaced the bad passage rather than
requiring a hand-built reproduction.

**Feedback capture.** `search_department_docs` calls already land in
`chat_messages.trace` JSONB with the query and the returned passages, so
recurring bad retrievals are greppable from Postgres with no new
instrumentation.

**Review loop.** Monthly, or sooner on a user report. The signature to grep for
is an answer that cites a section title without quoting its content — that is
this exact failure, and it is what the user reported. If it recurs after this
change, the next suspects are the skip list (a front-matter heading worded
differently) and `rag_chunk_min_body_chars` against a terser corpus.

## Not in scope

Structural TOC detection (rejected above); reranking; changing RRF weights or
`rag_candidate_pool`; query rewriting or expansion; OCR; altering `chunk_table`
or the spreadsheet path; changing the `'english'` text-search configuration.
Each is independently additive, and none blocks this fix.

## Known issues (not fixed here)

Even after filtering, a query whose phrasing matches no chunk lexically leans
entirely on the dense channel — `"areas of investment"` is such a query, since
Chapter 4's sections are titled *"Investment in Permitted Shares"*,
*"Investment in securities issued by GON"* and so on. Merging mitigates this by
putting more of a section into each chunk, but a genuinely paraphrased query
against a corpus that never uses the paraphrase remains a dense-only retrieval.
The new per-channel ranks are what will make that diagnosable if it recurs.

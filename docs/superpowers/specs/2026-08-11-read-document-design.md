# read_document — Design (PDF, DOCX, TXT, MD, JSON)

**Date:** 2026-08-11
**Status:** approved, implementing
**Supersedes:** `2026-08-08-read-document-design.md` (designed but never
implemented — no `documents.py`, no `ingest.py`, no `read_document.py` exist).
That spec's format decisions are carried forward verbatim where unchanged; this
one adds PDF and revises the scanned-file handling.

## Goal

Let a user upload a `.pdf`, `.docx`, `.txt`, `.md` or `.json` file, attach it to
a chat turn, and have the model read it. Today the upload allowlist is
spreadsheet-only (`app/files/router.py:48`), so none of these can even be
uploaded. This closes the gateway's weakest side: reading.

Scope is **readable text extraction**. Tables and JSON are flattened to
something legible, not preserved as structured data.

## Decisions

1. **All five formats in one slice.** The plumbing (format dispatch, widened
   allowlist, turn-open summary) is shared; the four non-PDF readers are ~10
   lines each. Building PDF alone would mean reopening the same three files.
2. **`pypdf>=6.0`** for PDF text extraction (resolves to 6.15.0, verified as a
   `py3-none-any` wheel — pure Python, zero dependencies, BSD). Nothing native
   enters the API image. PyMuPDF is AGPL (ruled out for a bank deployment);
   `pypdfium2` ships a native binary and is only present transitively via
   Docling; Docling itself is worker-only by design.
3. **One tool, line-paged.** `read_document(file_id, start_line?, max_lines?)`,
   mirroring `read_excel`'s row paging. A PDF's pages surface as `[page N]`
   marker lines inside the line stream — no second paging unit, no
   format-conditional parameters. No `inspect_document` companion: a document is
   linear text, not a branching workbook.
4. **OCR is fully deferred.** No Docling changes, no worker changes, no queue or
   schema changes, no migrations. This slice builds the *seam* only (see
   "The OCR seam").
5. **Metadata leads the tool result.** Page counts, truncation and next-page
   guidance go at the **top**, not the bottom (see "Why metadata leads").

## Components

### `app/files/documents.py` (new, pure — no DB, no HTTP)

Parallel to `readers.py`. Reports facts; makes no policy decisions.

- `DOCUMENT_EXTS = {".pdf", ".docx", ".txt", ".md", ".json"}`
- `MAX_PDF_PAGES = 500`
- `read_lines(path) -> DocumentText` where
  `DocumentText{kind, lines, pages, text_pages, pages_skipped}`
  (`pages`/`text_pages`/`pages_skipped` are `None` for non-PDF):
  - **`.pdf`** — `pypdf.PdfReader`. Each page contributes a `[page N]` marker
    line followed by its extracted text. A page yielding no text emits
    `[page N] (no extractable text — likely a scanned image)` and nothing else,
    so a gap can never read to the model as "nothing was there". Beyond
    `MAX_PDF_PAGES`, stop and set `pages_skipped`. `text_pages` counts pages that
    produced text — **`text_pages == 0` is returned normally, not raised**; the
    tool decides what that means.
  - **`.docx`** — `python-docx`: each paragraph is one line; a heading
    (`style.name` starts `Heading`) is prefixed `#`; each table renders as
    `a | b | c` pipe rows with a blank line either side.
  - **`.txt` / `.md`** — decode utf-8 (utf-8-sig fallback) with
    `errors="replace"`, then `splitlines()`.
  - **`.json`** — `json.loads` then `json.dumps(indent=2)`. On
    `JSONDecodeError`, fall back to the raw decoded text with
    `kind="JSON (unparsed)"`.
  - Unsupported extension → `ReadError`.
- Exceptions (`ReadError` is reused from `readers`):
  - `EncryptedDocument(ReadError)` — raised only after `decrypt("")` fails.
    Most bank PDFs carry an empty owner password and open fine; only a genuine
    user password raises.
  - `ReadError` — every `pypdf` failure (malformed xref, truncated stream,
    bad object) is caught and wrapped. No `pypdf` exception escapes the module.
- `summarize_document(path) -> DocumentSummary{kind, lines, chars, pages,
  text_pages}` with `.text()` and `.as_dict()`. **Computed from `read_lines`**
  (one parse), so the summary and the tool output can never disagree. A scanned
  PDF summarises as `"PDF, 12 pages, no extractable text (scanned)"`.

Caps live in the tool, not here — the reader returns everything and the window is
applied downstream, symmetric with `readers`.

### `app/files/ingest.py` (new, tiny)

The single answer to "which family is this file", so neither the upload route nor
turn-open branches on extension:

- `SPREADSHEET_EXTS`, `DOCUMENT_EXTS`, and `UPLOAD_TYPES` (ext → stored media
  type) merged from both families. `.xlsm` stays absent.
- `summarize(path)` — dispatches to `readers.summarize` or
  `documents.summarize_document`, both of which already expose `.text()` /
  `.as_dict()`.

Media types: reuse the existing `PDF_MEDIA_TYPE` / `DOCX_MEDIA_TYPE` constants in
`store.py` (already defined for `create_pdf` / `create_docx`); add `text/plain`,
`text/markdown`, `application/json`.

### `app/tools/local/read_document.py` (new)

Thin adapter, same shape as `read_excel.py`. This is where policy lives.

- Validate `file_id` → `resolve_file` (owner-scoped) → path.
- A spreadsheet id returns
  `ERROR: this is a spreadsheet — use inspect_excel / read_excel instead.`
- `documents.read_lines(path)`, then:
  - **Fully scanned PDF** (`pages > 0 and text_pages == 0`) →
    `ERROR: this PDF appears to contain scanned images with no text layer — OCR
    is not available yet.`
  - Otherwise page: `start_line` (1-based, default 1), `max_lines`
    (default/cap `READ_DOC_MAX_LINES = 400`), under the character budget below.
- Registered by adding `read_document.SPEC` to `LOCAL_TOOLS`; `registry.py`
  never changes.

#### Output shape (metadata first)

```
PDF, 12 pages, 340 lines — showing lines 1–40 of 340.
TRUNCATED: call read_document again with start_line=41 to continue.
2 of 12 pages have no extractable text (likely scanned images).

[page 1]
Credit Policy Manual
Effective 2026-04-01
…
```

The `TRUNCATED:` line is present only when truncated; the scanned-pages line only
when some page came back empty.

#### Why metadata leads

`_for_model` in `agent/loop.py` cuts any tool result over
`MAX_TOOL_RESULT_CHARS` (8000) and appends its own `[TRUNCATED …]` note. A
trailing "call again with `start_line=41`" — where `read_excel` puts it — is
exactly what that cut eats. Leading metadata survives. This is a deliberate
divergence from `read_excel`, documented here so it doesn't read as an
inconsistency to be tidied away.

#### Character budget

`READ_MAX_CHARS = 40_000` is retained as the reader's documented ceiling. But the
tool emits at most `MAX_TOOL_RESULT_CHARS` minus header room, so **our own cap
always bites before the loop's**.

**Truncation happens on whole logical lines.** A line that would cross the
budget is dropped entirely, never emitted partially, so the last line in the
output is complete and the header's `start_line=N+1` resumes at exactly the
first line the model did *not* receive. Cutting mid-line would leave the model a
fragment it cannot tell is a fragment, and make the next-page number off by a
partial line. (Edge case: a single line longer than the whole budget is emitted
alone and hard-cut, with the header saying so — otherwise the reader would
deadlock, unable to make progress.)

This is a correctness requirement, not tidiness:
if the tool announced "showing lines 1–400, continue at 401" and the loop then
silently cut the body at 8000 chars, the model would resume at 401 and skip
everything between — a silent data loss that looks like a complete read. Because
the tool truncates, the line numbers it reports are the lines that actually
reached the model.

`read_excel` has the same latent mismatch (40k emitted, 8k delivered). It is
**out of scope here** and recorded in "Known issues" rather than fixed silently.

### Changed call sites (no migration)

- **`app/files/router.py`** — `_UPLOAD_TYPES` becomes `ingest.UPLOAD_TYPES`; the
  xlsx zip-bomb guard extends to `.docx` (also a zip: `ext in {".xlsx", ".docx"}`);
  the parse-check becomes `await asyncio.to_thread(ingest.summarize, dest)`,
  which takes `.xlsx`/`.csv` off the event loop as a free side effect. A
  `ReadError` still unlinks and 400s. `UploadResponse.summary` stays typed
  `dict`; its shape is now a documented union.
- **`app/history/service.py`** — `_resolve_attachments` swaps
  `readers.summarize` for `ingest.summarize`. Nothing else: ownership check,
  persisted `{id, filename, summary}` note and re-emission on later turns are
  already built.

### Routing

This is the 16th tool schema (~+200 prompt tokens). The live risk is a user
attaching a PDF policy and the model reaching for `search_department_docs`
instead. Per the CLAUDE.md convention that tool descriptions *are* the routing
prompt, `read_document`'s description pins it to "a document the USER attached to
THIS chat" and cross-references both the department corpus and the spreadsheet
tools. Locked by a test, as `test_descriptions_route_totals_to_aggregate_excel`
locks the existing pair.

## The OCR seam

OCR is deferred, but this slice makes the later work cheap and non-invasive:

- The scanned-PDF error string is **distinct and asserted in tests** — a future
  OCR path has an unambiguous condition to hook onto.
- `documents.read_lines` reports `text_pages` and emits per-page empty markers,
  so a future router knows *which* pages need OCR, not just that some do.
- Nothing here assumes text extraction is synchronous or in-process.

For the record, on why OCR is not simply "use the worker": the worker's Docling
sets `do_ocr = False` (`app/rag/parsing.py:152`) and already raises "document
produced no text — a scanned PDF needs OCR, which v1 does not do"
(`parsing.py:235`), so routing there today fails identically but slower.
`ingest_jobs.document_id` is a hard FK to `documents` (the department corpus),
while chat uploads live in `generated_files` — disjoint resolvers, so it would
need a schema change. And worker routing makes `POST /v1/files` async, which
chat attachment cannot absorb without "still processing" states.

## Error handling

| Condition | Where | Result |
|---|---|---|
| Unknown / foreign `file_id` | read | `ERROR: no such file (unknown id, or you don't own it).` — never distinguishes the two |
| Spreadsheet id | read | `ERROR: this is a spreadsheet — use inspect_excel / read_excel instead.` |
| Fully scanned PDF | read | `ERROR: this PDF appears to contain scanned images with no text layer — OCR is not available yet.` — **uploads fine** |
| Password-protected PDF | upload → 400, read → `ERROR` | Distinct message; only after `decrypt("")` fails |
| Corrupt PDF | upload → 400, read → `ERROR` | Every `pypdf` exception wrapped in `ReadError` |
| Individual blank page | read | `[page 4] (no extractable text — likely a scanned image)` inline |
| PDF over 500 pages | read | Not an error — first 500 read, `pages 501–620 were not read` in the header |
| Invalid JSON | read | Not an error — raw text, `kind="JSON (unparsed)"` |
| `start_line` past the end | read | States the total line count so the model can re-page |

Upload accepts a scanned PDF because it is a *valid* file the user may still want
stored and attached; it rejects corrupt and password-protected files because
those are unreadable by anything, and the user can act on that at upload time.

## Security

- **Owner-scoping** via the existing `resolve_file` contextvar. A foreign id
  returns the standard "no such file", never data.
- **Size cap** (413) bounds every upload; the zip-bomb guard now covers `.docx`
  as well as `.xlsx`.
- **Text extraction only.** `pypdf` is pure Python with no shell-out and no
  renderer; embedded JavaScript and `/Launch` actions are never executed.
- **Output to the model is bounded three ways** independent of input size:
  page cap, line window, character budget. A decompression-heavy PDF cannot
  blow *context*. It CAN blow *memory*: unlike `.xlsx`/`.docx` (zip-bomb
  guarded in `router.py` at upload time), a `.pdf` has no decompression guard,
  and `pypdf` inflates `FlateDecode` page streams with no size limit inside
  the API process. The three output bounds above are downstream of that
  extraction and don't help until it's already finished. See "Known issues".
- **No `eval`**; JSON via `json.loads`.
- **`errors="replace"`** on text decode — a hostile or binary file degrades to
  mojibake rather than crashing the reader.

## Testing

No binary fixtures are checked in: `fpdf2` (already a dependency) generates the
PDFs at test time — including the image-only pages, since Pillow comes with
fpdf2 — and `pypdf`'s writer produces the encrypted one. A corrupt PDF is a
truncated byte string, no fixture needed.

- **`tests/test_documents.py`** — the pure reader per format. PDF: page markers
  in order; a blank page's marker text; `text_pages == 0` returned (not raised)
  for a fully scanned file; encrypted → `EncryptedDocument`; corrupt →
  `ReadError`; a >500-page file sets `pages_skipped`. Plus docx headings and
  pipe-row tables, md, txt with an undecodable byte, valid JSON pretty-printed,
  invalid JSON falling back with `kind="JSON (unparsed)"`. And
  `summarize_document` text for each.
- **`tests/test_read_document_tool.py`** — missing id; foreign/unknown id;
  spreadsheet pointer error; the fully-scanned distinct error; multi-page
  truncation where the header's `start_line` matches the last line actually
  emitted; metadata present in the first line of output.
- **`tests/test_document_upload.py`** — the widened allowlist accepts `.pdf`,
  `.docx`, `.txt`; `.xlsm` still 400s; the docx zip-bomb guard fires; a **scanned
  PDF uploads successfully (201)** with a summary saying so; a corrupt PDF 400s;
  an encrypted PDF 400s.
- **`tests/test_tool_descriptions.py`** — locks the `read_document` ↔
  `read_excel` ↔ `search_department_docs` cross-references alongside the existing
  totals-routing assertion.

## Evaluation & Improvement

**Success metric.** Extraction fidelity: the share of `read_document` outputs a
human judges faithful to the source — right content, right order, right page
attribution. The failure this replaces is "can't read it at all", so the bar is
legibility, not layout perfection.

**Eval.** 8 labelled cases in `tests/test_document_eval.py`, each a generated
file plus its expected extracted text and summary line:

1. Text PDF, 3 pages → `[page N]` markers in order, text under the right page.
2. Mixed PDF, pages 2–3 image-only → those pages carry the no-text marker, page
   1 and 4 carry text, no error raised.
3. Fully scanned PDF → the distinct scanned error, and a successful upload.
4. `.docx` with a heading, body paragraphs and a 2×3 table → `#` heading and
   pipe rows.
5. `.md` with a list and a code fence → verbatim passthrough.
6. Multi-page `.txt` read with `start_line`/`max_lines` → correct window, header
   `start_line` matches the last line emitted.
7. Nested valid `.json` → pretty-printed, keys indented.
8. Invalid `.json` → raw text, `kind="JSON (unparsed)"`.

Scored as deterministic substring/format assertions — the reader is not a model —
so the target is **8/8** and any failure is a bug. Baseline recorded on first run.

**Feedback capture.** Every `read_document` call already lands in the turn
`trace` JSONB on `chat_messages` with its args and returned text, so recurring
real-world gaps are greppable from Postgres with no new instrumentation.

**Review loop.** Monthly, or sooner on a user report of unreadable output: grep
stored traces for `read_document` results that came back empty, heavily
truncated, or dense with no-text page markers. A cluster of the last one is the
signal that the OCR slice has earned its place — which makes this loop the
trigger for the deferred work, not just a hygiene check.

## Known issues (not fixed here)

`read_excel` emits up to `READ_MAX_CHARS` (40k) while `agent/loop.py` delivers at
most `MAX_TOOL_RESULT_CHARS` (8k), so its trailing next-page guidance can be cut
and its `start_row` advice can overshoot the rows the model actually saw. Same
class of bug this spec avoids for documents by leading with metadata and
self-truncating. Fixing `read_excel` is a separate change.

**No PDF decompression guard.** `.xlsx`/`.docx` get a cumulative-uncompressed-size
check against the zip's `infolist()` before either is ever parsed (`router.py`).
A PDF has no equivalent: `pypdf` decompresses each page's `FlateDecode` stream
into memory with no size ceiling, so a small file engineered to inflate
massively can spike API-process memory before `read_document`'s three output
bounds ever get a chance to apply — they bound what reaches the *model*, not
what `pypdf` allocates while extracting. Deferred rather than fixed here because
the obvious naive guard — stop extracting once a cumulative extracted-character
ceiling is hit — reintroduces the exact lying-header problem this design exists
to prevent: `doc.pages`/`doc.text_pages` (and therefore the header's total line
count) would become partial without any signal saying so, unless the guard is
threaded through as its own reported fact (closer to `pages_skipped`) rather
than a silent early exit. That's real design work, not a one-line change, so it
stays out of scope for this slice.

## Not in scope

OCR / extracting text from scanned pages — scanned PDFs themselves are accepted,
detected and reported in this slice; Docling, worker, queue or schema changes; `.doc`
(legacy binary), `.rtf`, `.odt`; structured extraction (tables or JSON as data
rather than text); search within a document; cross-document questions;
`inspect_document`. Each is additive later; none blocks the core capability.

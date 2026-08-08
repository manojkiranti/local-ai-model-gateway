# read_document — Design

**Date:** 2026-08-08
**Status:** approved, implementing

## Goal
Let a user upload a `.docx`, `.txt`, `.md`, or `.json` file, attach it to a chat
turn, and have the model read its contents — the inverse of `create_docx`, and
the reading counterpart to the spreadsheet tools. Today the upload allowlist is
spreadsheet-only (`.xlsx`/`.csv`), so none of these formats can even be
uploaded. This closes the gap on the gateway's weakest side: reading.

Scope is deliberately narrow: extract **readable text**. Tables and JSON are
flattened to something legible, not preserved as structured data. Structure-aware
extraction is deferred until a real need appears. PDF is explicitly out of scope
here — it carries its own decisions (text-layer vs. scanned/OCR, package choice)
and gets its own brainstorm afterward.

## Approach (chosen forks)
- **One tool, line-paged.** `read_document(file_id, start_line?, max_lines?)`,
  mirroring `read_excel`'s row paging so the model reuses a pattern it already
  drives. No `inspect_document` companion — a document is linear text, not a
  branching workbook, so there is nothing to survey first.
- **Read as text, flatten structure.** `.docx` tables become `a | b | c` pipe
  rows; `.json` is pretty-printed. No structured extraction.
- **Invalid JSON is accepted, not rejected.** Pretty-print if it parses; otherwise
  serve the raw text with a note. Matches how `.txt`/`.md` never reject on
  content — the model can still read near-valid JSON.
- **New pure module**, `app/files/documents.py`, parallel to `readers.py` — the
  same split that keeps `aggregate.py`/`numeric.py` focused.
- **No new dependency** (`python-docx` is already installed for `create_docx`)
  and **no DB migration** (documents reuse `generated_files`, `source=uploaded`).

## Components

### `app/files/documents.py` (new, pure — no DB, no HTTP)
Normalizes any supported document to a list of text **lines**, plus a summary.

- `DOCUMENT_EXTS = {".docx", ".txt", ".md", ".json"}`.
- `read_lines(path) -> DocumentText` where
  `DocumentText{kind: str, lines: list[str]}`:
  - **`.txt` / `.md`** — decode utf-8 (utf-8-sig fallback) with
    `errors="replace"` so a binary file renamed `.txt` yields mojibake, never a
    crash; `splitlines()`.
  - **`.docx`** — `python-docx`: each paragraph → one line; a heading paragraph
    (`style.name` starts `Heading`) is prefixed with `#`; each table renders as
    `a | b | c` pipe rows, one line per row, a blank line before and after.
  - **`.json`** — `json.loads` then `json.dumps(indent=2)`, split into lines. On
    `JSONDecodeError`, fall back to the raw decoded text and set
    `kind="JSON (unparsed)"` so the header line tells the model it is raw.
  - Unsupported extension → `ReadError` (reuse `readers.ReadError`).
- `summarize_document(path) -> DocumentSummary` where
  `DocumentSummary{kind, lines, chars}` with `.text()` (e.g.
  `"Word document, 45 lines"`, `"Text file, 320 lines"`,
  `"JSON, 1,240 lines"`) and `.as_dict()`. **Computed from `read_lines`** (one
  parse), so `kind` and the counts never diverge from what the tool returns —
  invalid JSON reports `kind="JSON (unparsed)"` in both places.

Caps live in the tool, not here (the reader returns everything; the window is
applied downstream), symmetric with how the spreadsheet reader's caps sit above
the raw grid.

### Format dispatch (`app/files/ingest.py`, new, tiny)
A single source of truth for "which family is this file", so the upload route and
the turn-open path never branch on extension themselves:
- `SPREADSHEET_EXTS`, `DOCUMENT_EXTS`, and `UPLOAD_TYPES` (ext → stored media
  type) merged from both families.
- `summarize(path)` — dispatches by extension to `readers.summarize` (spreadsheet)
  or `documents.summarize_document`, both of which already expose `.text()` /
  `.as_dict()`. The two existing call sites switch to this one function.

### `app/tools/local/read_document.py` (new)
Thin adapter, same shape as `read_excel.py`:
- Validate `file_id`; `resolve_file` (owner-scoped) → path.
- If the file is a spreadsheet type, return
  `ERROR: this is a spreadsheet — use inspect_excel / read_excel instead.`
- `documents.read_lines(path)`, then page: `start_line` (1-based, default 1),
  `max_lines` (default/cap `READ_DOC_MAX_LINES = 400`), and a character budget
  `READ_MAX_CHARS` (reuse the 40k from `readers`). Header line names the kind and
  total line count; on truncation, tell the model to call again with the next
  `start_line`.
- Registered by adding `read_document.SPEC` to `LOCAL_TOOLS`; `registry.py`
  unchanged.

### Upload route (`app/files/router.py`)
- `_UPLOAD_TYPES` replaced by `ingest.UPLOAD_TYPES` (now covers all six
  extensions). `.xlsm` still absent.
- The existing **xlsx zip-bomb guard also runs for `.docx`** (docx is a zip) —
  the condition becomes `ext in {".xlsx", ".docx"}`.
- The parse-check/summary step calls `ingest.summarize(dest)`; a document that
  cannot be read raises `ReadError` → unlink + 400, exactly like a bad
  spreadsheet. (Invalid JSON does NOT raise — it reads as raw text.)
- `UploadResponse.summary` stays typed `dict`; its shape is now a union
  (spreadsheet or document), documented in the docstring.

### Turn-open (`app/history/service.py`)
`_resolve_attachments` swaps `readers.summarize` for `ingest.summarize`. Nothing
else changes — documents attach via `file_ids`, get an ownership check, a
persisted `{id, filename, summary}` note, and re-emission on later turns, all
already built.

## Security
- Owner-scoped via the existing `resolve_file` contextvar; a foreign id returns
  the standard `ERROR: no such file`, never data.
- Size cap (413) bounds every upload; the `.docx` zip-bomb guard bounds
  decompressed size. `.txt`/`.md`/`.json` have no decompression step.
- No `eval`; JSON via `json.loads`, never `eval`.
- Text decode uses `errors="replace"` — a hostile/binary file degrades to
  readable mojibake, it does not crash the reader.
- Tool output is bounded by the line + char caps regardless of file size.

## Testing
- `tests/test_documents.py` — the pure reader per format: docx paragraphs + a
  heading + a table rendered as pipe rows; md; txt containing an undecodable
  byte; valid JSON pretty-printed; invalid JSON falling back to raw with the
  `(unparsed)` kind. Plus `summarize_document` text for each.
- `tests/test_read_document_tool.py` — end-to-end through the tool fn: missing
  id, foreign/unknown id, a multi-page document with truncation + next-page
  guidance, and a `.xlsx` id returning the pointer error.
- `tests/test_document_upload.py` — the widened allowlist accepts a `.docx` and a
  `.txt`; the zip-bomb guard still fires on a crafted `.docx`; a `.xlsm` still
  400s; an attached document produces the right summary line.

## Evaluation & Improvement

**Success metric.** Extraction fidelity: for an uploaded document, the share of
read_document outputs whose text a human judges faithful to the source (right
content, right order, tables legible). The failure this replaces is "can't read
it at all", so any readable extraction beats the status quo; the bar is
legibility, not layout perfection.

**Eval.** 5 labelled cases in `tests/test_document_eval.py`, each a file plus the
expected extracted text and summary line:
1. `.docx` with headings, body paragraphs, and a 2×3 table → text includes the
   `#`-prefixed heading and the table as pipe rows.
2. `.md` with a list and a code fence → passes through verbatim.
3. `.txt`, multi-page, read with `start_line`/`max_lines` → correct window +
   truncation guidance.
4. valid `.json`, nested → pretty-printed, keys present and indented.
5. invalid `.json` → raw text served, kind reported as `JSON (unparsed)`.

Scored as substring/format assertions (deterministic — the reader is not a
model), so the target is 5/5 and any failure is a bug. Baseline recorded on
first run.

**Feedback capture.** Every read_document call lands in the turn `trace` JSONB on
`chat_messages` with its args and returned text, so a recurring real-world gap (a
`.docx` feature that flattens badly, an encoding that mangles) is greppable from
Postgres without new instrumentation.

**Review loop.** Monthly, or sooner on a user report of unreadable output: grep
the stored traces for `read_document` results that came back empty or truncated
oddly, look at the source format, and extend `documents.py` + the eval set with
the real case.

## Not in scope
PDF (its own brainstorm), OCR / scanned documents, `.doc` (legacy binary),
`.rtf`, `.odt`, structured extraction (tables/JSON as data rather than text),
search within a document, and cross-document questions. Each is additive later;
none block the core reading capability.

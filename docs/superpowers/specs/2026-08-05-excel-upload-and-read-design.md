# Excel/CSV Upload & Read — Design

**Date:** 2026-08-05
**Status:** approved, implementing

## Goal
Let a user upload a spreadsheet (`.xlsx`/`.csv`), attach it to a chat turn, and
have the local model answer questions about its contents. Mirrors the existing
`create_excel` (generate) capability with the inverse (ingest + read).

## Approach (chosen forks)
- **Read strategy:** two read tools — `inspect_excel` (structure of every sheet)
  and `read_excel` (rows of one sheet, capped). Model reasons over the rows
  itself; no pandas, no structured-query tool. Honest truncation with guidance.
- **Storage:** reuse `generated_files` + a new `source` column
  (`'generated'|'uploaded'`). Uploads inherit owner-scoped download / list /
  delete for free.
- **Attach:** `file_ids` on `POST /v1/chat`; gateway verifies ownership and
  injects a short system note naming the attached files by id + summary.
  Persisted on the user message (`chat_messages.attachments` JSONB) so the note
  is re-emitted on later turns without the frontend resending ids.
- **Formats:** `.xlsx` + `.csv`, normalized to one `Table` shape.

## Components

### `app/files/readers.py` (new, pure — no DB, no HTTP)
`load_table(path, *, sheet=None) -> Table` and
`inspect_workbook(path) -> list[SheetInfo]`.
- xlsx: `openpyxl load_workbook(read_only=True, data_only=True)` — cached values,
  **formulas never evaluated**. Iterates `wb.sheetnames`; reports hidden sheets;
  skips chart-only sheets (no cells).
- csv: stdlib `csv`, `utf-8-sig`, delimiter sniff; presented as ONE pseudo-sheet
  named after the file stem.
- `Table{sheet_name, headers, rows, total_rows, total_cols, truncated}`.
- Caps: `READ_MAX_ROWS≈200`, `READ_MAX_CHARS≈40_000`, cell text clipped.

### `POST /v1/files` (upload, authed, multipart)
Field `file`. Order of guards:
1. size cap `UPLOAD_MAX_BYTES` (default 10 MB) while streaming → 413.
2. extension allowlist `.xlsx/.csv` (reject `.xlsm`) → 400.
3. xlsx zip-bomb guard: sum uncompressed sizes, refuse > ~200 MB → 400.
4. parse check (open/decode); unparseable → unlink + 400.
Stored at `FILES_DIR/<user_id>/<uuid>.<ext>` (uuid name, never the upload name).
Writes a `generated_files` row with `source='uploaded'`. Returns file meta +
`summary` (sheets/rows/headers) for an immediate UI chip.

### Tools `inspect_excel` / `read_excel`
- Both take `file_id`; resolve to a path via a contextvar **file source** that
  only returns the path if the caller owns it (symmetric to the file sink).
- `inspect_excel(file_id)` → every sheet: name, rows/cols, headers, column types
  + samples, first ~10 rows, hidden flag.
- `read_excel(file_id, sheet?, columns?, start_row?, max_rows?)` → one sheet as a
  text table, capped. On a multi-sheet file with `sheet` omitted: read the first
  sheet AND list the other sheet names in the response (no silent wrong-tab).
- Unknown/foreign id → `ERROR: no such file` (never data).

### Plumbing
- `turn_files(user_id, session_id)` context manager installs sink + source
  together, replacing the single `with file_sink(...)` in `chat/router.py`
  (still inside the stream generator).
- `ChatTurnRequest.file_ids: list[str] | None`. `open_turn` verifies ownership
  (404 on foreign id), persists ids on the user message, injects the system note.
- `build_context_messages` re-emits the note from persisted `attachments`.

## Migration (one)
`generated_files.source String(16) not null server_default 'generated'`;
`chat_messages.attachments JSONB null`.

## Security
Owner-scoped on every read; no user-controlled path component; no formula/macro
execution; row/cell caps bound memory + context; size + zip-bomb caps on upload.
**Flagged, not solved here:** (1) uploaded cell contents are untrusted text
entering the model context — same prompt-injection class as `fetch_url`
(STATUS.md); (2) uploaded sheets will contain client PII/financials — stays
owner-scoped and out of logs.

## Dependencies
None new for logic (`openpyxl` already pinned). Pin `python-multipart`
(already installed, now a direct dep) in `requirements.txt`.

## Tests (TDD, offline)
readers (both formats, caps, cached formula values, multi-sheet, corrupt) ·
upload route (happy, 413, bad ext, corrupt, zip-bomb, lands as `uploaded`,
owner-scoped download/delete) · tools (fake source, foreign id → ERROR, sheet
select, omitted-sheet lists others, truncation message) · chat (`file_ids`
ownership 404, note injected, attachment survives to next turn).

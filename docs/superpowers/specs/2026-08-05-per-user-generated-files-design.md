# Per-user generated files — design

_2026-08-05_

## Problem

Generated files (create_excel/html/chart/pdf) all land in one flat
`generated_files/` dir with an **in-memory** UUID→record index. Consequences:

- The index is lost on restart (files orphaned); nothing can list a user's files.
- `GET /v1/files/{id}` is authed but **not owner-scoped** — any logged-in user
  who has the UUID can download any file.
- Tool functions call `file_store.save(...)` and never see the caller, so there
  is no `user_id` to scope by.

Goal: a user can see **all their generated files** in the UI, downloads are
locked to the owner, and the index survives restarts.

## Decisions (locked with the user)

1. **Index storage:** Postgres table `generated_files` (source of truth,
   survives restarts, listable) — mirrors chat history.
2. **Owner scoping:** `GET /v1/files/{id}` returns 404 unless the caller owns it.
3. **On-disk layout:** per-user subfolder `generated_files/{user_id}/{uuid}.ext`.

## Data model

New ORM model `GeneratedFile` → table `generated_files` (mirrors `ChatSession`):

| column       | type            | notes                                        |
|--------------|-----------------|----------------------------------------------|
| `id`         | String(32) PK   | uuid-hex, unguessable, stable render key      |
| `user_id`    | FK users.id     | index, not null, `ondelete=CASCADE`           |
| `session_id` | String(32) null | chat that produced it (FK chat_sessions, `ondelete=SET NULL`); enables "files in this chat" later |
| `filename`   | String(255)     | display/download name                         |
| `media_type` | String(128)     | e.g. application/pdf                          |
| `size`       | Integer         | bytes                                         |
| `path`       | String(1024)    | on-disk path                                  |
| `created_at` | timestamptz     | server_default now()                          |

One Alembic autogenerate migration.

## The save path — contextvar "file sink"

Tools call `file_store.save(...)` and `registry.dispatch(name, args)` does not
pass the user. Rather than change every tool signature, thread the caller via a
**contextvar** holding a *file sink*:

- `ContextVar[_FileSink] current_sink`.
- At the start of each turn the chat router (which has `user` and `session_id`)
  sets a `PostgresFileSink(user_id, session_id)` for the turn's duration, then
  resets it.
- `file_store.save(data, *, filename, media_type)` becomes **async** and
  delegates to `current_sink.get()`. The Postgres sink:
  - writes `generated_files/{user_id}/{uuid}.ext`,
  - inserts the `generated_files` row in **its own committed transaction** (a
    fresh `SessionLocal()`), so a created file is durable even if the turn later
    fails. (Decoupled from the chat transaction on purpose.)
- **Fallback:** if no sink is set (calls outside a turn, and offline tool tests),
  `save` uses the existing in-memory disk store (today's behaviour). So the 4
  tools only change `file_store.save(...)` → `await file_store.save(...)`, and
  offline tool tests keep passing without a DB.

**Streaming gotcha:** the contextvar must be set *inside* the async generator
Starlette iterates (not merely in the router before returning the
`StreamingResponse`), or the sink is invisible while the loop runs. Set/reset it
in the turn-execution coroutine and the streaming generator alike.

## Endpoints

- `GET /v1/files/{id}` — now **owner-scoped**: look the row up in Postgres; 404
  unless `row.user_id == caller.id`; stream from `row.path`
  (`FileResponse`, `Content-Disposition: attachment`, `nosniff`). 410 if the row
  exists but the file is gone from disk.
- `GET /v1/files` (new) — the caller's files, newest first:
  `[{id, filename, media_type, size, created_at}]`. For the UI "my files"
  gallery. Mirrors `GET /v1/sessions`.

Data access lives in `app/files/repository.py` (no commits inside request-scoped
functions; same convention as `history/repository.py`). The Postgres sink is the
one place that commits on its own (independent durability).

## Testing

- **Repository / integration** (skip when PG unreachable, like existing suite):
  `record_file` inserts a row; `list_files` returns newest-first for a user and
  excludes other users' files; `get_owned_file` returns None for a non-owner.
- **Router:** `GET /v1/files` returns only the caller's files; `GET
  /v1/files/{id}` 404s for a non-owner; 200 + bytes for the owner.
- **Offline tool tests unchanged:** create_excel/chart/pdf tests keep using the
  fallback in-memory sink (no DB), asserting the same download-link string shape.

## Out of scope (later)

- Deleting files (`DELETE /v1/files/{id}`), pagination of `GET /v1/files`,
  orphan cleanup of on-disk files whose rows were rolled back, per-file quotas.
- Backfilling the pre-existing in-memory files (there is no durable record of
  them to migrate).

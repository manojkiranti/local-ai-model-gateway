# Chat source citations (citations v2) — design

**Date:** 2026-08-19  **Branch:** `feat/citations-v2` (forked from `main`, currently
identical to it)  **Status:** approved design, not yet implemented

## 1. The gap

Phase 8 (§29 of `docs/nrb-integration.md`) put citation data in the **string** that
`search_department_docs` returns to the model: `[N] "title" — page 7 — doc=<id>`,
plus, for an NRB chunk, a `route:` line with the machine-recovered caveat and the
public `source:` URL. That is everything the *model* needs.

Nothing structured leaves the gateway. `ChatTurnResponse` carries
`session_id / message / model / stop_reason / trace`; there is no `sources` field,
no `chat_messages.sources` column and no way to open a cited document. A frontend
therefore cannot render source chips or links, and a user cannot verify an answer —
which matters most for exactly the documents whose text is machine-recovered and
explicitly unverified (§15, §16.6, §17.6).

This design closes that gap: the same provenance the model already sees, published
as structured data on every path an answer travels.

## 2. Prior art, and what is reused

`feat/rag-source-citations` (commits `2779867`, `a60f08b`) implemented this and was
**deferred by decision**, not abandoned (§9.10, §27). It is the starting point:
cherry-pick both commits onto `feat/citations-v2`, drop what `main` has since
acquired, then layer on what v1 could not have known about.

Already on `main` from other work, so **dropped from the cherry-pick**: the Docling
CPU/no-OCR pipeline pinning in `app/rag/parsing.py`, and `Dockerfile.worker`'s
OpenCV native libraries.

Still missing on `main`, so **kept**: `app/rag/sources.py`, `chat_messages.sources`
(migration `d4a91f2c7b3e`), the chat/history wiring, the document download route,
`Settings.rag_docs_base`, and ~900 lines of tests.

New in v2, because v1 predates the entire NRB integration: NRB provenance on the
payload (§4.2), and the filestore branch in the download route (§5.2).

## 3. Decisions

| Decision | Choice | Why |
|---|---|---|
| Payload granularity | Document-level, pages aggregated | A reader wants one link per document, not one entry per retrieved chunk. Passage-level is a non-goal (§9). |
| NRB provenance | Included | A citation that hides the extraction route presents machine-recovered text as authoritative. §29 already refuses to do that for the model; the UI must not undo it. |
| Delivery on a stream | Terminal `done` event only | Citations resolve against the **final answer's** `[N]` markers, so they cannot be correct before the turn ends. One shape, no partial state. |
| Reuse strategy | Cherry-pick v1, then adapt | Keeps a reviewed design and its tests. |
| Alembic | Rebase `d4a91f2c7b3e` onto `f4c1a90b7d62` | §27.4's prescribed route: one linear head, no merge revision. Cost is a one-off dev-DB reconciliation (§6). |
| `EXPOSE_TRACE` | Does **not** gate sources | A trace is diagnostics; a citation is the product. v1's rule, kept. |

## 4. The contract

### 4.1 Where `sources` appears

| Surface | Shape |
|---|---|
| `POST /v1/chat`, `stream:false` | `sources: SourceOut[] \| null` on the response body |
| `POST /v1/chat`, `stream:true` | `"sources"` on the terminal `done` event (and `null` on the MCP-failure `done`) |
| `GET /v1/sessions/{id}` | `sources` on each assistant `MessageOut` |
| `chat_messages.sources` | JSONB, written on every assistant row, `NULL` when no corpus was searched |
| `GET /v1/departments/{code}/documents/{id}/download` | the cited document's original bytes |

`null` rather than `[]` when no search ran: an empty list reads as "we searched and
found nothing", which is a different fact.

### 4.2 `SourceOut`

```jsonc
{
  "document_id": "9f2c…", "title": "Leave Policy 2024",
  "department_code": "hr",
  "file_name": "leave-policy.pdf", "file_type": "pdf",
  "pages": [3, 7],          // ascending; empty for csv/xlsx/typed text — not an error
  "cited": true,            // the model's [N] named it, vs. merely presented to it
  "download_url": "/v1/departments/hr/documents/9f2c…/download",

  // Present only for an NRB-origin document; null/absent for an ordinary upload.
  "origin": "nrb",
  "source_url": "https://www.nrb.org.np/…",
  "published_at": "2024-05-02",
  "routes": ["ocr", "legacy_conversion"],   // union over this document's contributing pages
  "machine_recovered": true,
  "verify_note": "machine-recovered — VERIFY figures, dates and names against the source"
}
```

Field rules:

* **`download_url` is derived at serialization**, never stored, so persisted rows
  survive a route change. Safe because `departments.code` is immutable (`PATCH`
  updates only `name`/`is_active`).
* **`verify_note` is emitted from the same module-level constant
  `search_department_docs` renders into the model's context.** Two copies of that
  sentence would drift; a test asserts one constant with two readers.
* **`routes` is a union over the pages of that document that were presented to the model**, because a single
  NRB PDF is routed per PAGE (§16) and may mix native, converted and OCR'd text.
* **`machine_recovered`** is true when any contributing chunk's route is
  `ocr`/`legacy_conversion` or its metadata says `authoritative: false`. Native NRB
  text gets `routes` **without** the flag — over-warning on trustworthy text trains
  a reader to ignore the warning (§29.2).
* `origin` mirrors `documents.metadata.origin` (`nrb`) or falls back to
  `documents.source` (`upload`/`manual`).

### 4.3 Frontend notes

Downloads are behind JWT, so `<a href>` cannot fetch them — the frontend must GET
with the `Authorization` header and build a blob URL, exactly as it already does for
`/v1/files/{id}`. `docs/frontend-sync-prompt.md` is updated with the whole contract.

## 5. Mechanism

### 5.1 Collection and resolution (`app/rag/sources.py`)

A tool returns a string, so there is no return channel for structured provenance.
The collector is a **contextvar**, installed by the chat router beside `turn_files`
and `_department_scope`, and it inherits both of their rules:

* it must be installed **inside** the async generator Starlette iterates, or it is
  invisible while the loop runs;
* the collector object is constructed **outside** the `with`, because the `finally`
  that persists the assistant row runs after `source_scope` has reset the
  contextvar.

`record_search` is a no-op when nobody installed a collector, so direct tool tests
and any future non-chat caller need not care.

**Only passages that survived the tool's character budget are recorded.** A passage
the budget dropped was never in the model's context; listing its document as a
source would be fabricated provenance. This is why `_format` returns
`(text, presented)` and why both of its drop paths discard from the END, preserving
`[1..k]` numbering.

Resolution against the final answer:

| Case | Result |
|---|---|
| No search ran | `None` |
| One search, parseable `[N]` markers | those documents, `cited: true`; out-of-range markers dropped, not an error |
| One search, no parseable markers | every presented document, `cited: false` — the answer was still grounded in them |
| Several searches | every presented document, `cited: false` — each call restarts numbering at `[1]`, so a marker is genuinely ambiguous and guessing would link the wrong file |

`SourceChunk` (deliberately not `retrieval.RetrievedChunk`, so this module stays
free of the database import and unit-testable without Postgres) gains
`origin`, `route`, `authoritative`, `source_url`, `published_at`. All five are read
from the `chunk_metadata` / `doc_metadata` that `RetrievedChunk` **already carries**
— §29 added them and retrieval deliberately does not interpret them.

### 5.2 Retrieval

One additive change: the search SELECT also returns `doc.file_name` and
`doc.file_type`, carried on `RetrievedChunk` with defaults. The `documents` join is
already there for `title`, so the two columns are free. No schema change.

### 5.3 The download route

`GET /v1/departments/{code}/documents/{document_id}/download`, JWT-protected.

Status codes follow the routes beside it rather than one blanket rule: an ungranted
**department** is 403 (as `_require_department_access` and `GET /{code}/documents`
already answer), while anything at **document** granularity is 404 — unknown id, a
document in another department, or a non-`ready` document requested by a member.
Distinguishing "exists but you may not have it" from "does not exist" would leak
the corpus's shape. Traversal on the round-tripped `storage_key` is a 404; a row
whose bytes are missing on disk is a 404, not a 500.

**Byte resolution branches on origin, and the NRB branch is the part v1 could not
have had:**

* `origin == "nrb"` → `filestore.resolve_path(storage_key_for(content_hash, file_type))`,
  reconstructed from the content hash rather than read from `storage_key`. This
  mirrors `worker._document_path` exactly (§28), so a row minted under the old
  copy scheme still resolves without a migration. Since §28 there is **no copy**
  under `RAG_DOCS_DIR`, so v1's single resolution path would 404 every NRB
  citation.
* otherwise → `resolve_storage_path(doc.storage_key, settings.rag_docs_base)`.

`app.nrb.filestore` is stdlib + config only and the API already imports
`app.nrb.router`, so no import boundary is crossed and no worker-only dependency
is pulled in.

`file_type` maps to a media type from a closed vocabulary; anything unrecognised is
`application/octet-stream` rather than guessed, because a wrong type invites the
browser to render it inline. The download filename is sanitised of control
characters and falls back to the title for typed-in (`source='manual'`) documents,
which have no `file_name`.

### 5.4 Path anchoring (`Settings.rag_docs_base`)

A relative `RAG_DOCS_DIR` is anchored to the **repository root**, never the process
CWD, and an absolute value is used verbatim. `filestore.base_dir()` already does
precisely this for the NRB tree, for precisely this reason. It becomes load-bearing
here because the API turns into a *reader* of the corpus tree: a CWD-relative read
in a process launched from an unexpected directory is the §18 defect class — the
call "succeeds", nothing is served, and no health check notices.

### 5.5 Deployment gap found while checking §5.3 (pre-existing)

`NRB_FILES_DIR` appears in neither `.env.docker` nor `.env.docker.example`, and no
`nrb_files` volume is mounted in `docker-compose.yml`. In containers it therefore
defaults to `/app/nrb_files`: unshared between `nrb-runner` (which downloads) and
`worker` (which reads), and invisible to `gateway` (which would serve it). This
predates citations — it is §28 follow-through, since §28 removed the `RAG_DOCS_DIR`
copy that had been masking it — but NRB citation downloads cannot work in Compose
until it is fixed, so this work adds the named volume to all three services plus the
env line to both templates.

## 6. Alembic and the dev database

### 6.1 The graph

Both feature branches fork from `main`'s old head `c33c0fd56028`:

```
c33c0fd56028
  ├── NRB: 9a1c4f7b2e05 → … → f4c1a90b7d62   (7 revisions, merged to main)
  └── citations: d4a91f2c7b3e                (deferred sibling)
```

Per §27.4, un-deferring citations means **re-pointing `d4a91f2c7b3e.down_revision`
to `f4c1a90b7d62`** — the sibling becomes a descendant. No merge revision, ever;
the graph stays linear. The revision id is kept, because `local_ai_gateway` is
stamped at it.

### 6.2 The dev-database reconciliation (explicit, one-off, gated)

Rebasing fixes the graph but **not** the stamped database. `local_ai_gateway` is at
`d4a91f2c7b3e` and has `chat_messages.sources`, but holds **no NRB schema**. After
the rebase Alembic would consider it already at head, apply nothing, and leave the
seven NRB migrations permanently unapplied — so `/v1/nrb/*` would fail against it.

§27.4 assigns this reconciliation to the citations owner. It is:

1. Drop `chat_messages.sources` on `local_ai_gateway` (development data only).
2. `alembic stamp c33c0fd56028` — the baseline both branches forked from.
3. `alembic upgrade head` — applies the 7 NRB revisions, then citations.
4. Verify: `alembic heads` prints exactly one; `alembic current` is the citations
   revision; `chat_messages.sources` and the `nrb_*` tables both exist.

This is destructive and must not be run without the user's explicit go-ahead at that
step. It is also the **only** case in which the CLAUDE.md prohibition on stamping is
lifted, and only because §27.4 wrote this exact procedure for this exact moment.
`local_ai_gateway_p4` is not touched by any of it.

## 7. Files touched

| File | Change |
|---|---|
| `app/rag/sources.py` | new (from v1) + NRB fields on `SourceChunk`, `routes`/`machine_recovered`/`verify_note` in the document rollup |
| `app/rag/retrieval.py` | SELECT `doc.file_name`, `doc.file_type`; carry on `RetrievedChunk` |
| `app/tools/local/search_department_docs.py` | `_format` returns `(text, presented)`; `record_search` with NRB metadata; export the caveat constant |
| `app/chat/router.py` | install `source_scope`; resolve after the loop; both turn paths; `done` event |
| `app/chat/schemas.py` | `SourceOut`; `sources` on `ChatTurnResponse` |
| `app/history/{models,repository,router,schemas}.py` | `sources` column, write path, replay with derived `download_url` |
| `app/rag/router.py` | the download route; `rag_docs_base` at every call site |
| `app/rag/worker.py` | `rag_docs_base` at its resolution call site |
| `app/config.py` | `PROJECT_ROOT`, `Settings.rag_docs_base` |
| `alembic/versions/d4a91f2c7b3e_*.py` | rebased `down_revision` |
| `docker-compose.yml`, `.env.docker`, `.env.docker.example`, `.env.example` | `nrb_files` volume + `NRB_FILES_DIR` |
| docs | this spec, the plan, `CLAUDE.md`, `AGENTS.md`, `README.md`, `docs/frontend-sync-prompt.md`, `docs/nrb-integration.md` §30 |

## 8. Testing

Cherry-picked from v1: `tests/test_rag_sources.py` (pure resolution),
`test_rag_sources_persistence.py`, `test_rag_source_presentation.py`,
`test_rag_document_download.py`, `test_config_paths.py`, `test_env_templates.py`.

Added for v2:

1. An NRB citation carries `origin`, `routes`, `source_url`, `published_at`.
2. An OCR or legacy-conversion page sets `machine_recovered` **and** `verify_note`.
3. A native NRB page carries `routes` **without** `machine_recovered`.
4. A generic upload's payload is byte-for-byte what v1 produced (no NRB keys).
5. A turn mixing an NRB document and an ordinary upload produces both shapes.
6. Downloading an NRB document resolves through the filestore, not `RAG_DOCS_DIR`.
7. The caveat sentence is one constant with two readers (tool text and API field).
8. A budget-trimmed passage's document is absent from `sources` (anti-fabrication).

## 9. Non-goals

Passage-level payloads and inline `[N]` anchor mapping; reranking or abstention
(retrieval's fused score has no absolute meaning, so it is not a threshold); a
global cross-department NRB corpus (§29.1, deferred); the UI itself, which lives in
the separate `local-ai-model-frontend` repo — only its contract doc is updated here.

## 10. Evaluation & Improvement

**Success metric.** Every RAG-grounded answer returns at least one source, and no
source names a document that was not in the model's context. The second half is the
one that matters: fabricated provenance is worse than absent provenance, and the
budget-survivor rule (§5.1) is what enforces it. Proxy for SQLs; the first
user-visible citation surface in the product.

**Eval.** 10 labelled turns scored against an expected `sources` JSON: single search
with markers; single search without markers; multiple searches; no search; an
out-of-range `[9]` marker; a budget-trimmed result; NRB `ocr`; NRB
`legacy_conversion`; NRB `native`; a generic upload. Pass = exact match on
`document_id`, `pages`, `cited`, `machine_recovered`. Current rate: not yet run.

**Feedback capture.** `chat_messages.sources` is itself the audit log — every turn's
resolved provenance is persisted regardless of `EXPOSE_TRACE`. Out-of-range marker
counts are logged as the signal for prompt tuning (a model citing `[9]` over five
passages is a prompt problem, not a resolver problem). No new store.

**Review loop.** After the first real NRB corpus ingest (do citations render with the
expected route split on live chunks), after the frontend renders them (are chips and
the verify badge usable), and after §15's Nepali review — which may soften the
caveat wording for conversions that are by then verified.

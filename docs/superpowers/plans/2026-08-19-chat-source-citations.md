# Chat Source Citations (v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the provenance the model already sees as structured `sources` on
every chat surface, and let a user open the cited document.

**Architecture:** A per-turn contextvar collector (`app/rag/sources.py`) records the
passages `search_department_docs` actually showed the model; after the agent loop
ends, the model's `[N]` markers are resolved against that list into document-level
sources, returned on `/v1/chat`, on the stream's `done` event and on session replay,
and persisted to `chat_messages.sources`. A JWT'd download route serves the cited
bytes, resolving NRB documents from the content-addressed NRB filestore and
everything else from `RAG_DOCS_DIR`.

**Tech Stack:** FastAPI/Starlette, Pydantic v2, SQLAlchemy 2 async + asyncpg,
Alembic, Postgres + pgvector, pytest. Python 3.10, this repo's `.venv` only.

**Spec:** `docs/superpowers/specs/2026-08-19-chat-source-citations-design.md`

## Global Constraints

- Use **this** checkout's `.venv` for everything (`.venv/bin/pytest`,
  `.venv/bin/alembic`). Never a sibling project's environment.
- Branch is `feat/citations-v2`. Do not commit to `main`.
- **Never** apply, revert or stamp a migration without the user's explicit
  go-ahead. Task 3 is gated on it and says so.
- Contextvars used by tools (`turn_files`, `_department_scope`, `source_scope`)
  MUST be installed **inside** the async generator Starlette iterates.
- Docling, torch and any OCR stack must never enter the API import path.
  `app.nrb.filestore` is stdlib + config only and is safe to import in the API.
- `sources` is **never** gated by `EXPOSE_TRACE`. The trace is diagnostics; a
  citation is the product.
- A source may only name a document whose passage **survived the tool's character
  budget** — a trimmed passage was never in the model's context.
- The caveat sentence is ONE constant with two readers:
  `machine-recovered — VERIFY figures, dates and names against the source`
- Every production behavior change ships with tests in the same task.
- Current Alembic head: `f4c1a90b7d62`. Pre-fork baseline: `c33c0fd56028`.
  Deferred citations revision: `d4a91f2c7b3e`.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/rag/sources.py` | Collector, `[N]` resolution, document rollup, the caveat constant, `download_url` derivation. No DB import — unit-testable without Postgres. |
| `app/rag/retrieval.py` | Adds `doc.file_name`, `doc.file_type`, `doc.source` to the SELECT. Interprets nothing. |
| `app/tools/local/search_department_docs.py` | Reports which passages it presented, with their metadata. Renders the same caveat constant into the model's text. |
| `app/chat/{router,schemas}.py` | Installs the collector, resolves after the loop, serializes `SourceOut`. |
| `app/history/{models,repository,router,schemas}.py` | Persists and replays `sources`. |
| `app/rag/router.py` | The document download route and its byte resolution. |
| `app/config.py` | `PROJECT_ROOT`, `Settings.rag_docs_base`. |

---

## Task 1: Land the deferred v1 implementation

The whole citations feature exists on `feat/rag-source-citations` and was deferred,
not abandoned. Cherry-pick commit `2779867` only. The second commit (`a60f08b`)
is mostly work `main` already has (Docling CPU pinning, `Dockerfile.worker`'s
OpenCV libraries) plus stale env-template edits; its one useful piece
(`rag_docs_base`) is Task 4, hand-applied.

**Files:**
- Create (by cherry-pick): `app/rag/sources.py`,
  `alembic/versions/d4a91f2c7b3e_add_chat_message_sources.py`,
  `tests/test_rag_sources.py`, `tests/test_rag_sources_persistence.py`,
  `tests/test_rag_source_presentation.py`, `tests/test_rag_document_download.py`
- Modify (by cherry-pick): `app/chat/router.py`, `app/chat/schemas.py`,
  `app/history/{models,repository,router,schemas}.py`, `app/rag/router.py`,
  `app/tools/local/search_department_docs.py`
- Resolve by hand: `app/rag/retrieval.py` (the only conflict)

**Interfaces:**
- Produces: `SourceChunk(document_id, title, file_name, file_type, page_number)`,
  `SourceCollector`, `source_scope(collector)`, `record_search(department_code, chunks)`,
  `resolve_sources(records, answer) -> list[dict] | None`,
  `with_download_urls(sources)`, `download_url_for(code, document_id)`;
  `RetrievedChunk.file_name` / `.file_type`;
  `GET /v1/departments/{code}/documents/{id}/download`.

- [ ] **Step 1: Confirm the starting point**

```bash
git rev-parse --abbrev-ref HEAD      # expect: feat/citations-v2
git status --porcelain               # expect: empty
git log --oneline main..HEAD         # expect: only the spec commit
```

- [ ] **Step 2: Cherry-pick the implementation commit, leaving it staged**

```bash
git cherry-pick -n 2779867
```

Expected: one conflict, `CONFLICT (content): Merge conflict in app/rag/retrieval.py`.
Every other file applies cleanly. This is a "both added fields" conflict, not a
semantic one.

- [ ] **Step 3: Resolve `app/rag/retrieval.py` by keeping both sides**

`main` added `dense_rank`/`lexical_rank`/`chunk_metadata`/`doc_metadata` (§29);
v1 added `file_name`/`file_type`. Both are wanted. The dataclass tail becomes:

```python
    # Which rank each channel gave this chunk, or None if that channel did not
    # return it at all. Diagnostics only — never rendered into the tool result.
    dense_rank: int | None
    lexical_rank: int | None
    # The chunk's `document_chunks.metadata` and its document's `documents.metadata`,
    # verbatim. Retrieval does not interpret them — an NRB chunk carries `route`
    # and (for OCR) `authoritative: false` here, and its document carries
    # `page_url`/`published_at`. Empty for a generic upload.
    chunk_metadata: dict = field(default_factory=dict)
    doc_metadata: dict = field(default_factory=dict)
    # Carried for citations, not for retrieval. Defaulted so existing callers
    # that construct this by position keep working; the `documents` join is
    # already there for `title`, so these columns are free.
    file_name: str | None = None
    file_type: str | None = None
```

And the constructor tail in `search_chunks`:

```python
            dense_rank=(None if r["dense_rank"] is None else int(r["dense_rank"])),
            lexical_rank=(
                None if r["lexical_rank"] is None else int(r["lexical_rank"])
            ),
            chunk_metadata=_as_dict(r["chunk_metadata"]),
            doc_metadata=_as_dict(r["doc_metadata"]),
            file_name=r["file_name"],
            file_type=r["file_type"],
```

Keep the SELECT's `doc.file_name` / `doc.file_type` lines that the cherry-pick
added, and the `_as_dict` helper that `main` added. Then:

```bash
grep -c '<<<<<<<\|>>>>>>>' app/rag/retrieval.py   # expect: 0
git add app/rag/retrieval.py
```

- [ ] **Step 4: Run the citation unit tests**

```bash
.venv/bin/pytest tests/test_rag_sources.py tests/test_rag_source_presentation.py -q
```

Expected: PASS. These are pure — no Postgres, no model server.
`test_sources_never_name_a_document_that_was_trimmed_away` in
`test_rag_source_presentation.py` is the anti-fabrication guard the whole feature
rests on — if it fails, stop and fix it before anything else.

- [ ] **Step 5: Run the suites the cherry-pick touched**

```bash
.venv/bin/pytest tests/test_search_department_docs.py tests/test_rag_sources_persistence.py \
                 tests/test_rag_document_download.py -q
```

Expected: PASS, or SKIP where Postgres is unavailable. A skip is not a pass —
record which ones skipped. If `test_search_department_docs.py` fails, the NRB
citation-header tests (§29) and v1's `_format` change disagree: fix by keeping
`_nrb_provenance` in `_header` **and** v1's `(text, presented)` return.

- [ ] **Step 6: Run the whole suite**

```bash
.venv/bin/pytest -q 2>&1 | tail -20
```

Expected: no new failures versus `main`. Note the baseline first if unsure:
`git stash && .venv/bin/pytest -q 2>&1 | tail -3 && git stash pop`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(rag): structured source citations + document download (cherry-pick)

Cherry-picks 2779867 from the deferred feat/rag-source-citations branch.
Only conflict was app/rag/retrieval.py, where main's §29 metadata columns
and v1's file_name/file_type both wanted the dataclass tail — both kept.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Rebase the citations migration onto the NRB head

The migration arrived as a **sibling** of the NRB chain (both fork from
`c33c0fd56028`). §27.4 of `docs/nrb-integration.md` prescribes re-pointing it at
the current head so the graph stays one linear line — no merge revision, ever. The
revision id is kept, because the dev database is stamped at it.

**Files:**
- Modify: `alembic/versions/d4a91f2c7b3e_add_chat_message_sources.py`
- Create: `tests/test_alembic_lineage.py`

**Interfaces:**
- Consumes: the file created in Task 1.
- Produces: a single Alembic head at `d4a91f2c7b3e`, whose ancestor chain contains
  all seven NRB revisions.

- [ ] **Step 1: Write the failing test**

```python
"""The migration graph must stay a single line.

Citations forked from `c33c0fd56028` at the same time as the NRB chain and was
deferred (§27). Un-deferring it means turning the sibling into a DESCENDANT of
the NRB head — never a merge revision, which would make the graph a diamond that
every future branch has to reason about.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
NRB_HEAD = "f4c1a90b7d62"
CITATIONS = "d4a91f2c7b3e"


def _scripts() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_there_is_exactly_one_head():
    assert len(_scripts().get_heads()) == 1


def test_citations_is_the_head_and_sits_on_the_nrb_chain():
    scripts = _scripts()
    assert scripts.get_heads() == (CITATIONS,)
    assert scripts.get_revision(CITATIONS).down_revision == NRB_HEAD


def test_every_nrb_revision_is_an_ancestor_of_citations():
    """A database upgraded to head gets the NRB schema AND the sources column."""
    scripts = _scripts()
    chain = {rev.revision for rev in scripts.iterate_revisions(CITATIONS, "base")}
    assert NRB_HEAD in chain
    assert "c33c0fd56028" in chain
```

- [ ] **Step 2: Run it to verify it fails**

```bash
.venv/bin/pytest tests/test_alembic_lineage.py -q
```

Expected: FAIL — two heads, and `down_revision` is `c33c0fd56028`.

- [ ] **Step 3: Re-point the revision**

In `alembic/versions/d4a91f2c7b3e_add_chat_message_sources.py`:

```python
# Revises: f4c1a90b7d62
#
# Rebased 2026-08-19 per docs/nrb-integration.md §27.4: this revision was
# authored as a SIBLING of the NRB chain (both off c33c0fd56028) while citations
# was deferred. Un-deferring it makes it a descendant instead, so the graph stays
# one linear head and no merge revision is ever needed. The revision ID is
# deliberately unchanged — a database is stamped at it.

down_revision: Union[str, None] = "f4c1a90b7d62"
```

Also update the `Revises:` line in the module docstring so the file does not
contradict itself.

- [ ] **Step 4: Run the test and the offline resolution**

```bash
.venv/bin/pytest tests/test_alembic_lineage.py -q
.venv/bin/alembic heads
```

Expected: PASS, and `alembic heads` prints exactly one revision (`d4a91f2c7b3e`).

- [ ] **Step 5: Prove `base → head` resolves offline, touching no database**

```bash
DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:5432/none" \
  .venv/bin/alembic upgrade head --sql 2>&1 | grep -c "^-- Running upgrade"
```

Expected: `13` (main's 5 + NRB's 7 + citations). Exit status 0. This is the same
check §27.3 ran for the NRB merge; it renders SQL and connects to nothing.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/d4a91f2c7b3e_add_chat_message_sources.py tests/test_alembic_lineage.py
git commit -m "fix(alembic): rebase d4a91f2c7b3e onto the NRB head (§27.4)

Citations was authored as a sibling of the NRB chain and deferred. Turning it
into a descendant keeps a single linear head with no merge revision. Revision id
unchanged — a database is stamped at it. Locked by tests/test_alembic_lineage.py.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Reconcile the dev database — GATED, DESTRUCTIVE, ASK FIRST

**STOP. Do not run any command in this task until the user has said go for this
specific task.** It drops a column and rewrites an Alembic stamp. `CLAUDE.md` and
`AGENTS.md` both forbid doing either on a whim; §27.4 authorizes it here, once,
for the citations owner — which is this work.

Rebasing fixed the graph, not the stamped database. `local_ai_gateway` is stamped
at `d4a91f2c7b3e` and already has `chat_messages.sources`, but holds **no NRB
schema**. Alembic now believes that database is at head and would apply nothing —
leaving the seven NRB revisions permanently unapplied and `/v1/nrb/*` broken
against it. `local_ai_gateway_p4` (the NRB scratch DB) is not touched by any of
this.

**Files:** none. This task changes a development database only.

- [ ] **Step 1: Record the starting state**

```bash
.venv/bin/alembic current
psql -h 127.0.0.1 -U postgres -d local_ai_gateway -c "\d chat_messages" | grep sources
psql -h 127.0.0.1 -U postgres -d local_ai_gateway -c "\dt nrb_*"
```

Expected: `d4a91f2c7b3e`, a `sources` column present, and **no** `nrb_*` tables.
If any `nrb_*` table exists, STOP — the premise is wrong, re-derive before acting.

- [ ] **Step 2: Ask the user for explicit approval, quoting Step 1's output**

Say plainly: this drops `chat_messages.sources` (development chat data only) and
re-stamps the database, then replays 8 migrations. Wait for a yes.

- [ ] **Step 3: Drop the column and reset the stamp**

```bash
psql -h 127.0.0.1 -U postgres -d local_ai_gateway \
  -c "ALTER TABLE chat_messages DROP COLUMN sources;"
.venv/bin/alembic stamp c33c0fd56028
```

- [ ] **Step 4: Upgrade**

```bash
.venv/bin/alembic upgrade head
```

Expected: 8 revisions applied (7 NRB + citations), exit 0.

- [ ] **Step 5: Verify**

```bash
.venv/bin/alembic current                                  # d4a91f2c7b3e (head)
.venv/bin/alembic heads                                    # exactly one
psql -h 127.0.0.1 -U postgres -d local_ai_gateway -c "\d chat_messages" | grep sources
psql -h 127.0.0.1 -U postgres -d local_ai_gateway -c "\dt nrb_*" | wc -l
```

Expected: head reached, `sources` back as `jsonb`, and the `nrb_*` tables present.

- [ ] **Step 6: Report, do not commit**

Nothing to commit — this task produced no file changes. Report exactly what ran
and what the verification printed.

---

## Task 4: Anchor `RAG_DOCS_DIR` to the repository root

`rag_docs_dir` defaults to the relative `"rag_documents"` and is resolved with the
process's CWD. Two processes share it (the API writes, the worker reads), and this
work adds a third reader — the download route. `filestore.base_dir()` already
anchors the NRB tree to the repo root for exactly this reason; the corpus tree
should not be the odd one out. A CWD-relative read is the §18 defect class: the
call "succeeds", nothing is served, no health check notices.

**Files:**
- Modify: `app/config.py`, `app/rag/router.py`, `app/rag/worker.py`
- Test: `tests/test_config_paths.py` (lift from commit `a60f08b`)

**Interfaces:**
- Produces: `app.config.PROJECT_ROOT`, `Settings.rag_docs_base -> str` (absolute).
- Consumed by: Task 6's `_document_path`.

- [ ] **Step 1: Bring in the failing test**

```bash
git checkout a60f08b -- tests/test_config_paths.py
```

It asserts four things: a relative value anchors to `PROJECT_ROOT`, an absolute
value passes through, the answer is independent of `os.chdir`, and the result is
absolute.

- [ ] **Step 2: Run it to verify it fails**

```bash
.venv/bin/pytest tests/test_config_paths.py -q
```

Expected: FAIL — `ImportError: cannot import name 'PROJECT_ROOT' from 'app.config'`.

- [ ] **Step 3: Implement**

At the top of `app/config.py`, after the `functools` import:

```python
from pathlib import Path

# Repo root (app/config.py -> app -> repo). Used to anchor relative paths so they
# do not depend on a process's working directory — the API, the ingest worker and
# the download route are separate readers that must resolve the corpus to the
# SAME place. `app/nrb/filestore.base_dir()` does this for the NRB tree already.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
```

And, beside the other `@property` helpers on `Settings`:

```python
    @property
    def rag_docs_base(self) -> str:
        """Absolute corpus directory. A relative RAG_DOCS_DIR is anchored to the
        PROJECT ROOT, never the process CWD, so every process resolves the same
        tree no matter how it was launched. An absolute value is used verbatim."""
        path = Path(self.rag_docs_dir)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)
```

- [ ] **Step 4: Run the test**

```bash
.venv/bin/pytest tests/test_config_paths.py -q
```

Expected: PASS (4 tests).

- [ ] **Step 5: Switch every call site**

Replace `settings.rag_docs_dir` with `settings.rag_docs_base` in
`app/rag/router.py` (upload, typed-text, both compensation deletes, `_accept`, and
the download route from Task 1) and in `app/rag/worker.py`'s
`resolve_storage_path(...)` call. Verify none are left:

```bash
grep -rn "rag_docs_dir" app/ | grep -v "rag_docs_dir:" | grep -v rag_docs_base
```

Expected: only `app/config.py`'s field declaration.

- [ ] **Step 6: Run the RAG suites**

```bash
.venv/bin/pytest tests/test_rag_document_download.py tests/test_config_paths.py -q
.venv/bin/pytest -q 2>&1 | tail -5
```

Expected: PASS or documented skips; no new failures.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/rag/router.py app/rag/worker.py tests/test_config_paths.py
git commit -m "fix(config): anchor a relative RAG_DOCS_DIR to the repo root

Three processes now read the corpus tree (API, worker, download route) and a
CWD-relative resolve makes them disagree silently. Mirrors filestore.base_dir().

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: NRB provenance on the citation payload

v1 predates NRB entirely. Its `SourceOut` cannot distinguish a trusted internal
upload from a page whose text a machine reconstructed and no Nepali reader has
verified (§15, §16.6, §17.6). §29 already refuses to hide that from the **model**;
the UI must not undo it.

**Files:**
- Modify: `app/rag/sources.py`, `app/rag/retrieval.py`,
  `app/tools/local/search_department_docs.py`, `app/chat/schemas.py`
- Test: `tests/test_rag_sources_nrb.py` (create),
  `tests/test_search_department_docs.py` (add one test)

**Interfaces:**
- Consumes: `SourceChunk`, `_document_sources`, `resolve_sources` (Task 1);
  `RetrievedChunk.chunk_metadata` / `.doc_metadata` (already on `main`).
- Produces: `sources.VERIFY_NOTE`, `sources.RECOVERED_ROUTES`, `sources.NRB_ORIGIN`,
  `SourceChunk(origin=, route=, authoritative=, source_url=, published_at=)`,
  `RetrievedChunk.doc_source`, and the NRB keys on each source dict:
  `origin`, `source_url`, `published_at`, `routes`, `machine_recovered`,
  `verify_note`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rag_sources_nrb.py`:

```python
"""An NRB citation must carry its extraction route, and say so when the text was
machine-recovered.

The route is the only thing that tells a reader whether a figure came from a
trustworthy text layer, from PP-OCRv5 (explicitly `authoritative: false`, §16.6)
or from a legacy-font conversion that no Nepali reader has checked (§15). Native
NRB text gets the route WITHOUT a caveat — over-warning trains a reader to ignore
the warning.
"""

from app.rag.sources import (
    RECOVERED_ROUTES,
    VERIFY_NOTE,
    SearchRecord,
    SourceChunk,
    resolve_sources,
)


def nrb_chunk(route, *, page=1, authoritative=None, document_id="nrb1"):
    return SourceChunk(
        document_id=document_id,
        title="Unified Directive 2081",
        file_name="directive.pdf",
        file_type="pdf",
        page_number=page,
        origin="nrb",
        route=route,
        authoritative=authoritative,
        source_url="https://www.nrb.org.np/circular/directive-2081/",
        published_at="2024-05-02",
    )


def upload_chunk(document_id="u1", page=3):
    return SourceChunk(
        document_id=document_id,
        title="Leave Policy",
        file_name="leave.pdf",
        file_type="pdf",
        page_number=page,
        origin="upload",
    )


def one(records_chunks, answer="see [1]"):
    sources = resolve_sources(
        [SearchRecord(department_code="nrb", chunks=records_chunks)], answer
    )
    assert sources is not None
    return sources


def test_ocr_page_is_flagged_machine_recovered_with_the_verify_note():
    source = one([nrb_chunk("ocr", authoritative=False)])[0]
    assert source["origin"] == "nrb"
    assert source["routes"] == ["ocr"]
    assert source["machine_recovered"] is True
    assert source["verify_note"] == VERIFY_NOTE
    assert source["source_url"] == "https://www.nrb.org.np/circular/directive-2081/"
    assert source["published_at"] == "2024-05-02"


def test_legacy_conversion_is_also_machine_recovered():
    source = one([nrb_chunk("legacy_conversion")])[0]
    assert source["machine_recovered"] is True
    assert source["verify_note"] == VERIFY_NOTE


def test_native_nrb_text_carries_the_route_without_the_caveat():
    source = one([nrb_chunk("native")])[0]
    assert source["routes"] == ["native"]
    assert source["machine_recovered"] is False
    assert source["verify_note"] is None


def test_routes_are_the_union_over_the_documents_presented_pages():
    """One NRB PDF is routed per PAGE, so a single document really can mix."""
    source = one(
        [nrb_chunk("native", page=1), nrb_chunk("ocr", page=2, authoritative=False)],
        answer="see [1] and [2]",
    )[0]
    assert source["pages"] == [1, 2]
    assert source["routes"] == ["native", "ocr"]
    assert source["machine_recovered"] is True


def test_a_generic_upload_carries_no_nrb_keys():
    source = one([upload_chunk()])[0]
    assert source["origin"] == "upload"
    for absent in ("source_url", "published_at", "routes", "machine_recovered",
                   "verify_note"):
        assert absent not in source


def test_a_mixed_turn_produces_both_shapes():
    sources = one(
        [nrb_chunk("ocr", authoritative=False), upload_chunk()],
        answer="[1] and [2]",
    )
    by_id = {s["document_id"]: s for s in sources}
    assert by_id["nrb1"]["machine_recovered"] is True
    assert "machine_recovered" not in by_id["u1"]


def test_recovered_routes_is_the_set_the_tool_also_uses():
    assert RECOVERED_ROUTES == frozenset({"ocr", "legacy_conversion"})
```

Add to `tests/test_search_department_docs.py`:

```python
def test_the_caveat_is_one_constant_with_two_readers():
    """The model's context and the API's `verify_note` must never drift apart."""
    from app.rag import sources as rag_sources
    from app.tools.local import search_department_docs as tool

    assert tool._VERIFY is rag_sources.VERIFY_NOTE
    assert tool._RECOVERED_ROUTES is rag_sources.RECOVERED_ROUTES
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_rag_sources_nrb.py -q
```

Expected: FAIL — `ImportError: cannot import name 'VERIFY_NOTE'`.

- [ ] **Step 3: Move the caveat vocabulary into `app/rag/sources.py`**

```python
# The vocabulary of a machine-recovered citation, defined ONCE and read twice:
# `search_department_docs` renders it into the model's context, and the chat API
# publishes it as `verify_note`. Two copies of this sentence would drift, and a
# UI badge that disagreed with the answer text is worse than neither.
NRB_ORIGIN = "nrb"
RECOVERED_ROUTES = frozenset({"ocr", "legacy_conversion"})
VERIFY_NOTE = "machine-recovered — VERIFY figures, dates and names against the source"
```

In `app/tools/local/search_department_docs.py`, delete the local `_VERIFY` and
`_RECOVERED_ROUTES` definitions and import them instead (keeping the names, so
`_nrb_provenance` is untouched):

```python
from ...rag.sources import (
    RECOVERED_ROUTES as _RECOVERED_ROUTES,
    VERIFY_NOTE as _VERIFY,
    SourceChunk,
    record_search,
)
```

- [ ] **Step 4: Widen `SourceChunk`**

```python
@dataclass(frozen=True)
class SourceChunk:
    document_id: str
    title: str
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    page_number: Optional[int] = None
    # Provenance, carried opaquely from the chunk's and document's metadata.
    # `origin` is "nrb" for a catalog document, else the document's own source
    # ("upload"/"manual"). The rest are NRB-only and stay None elsewhere.
    origin: Optional[str] = None
    route: Optional[str] = None
    authoritative: Optional[bool] = None
    source_url: Optional[str] = None
    published_at: Optional[str] = None
```

- [ ] **Step 5: Roll provenance up per document**

In `_document_sources`, when creating an entry:

```python
            entry = {
                "document_id": chunk.document_id,
                "title": chunk.title,
                "department_code": department_code,
                "file_name": chunk.file_name,
                "file_type": chunk.file_type,
                "pages": [],
                "cited": cited,
                "origin": chunk.origin,
            }
            if chunk.origin == NRB_ORIGIN:
                # NRB-only keys. Absent (not null) for an ordinary upload, so a
                # client can tell "not an NRB document" from "NRB, route unknown".
                entry["source_url"] = chunk.source_url
                entry["published_at"] = chunk.published_at
                entry["routes"] = []
                entry["machine_recovered"] = False
                entry["verify_note"] = None
            by_document[chunk.document_id] = entry
```

and, for every chunk (new or existing entry), after the `pages` accumulation:

```python
        if chunk.origin == NRB_ORIGIN:
            # A single NRB PDF is routed per PAGE (§16), so one document can mix
            # native text, a legacy-font conversion and OCR. Report the union.
            if chunk.route and chunk.route not in entry["routes"]:
                entry["routes"].append(chunk.route)
            if chunk.route in RECOVERED_ROUTES or chunk.authoritative is False:
                entry["machine_recovered"] = True
```

and in the finalizing loop:

```python
    for entry in by_document.values():
        entry["pages"].sort()
        if entry.get("machine_recovered"):
            entry["verify_note"] = VERIFY_NOTE
        if "routes" in entry:
            entry["routes"].sort()
```

- [ ] **Step 6: Carry `documents.source` through retrieval**

In `app/rag/retrieval.py`'s SELECT, beside `doc.file_name` / `doc.file_type`:

```sql
       doc.source      AS doc_source,
```

On the dataclass, beside `file_name`/`file_type`:

```python
    # `documents.source` ("upload"/"manual"). Only a citation reads it — it is
    # what `origin` falls back to when a document is not from the NRB catalog.
    doc_source: str | None = None
```

and in the row mapping: `doc_source=r["doc_source"],`.

- [ ] **Step 7: Populate the new fields in the tool**

In `app/tools/local/search_department_docs.py`, replace the inline `SourceChunk`
comprehension in `_search_department_docs` with a named builder:

```python
def _source_chunk(chunk: RetrievedChunk) -> SourceChunk:
    """The citation's view of a retrieved passage.

    Reads the SAME metadata `_nrb_provenance` renders for the model, so the two
    can never describe a passage differently: the route and trust flag are the
    CHUNK's (per page), the URL and date are the DOCUMENT's.
    """
    cm = chunk.chunk_metadata or {}
    dm = chunk.doc_metadata or {}
    origin = cm.get("origin") or dm.get("origin") or chunk.doc_source
    return SourceChunk(
        document_id=chunk.document_id,
        title=chunk.title,
        file_name=chunk.file_name,
        file_type=chunk.file_type,
        page_number=chunk.page_number,
        origin=origin,
        route=cm.get("route"),
        authoritative=cm.get("authoritative"),
        source_url=dm.get("page_url") or dm.get("source_url"),
        published_at=dm.get("published_at"),
    )
```

and call it: `record_search(department.code, [_source_chunk(c) for c in presented])`.

- [ ] **Step 8: Widen `SourceOut`**

In `app/chat/schemas.py`, on `SourceOut`:

```python
    # Where the document came from: "nrb" for a catalog document, else the
    # document's own source ("upload"/"manual").
    origin: Optional[str] = None
    # NRB-only. `routes` is the union over the pages presented to the model, and
    # `machine_recovered` is true when any of them was OCR'd or converted from a
    # legacy font — text that is retrieval-grade but NOT authoritative for a
    # figure, date or name. `verify_note` carries the wording the model was shown.
    source_url: Optional[str] = None
    published_at: Optional[str] = None
    routes: Optional[list[str]] = None
    machine_recovered: Optional[bool] = None
    verify_note: Optional[str] = None
```

- [ ] **Step 9: Run the tests**

```bash
.venv/bin/pytest tests/test_rag_sources_nrb.py tests/test_rag_sources.py \
                 tests/test_search_department_docs.py -q
```

Expected: PASS. If `test_a_generic_upload_carries_no_nrb_keys` fails, an NRB key
is being written unconditionally — it must be inside the `origin == NRB_ORIGIN`
branch.

- [ ] **Step 10: Commit**

```bash
git add app/rag/sources.py app/rag/retrieval.py app/chat/schemas.py \
        app/tools/local/search_department_docs.py \
        tests/test_rag_sources_nrb.py tests/test_search_department_docs.py
git commit -m "feat(rag): NRB provenance on structured citations

Route, machine-recovered flag, verify note, NRB source URL and published date
on each source — the same facts §29 renders for the model, from one constant
with two readers so the badge and the answer cannot drift.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Serve NRB bytes from the filestore in the download route

v1 resolves every document under `RAG_DOCS_DIR`. Since §28 an NRB document has
**no copy** there — its bytes live content-addressed in the NRB filestore, and
`worker._document_path` reconstructs the key from the content hash rather than
trusting `storage_key` (so rows minted under the old copy scheme still resolve).
Without this branch every NRB citation's download 404s.

**Files:**
- Modify: `app/rag/router.py`
- Test: `tests/test_rag_document_download_nrb.py` (create)

**Interfaces:**
- Consumes: `Settings.rag_docs_base` (Task 4), the download route (Task 1).
- Produces: `app.rag.router._document_path(doc, settings) -> Path`.

- [ ] **Step 1: Write the failing test**

```python
"""An NRB citation's download must read the NRB filestore, not RAG_DOCS_DIR.

§28 removed the per-corpus copy: an NRB document's bytes exist once, content-
addressed under NRB_FILES_DIR, and `documents.storage_key` holds the FILESTORE
key. Resolving that key under RAG_DOCS_DIR yields a path that does not exist, so
the route would 404 every NRB source while reporting the document as ready.
"""

import hashlib
from pathlib import Path

from app.rag.router import _document_path


class FakeDoc:
    def __init__(self, **kw):
        self.storage_key = kw.get("storage_key")
        self.file_type = kw.get("file_type", "pdf")
        self.content_hash = kw.get("content_hash", "")
        self.meta = kw.get("meta", {})


class FakeSettings:
    def __init__(self, docs_base):
        self.rag_docs_base = str(docs_base)


def test_an_nrb_document_resolves_into_the_filestore(tmp_path, monkeypatch):
    payload = b"%PDF-1.4 nrb"
    digest = hashlib.sha256(payload).hexdigest()

    from app.nrb import filestore

    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path / "nrb_files")
    blob = tmp_path / "nrb_files" / digest[:2] / f"{digest}.pdf"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)

    doc = FakeDoc(
        storage_key=f"{digest[:2]}/{digest}.pdf",
        content_hash=digest,
        meta={"origin": "nrb"},
    )
    assert _document_path(doc, FakeSettings(tmp_path / "rag_documents")) == blob


def test_an_nrb_row_minted_under_the_old_copy_scheme_still_resolves(tmp_path, monkeypatch):
    """The key is RECONSTRUCTED from the hash, so a legacy RAG_DOCS_DIR-style
    storage_key on an NRB row is ignored rather than followed."""
    payload = b"%PDF-1.4 legacy row"
    digest = hashlib.sha256(payload).hexdigest()

    from app.nrb import filestore

    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path / "nrb_files")
    blob = tmp_path / "nrb_files" / digest[:2] / f"{digest}.pdf"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)

    doc = FakeDoc(
        storage_key="nrb-p7/0123456789abcdef.pdf",   # the old copy scheme
        content_hash=digest,
        meta={"origin": "nrb"},
    )
    assert _document_path(doc, FakeSettings(tmp_path / "rag_documents")) == blob


def test_an_ordinary_upload_still_resolves_under_the_corpus_tree(tmp_path):
    doc = FakeDoc(storage_key="hr/abc.pdf", meta={})
    docs_base = tmp_path / "rag_documents"
    assert _document_path(doc, FakeSettings(docs_base)) == docs_base / "hr" / "abc.pdf"


def test_an_nrb_document_falls_back_to_the_metadata_blob_hash(tmp_path, monkeypatch):
    """`worker._document_path` accepts either; so must this, or a row whose
    content_hash was never backfilled becomes unservable."""
    payload = b"%PDF-1.4 meta hash"
    digest = hashlib.sha256(payload).hexdigest()

    from app.nrb import filestore

    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path / "nrb_files")
    blob = tmp_path / "nrb_files" / digest[:2] / f"{digest}.pdf"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)

    doc = FakeDoc(content_hash="", meta={"origin": "nrb", "blob_sha256": digest})
    assert _document_path(doc, FakeSettings(tmp_path / "rag_documents")) == blob
```

- [ ] **Step 2: Run it to verify it fails**

```bash
.venv/bin/pytest tests/test_rag_document_download_nrb.py -q
```

Expected: FAIL — `ImportError: cannot import name '_document_path'`.

- [ ] **Step 3: Implement `_document_path` in `app/rag/router.py`**

```python
def _document_path(doc, settings) -> Path:
    """Where this document's bytes actually live.

    Two trees, because §28 removed the per-corpus copy: an NRB document's bytes
    exist once, content-addressed under NRB_FILES_DIR, and everything else lives
    under RAG_DOCS_DIR. The NRB key is RECONSTRUCTED from the content hash rather
    than read from `storage_key`, exactly as `worker._document_path` does — a row
    minted under the old copy scheme carries a RAG_DOCS_DIR-style key that no
    longer points at anything, and following it would 404 a document that is
    present on disk.

    The import is local so the module graph stays honest about what the API
    loads; `app.nrb.filestore` is stdlib + config only and pulls in no worker
    dependency.
    """
    if (doc.meta or {}).get("origin") == "nrb":
        from ..nrb import filestore

        digest = doc.content_hash or str((doc.meta or {}).get("blob_sha256") or "")
        return filestore.resolve_path(
            filestore.storage_key_for(digest, doc.file_type)
        )
    if not doc.storage_key:
        raise StorageError("document has no storage_key")
    return resolve_storage_path(doc.storage_key, settings.rag_docs_base)
```

Add `from pathlib import Path` if it is not already imported.

- [ ] **Step 4: Use it in the route**

Replace the route's `resolve_storage_path(...)` block with:

```python
    settings = get_settings()
    try:
        path = _document_path(doc, settings)
    except Exception as exc:  # StorageError, FileStoreError, or a bad digest
        # The key round-tripped through the database, so it is untrusted on the
        # way back: a traversal attempt or a malformed hash is a 404 to the
        # caller and never a served file.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown document"
        ) from exc
```

Keep the existing `if not path.is_file(): 404 "Document file is missing"` check —
a row whose bytes were removed out of band is genuinely nothing to serve, not a 500.
The `if not doc.storage_key` pre-check that Task 1 brought in must move **below**
this, or be deleted: an NRB row is valid without a usable `storage_key`.

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/pytest tests/test_rag_document_download_nrb.py tests/test_rag_document_download.py -q
```

Expected: PASS (integration cases may SKIP without Postgres — say so if they do).

- [ ] **Step 6: Verify the API import graph is still clean**

```bash
.venv/bin/python -c "
import sys, app.main
bad = [m for m in ('docling','torch','transformers') if m in sys.modules]
print('leaked:', bad); assert not bad
"
```

Expected: `leaked: []`.

- [ ] **Step 7: Commit**

```bash
git add app/rag/router.py tests/test_rag_document_download_nrb.py
git commit -m "fix(rag): resolve NRB citation downloads from the filestore

§28 removed the RAG_DOCS_DIR copy, so an NRB document's bytes are only in the
content-addressed filestore. Key is reconstructed from the content hash, as
worker._document_path does, so old copy-scheme rows still resolve.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Make the NRB blob tree reachable in Compose

Found while checking Task 6, and pre-existing: `NRB_FILES_DIR` is in neither
`.env.docker` nor `.env.docker.example`, and no `nrb_files` volume is mounted. In
containers it therefore defaults to `/app/nrb_files` — unshared between
`nrb-runner` (which downloads) and `worker` (which reads), and invisible to
`gateway` (which must now serve it). This is §28 follow-through: removing the
`RAG_DOCS_DIR` copy also removed what had been masking it.

**Files:**
- Modify: `docker-compose.yml`, `.env.docker.example`, `.env.example`
- Test: `tests/test_env_templates.py` (lift from commit `a60f08b`),
  `tests/test_compose_volumes.py` (create)

- [ ] **Step 1: Bring in the env-template test and write the compose test**

```bash
git checkout a60f08b -- tests/test_env_templates.py
```

Create `tests/test_compose_volumes.py`:

```python
"""Every process that touches a shared tree must mount the same volume.

The NRB blob store is written by `nrb-runner`, read by `worker` during ingest and
now read by `gateway` to serve a citation's download. A container-local directory
would give each of them a private, empty copy — and the failure is silent: the
runner reports a successful fetch, the worker records "blob missing", the gateway
404s a document it lists as ready.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
SHARING = ("gateway", "worker", "nrb-runner")


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_the_nrb_files_volume_is_declared():
    assert "nrb_files" in compose()["volumes"]


def test_every_nrb_touching_service_mounts_it():
    services = compose()["services"]
    for name in SHARING:
        mounts = [m.split(":")[0] for m in services[name].get("volumes", [])]
        assert "nrb_files" in mounts, f"{name} does not mount nrb_files"


def test_they_all_mount_it_at_the_same_path():
    services = compose()["services"]
    targets = {
        m.split(":")[1]
        for name in SHARING
        for m in services[name].get("volumes", [])
        if m.startswith("nrb_files:")
    }
    assert targets == {"/app/nrb_files"}
```

- [ ] **Step 2: Run both to verify they fail**

```bash
.venv/bin/pytest tests/test_compose_volumes.py tests/test_env_templates.py -q
```

Expected: `test_compose_volumes.py` all FAIL. `test_env_templates.py` fails with
two pre-existing drifts, both real:
`settings exist in app/config.py but are absent from .env.example: ['RAG_CANDIDATE_POOL', 'RAG_CHUNK_MIN_BODY_CHARS', 'RAG_HNSW_EF_SEARCH', 'RAG_MAX_QUERY_CHARS', 'RAG_RELEVANCE_THRESHOLD', 'RAG_RERANK_ENABLED', 'RAG_RERANK_MODEL', 'RAG_RERANK_POOL', 'RAG_RRF_K', 'RAG_SKIP_SECTIONS', 'RAG_TOOL_RESULT_MAX_CHARS', 'RAG_TOP_K']`
and `.env.example sets keys that are not app/config.py settings: ['INSTALL_OCR']`.
If `yaml` is missing, install PyYAML into this venv and add it to
`requirements.txt` under a comment saying it is test-only tooling.

- [ ] **Step 3: Add the volume to `docker-compose.yml`**

Add `- nrb_files:/app/nrb_files` to the `volumes:` list of `gateway`, `worker` and
`nrb-runner`, each with a comment saying why that service needs it, and add
`nrb_files:` to the top-level `volumes:` block:

```yaml
volumes:
  gateway_files:
  rag_documents:
  # The NRB blob store. Written by nrb-runner (Phase 5 fetch), read by worker
  # (recovery + ingest) and by gateway (serving a citation's download). Since
  # §28 there is no RAG_DOCS_DIR copy, so this volume IS the corpus's bytes.
  nrb_files:
  worker_cache:
```

- [ ] **Step 4: Document the setting in both templates**

Add to `.env.docker.example` and `.env.docker`:

```bash
# Content-addressed NRB blob store. Must match the nrb_files volume's mount
# point in docker-compose.yml — nrb-runner writes it, worker and gateway read it.
NRB_FILES_DIR=nrb_files
```

Add the 12 undocumented RAG settings Step 2 named to `.env.example` with their
current defaults from `app/config.py`, and add `INSTALL_OCR` to
`test_env_templates.py`'s `FOREIGN_KEYS` with a one-line justification (it is a
Docker **build** arg, not a `Settings` field).

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/pytest tests/test_compose_volumes.py tests/test_env_templates.py -q
```

Expected: PASS.

- [ ] **Step 6: Validate the compose file parses**

```bash
docker compose config >/dev/null && echo OK
```

Expected: `OK`. If Docker is unavailable here, say so explicitly rather than
implying the stack was exercised.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml .env.docker .env.docker.example .env.example \
        tests/test_compose_volumes.py tests/test_env_templates.py
git commit -m "fix(deploy): share the NRB blob store across the three containers

§28 follow-through: with no RAG_DOCS_DIR copy, nrb-runner, worker and gateway
must mount one nrb_files volume or each gets a private empty tree and fails
silently. Also documents 12 RAG settings missing from .env.example.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: The citation eval set

Per the project's Evaluation & Improvement standard, the feature ships with a
small labelled set and a score. It is deliberately pure — no Postgres, no model
server — so it runs in CI and answers one question: does resolution ever name a
document that was not in the model's context?

**Files:**
- Test: `tests/test_citation_eval.py` (create)

- [ ] **Step 1: Write the eval**

```python
"""Citation resolution eval — 10 labelled turns.

Success metric: every RAG-grounded answer returns at least one source, and NO
source names a document the model was not shown. The second half is the one that
matters — fabricated provenance is worse than absent provenance, because a link
makes an answer look checked.
"""

import pytest

from app.rag.sources import SearchRecord, SourceChunk, resolve_sources


def chunk(doc_id, page=1, **kw):
    return SourceChunk(
        document_id=doc_id, title=f"Doc {doc_id}", file_name=f"{doc_id}.pdf",
        file_type="pdf", page_number=page, origin=kw.pop("origin", "upload"), **kw
    )


NRB = dict(origin="nrb", source_url="https://www.nrb.org.np/x/", published_at="2024-01-01")

CASES = [
    # (name, records, answer, expected_document_ids, expected_cited)
    ("single search, one marker",
     [SearchRecord("hr", [chunk("a"), chunk("b")])], "per [1]", ["a"], True),
    ("single search, two markers",
     [SearchRecord("hr", [chunk("a"), chunk("b")])], "[1] and [2]", ["a", "b"], True),
    ("single search, no markers",
     [SearchRecord("hr", [chunk("a"), chunk("b")])], "no citation", ["a", "b"], False),
    ("out-of-range marker is dropped",
     [SearchRecord("hr", [chunk("a")])], "see [9]", ["a"], False),
    ("a year in the text is not a citation",
     [SearchRecord("hr", [chunk("a")])], "in [2024] we", ["a"], False),
    ("two searches: numbering is ambiguous, nothing is claimed as cited",
     [SearchRecord("hr", [chunk("a")]), SearchRecord("hr", [chunk("b")])],
     "[1]", ["a", "b"], False),
    ("same document on two pages collapses to one source",
     [SearchRecord("hr", [chunk("a", page=2), chunk("a", page=5)])],
     "[1] and [2]", ["a"], True),
    ("nrb ocr page",
     [SearchRecord("nrb", [chunk("n1", route="ocr", authoritative=False, **NRB)])],
     "[1]", ["n1"], True),
    ("nrb legacy conversion",
     [SearchRecord("nrb", [chunk("n2", route="legacy_conversion", **NRB)])],
     "[1]", ["n2"], True),
    ("nrb native",
     [SearchRecord("nrb", [chunk("n3", route="native", **NRB)])],
     "[1]", ["n3"], True),
]


@pytest.mark.parametrize("name,records,answer,expected_ids,expected_cited",
                         CASES, ids=[c[0] for c in CASES])
def test_case(name, records, answer, expected_ids, expected_cited):
    sources = resolve_sources(records, answer)
    assert sources is not None
    assert [s["document_id"] for s in sources] == expected_ids
    assert all(s["cited"] is expected_cited for s in sources)


def test_a_turn_with_no_search_has_no_sources():
    """None, not [] — "searched nothing" and "found nothing" are different facts."""
    assert resolve_sources([], "hello") is None


def test_no_source_is_ever_invented():
    """The anti-fabrication invariant, over the whole eval set."""
    for _, records, answer, _, _ in CASES:
        presented = {c.document_id for r in records for c in r.chunks}
        for source in resolve_sources(records, answer) or []:
            assert source["document_id"] in presented


def test_machine_recovered_matches_the_route():
    expected = {"n1": True, "n2": True, "n3": False}
    for doc_id, flag in expected.items():
        records = [r for _, rs, _, ids, _ in CASES for r in rs if doc_id in ids]
        if not records:
            continue
        source = (resolve_sources(records, "[1]") or [])[0]
        assert source["machine_recovered"] is flag
```

- [ ] **Step 2: Run it**

```bash
.venv/bin/pytest tests/test_citation_eval.py -q
```

Expected: PASS, 14 tests. Record the pass rate in the docs update (Task 9). If a
case fails, the resolver is wrong, not the case — fix `sources.py`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_citation_eval.py
git commit -m "test(rag): 10-case citation resolution eval

Locks the anti-fabrication invariant (a source may only name a document the
model was shown) and the machine-recovered flag per route.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Documentation

The repo's engineering record is load-bearing here — several past mistakes came
from re-deriving state instead of reading it. Three documents are currently wrong
about citations, and the frontend contract does not mention them at all.

**Files:**
- Modify: `CLAUDE.md`, `AGENTS.md`, `README.md`,
  `docs/frontend-sync-prompt.md`, `docs/nrb-integration.md`

- [ ] **Step 1: `docs/frontend-sync-prompt.md` — the contract the UI reads**

In the contract block, extend the `stream=false` response and the `done` event with
`"sources": null | [ … ]`, document `SourceOut`'s fields (including the NRB-only
ones and what `machine_recovered` obliges the UI to show), add `sources` to
`GET /v1/sessions/{id}`'s message shape, and add:

```
DEPARTMENT DOCUMENT DOWNLOAD (authed):
  GET /v1/departments/{code}/documents/{document_id}/download -> raw bytes
      403 no grant for the department; 404 unknown/not-ready/foreign document
      GOTCHA: same as /v1/files/{id} — behind JWT, so an <a href> cannot fetch it.
      Use the Authorization header, take the blob, make a blob: URL.
      A source with machine_recovered=true MUST render its verify_note; that text
      is not decoration, it is the only signal that a figure came from OCR or a
      legacy-font conversion that no human has checked.
```

- [ ] **Step 2: `CLAUDE.md` — endpoints and a gotcha**

Add the download route to the authed endpoint list; add `sources` to the
`POST /v1/chat` response description; and add a gotcha paragraph covering: the
collector is a contextvar with the same streaming rule as `file_sink`; only
budget-surviving passages may be cited; multi-search turns cannot attribute `[N]`;
`sources` is not gated by `EXPOSE_TRACE`; `download_url` is derived, never stored;
and the NRB download resolves from the filestore, not `RAG_DOCS_DIR`.

- [ ] **Step 3: `AGENTS.md` — correct the stale NRB paragraph**

It still says the recovery cache and supersession "do not exist" and that Phase 8
"is not started"; all three are done (§21, §22, §29). Fix those sentences and add
citations to the core contracts list.

- [ ] **Step 4: `README.md`**

Add the download endpoint and the `sources` field to the endpoint tables.

- [ ] **Step 5: `docs/nrb-integration.md` — a new §30**

Write "§30. Citations un-deferred — chat-level source citations (v2)" covering:
the §27.4 lineage step as executed (rebased revision, one head, the dev-DB
reconciliation and its outcome); what shipped; the filestore branch in the
download route and why v1 could not have had it; the Compose volume gap found and
fixed; and an **Evaluation & Improvement** subsection with the four standard
answers — success metric (≥1 source per grounded answer, zero fabricated),
eval (Task 8's 10 cases + observed pass rate), feedback capture
(`chat_messages.sources` as the audit log; out-of-range marker logging), and
review loop (first corpus ingest, first UI render, after §15's Nepali review).

- [ ] **Step 6: Run the full suite one last time**

```bash
.venv/bin/pytest -q 2>&1 | tail -10
```

Expected: no failures; report which suites skipped and why.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md AGENTS.md README.md docs/frontend-sync-prompt.md docs/nrb-integration.md
git commit -m "docs: chat source citations (v2) — contract, gotchas, and §30

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Not in this plan

Passage-level payloads and inline `[N]` anchor mapping; reranking or abstention;
a global cross-department NRB corpus (§29.1); the UI itself, which lives in the
separate `local-ai-model-frontend` repo. Running the NRB corpus ingest is
independent work that this feature does not block and is not blocked by.

# RAG Slice 2 — Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin uploads a PDF/DOCX/XLSX/CSV — or types text — into a department, and a **separate worker process** parses, chunks, embeds and atomically stores it as searchable `document_chunks`.

**Architecture:** The API process only writes a `documents` row plus a queued `ingest_jobs` row and returns `202`. A second process (`python -m app.rag.worker`, same repo, **separate dependency set**) claims jobs with `FOR UPDATE SKIP LOCKED`, does all the slow work outside any transaction, then commits one short atomic replacement. Docling and torch never enter the API environment.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async + asyncpg, Postgres 16 + pgvector 0.8.5, Docling (worker only), Ollama `/v1/embeddings`, pytest.

## Global Constraints

- Use **this project's venv**: `.venv/bin/python`, `.venv/bin/pip`, `.venv/bin/alembic`, `.venv/bin/pytest`. Never a sibling's.
- **Embedding dimension is exactly 1536.** Native Qwen3 output is 2560; the gateway truncates and re-normalizes. Nothing is stored without asserting `len == 1536`.
- **Docling/torch must NOT be importable from the API process's dependency set.** `requirements.txt` stays light; `requirements-worker.txt` carries the heavy tree. Any Docling import is lazy and lives only on the worker code path.
- Repository functions take an `AsyncSession` and **do not commit** — except the ingest replacement transaction, which owns its boundary by design.
- Integration tests **skip cleanly** when Postgres or Ollama is unreachable, and when Docling or the embedding model is absent. The offline suite must stay green.
- `RAG_DOCS_DIR` is separate from `FILES_DIR`; `documents.storage_key` is a **relative** key under it, never an absolute path.
- Slice 1 is **locked**. Do not modify `app/rag/models.py`, `access.py`, `context.py`, the migration, or their tests except where a step here explicitly says so.
- **Do not stage or commit unrelated working-tree changes.** The branch carries pre-existing modifications to `app/agent/loop.py`, `app/config.py`, `app/history/*`, `app/tools/local/*`, `.env.example` and several tests. `git add` only the exact files each task names. `app/config.py` IS touched by Task 1 — add only the new RAG settings and stage it deliberately, reviewing `git diff --staged app/config.py` first.

**Out of scope for this slice** (do not build): the retrieval query, the reranker, `search_department_docs`, `rag_context` wiring into `/v1/chat`, the `rag_queries`/`rag_feedback` audit tables, the eval harness, OCR for scanned PDFs, and any change to the slice-1 authorization contract.

## Environment prerequisites

Two setup tasks, not architecture decisions. Task 2 and Task 4 skip cleanly without them, but the slice is not verified until both are done:

```bash
# 1. The embedding model (native 2560 dims) — STILL OUTSTANDING
ollama pull qwen3-embedding:4b-q8_0

# 2. The worker dependency tree — ALREADY INSTALLED in the dev venv
.venv/bin/pip install -r requirements-worker.txt   # created in Task 8
```

**Docling 2.118.1 is already installed in the dev venv**, so the Docling parsing
tests run from Task 4 onward rather than skipping. It stays out of
`requirements.txt` regardless — the dependency split is about what the API
*image* carries, and `test_docling_is_not_imported_at_module_scope` plus the
post-Task-7 import-graph check are what enforce it. Having Docling importable
makes those checks stronger, not weaker: they now prove the API avoids it by
design rather than by absence.

## Measured facts this plan relies on

Verified against the live stack rather than assumed:

- `/v1/embeddings` **accepts a list `input`** and returns one object per item with an explicit `index` — batching works, and `index` is authoritative, so results must be reordered by it rather than trusted in array order.
- Ollama honours `dimensions` and returns an L2-normalized vector, but we truncate and normalize in the gateway anyway as a portability contract (see the spec).
- `qwen3-embedding:4b-q8_0` is **not currently pulled**; only `nomic-embed-text:latest` is present.
- Docling pulls **90 packages** including torch, torchvision, transformers, opencv and the full NVIDIA CUDA stack. This is why the dependency split is a constraint, not a preference.

## File structure

| File | Responsibility |
|---|---|
| `app/config.py` | +`RAG_*` settings (docs dir, embed model/dim/batch, chunking, worker timing) |
| `app/rag/storage.py` | `storage_key` minting + traversal-safe resolution under `RAG_DOCS_DIR` |
| `app/rag/embedding.py` | `embed_texts(mode=query\|document)`, truncate→1536, normalize, dimension assertion |
| `app/rag/chunking.py` | `Chunk` dataclass + text/markdown and spreadsheet chunkers (pure) |
| `app/rag/parsing.py` | file_type → `list[Chunk]`; Docling for pdf/docx (lazy import), `readers.py` for xlsx/csv |
| `app/rag/jobs.py` | Queue: enqueue, `SKIP LOCKED` claim, heartbeat, finish, stale sweep |
| `app/rag/documents.py` | `documents` data access: create-with-hash, archive, list, get |
| `app/rag/ingest.py` | `replace_chunks` atomic transaction + `run_ingest` pipeline |
| `app/rag/worker.py` | Worker entrypoint: preflight, claim loop, heartbeat |
| `app/rag/router.py` | +admin document endpoints and `GET /v1/ingest-jobs/{id}` |
| `app/rag/schemas.py` | +document/job request & response models |
| `requirements-worker.txt` | `-r requirements.txt` + Docling |

---

### Task 1: Config + document storage

**Files:**
- Modify: `app/config.py` (RAG settings only — see the staging warning above)
- Modify: `.env.example`
- Create: `app/rag/storage.py`
- Test: `tests/test_rag_storage.py`

**Interfaces:**
- Produces: `Settings.rag_docs_dir`, `.rag_embed_model`, `.rag_embed_dim`, `.rag_embed_batch`, `.rag_chunk_max_chars`, `.rag_chunk_overlap_chars`, `.rag_ingest_poll_seconds`, `.rag_ingest_stale_minutes`, `.rag_ingest_heartbeat_seconds`
- Produces: `mint_storage_key(department_code: str, filename: str) -> str`, `delete_document(storage_key: str, base_dir: str) -> bool`, `resolve_storage_path(storage_key: str, base_dir: str) -> Path`, `write_document(data: bytes, storage_key: str, base_dir: str) -> None`, `StorageError`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_storage.py`:

```python
"""storage_key minting and traversal-safe resolution. Pure — tmp dirs only.

The key is RELATIVE by design: rows stay portable across hosts and the same
value becomes an object-storage key later. Resolution therefore has to defend
against a key that tries to climb out of the base directory.
"""

from pathlib import Path

import pytest

from app.rag.storage import (
    StorageError,
    delete_document,
    mint_storage_key,
    resolve_storage_path,
    write_document,
)


def test_key_is_relative_and_under_the_department():
    key = mint_storage_key("hr", "Leave Policy.pdf")
    assert not key.startswith("/")
    assert key.startswith("hr/")
    assert key.endswith(".pdf")


def test_key_does_not_reuse_the_caller_supplied_name():
    """The on-disk name is a uuid + extension, exactly like the file store —
    no traversal or collision from a user-chosen filename."""
    key = mint_storage_key("hr", "../../etc/passwd.pdf")
    assert ".." not in key
    assert "passwd" not in key


def test_keys_are_unique_per_call():
    assert mint_storage_key("hr", "a.pdf") != mint_storage_key("hr", "a.pdf")


def test_missing_extension_falls_back_to_bin():
    assert mint_storage_key("hr", "noext").endswith(".bin")


def test_department_code_is_slugged_not_trusted():
    key = mint_storage_key("../hr", "a.pdf")
    assert not key.startswith("..")


def test_resolve_returns_a_path_under_the_base(tmp_path):
    key = mint_storage_key("hr", "a.pdf")
    resolved = resolve_storage_path(key, str(tmp_path))
    assert str(resolved).startswith(str(tmp_path))


@pytest.mark.parametrize("evil", [
    "../outside.pdf",
    "hr/../../outside.pdf",
    "/etc/passwd",
    "hr/./../../x.pdf",
])
def test_resolution_refuses_to_escape_the_base(tmp_path, evil):
    with pytest.raises(StorageError):
        resolve_storage_path(evil, str(tmp_path))


def test_write_creates_parent_directories_and_bytes(tmp_path):
    key = mint_storage_key("hr", "a.pdf")
    write_document(b"hello", key, str(tmp_path))
    assert resolve_storage_path(key, str(tmp_path)).read_bytes() == b"hello"


def test_write_refuses_an_escaping_key(tmp_path):
    with pytest.raises(StorageError):
        write_document(b"x", "../evil.pdf", str(tmp_path))


def test_delete_removes_the_file(tmp_path):
    key = mint_storage_key("hr", "a.pdf")
    write_document(b"hello", key, str(tmp_path))
    assert delete_document(key, str(tmp_path)) is True
    assert not resolve_storage_path(key, str(tmp_path)).exists()


def test_delete_is_idempotent(tmp_path):
    """Compensation runs on an error path — it must never raise and mask the
    original failure."""
    key = mint_storage_key("hr", "a.pdf")
    assert delete_document(key, str(tmp_path)) is False


def test_delete_refuses_an_escaping_key(tmp_path):
    outside = tmp_path.parent / "victim.txt"
    outside.write_text("do not delete me")
    with pytest.raises(StorageError):
        delete_document("../victim.txt", str(tmp_path))
    assert outside.exists()


def test_delete_never_raises_on_a_directory(tmp_path):
    """Defensive: a key that somehow names a directory must not blow up the
    compensation path."""
    (tmp_path / "hr").mkdir()
    assert delete_document("hr", str(tmp_path)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.storage'`

- [ ] **Step 3: Add the settings**

In `app/config.py`, add after the `fetch_url_allowlist` block:

```python
    # --- RAG: department corpus ingestion ---
    # Corpus documents are org knowledge, NOT per-user files — separate tree.
    rag_docs_dir: str = "rag_documents"
    # Native output is 2560; we MRL-truncate to 1536 because pgvector's HNSW
    # index caps at 2000 dimensions. Must match vector(1536) in the schema.
    rag_embed_model: str = "qwen3-embedding:4b-q8_0"
    rag_embed_dim: int = 1536
    rag_embed_batch: int = 32          # texts per /v1/embeddings request
    rag_chunk_max_chars: int = 2000
    rag_chunk_overlap_chars: int = 200
    # Worker loop timing.
    rag_ingest_poll_seconds: float = 2.0
    rag_ingest_stale_minutes: int = 10  # running + stale heartbeat -> failed
    # Must be comfortably below stale_minutes*60 — a big PDF spends far longer
    # than the stale window in parse+embed, and without beats the sweep would
    # fail a job that is working fine.
    rag_ingest_heartbeat_seconds: float = 30.0
```

And in `.env.example`, after the `FETCH_URL_ALLOWLIST` block:

```bash
# --- RAG (department corpus ingestion; the WORKER reads these too) ---
# Corpus documents live here, separate from FILES_DIR. documents.storage_key is
# a RELATIVE key under this directory.
RAG_DOCS_DIR=rag_documents
# Native 2560 dims, MRL-truncated to 1536 (pgvector HNSW caps at 2000).
RAG_EMBED_MODEL=qwen3-embedding:4b-q8_0
RAG_EMBED_DIM=1536
RAG_EMBED_BATCH=32
RAG_CHUNK_MAX_CHARS=2000
RAG_CHUNK_OVERLAP_CHARS=200
RAG_INGEST_POLL_SECONDS=2.0
RAG_INGEST_STALE_MINUTES=10
# Keep well below STALE_MINUTES*60: a large PDF spends longer than the stale
# window in parse+embed, and the periodic heartbeat is what stops the sweep
# failing a healthy job.
RAG_INGEST_HEARTBEAT_SECONDS=30
```

- [ ] **Step 4: Write the storage module**

Create `app/rag/storage.py`:

```python
"""On-disk layout for department corpus documents.

`documents.storage_key` is a RELATIVE key like `hr/9f3c....pdf`, never an
absolute path (unlike `generated_files.path`). Rows stay portable across hosts
and the same value becomes the bucket key if this moves to object storage.

Resolution is traversal-safe: a key is only ever joined to the configured base
and then checked to still be inside it. The key is minted by us, but it round
trips through the database, so it is treated as untrusted on the way back.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4


class StorageError(Exception):
    """A key that does not resolve to a location inside the base directory."""


_SLUG = re.compile(r"[^a-z0-9._-]+")


def _slug(value: str) -> str:
    cleaned = _SLUG.sub("-", value.strip().lower()).strip(".-")
    return cleaned or "misc"


def mint_storage_key(department_code: str, filename: str) -> str:
    """A fresh relative key: `<dept>/<uuid><ext>`.

    The caller-supplied filename contributes ONLY its extension — the on-disk
    name is a uuid, exactly like the generated-file store, so a hostile name
    cannot traverse or collide.
    """
    ext = Path(filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext or ""):
        ext = ".bin"
    return f"{_slug(department_code)}/{uuid4().hex}{ext}"


def resolve_storage_path(storage_key: str, base_dir: str) -> Path:
    """Absolute path for a key, refusing anything outside `base_dir`."""
    base = Path(base_dir).resolve()
    candidate = (base / storage_key).resolve()
    if candidate != base and base not in candidate.parents:
        raise StorageError(f"storage key escapes the base directory: {storage_key!r}")
    return candidate


def write_document(data: bytes, storage_key: str, base_dir: str) -> None:
    """Persist bytes at `storage_key`, creating parent directories."""
    path = resolve_storage_path(storage_key, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def delete_document(storage_key: str, base_dir: str) -> bool:
    """Remove a stored file. True if something was deleted.

    This is **compensation**: the upload routes write the file before the
    database work is known to succeed, so a duplicate-content 409 or a failed
    commit would otherwise leak an orphan. It therefore must not raise on a
    missing file or a directory — an exception here would mask the original
    error it is cleaning up after. A traversal attempt still raises, because
    deleting outside the base directory is never compensation.
    """
    path = resolve_storage_path(storage_key, base_dir)   # raises on traversal
    try:
        path.unlink()
        return True
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return False
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_storage.py -v`
Expected: PASS, 16 collected (13 functions; the traversal test is parametrized ×4)

- [ ] **Step 6: Commit**

Review the config diff first so no unrelated in-flight change is swept in:

```bash
git add app/config.py .env.example app/rag/storage.py tests/test_rag_storage.py
git diff --staged app/config.py        # MUST show only the new RAG settings
git commit -m "feat(rag): RAG config + traversal-safe corpus document storage"
```

---

### Task 2: Embedding helper

**Files:**
- Create: `app/rag/embedding.py`
- Test: `tests/test_rag_embedding.py`
- Test: `tests/test_rag_embedding_live.py`

**Interfaces:**
- Consumes: `app.ollama.client.OllamaClient.embeddings(payload) -> dict`
- Produces: `EmbeddingError`, `QUERY_INSTRUCTION`, `format_query(text) -> str`, `truncate_normalize(vec, dim) -> list[float]`, `async embed_texts(client, texts, *, mode, model, dim, batch_size) -> list[list[float]]`

**Three things this must get right, all of which fail silently if wrong:**

1. **Qwen3-Embedding is asymmetric.** Queries carry an instruction prefix; documents do not. Slice 2 only calls `mode="document"`, but the helper ships both because slice 3 needs `mode="query"` and mismatched sides just quietly degrade retrieval.
2. **Truncate to 1536 then re-normalize.** An MRL sub-vector is not unit-norm. Optional for cosine, mandatory for `<#>`/`<->`.
3. **Reorder batch results by `index`.** The API returns one object per input with an explicit index; array order is not contractual.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_embedding.py`:

```python
"""Embedding helper. Pure — a fake client, no network.

Locks the three silent-failure modes: query/document asymmetry, truncation +
renormalization, and batch result ordering.
"""

import asyncio
import math

import pytest

from app.rag.embedding import (
    EmbeddingError,
    QUERY_INSTRUCTION,
    embed_texts,
    format_query,
    truncate_normalize,
)

DIM = 1536


class FakeClient:
    """Returns deterministic, DIRECTIONALLY DISTINCT vectors.

    A constant vector like [k, k, k, ...] normalizes to the same unit vector for
    every k, which would make an ordering test vacuous — the vectors would be
    identical no matter how they were shuffled. So input i gets a one-hot-ish
    vector with its marker in slot i, which survives normalization.
    """

    def __init__(self, native_dim=2560, shuffle=False, dim_override=None,
                 bad_index=None, duplicate_index=False):
        self.native_dim = native_dim
        self.shuffle = shuffle
        self.dim_override = dim_override
        self.bad_index = bad_index
        self.duplicate_index = duplicate_index
        self.payloads = []

    def _vector(self, i, n):
        vec = [0.0] * n
        vec[i % n] = 1.0        # direction depends on i, survives normalization
        vec[(i + 1) % n] = 0.5
        return vec

    async def embeddings(self, payload):
        self.payloads.append(payload)
        n = self.dim_override or self.native_dim
        data = [
            {"index": i, "embedding": self._vector(i, n)}
            for i, _ in enumerate(payload["input"])
        ]
        if self.duplicate_index:
            for item in data:
                item["index"] = 0
        if self.bad_index is not None:
            data[0]["index"] = self.bad_index
        if self.shuffle:
            data = list(reversed(data))  # index is authoritative, order is not
        return {"data": data}


def _argmax(vec):
    return max(range(len(vec)), key=lambda i: vec[i])


def _run(coro):
    return asyncio.run(coro)


def test_query_mode_uses_the_instruction_prefix():
    formatted = format_query("how much annual leave?")
    assert formatted.startswith("Instruct: ")
    assert QUERY_INSTRUCTION in formatted
    assert "Query: how much annual leave?" in formatted


def test_documents_are_embedded_raw():
    client = FakeClient()
    _run(embed_texts(client, ["a policy paragraph"], mode="document",
                     model="m", dim=DIM, batch_size=8))
    assert client.payloads[0]["input"] == ["a policy paragraph"]


def test_queries_are_embedded_with_the_prefix():
    client = FakeClient()
    _run(embed_texts(client, ["annual leave"], mode="query",
                     model="m", dim=DIM, batch_size=8))
    sent = client.payloads[0]["input"][0]
    assert sent.startswith("Instruct: ") and "annual leave" in sent


def test_unknown_mode_is_rejected():
    client = FakeClient()
    with pytest.raises(EmbeddingError):
        _run(embed_texts(client, ["x"], mode="sideways",
                         model="m", dim=DIM, batch_size=8))


def test_truncate_normalize_yields_unit_length():
    out = truncate_normalize([3.0] * 2560, DIM)
    assert len(out) == DIM
    assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, rel_tol=1e-9)


def test_truncate_keeps_the_leading_dimensions():
    """MRL packs the most significant dimensions first — take the head."""
    vec = [float(i) for i in range(2560)]
    out = truncate_normalize(vec, 4)
    assert out[0] < out[1] < out[2] < out[3]   # order of 0,1,2,3 preserved


def test_a_short_vector_is_an_error_not_a_pad():
    with pytest.raises(EmbeddingError):
        truncate_normalize([1.0] * 768, DIM)


def test_a_zero_vector_is_an_error_not_a_divide_by_zero():
    with pytest.raises(EmbeddingError):
        truncate_normalize([0.0] * 2560, DIM)


def test_every_returned_vector_is_exactly_dim_wide():
    client = FakeClient()
    out = _run(embed_texts(client, ["a", "b", "c"], mode="document",
                           model="m", dim=DIM, batch_size=8))
    assert [len(v) for v in out] == [DIM, DIM, DIM]


def test_results_are_reordered_by_index_not_array_position():
    """The backend may return objects in any order; `index` is authoritative.

    FakeClient encodes input position as the vector's DIRECTION (slot i), which
    survives normalization — a constant-magnitude encoding would not.
    """
    ordered = FakeClient(shuffle=False)
    shuffled = FakeClient(shuffle=True)
    a = _run(embed_texts(ordered, ["a", "b", "c"], mode="document",
                         model="m", dim=DIM, batch_size=8))
    b = _run(embed_texts(shuffled, ["a", "b", "c"], mode="document",
                         model="m", dim=DIM, batch_size=8))
    assert a == b                       # reordering undoes the shuffle exactly
    assert [_argmax(v) for v in a] == [0, 1, 2]


def test_out_of_range_index_is_rejected():
    with pytest.raises(EmbeddingError):
        _run(embed_texts(FakeClient(bad_index=99), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_negative_index_is_rejected():
    with pytest.raises(EmbeddingError):
        _run(embed_texts(FakeClient(bad_index=-1), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_duplicate_indexes_are_rejected():
    """Two objects claiming index 0 would silently drop an input."""
    with pytest.raises(EmbeddingError):
        _run(embed_texts(FakeClient(duplicate_index=True), ["a", "b"],
                         mode="document", model="m", dim=DIM, batch_size=8))


def test_a_missing_index_field_is_rejected_not_defaulted():
    class NoIndex(FakeClient):
        async def embeddings(self, payload):
            full = await super().embeddings(payload)
            for item in full["data"]:
                item.pop("index")
            return full

    with pytest.raises(EmbeddingError):
        _run(embed_texts(NoIndex(), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_a_non_integer_index_is_rejected():
    class StringIndex(FakeClient):
        async def embeddings(self, payload):
            full = await super().embeddings(payload)
            for item in full["data"]:
                item["index"] = str(item["index"])
            return full

    with pytest.raises(EmbeddingError):
        _run(embed_texts(StringIndex(), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_batching_splits_requests_and_preserves_overall_order():
    client = FakeClient()
    texts = [f"t{i}" for i in range(10)]
    out = _run(embed_texts(client, texts, mode="document",
                           model="m", dim=DIM, batch_size=4))
    assert len(out) == 10
    assert [len(p["input"]) for p in client.payloads] == [4, 4, 2]


def test_empty_input_makes_no_request():
    client = FakeClient()
    assert _run(embed_texts(client, [], mode="document",
                            model="m", dim=DIM, batch_size=4)) == []
    assert client.payloads == []


def test_a_backend_returning_too_few_dimensions_is_an_error():
    """Guards against silently storing a wrong-width vector if the model or the
    backend changes underneath us."""
    client = FakeClient(dim_override=768)
    with pytest.raises(EmbeddingError):
        _run(embed_texts(client, ["a"], mode="document",
                         model="m", dim=DIM, batch_size=4))


def test_a_missing_result_is_an_error_not_a_short_list():
    class Truncating(FakeClient):
        async def embeddings(self, payload):
            full = await super().embeddings(payload)
            full["data"] = full["data"][:-1]
            return full

    with pytest.raises(EmbeddingError):
        _run(embed_texts(Truncating(), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_embedding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.embedding'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/embedding.py`:

```python
"""Embedding for the department corpus.

Qwen3-Embedding is **asymmetric**: a query carries an instruction prefix, a
document does not. Embedding both sides the same way is the most common way to
lose retrieval quality with this model family, and it fails silently — you just
get mediocre results. Hence `mode` is required, never defaulted.

Native output is 2560 dimensions; we MRL-truncate to 1536 because pgvector's
HNSW index caps at 2000, then re-normalize. Ollama honours a `dimensions`
parameter and normalizes for us, but doing it here is a portability contract:
whether a backend renormalizes after truncating is backend-specific, and a
non-unit sub-vector silently breaks `<#>` and `<->`.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Protocol, Sequence

Mode = Literal["query", "document"]

# From the Qwen3-Embedding model card. Slice 3 (retrieval) is the only caller of
# query mode; it lives here so both sides can never drift apart.
QUERY_INSTRUCTION = (
    "Given a search query, retrieve relevant passages that answer the query"
)


class EmbeddingError(Exception):
    """The backend returned something we refuse to store."""


class EmbeddingClient(Protocol):
    async def embeddings(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def format_query(text: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"


def truncate_normalize(vec: Sequence[float], dim: int) -> list[float]:
    """Take the leading `dim` components (MRL packs the most significant first)
    and rescale to unit length."""
    if len(vec) < dim:
        raise EmbeddingError(
            f"embedding has {len(vec)} dimensions, need at least {dim}"
        )
    head = [float(x) for x in vec[:dim]]
    norm = math.sqrt(sum(x * x for x in head))
    if norm == 0.0:
        raise EmbeddingError("embedding is a zero vector; refusing to store it")
    return [x / norm for x in head]


async def embed_texts(
    client: EmbeddingClient,
    texts: Sequence[str],
    *,
    mode: Mode,
    model: str,
    dim: int,
    batch_size: int,
) -> list[list[float]]:
    """Embed `texts` in batches, returning unit-length `dim`-wide vectors in the
    same order as the input."""
    if mode not in ("query", "document"):
        raise EmbeddingError(f"unknown embedding mode {mode!r}")
    if not texts:
        return []

    prepared = [format_query(t) if mode == "query" else t for t in texts]
    out: list[list[float]] = []

    for start in range(0, len(prepared), max(1, batch_size)):
        batch = prepared[start : start + max(1, batch_size)]
        response = await client.embeddings({"model": model, "input": list(batch)})
        out.extend(_ordered_vectors(response, len(batch), dim))

    return out


def _ordered_vectors(response: dict, expected: int, dim: int) -> list[list[float]]:
    """Validate a batch response and return its vectors in INPUT order.

    `index` is authoritative — array order is not contractual — but a bad index
    is far worse than a missing one: silently defaulting it (`.get("index", 0)`)
    would map several results onto the same input and quietly drop the rest, so
    every failure mode here is an exception rather than a fallback.
    """
    data = response.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError(
            f"expected {expected} embeddings, got "
            f"{len(data) if isinstance(data, list) else type(data).__name__}"
        )

    by_index: dict[int, Any] = {}
    for item in data:
        if not isinstance(item, dict) or "index" not in item:
            raise EmbeddingError("embedding result is missing its `index`")
        idx = item["index"]
        # bool is an int subclass; a True index is a bug, not position 1.
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise EmbeddingError(f"embedding `index` is not an integer: {idx!r}")
        if not 0 <= idx < expected:
            raise EmbeddingError(
                f"embedding `index` {idx} out of range for a batch of {expected}"
            )
        if idx in by_index:
            raise EmbeddingError(f"duplicate embedding `index` {idx}")
        by_index[idx] = item

    if set(by_index) != set(range(expected)):  # pragma: no cover - defensive
        missing = sorted(set(range(expected)) - set(by_index))
        raise EmbeddingError(f"missing embeddings for inputs {missing}")

    return [truncate_normalize(by_index[i]["embedding"], dim) for i in range(expected)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_embedding.py -v`
Expected: PASS, 19 tests

- [ ] **Step 5: Write the live-backend test**

Create `tests/test_rag_embedding_live.py`:

```python
"""Live embedding-backend check. Skips unless Ollama is up AND the configured
model is pulled — so the offline suite stays green, but a real run proves the
contract the schema depends on.
"""

import asyncio

import httpx
import pytest

from app.config import get_settings
from app.ollama.client import OllamaClient
from app.rag.embedding import embed_texts


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def model_available(settings):
    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        names = {m["name"] for m in resp.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Ollama unreachable: {type(exc).__name__}")
    if settings.rag_embed_model not in names:
        pytest.skip(
            f"{settings.rag_embed_model} not pulled "
            f"(run: ollama pull {settings.rag_embed_model})"
        )
    return settings.rag_embed_model


def test_document_embedding_is_exactly_1536_and_unit_length(settings, model_available):
    async def go():
        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        try:
            return await embed_texts(
                client, ["Annual leave accrues monthly."], mode="document",
                model=model_available, dim=settings.rag_embed_dim,
                batch_size=settings.rag_embed_batch,
            )
        finally:
            await client.aclose()

    vecs = _run(go())
    assert len(vecs) == 1
    assert len(vecs[0]) == settings.rag_embed_dim == 1536
    norm = sum(x * x for x in vecs[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_batch_order_is_preserved_against_the_real_backend(settings, model_available):
    async def go():
        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        try:
            return await embed_texts(
                client, ["alpha alpha alpha", "beta beta beta", "alpha alpha alpha"],
                mode="document", model=model_available,
                dim=settings.rag_embed_dim, batch_size=8,
            )
        finally:
            await client.aclose()

    a, b, a2 = _run(go())
    # Identical inputs must land in positions 0 and 2, not be shuffled.
    assert a == pytest.approx(a2, abs=1e-6)
    assert a != pytest.approx(b, abs=1e-6)


def test_query_and_document_modes_produce_different_vectors(settings, model_available):
    """Proves the instruction prefix is actually reaching the model — if these
    matched, the asymmetry would be silently absent."""
    async def go():
        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        try:
            text = "annual leave entitlement"
            q = await embed_texts(client, [text], mode="query",
                                  model=model_available,
                                  dim=settings.rag_embed_dim, batch_size=4)
            d = await embed_texts(client, [text], mode="document",
                                  model=model_available,
                                  dim=settings.rag_embed_dim, batch_size=4)
            return q[0], d[0]
        finally:
            await client.aclose()

    q, d = _run(go())
    assert q != pytest.approx(d, abs=1e-6)
```

- [ ] **Step 6: Run the live test**

Run: `.venv/bin/pytest tests/test_rag_embedding_live.py -v`
Expected: PASS if `qwen3-embedding:4b-q8_0` is pulled; SKIP with a message naming the pull command otherwise. A skip here is acceptable now but **the slice is not verified until these pass**.

- [ ] **Step 7: Commit**

```bash
git add app/rag/embedding.py tests/test_rag_embedding.py tests/test_rag_embedding_live.py
git commit -m "feat(rag): embedding helper — query/document asymmetry, 2560->1536 + normalize"
```

---

### Task 3: Chunking

**Files:**
- Create: `app/rag/chunking.py`
- Test: `tests/test_rag_chunking.py`

**Interfaces:**
- Produces: `Chunk` (frozen dataclass: `content: str`, `chunk_index: int`, `page_number: int | None`, `section: str | None`, `element_type: str | None`, `token_count: int | None`), `chunk_text(text, *, max_chars, overlap_chars, section=None) -> list[Chunk]`, `chunk_table(headers, rows, *, sheet_name, max_chars) -> list[Chunk]`, `renumber(chunks) -> list[Chunk]`

**The spreadsheet rule:** every table chunk repeats the header row, so a chunk retrieved on its own is self-describing. Without that a retrieved row is a list of bare values with no idea what the columns mean.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_chunking.py`:

```python
"""Chunking. Pure — no IO, no models."""

import pytest

from app.rag.chunking import Chunk, chunk_table, chunk_text, renumber


def test_short_text_is_one_chunk():
    chunks = chunk_text("a short policy note", max_chars=2000, overlap_chars=200)
    assert len(chunks) == 1
    assert chunks[0].content == "a short policy note"
    assert chunks[0].chunk_index == 0


def test_empty_or_whitespace_text_yields_no_chunks():
    assert chunk_text("   \n\t ", max_chars=100, overlap_chars=10) == []


def test_long_text_splits_and_respects_the_cap():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text, max_chars=200, overlap_chars=20)
    assert len(chunks) > 1
    assert all(len(c.content) <= 200 for c in chunks)


def test_chunks_are_indexed_contiguously_from_zero():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = chunk_text(text, max_chars=100, overlap_chars=10)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_consecutive_chunks_overlap():
    """Overlap keeps a sentence spanning a boundary retrievable from either side."""
    text = " ".join(f"w{i}" for i in range(300))
    chunks = chunk_text(text, max_chars=120, overlap_chars=40)
    assert len(chunks) >= 2
    tail = chunks[0].content[-20:]
    assert tail.split()[-1] in chunks[1].content


def test_no_overlap_when_overlap_is_zero():
    text = "x" * 300
    chunks = chunk_text(text, max_chars=100, overlap_chars=0)
    assert "".join(c.content for c in chunks) == text


def test_overlap_larger_than_max_chars_does_not_loop_forever():
    """A misconfiguration must degrade, not hang."""
    text = "y" * 500
    chunks = chunk_text(text, max_chars=50, overlap_chars=500)
    assert 0 < len(chunks) < 100


def test_section_is_carried_onto_every_chunk():
    chunks = chunk_text("a b c", max_chars=10, overlap_chars=0,
                        section="Leave Policy > Annual")
    assert all(c.section == "Leave Policy > Annual" for c in chunks)


def test_table_chunks_repeat_the_header_row():
    """Each chunk must be self-describing — a bare row of values is useless
    when it is the only thing retrieved."""
    headers = ["Employee", "Department", "Days"]
    rows = [[f"Person {i}", "HR", str(i)] for i in range(50)]
    chunks = chunk_table(headers, rows, sheet_name="Leave", max_chars=200)
    assert len(chunks) > 1
    for c in chunks:
        assert "Employee" in c.content
        assert "Department" in c.content


def test_table_chunks_name_their_sheet_and_are_typed():
    chunks = chunk_table(["A"], [["1"]], sheet_name="Balances", max_chars=500)
    assert "Balances" in chunks[0].content
    assert chunks[0].element_type == "table"


def test_table_with_no_rows_yields_nothing():
    assert chunk_table(["A", "B"], [], sheet_name="Empty", max_chars=500) == []


def test_a_single_row_wider_than_the_cap_is_still_emitted():
    """Losing data silently is worse than exceeding the cap."""
    wide = ["z" * 900]
    chunks = chunk_table(["A"], [wide], sheet_name="S", max_chars=100)
    assert len(chunks) == 1
    assert "z" * 900 in chunks[0].content


def test_renumber_makes_indices_contiguous_across_concatenated_groups():
    a = chunk_text("one", max_chars=50, overlap_chars=0)
    b = chunk_text("two", max_chars=50, overlap_chars=0)
    merged = renumber(a + b)
    assert [c.chunk_index for c in merged] == [0, 1]


def test_chunk_is_immutable():
    import dataclasses
    c = Chunk(content="x", chunk_index=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.content = "y"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_chunking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.chunking'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/chunking.py`:

```python
"""Turning parsed content into retrievable chunks.

Two shapes, because prose and grids fail differently:

- **Prose** splits on a character budget with overlap, preferring paragraph then
  sentence then word boundaries, so a sentence straddling a boundary is still
  retrievable from either side.
- **Tables** repeat the header row in EVERY chunk. A spreadsheet row retrieved
  on its own is a list of bare values with no idea what its columns mean; the
  header is what makes a chunk self-describing.

Nothing here does IO or calls a model — it is all pure and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence


@dataclass(frozen=True)
class Chunk:
    content: str
    chunk_index: int
    page_number: int | None = None      # PDF only
    section: str | None = None          # heading path
    element_type: str | None = None     # text|heading|table|list
    token_count: int | None = None


def _split_point(text: str, limit: int) -> int:
    """Best boundary at or before `limit`: paragraph, then sentence, then word."""
    window = text[:limit]
    for sep in ("\n\n", ". ", "\n", " "):
        idx = window.rfind(sep)
        if idx > limit // 2:            # don't take a uselessly early break
            return idx + len(sep)
    return limit


def chunk_text(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
    section: str | None = None,
    page_number: int | None = None,
    element_type: str = "text",
) -> list[Chunk]:
    """Split prose into overlapping chunks of at most `max_chars`."""
    body = text.strip()
    if not body:
        return []

    # A misconfigured overlap >= max_chars would never advance. Clamp so it
    # degrades to a smaller overlap instead of hanging.
    overlap = max(0, min(overlap_chars, max_chars // 2))
    step_floor = max(1, max_chars - overlap)

    chunks: list[Chunk] = []
    pos = 0
    while pos < len(body):
        remaining = body[pos:]
        if len(remaining) <= max_chars:
            piece, advance = remaining, len(remaining)
        else:
            cut = _split_point(remaining, max_chars)
            piece, advance = remaining[:cut], max(cut - overlap, step_floor)
        piece = piece.strip()
        if piece:
            chunks.append(
                Chunk(
                    content=piece,
                    chunk_index=len(chunks),
                    section=section,
                    page_number=page_number,
                    element_type=element_type,
                )
            )
        pos += advance
    return chunks


def chunk_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    sheet_name: str,
    max_chars: int,
) -> list[Chunk]:
    """Group rows into chunks, repeating the header in each so a chunk read in
    isolation still says what its columns are."""
    if not rows:
        return []

    header_line = " | ".join(str(h) for h in headers)
    preamble = f"Sheet: {sheet_name}\n{header_line}\n"

    chunks: list[Chunk] = []
    buffer: list[str] = []
    size = len(preamble)

    def flush() -> None:
        if buffer:
            chunks.append(
                Chunk(
                    content=preamble + "\n".join(buffer),
                    chunk_index=len(chunks),
                    section=sheet_name,
                    element_type="table",
                )
            )

    for row in rows:
        line = " | ".join(str(c) for c in row)
        # A single row wider than the cap still gets emitted: dropping data
        # silently is worse than one oversized chunk.
        if buffer and size + len(line) + 1 > max_chars:
            flush()
            buffer, size = [], len(preamble)
        buffer.append(line)
        size += len(line) + 1
    flush()
    return chunks


def renumber(chunks: Sequence[Chunk]) -> list[Chunk]:
    """Make `chunk_index` contiguous from 0 across concatenated groups —
    `uq_document_chunks_doc_index` requires uniqueness per document."""
    return [replace(c, chunk_index=i) for i, c in enumerate(chunks)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_chunking.py -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Commit**

```bash
git add app/rag/chunking.py tests/test_rag_chunking.py
git commit -m "feat(rag): chunking — overlapping prose + self-describing table chunks"
```

---

### Task 4: Parsing

**Files:**
- Create: `app/rag/parsing.py`
- Test: `tests/test_rag_parsing.py`
- Test: `tests/test_rag_parsing_docling.py`

**Interfaces:**
- Consumes: `app.rag.chunking`, `app.files.readers.open_sheet_rows`
- Produces: `ParseError`, `SUPPORTED_FILE_TYPES: frozenset[str]`, `detect_file_type(filename) -> str`, `parse_to_chunks(path: Path, file_type: str, *, max_chars, overlap_chars) -> list[Chunk]`, `parse_text_to_chunks(text, *, max_chars, overlap_chars) -> list[Chunk]`

**The dependency rule this file exists to enforce:** Docling is imported **inside** the pdf/docx branch, never at module scope. The API process imports nothing from here, but a stray import must not drag torch into the API image — and when Docling is genuinely missing, the failure must name the fix.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_parsing.py`:

```python
"""Parsing for formats that need no heavy dependency: xlsx, csv, text.

Docling-backed formats live in tests/test_rag_parsing_docling.py so this file
runs in the API environment, where Docling is deliberately absent.
"""

import csv as _csv
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.rag.parsing import (
    ParseError,
    detect_file_type,
    parse_text_to_chunks,
    parse_to_chunks,
)

OPTS = {"max_chars": 500, "overlap_chars": 50}


@pytest.fixture()
def csv_file(tmp_path):
    p = tmp_path / "leave.csv"
    with p.open("w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["Employee", "Department", "Days"])
        for i in range(30):
            w.writerow([f"Person {i}", "HR", str(i)])
    return p


@pytest.fixture()
def xlsx_file(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Balances"
    ws.append(["Employee", "Department", "Days"])
    for i in range(30):
        ws.append([f"Person {i}", "HR", i])
    p = tmp_path / "balances.xlsx"
    wb.save(p)
    return p


@pytest.mark.parametrize("name,expected", [
    ("a.pdf", "pdf"), ("a.PDF", "pdf"), ("a.docx", "docx"),
    ("a.xlsx", "xlsx"), ("a.csv", "csv"), ("a.txt", "text"), ("a.md", "text"),
])
def test_detect_file_type(name, expected):
    assert detect_file_type(name) == expected


def test_unsupported_extension_is_rejected():
    with pytest.raises(ParseError):
        detect_file_type("malware.exe")


def test_text_parses_to_chunks():
    chunks = parse_text_to_chunks("A typed-in policy note.", **OPTS)
    assert len(chunks) == 1
    assert "typed-in policy" in chunks[0].content


def test_empty_typed_text_is_an_error_not_an_empty_document():
    """A document with zero chunks would be silently unsearchable."""
    with pytest.raises(ParseError):
        parse_text_to_chunks("   ", **OPTS)


def test_csv_chunks_repeat_the_header(csv_file):
    chunks = parse_to_chunks(csv_file, "csv", **OPTS)
    assert chunks
    assert all("Employee" in c.content for c in chunks)
    assert all(c.element_type == "table" for c in chunks)


def test_xlsx_chunks_cover_every_row(xlsx_file):
    """open_sheet_rows is uncapped, unlike load_table's ~200-row window."""
    chunks = parse_to_chunks(xlsx_file, "xlsx", **OPTS)
    joined = "\n".join(c.content for c in chunks)
    assert "Person 0" in joined
    assert "Person 29" in joined


def test_xlsx_covers_every_sheet(tmp_path):
    wb = Workbook()
    wb.active.title = "First"
    wb.active.append(["A"])
    wb.active.append(["one"])
    second = wb.create_sheet("Second")
    second.append(["B"])
    second.append(["two"])
    p = tmp_path / "multi.xlsx"
    wb.save(p)

    chunks = parse_to_chunks(p, "xlsx", **OPTS)
    joined = "\n".join(c.content for c in chunks)
    assert "First" in joined and "Second" in joined
    assert "one" in joined and "two" in joined


def test_chunk_indices_are_contiguous_across_sheets(tmp_path):
    wb = Workbook()
    wb.active.title = "S1"
    wb.active.append(["A"])
    for i in range(40):
        wb.active.append([f"r{i}"])
    s2 = wb.create_sheet("S2")
    s2.append(["B"])
    for i in range(40):
        s2.append([f"q{i}"])
    p = tmp_path / "two.xlsx"
    wb.save(p)

    chunks = parse_to_chunks(p, "xlsx", max_chars=120, overlap_chars=0)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_a_corrupt_spreadsheet_raises_parse_error(tmp_path):
    p = tmp_path / "broken.xlsx"
    p.write_bytes(b"not a spreadsheet")
    with pytest.raises(ParseError):
        parse_to_chunks(p, "xlsx", **OPTS)


def test_an_empty_sheet_produces_no_chunks_and_raises(tmp_path):
    wb = Workbook()
    wb.active.append(["Header"])
    p = tmp_path / "headers_only.xlsx"
    wb.save(p)
    with pytest.raises(ParseError):
        parse_to_chunks(p, "xlsx", **OPTS)


def test_docling_is_not_imported_at_module_scope():
    """The API image must never pull torch. If this import moves to the top of
    parsing.py, this fails in the API environment."""
    import sys
    import app.rag.parsing  # noqa: F401
    assert "docling" not in sys.modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_parsing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.parsing'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/parsing.py`:

```python
"""Parsing a corpus document into chunks.

Format split, and why:

- **pdf / docx -> Docling, walking `iterate_items()`.** Layout analysis and
  table-structure recognition are the difference between a usable and a useless
  PDF chunk. We iterate items rather than dumping `export_to_markdown()`,
  because the dump discards exactly what slice-3 citations need: the real
  `page_no` from `item.prov`, the heading path, and the element label.
- **xlsx / csv -> `app/files/readers.py`.** One spreadsheet normalizer is shared
  with `read_excel`/`aggregate_excel`; a second would diverge from the tools that
  already read spreadsheets here, and Docling buys nothing on a plain grid. Uses
  `open_sheet_rows` (uncapped streaming), NOT `load_table` (~200-row window).
- **text / md -> straight to the prose chunker.**

**Docling is imported lazily, inside the branch that needs it.** The API process
must never load torch; a stray module-scope import would drag ~90 packages into
the API image. `tests/test_rag_parsing.py` asserts this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from dataclasses import replace

from ..files import readers
from .chunking import Chunk, chunk_table, chunk_text, renumber


class ParseError(Exception):
    """The file could not be turned into at least one chunk."""


SUPPORTED_FILE_TYPES = frozenset({"pdf", "docx", "xlsx", "csv", "text"})

_EXT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
}


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    file_type = _EXT_MAP.get(ext)
    if file_type is None:
        raise ParseError(
            f"unsupported file type {ext or '(none)'}; "
            f"supported: {', '.join(sorted(_EXT_MAP))}"
        )
    return file_type


def parse_text_to_chunks(text: str, *, max_chars: int, overlap_chars: int) -> list[Chunk]:
    chunks = chunk_text(text, max_chars=max_chars, overlap_chars=overlap_chars)
    if not chunks:
        raise ParseError("no text content to index")
    return chunks


def _parse_spreadsheet(path: Path, *, max_chars: int) -> list[Chunk]:
    """Every sheet, every row. Header repeats into each chunk (see chunk_table)."""
    try:
        sheets = [s.name for s in readers.inspect_workbook(path)]
    except readers.ReadError as exc:
        raise ParseError(str(exc)) from exc

    collected: list[Chunk] = []
    for sheet in sheets or [None]:
        try:
            with readers.open_sheet_rows(path, sheet=sheet) as stream:
                rows = [row for row in stream.rows if any(str(c).strip() for c in row)]
                collected.extend(
                    chunk_table(
                        stream.headers, rows,
                        sheet_name=stream.sheet_name, max_chars=max_chars,
                    )
                )
        except readers.ReadError as exc:
            raise ParseError(str(exc)) from exc
    return renumber(collected)


# Docling's label vocabulary -> our four element_type values.
_ELEMENT_TYPES = {
    "section_header": "heading",
    "title": "heading",
    "page_header": "heading",
    "table": "table",
    "list_item": "list",
}


def _heading_path(stack: list[tuple[int, str]]) -> str | None:
    return " > ".join(text for _level, text in stack) if stack else None


def _with_context(chunks: list[Chunk], section: str | None) -> list[Chunk]:
    """Prepend the heading path to each chunk's CONTENT, not just its metadata.

    `tsv` is generated from `content` alone, so a heading kept only in the
    `section` column would be invisible to the lexical channel — a query for
    "carry over" would miss the section actually titled "Carry Over". This is the
    same reasoning that repeats the header row into every table chunk.
    """
    if not section:
        return chunks
    return [replace(c, content=f"{section}\n\n{c.content}") for c in chunks]


def _parse_with_docling(path: Path, *, max_chars: int, overlap_chars: int) -> list[Chunk]:
    """PDF/DOCX via Docling, PRESERVING provenance. Imported HERE, never at
    module scope.

    Walks `iterate_items()` rather than dumping `export_to_markdown()`, because
    the markdown dump throws away exactly what slice-3 citations need: the real
    `page_no` from `item.prov`, the heading path, and the element label.
    Verified against docling 2.118: `iterate_items()` yields `(item, level)`,
    `item.prov[0].page_no` is 1-based, and `item.label` is a `DocItemLabel`.
    """
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ParseError(
            "Docling is not installed. PDF/DOCX ingestion runs in the WORKER "
            "environment only: pip install -r requirements-worker.txt"
        ) from exc

    try:
        document = DocumentConverter().convert(str(path)).document
    except Exception as exc:  # noqa: BLE001 - Docling raises a wide range
        raise ParseError(f"could not parse document: {exc}") from exc

    collected: list[Chunk] = []
    headings: list[tuple[int, str]] = []

    for item, _tree_level in document.iterate_items():
        label = getattr(getattr(item, "label", None), "value", "") or ""
        prov = getattr(item, "prov", None) or []
        page = prov[0].page_no if prov else None

        if label in ("section_header", "title"):
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            level = getattr(item, "level", 1) or 1
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text))
            continue  # the heading itself is carried into following chunks

        if label == "table":
            try:
                text = item.export_to_markdown(document).strip()
            except Exception:  # noqa: BLE001 - a malformed table is not fatal
                text = ""
        else:
            text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        pieces = chunk_text(
            text,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            section=_heading_path(headings),
            page_number=page,
            element_type=_ELEMENT_TYPES.get(label, "text"),
        )
        collected.extend(_with_context(pieces, _heading_path(headings)))

    if not collected:
        raise ParseError(
            "document produced no text — a scanned PDF needs OCR, which v1 does not do"
        )
    return collected


def parse_to_chunks(
    path: Path, file_type: str, *, max_chars: int, overlap_chars: int
) -> list[Chunk]:
    """Dispatch on `file_type`, returning contiguously indexed chunks."""
    if file_type in ("xlsx", "csv"):
        chunks = _parse_spreadsheet(path, max_chars=max_chars)
    elif file_type in ("pdf", "docx"):
        chunks = _parse_with_docling(
            path, max_chars=max_chars, overlap_chars=overlap_chars
        )
    elif file_type == "text":
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ParseError(f"could not read file: {exc}") from exc
        chunks = chunk_text(body, max_chars=max_chars, overlap_chars=overlap_chars)
    else:
        raise ParseError(f"unsupported file type {file_type!r}")

    if not chunks:
        raise ParseError("no indexable content found")
    return renumber(chunks)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_parsing.py -v`
Expected: PASS, 17 collected (11 functions; `test_detect_file_type` is parametrized ×7)

- [ ] **Step 5: Write the Docling-dependent test**

Create `tests/test_rag_parsing_docling.py`:

```python
"""PDF/DOCX parsing. Skips entirely unless Docling is installed, so this file is
green in the API environment and meaningful in the worker environment.
"""

from pathlib import Path

import pytest

docling = pytest.importorskip(
    "docling", reason="Docling lives in the worker env: pip install -r requirements-worker.txt"
)

from app.rag.parsing import ParseError, parse_to_chunks  # noqa: E402

OPTS = {"max_chars": 800, "overlap_chars": 80}


@pytest.fixture(scope="module")
def docx_file(tmp_path_factory):
    from docx import Document

    doc = Document()
    doc.add_heading("Leave Policy", level=1)
    doc.add_paragraph("Annual leave accrues monthly for all permanent staff.")
    doc.add_heading("Carry Over", level=2)
    doc.add_paragraph("Up to five days may be carried into the next year.")
    path = tmp_path_factory.mktemp("docling") / "policy.docx"
    doc.save(path)
    return path


@pytest.fixture(scope="module")
def pdf_file(tmp_path_factory):
    """A real 2-page PDF with a heading per page.

    NOTE the explicit width and `set_xy`: `multi_cell(0, ...)` raises
    `FPDFException: Not enough horizontal space` once the cursor is sitting at
    the right margin after a previous cell. This form is verified to work.
    """
    from fpdf import FPDF

    def line(pdf, text, size=12, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.set_xy(pdf.l_margin, pdf.get_y())
        pdf.multi_cell(w=pdf.w - pdf.l_margin - pdf.r_margin, h=8, text=text)

    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    line(pdf, "Leave Policy", 16, True)
    line(pdf, "Annual leave accrues monthly for all permanent staff.")
    pdf.add_page()
    line(pdf, "Carry Over", 16, True)
    line(pdf, "Up to five days may be carried into the next year.")

    path = tmp_path_factory.mktemp("docling") / "policy.pdf"
    pdf.output(str(path))
    return path


def test_docx_text_is_extracted(docx_file):
    chunks = parse_to_chunks(docx_file, "docx", **OPTS)
    joined = " ".join(c.content for c in chunks)
    assert "accrues monthly" in joined
    assert "carried into the next year" in joined


def test_pdf_text_is_extracted_across_pages(pdf_file):
    chunks = parse_to_chunks(pdf_file, "pdf", **OPTS)
    joined = " ".join(c.content for c in chunks)
    assert "accrues monthly" in joined
    assert "carried into the next year" in joined


def test_pdf_chunks_carry_real_page_numbers(pdf_file):
    """The reason we walk iterate_items() instead of dumping markdown: slice-3
    citations need the page. Verified against docling 2.118 — prov[0].page_no
    is 1-based."""
    chunks = parse_to_chunks(pdf_file, "pdf", **OPTS)
    pages = {c.page_number for c in chunks if c.page_number is not None}
    assert pages, "no chunk carried a page number — provenance was lost"
    assert pages == {1, 2}

    on_page_1 = " ".join(c.content for c in chunks if c.page_number == 1)
    on_page_2 = " ".join(c.content for c in chunks if c.page_number == 2)
    assert "accrues monthly" in on_page_1
    assert "carried into the next year" in on_page_2


def test_chunks_carry_the_heading_path_as_section(pdf_file):
    chunks = parse_to_chunks(pdf_file, "pdf", **OPTS)
    sections = {c.section for c in chunks if c.section}
    assert "Leave Policy" in sections
    assert "Carry Over" in sections


def test_heading_text_is_inside_the_content_so_it_is_lexically_searchable(pdf_file):
    """`tsv` is generated from `content` alone — a heading kept only in the
    `section` column would be invisible to the lexical channel."""
    chunks = parse_to_chunks(pdf_file, "pdf", **OPTS)
    body = next(c for c in chunks if "carried into the next year" in c.content)
    assert "Carry Over" in body.content


def test_element_types_are_populated(docx_file):
    chunks = parse_to_chunks(docx_file, "docx", **OPTS)
    assert {c.element_type for c in chunks} <= {"text", "heading", "table", "list"}
    assert any(c.element_type == "text" for c in chunks)


def test_chunk_indices_are_contiguous(docx_file):
    chunks = parse_to_chunks(docx_file, "docx", max_chars=120, overlap_chars=0)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_a_non_document_file_raises_parse_error(tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"definitely not a pdf")
    with pytest.raises(ParseError):
        parse_to_chunks(bad, "pdf", **OPTS)
```

- [ ] **Step 6: Run it (expect a clean skip for now)**

Run: `.venv/bin/pytest tests/test_rag_parsing_docling.py -v`
Expected: SKIPPED until Task 8 installs `requirements-worker.txt`. Re-run after Task 8; it must pass there.

- [ ] **Step 7: Commit**

```bash
git add app/rag/parsing.py tests/test_rag_parsing.py tests/test_rag_parsing_docling.py
git commit -m "feat(rag): parsing — Docling (lazy) for pdf/docx, readers.py for xlsx/csv"
```

---

### Task 5: Job queue

**Files:**
- Create: `app/rag/jobs.py`
- Test: `tests/test_rag_jobs_integration.py`

**Interfaces:**
- Produces: `JobConflict`, `async enqueue(session, *, document_id) -> IngestJob`, `async claim_next(session) -> IngestJob | None`, `async heartbeat(session, job_id) -> None`, `async finish(session, job_id, *, status, error=None, chunks_total=None, chunks_done=None) -> None`, `async sweep_stale(session, *, stale_minutes) -> int`, `async get_job(session, job_id) -> IngestJob | None`

**Two different guarantees, easily confused:**
- `FOR UPDATE SKIP LOCKED` stops two workers claiming the same **row**.
- `ux_ingest_jobs_active_document` (slice 1) stops two active **jobs** existing for one document. `enqueue` catches that violation and raises `JobConflict`, which the router turns into 409.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_jobs_integration.py`:

```python
"""Ingest job queue against real Postgres. Skips if the DB is unreachable.

Throwaway NullPool engine per call — the app's module-level engine pools
connections bound to the first event loop (see CLAUDE.md).
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import jobs
from app.rag.models import JOB_FAILED, JOB_RUNNING, JOB_SUCCEEDED


def _run(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _sql(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def docs():
    """A department with two documents; everything removed afterwards."""
    _skip_if_no_db()
    tag = uuid.uuid4().hex[:8]
    ids = [uuid.uuid4().hex, uuid.uuid4().hex]

    async def setup(conn):
        dept = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'J') RETURNING id"),
            {"c": f"jobs{tag}"})).scalar_one()
        for n, doc_id in enumerate(ids):
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'pending')"),
                {"i": doc_id, "d": dept, "h": f"{n}" * 64})
        return dept

    dept = _sql(setup)
    yield {"dept": dept, "a": ids[0], "b": ids[1]}

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id = :d"),
                           {"d": dept})
        await conn.execute(text("DELETE FROM departments WHERE id = :d"), {"d": dept})
    _sql(teardown)


def test_enqueue_creates_a_queued_job(docs):
    async def go(s):
        job = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        return job.status, job.document_id

    status, doc_id = _run(go)
    assert status == "queued" and doc_id == docs["a"]


def test_a_second_active_job_for_one_document_is_a_conflict(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        with pytest.raises(jobs.JobConflict):
            await jobs.enqueue(s, document_id=docs["a"])
            await s.commit()
        return True

    assert _run(go) is True


def test_a_finished_job_does_not_block_a_new_one(docs):
    async def go(s):
        first = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        await jobs.finish(s, first.id, status=JOB_SUCCEEDED)
        await s.commit()
        second = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        return second.status

    assert _run(go) == "queued"


def test_claim_marks_running_and_increments_attempts(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        claimed = await jobs.claim_next(s)
        await s.commit()
        return claimed.status, claimed.attempts, claimed.started_at is not None

    status, attempts, started = _run(go)
    assert status == JOB_RUNNING and attempts == 1 and started


def test_claim_returns_none_when_the_queue_is_empty(docs):
    async def go(s):
        # Drain anything an earlier test left queued, then confirm empty.
        while await jobs.claim_next(s):
            await s.commit()
        await s.commit()
        return await jobs.claim_next(s)

    assert _run(go) is None


def test_set_chunks_total_records_the_progress_denominator(docs):
    async def go(s):
        job = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        await jobs.set_chunks_total(s, job.id, 42)
        await s.commit()
        return (await jobs.get_job(s, job.id)).chunks_total

    assert _run(go) == 42


def test_claim_is_fifo(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        await jobs.enqueue(s, document_id=docs["b"])
        await s.commit()
        first = await jobs.claim_next(s)
        await s.commit()
        return first.document_id

    assert _run(go) == docs["a"]


def test_two_concurrent_workers_never_claim_the_same_job(docs):
    """SKIP LOCKED: one gets the job, the other gets the next one or None."""
    async def go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await jobs.enqueue(s, document_id=docs["a"])
                await s.commit()

            async def worker():
                async with Session() as s:
                    claimed = await jobs.claim_next(s)
                    await s.commit()
                    return claimed.id if claimed else None

            return await asyncio.gather(worker(), worker())
        finally:
            await engine.dispose()

    a, b = asyncio.run(go())
    assert {a, b} != {None}                    # somebody got it
    assert a is None or b is None or a != b    # never the same row twice


def test_heartbeat_advances(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        before = job.heartbeat_at
        await jobs.heartbeat(s, job.id)
        await s.commit()
        after = (await jobs.get_job(s, job.id)).heartbeat_at
        return before, after

    before, after = _run(go)
    assert after is not None and (before is None or after >= before)


def test_finish_records_failure_and_the_error_text(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await jobs.finish(s, job.id, status=JOB_FAILED, error="parse blew up")
        await s.commit()
        done = await jobs.get_job(s, job.id)
        return done.status, done.error, done.finished_at is not None

    status, error, finished = _run(go)
    assert status == JOB_FAILED and "blew up" in error and finished


def test_sweep_fails_a_stale_running_job(docs):
    """A worker that died mid-job must not hold the document forever."""
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await s.execute(text(
            "UPDATE ingest_jobs SET heartbeat_at = now() - interval '1 hour'"
            " WHERE id = :i"), {"i": job.id})
        await s.commit()
        swept = await jobs.sweep_stale(s, stale_minutes=10)
        await s.commit()
        return swept, (await jobs.get_job(s, job.id)).status

    swept, status = _run(go)
    assert swept == 1 and status == JOB_FAILED


def test_sweep_leaves_a_live_job_alone(docs):
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await jobs.heartbeat(s, job.id)
        await s.commit()
        swept = await jobs.sweep_stale(s, stale_minutes=10)
        await s.commit()
        return swept, (await jobs.get_job(s, job.id)).status

    swept, status = _run(go)
    assert swept == 0 and status == JOB_RUNNING


def test_a_swept_job_frees_the_document_for_a_retry(docs):
    """The whole point of the sweep — the partial unique index must let go."""
    async def go(s):
        await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        job = await jobs.claim_next(s)
        await s.commit()
        await s.execute(text(
            "UPDATE ingest_jobs SET heartbeat_at = now() - interval '1 hour'"
            " WHERE id = :i"), {"i": job.id})
        await s.commit()
        await jobs.sweep_stale(s, stale_minutes=10)
        await s.commit()
        retry = await jobs.enqueue(s, document_id=docs["a"])
        await s.commit()
        return retry.status

    assert _run(go) == "queued"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_jobs_integration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.jobs'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/jobs.py`:

```python
"""The ingest queue — Postgres as the broker, no Redis, no Celery.

Two guarantees that are easy to conflate:

- `SELECT ... FOR UPDATE SKIP LOCKED` stops two workers claiming the same job
  ROW. That is what makes `claim_next` safe to run in N processes.
- `ux_ingest_jobs_active_document` (slice 1) stops two active JOBS existing for
  one document — SKIP LOCKED says nothing about that. `enqueue` translates the
  violation into `JobConflict`, which the router turns into 409 rather than 500.

`heartbeat_at` exists because a worker can die holding a `running` job. The
sweep fails anything whose heartbeat has gone stale, which releases the partial
unique index and lets the document be re-queued.
"""

from __future__ import annotations

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    IngestJob,
)


class JobConflict(Exception):
    """An active (queued|running) job already exists for this document."""


async def enqueue(session: AsyncSession, *, document_id: str) -> IngestJob:
    """Queue an ingest. Raises JobConflict if one is already active."""
    job = IngestJob(document_id=document_id, status=JOB_QUEUED)
    session.add(job)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise JobConflict(
            f"an ingest is already queued or running for document {document_id}"
        ) from exc
    return job


async def claim_next(session: AsyncSession) -> IngestJob | None:
    """Atomically take the oldest queued job. None when the queue is empty.

    SKIP LOCKED lets N workers poll the same table without blocking each other:
    a row another transaction holds is passed over rather than waited on.
    """
    claimed_id = (
        await session.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET status       = :running,
                       started_at   = now(),
                       heartbeat_at = now(),
                       attempts     = attempts + 1
                 WHERE id = (
                       SELECT id FROM ingest_jobs
                        WHERE status = :queued
                        ORDER BY created_at
                          FOR UPDATE SKIP LOCKED
                        LIMIT 1)
             RETURNING id
                """
            ),
            {"running": JOB_RUNNING, "queued": JOB_QUEUED},
        )
    ).scalar_one_or_none()

    if claimed_id is None:
        return None
    return await get_job(session, claimed_id)


async def get_job(session: AsyncSession, job_id: str) -> IngestJob | None:
    return (
        await session.execute(select(IngestJob).where(IngestJob.id == job_id))
    ).scalar_one_or_none()


async def heartbeat(session: AsyncSession, job_id: str) -> None:
    """Say the worker is still alive, so the sweep leaves this job alone."""
    await session.execute(
        update(IngestJob)
        .where(IngestJob.id == job_id)
        .values(heartbeat_at=func.now())
    )


async def finish(
    session: AsyncSession,
    job_id: str,
    *,
    status: str,
    error: str | None = None,
    chunks_total: int | None = None,
    chunks_done: int | None = None,
) -> None:
    values: dict = {"status": status, "finished_at": func.now(), "error": error}
    if chunks_total is not None:
        values["chunks_total"] = chunks_total
    if chunks_done is not None:
        values["chunks_done"] = chunks_done
    await session.execute(
        update(IngestJob).where(IngestJob.id == job_id).values(**values)
    )


async def set_chunks_total(session: AsyncSession, job_id: str, total: int) -> None:
    """Record how many chunks this job will embed, so `chunks_done` has a
    meaningful denominator while the worker is mid-flight."""
    await session.execute(
        update(IngestJob).where(IngestJob.id == job_id).values(chunks_total=total)
    )


async def sweep_stale(session: AsyncSession, *, stale_minutes: int) -> int:
    """Fail `running` jobs whose worker stopped heartbeating. Returns the count.

    This is what makes a killed worker recoverable: failing the job releases
    `ux_ingest_jobs_active_document`, so the document can be queued again.
    """
    result = await session.execute(
        text(
            """
            UPDATE ingest_jobs
               SET status      = :failed,
                   finished_at = now(),
                   error       = COALESCE(error,
                                 'worker stopped heartbeating; swept as stale')
             WHERE status = :running
               AND heartbeat_at < now() - make_interval(mins => :mins)
            """
        ),
        {"failed": JOB_FAILED, "running": JOB_RUNNING, "mins": stale_minutes},
    )
    return result.rowcount or 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_jobs_integration.py -v`
Expected: PASS, 13 tests

- [ ] **Step 5: Commit**

```bash
git add app/rag/jobs.py tests/test_rag_jobs_integration.py
git commit -m "feat(rag): ingest queue on Postgres — SKIP LOCKED claim, heartbeat, stale sweep"
```

---

### Task 6: Document repository + atomic replacement

**Files:**
- Create: `app/rag/documents.py`
- Create: `app/rag/ingest.py`
- Test: `tests/test_rag_ingest_integration.py`

**Interfaces:**
- `documents.py` produces: `DocumentConflict`, `async create_document(session, *, department_id, title, source, file_type, content_hash, storage_key=None, file_name=None, uploaded_by=None) -> Document`, `async get_document(session, document_id) -> Document | None`, `async list_documents(session, department_id, *, include_archived=False, ready_only=False) -> list[Document]`, `async lock_document(session, document_id) -> Document | None`, `async archive_document(session, document_id) -> bool`, `content_hash_of(data: bytes) -> str`
- `ingest.py` produces: `async replace_chunks(session, *, document_id, department_id, chunks, embeddings, embed_model, embed_dim) -> int`, `async archive_chunks(session, *, document_id) -> None`, `DocumentGone`, `CHUNK_INSERT_BATCH = 500`

**The atomicity contract.** All parsing and embedding happens *before* `BEGIN`. The database work is one short transaction: `DELETE` prior chunks → batched `INSERT`s (~500 rows/statement, same transaction) → `UPDATE documents`. On failure everything rolls back: a new document exposes zero chunks, and a re-ingest keeps serving the previous complete version until the replacement commits.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_ingest_integration.py`:

```python
"""Atomic chunk replacement against real Postgres. Skips if the DB is down."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import documents as docs_repo
from app.rag import ingest
from app.rag.chunking import Chunk
from app.rag.models import STATUS_ARCHIVED, STATUS_READY

DIM = 1536


def _vec(seed: float):
    return [seed] * DIM


def _run(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _sql(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def doc():
    _skip_if_no_db()
    tag = uuid.uuid4().hex[:8]
    doc_id = uuid.uuid4().hex

    async def setup(conn):
        dept = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'I') RETURNING id"),
            {"c": f"ing{tag}"})).scalar_one()
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'pending')"),
            {"i": doc_id, "d": dept, "h": "a" * 64})
        return dept

    dept = _sql(setup)
    yield {"id": doc_id, "dept": dept}

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id = :d"),
                           {"d": dept})
        await conn.execute(text("DELETE FROM departments WHERE id = :d"), {"d": dept})
    _sql(teardown)


def _chunks(n, prefix="chunk"):
    return [Chunk(content=f"{prefix} {i}", chunk_index=i) for i in range(n)]


def test_replace_inserts_chunks_and_marks_the_document_ready(doc):
    async def go(s):
        n = await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(3), embeddings=[_vec(0.1)] * 3,
            embed_model="m", embed_dim=DIM)
        row = await docs_repo.get_document(s, doc["id"])
        count = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()
        return n, row.status, row.chunk_count, row.embed_model, row.embed_dim, count

    n, status, chunk_count, model, dim, count = _run(go)
    assert n == 3 and count == 3
    assert status == STATUS_READY and chunk_count == 3
    assert model == "m" and dim == DIM


def test_re_ingest_replaces_rather_than_appends(doc):
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(5, "old"), embeddings=[_vec(0.1)] * 5,
            embed_model="m", embed_dim=DIM)
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(2, "new"), embeddings=[_vec(0.2)] * 2,
            embed_model="m", embed_dim=DIM)
        rows = (await s.execute(text(
            "SELECT content FROM document_chunks WHERE document_id = :d"
            " ORDER BY chunk_index"), {"d": doc["id"]})).scalars().all()
        return rows

    rows = _run(go)
    assert len(rows) == 2
    assert all(r.startswith("new") for r in rows)


def test_a_failed_replacement_leaves_the_previous_version_intact(doc):
    """The reason parse/embed happen OUTSIDE the transaction: a re-ingest that
    blows up must keep serving the last complete version."""
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(4, "good"), embeddings=[_vec(0.1)] * 4,
            embed_model="m", embed_dim=DIM)

        # Wrong-width vector -> the INSERT fails mid-transaction.
        with pytest.raises(Exception):
            await ingest.replace_chunks(
                s, document_id=doc["id"], department_id=doc["dept"],
                chunks=_chunks(3, "bad"), embeddings=[[0.1] * 999] * 3,
                embed_model="m", embed_dim=DIM)
        await s.rollback()

        rows = (await s.execute(text(
            "SELECT content FROM document_chunks WHERE document_id = :d"
            " ORDER BY chunk_index"), {"d": doc["id"]})).scalars().all()
        return rows

    rows = _run(go)
    assert len(rows) == 4
    assert all(r.startswith("good") for r in rows)


def test_mismatched_chunk_and_embedding_counts_are_rejected(doc):
    async def go(s):
        with pytest.raises(ValueError):
            await ingest.replace_chunks(
                s, document_id=doc["id"], department_id=doc["dept"],
                chunks=_chunks(3), embeddings=[_vec(0.1)] * 2,
                embed_model="m", embed_dim=DIM)
        return True

    assert _run(go) is True


def test_a_wrong_width_embedding_is_refused_before_the_insert(doc):
    async def go(s):
        with pytest.raises(ValueError):
            await ingest.replace_chunks(
                s, document_id=doc["id"], department_id=doc["dept"],
                chunks=_chunks(1), embeddings=[[0.1] * 768],
                embed_model="m", embed_dim=DIM)
        return True

    assert _run(go) is True


def test_batching_handles_more_than_one_statement_worth(doc):
    """Exercises the >500-row path in one transaction."""
    n = ingest.CHUNK_INSERT_BATCH + 25

    async def go(s):
        written = await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(n), embeddings=[_vec(0.05)] * n,
            embed_model="m", embed_dim=DIM)
        count = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()
        return written, count

    written, count = _run(go)
    assert written == n == count


def test_stored_chunks_carry_the_documents_department(doc):
    """The composite FK would reject anything else — this proves we pass it."""
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(2), embeddings=[_vec(0.1)] * 2,
            embed_model="m", embed_dim=DIM)
        return (await s.execute(text(
            "SELECT DISTINCT department_id FROM document_chunks"
            " WHERE document_id = :d"), {"d": doc["id"]})).scalars().all()

    assert _run(go) == [doc["dept"]]


def test_tsv_is_generated_for_stored_chunks(doc):
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=[Chunk(content="annual leave loans", chunk_index=0)],
            embeddings=[_vec(0.1)], embed_model="m", embed_dim=DIM)
        return (await s.execute(text(
            "SELECT tsv::text FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()

    tsv = _run(go)
    assert "loan" in tsv and "loans" not in tsv


def test_archiving_removes_chunks_but_keeps_the_row(doc):
    """Archived documents must stop being retrievable — chunks carry no status
    and HNSW filters before a join would be reachable."""
    async def go(s):
        await ingest.replace_chunks(
            s, document_id=doc["id"], department_id=doc["dept"],
            chunks=_chunks(3), embeddings=[_vec(0.1)] * 3,
            embed_model="m", embed_dim=DIM)
        archived = await docs_repo.archive_document(s, doc["id"])
        await s.commit()
        row = await docs_repo.get_document(s, doc["id"])
        count = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
            {"d": doc["id"]})).scalar_one()
        return archived, row.status, row.chunk_count, count

    archived, status, chunk_count, count = _run(go)
    assert archived is True
    assert status == STATUS_ARCHIVED
    assert count == 0
    assert chunk_count == 3   # retained for audit


def test_archiving_frees_the_content_hash_for_re_upload(doc):
    async def go(s):
        await docs_repo.archive_document(s, doc["id"])
        await s.commit()
        fresh = await docs_repo.create_document(
            s, department_id=doc["dept"], title="again", source="upload",
            file_type="pdf", content_hash="a" * 64)
        await s.commit()
        return fresh.id != doc["id"]

    assert _run(go) is True


def test_duplicate_content_hash_in_one_department_is_a_conflict(doc):
    async def go(s):
        with pytest.raises(docs_repo.DocumentConflict):
            await docs_repo.create_document(
                s, department_id=doc["dept"], title="dupe", source="upload",
                file_type="pdf", content_hash="a" * 64)
            await s.commit()
        return True

    assert _run(go) is True


def test_content_hash_is_stable_and_content_addressed():
    assert docs_repo.content_hash_of(b"abc") == docs_repo.content_hash_of(b"abc")
    assert docs_repo.content_hash_of(b"abc") != docs_repo.content_hash_of(b"abd")
    assert len(docs_repo.content_hash_of(b"abc")) == 64


def test_list_documents_hides_archived_by_default(doc):
    async def go(s):
        await docs_repo.archive_document(s, doc["id"])
        await s.commit()
        visible = await docs_repo.list_documents(s, doc["dept"])
        everything = await docs_repo.list_documents(
            s, doc["dept"], include_archived=True)
        return len(visible), len(everything)

    visible, everything = _run(go)
    assert visible == 0 and everything == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_ingest_integration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.documents'`

- [ ] **Step 3: Write the document repository**

Create `app/rag/documents.py`:

```python
"""Data-access for corpus documents.

Convention as elsewhere: takes an AsyncSession, does not commit. The one place
that owns a transaction deliberately is `ingest.replace_chunks`.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import STATUS_ARCHIVED, STATUS_READY, Document


class DocumentConflict(Exception):
    """A non-archived document with this content already exists here."""


def content_hash_of(data: bytes) -> str:
    """sha256 of the bytes (or of typed text encoded utf-8). Drives
    `ux_documents_active_content`, which makes re-upload idempotent."""
    return hashlib.sha256(data).hexdigest()


async def create_document(
    session: AsyncSession,
    *,
    department_id: int,
    title: str,
    source: str,
    file_type: str,
    content_hash: str,
    storage_key: str | None = None,
    file_name: str | None = None,
    uploaded_by: int | None = None,
) -> Document:
    """Insert a `pending` document. Raises DocumentConflict when a non-archived
    document with the same content already exists in this department."""
    doc = Document(
        department_id=department_id,
        title=title,
        source=source,
        file_type=file_type,
        content_hash=content_hash,
        storage_key=storage_key,
        file_name=file_name,
        uploaded_by=uploaded_by,
    )
    session.add(doc)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DocumentConflict(
            "a document with identical content already exists in this department"
        ) from exc
    return doc


async def get_document(session: AsyncSession, document_id: str) -> Document | None:
    return (
        await session.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()


async def lock_document(session: AsyncSession, document_id: str) -> Document | None:
    """Fetch a document with `SELECT ... FOR UPDATE`.

    Archiving and the ingest replacement both mutate the same document and its
    chunks. Without serializing on this row they interleave and the document is
    **resurrected**: the worker parses and embeds, an admin archives (chunks
    deleted, status='archived'), then the worker's replacement commits, putting
    the chunks back and flipping status to 'ready'. Whoever takes the lock first
    wins, and the loser sees the committed outcome and acts on it.
    """
    return (
        await session.execute(
            select(Document).where(Document.id == document_id).with_for_update()
        )
    ).scalar_one_or_none()


async def list_documents(
    session: AsyncSession,
    department_id: int,
    *,
    include_archived: bool = False,
    ready_only: bool = False,
) -> list[Document]:
    """`ready_only` is the member view: a pending or failed document is not part
    of the corpus their answers can cite. Admins get everything non-archived."""
    stmt = select(Document).where(Document.department_id == department_id)
    if ready_only:
        stmt = stmt.where(Document.status == STATUS_READY)
    elif not include_archived:
        stmt = stmt.where(Document.status != STATUS_ARCHIVED)
    result = await session.execute(stmt.order_by(Document.created_at.desc()))
    return list(result.scalars())


async def archive_document(session: AsyncSession, document_id: str) -> bool:
    """Retire a document: delete every chunk, keep the row for audit.

    Chunks carry no status and HNSW filters before a join would be reachable, so
    an archived document whose chunks survived would keep being retrieved and
    cited. `chunk_count` is deliberately NOT reset — it is the audit record of
    what the document held.

    Takes `FOR UPDATE` on the document row so it cannot interleave with an
    in-flight ingest replacement (see `lock_document`). If archive commits
    first, the worker's replacement aborts; if the replacement commits first,
    archive then removes the new chunks and wins.
    """
    from .ingest import archive_chunks  # local import: ingest imports this module

    doc = await lock_document(session, document_id)
    if doc is None:
        return False
    await archive_chunks(session, document_id=document_id)
    doc.status = STATUS_ARCHIVED
    await session.flush()
    return True
```

- [ ] **Step 4: Write the atomic replacement**

Create `app/rag/ingest.py`:

```python
"""Persisting a parsed, embedded document — atomically.

Everything slow (parse, chunk, embed) happens BEFORE this module is called, so
the transaction here is short. The sequence is:

    BEGIN
      DELETE the document's existing chunks
      INSERT the new ones, batched ~500 rows per statement (SAME transaction)
      UPDATE the document: status/chunk_count/embed_model/embed_dim
    COMMIT

Batching bounds per-statement memory without giving up atomicity. On failure the
caller rolls back, and the consequences are the ones we want: a new document
exposes zero chunks rather than a partial index, and a re-ingest keeps serving
the previous complete version until the replacement commits.

`embedding NOT NULL` guarantees no chunk is ever unsearchable; this transaction
is the separate guarantee that no document is ever half-indexed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from .chunking import Chunk
from .documents import lock_document
from .models import STATUS_ARCHIVED, STATUS_READY, Document, DocumentChunk

# Rows per INSERT statement. Bounds statement size; atomicity comes from the
# surrounding transaction, not from doing it in one statement.
CHUNK_INSERT_BATCH = 500


class DocumentGone(Exception):
    """The document was archived or deleted while it was being ingested.

    Not a failure of the document — a failure of THIS job. The worker records
    the job as failed and leaves the document exactly as the archive left it.
    """


async def archive_chunks(session: AsyncSession, *, document_id: str) -> None:
    """Remove every chunk for a document (used by archive and by replacement)."""
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )


async def replace_chunks(
    session: AsyncSession,
    *,
    document_id: str,
    department_id: int,
    chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
    embed_model: str,
    embed_dim: int,
) -> int:
    """Swap in a document's chunks and mark it ready. Returns the row count.

    Does NOT commit — the worker owns the boundary so a failure anywhere in the
    sequence rolls the whole thing back.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"{len(chunks)} chunks but {len(embeddings)} embeddings"
        )
    if not chunks:
        raise ValueError("refusing to store a document with zero chunks")

    # Serialize against archive_document, and re-read the status UNDER the lock.
    # The document may have been archived while we were parsing and embedding;
    # writing chunks now would resurrect it.
    doc = await lock_document(session, document_id)
    if doc is None:
        raise DocumentGone(f"document {document_id} no longer exists")
    if doc.status == STATUS_ARCHIVED:
        raise DocumentGone(
            f"document {document_id} was archived while it was being ingested"
        )
    # Belt and braces: vector(1536) would reject this too, but failing here
    # gives a clear message instead of a constraint error mid-transaction.
    for i, vec in enumerate(embeddings):
        if len(vec) != embed_dim:
            raise ValueError(
                f"embedding {i} has {len(vec)} dimensions, expected {embed_dim}"
            )

    await archive_chunks(session, document_id=document_id)

    rows = [
        {
            "document_id": document_id,
            # Passed explicitly: the composite FK requires it to be the
            # document's own department, and Postgres enforces that.
            "department_id": department_id,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content,
            "embedding": list(vec),
            "token_count": chunk.token_count,
            "page_number": chunk.page_number,
            "section": chunk.section,
            "element_type": chunk.element_type,
        }
        for chunk, vec in zip(chunks, embeddings)
    ]

    for start in range(0, len(rows), CHUNK_INSERT_BATCH):
        await session.execute(
            DocumentChunk.__table__.insert(), rows[start : start + CHUNK_INSERT_BATCH]
        )

    await session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(
            status=STATUS_READY,
            chunk_count=len(rows),
            embed_model=embed_model,
            embed_dim=embed_dim,
            updated_at=datetime.now(timezone.utc),
        )
    )
    return len(rows)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_ingest_integration.py -v`
Expected: PASS, 13 tests

- [ ] **Step 6: Commit**

```bash
git add app/rag/documents.py app/rag/ingest.py tests/test_rag_ingest_integration.py
git commit -m "feat(rag): document repository + atomic chunk replacement transaction"
```

---

### Task 7: Admin document API

**Files:**
- Modify: `app/rag/schemas.py`
- Modify: `app/rag/router.py`
- Modify: `app/main.py` (mount the ingest-jobs router)
- Create: `app/rag/jobs_router.py`
- Test: `tests/test_rag_documents_api.py`

**Interfaces:**

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/departments/{code}/documents` | admin | multipart upload → 202 `{document_id, job_id}` |
| `POST` | `/v1/departments/{code}/documents/text` | admin | typed text → 202 (`source='manual'`) |
| `GET` | `/v1/departments/{code}/documents` | member of dept | list (archived hidden unless `?include_archived=true`, admin only) |
| `DELETE` | `/v1/departments/{code}/documents/{id}` | admin | archive: 204, chunks removed, row retained |
| `GET` | `/v1/ingest-jobs/{id}` | admin | poll progress |

Status codes that must be exact: **413** over `upload_max_bytes`, **400** unsupported extension, **409** duplicate content hash, **409** ingest already active, **404** unknown department/document.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_documents_api.py`:

```python
"""Corpus document admin API. Real Postgres + TestClient; skips if the DB is down.

The API process never parses or embeds — it writes two rows and returns 202, so
these tests need no Ollama and no Docling.
"""

import io
import uuid

import pytest
from starlette.testclient import TestClient

from app.main import app

PASSWORD = "supersecret123"


def _auth(client, email):
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _me(client, headers):
    return client.get("/users/me", headers=headers).json()


@pytest.fixture()
def env():
    with TestClient(app) as client:
        admin = _auth(client, "admin@example.com")
        if _me(client, admin).get("role") != "admin":
            pytest.skip("admin@example.com is not an admin in this database")
        member = _auth(client, f"docs-member-{uuid.uuid4().hex[:8]}@example.com")
        uid = _me(client, member)["id"]
        code = f"docs{uuid.uuid4().hex[:6]}"
        client.post("/v1/departments", json={"code": code, "name": "Docs"},
                    headers=admin)
        client.post(f"/v1/departments/{code}/members", json={"user_id": uid},
                    headers=admin)
        yield client, admin, member, code


def _upload(client, headers, code, name, data, ctype="text/csv"):
    return client.post(
        f"/v1/departments/{code}/documents",
        files={"file": (name, io.BytesIO(data), ctype)},
        data={"title": "A Document"},
        headers=headers,
    )


CSV = b"Employee,Department,Days\nAlice,HR,10\nBob,HR,12\n"


def test_upload_returns_202_with_a_document_and_job_id(env):
    client, admin, _member, code = env
    resp = _upload(client, admin, code, "leave.csv", CSV)
    assert resp.status_code == 202
    body = resp.json()
    assert body["document_id"] and body["job_id"]
    assert body["status"] == "queued"


def test_uploaded_document_starts_pending_with_no_chunks(env):
    client, admin, _member, code = env
    _upload(client, admin, code, "leave.csv", CSV)
    listed = client.get(f"/v1/departments/{code}/documents", headers=admin).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "pending"
    assert listed[0]["chunk_count"] == 0


def test_member_cannot_upload(env):
    client, _admin, member, code = env
    assert _upload(client, member, code, "leave.csv", CSV).status_code == 403


def test_member_can_list_their_departments_documents(env):
    client, admin, member, code = env
    _upload(client, admin, code, "leave.csv", CSV)
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=member).status_code == 200


def test_a_non_member_cannot_list_documents(env):
    client, admin, _member, code = env
    outsider = _auth(client, f"outsider-{uuid.uuid4().hex[:8]}@example.com")
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=outsider).status_code == 403


def test_unsupported_extension_is_400(env):
    client, admin, _member, code = env
    resp = _upload(client, admin, code, "payload.exe", b"MZ", "application/octet-stream")
    assert resp.status_code == 400


def test_oversized_upload_is_413(env):
    client, admin, _member, code = env
    from app.config import get_settings
    big = b"x" * (get_settings().upload_max_bytes + 1)
    assert _upload(client, admin, code, "big.csv", big).status_code == 413


def test_duplicate_content_is_409(env):
    client, admin, _member, code = env
    assert _upload(client, admin, code, "leave.csv", CSV).status_code == 202
    assert _upload(client, admin, code, "same.csv", CSV).status_code == 409


def test_typed_text_is_accepted_as_a_manual_document(env):
    client, admin, _member, code = env
    resp = client.post(
        f"/v1/departments/{code}/documents/text",
        json={"title": "Verbal policy", "content": "Leave requests go to your lead."},
        headers=admin,
    )
    assert resp.status_code == 202
    listed = client.get(f"/v1/departments/{code}/documents", headers=admin).json()
    assert listed[0]["source"] == "manual"
    assert listed[0]["file_name"] is None


def test_empty_typed_text_is_400(env):
    client, admin, _member, code = env
    resp = client.post(f"/v1/departments/{code}/documents/text",
                       json={"title": "Empty", "content": "   "}, headers=admin)
    assert resp.status_code == 400


def test_archiving_hides_the_document_and_frees_the_hash(env):
    client, admin, _member, code = env
    doc_id = _upload(client, admin, code, "leave.csv", CSV).json()["document_id"]

    assert client.delete(f"/v1/departments/{code}/documents/{doc_id}",
                         headers=admin).status_code == 204
    assert client.get(f"/v1/departments/{code}/documents", headers=admin).json() == []
    # Same content can now be re-uploaded.
    assert _upload(client, admin, code, "leave.csv", CSV).status_code == 202


def test_include_archived_is_admin_only(env):
    client, admin, member, code = env
    doc_id = _upload(client, admin, code, "leave.csv", CSV).json()["document_id"]
    client.delete(f"/v1/departments/{code}/documents/{doc_id}", headers=admin)

    seen = client.get(f"/v1/departments/{code}/documents?include_archived=true",
                      headers=admin).json()
    assert len(seen) == 1
    assert client.get(f"/v1/departments/{code}/documents?include_archived=true",
                      headers=member).status_code == 403


def test_unknown_department_is_404(env):
    client, admin, _member, _code = env
    assert _upload(client, admin, "nope-xyz", "a.csv", CSV).status_code == 404


def test_job_status_is_pollable(env):
    client, admin, _member, code = env
    job_id = _upload(client, admin, code, "leave.csv", CSV).json()["job_id"]
    resp = client.get(f"/v1/ingest-jobs/{job_id}", headers=admin)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["chunks_done"] == 0


def test_unknown_job_is_404(env):
    client, admin, _member, _code = env
    assert client.get(f"/v1/ingest-jobs/{uuid.uuid4().hex}",
                      headers=admin).status_code == 404


def test_members_see_only_ready_documents(env):
    """A pending or failed document is not part of the corpus a member's answers
    can cite, so surfacing it only invites 'why can't the assistant see this?'."""
    client, admin, member, code = env
    _upload(client, admin, code, "leave.csv", CSV)   # stays 'pending', no worker

    assert client.get(f"/v1/departments/{code}/documents",
                      headers=admin).json() != []
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=member).json() == []


def test_member_response_omits_embed_model(env):
    client, admin, member, code = env
    _upload(client, admin, code, "leave.csv", CSV)

    admin_row = client.get(f"/v1/departments/{code}/documents",
                           headers=admin).json()[0]
    assert "embed_model" in admin_row
    # Members get the leaner shape; no operational model inventory.
    body = client.get(f"/v1/departments/{code}/documents", headers=member).json()
    assert all("embed_model" not in row for row in body)


def test_corpus_operations_reject_an_inactive_department(env):
    """Soft-disabled means gone from the product — 404, for admins too, matching
    resolve_department in slice 1."""
    client, admin, _member, code = env
    assert client.patch(f"/v1/departments/{code}", json={"is_active": False},
                        headers=admin).status_code == 200

    assert _upload(client, admin, code, "leave.csv", CSV).status_code == 404
    assert client.post(f"/v1/departments/{code}/documents/text",
                       json={"title": "T", "content": "body"},
                       headers=admin).status_code == 404
    assert client.get(f"/v1/departments/{code}/documents",
                      headers=admin).status_code == 404


def test_a_duplicate_upload_does_not_leak_a_stored_file(env, tmp_path, monkeypatch):
    """The file is written before the DB work is known to succeed, so a 409 must
    compensate — otherwise every duplicate upload orphans a file forever."""
    from app.config import get_settings

    client, admin, _member, code = env
    monkeypatch.setattr(get_settings(), "rag_docs_dir", str(tmp_path))

    assert _upload(client, admin, code, "leave.csv", CSV).status_code == 202
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    assert _upload(client, admin, code, "same.csv", CSV).status_code == 409
    after = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    assert after == before, "the rejected upload left an orphaned file behind"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_documents_api.py -v`
Expected: FAIL — the document routes 404.

- [ ] **Step 3: Add the schemas**

Append to `app/rag/schemas.py`:

```python
class TextDocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


class DocumentOut(BaseModel):
    """Member-facing. Deliberately omits `embed_model` — which model produced
    the vectors is an operations detail with no UI use, and leaking the model
    inventory to every reader buys nothing."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    department_id: int
    title: str
    source: str
    file_type: str
    file_name: str | None
    status: str
    chunk_count: int
    created_at: datetime


class DocumentAdminOut(DocumentOut):
    """Admin-facing: adds the operational fields used to manage the corpus."""

    embed_model: str | None
    embed_dim: int | None
    updated_at: datetime


class IngestAccepted(BaseModel):
    document_id: str
    job_id: str
    status: str


class IngestJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    status: str
    chunks_total: int | None
    chunks_done: int
    attempts: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None
```

- [ ] **Step 4: Add the document routes**

Append to `app/rag/router.py` (add these imports at the top of the file):

```python
from fastapi import File, Form, Query, UploadFile

from ..config import get_settings
from . import documents as docs_repo
from . import jobs as jobs_repo
from .parsing import ParseError, detect_file_type
from .schemas import (
    DocumentAdminOut,
    DocumentOut,
    IngestAccepted,
    TextDocumentCreate,
)
from .storage import delete_document, mint_storage_key, write_document
```

then the routes:

```python
async def _require_active_department(session: AsyncSession, code: str):
    """Corpus operations reject an INACTIVE department, not just an unknown one.

    404 rather than 403, and for admins too — matching `access.resolve_department`
    in slice 1. A soft-disabled department is gone from the product; ingesting
    into it or listing it would contradict that.
    """
    dept = await _require_department(session, code)
    if not dept.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department"
        )
    return dept


async def _require_department_access(session: AsyncSession, user: User, code: str):
    """Read access to a department's document list: admin, or a grant."""
    dept = await _require_active_department(session, code)
    if user.role != ROLE_ADMIN:
        allowed = await repo.has_department_access(
            session, user_id=user.id, department_id=dept.id
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this department",
            )
    return dept


async def _accept(
    session: AsyncSession, doc, *, storage_key: str, docs_dir: str
) -> IngestAccepted:
    """Queue the ingest and return 202's body. The API never parses or embeds —
    that is the worker's job, and Docling must never load in this process.

    Compensates the stored file if queuing or committing fails: the bytes were
    written before the transaction was known to succeed, so without this a
    failed enqueue leaves an orphan on disk that nothing will ever reference.
    """
    try:
        job = await jobs_repo.enqueue(session, document_id=doc.id)
        await session.commit()
    except jobs_repo.JobConflict as exc:
        delete_document(storage_key, docs_dir)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, docs_dir)
        raise
    return IngestAccepted(document_id=doc.id, job_id=job.id, status=job.status)


@router.post(
    "/{code}/documents",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    code: str,
    title: str = Form(...),
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    settings = get_settings()
    dept = await _require_active_department(session, code)

    try:
        file_type = detect_file_type(file.filename or "")
    except ParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    data = await file.read()
    if len(data) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {settings.upload_max_bytes} bytes",
        )
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="file is empty")

    storage_key = mint_storage_key(dept.code, file.filename or "document")
    write_document(data, storage_key, settings.rag_docs_dir)

    try:
        doc = await docs_repo.create_document(
            session, department_id=dept.id, title=title, source="upload",
            file_type=file_type, content_hash=docs_repo.content_hash_of(data),
            storage_key=storage_key, file_name=file.filename,
            uploaded_by=admin.id,
        )
    except docs_repo.DocumentConflict as exc:
        # Compensate: the bytes are already on disk and nothing will reference
        # them now. A duplicate upload must not leak a file.
        delete_document(storage_key, settings.rag_docs_dir)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, settings.rag_docs_dir)
        raise

    return await _accept(
        session, doc, storage_key=storage_key, docs_dir=settings.rag_docs_dir
    )


@router.post(
    "/{code}/documents/text",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_text_document(
    code: str,
    body: TextDocumentCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    """Typed-in knowledge: source='manual', no file_name, no storage_key."""
    settings = get_settings()
    dept = await _require_active_department(session, code)

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="content is empty")

    # Stored as a .txt under the same tree so the worker has one read path.
    storage_key = mint_storage_key(dept.code, "typed.txt")
    data = content.encode("utf-8")
    write_document(data, storage_key, settings.rag_docs_dir)

    try:
        doc = await docs_repo.create_document(
            session, department_id=dept.id, title=body.title, source="manual",
            file_type="text", content_hash=docs_repo.content_hash_of(data),
            storage_key=storage_key, file_name=None, uploaded_by=admin.id,
        )
    except docs_repo.DocumentConflict as exc:
        delete_document(storage_key, settings.rag_docs_dir)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        delete_document(storage_key, settings.rag_docs_dir)
        raise

    return await _accept(
        session, doc, storage_key=storage_key, docs_dir=settings.rag_docs_dir
    )


@router.get("/{code}/documents", response_model=None)
async def list_department_documents(
    code: str,
    include_archived: bool = Query(False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentAdminOut] | list[DocumentOut]:
    """Admins manage; members browse.

    A member sees only `ready` documents — a `pending` or `failed` one is not
    part of the corpus their answers can cite, and surfacing it just invites
    "why can't the assistant see this?". Admins see every non-archived document
    because managing failures is exactly their job, plus `?include_archived=`.

    `response_model=None` because the two roles genuinely return different
    shapes; FastAPI serializes whichever model is returned.
    """
    dept = await _require_department_access(session, user, code)

    if user.role != ROLE_ADMIN:
        if include_archived:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can list archived documents",
            )
        rows = await docs_repo.list_documents(session, dept.id, ready_only=True)
        return [DocumentOut.model_validate(d) for d in rows]

    rows = await docs_repo.list_documents(
        session, dept.id, include_archived=include_archived
    )
    return [DocumentAdminOut.model_validate(d) for d in rows]


@router.delete(
    "/{code}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def archive_department_document(
    code: str,
    document_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Archive: chunks removed so it stops being retrievable, row retained for
    audit. Not a delete — `documents.chunk_count` stays as the record."""
    dept = await _require_active_department(session, code)
    doc = await docs_repo.get_document(session, document_id)
    if doc is None or doc.department_id != dept.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown document")
    await docs_repo.archive_document(session, document_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 5: Add the job-status router**

Create `app/rag/jobs_router.py`:

```python
"""Ingest job progress. Separate router because the path is not under
/v1/departments — a job id is enough to identify the work."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..db.session import get_session
from ..users.models import User
from . import jobs as jobs_repo
from .schemas import IngestJobOut

router = APIRouter(prefix="/v1/ingest-jobs", tags=["departments"])


@router.get("/{job_id}", response_model=IngestJobOut)
async def get_ingest_job(
    job_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> IngestJobOut:
    job = await jobs_repo.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown ingest job")
    return IngestJobOut.model_validate(job)
```

In `app/main.py`, add the import beside the existing rag router import:

```python
from .rag.jobs_router import router as ingest_jobs_router
```

and register it after `app.include_router(departments_router)`:

```python
app.include_router(ingest_jobs_router)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_documents_api.py -v`
Expected: PASS, 19 tests

- [ ] **Step 7: Confirm the API process still has no Docling**

```bash
.venv/bin/python -c "
import sys
from app.main import app
assert 'docling' not in sys.modules, 'Docling leaked into the API import graph'
assert 'torch' not in sys.modules, 'torch leaked into the API import graph'
print('API import graph is clean')
"
```

Expected: `API import graph is clean`. If it fails, a module-scope Docling import has crept in — fix it rather than accepting it.

- [ ] **Step 8: Commit**

```bash
git add app/rag/schemas.py app/rag/router.py app/rag/jobs_router.py app/main.py \
        tests/test_rag_documents_api.py
git commit -m "feat(rag): admin document upload/list/archive + ingest job status"
```

---

### Task 8: Worker process + dependency split

**Files:**
- Create: `requirements-worker.txt`
- Create: `app/rag/worker.py`
- Test: `tests/test_rag_worker_integration.py`

**Interfaces:**
- Produces: `async preflight(client, settings) -> None`, `DocSnapshot`, `async process_job(Session, client, settings, job) -> None`, `async run_once(engine, client, settings) -> bool`, `async main() -> None`, `WorkerPreflightError`

**Preflight is the point of this task.** The worker refuses to start unless the embedding backend answers and returns a vector that truncates to exactly `rag_embed_dim`. Discovering a dimension mismatch after inserting half a corpus is far worse than refusing to boot.

- [ ] **Step 1: Create the worker dependency set**

Create `requirements-worker.txt`:

```
# Ingest-worker dependencies. NOT installed in the API image.
#
# Docling pulls ~90 packages including torch, torchvision, transformers,
# opencv and the full NVIDIA CUDA stack — several GB. The API process must
# never carry that, which is why ingestion runs as a separate process with its
# own dependency set (and its own image, when containerised).
-r requirements.txt

docling>=2.0
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_rag_worker_integration.py`:

```python
"""Worker loop against real Postgres. Embedding is faked so this runs without
Ollama; tests/test_rag_embedding_live.py covers the real backend.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import jobs, worker
from app.rag.models import (
    JOB_FAILED,
    JOB_SUCCEEDED,
    STATUS_FAILED,
    STATUS_READY,
)

DIM = 1536


class FakeEmbedClient:
    """Returns native-width vectors, like the real backend before truncation."""

    def __init__(self, native_dim=2560, fail=False):
        self.native_dim = native_dim
        self.fail = fail

    async def embeddings(self, payload):
        if self.fail:
            raise RuntimeError("embedding backend is down")
        return {
            "data": [
                {"index": i, "embedding": [0.01 * (i + 1)] * self.native_dim}
                for i, _ in enumerate(payload["input"])
            ]
        }


def _sql(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def queued_csv(tmp_path):
    """A department + a queued CSV document, file written to a temp docs dir."""
    _skip_if_no_db()
    from app.rag.storage import mint_storage_key, write_document

    tag = uuid.uuid4().hex[:8]
    doc_id = uuid.uuid4().hex
    key = mint_storage_key("wk", "leave.csv")
    write_document(
        b"Employee,Department,Days\nAlice,HR,10\nBob,HR,12\n", key, str(tmp_path)
    )

    async def setup(conn):
        dept = (await conn.execute(text(
            "INSERT INTO departments (code, name) VALUES (:c, 'W') RETURNING id"),
            {"c": f"wk{tag}"})).scalar_one()
        await conn.execute(text(
            "INSERT INTO documents (id, department_id, title, source, file_type,"
            " content_hash, status, storage_key) VALUES"
            " (:i, :d, 'T', 'upload', 'csv', :h, 'pending', :k)"),
            {"i": doc_id, "d": dept, "h": "c" * 64, "k": key})
        job_id = uuid.uuid4().hex
        await conn.execute(text(
            "INSERT INTO ingest_jobs (id, document_id, status)"
            " VALUES (:j, :i, 'queued')"), {"j": job_id, "i": doc_id})
        return dept, job_id

    dept, job_id = _sql(setup)
    yield {"dept": dept, "doc": doc_id, "job": job_id, "docs_dir": str(tmp_path)}

    async def teardown(conn):
        await conn.execute(text("DELETE FROM documents WHERE department_id = :d"),
                           {"d": dept})
        await conn.execute(text("DELETE FROM departments WHERE id = :d"), {"d": dept})
    _sql(teardown)


def _settings_with(docs_dir):
    settings = get_settings().model_copy(update={"rag_docs_dir": docs_dir})
    return settings


def _run_once(queued, client):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            return await worker.run_once(
                engine, client, _settings_with(queued["docs_dir"])
            )
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _read(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def test_run_once_returns_false_on_an_empty_queue(tmp_path):
    _skip_if_no_db()

    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            # Drain anything another test left behind, then confirm empty.
            while await worker.run_once(engine, FakeEmbedClient(),
                                        _settings_with(str(tmp_path))):
                pass
            return await worker.run_once(engine, FakeEmbedClient(),
                                         _settings_with(str(tmp_path)))
        finally:
            await engine.dispose()

    assert asyncio.run(main()) is False


def test_a_queued_document_is_parsed_embedded_and_stored(queued_csv):
    assert _run_once(queued_csv, FakeEmbedClient()) is True

    async def check(s):
        doc = (await s.execute(text(
            "SELECT status, chunk_count, embed_dim, embed_model FROM documents"
            " WHERE id = :i"), {"i": queued_csv["doc"]})).one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        job = await jobs.get_job(s, queued_csv["job"])
        return doc, chunks, job.status

    doc, chunks, job_status = _read(check)
    assert doc.status == STATUS_READY
    assert doc.chunk_count > 0 and chunks == doc.chunk_count
    assert doc.embed_dim == DIM
    assert job_status == JOB_SUCCEEDED


def test_stored_chunks_are_exactly_1536_wide(queued_csv):
    _run_once(queued_csv, FakeEmbedClient())

    async def check(s):
        return (await s.execute(text(
            "SELECT DISTINCT vector_dims(embedding) FROM document_chunks"
            " WHERE document_id = :i"), {"i": queued_csv["doc"]})).scalars().all()

    assert _read(check) == [DIM]


def test_an_embedding_failure_fails_the_job_and_stores_nothing(queued_csv):
    """The atomic transaction's whole purpose: a failure leaves zero chunks."""
    assert _run_once(queued_csv, FakeEmbedClient(fail=True)) is True

    async def check(s):
        doc = (await s.execute(text(
            "SELECT status, chunk_count FROM documents WHERE id = :i"),
            {"i": queued_csv["doc"]})).one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        job = await jobs.get_job(s, queued_csv["job"])
        return doc.status, doc.chunk_count, chunks, job.status, job.error

    status, chunk_count, chunks, job_status, error = _read(check)
    assert chunks == 0 and chunk_count == 0
    assert status == STATUS_FAILED
    assert job_status == JOB_FAILED and error


def test_a_failed_job_frees_the_document_to_be_requeued(queued_csv):
    _run_once(queued_csv, FakeEmbedClient(fail=True))

    async def requeue(s):
        job = await jobs.enqueue(s, document_id=queued_csv["doc"])
        await s.commit()
        return job.status

    assert _read(requeue) == "queued"


def test_a_failed_re_ingest_leaves_a_ready_document_untouched(queued_csv):
    """A document already serving good chunks must not be libelled `failed` by
    a later ingest that blew up — the replacement rolled back, so its previous
    chunks are still there and still correct."""
    assert _run_once(queued_csv, FakeEmbedClient()) is True   # first ingest: ready

    async def before(s):
        return (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()

    first_count = _read(before)
    assert first_count > 0

    async def requeue(s):
        await jobs.enqueue(s, document_id=queued_csv["doc"])
        await s.commit()

    _read(requeue)
    _run_once(queued_csv, FakeEmbedClient(fail=True))         # re-ingest fails

    async def after(s):
        doc = (await s.execute(text(
            "SELECT status, chunk_count FROM documents WHERE id = :i"),
            {"i": queued_csv["doc"]})).one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        return doc.status, doc.chunk_count, chunks

    status, chunk_count, chunks = _read(after)
    assert status == STATUS_READY          # NOT failed
    assert chunks == first_count           # previous version intact
    assert chunk_count == first_count


def test_an_archived_document_is_not_resurrected_by_an_in_flight_ingest(queued_csv):
    """The race: worker parses+embeds, admin archives, worker commits. Without
    the FOR UPDATE check the chunks come back and status flips to ready."""
    async def archive(s):
        from app.rag import documents as docs_repo
        await docs_repo.archive_document(s, queued_csv["doc"])
        await s.commit()

    _read(archive)                                  # archived BEFORE the worker runs
    assert _run_once(queued_csv, FakeEmbedClient()) is True

    async def after(s):
        doc = (await s.execute(text(
            "SELECT status FROM documents WHERE id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        chunks = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :i"),
            {"i": queued_csv["doc"]})).scalar_one()
        job = await jobs.get_job(s, queued_csv["job"])
        return doc, chunks, job.status, job.error

    status, chunks, job_status, error = _read(after)
    assert status == "archived"      # stayed archived
    assert chunks == 0               # NOT resurrected
    assert job_status == JOB_FAILED
    assert "archived" in (error or "").lower()


def test_the_heartbeat_advances_during_a_slow_job(queued_csv):
    """A job longer than the stale window must not be swept. The periodic
    heartbeat is what prevents that; one beat after embedding would not."""
    class SlowClient(FakeEmbedClient):
        async def embeddings(self, payload):
            await asyncio.sleep(0.35)
            return await super().embeddings(payload)

    settings = _settings_with(queued_csv["docs_dir"]).model_copy(
        update={"rag_ingest_heartbeat_seconds": 0.05}
    )

    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            return await worker.run_once(engine, SlowClient(), settings)
        finally:
            await engine.dispose()

    assert asyncio.run(main()) is True

    async def beats(s):
        job = await jobs.get_job(s, queued_csv["job"])
        return job.status, job.heartbeat_at, job.started_at

    status, heartbeat_at, started_at = _read(beats)
    assert status == JOB_SUCCEEDED
    assert heartbeat_at is not None and started_at is not None
    assert heartbeat_at > started_at   # at least one beat landed mid-flight


def test_parsing_runs_off_the_event_loop(queued_csv, monkeypatch):
    """Docling is synchronous and CPU-bound; running it inline would block the
    heartbeat. Assert it goes through asyncio.to_thread."""
    seen = {}
    real = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        seen["fn"] = getattr(fn, "__name__", str(fn))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "to_thread", spy)
    assert _run_once(queued_csv, FakeEmbedClient()) is True
    assert seen.get("fn") == "_load_chunks_sync"


def test_preflight_rejects_a_backend_returning_too_few_dimensions(tmp_path):
    async def go():
        await worker.preflight(
            FakeEmbedClient(native_dim=768), _settings_with(str(tmp_path))
        )

    with pytest.raises(worker.WorkerPreflightError):
        asyncio.run(go())


def test_preflight_accepts_a_native_width_backend(tmp_path):
    async def go():
        await worker.preflight(
            FakeEmbedClient(native_dim=2560), _settings_with(str(tmp_path))
        )
        return True

    assert asyncio.run(go()) is True


def test_preflight_rejects_an_unreachable_backend(tmp_path):
    async def go():
        await worker.preflight(
            FakeEmbedClient(fail=True), _settings_with(str(tmp_path))
        )

    with pytest.raises(worker.WorkerPreflightError):
        asyncio.run(go())
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_worker_integration.py -v`
Expected: FAIL with `ImportError: cannot import name 'worker'`

- [ ] **Step 4: Write the worker**

Create `app/rag/worker.py`:

```python
"""The ingest worker: a separate process, deliberately.

Run it with:

    .venv/bin/python -m app.rag.worker

It shares this repository and its database, but NOT the API's dependency set —
Docling drags in torch, transformers, opencv and the CUDA stack, which must
never enter the API image. Ingestion is also slow and memory-hungry, so it does
not belong in a process serving requests.

The loop is deliberately dull: sweep stale jobs, claim one with SKIP LOCKED, do
all the slow work with no transaction open, then commit one short atomic
replacement. Postgres is the queue; there is no Redis and no Celery.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from ..config import Settings, get_settings
from ..ollama.client import OllamaClient
from . import documents as docs_repo
from . import ingest
from . import jobs as jobs_repo
from .embedding import EmbeddingError, embed_texts, truncate_normalize
from .models import JOB_FAILED, JOB_SUCCEEDED, STATUS_FAILED, STATUS_READY, STATUS_ARCHIVED
from .parsing import ParseError, parse_to_chunks
from .storage import resolve_storage_path

log = logging.getLogger("rag.worker")


class WorkerPreflightError(Exception):
    """The embedding backend is unusable; refuse to start."""


async def preflight(client, settings: Settings) -> None:
    """Prove the embedding backend works and returns the expected width BEFORE
    touching any job.

    Discovering a dimension mismatch after inserting half a corpus is far worse
    than refusing to boot: `vector(1536)` would start rejecting inserts partway
    through, leaving documents half-indexed.
    """
    try:
        response = await client.embeddings(
            {"model": settings.rag_embed_model, "input": ["preflight"]}
        )
        vector = response["data"][0]["embedding"]
    except Exception as exc:  # noqa: BLE001 - any failure is fatal here
        raise WorkerPreflightError(
            f"embedding backend {settings.ollama_base_url} "
            f"({settings.rag_embed_model}) is unusable: {exc}"
        ) from exc

    try:
        truncated = truncate_normalize(vector, settings.rag_embed_dim)
    except EmbeddingError as exc:
        raise WorkerPreflightError(
            f"{settings.rag_embed_model} returned {len(vector)} dimensions; "
            f"RAG_EMBED_DIM is {settings.rag_embed_dim}. Pull the right model "
            f"(ollama pull {settings.rag_embed_model}) or fix the config."
        ) from exc

    if len(truncated) != settings.rag_embed_dim:  # pragma: no cover - defensive
        raise WorkerPreflightError("truncation did not produce the configured width")

    log.info(
        "preflight ok: %s -> %d native dims, storing %d",
        settings.rag_embed_model, len(vector), settings.rag_embed_dim,
    )


@dataclass(frozen=True)
class DocSnapshot:
    """The fields the pipeline needs, read once so no transaction stays open.

    `get_document` runs inside a session, and an SQLAlchemy session holds a
    transaction (and a pooled connection) open from its first query until commit
    or rollback. Parsing a 200-page PDF with that transaction open would pin a
    connection for minutes and hold row locks for no reason. So we snapshot,
    close, and only then do the slow work.
    """

    id: str
    department_id: int
    file_type: str
    storage_key: str | None
    status: str


async def _snapshot_document(Session, document_id: str) -> DocSnapshot | None:
    async with Session() as session:
        doc = await docs_repo.get_document(session, document_id)
        snap = (
            None
            if doc is None
            else DocSnapshot(
                id=doc.id,
                department_id=doc.department_id,
                file_type=doc.file_type,
                storage_key=doc.storage_key,
                status=doc.status,
            )
        )
        await session.rollback()  # read-only: end the transaction immediately
        return snap


def _load_chunks_sync(snap: DocSnapshot, settings: Settings):
    """Parse the stored bytes. SYNCHRONOUS and CPU-bound — Docling is not async.

    Called via `asyncio.to_thread` so it cannot block the event loop, which
    would starve the heartbeat and let the stale sweep kill a healthy job.
    """
    if not snap.storage_key:
        raise ParseError(f"document {snap.id} has no storage_key")
    path: Path = resolve_storage_path(snap.storage_key, settings.rag_docs_dir)
    if not path.exists():
        raise ParseError(f"stored file is missing: {snap.storage_key}")
    return parse_to_chunks(
        path,
        snap.file_type,
        max_chars=settings.rag_chunk_max_chars,
        overlap_chars=settings.rag_chunk_overlap_chars,
    )


async def _heartbeat_loop(Session, job_id: str, interval: float) -> None:
    """Keep saying the job is alive until cancelled.

    A single heartbeat after embedding is not enough: a large PDF can spend far
    longer than `rag_ingest_stale_minutes` in parse+embed, and the sweep would
    fail a job that is working perfectly well. Uses its own short-lived session
    per beat so it never contends with the pipeline's transactions.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with Session() as session:
                await jobs_repo.heartbeat(session, job_id)
                await session.commit()
        except Exception:  # noqa: BLE001 - a missed beat must not kill the job
            log.warning("heartbeat failed for job %s", job_id, exc_info=True)


async def _record_failure(Session, job, exc: Exception) -> None:
    """Fail the JOB; demote the DOCUMENT only if it was not already serving.

    A re-ingest that fails must leave a `ready` document exactly as it was —
    its previous chunks are still there and still correct (the replacement
    transaction rolled back), so marking it `failed` would libel a healthy
    document. Only a document that never had a good version becomes `failed`.
    An `archived` document is left alone entirely.
    """
    async with Session() as session:
        doc = await docs_repo.lock_document(session, job.document_id)
        if doc is not None and doc.status not in (STATUS_READY, STATUS_ARCHIVED):
            doc.status = STATUS_FAILED
        await jobs_repo.finish(
            session, job.id, status=JOB_FAILED, error=str(exc)[:2000]
        )
        await session.commit()
    log.warning("ingest failed for %s: %s", job.document_id, exc)


async def process_job(Session, client, settings: Settings, job) -> None:
    """Run one job to completion, recording the outcome on both rows.

    Shape of this function is the point: **no transaction is open while parsing
    or embedding.** Each DB touch is its own short session, and a background
    heartbeat runs for the whole duration.
    """
    snap = await _snapshot_document(Session, job.document_id)
    if snap is None:
        await _record_failure(Session, job, RuntimeError("document no longer exists"))
        return

    heart = asyncio.create_task(
        _heartbeat_loop(Session, job.id, settings.rag_ingest_heartbeat_seconds)
    )
    failure: Exception | None = None
    written = total = 0

    try:
        # --- slow work: NO transaction open, off the event loop ---
        chunks = await asyncio.to_thread(_load_chunks_sync, snap, settings)
        total = len(chunks)

        async with Session() as session:
            await jobs_repo.set_chunks_total(session, job.id, total)
            await session.commit()

        vectors = await embed_texts(
            client,
            [c.content for c in chunks],
            mode="document",                      # documents are embedded raw
            model=settings.rag_embed_model,
            dim=settings.rag_embed_dim,
            batch_size=settings.rag_embed_batch,
        )

        # --- short atomic replacement, its own transaction ---
        async with Session() as session:
            written = await ingest.replace_chunks(
                session,
                document_id=snap.id,
                department_id=snap.department_id,
                chunks=chunks,
                embeddings=vectors,
                embed_model=settings.rag_embed_model,
                embed_dim=settings.rag_embed_dim,
            )
            await session.commit()

    except Exception as exc:  # noqa: BLE001 - one job must never kill the loop
        # ParseError / StorageError / EmbeddingError / DocumentGone / ValueError
        # all land here, as does anything Docling raises.
        failure = exc
    finally:
        # Stop the heartbeat BEFORE writing the terminal state, so a beat cannot
        # land after the job is finished.
        heart.cancel()
        with suppress(asyncio.CancelledError):
            await heart

    if failure is not None:
        await _record_failure(Session, job, failure)
        return

    async with Session() as session:
        await jobs_repo.finish(
            session, job.id, status=JOB_SUCCEEDED,
            chunks_total=total, chunks_done=written,
        )
        await session.commit()
    log.info("ingested %s (%d chunks)", snap.id, written)


async def run_once(engine: AsyncEngine, client, settings: Settings) -> bool:
    """Sweep, claim one job, process it. True if a job was handled."""
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        await jobs_repo.sweep_stale(
            session, stale_minutes=settings.rag_ingest_stale_minutes
        )
        await session.commit()
        job = await jobs_repo.claim_next(session)
        await session.commit()

    if job is None:
        return False

    await process_job(Session, client, settings, job)
    return True


async def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    try:
        await preflight(client, settings)
        log.info("ingest worker started; polling every %.1fs",
                 settings.rag_ingest_poll_seconds)
        while not stopping.is_set():
            try:
                did_work = await run_once(engine, client, settings)
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("worker iteration failed; continuing")
                did_work = False
            if not did_work:
                try:
                    await asyncio.wait_for(
                        stopping.wait(), timeout=settings.rag_ingest_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
    finally:
        await client.aclose()
        await engine.dispose()
        log.info("ingest worker stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
```

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/pytest tests/test_rag_worker_integration.py -v
```

Expected: PASS, 12 tests. (`jobs.set_chunks_total`, which `process_job` calls, was
added and tested back in Task 5 — re-run `tests/test_rag_jobs_integration.py` too
if you skipped ahead.)

Four of these are the invariants that make the worker safe, and they are the ones
to re-read if any fail:
`test_a_failed_re_ingest_leaves_a_ready_document_untouched`,
`test_an_archived_document_is_not_resurrected_by_an_in_flight_ingest`,
`test_the_heartbeat_advances_during_a_slow_job`,
`test_parsing_runs_off_the_event_loop`.

- [ ] **Step 6: Install the worker dependencies and re-run the Docling tests**

```bash
.venv/bin/pip install -r requirements-worker.txt
.venv/bin/pytest tests/test_rag_parsing_docling.py -v
```

Expected: the 8 Docling tests now PASS instead of skipping — including the four that assert real page numbers, heading paths and element types. This is slow the first time — Docling downloads layout models on first conversion.

- [ ] **Step 7: Verify the API is still Docling-free**

Installing Docling into the dev venv does **not** make it acceptable in the API's import graph. Re-run:

```bash
.venv/bin/python -c "
import sys
from app.main import app
assert 'docling' not in sys.modules and 'torch' not in sys.modules
print('API import graph still clean')
"
```

Expected: `API import graph still clean`.

- [ ] **Step 8: Commit**

```bash
git add requirements-worker.txt app/rag/worker.py app/rag/jobs.py \
        tests/test_rag_worker_integration.py tests/test_rag_jobs_integration.py
git commit -m "feat(rag): ingest worker process + dimension preflight + worker dependency split"
```

---

### Task 9: End-to-end verification and documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `DOCKER.md` (if present; otherwise skip that step and note it)
- Test: `tests/test_rag_ingest_e2e.py`

- [ ] **Step 1: Write the end-to-end test**

Create `tests/test_rag_ingest_e2e.py`:

```python
"""Upload through the API, ingest with the worker, assert searchable chunks.

Skips unless Postgres AND the real embedding model are both available — this is
the test that proves the whole slice, so it must not be faked.
"""

import asyncio
import io
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.ollama.client import OllamaClient
from app.rag import worker

PASSWORD = "supersecret123"
CSV = b"Employee,Department,Days\nAlice,HR,10\nBob,HR,12\nCarol,HR,7\n"


@pytest.fixture(scope="module")
def model_available():
    settings = get_settings()
    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        names = {m["name"] for m in resp.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Ollama unreachable: {type(exc).__name__}")
    if settings.rag_embed_model not in names:
        pytest.skip(f"ollama pull {settings.rag_embed_model} first")
    return True


def test_upload_then_ingest_produces_searchable_chunks(model_available):
    settings = get_settings()
    code = f"e2e{uuid.uuid4().hex[:6]}"

    with TestClient(app) as client:
        try:
            client.post("/auth/register",
                        json={"email": "admin@example.com", "password": PASSWORD})
            login = client.post("/auth/login",
                                json={"email": "admin@example.com",
                                      "password": PASSWORD})
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Postgres unreachable: {type(exc).__name__}")
        if login.status_code != 200:
            pytest.skip("admin login failed")
        admin = {"Authorization": f"Bearer {login.json()['access_token']}"}
        if client.get("/users/me", headers=admin).json().get("role") != "admin":
            pytest.skip("admin@example.com is not an admin here")

        client.post("/v1/departments", json={"code": code, "name": "E2E"},
                    headers=admin)
        accepted = client.post(
            f"/v1/departments/{code}/documents",
            files={"file": ("leave.csv", io.BytesIO(CSV), "text/csv")},
            data={"title": "Leave balances"}, headers=admin,
        )
        assert accepted.status_code == 202
        doc_id = accepted.json()["document_id"]
        job_id = accepted.json()["job_id"]

        # Run the worker inline, exactly as the process would.
        async def drain():
            engine = create_async_engine(settings.database_url, poolclass=NullPool)
            ollama = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
            try:
                await worker.preflight(ollama, settings)
                for _ in range(10):
                    if not await worker.run_once(engine, ollama, settings):
                        break
            finally:
                await ollama.aclose()
                await engine.dispose()

        asyncio.run(drain())

        job = client.get(f"/v1/ingest-jobs/{job_id}", headers=admin).json()
        assert job["status"] == "succeeded", job.get("error")

        listed = client.get(f"/v1/departments/{code}/documents",
                            headers=admin).json()
        assert listed[0]["status"] == "ready"
        assert listed[0]["chunk_count"] > 0
        assert listed[0]["embed_model"] == settings.rag_embed_model

        # The chunks are genuinely searchable on BOTH channels.
        async def probe():
            engine = create_async_engine(settings.database_url, poolclass=NullPool)
            try:
                async with engine.begin() as conn:
                    dims = (await conn.execute(text(
                        "SELECT DISTINCT vector_dims(embedding) FROM document_chunks"
                        " WHERE document_id = :d"), {"d": doc_id})).scalars().all()
                    lexical = (await conn.execute(text(
                        "SELECT count(*) FROM document_chunks"
                        " WHERE document_id = :d"
                        "   AND tsv @@ websearch_to_tsquery('english', 'Alice')"),
                        {"d": doc_id})).scalar_one()
                    return dims, lexical
            finally:
                await engine.dispose()

        dims, lexical = asyncio.run(probe())
        assert dims == [1536]
        assert lexical > 0

        # Cleanup.
        client.delete(f"/v1/departments/{code}/documents/{doc_id}", headers=admin)
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_rag_ingest_e2e.py -v`
Expected: PASS with the model pulled; a clean SKIP naming the pull command otherwise.

- [ ] **Step 3: Update CLAUDE.md**

Add `rag/` detail to the Layout entry — replace the existing `rag/` clause with:

```
`rag/` (department-scoped RAG: `models` = `departments` + `user_departments` +
`documents` + `document_chunks` (pgvector `vector(1536)` + generated `tsv`) +
`ingest_jobs`, `context` = `rag_context`/`current_department` contextvar,
`access.resolve_department` = the permission boundary, `repository`/`documents` =
data access, `storage` = `storage_key` minting + traversal-safe resolution,
`chunking`/`parsing` = content → `Chunk[]` (Docling lazily, worker only),
`embedding` = query/document-aware embed + 2560→1536 + normalize, `jobs` =
Postgres queue, `ingest` = atomic replacement, `worker` = the separate ingest
process, `router`/`jobs_router` = `/v1/departments` + `/v1/ingest-jobs`).
```

Add to Endpoints:

```
`POST /v1/departments/{code}/documents` (admin, multipart → 202
`{document_id, job_id}`; 400 bad ext/empty, 409 duplicate content, 413 over cap),
`POST /v1/departments/{code}/documents/text` (admin, typed text → `source=manual`),
`GET /v1/departments/{code}/documents` (department members; `?include_archived=`
is admin-only), `DELETE /v1/departments/{code}/documents/{id}` (admin; archives —
chunks removed, row retained), `GET /v1/ingest-jobs/{id}` (admin, progress).
```

Add to Conventions / gotchas:

```
- **Ingestion runs in a SEPARATE process:** `.venv/bin/python -m app.rag.worker`.
  It shares the repo and database but not the dependency set — Docling pulls ~90
  packages including torch and the CUDA stack, which must never enter the API
  image. `requirements-worker.txt` = `-r requirements.txt` + docling. Docling is
  imported INSIDE `parsing._parse_with_docling`, never at module scope, and
  `test_docling_is_not_imported_at_module_scope` locks that.
- **The API never parses or embeds.** Upload writes a `documents` row + a queued
  `ingest_jobs` row and returns **202**. All slow work is the worker's.
- **The worker refuses to start on a dimension mismatch** (`worker.preflight`).
  Finding out after half a corpus is inserted is far worse than not booting —
  `vector(1536)` would start rejecting inserts partway through.
- **Parse/chunk/embed happen OUTSIDE the replacement transaction.** The DB work
  is `DELETE` → batched `INSERT`s (500/statement, same transaction) → `UPDATE
  documents`. A failed re-ingest keeps serving the previous complete version;
  a failed first ingest leaves zero chunks, never a half-indexed document.
- **Corpus spreadsheets are searchable but NOT aggregatable in v1.**
  `aggregate_excel` resolves through `resolve_file` → `generated_files` (per-user
  uploads); corpus documents live in `documents` under `RAG_DOCS_DIR`, so the
  resolvers are disjoint and it cannot reach them. Totals work on spreadsheets a
  user attaches to the chat. Each corpus table chunk repeats its header row so a
  chunk retrieved alone is still self-describing.
- **Qwen3-Embedding is asymmetric:** queries get an `Instruct:`/`Query:` prefix,
  documents do not. `embed_texts` requires an explicit `mode` and never defaults
  it — getting this wrong just silently degrades retrieval.
- **`/v1/embeddings` batch results are ordered by `index`, not array position.**
  `embed_texts` re-sorts; do not "simplify" that away.
```

- [ ] **Step 4: Full verification**

```bash
.venv/bin/pytest -q
.venv/bin/pytest tests/test_rag_storage.py tests/test_rag_embedding.py \
                 tests/test_rag_chunking.py tests/test_rag_parsing.py \
                 tests/test_rag_jobs_integration.py \
                 tests/test_rag_ingest_integration.py \
                 tests/test_rag_documents_api.py \
                 tests/test_rag_worker_integration.py -v
```

Expected: no regressions, and **123 slice-2 tests** pass — 16 storage + 19 embedding + 14 chunking + 17 parsing + 13 jobs + 13 ingest + 19 documents-api + 12 worker. (Collected counts: `test_rag_storage` has 13 functions with one parametrized ×4, `test_rag_parsing` has 11 with one parametrized ×7.)

A further **12 are environment-gated** and skip until the prerequisites are met: 8 Docling parsing, 3 live-embedding, 1 end-to-end. **The slice is not verified until those 8 pass** — a green run made entirely of skips proves nothing about ingestion.

Confirm the routes are mounted (Starlette lazy-mount caveat — check `/openapi.json`, never `isinstance` on `app.routes`):

```bash
.venv/bin/python -c "
from starlette.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    paths = [p for p in c.get('/openapi.json').json()['paths']
             if 'document' in p or 'ingest' in p]
    print('\n'.join(sorted(paths)))
"
```

Expected: `/v1/departments/{code}/documents`, `/v1/departments/{code}/documents/text`, `/v1/departments/{code}/documents/{document_id}`, `/v1/ingest-jobs/{job_id}`.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md tests/test_rag_ingest_e2e.py
git commit -m "docs+test(rag): slice 2 end-to-end ingest verification and conventions"
```

---

## Evaluation & Improvement

Slice 2 produces no user-facing answers, so it is measured on **ingestion fidelity** — whether what went in is what became searchable.

**1. Success metric.** Ingest success rate: share of queued jobs reaching `succeeded` without manual intervention. Secondary: chunk yield per document type (a PDF producing 1 chunk usually means a scanned page needing OCR, which v1 does not do).

**2. Eval.** A fixture corpus of 8 documents committed under `tests/fixtures/rag_corpus/` — 2 PDFs (one text-layer, one with a table), 2 DOCX, 2 XLSX (one multi-sheet), 1 CSV, 1 typed text — each with an expected chunk-count range and 3 substrings that must appear in some chunk. Scored by a script asserting every document reaches `ready` and every expected substring is retrievable via `tsv @@ websearch_to_tsquery`. Record the baseline at implementation; no pass rate yet.

**3. Feedback capture.** `ingest_jobs.error` is the durable record of every failure, with `attempts` distinguishing transient from persistent. A weekly query over `status='failed'` grouped by error prefix is the queue of parser bugs to fix. `documents.embed_model` / `embed_dim` identify documents holding stale vectors after a model change.

**4. Review loop.** Monthly review of failed jobs and of documents whose chunk yield falls outside the fixture-derived range. Hard gate: any change to chunking parameters, the parser, or the embedding model re-runs the fixture corpus, and a regression in document-reaches-ready or substring-retrievable blocks the change. Re-embedding after a model change is a backfill, not a migration — enqueue every `ready` document whose `embed_model` differs from the configured one.

## Out of scope — do not build in this slice

- The retrieval query, RRF fusion, the reranker, `search_department_docs`.
- Wiring `rag_context`/`resolve_department` into `/v1/chat`, and the slice-3
  change that makes `department` optional on an existing bound session.
- `rag_queries` / `rag_feedback` and the retrieval eval set.
- OCR for scanned PDFs (`parse_with_docling` raises a clear error naming this).
- A `docling-serve` HTTP service — the worker imports Docling directly, by decision.
- Redis, Celery, or any queue other than `ingest_jobs` + `FOR UPDATE SKIP LOCKED`.
- Re-embedding backfill tooling (the query is noted above; the tool is later).
- Corpus spreadsheet aggregation via `aggregate_excel`.
- Docker/compose changes for the worker image beyond documenting the split.

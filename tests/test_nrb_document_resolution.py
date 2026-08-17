"""Where an NRB document's bytes come from at parse time.

Since the `RAG_DOCS_DIR` decision (docs/nrb-integration.md §28), an NRB-origin
document is NOT copied into `RAG_DOCS_DIR`; it is resolved directly from the
content-addressed NRB filestore. This locks the three properties that makes that
safe:

  1. an NRB-origin snapshot resolves to the FILESTORE blob, keyed by its content
     hash — never under `RAG_DOCS_DIR`;
  2. a legacy NRB row (one created under the OLD copy scheme, whose `storage_key`
     is a `RAG_DOCS_DIR` uuid key) STILL resolves, because resolution reconstructs
     the filestore key from the content hash and ignores `storage_key` entirely —
     so the frozen 31-document p4 cohort survives with no migration and no
     re-ingest;
  3. a generic (non-NRB) document is unaffected and still resolves under
     `RAG_DOCS_DIR`.

Pure unit tests: `_document_path` is synchronous and needs no database. The
filestore base is pointed at a tmp dir via `NRB_FILES_DIR` through the real
`filestore.base_dir()`, so `storage_key_for` + `resolve_path` run for real.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.nrb import filestore
from app.rag.parsing import ParseError
from app.rag.worker import DocSnapshot, _document_path


def _write_blob(base, body: bytes, extension: str = "pdf") -> str:
    sha = hashlib.sha256(body).hexdigest()
    key = filestore.storage_key_for(sha, extension)
    path = base / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return sha


def _settings(rag_dir) -> SimpleNamespace:
    # `_document_path` reads only `.rag_docs_dir`, and only on the generic branch.
    return SimpleNamespace(rag_docs_dir=str(rag_dir))


def test_an_nrb_document_resolves_from_the_filestore_not_rag_docs_dir(
    tmp_path, monkeypatch
):
    files = tmp_path / "nrb_files"
    monkeypatch.setattr(filestore, "base_dir", lambda: files)
    sha = _write_blob(files, b"a nrb circular")

    snap = DocSnapshot(
        id="doc-nrb",
        department_id=1,
        file_type="pdf",
        storage_key=f"{sha[:2]}/{sha}.pdf",   # the real NRB key (new-scheme rows)
        status="pending",
        content_hash=sha,
        meta={"origin": "nrb"},
    )

    path = _document_path(snap, _settings(tmp_path / "rag"))
    # Resolved under the FILESTORE, by content hash — never the rag tree.
    assert path == (files / f"{sha[:2]}/{sha}.pdf").resolve()
    assert path.read_bytes() == b"a nrb circular"
    assert not (tmp_path / "rag").exists()


def test_a_legacy_nrb_row_with_a_rag_style_storage_key_still_resolves(
    tmp_path, monkeypatch
):
    """The 31-document p4 cohort was ingested under the copy scheme.

    Those rows carry a `RAG_DOCS_DIR` uuid `storage_key`, not a filestore key.
    Resolution must ignore `storage_key` for NRB and reconstruct from the content
    hash, or the cohort would break on any re-ingest — and the brief forbids
    re-running it.
    """
    files = tmp_path / "nrb_files"
    monkeypatch.setattr(filestore, "base_dir", lambda: files)
    sha = _write_blob(files, b"legacy cohort blob")

    snap = DocSnapshot(
        id="doc-legacy",
        department_id=1,
        file_type="pdf",
        storage_key="nrb-p7/deadbeefdeadbeefdeadbeefdeadbeef.pdf",  # OLD rag key
        status="pending",
        content_hash=sha,
        meta={"origin": "nrb"},
    )

    path = _document_path(snap, _settings(tmp_path / "rag"))
    assert path.read_bytes() == b"legacy cohort blob"


def test_a_missing_nrb_blob_is_a_parse_error_naming_the_filestore_key(
    tmp_path, monkeypatch
):
    files = tmp_path / "nrb_files"
    files.mkdir()
    monkeypatch.setattr(filestore, "base_dir", lambda: files)
    sha = hashlib.sha256(b"never written").hexdigest()

    snap = DocSnapshot(
        id="doc-gone",
        department_id=1,
        file_type="pdf",
        storage_key=None,
        status="pending",
        content_hash=sha,
        meta={"origin": "nrb"},
    )

    with pytest.raises(ParseError) as exc:
        _document_path(snap, _settings(tmp_path / "rag"))
    assert sha[:2] in str(exc.value)


def test_a_generic_document_is_unaffected_and_resolves_under_rag_docs_dir(
    tmp_path,
):
    rag = tmp_path / "rag"
    (rag / "hr").mkdir(parents=True)
    (rag / "hr" / "x.txt").write_bytes(b"a normal upload")

    snap = DocSnapshot(
        id="doc-generic",
        department_id=1,
        file_type="txt",
        storage_key="hr/x.txt",
        status="pending",
        content_hash="whatever",
        meta={},                       # no origin → generic
    )

    path = _document_path(snap, _settings(rag))
    assert path == (rag / "hr" / "x.txt").resolve()
    assert path.read_bytes() == b"a normal upload"

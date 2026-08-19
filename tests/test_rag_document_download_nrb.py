"""An NRB citation's download must read the NRB filestore, not RAG_DOCS_DIR.

§28 removed the per-corpus copy: an NRB document's bytes exist ONCE, content-
addressed under NRB_FILES_DIR, and `documents.storage_key` holds the FILESTORE
key. Resolving that key under RAG_DOCS_DIR yields a path that does not exist, so
the route would 404 every NRB source while the document is listed as ready — the
§18 failure shape, where nothing errors and nothing is served.

The key is RECONSTRUCTED from the content hash rather than read from
`storage_key`, exactly as `worker._document_path` does, because a row minted under
the old copy scheme carries a RAG_DOCS_DIR-style key that no longer points at
anything.
"""

import hashlib

import pytest

from app.rag.router import _document_path
from app.rag.storage import StorageError


class FakeDoc:
    def __init__(self, **kw):
        self.storage_key = kw.get("storage_key")
        self.file_type = kw.get("file_type", "pdf")
        self.content_hash = kw.get("content_hash", "")
        self.meta = kw.get("meta", {})


class FakeSettings:
    def __init__(self, docs_base):
        self.rag_docs_base = str(docs_base)


def _blob(tmp_path, monkeypatch, payload: bytes, ext: str = "pdf"):
    """Put `payload` in a throwaway filestore and return (digest, path)."""
    from app.nrb import filestore

    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path / "nrb_files")
    path = tmp_path / "nrb_files" / digest[:2] / f"{digest}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return digest, path


def test_an_nrb_document_resolves_into_the_filestore(tmp_path, monkeypatch):
    digest, blob = _blob(tmp_path, monkeypatch, b"%PDF-1.4 nrb")
    doc = FakeDoc(
        storage_key=f"{digest[:2]}/{digest}.pdf",
        content_hash=digest,
        meta={"origin": "nrb"},
    )
    assert _document_path(doc, FakeSettings(tmp_path / "rag_documents")) == blob


def test_an_nrb_row_minted_under_the_old_copy_scheme_still_resolves(tmp_path, monkeypatch):
    """The key comes from the hash, so a legacy RAG_DOCS_DIR-style storage_key on
    an NRB row is ignored rather than followed — no migration, no re-ingest."""
    digest, blob = _blob(tmp_path, monkeypatch, b"%PDF-1.4 legacy row")
    doc = FakeDoc(
        storage_key="nrb-p7/0123456789abcdef.pdf",  # the pre-§28 copy scheme
        content_hash=digest,
        meta={"origin": "nrb"},
    )
    assert _document_path(doc, FakeSettings(tmp_path / "rag_documents")) == blob


def test_an_nrb_document_falls_back_to_the_metadata_blob_hash(tmp_path, monkeypatch):
    """`worker._document_path` accepts either identity; so must this, or a row
    whose content_hash was never backfilled becomes unservable."""
    digest, blob = _blob(tmp_path, monkeypatch, b"%PDF-1.4 meta hash")
    doc = FakeDoc(content_hash="", meta={"origin": "nrb", "blob_sha256": digest})
    assert _document_path(doc, FakeSettings(tmp_path / "rag_documents")) == blob


def test_an_ordinary_upload_still_resolves_under_the_corpus_tree(tmp_path):
    doc = FakeDoc(storage_key="hr/abc.pdf", meta={})
    docs_base = tmp_path / "rag_documents"
    assert _document_path(doc, FakeSettings(docs_base)) == docs_base / "hr" / "abc.pdf"


def test_a_non_nrb_document_without_a_storage_key_is_refused(tmp_path):
    """Typed-in documents always have a minted key; a row without one has no
    bytes to serve and must not silently resolve to the base directory."""
    with pytest.raises(StorageError):
        _document_path(FakeDoc(storage_key=None, meta={}), FakeSettings(tmp_path))


def test_an_nrb_document_with_no_usable_digest_is_refused(tmp_path, monkeypatch):
    """A hash is the only identity an NRB blob has. Refuse rather than build a
    path out of an empty string."""
    from app.nrb import filestore

    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path / "nrb_files")
    with pytest.raises(Exception):
        _document_path(
            FakeDoc(content_hash="", meta={"origin": "nrb"}), FakeSettings(tmp_path)
        )

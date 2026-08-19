"""The corpus base directory must not depend on the process's working directory.

`RAG_DOCS_DIR` is shared by TWO processes — the API writes the bytes, the ingest
worker reads them — and its default is the relative `"rag_documents"`. Resolved
with a bare `Path(value).resolve()`, that means "relative to wherever this
process happened to be started", so a gateway in Docker (`WORKDIR /app`) and a
worker started by hand from a repo checkout silently disagree about where the
corpus lives. The DB row is written either way, so the mismatch does not surface
until the worker fails a job with `stored file is missing: <key>`.

Anchoring relative values to the project root removes the whole class: both
processes compute the same absolute path no matter how they were launched.
"""

import os
from pathlib import Path

from app.config import PROJECT_ROOT, Settings

BASE_ENV = {
    "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
    "jwt_secret": "test-secret",
}


def settings_with(**overrides) -> Settings:
    return Settings(**BASE_ENV, **overrides)


def test_relative_docs_dir_anchors_to_project_root():
    """The default is relative; it must mean <repo>/rag_documents, not <cwd>/…"""
    settings = settings_with(rag_docs_dir="rag_documents")
    assert settings.rag_docs_base == str(PROJECT_ROOT / "rag_documents")


def test_absolute_docs_dir_passes_through_unchanged():
    """A deployment that pins an absolute path gets exactly that path."""
    settings = settings_with(rag_docs_dir="/srv/local-ai/rag_documents")
    assert settings.rag_docs_base == "/srv/local-ai/rag_documents"


def test_docs_base_is_independent_of_working_directory(tmp_path):
    """The regression test for the actual bug.

    Same config, two different working directories, one answer. Before the fix
    these differed, which is exactly how the gateway and the worker ended up
    reading different filesystems.
    """
    settings = settings_with(rag_docs_dir="rag_documents")

    original = Path.cwd()
    try:
        os.chdir(tmp_path)
        from_tmp = settings.rag_docs_base
        os.chdir(PROJECT_ROOT)
        from_root = settings.rag_docs_base
    finally:
        os.chdir(original)

    assert from_tmp == from_root


def test_docs_base_is_absolute():
    assert Path(settings_with().rag_docs_base).is_absolute()

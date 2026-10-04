"""Re-ingest exactly the named documents (spec §8.2c)."""

from __future__ import annotations

import inspect

from app.rag import reingest


def test_reingest_accepts_document_ids():
    assert "document_ids" in inspect.signature(reingest.reingest).parameters


def test_the_cli_reads_an_ids_file(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("a1\n\nb2\n")
    assert reingest.read_ids_file(ids) == ["a1", "b2"]

"""Before/after proof that the repair changed exactly the detected documents (spec §8.2d)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nrb_corpus_fingerprint as F  # noqa: E402


def _doc(digest, routes=("native",), status="ready", chunks=3):
    return {"status": status, "chunks": chunks, "digest": digest, "routes": list(routes)}


def test_exactly_the_expected_documents_changed():
    before = {"documents": {"a": _doc("1"), "b": _doc("2")}}
    after = {"documents": {"a": _doc("9"), "b": _doc("2")}}
    out = F.compare(before, after, {"a"})
    assert out["ok"] and out["changed"] == ["a"]


def test_an_unexpected_change_fails():
    before = {"documents": {"a": _doc("1"), "b": _doc("2")}}
    after = {"documents": {"a": _doc("9"), "b": _doc("7")}}
    out = F.compare(before, after, {"a"})
    assert not out["ok"] and out["unexpected"] == ["b"]


def test_a_route_change_fails_even_when_expected():
    before = {"documents": {"a": _doc("1", ("native",))}}
    after = {"documents": {"a": _doc("9", ("native", "ocr"))}}
    assert not F.compare(before, after, {"a"})["ok"]


def test_an_expected_document_that_did_not_change_fails():
    before = {"documents": {"a": _doc("1")}}
    after = {"documents": {"a": _doc("1")}}
    assert F.compare(before, after, {"a"})["unchanged_expected"] == ["a"]

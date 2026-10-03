"""native-3 extraction rows: native-2's verdict, plus the repair's evidence (spec §3.2)."""

from __future__ import annotations

import pytest

pytest.importorskip("uharfbuzz")

from app.nrb import extraction  # noqa: E402
from tests import nrb_font_pdf as W  # noqa: E402


def test_native3_classifies_exactly_as_native2_and_adds_only_evidence(tmp_path):
    path = W.word_pdf(tmp_path, W.LOHIT.read_bytes())
    v2 = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")
    v3 = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-3")
    assert (v3.status, v3.reason, v3.text) == (v2.status, v2.reason, v2.text)
    for key, value in v2.metrics.items():
        if key != "duration_ms":
            assert v3.metrics[key] == value, key
    evidence = v3.metrics["native3"]
    assert evidence["detection"]["suspect"] is True
    assert evidence["statuses"] == {"font_tables": 1}


def test_native3_is_a_supported_version():
    assert "native-3" in extraction.SUPPORTED_EXTRACTOR_VERSIONS

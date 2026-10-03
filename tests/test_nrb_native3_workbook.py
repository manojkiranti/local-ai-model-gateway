"""The reader sheet's pure parts (spec §6.4)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nrb_native3_review_import as I  # noqa: E402
import nrb_native3_workbook as WB  # noqa: E402


def test_cluster_classes_find_each_risky_shape():
    assert WB.cluster_classes("गर्दा") == {"reph"}
    assert WB.cluster_classes("प्रकृति") == {"rakar", "prebase"}
    assert WB.cluster_classes("क्षेत्र") == {"conjunct", "rakar"}
    assert WB.cluster_classes("नेपाल") == {"plain"}


def test_word_version_reads_the_producer():
    assert WB.word_version("Microsoft® Word 2013") == "Word 2013"
    assert WB.word_version("Microsoft® Office Word 2007") == "Word 2007"
    assert WB.word_version("Microsoft® Word for Microsoft 365") == "Word 365"
    assert WB.word_version("Acrobat Distiller 10.0") == "other"


def test_the_excerpt_window_is_at_most_eight_lines_around_the_hit():
    assert WB.window(list("abcdefghijklmnop"), 7) == (5, 13)
    assert WB.window(list("abc"), 0) == (0, 3)


def test_wrong_words_are_counted_from_the_list_not_guessed():
    assert I.count_wrong("correct", "") == 0
    assert I.count_wrong("wrong words (list them)", "अकोर्, पनेर्छ") == 2
    assert I.count_wrong("wrong throughout", "") is None
    assert I.count_wrong("", "") is None


def test_the_pass_criterion_is_wer_and_no_class_wrong_twice():
    rows = [
        {"source": "cohort", "cluster": "reph", "verdict": "correct", "wrong": "", "repaired": "क " * 400},
        {"source": "cohort", "cluster": "reph", "verdict": "wrong words (list them)", "wrong": "अकोर्",
         "repaired": "क " * 400},
        {"source": "production", "cluster": "reph", "verdict": "wrong words (list them)", "wrong": "x y",
         "repaired": "क"},
    ]
    out = I.score(rows)
    assert out["evidence_rows"] == 2 and out["wrong_words"] == 1
    assert out["wer"] == round(1 / 800, 5)
    assert out["passed"] is True
    rows.append({"source": "cohort", "cluster": "reph", "verdict": "wrong words (list them)",
                 "wrong": "पनेर्छ", "repaired": "क"})
    assert I.score(rows)["passed"] is False

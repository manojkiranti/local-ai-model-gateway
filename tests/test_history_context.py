"""Pure (no-DB, no-GPU) tests for the history context budget.

Split out of repository.py for the same reason app/rag/ranking.py is split from
access.py: the code deciding what the model does and does not see should be
provable with no database and no model server.
"""

from __future__ import annotations

from app.history.context import estimate_tokens


def test_devanagari_costs_more_tokens_than_latin_of_equal_length():
    # THE test for this module. A `len(text)/4` estimator passes every other
    # test in this file and still under-counts a Nepali thread into an
    # overflow, which is the bug the budget exists to prevent.
    latin = "a" * 100
    devanagari = "क" * 100
    assert estimate_tokens(devanagari) > estimate_tokens(latin)


def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


def test_estimate_grows_with_length():
    assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)


def test_mixed_script_is_between_the_two_pure_cases():
    latin = "a" * 200
    devanagari = "क" * 200
    mixed = "a" * 100 + "क" * 100
    assert estimate_tokens(latin) < estimate_tokens(mixed) < estimate_tokens(devanagari)

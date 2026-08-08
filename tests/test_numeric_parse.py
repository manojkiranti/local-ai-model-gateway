"""The numeric coercion table — the trust-critical piece of aggregate_excel.

Every spreadsheet cell reaches us as display text, so a wrong answer here is a
wrong total downstream. Table-driven so adding a real-world format found in
production is a one-line change.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.files.numeric import parse_number

PARSES = [
    ("1234", Decimal("1234")),
    ("1,234.50", Decimal("1234.50")),
    ("$1,234.50", Decimal("1234.50")),
    ("£99", Decimal("99")),
    ("€1,000", Decimal("1000")),
    ("-$5", Decimal("-5")),
    ("$-5", Decimal("-5")),
    ("(500)", Decimal("-500")),
    ("($1,200.25)", Decimal("-1200.25")),
    ("12%", Decimal("0.12")),
    ("  42  ", Decimal("42")),
    ("1 234", Decimal("1234")),          # plain-space thousands separator
    ("1\u00a0234", Decimal("1234")),   # non-breaking space
    ("1\u202f234", Decimal("1234")),   # narrow no-break space
    ("0", Decimal("0")),
    ("1e3", Decimal("1000")),
    ("3.5", Decimal("3.5")),
]

REJECTS = [
    "", "   ", "N/A", "n/a", "see note 3", "TBC", "-", "1.2.3", "$", "%",
    "nan", "NaN", "Infinity", "-inf",     # Decimal() accepts these — must NOT
]


@pytest.mark.parametrize("text,expected", PARSES)
def test_parses(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", REJECTS)
def test_rejects(text):
    assert parse_number(text) is None


def test_percent_of_negative():
    assert parse_number("(12%)") == Decimal("-0.12")


def test_returns_decimal_not_float():
    # 0.1 + 0.2 must be exact when accumulated downstream.
    assert parse_number("0.1") + parse_number("0.2") == Decimal("0.3")


def test_comma_is_always_a_thousands_separator():
    # European decimal-comma is explicitly NOT supported; "1,5" is 15, not 1.5.
    # Documented so a future reader knows this is a decision, not an oversight.
    assert parse_number("1,5") == Decimal("15")

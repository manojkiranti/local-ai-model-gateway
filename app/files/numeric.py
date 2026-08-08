"""Coerce a spreadsheet cell's display text back into a number.

Every cell reaches the aggregator as a string (`readers._cell` stringifies
everything), so summing a column means parsing numbers back out of human
formatting: "$1,234.50", "(500)", "12%", "1 234".

Pure string manipulation into `Decimal` — **never `eval`**, matching
`calculator.py`. `Decimal` rather than `float` because these are money figures
and float accumulation drifts (1204299.9999998).

Blank is NOT special-cased here: "" returns None like any other non-number. The
caller distinguishes 'absent' from 'unparseable' — see aggregate.py.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_CURRENCY = "$€£₹¥"
# plain, non-breaking and narrow-no-break spaces all show up as thousands
# separators in exported sheets.
_SPACES = (" ", " ", " ")  # nbsp, narrow-nbsp, plain space


def parse_number(text: str) -> Decimal | None:
    """The cell's numeric value, or None if it isn't a number."""
    s = str(text).strip()
    if not s:
        return None

    negative = False
    # Accounting negatives: (500) -> -500
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    percent = s.endswith("%")
    if percent:
        s = s[:-1].strip()

    # Sign may sit either side of the currency symbol: "-$5" and "$-5".
    # Every test below guards on `s` first: "" is a substring of any string, so
    # an unguarded `s[:1] in _CURRENCY` is True once s empties — and the while
    # loop never terminates on input like "$" or "-".
    if s and s[0] in "+-":
        negative = negative or s[0] == "-"
        s = s[1:].strip()
    while s and s[0] in _CURRENCY:
        s = s[1:].strip()
    if s and s[0] in "+-":
        negative = negative or s[0] == "-"
        s = s[1:].strip()

    for ch in _SPACES:
        s = s.replace(ch, "")
    s = s.replace(",", "")
    if not s:
        return None

    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    # Decimal() happily accepts "nan"/"Infinity"; either would poison a sum.
    if not value.is_finite():
        return None

    if percent:
        value = value / Decimal(100)
    return -value if negative else value

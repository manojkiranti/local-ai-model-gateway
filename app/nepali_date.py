"""Bikram Sambat <-> Gregorian conversion, and Nepal's fiscal year.

Pure module — no DB, no HTTP, no model — for the reason `app/localtime.py` and
`app/rag/permissions.py` are pure: this decides what date a document is FROM,
and proving it should not require standing anything up. The month-length table
and its provenance live in `nepali_calendar_data.py`.

**There is no formula.** BS month lengths vary 29-32 days with no cycle (see the
data module), so both directions are day counting from a single anchor.

**Out of range REFUSES; it never extrapolates.** A date past the table cannot be
computed, and guessing would be indistinguishable from an answer — the
`app/nrb/` fail-closed rule, for the same reason: a plausible wrong date is worse
than a stated gap.

Two traps this module exists to keep out of the rest of the codebase:

  * **"Today" is Nepal time.** `today()` goes through `app.localtime`, never
    `date.today()`. From 18:15 UTC it is already tomorrow in Kathmandu — the
    live failure that put a stale year's rates in a `get_nrb_forex` answer.
  * **The fiscal year starts SHRAWAN 1, not Baisakh 1.** FY 2082/83 begins in
    BS month 4 of 2082 (AD 2025-07-17). Anchoring it to the BS new year is off
    by three and a half months and would mislabel every NRB circular.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from . import localtime
from .nepali_calendar_data import (
    ANCHOR_AD,
    ANCHOR_BS,
    FISCAL_START_MONTH,
    MONTH_LENGTHS,
    MONTH_NAMES,
    MONTH_NAMES_NEPALI,
)

__all__ = [
    "BsDate", "InvalidDate", "OutOfRange", "days_in_month", "fiscal_year",
    "fiscal_year_span", "from_ad", "parse", "supported_years", "to_ad", "today",
]


class InvalidDate(ValueError):
    """The date itself is not a real BS date (bad month, day past month end)."""


class OutOfRange(ValueError):
    """A real date, but outside the years the table covers."""


@dataclass(frozen=True, order=True)
class BsDate:
    year: int
    month: int
    day: int

    @property
    def month_name(self) -> str:
        return MONTH_NAMES[self.month - 1]

    @property
    def month_name_nepali(self) -> str:
        return MONTH_NAMES_NEPALI[self.month - 1]

    def isoformat(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.day} {self.month_name} {self.year} BS"


_ANCHOR = date(*ANCHOR_AD)
_MIN_YEAR = min(MONTH_LENGTHS)
_MAX_YEAR = max(MONTH_LENGTHS)


def supported_years() -> range:
    return range(_MIN_YEAR, _MAX_YEAR + 1)


def days_in_month(year: int, month: int) -> int:
    """Length of one BS month. Only the table knows — there is no rule."""
    if year not in MONTH_LENGTHS:
        raise OutOfRange(
            f"BS year {year} is outside the supported range "
            f"({_MIN_YEAR}-{_MAX_YEAR})"
        )
    if not 1 <= month <= 12:
        raise InvalidDate(f"BS month {month} does not exist (months are 1-12)")
    return MONTH_LENGTHS[year][month - 1]


def _days_since_anchor(bs: BsDate) -> int:
    length = days_in_month(bs.year, bs.month)  # validates year and month
    if not 1 <= bs.day <= length:
        raise InvalidDate(
            f"{bs.year}-{bs.month:02d} has {length} days, so day {bs.day} "
            "does not exist"
        )
    total = 0
    for year in range(ANCHOR_BS[0], bs.year):
        total += sum(MONTH_LENGTHS[year])
    total += sum(MONTH_LENGTHS[bs.year][: bs.month - 1])
    return total + (bs.day - 1)


def to_ad(bs: BsDate) -> date:
    """The Gregorian date of a BS date."""
    return _ANCHOR + timedelta(days=_days_since_anchor(bs))


_MAX_AD = _ANCHOR + timedelta(
    days=sum(sum(MONTH_LENGTHS[y]) for y in supported_years()) - 1
)


def from_ad(value: date) -> BsDate:
    """The BS date of a Gregorian date."""
    if value < _ANCHOR or value > _MAX_AD:
        raise OutOfRange(
            f"{value.isoformat()} is outside the supported range "
            f"({_ANCHOR.isoformat()} to {_MAX_AD.isoformat()})"
        )
    remaining = (value - _ANCHOR).days
    year = ANCHOR_BS[0]
    while remaining >= sum(MONTH_LENGTHS[year]):
        remaining -= sum(MONTH_LENGTHS[year])
        year += 1
    month = 1
    while remaining >= MONTH_LENGTHS[year][month - 1]:
        remaining -= MONTH_LENGTHS[year][month - 1]
        month += 1
    return BsDate(year, month, remaining + 1)


def today() -> BsDate:
    """Today in Nepal, as a BS date. Never `date.today()` — see the docstring."""
    return from_ad(localtime.today())


# --------------------------------------------------------------------------- #
# Fiscal year
# --------------------------------------------------------------------------- #
def fiscal_year(bs: BsDate) -> str:
    """Nepal's fiscal year for a BS date, as NRB writes it ('2082/83')."""
    start = bs.year if bs.month >= FISCAL_START_MONTH else bs.year - 1
    end = start + 1
    # Two digits is NRB's own spelling, but at a century roll '%02d' of 2100 is
    # '00', which reads back as the year 2000 and fails the consecutive check.
    # BS 2099 is AD 2042, inside the table, so this is reachable — spell the
    # year in full there rather than emit a label we would reject.
    tail = f"{end % 100:02d}" if end % 100 else str(end)
    return f"{start}/{tail}"


_FY = re.compile(r"^(\d{4})\s*[/-]\s*(\d{2}|\d{4})$")


def fiscal_year_span(label: str) -> tuple[BsDate, BsDate]:
    """First and last BS day of a fiscal year label ('2082/83', '2082-83')."""
    match = _FY.match(_ascii_digits(str(label)).strip())
    if match is None:
        raise InvalidDate(
            f"{label!r} is not a fiscal year label (expected '2082/83')"
        )
    start_year = int(match.group(1))
    tail = match.group(2)
    end_year = int(tail) if len(tail) == 4 else (start_year // 100) * 100 + int(tail)
    # A label spanning anything but two consecutive years is a typo, not a range.
    if end_year != start_year + 1:
        raise InvalidDate(
            f"{label!r} does not name two consecutive years "
            f"({start_year} then {end_year})"
        )
    end_month = FISCAL_START_MONTH - 1
    return (
        BsDate(start_year, FISCAL_START_MONTH, 1),
        BsDate(end_year, end_month, days_in_month(end_year, end_month)),
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _ascii_digits(text: str) -> str:
    """Fold Devanagari (and any other) digits to ASCII. NRB writes २०८२."""
    return "".join(
        str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch
        for ch in text
    )


def _month_aliases() -> dict[str, int]:
    """Romanisation is not standardised, so accept the spellings in the wild."""
    aliases: dict[str, int] = {}
    for index, (roman, nepali) in enumerate(zip(MONTH_NAMES, MONTH_NAMES_NEPALI), 1):
        aliases[roman.casefold()] = index
        aliases[nepali] = index
    for name, index in {
        "baishakh": 1, "baisakh": 1, "vaisakh": 1, "boishakh": 1,
        "jeth": 2, "jestha": 2, "jyestha": 2, "jeshtha": 2,
        "asar": 3, "ashadh": 3, "asadh": 3, "ashad": 3, "aashadh": 3,
        "sawan": 4, "saun": 4, "shrawan": 4, "shrawn": 4, "srawan": 4,
        "bhadau": 5, "bhadra": 5, "bhado": 5,
        "asoj": 6, "ashoj": 6, "ashwin": 6, "ashvin": 6, "aswin": 6,
        "kartik": 7, "karthik": 7, "kattik": 7,
        "mangsir": 8, "mangshir": 8, "marga": 8, "margashirsha": 8,
        "poush": 9, "push": 9, "pausha": 9, "paush": 9,
        "magh": 10, "maagh": 10,
        "falgun": 11, "phalgun": 11, "fagun": 11,
        "chaitra": 12, "chait": 12, "chaita": 12,
    }.items():
        aliases[name] = index
    return aliases


_ALIASES = _month_aliases()
_NUMERIC = re.compile(r"^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})$")
_NAMED = re.compile(r"^(\d{1,2})\s+(\S+)\s+(\d{4})$")


def parse(text: str) -> BsDate:
    """Read a BS date written as '2082-01-31', '2082/1/31' or '1 Shrawan 2082'.

    Devanagari digits are folded to ASCII first. An unrecognised month name is
    REFUSED rather than guessed — a wrong month is a 30-day error that looks
    entirely plausible.
    """
    cleaned = _ascii_digits(str(text)).strip()

    match = _NUMERIC.match(cleaned)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _validated(BsDate(year, month, day))

    match = _NAMED.match(cleaned)
    if match:
        day, name, year = match.groups()
        index = _ALIASES.get(name.casefold())
        if index is None:
            raise InvalidDate(
                f"unknown Nepali month {name!r} — expected one of: "
                f"{', '.join(MONTH_NAMES)}"
            )
        return _validated(BsDate(int(year), index, int(day)))

    raise InvalidDate(
        f"{text!r} is not a Bikram Sambat date (expected '2082-01-31' or "
        "'1 Shrawan 2082')"
    )


def _validated(bs: BsDate) -> BsDate:
    _days_since_anchor(bs)  # raises InvalidDate / OutOfRange
    return bs

"""Local tool: nepali_date — Bikram Sambat <-> Gregorian, and the fiscal year.

The model cannot do this arithmetic. BS month lengths vary 29-32 days with no
rule (see `app/nepali_calendar_data.py`), so a converted date is either looked
up in the table or invented — and an invented one is the right shape, the right
month, and wrong by weeks. Asked directly, the model produces one confidently.

Every answer states BOTH calendars and the fiscal year, because for NRB work the
fiscal year is what the catalog files a document under ('2082/83' is a category
slug, `documents.metadata`'s `fiscal_year`, and part of a circular number).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ... import nepali_date as bs
from .base import LocalToolSpec

# The last Gregorian year the table reaches; a "date" past it on the AD path
# is a BS year that was sent without to='ad'.
_LAST_AD_YEAR = bs.to_ad(bs.BsDate(max(bs.supported_years()), 1, 1)).year

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _describe(bs_date: bs.BsDate, ad_date: date) -> str:
    return (
        f"{bs_date.isoformat()} BS ({bs_date.day} {bs_date.month_name} "
        f"{bs_date.year}, {bs_date.month_name_nepali}) = "
        f"{ad_date.isoformat()} AD, {_WEEKDAYS[ad_date.weekday()]}. "
        f"Nepali fiscal year {bs.fiscal_year(bs_date)}."
    )


async def _nepali_date(args: dict[str, Any]) -> str:
    label = args.get("fiscal_year")
    if label:
        try:
            start, end = bs.fiscal_year_span(str(label))
        except (bs.InvalidDate, bs.OutOfRange) as exc:
            return f"ERROR: {exc}."
        # Echo the CANONICAL label, not the caller's spelling: NRB writes the
        # same year as '2082/83' and as the slug '2082-83'.
        canonical = bs.fiscal_year(start)
        return (
            f"Nepali fiscal year {canonical} runs {start.isoformat()} BS "
            f"({start.day} {start.month_name} {start.year}) to {end.isoformat()} BS "
            f"({end.day} {end.month_name} {end.year}) — "
            f"{bs.to_ad(start).isoformat()} to {bs.to_ad(end).isoformat()} AD."
        )

    raw = args.get("date")
    target = str(args.get("to") or "bs").strip().lower()
    if target not in ("bs", "ad"):
        return "ERROR: 'to' must be 'bs' (Nepali) or 'ad' (Gregorian)."

    if raw is None or not str(raw).strip():
        today_bs = bs.today()
        return "Today: " + _describe(today_bs, bs.to_ad(today_bs))

    text = str(raw).strip()
    try:
        if target == "bs":
            # The input is Gregorian; hand back the Nepali date.
            ad_date = date.fromisoformat(_ascii(text))
            if ad_date.year > _LAST_AD_YEAR:
                # Almost certainly a BS year sent on the default path. An AD
                # range error gives the model nothing to correct, so name the
                # fix instead of letting it retry or give up.
                return (
                    f"ERROR: {text!r} looks like a Bikram Sambat date, not a "
                    "Gregorian one. To convert it to Gregorian, call again with "
                    "to='ad'."
                )
            bs_date = bs.from_ad(ad_date)
        else:
            bs_date = bs.parse(text)
            ad_date = bs.to_ad(bs_date)
    except (bs.InvalidDate, bs.OutOfRange) as exc:
        return f"ERROR: {exc}."
    except ValueError:
        return (
            f"ERROR: could not read {text!r} as a "
            f"{'Gregorian' if target == 'bs' else 'Bikram Sambat'} date. "
            "Use YYYY-MM-DD (e.g. '2025-05-15' AD, or '2082-01-31' BS)."
        )
    return _describe(bs_date, ad_date)


def _ascii(text: str) -> str:
    """Devanagari digits reach this tool too — NRB writes २०८२."""
    import unicodedata

    return "".join(
        str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch
        for ch in text
    )


SPEC = LocalToolSpec(
    name="nepali_date",
    description=(
        "Convert between the Nepali (Bikram Sambat) calendar and the Gregorian "
        "one, and get Nepal's fiscal year. ALWAYS use this for a BS date — never "
        "work one out yourself, the months vary 29-32 days with no rule. Pass "
        "'date' with to='bs' to turn a Gregorian date into a Nepali one, or "
        "to='ad' for the reverse; omit 'date' for today in Nepal. Pass "
        "'fiscal_year' (e.g. '2082/83') for that year's start and end dates. "
        "The fiscal year runs Shrawan 1 to Ashar end, NOT the Nepali new year."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "YYYY-MM-DD, or '1 Shrawan 2082'. Omit for today.",
            },
            "to": {
                "type": "string",
                "enum": ["bs", "ad"],
                "description": "Calendar to convert TO (default 'bs').",
            },
            "fiscal_year": {
                "type": "string",
                "description": "A fiscal year label like '2082/83' to get its span.",
            },
        },
        "required": [],
    },
    func=_nepali_date,
)

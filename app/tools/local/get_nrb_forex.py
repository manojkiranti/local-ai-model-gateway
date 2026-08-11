"""Local tool: get_nrb_forex — official Nepal Rastra Bank exchange rates.

Argument validation and formatting live here; the HTTP call lives in
`app/nrb/client.py`. **There is no `url` parameter**, deliberately: the endpoint
is application config, so this tool cannot be steered at another host the way a
generic fetcher could. `fetch_url` keeps its SSRF guards for that job.

**No argument means today.** The date is resolved from the server clock via
`app/localtime.py` (Nepal time), never from the model. This is not a convenience:
a model has no reliable "now", so requiring a date invites it to supply one from
training data — and NRB happily answers for 2023, so the result looks right and is
years stale.

Two correctness details the output must carry:

  * **The unit.** NRB quotes NPR per unit of foreign currency and the unit is not
    always 1 (INR 100, JPY 10). A rate printed without it is off by 100x.
  * **NRB's own figures, verbatim.** Buy/sell stay the strings the API sent — no
    float round-trip, no arithmetic, no conversion. This tool reports official
    rates; it does not compute with them.

Sizing: the range caps below keep output at a few thousand characters, well under
the agent loop's MAX_TOOL_RESULT_CHARS (8000). `_budget` is a backstop for a day
NRB publishes far more currencies than today's ~22, and it announces itself — a
silent cut reads to the model as a complete answer.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ... import localtime
from ...nrb.client import ForexDay, NRBError, Rate, fetch_forex_rates
from .base import LocalToolSpec

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")

MAX_RANGE_DAYS = 31            # one month of daily rates for a single currency
MAX_DAYS_ALL_CURRENCIES = 3    # a full day is ~22 rows; a month of them is not readable
MAX_OUTPUT_CHARS = 6000        # under the loop's 8000 cap, with room for the note
MAX_CODES_LISTED = 40          # when telling the model which currencies do exist

SOURCE_LINE = "Source: Nepal Rastra Bank"
UNIT_NOTE = "Rates are NPR per the stated unit of foreign currency."
NO_ESTIMATE = "Do not estimate or calculate a rate that is not listed here."
TRUNCATION_NOTE = (
    "[TRUNCATED: only the first {shown} of {total} days are shown. Ask for a "
    "narrower date range to see the rest.]"
)


# --------------------------------------------------------------------------- #
# Validation (pure)
# --------------------------------------------------------------------------- #
def _is_absent(value: Any) -> bool:
    """True for an argument the model simply didn't supply.

    A blank string counts as absent, not as a malformed date: models emit `""`
    for "no value" as readily as they omit the key.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_date(value: Any, field: str) -> tuple[date | None, str | None]:
    """Strict YYYY-MM-DD. Returns (date, error) — exactly one is set.

    The regex is not redundant with `fromisoformat`: from 3.11 that also accepts
    'YYYYMMDD' and other ISO shapes, and NRB only understands 'Y-m-d'. Pinning the
    format here keeps the tool's contract the same on any Python we run on.
    """
    if not isinstance(value, str) or not value.strip():
        return None, f"'{field}' must be a date in YYYY-MM-DD format."
    text = value.strip()
    if not DATE_RE.match(text):
        return None, f"'{field}' must be a date in YYYY-MM-DD format (got '{text}')."
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, f"'{field}' is not a real calendar date (got '{text}')."


def _parse_currency(value: Any) -> tuple[str | None, str | None]:
    """Optional ISO3 code, uppercased. Returns (code, error); both None = absent.

    Match is exact on the ISO3 code — no fuzzy matching, because silently
    answering about a currency the user didn't ask for is worse than a clear miss.
    """
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, (
            "'currency' must be a 3-letter ISO currency code such as USD, EUR, "
            "GBP or INR, or omitted for all currencies."
        )
    text = value.strip()
    if not CURRENCY_RE.match(text):
        return None, (
            f"'currency' must be a 3-letter ISO currency code such as USD, EUR, "
            f"GBP or INR (got '{text}')."
        )
    return text.upper(), None


# --------------------------------------------------------------------------- #
# Formatting (pure)
# --------------------------------------------------------------------------- #
def _rate_of(day: ForexDay, code: str) -> Rate | None:
    for rate in day.rates:
        if rate.iso3 == code:
            return rate
    return None


def _published(day: ForexDay) -> str:
    return f" (published {day.published_on})" if day.published_on else ""


def _rate_row(rate: Rate) -> str:
    return f"{rate.iso3} | unit {rate.unit} | buy {rate.buy} | sell {rate.sell}"


def _single_day_single_currency(day: ForexDay, rate: Rate) -> str:
    return "\n".join([
        "Nepal Rastra Bank foreign exchange rate",
        "",
        f"Date: {day.date}",
        f"Currency: {rate.iso3} — {rate.name}",
        f"Unit: {rate.unit}",
        f"Buy: NPR {rate.buy} per {rate.unit} {rate.iso3}",
        f"Sell: NPR {rate.sell} per {rate.unit} {rate.iso3}",
        f"{SOURCE_LINE}{_published(day)}",
    ])


def _single_day_all_currencies(day: ForexDay) -> str:
    lines = [
        "Nepal Rastra Bank foreign exchange rates",
        f"Date: {day.date}{_published(day)}",
        UNIT_NOTE,
        "",
    ]
    lines.extend(_rate_row(rate) for rate in day.rates)
    lines.extend(["", SOURCE_LINE])
    return "\n".join(lines)


def _range_single_currency(days: list[ForexDay], code: str) -> str:
    first = _rate_of(days[0], code)
    name = first.name if first else code
    unit = first.unit if first else "1"
    header = [
        f"Nepal Rastra Bank foreign exchange rates — {code} ({name}), unit {unit}",
        f"{days[0].date} to {days[-1].date} — {len(days)} day(s) published",
        UNIT_NOTE,
        "",
    ]
    rows = []
    for day in days:
        rate = _rate_of(day, code)
        if rate is None:
            continue
        rows.append(f"{day.date} | buy {rate.buy} | sell {rate.sell}")
    return _budget(header, rows, len(rows), footer=[SOURCE_LINE])


def _range_all_currencies(days: list[ForexDay]) -> str:
    header = [
        "Nepal Rastra Bank foreign exchange rates",
        f"{days[0].date} to {days[-1].date} — {len(days)} day(s) published",
        UNIT_NOTE,
    ]
    blocks = []
    for day in days:
        block = ["", f"Date: {day.date}{_published(day)}"]
        block.extend(_rate_row(rate) for rate in day.rates)
        blocks.append("\n".join(block))
    return _budget(header, blocks, len(days), footer=["", SOURCE_LINE])


def _budget(header: list[str], entries: list[str], total: int, footer: list[str]) -> str:
    """Join header + entries + footer, dropping trailing entries to fit the budget.

    Whatever is dropped is announced. The agent loop's own cut at
    MAX_TOOL_RESULT_CHARS is silent, and a silently halved rate table reads to the
    model as the complete published set.
    """
    kept = list(entries)
    while True:
        shown = len(kept)
        note = [] if shown == total else ["", TRUNCATION_NOTE.format(shown=shown, total=total)]
        out = "\n".join([*header, *kept, *footer, *note])
        if len(out) <= MAX_OUTPUT_CHARS or len(kept) <= 1:
            return out
        kept.pop()


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
async def _get_nrb_forex(args: dict[str, Any]) -> str:
    # No date supplied = today, resolved HERE from the server clock rather than
    # left to the model. A model with no reliable sense of "now" will otherwise
    # fill the gap from training data and ask for a date years in the past — the
    # rates come back internally consistent and completely stale.
    if _is_absent(args.get("from")):
        from_date, error = localtime.today(), None
    else:
        from_date, error = _parse_date(args.get("from"), "from")
    if error:
        return f"ERROR: {error}"

    raw_to = args.get("to")
    if _is_absent(raw_to):
        to_date = from_date  # a single date is the common case
    else:
        to_date, error = _parse_date(raw_to, "to")
        if error:
            return f"ERROR: {error}"

    assert from_date is not None and to_date is not None  # both validated above
    if from_date > to_date:
        return (
            f"ERROR: 'from' ({from_date.isoformat()}) is after 'to' "
            f"({to_date.isoformat()}). Give the earlier date as 'from'."
        )

    currency, error = _parse_currency(args.get("currency"))
    if error:
        return f"ERROR: {error}"

    span = (to_date - from_date).days + 1
    if span > MAX_RANGE_DAYS:
        return (
            f"ERROR: the date range is {span} days; at most {MAX_RANGE_DAYS} days "
            f"can be requested at once. Ask for a shorter period."
        )
    if currency is None and span > MAX_DAYS_ALL_CURRENCIES:
        return (
            f"ERROR: a range of {span} days returns every currency for every day, "
            f"which is too much. Either set 'currency' (e.g. 'USD') for the whole "
            f"range, or request at most {MAX_DAYS_ALL_CURRENCIES} days."
        )

    try:
        days = await fetch_forex_rates(from_date.isoformat(), to_date.isoformat())
    except NRBError as exc:
        return f"ERROR: {exc.message}"

    period = (
        from_date.isoformat()
        if from_date == to_date
        else f"{from_date.isoformat()} to {to_date.isoformat()}"
    )
    if not days:
        return (
            f"Nepal Rastra Bank has published no foreign exchange rates for "
            f"{period}. Rates are published for each day; a future date, or one "
            f"not yet published, has none. {NO_ESTIMATE}"
        )

    # A day can be published with every currency's buy/sell null — that is how a
    # non-trading day (public holiday) looks. Such a day has nothing to report, so
    # it is dropped here rather than rendered as an empty rate table.
    unquoted = [day.date for day in days if not day.rates]
    days = [day for day in days if day.rates]
    if not days:
        dates = ", ".join(unquoted)
        return (
            f"Nepal Rastra Bank quoted no exchange rates for {period}. It "
            f"published an entry for {dates} with no rates — it does not quote "
            f"rates on every day (public holidays, for example). Ask for a "
            f"nearby trading day. {NO_ESTIMATE}"
        )

    if currency is not None:
        matching = [day for day in days if _rate_of(day, currency) is not None]
        if not matching:
            available = sorted({rate.iso3 for day in days for rate in day.rates})
            shown = ", ".join(available[:MAX_CODES_LISTED]) or "none"
            more = "" if len(available) <= MAX_CODES_LISTED else " …"
            return (
                f"Nepal Rastra Bank published no rate for '{currency}' for "
                f"{period}. Currencies published for that period: {shown}{more}. "
                f"{NO_ESTIMATE}"
            )
        if len(matching) == 1:
            day = matching[0]
            rate = _rate_of(day, currency)
            assert rate is not None  # matching is filtered on exactly this
            return _single_day_single_currency(day, rate)
        return _range_single_currency(matching, currency)

    if len(days) == 1:
        return _single_day_all_currencies(days[0])
    return _range_all_currencies(days)


SPEC = LocalToolSpec(
    name="get_nrb_forex",
    description=(
        "Get official Nepal Rastra Bank (NRB) foreign exchange BUYING and SELLING "
        "rates in NPR for a date or a date range — USD, EUR, GBP, INR and other "
        "currencies NRB publishes. ALWAYS call this tool for an NRB rate, "
        "including for today: never state a rate from memory, because remembered "
        "rates are years out of date. Omit 'from' to get TODAY's rates — the "
        "gateway fills in today's date, so you do not need get_current_time. Give "
        "'from' (YYYY-MM-DD) only for a past date, and 'to' only for a range. "
        "Rates are quoted per unit of foreign currency and the unit is not always "
        "1 (INR is per 100) — always report the unit, and the date the rates were "
        "published for. Do NOT use this for NRB monetary policy, circulars, "
        "directives, laws, regulations, notices or reports, and do not use "
        "fetch_url for NRB rates."
    ),
    parameters={
        "type": "object",
        "properties": {
            "from": {
                "type": "string",
                "description": (
                    "The date to get rates for, YYYY-MM-DD, e.g. '2026-08-10'. "
                    "OMIT for today — do not guess today's date."
                ),
            },
            "to": {
                "type": "string",
                "description": (
                    "End of the date range, YYYY-MM-DD. Omit for a single date. "
                    f"At most {MAX_RANGE_DAYS} days, or "
                    f"{MAX_DAYS_ALL_CURRENCIES} days if 'currency' is omitted."
                ),
            },
            "currency": {
                "type": "string",
                "description": (
                    "Optional 3-letter ISO currency code, e.g. 'USD', 'EUR', "
                    "'GBP', 'INR'. Omit to get every currency for the date."
                ),
            },
        },
        # Nothing is required: no argument at all means "today, all currencies",
        # which is the most common question. Requiring 'from' forced the model to
        # produce a date, and a model that does not know today's date produces a
        # wrong one.
        "required": [],
    },
    func=_get_nrb_forex,
)

"""HTTP client for Nepal Rastra Bank's official Forex API.

One package per external service with the wire format in `client.py`, like
`app/ollama` and `app/mcp`: the tool that uses this (`get_nrb_forex`) never sees
httpx, JSON or pagination, and NRB's endpoint can move without touching the
tool.

**The host is application config, never a model argument.** The base URL comes
from `NRB_API_BASE_URL` and the path is hardcoded, so this is not a second
`fetch_url` — there is nothing for a prompt injection to point at.

Three quirks of the live API, verified against it on 2026-08-10, that a
reasonable implementation would otherwise get wrong:

  1. **`page` and `per_page` are mandatory.** Omit them and the API answers with
     validation errors and `payload: null` — so they are always sent.
  2. **The HTTP status is always 200.** The real status is `status.code` in the
     body.
  3. **`status.code` is 400 for an empty-but-valid query too** (a future date, a
     reversed range) — with `errors.validation: null` and `payload: []`. So
     success is decided on `data.payload` being a LIST, not on `status.code`.
     Treating 400 as a failure would report "NRB rejected the request" for a date
     that simply has no rates published.

Response shape:

    {"status": {"code": 200},
     "errors": {"validation": null | {field: [msg, …]}},
     "data": {"payload": [{"date": "2026-08-08",
                           "published_on": "...", "modified_on": "...",
                           "rates": [{"currency": {"iso3": "USD",
                                                   "name": "U.S. Dollar",
                                                   "unit": 1},
                                      "buy": "152.04", "sell": "152.64"}]}]},
     "pagination": {"page": 1, "pages": 1, "per_page": 100, "total": 41, …}}

Rate values are kept as the STRINGS NRB sent. They are official figures — a
round-trip through float is a way to publish a number the central bank didn't.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger("app.nrb")

RATES_PATH = "/rates"
PER_PAGE = 100          # the API's maximum (it rejects more)
MAX_PAGES = 5           # backstop: bounded even if `pagination.pages` misreports
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 2_000_000
USER_AGENT = "local-ai-gateway/1.0 (+get_nrb_forex)"

__all__ = ["NRBError", "Rate", "ForexDay", "fetch_forex_rates"]


class NRBError(Exception):
    """A failure talking to, or understanding, the NRB API.

    ``message`` is written to be handed straight to the model: one sentence, no
    exception internals, no URLs. Diagnostics go to the log instead.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Rate:
    iso3: str
    name: str
    unit: str   # NPR values are quoted PER THIS MANY units (INR 100, JPY 10)
    buy: str
    sell: str


@dataclass(frozen=True)
class ForexDay:
    date: str
    published_on: str | None
    modified_on: str | None
    rates: tuple[Rate, ...]


# --------------------------------------------------------------------------- #
# Parsing (pure)
# --------------------------------------------------------------------------- #
def _validation_summary(errors: Any) -> str | None:
    """Flatten `errors.validation` into one short line, or None if there is none."""
    if not isinstance(errors, dict):
        return None
    validation = errors.get("validation")
    if not isinstance(validation, dict) or not validation:
        return None
    parts: list[str] = []
    for field, messages in list(validation.items())[:3]:
        if isinstance(messages, list) and messages:
            parts.append(f"{field}: {messages[0]}")
        elif isinstance(messages, str):
            parts.append(f"{field}: {messages}")
    return "; ".join(parts) or None


def _as_text(value: Any) -> str | None:
    """A JSON scalar as text. NRB sends `unit` as a number and rates as strings."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


UNQUOTED = "unquoted"      # NRB published the currency with null buy/sell
UNREADABLE = "unreadable"  # the entry is not shaped like a rate at all


def _parse_rate(raw: Any) -> tuple[Rate | None, str | None]:
    """One rate entry as (rate, skip_reason) — exactly one is set.

    A null `buy`/`sell` is NOT a broken response: on a non-trading day (a public
    holiday) NRB publishes every currency with both quotes null. That is an
    expected daily condition, so it is kept distinct from a genuinely malformed
    entry — logging 22 warnings for a holiday would train us to ignore the log.
    Either way no rate is produced: there is nothing to report and nothing to
    invent.
    """
    if not isinstance(raw, dict):
        return None, UNREADABLE
    currency = raw.get("currency")
    if not isinstance(currency, dict):
        return None, UNREADABLE
    iso3 = _as_text(currency.get("iso3"))
    if not iso3:
        return None, UNREADABLE
    buy = _as_text(raw.get("buy"))
    sell = _as_text(raw.get("sell"))
    if buy is None or sell is None:
        return None, UNQUOTED
    return Rate(
        iso3=iso3.upper(),
        name=_as_text(currency.get("name")) or iso3.upper(),
        unit=_as_text(currency.get("unit")) or "1",
        buy=buy,
        sell=sell,
    ), None


def _parse_day(raw: Any) -> ForexDay | None:
    if not isinstance(raw, dict):
        return None
    day = _as_text(raw.get("date"))
    if not day:
        return None
    entries = raw.get("rates")
    rates: list[Rate] = []
    unquoted = 0
    unreadable = 0
    if isinstance(entries, list):
        for entry in entries:
            rate, reason = _parse_rate(entry)
            if rate is None:
                if reason == UNQUOTED:
                    unquoted += 1
                else:
                    unreadable += 1
                continue
            rates.append(rate)
    if unreadable:
        logger.warning("NRB forex: %s — %d unreadable rate entries", day, unreadable)
    if unquoted and not rates:
        # The normal shape of a non-trading day; one line, not one per currency.
        logger.info("NRB forex: %s quoted no rates (%d currencies null)", day, unquoted)
    return ForexDay(
        date=day,
        published_on=_as_text(raw.get("published_on")),
        modified_on=_as_text(raw.get("modified_on")),
        rates=tuple(rates),
    )


def parse_rates_body(body: Any) -> tuple[list[ForexDay], int]:
    """Extract (days, total_pages) from one response body.

    Raises NRBError when the body is not a rates response at all, or when NRB
    reports a real validation failure. An empty `payload` list is a legitimate
    "nothing published" answer and returns ([], pages) — see the module docstring
    for why that cannot be decided from `status.code`.
    """
    if not isinstance(body, dict):
        raise NRBError("The Nepal Rastra Bank API returned an unexpected response.")

    data = body.get("data")
    payload = data.get("payload") if isinstance(data, dict) else None

    if not isinstance(payload, list):
        summary = _validation_summary(body.get("errors"))
        status = body.get("status")
        code = status.get("code") if isinstance(status, dict) else None
        if summary:
            logger.warning("NRB forex: request rejected (%s)", summary)
            raise NRBError(f"The Nepal Rastra Bank API rejected the request ({summary}).")
        logger.warning("NRB forex: unreadable body (status=%r)", code)
        raise NRBError("The Nepal Rastra Bank API returned an unexpected response.")

    days: list[ForexDay] = []
    for entry in payload:
        day = _parse_day(entry)
        if day is None:
            logger.warning("NRB forex: skipped an unreadable day entry")
            continue
        days.append(day)

    pagination = body.get("pagination")
    pages = pagination.get("pages") if isinstance(pagination, dict) else None
    total_pages = pages if isinstance(pages, int) and pages > 0 else 1
    return days, total_pages


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def _get_page(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Any:
    """One GET against the NRB rates endpoint, returning the decoded body.

    Redirects are NOT followed: the host is ours to decide, and a 3xx off this
    endpoint is a change we want to notice rather than chase. The body is read
    through a byte cap so an unexpected response cannot exhaust memory.
    """
    try:
        async with client.stream("GET", url, params=params) as resp:
            if resp.is_redirect:
                logger.warning(
                    "NRB forex: unexpected redirect to %r", resp.headers.get("location", "")
                )
                raise NRBError(
                    "The Nepal Rastra Bank API responded with an unexpected redirect."
                )
            if resp.status_code >= 400:
                logger.warning("NRB forex: HTTP %s from the rates endpoint", resp.status_code)
                raise NRBError(
                    f"The Nepal Rastra Bank API returned HTTP {resp.status_code}."
                )
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    logger.warning("NRB forex: response exceeded %d bytes", MAX_RESPONSE_BYTES)
                    raise NRBError(
                        "The Nepal Rastra Bank API response was unexpectedly large."
                    )
    except httpx.TimeoutException as exc:
        logger.warning("NRB forex: timeout (%s)", exc)
        raise NRBError("The Nepal Rastra Bank API timed out.") from exc
    except httpx.HTTPError as exc:
        logger.warning("NRB forex: transport error (%s: %s)", type(exc).__name__, exc)
        raise NRBError("Could not reach the Nepal Rastra Bank API.") from exc

    try:
        return json.loads(body)
    except ValueError as exc:
        logger.warning("NRB forex: undecodable JSON body (%s)", exc)
        raise NRBError(
            "The Nepal Rastra Bank API returned a response that could not be read."
        ) from exc


async def fetch_forex_rates(from_date: str, to_date: str) -> list[ForexDay]:
    """Every published day between `from_date` and `to_date` (inclusive), oldest first.

    Dates must already be validated `YYYY-MM-DD` — this is the transport, not the
    validator. Pagination is handled here so the model never sees it; it is bounded
    by MAX_PAGES so a misreported `pagination.pages` cannot loop forever. There are
    no retries: a failed turn the model can report beats an unbounded one.
    """
    base = get_settings().nrb_api_base_url.rstrip("/")
    url = f"{base}{RATES_PATH}"
    timeout = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)

    by_date: dict[str, ForexDay] = {}
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as client:
        page = 1
        while page <= MAX_PAGES:
            body = await _get_page(
                client,
                url,
                # page/per_page are REQUIRED by the API, not optional tuning.
                {"from": from_date, "to": to_date, "page": page, "per_page": PER_PAGE},
            )
            days, total_pages = parse_rates_body(body)
            for day in days:
                by_date.setdefault(day.date, day)
            if not days or page >= total_pages:
                break
            page += 1
        else:
            logger.warning(
                "NRB forex: stopped after %d pages for %s..%s", MAX_PAGES, from_date, to_date
            )

    # ISO dates sort lexicographically; don't rely on the API's ordering.
    return [by_date[key] for key in sorted(by_date)]

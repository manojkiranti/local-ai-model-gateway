"""The deployment's wall-clock date (Nepal time).

Two callers need "what day is it *here*": the agent's system prompt, which tells
the model today's date, and `get_nrb_forex`, which defaults to today when the
model doesn't supply a date. Both must agree, so the offset lives in one place.

**Why a literal offset and not `ZoneInfo("Asia/Kathmandu")`:** Nepal Standard
Time is a fixed UTC+05:45 with no DST, so there is nothing for a tz database to
tell us — and `zoneinfo` resolves against the *system* tzdata, which the slim
container images do not install. That failure would be a runtime
`ZoneInfoNotFoundError` inside a turn, not a build error.

**Why not UTC:** Nepal is 5h45m ahead, so from 18:15 UTC onwards it is already
the next day in Kathmandu. A UTC-derived "today" is the wrong date for roughly a
quarter of every day — and for NRB rates that means asking for the wrong
publication day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Nepal Standard Time. Fixed; no DST.
NPT = timezone(timedelta(hours=5, minutes=45), name="NPT")

__all__ = ["NPT", "now", "today", "today_iso"]


def now() -> datetime:
    """The current instant as Nepal local time."""
    return datetime.now(NPT)


def today() -> date:
    """Today's calendar date in Nepal."""
    return now().date()


def today_iso() -> str:
    """Today's date in Nepal as YYYY-MM-DD."""
    return today().isoformat()

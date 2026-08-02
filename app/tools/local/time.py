"""Local tool: get_current_time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import LocalToolSpec


async def _get_current_time(args: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    return f"The current UTC time is {now.isoformat(timespec='seconds')}."


SPEC = LocalToolSpec(
    name="get_current_time",
    description="Get the current date and time in UTC (ISO 8601). Takes no arguments.",
    parameters={"type": "object", "properties": {}, "required": []},
    func=_get_current_time,
)

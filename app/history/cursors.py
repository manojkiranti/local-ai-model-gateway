"""Opaque keyset cursors for chat-history pagination.

Keyset, not offset, and that is not a style choice: sessions sort by
`updated_at DESC` and every new turn bumps `updated_at`, so rows MOVE BETWEEN
PAGES while the user scrolls. Offset paging would show some sessions twice and
skip others entirely.

What keyset actually buys here: it prevents DUPLICATES (a row already returned
is never returned again on a later page of the same scroll, because the cursor
is a position, not a count). It does NOT make the scroll immune to a session
being bumped to the top mid-scroll — a session sitting below the cursor that
receives a new turn moves to page one, which is now above where the cursor
points, so THAT scroll skips it. It reappears correctly on a fresh fetch of
page one; it is not lost, only absent from the scroll already in progress. A
frontend author should read this as "no dupes, not a live feed," not as "never
misses anything."

The payload is base64 so it is opaque — the session keyset needs `id` as a
tiebreaker today because `updated_at` is not unique, and a client that never
parsed the cursor cannot break when that changes.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime


class BadCursor(Exception):
    """An undecodable cursor. The router answers 400 — never a silent page one."""


def _encode(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode(raw: str) -> str:
    if not raw:
        raise BadCursor("empty cursor")
    try:
        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise BadCursor(str(exc)) from exc


def encode_session_cursor(updated_at: datetime, id: str) -> str:
    return _encode(f"s|{updated_at.isoformat()}|{id}")


def decode_session_cursor(raw: str) -> tuple[datetime, str]:
    parts = _decode(raw).split("|")
    if len(parts) != 3 or parts[0] != "s":
        raise BadCursor("not a session cursor")
    try:
        return datetime.fromisoformat(parts[1]), parts[2]
    except ValueError as exc:
        raise BadCursor(str(exc)) from exc


def encode_seq_cursor(seq: int) -> str:
    return _encode(f"m|{seq}")


def decode_seq_cursor(raw: str) -> int:
    parts = _decode(raw).split("|")
    if len(parts) != 2 or parts[0] != "m":
        raise BadCursor("not a message cursor")
    try:
        return int(parts[1])
    except ValueError as exc:
        raise BadCursor(str(exc)) from exc

"""Pure tests for the pagination cursor codec (no DB)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.history.cursors import (
    BadCursor,
    decode_seq_cursor,
    decode_session_cursor,
    encode_seq_cursor,
    encode_session_cursor,
)


def test_session_cursor_round_trips():
    when = datetime(2026, 8, 22, 13, 45, 6, 123456, tzinfo=timezone.utc)
    raw = encode_session_cursor(when, "abc123")
    assert decode_session_cursor(raw) == (when, "abc123")


def test_session_cursor_is_opaque():
    # Opaque so the keyset tiebreaker can change later without a client caring.
    raw = encode_session_cursor(datetime.now(timezone.utc), "abc123")
    assert "abc123" not in raw


def test_seq_cursor_round_trips():
    assert decode_seq_cursor(encode_seq_cursor(42)) == 42


@pytest.mark.parametrize("bad", ["", "!!!!", "Zm9v", "not-base64", "eyJhIjoxfQ=="])
def test_a_bad_cursor_raises_rather_than_silently_meaning_page_one(bad):
    # A client stuck re-reading page one looks like "history is broken" and is
    # invisible server-side. The router turns this into a 400.
    with pytest.raises(BadCursor):
        decode_session_cursor(bad)

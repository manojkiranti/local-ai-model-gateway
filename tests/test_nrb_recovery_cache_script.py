"""`scripts/nrb_recovery_cache.py --stats` prints what `recovery_cache.stats` returns."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.nrb import recovery_cache

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "nrb_recovery_cache.py"


def _script():
    spec = importlib.util.spec_from_file_location("nrb_recovery_cache_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def rollback(self):
        return None


def test_stats_prints_the_native_repair_breakdown(monkeypatch, capsys):
    """The native units by repair status: the operator's view of native-3 (final review I3)."""
    payload = {
        "versions": [{"base_version": recovery_cache.base_version(), "documents": 3, "units": 40}],
        "routes": [{"route": "native", "engine_version": "native-3/x", "ok": True, "units": 40}],
        "native_repair": [
            {"repair": "passthrough", "units": 9},
            {"repair": "font_tables", "units": 25},
            {"repair": "unrepaired:coverage", "units": 6},
        ],
    }

    async def fake_stats(_session):
        return payload

    monkeypatch.setattr(recovery_cache, "stats", fake_stats)
    asyncio.run(_script().do_stats(_Session))
    out = capsys.readouterr().out
    assert "--- native units by repair status ---" in out
    section = out.split("--- native units by repair status ---", 1)[1]
    rows = [line.split() for line in section.strip().splitlines()]
    assert rows == [["font_tables", "25"], ["passthrough", "9"], ["unrepaired:coverage", "6"]]


def test_stats_says_so_when_no_native_unit_is_cached(monkeypatch, capsys):
    async def fake_stats(_session):
        return {"versions": [], "routes": [], "native_repair": []}

    monkeypatch.setattr(recovery_cache, "stats", fake_stats)
    asyncio.run(_script().do_stats(_Session))
    section = capsys.readouterr().out.split("--- native units by repair status ---", 1)[1]
    assert section.strip() == "(none)"

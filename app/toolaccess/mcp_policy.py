"""Pure decisions about which MCP tools a user may use.

The local-tool rule (`policy.py`) applied to the remote MCP server's tools. No
database, no HTTP, no MCP session — so the rule is provable on its own.

THE RULE. Each MCP tool belongs to a SYSTEM (HRMS, iZone, EMS, or "other"),
and each user holds an ACCESS LEVEL per system:

  none  the system's tools are hidden from the user's model entirely
  read  only its look-up tools (the same read/write classifier the gateway
        already applies server-wide with MCP_TOOL_MODE=read_only)
  full  its look-up AND its data-changing tools

with no stored level meaning `DEFAULT_LEVEL`. An admin may additionally
override a single tool for one user, either way; a per-tool override beats the
level. Both are CEILINGED by the server-wide MCP_TOOL_MODE filter, which runs
first: a tool that filter excludes is never offered, whatever a user holds —
so "full" only reaches write tools on a deployment running MCP_TOOL_MODE=all.

This is VISIBILITY, decided by the gateway. It is separate from the MCP grants
(`app/mcp/grants.py`), which the gateway forwards and the MCP SERVER enforces
(e.g. `mcp-hrms` opens the HRMS tools there). A user needs both: the level
here to see a tool, and whatever the MCP server requires to run it.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from ..mcp.client import tokenize

# Stable ids (used in URLs and stored) — the labels are display copy.
SYSTEM_HRMS = "hrms"
SYSTEM_IZONE = "izone"
SYSTEM_EMS = "ems"
SYSTEM_OTHER = "other"

SYSTEM_LABELS: dict[str, str] = {
    SYSTEM_HRMS: "HRMS (employees, attendance, leave)",
    SYSTEM_IZONE: "iZone (documents, circulars)",
    SYSTEM_EMS: "EMS (expenses)",
    SYSTEM_OTHER: "Other MCP tools",
}
SYSTEM_ORDER: tuple[str, ...] = tuple(SYSTEM_LABELS)

LEVEL_NONE = "none"
LEVEL_READ = "read"
LEVEL_FULL = "full"
LEVELS: tuple[str, ...] = (LEVEL_NONE, LEVEL_READ, LEVEL_FULL)

# What a user gets for a system an admin has not set. "read" keeps today's
# behaviour: every user already saw the read-only MCP tools.
DEFAULT_LEVEL = LEVEL_READ

_SYSTEM_TOKENS = {SYSTEM_HRMS, SYSTEM_IZONE, SYSTEM_EMS}


def system_of(tool: str) -> str:
    """The system a tool belongs to, from its name's TOKENS (never substrings:
    `list_izone_list_items` contains "ems" inside "items" and is iZone's)."""
    return next((t for t in tokenize(tool) if t in _SYSTEM_TOKENS), SYSTEM_OTHER)


def is_write(tool: str, *, read_prefixes: Iterable[str], write_keywords: Iterable[str]) -> bool:
    """Whether a tool may change data: the server-wide read_only classifier,
    inverted — a write-like token, or no read-like token at all, is a write.
    Erring that way is deliberate: an unclassifiable tool needs `full`."""
    tokens = tokenize(tool)
    writes, reads = set(write_keywords), set(read_prefixes)
    return any(t in writes for t in tokens) or not any(t in reads for t in tokens)


def level_for(system: str, levels: Mapping[str, str]) -> str:
    """The stored level for `system`, or the default. An unknown stored value
    (one that escaped the CHECK) fails closed to `none`."""
    level = levels.get(system, DEFAULT_LEVEL)
    return level if level in LEVELS else LEVEL_NONE


def level_allows(level: str, *, write: bool) -> bool:
    if level == LEVEL_FULL:
        return True
    if level == LEVEL_READ:
        return not write
    return False


def is_allowed(
    tool: str, *, write: bool, levels: Mapping[str, str], overrides: Mapping[str, bool]
) -> bool:
    """A per-tool override wins; otherwise the tool's system level decides."""
    override = overrides.get(tool)
    if override is not None:
        return override
    return level_allows(level_for(system_of(tool), levels), write=write)


class McpAccess:
    """One user's MCP access for one request: their stored levels and per-tool
    overrides, plus the classifier deciding read vs write. Built once per
    request by `dependencies.get_mcp_access` and handed to the registry, which
    drops every MCP tool this says no to — not offered to the model, and so
    not dispatchable."""

    def __init__(
        self,
        *,
        levels: Mapping[str, str],
        overrides: Mapping[str, bool],
        read_prefixes: Iterable[str],
        write_keywords: Iterable[str],
    ) -> None:
        self.levels = dict(levels)
        self.overrides = dict(overrides)
        self._read_prefixes = frozenset(read_prefixes)
        self._write_keywords = frozenset(write_keywords)

    def is_write(self, tool: str) -> bool:
        return is_write(tool, read_prefixes=self._read_prefixes, write_keywords=self._write_keywords)

    def allows(self, tool: str) -> bool:
        return is_allowed(
            tool, write=self.is_write(tool), levels=self.levels, overrides=self.overrides
        )

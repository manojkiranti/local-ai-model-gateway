"""Request and response models for the tool-access admin routes."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ToolAccessItem(BaseModel):
    """One local tool as it stands for one user."""

    name: str
    summary: str
    group: str
    # What the user gets with no override (the starter set).
    default_allowed: bool
    # An admin's explicit decision, or null when the default applies.
    override: bool | None
    # The effective answer: `override` if set, else `default_allowed`.
    allowed: bool
    updated_at: datetime | None
    updated_by: int | None


class ToolAccessGroup(BaseModel):
    """One group as it stands for one user — the unit an admin switches."""

    id: str
    label: str
    # The group's tools, in display order.
    tools: list[str]
    # True when every tool is allowed, False when none is, None when mixed.
    allowed: bool | None
    # The starter set's answer for the group (None if its tools differ).
    default_allowed: bool | None
    # "default": no overrides; "granted"/"blocked": every tool overridden the
    # same way; "mixed": anything else (e.g. per-tool overrides set via the API).
    state: Literal["default", "granted", "blocked", "mixed"]
    # The most recent override in the group, if any (the audit fact).
    updated_at: datetime | None
    updated_by: int | None


class ToolAccessResponse(BaseModel):
    user_id: int
    # In display order; the admin screen shows these.
    groups: list[ToolAccessGroup]
    # Per-tool detail behind the groups.
    items: list[ToolAccessItem]


class ToolAccessUpdate(BaseModel):
    """`extra="forbid"` like `UserUpdate` and `GrantCreate`: an unexpected field
    is refused loudly rather than silently ignored."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool


# --- MCP access ----------------------------------------------------------------


McpLevel = Literal["none", "read", "full"]


class McpSystemAccess(BaseModel):
    """One MCP system (HRMS/iZone/EMS/other) as it stands for one user."""

    id: str
    label: str
    # The effective level: the stored one, else `default_level`.
    level: McpLevel
    default_level: McpLevel
    # Whether an admin has set it (False = the default applies).
    overridden: bool
    updated_at: datetime | None
    updated_by: int | None


class McpToolAccessItem(BaseModel):
    """One MCP tool as it stands for one user."""

    name: str
    summary: str
    system: str
    # "write" = may change data: needs the `full` level (or an override).
    kind: Literal["read", "write"]
    # False when the server-wide MCP_TOOL_MODE excludes it: nobody gets it,
    # whatever their level or override, until that mode changes.
    server_enabled: bool
    # An admin's per-tool decision, or null when the system level decides.
    override: bool | None
    # The effective answer for this user.
    allowed: bool
    updated_at: datetime | None
    updated_by: int | None


class McpAccessResponse(BaseModel):
    user_id: int
    tool_mode: str
    # False when the MCP server could not be listed: `systems` is still
    # complete and editable, `tools` is empty.
    mcp_reachable: bool
    systems: list[McpSystemAccess]
    tools: list[McpToolAccessItem]


class McpLevelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: McpLevel

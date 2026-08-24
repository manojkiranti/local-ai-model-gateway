"""Pure decisions about which MCP grants a caller holds, and how they travel.

No database, no HTTP, no FastAPI — for the reason `app/rag/permissions.py` and
`app/users/policy.py` are pure. This is the code that decides whether somebody
can read salary data or run SQL over the expenses database, and proving that
should not require standing up Postgres.

WHAT IS DELIBERATELY NOT HERE: a tool -> grant map. `canAccess` on the MCP
server is applied at session construction, so its `tools/list` already returns
exactly the tools an identity may call. A second copy of that mapping here would
drift, and the dangerous direction of drift is silent: "the gateway hides a tool
the MCP would allow" is a lost capability with no error on either side. One
mapping, in the enforcer, read by both.

The six strings below are the second of three copies (the third is
`ck_user_mcp_grants_key`, the first is the MCP server's `src/auth/mcp-roles.ts`).
That is the `ck_api_keys_scopes` / `policy.ALL_SCOPES` arrangement on purpose:
the CHECK stops a typo being STORED, this frozenset stops one being HONOURED,
and the server's copy stops one being ENFORCED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# --- the vocabulary ------------------------------------------------------- #
# Roles name a SYSTEM (hyphenated); permissions name a SHARP EDGE inside one
# (dotted). Adding a dangerous tool adds a permission, never a new role.
ROLE_HRMS = "mcp-hrms"
ROLE_IZONE = "mcp-izone"
ROLE_EMS = "mcp-ems"

PERM_HRMS_FULL = "mcp.hrms.full"
PERM_HRMS_TASKS = "mcp.hrms.tasks"
PERM_EMS_QUERY = "mcp.ems.query"

ROLES: frozenset[str] = frozenset({ROLE_HRMS, ROLE_IZONE, ROLE_EMS})
PERMISSIONS: frozenset[str] = frozenset(
    {PERM_HRMS_FULL, PERM_HRMS_TASKS, PERM_EMS_QUERY}
)
ALL_GRANTS: frozenset[str] = ROLES | PERMISSIONS

# --- the wire ------------------------------------------------------------- #
EMAIL_HEADER = "x-user-email"
ROLES_HEADER = "x-user-roles"
PERMISSIONS_HEADER = "x-user-permissions"


@dataclass(frozen=True)
class McpIdentity:
    """Who is asking, and what they hold.

    Frozen because it is captured by the streaming generator in
    `app/chat/router.py` and read after the request scope has ended.
    """

    email: str | None
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()

    @classmethod
    def from_grants(
        cls, *, email: str | None, grant_keys: Iterable[str]
    ) -> "McpIdentity":
        """Split a flat set of stored grant keys into roles and permissions.

        Unknown keys are dropped by intersection rather than raised on. The
        gateway and the MCP server deploy independently, so a key this build
        does not define may legitimately exist in the table; forwarding it would
        ask the server to reason about a grant neither side agrees on, and
        raising would 500 a chat turn over a row nobody is using.
        """
        keys = set(grant_keys)
        return cls(
            email=email,
            roles=frozenset(keys & ROLES),
            permissions=frozenset(keys & PERMISSIONS),
        )


def header_values(identity: McpIdentity | None) -> dict[str, str]:
    """The headers this identity contributes to an MCP request.

    Empty fields are omitted rather than sent blank: `x-user-roles: ` invites a
    future parser to treat the empty string as a grant, and an absent header
    already means "none" on the other side. Values are sorted so a request is
    reproducible and a test can assert on the exact string.
    """
    if identity is None:
        return {}
    headers: dict[str, str] = {}
    if identity.email:
        headers[EMAIL_HEADER] = identity.email
    if identity.roles:
        headers[ROLES_HEADER] = ",".join(sorted(identity.roles))
    if identity.permissions:
        headers[PERMISSIONS_HEADER] = ",".join(sorted(identity.permissions))
    return headers

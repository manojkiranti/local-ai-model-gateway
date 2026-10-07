"""Pure decisions about which local tools a user may use.

No database, no HTTP — like `app/mcp/grants.py` and `app/users/policy.py`, so
the rule can be proven without standing up Postgres.

THE RULE. Every user starts from the STARTER SET: every local tool except those
in `DEFAULT_OFF`. An admin may then override any tool for one user, either way:
switch a starter tool OFF (block it) or a default-off tool ON (grant it). A
stored override always wins; with none, the default applies. So a tool added to
the gateway later is ON for everyone unless it is also added to `DEFAULT_OFF` —
put anything that reaches outside the bank's network there.

Admins manage access BY GROUP: one switch sets or resets every tool in a
group at once (stored as one override per tool, so the rule above is
unchanged). `GROUPS` places each tool in a group; a tool missing from it falls
under "other", which an admin manages the same way.
"""

from __future__ import annotations

from typing import Iterable, Mapping

# Tools that reach outside the bank's network: off until an admin grants them.
DEFAULT_OFF: frozenset[str] = frozenset({"fetch_url", "get_nrb_forex"})

# Stable ids (used in URLs) — the labels are display copy.
GROUP_EVERYDAY = "everyday"
GROUP_READ = "read"
GROUP_CREATE = "create"
GROUP_DEPARTMENT = "department"
GROUP_EXTERNAL = "external"
GROUP_OTHER = "other"

# Display order and label of each group on the admin screen.
GROUP_LABELS: dict[str, str] = {
    GROUP_EVERYDAY: "Everyday helpers",
    GROUP_READ: "Read uploaded files",
    GROUP_CREATE: "Create files",
    GROUP_DEPARTMENT: "Department documents",
    GROUP_EXTERNAL: "External sources",
    GROUP_OTHER: "Other",
}
GROUP_ORDER: tuple[str, ...] = tuple(GROUP_LABELS)

GROUPS: Mapping[str, str] = {
    "calculator": GROUP_EVERYDAY,
    "date_math": GROUP_EVERYDAY,
    "get_current_time": GROUP_EVERYDAY,
    "nepali_date": GROUP_EVERYDAY,
    "read_document": GROUP_READ,
    "read_image": GROUP_READ,
    "read_excel": GROUP_READ,
    "inspect_excel": GROUP_READ,
    "aggregate_excel": GROUP_READ,
    "edit_excel": GROUP_READ,
    "create_pptx": GROUP_CREATE,
    "create_memo": GROUP_CREATE,
    "create_docx": GROUP_CREATE,
    "create_pdf": GROUP_CREATE,
    "create_excel": GROUP_CREATE,
    "create_csv": GROUP_CREATE,
    "create_chart": GROUP_CREATE,
    "create_html": GROUP_CREATE,
    "search_department_docs": GROUP_DEPARTMENT,
    "read_department_doc": GROUP_DEPARTMENT,
    "fetch_url": GROUP_EXTERNAL,
    "get_nrb_forex": GROUP_EXTERNAL,
}


def default_allowed(tool: str) -> bool:
    """Whether a user with no override for `tool` may use it."""
    return tool not in DEFAULT_OFF


def group_of(tool: str) -> str:
    return GROUPS.get(tool, GROUP_OTHER)


def tools_in_group(group: str, tools: Iterable[str]) -> list[str]:
    """The tools of `tools` that fall in `group`, in the order given."""
    return [t for t in tools if group_of(t) == group]


def is_allowed(tool: str, overrides: Mapping[str, bool]) -> bool:
    """A stored override wins; otherwise the default applies."""
    override = overrides.get(tool)
    return default_allowed(tool) if override is None else override


def allowed_tools(tools: Iterable[str], overrides: Mapping[str, bool]) -> frozenset[str]:
    """The subset of `tools` this user may use."""
    return frozenset(t for t in tools if is_allowed(t, overrides))


def summary(description: str) -> str:
    """The first sentence of a tool's model-facing description, for the admin
    screen — the full text is a prompt for the model, not copy for people."""
    text = " ".join(description.split())
    end = text.find(". ")
    return text if end < 0 else text[: end + 1]

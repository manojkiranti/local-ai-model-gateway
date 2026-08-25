"""Unit tests for the pure MCP grant core.

No database, no HTTP, no app import beyond the module under test — the same
rule `tests/test_department_permissions.py` follows for `app/rag/permissions.py`.
"""

import ast
import pathlib

import pytest

from app.agent.loop import describe_identity
from app.mcp import grants


def test_the_vocabulary_is_exactly_the_six_agreed_strings():
    assert grants.ROLES == frozenset({"mcp-hrms", "mcp-izone", "mcp-ems"})
    assert grants.PERMISSIONS == frozenset(
        {"mcp.hrms.full", "mcp.hrms.tasks", "mcp.ems.query"}
    )
    assert grants.ALL_GRANTS == grants.ROLES | grants.PERMISSIONS
    assert len(grants.ALL_GRANTS) == 6


def test_roles_and_permissions_do_not_overlap():
    # A string in both sets would be sorted into both header fields and the
    # MCP server would see it twice under different meanings.
    assert not (grants.ROLES & grants.PERMISSIONS)


def test_from_grants_splits_a_flat_key_set_by_kind():
    identity = grants.McpIdentity.from_grants(
        email="person@example.com",
        grant_keys=["mcp-hrms", "mcp.hrms.full", "mcp-ems"],
    )
    assert identity.roles == frozenset({"mcp-hrms", "mcp-ems"})
    assert identity.permissions == frozenset({"mcp.hrms.full"})


def test_an_unknown_grant_key_is_dropped_not_raised():
    """Fail-closed rule 3: the two sides deploy independently.

    A row that predates a vocabulary change, or a hand-inserted key, must not
    500 the chat endpoint — and must not be forwarded either, or the MCP server
    would be asked to reason about a grant this build does not define.
    """
    identity = grants.McpIdentity.from_grants(
        email="person@example.com", grant_keys=["mcp-hrms", "mcp-payroll", "nonsense"]
    )
    assert identity.roles == frozenset({"mcp-hrms"})
    assert identity.permissions == frozenset()


def test_an_identity_with_no_grants_is_representable():
    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=[])
    assert identity.roles == frozenset()
    assert identity.permissions == frozenset()
    # The email still travels: the MCP server logs refusals against it.
    assert grants.header_values(identity) == {"x-user-email": "a@b.c"}


def test_header_values_omits_empty_fields_entirely():
    """An empty header and an absent one mean the same thing to the server, but
    sending `x-user-roles: ` invites a future parser to treat '' as a grant."""
    identity = grants.McpIdentity(email=None, roles=frozenset(), permissions=frozenset())
    assert grants.header_values(identity) == {}


def test_header_values_are_sorted_so_they_are_deterministic():
    identity = grants.McpIdentity.from_grants(
        email="p@e.com", grant_keys=["mcp-izone", "mcp-ems", "mcp-hrms"]
    )
    assert grants.header_values(identity)["x-user-roles"] == "mcp-ems,mcp-hrms,mcp-izone"


def test_header_values_of_none_is_empty():
    # An unauthenticated or identity-less call must not forward anything.
    assert grants.header_values(None) == {}


def test_the_identity_is_frozen():
    """It is captured by a streaming generator and read after the request
    scope ends; a mutable identity could be edited mid-turn."""
    from dataclasses import FrozenInstanceError

    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=["mcp-hrms"])
    with pytest.raises(FrozenInstanceError):
        identity.email = "other@b.c"  # type: ignore[misc]


def test_the_module_is_pure_no_database_and_no_http():
    """AST-asserted, not grepped: the value of this module is that a capability
    boundary can be proved without a database or a network."""
    source = pathlib.Path("app/mcp/grants.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"sqlalchemy", "httpx", "fastapi", "app"}
    assert not (imported & forbidden), f"grants.py must stay pure; found {imported & forbidden}"


def _client():
    from app.mcp.client import MCPClient

    return MCPClient(
        server_url="http://127.0.0.1:3333/mcp",
        auth_token="secret",
        tool_mode="read_only",
        allowlist=[],
        read_prefixes=["get", "list"],
        write_keywords=["create"],
    )


def test_session_headers_carry_the_identity():
    identity = grants.McpIdentity.from_grants(
        email="person@example.com",
        grant_keys=["mcp-hrms", "mcp.hrms.full"],
    )
    headers = _client()._session_headers(identity)
    assert headers["Authorization"] == "Bearer secret"
    assert headers["x-user-email"] == "person@example.com"
    assert headers["x-user-roles"] == "mcp-hrms"
    assert headers["x-user-permissions"] == "mcp.hrms.full"


def test_session_headers_omit_grant_fields_for_an_ungranted_caller():
    identity = grants.McpIdentity.from_grants(email="person@example.com", grant_keys=[])
    headers = _client()._session_headers(identity)
    assert "x-user-roles" not in headers
    assert "x-user-permissions" not in headers
    # The auth token still goes: the caller is authenticated, just unprovisioned.
    assert headers["Authorization"] == "Bearer secret"


def test_no_identity_forwards_no_user_headers():
    headers = _client()._session_headers(None)
    assert headers == {"Authorization": "Bearer secret"}


def test_the_client_holds_no_identity_state_between_calls():
    """MCPClient is a process-wide singleton on app.state.mcp. If the identity
    were stored on it, two concurrent turns would race each other's grants and
    one user would act with another's permissions."""
    client = _client()
    hrms = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=["mcp-hrms"])
    ems = grants.McpIdentity.from_grants(email="d@e.f", grant_keys=["mcp-ems"])

    first = client._session_headers(hrms)
    second = client._session_headers(ems)
    third = client._session_headers(hrms)

    assert first["x-user-roles"] == "mcp-hrms"
    assert second["x-user-roles"] == "mcp-ems"
    assert third == first
    assert "identity" not in vars(client)


def test_the_returned_headers_are_a_fresh_dict_each_call():
    """A shared dict would let one turn's mutation reach another's request."""
    client = _client()
    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=["mcp-hrms"])
    first = client._session_headers(identity)
    first["x-user-roles"] = "mcp-ems"
    assert client._session_headers(identity)["x-user-roles"] == "mcp-hrms"


def test_no_call_site_still_passes_user_email_to_the_mcp_client():
    """AST check across the call sites: a forgotten one would compile,
    forward no grants, and present as 'the tools stopped working' with no error
    anywhere — the §18 failure class.

    Four files, not the brief's original three: app/chat/router.py is the
    primary call site — the one that actually runs a chat turn — and omitting
    it from this very check would defeat the point of the check.
    """
    import ast
    import pathlib

    for path in (
        "app/tools/router.py",
        "app/mcp/router.py",
        "app/agent/loop.py",
        "app/chat/router.py",
    ):
        tree = ast.parse(pathlib.Path(path).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                assert keyword.arg != "user_email", (
                    f"{path} still passes user_email= to an MCP call"
                )


def test_the_log_line_pairs_grants_held_with_tools_available():
    """The §10 metric: a lost capability is only detectable if the grants and
    the resulting tool list appear together. Two log lines on opposite sides of
    the wire cannot be correlated after the fact."""
    identity = grants.McpIdentity.from_grants(
        email="person@example.com", grant_keys=["mcp-hrms", "mcp.hrms.full"]
    )
    rendered = describe_identity(identity)
    assert "person@example.com" in rendered
    assert "mcp-hrms" in rendered
    assert "mcp.hrms.full" in rendered


def test_it_says_no_grants_rather_than_printing_an_empty_list():
    """`roles=[]` reads as a logging artefact; "no grants" reads as the fact it
    is — which is what someone scanning for a mis-provisioned account needs."""
    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=[])
    assert "no grants" in describe_identity(identity)


def test_it_handles_no_identity_at_all():
    assert describe_identity(None) == "no identity"

"""The rules governing a directory-provisioned user. No database, no network.

The single most dangerous mistake available in this change is reusing
`app/auth/router.py::_resolve_role` on the directory login path. That function
grants admin when the `users` table is empty — correct for a deliberate
registration, catastrophic for a login that provisions on demand, because on a
fresh deployment whoever signed in first would silently own the system.

It cannot be tested through the API (the harness has no empty-table fixture and
tests commit real rows into a database with thousands of users), so it is locked
two ways instead: the role function is tested directly, and the provisioning code
path is inspected with `ast` to prove it cannot reach the bootstrap rule. The
codebase already uses an AST check for exactly this kind of "this module must
never call that" invariant — see
tests/test_nrb_corpus_driver.py's extraction-evidence test.
"""

import ast
import inspect
from pathlib import Path

from app.auth import router as auth_router
from app.users.models import PROVIDER_AD, PROVIDER_LOCAL, ROLE_ADMIN, ROLE_MEMBER
from app.users.repository import resolve_directory_role

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Role resolution
# --------------------------------------------------------------------------

def test_a_directory_user_is_a_member_by_default():
    assert resolve_directory_role("someone@example.com", set()) == ROLE_MEMBER


def test_the_admin_allowlist_still_grants_admin():
    """ADMIN_EMAILS is how a real staff admin is designated, with no local account."""
    allowlist = {"boss@example.com"}
    assert resolve_directory_role("boss@example.com", allowlist) == ROLE_ADMIN


def test_a_non_listed_user_is_not_promoted_by_an_allowlist_existing():
    allowlist = {"boss@example.com"}
    assert resolve_directory_role("someone@example.com", allowlist) == ROLE_MEMBER


def test_role_resolution_takes_no_session_so_it_cannot_count_users():
    """The structural guarantee: with no database handle, the empty-table rule
    is not merely unused here — it is unreachable."""
    params = list(inspect.signature(resolve_directory_role).parameters)
    assert params == ["email", "admin_emails"]


# --------------------------------------------------------------------------
# The provisioning path cannot reach the registration bootstrap
# --------------------------------------------------------------------------

def _called_names(func) -> set[str]:
    """Every function/attribute name called inside `func`."""
    source = inspect.getsource(func)
    tree = ast.parse(inspect.cleandoc(source))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_the_provisioning_path_never_calls_the_registration_role_rule():
    """`_resolve_role` promotes the first user; a login must not use it."""
    called = _called_names(auth_router._login_unknown_identifier)
    assert "_resolve_role" not in called
    assert "resolve_directory_role" in called


def test_the_registration_bootstrap_is_reachable_only_from_register():
    """`_resolve_role` has exactly one caller, and it is `register`."""
    source = (REPO_ROOT / "app" / "auth" / "router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "_resolve_role" in _called_names_from_node(node):
            callers.add(node.name)

    assert callers == {"register"}, (
        "the empty-table 'first user becomes admin' rule must stay reachable only "
        f"from register(); found callers: {sorted(callers)}"
    )


def _called_names_from_node(node) -> set[str]:
    names = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            target = inner.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


# --------------------------------------------------------------------------
# The provider vocabulary
# --------------------------------------------------------------------------

def test_login_dispatch_handles_every_provider_in_the_vocabulary():
    """A provider the CHECK allows but the router does not branch on would fall
    into the final `else` and be refused — so the two must stay in step."""
    source = inspect.getsource(auth_router.login)
    assert f"PROVIDER_LOCAL" in source
    assert f"PROVIDER_AD" in source
    assert PROVIDER_LOCAL == "local"
    assert PROVIDER_AD == "ad"


def test_a_directory_user_is_created_without_a_password_hash():
    """Read the constructor rather than the database: `create_directory_user`
    must not be able to grow a hash argument without this failing."""
    source = inspect.getsource(
        __import__("app.users.repository", fromlist=["create_directory_user"]).create_directory_user
    )
    assert "password_hash=None" in source
    assert "PROVIDER_AD" in source

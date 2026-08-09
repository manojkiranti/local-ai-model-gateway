"""storage_key minting and traversal-safe resolution. Pure — tmp dirs only.

The key is RELATIVE by design: rows stay portable across hosts and the same
value becomes an object-storage key later. Resolution therefore has to defend
against a key that tries to climb out of the base directory.
"""

import pytest

from app.rag.storage import (
    StorageError,
    delete_document,
    mint_storage_key,
    resolve_storage_path,
    write_document,
)


def test_key_is_relative_and_under_the_department():
    key = mint_storage_key("hr", "Leave Policy.pdf")
    assert not key.startswith("/")
    assert key.startswith("hr/")
    assert key.endswith(".pdf")


def test_key_does_not_reuse_the_caller_supplied_name():
    """The on-disk name is a uuid + extension, exactly like the file store —
    no traversal or collision from a user-chosen filename."""
    key = mint_storage_key("hr", "../../etc/passwd.pdf")
    assert ".." not in key
    assert "passwd" not in key


def test_keys_are_unique_per_call():
    assert mint_storage_key("hr", "a.pdf") != mint_storage_key("hr", "a.pdf")


def test_missing_extension_falls_back_to_bin():
    assert mint_storage_key("hr", "noext").endswith(".bin")


def test_department_code_is_slugged_not_trusted():
    key = mint_storage_key("../hr", "a.pdf")
    assert not key.startswith("..")


def test_resolve_returns_a_path_under_the_base(tmp_path):
    key = mint_storage_key("hr", "a.pdf")
    resolved = resolve_storage_path(key, str(tmp_path))
    assert str(resolved).startswith(str(tmp_path))


@pytest.mark.parametrize("evil", [
    "../outside.pdf",
    "hr/../../outside.pdf",
    "/etc/passwd",
    "hr/./../../x.pdf",
])
def test_resolution_refuses_to_escape_the_base(tmp_path, evil):
    with pytest.raises(StorageError):
        resolve_storage_path(evil, str(tmp_path))


def test_write_creates_parent_directories_and_bytes(tmp_path):
    key = mint_storage_key("hr", "a.pdf")
    write_document(b"hello", key, str(tmp_path))
    assert resolve_storage_path(key, str(tmp_path)).read_bytes() == b"hello"


def test_write_refuses_an_escaping_key(tmp_path):
    with pytest.raises(StorageError):
        write_document(b"x", "../evil.pdf", str(tmp_path))


def test_delete_removes_the_file(tmp_path):
    key = mint_storage_key("hr", "a.pdf")
    write_document(b"hello", key, str(tmp_path))
    assert delete_document(key, str(tmp_path)) is True
    assert not resolve_storage_path(key, str(tmp_path)).exists()


def test_delete_is_idempotent(tmp_path):
    """Compensation runs on an error path — it must never raise and mask the
    original failure."""
    key = mint_storage_key("hr", "a.pdf")
    assert delete_document(key, str(tmp_path)) is False


def test_delete_refuses_an_escaping_key(tmp_path):
    outside = tmp_path.parent / "victim.txt"
    outside.write_text("do not delete me")
    with pytest.raises(StorageError):
        delete_document("../victim.txt", str(tmp_path))
    assert outside.exists()


def test_delete_never_raises_on_a_directory(tmp_path):
    """Defensive: a key that somehow names a directory must not blow up the
    compensation path."""
    (tmp_path / "hr").mkdir()
    assert delete_document("hr", str(tmp_path)) is False

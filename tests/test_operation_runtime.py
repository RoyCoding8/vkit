import hashlib
from pathlib import Path

import pytest

from vkit.operations._runtime import pinned_binary, pinned_node_helper


def test_pinned_binary_accepts_uppercase_sha256_and_returns_resolved_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "backend"
    content = b"pinned backend"
    binary.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setenv("VKIT_TEST_BACKEND", str(binary))
    monkeypatch.setenv("VKIT_TEST_BACKEND_SHA256", digest.upper())

    assert pinned_binary("VKIT_TEST_BACKEND", "VKIT_TEST_BACKEND_SHA256") == (
        binary.resolve(), digest)


def test_pinned_binary_rejects_missing_and_mismatched_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "backend"
    monkeypatch.setenv("VKIT_TEST_BACKEND", str(binary))
    monkeypatch.setenv("VKIT_TEST_BACKEND_SHA256", "0" * 64)
    assert pinned_binary("VKIT_TEST_BACKEND", "VKIT_TEST_BACKEND_SHA256") is None

    binary.write_bytes(b"different backend")
    assert pinned_binary("VKIT_TEST_BACKEND", "VKIT_TEST_BACKEND_SHA256") is None


@pytest.mark.parametrize("failure", [OSError, RuntimeError, ValueError])
def test_pinned_binary_reports_path_resolution_failures_as_unavailable(
        monkeypatch: pytest.MonkeyPatch, failure: type[Exception]) -> None:
    monkeypatch.setenv("VKIT_TEST_BACKEND", "backend")
    monkeypatch.setenv("VKIT_TEST_BACKEND_SHA256", "0" * 64)

    def fail_resolve(self: Path) -> Path:
        raise failure("cannot resolve configured path")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    assert pinned_binary("VKIT_TEST_BACKEND", "VKIT_TEST_BACKEND_SHA256") is None


@pytest.mark.parametrize("failure", [None, "helper", "lock", "node", "directory", "missing_pin"])
def test_node_helper_requires_both_pinned_files_and_a_node_executable(tmp_path, monkeypatch, failure):
    helper = tmp_path / "helper.cjs"
    lockfile = tmp_path / "package-lock.json"
    node = tmp_path / "node"
    helper.write_bytes(b"trusted helper")
    lockfile.write_bytes(b"trusted lock")
    node.write_bytes(b"node executable")
    helper_sha = hashlib.sha256(b"trusted helper").hexdigest()
    lock_sha = hashlib.sha256(b"trusted lock").hexdigest()
    monkeypatch.setenv("TEST_HELPER", str(helper))
    monkeypatch.setenv("TEST_HELPER_SHA", helper_sha.upper())
    monkeypatch.setenv("TEST_LOCK", str(lockfile))
    monkeypatch.setenv("TEST_LOCK_SHA", lock_sha.upper())
    monkeypatch.setenv("VKIT_NODE_BIN", str(node))
    if failure == "helper":
        helper.write_bytes(b"changed helper")
    elif failure == "lock":
        lockfile.write_bytes(b"changed lock")
    elif failure == "node":
        node.unlink()
    elif failure == "directory":
        monkeypatch.setenv("VKIT_NODE_BIN", str(tmp_path))
    elif failure == "missing_pin":
        monkeypatch.delenv("TEST_HELPER_SHA")

    actual = pinned_node_helper("TEST_HELPER", "TEST_HELPER_SHA", "TEST_LOCK", "TEST_LOCK_SHA")

    expected = (helper.resolve(), helper_sha, lockfile.resolve(), lock_sha, node.resolve())
    assert actual == (expected if failure is None else None)

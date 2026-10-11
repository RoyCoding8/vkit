import hashlib
from pathlib import Path

import pytest

from vkit.operations._runtime import pinned_binary


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

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from vkit.operations import regex_cost
from vkit.operations.regex_cost import analyze_regex_cost


@pytest.fixture
def configured_backend(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    repository = Path(__file__).resolve().parents[1]
    helper = repository / "tools" / "regex" / "helper.cjs"
    lockfile = repository / "tools" / "regex" / "package-lock.json"
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the pinned Recheck integration tests")
    version = subprocess.run([node, "--version"], capture_output=True, check=False)
    if version.returncode != 0 or version.stdout.decode().strip() != regex_cost.NODE_VERSION:
        pytest.skip("the pinned Node runtime is not installed")
    monkeypatch.setenv("VKIT_REGEX_HELPER", str(helper))
    monkeypatch.setenv("VKIT_REGEX_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_REGEX_LOCKFILE", str(lockfile))
    monkeypatch.setenv("VKIT_REGEX_LOCK_SHA256", hashlib.sha256(lockfile.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_NODE_BIN", str(Path(node).resolve()))
    return repository, Path(node).resolve()


def test_classifies_exponential_and_linear_patterns_from_actual_engine(
    configured_backend: tuple[Path, Path],
) -> None:
    root, _ = configured_backend

    vulnerable = analyze_regex_cost(root, "^(a+)+$")
    safe = analyze_regex_cost(root, "^a+$", "i")

    assert vulnerable["status"] == "VULNERABLE_MODEL", vulnerable
    assert vulnerable["diagnostics"]["checker"] == "automaton"
    assert vulnerable["diagnostics"]["complexity"] == {
        "type": "exponential", "summary": "exponential", "isFuzz": False}
    assert vulnerable["diagnostics"]["hotspot"] == [
        {"start": 2, "end": 3, "temperature": "heat"}]
    assert vulnerable["diagnostics"]["attack"]["pumps"] == [
        {"prefix": "a", "pump": "a", "bias": 0}]
    assert "theoretical attack description" in vulnerable["model_scope"]
    assert vulnerable["backend_identity"]["recheck"] == "4.5.0"
    assert safe["status"] == "SAFE_MODEL", safe
    assert safe["diagnostics"]["complexity"]["type"] == "linear"
    assert safe["diagnostics"]["flags"] == "i"


def test_classifies_ambiguous_alternation_with_a_hotspot(configured_backend: tuple[Path, Path]) -> None:
    root, _ = configured_backend

    result = analyze_regex_cost(root, "^(a|aa)+$")

    assert result["status"] == "VULNERABLE_MODEL", result
    assert result["diagnostics"]["complexity"]["type"] == "exponential"
    assert result["diagnostics"]["hotspot"] == [
        {"start": 2, "end": 3, "temperature": "heat"},
        {"start": 4, "end": 6, "temperature": "heat"},
    ]


def test_invalid_syntax_and_out_of_profile_flags_are_unsupported(
    configured_backend: tuple[Path, Path],
) -> None:
    root, _ = configured_backend

    invalid = analyze_regex_cost(root, "[")
    flag = analyze_regex_cost(root, "^a+$", "d")

    assert invalid["status"] == "UNSUPPORTED"
    assert invalid["diagnostics"]["error"]["kind"] == "invalid"
    assert flag["status"] == "UNSUPPORTED"
    assert "gimsuy" in flag["detail"]


def test_accepts_unicode_pattern_and_recheck_supported_flags(configured_backend: tuple[Path, Path]) -> None:
    root, _ = configured_backend

    result = analyze_regex_cost(root, "^(é+)+$", "u")

    assert result["status"] == "VULNERABLE_MODEL", result
    assert result["diagnostics"]["source"] == "^(é+)+$"
    assert result["diagnostics"]["flags"] == "u"


def test_pattern_and_timeout_bounds_are_enforced(configured_backend: tuple[Path, Path]) -> None:
    root, _ = configured_backend

    too_long = analyze_regex_cost(root, "a" * (regex_cost.MAX_PATTERN_BYTES + 1))
    assert too_long["status"] == "UNSUPPORTED"
    assert "UTF-8 bytes" in too_long["detail"]
    with pytest.raises(ValueError, match="timeout_seconds"):
        analyze_regex_cost(root, "a", timeout_seconds=0)


def test_wrong_helper_hash_reports_unavailable(configured_backend: tuple[Path, Path],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = configured_backend
    monkeypatch.setenv("VKIT_REGEX_SHA256", "0" * 64)

    result = analyze_regex_cost(root, "^a+$")

    assert result["status"] == "UNAVAILABLE"
    assert result["backend_identity"] is None


def test_worker_deadline_is_unknown(configured_backend: tuple[Path, Path],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = configured_backend
    calls = 0

    def timed_out(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, b"v24.16.0", b"")
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(regex_cost.subprocess, "run", timed_out)
    result = analyze_regex_cost(root, "^(a+)+$", timeout_seconds=1)

    assert result["status"] == "UNKNOWN"
    assert result["detail"] == "regex analysis exceeded its total time budget"


def test_malformed_helper_output_is_unknown(configured_backend: tuple[Path, Path],
                                           monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    root, node = configured_backend
    helper = tmp_path / "helper.cjs"
    lockfile = tmp_path / "package-lock.json"
    source_lock = root / "tools" / "regex" / "package-lock.json"
    helper.write_text('process.stdout.write("{}")\n', encoding="utf-8")
    lockfile.write_bytes(source_lock.read_bytes())
    monkeypatch.setenv("VKIT_REGEX_HELPER", str(helper))
    monkeypatch.setenv("VKIT_REGEX_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_REGEX_LOCKFILE", str(lockfile))
    monkeypatch.setenv("VKIT_REGEX_LOCK_SHA256", hashlib.sha256(lockfile.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_NODE_BIN", str(node))

    result = analyze_regex_cost(root, "^a+$")

    assert result["status"] == "UNKNOWN"
    assert result["detail"] == "the pinned regex helper returned malformed diagnostics"

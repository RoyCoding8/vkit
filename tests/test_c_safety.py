from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from vkit.operations.c_safety import check_c_safety


@pytest.fixture
def configured_cbmc(monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = os.environ.get("VKIT_CBMC_BIN")
    expected = os.environ.get("VKIT_CBMC_SHA256")
    if not binary and not expected:
        pytest.skip("a pinned CBMC 6.11.0 runtime is not configured")
    if not binary or not expected:
        pytest.fail("the configured CBMC runtime requires both its path and SHA-256")
    path = Path(binary).resolve()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected.lower():
        pytest.fail("the configured CBMC runtime does not match its SHA-256")
    monkeypatch.setenv("VKIT_CBMC_BIN", str(path))
    monkeypatch.setenv("VKIT_CBMC_SHA256", expected.lower())
    return path


def _source(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")


def test_safe_and_signed_overflow_are_reported_from_real_cbmc(tmp_path, configured_cbmc, monkeypatch):
    _source(tmp_path, "safe.c", "int safe(int value) { if (value > 0) return value; return 0; }")
    _source(tmp_path, "overflow.c", "int overflow(int left, int right) { return left + right; }")
    monkeypatch.setenv("PATH", "")

    safe = check_c_safety(tmp_path, "safe.c", "safe")
    overflow = check_c_safety(tmp_path, "overflow.c", "overflow")

    assert safe["status"] == "SAFE"
    assert safe["backend"] == "cbmc/6.11.0"
    assert safe["backend_sha256"] == hashlib.sha256(configured_cbmc.read_bytes()).hexdigest()
    assert safe["source_sha256"] == hashlib.sha256((tmp_path / "safe.c").read_bytes()).hexdigest()
    assert "checks: bounds, pointer, division-by-zero, signed-overflow, undefined-shift" in safe["model_scope"]
    assert safe["selected_checks"] == [
        "bounds-check", "pointer-check", "div-by-zero-check", "signed-overflow-check",
        "undefined-shift-check", "unwinding-assertions",
    ]
    assert "snapshot_source" not in safe
    assert overflow["status"] == "COUNTEREXAMPLE"
    assert "int overflow(int left, int right)" in overflow["snapshot_source"]
    diagnostic = next(item for item in overflow["diagnostics"] if item["status"] == "FAILURE")
    assert diagnostic["property_class"] == "overflow"
    assert diagnostic["file"] == "overflow.c"
    assert diagnostic["line_basis"] == "normalized_snapshot"
    assert diagnostic["ruleid"]
    failure = next(step for step in diagnostic["trace"] if step.get("stepType") == "failure")
    assert failure["sourceLocation"]["file"] == "overflow.c"


def test_local_array_bounds_counterexample_and_unwind_exhaustion(tmp_path, configured_cbmc, monkeypatch):
    _source(tmp_path, "bounds.c", "int bounds(int index) { int values[2] = {0, 0}; return values[index]; }")
    _source(tmp_path, "loop.c", "int loop(int limit) { int index = 0; while (index < limit) { ++index; } return index; }")
    monkeypatch.setenv("PATH", "")

    bounds = check_c_safety(tmp_path, "bounds.c", "bounds")
    loop = check_c_safety(tmp_path, "loop.c", "loop", unwind=2)

    assert bounds["status"] == "COUNTEREXAMPLE"
    assert any(item["property_class"] == "array bounds" and item["status"] == "FAILURE"
               for item in bounds["diagnostics"])
    assert loop["status"] == "UNKNOWN"
    assert "unwind" in loop["detail"]
    assert "int loop(int limit)" in loop["snapshot_source"]
    assert any(item["message"].startswith("unwinding assertion") for item in loop["diagnostics"])


def test_division_and_shift_checks_return_traced_counterexamples(tmp_path, configured_cbmc):
    _source(tmp_path, "divide.c", "int divide(int numerator, int denominator) { return numerator / denominator; }")
    _source(tmp_path, "shift.c", "int shift(int value, int count) { return value << count; }")

    divide = check_c_safety(tmp_path, "divide.c", "divide")
    shift = check_c_safety(tmp_path, "shift.c", "shift")

    assert divide["status"] == "COUNTEREXAMPLE"
    assert any(item["property_class"] == "division-by-zero" and item["status"] == "FAILURE"
               for item in divide["diagnostics"])
    assert shift["status"] == "COUNTEREXAMPLE"
    assert any(item["property_class"] == "undefined-shift" and item["status"] == "FAILURE"
               for item in shift["diagnostics"])


@pytest.mark.parametrize("source", [
    "int malformed( { return 1; }",
    "#include <stdio.h>\nint candidate(void) { return 0; }",
    "int candidate(int value) { return __CPROVER_assume(value); }",
    "int candidate(int value) { return helper(value); }",
    "int state; int candidate(void) { return state; }",
    "int candidate(void) { int value; return value; }",
    "int candidate(int *value) { return *value; }",
    "int candidate(int value) { return (float)value; }",
    "int candidate(int value) { return (long)value; }",
    "int candidate(void) { return sizeof(double); }",
    "int candidate(void *value) { return 0; }",
    "int candidate(void *) { return 0; }",
    "int candidate(int [4]) { return 0; }",
    "int candidate(int i) { int a[2] = {0, 0}; return (a + i) == a; }",
    "int candidate(void) { int a[2] = {0, 0}; return (a + 3) == a; }",
    "int candidate(void) { int a[1] = {0}; int b[1] = {0}; return a < b; }",
    "int candidate(void) { int x = 0; return x++ + x++; }",
    "int candidate(void) { int x = 0; return (x = 1) + x; }",
    "int candidate(void) { int x = 0; int a[2] = {0}; return a[x++]; }",
    "int candidate(void) { int x = x; return x; }",
    "int candidate(void) { int a[2] = {a[1], 0}; return a[0]; }",
    "int candidate(void) { int x = 1; { int x = x; return x; } }",
])
def test_source_outside_the_supported_subset_is_unsupported(tmp_path, source):
    _source(tmp_path, "candidate.c", source)

    result = check_c_safety(tmp_path, "candidate.c", "candidate")

    assert result["status"] == "UNSUPPORTED"
    assert result["source_sha256"]


def test_cbmc_type_check_failure_is_unsupported(tmp_path, configured_cbmc):
    _source(tmp_path, "type-error.c", "int type_error(void) { return missing_name; }")

    result = check_c_safety(tmp_path, "type-error.c", "type_error")

    assert result["status"] == "UNSUPPORTED"
    assert "symbol" in result["detail"]


def test_path_escape_and_symlink_are_rejected_before_backend_use(tmp_path):
    outside = tmp_path.parent / "outside.c"
    outside.write_text("int safe(void) { return 0; }", encoding="utf-8")
    with pytest.raises(ValueError, match="inside the repository"):
        check_c_safety(tmp_path, "../outside.c", "safe")

    link = tmp_path / "linked.c"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows account")
    with pytest.raises(ValueError, match="symlinks"):
        check_c_safety(tmp_path, link.name, "safe")


def test_source_and_local_array_budgets_are_enforced(tmp_path):
    _source(tmp_path, "large-array.c", "int large(int index) { int values[257] = {0}; return values[index]; }")
    _source(tmp_path, "large-source.c", "int large_source(void) { return 0; }" + " " * 65_536)

    array = check_c_safety(tmp_path, "large-array.c", "large")

    assert array["status"] == "UNSUPPORTED"
    with pytest.raises(ValueError, match="at most 65536 bytes"):
        check_c_safety(tmp_path, "large-source.c", "large_source")


def test_missing_or_mismatched_runtime_is_unavailable(tmp_path, monkeypatch):
    _source(tmp_path, "safe.c", "int safe(void) { return 0; }")
    monkeypatch.delenv("VKIT_CBMC_BIN", raising=False)
    monkeypatch.delenv("VKIT_CBMC_SHA256", raising=False)

    missing = check_c_safety(tmp_path, "safe.c", "safe")

    fake = tmp_path / "not-cbmc.exe"
    fake.write_bytes(b"not a CBMC executable")
    monkeypatch.setenv("VKIT_CBMC_BIN", str(fake))
    monkeypatch.setenv("VKIT_CBMC_SHA256", "0" * 64)
    mismatch = check_c_safety(tmp_path, "safe.c", "safe")

    assert missing["status"] == mismatch["status"] == "UNAVAILABLE"


def test_hard_timeout_is_unknown(tmp_path, monkeypatch):
    _source(tmp_path, "safe.c", "int safe(void) { return 0; }")
    binary = tmp_path / "pinned-but-unresponsive.exe"
    binary.write_bytes(b"fixture")
    monkeypatch.setenv("VKIT_CBMC_BIN", str(binary))
    monkeypatch.setenv("VKIT_CBMC_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest())

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("vkit.operations.c_safety.subprocess.run", timeout)

    result = check_c_safety(tmp_path, "safe.c", "safe")

    assert result["status"] == "UNKNOWN"
    assert "time budget" in result["detail"]


@pytest.mark.parametrize("unwind,timeout", [(0, 10), (257, 10), (16, 0), (16, 61), (True, 10)])
def test_invalid_limits_are_rejected(tmp_path, unwind, timeout):
    with pytest.raises(ValueError):
        check_c_safety(tmp_path, "not-read.c", "safe", unwind=unwind, timeout_seconds=timeout)

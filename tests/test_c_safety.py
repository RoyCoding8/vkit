from __future__ import annotations

import hashlib
import os
import shutil
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
    gcc = shutil.which("gcc")
    if gcc is not None:
        compiler = Path(gcc).resolve()
        monkeypatch.setenv("VKIT_C_PREPROCESSOR_BIN", str(compiler))
        monkeypatch.setenv("VKIT_C_PREPROCESSOR_SHA256", hashlib.sha256(compiler.read_bytes()).hexdigest())
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


def _heap_program(tail: str) -> str:
    return (
        "int *acquire(void) { return malloc(sizeof(int)); }\n"
        "void release(int *pointer) { free(pointer); }\n"
        "int entry(int value) { int *pointer = acquire(); if (!pointer) return 0; "
        "*pointer = value; " + tail + " }"
    )


def test_heap_callgraph_handles_allocation_failure_and_balanced_release(tmp_path, configured_cbmc):
    _source(tmp_path, "heap.c", _heap_program("int result = *pointer; release(pointer); return result;"))

    result = check_c_safety(tmp_path, "heap.c", "entry", profile="heap-callgraph", timeout_seconds=30)

    assert result["status"] == "SAFE", result
    assert result["profile"] == "heap-callgraph"
    assert result["selected_checks"] == [
        "bounds-check", "pointer-check", "div-by-zero-check", "signed-overflow-check",
        "undefined-shift-check", "unwinding-assertions", "memory-leak-check", "library-precondition-check",
    ]
    assert "allocation can fail" in result["model_scope"]


@pytest.mark.parametrize("tail,description", [
    ("release(pointer); return *pointer;", "deallocated"),
    ("release(pointer); release(pointer); return 0;", "double free"),
    ("return *pointer;", "leak"),
])
def test_heap_lifetime_failures_have_actual_cbmc_traces(tmp_path, configured_cbmc, tail, description):
    _source(tmp_path, "heap.c", _heap_program(tail))

    result = check_c_safety(tmp_path, "heap.c", "entry", profile="heap-callgraph", timeout_seconds=30)

    assert result["status"] == "COUNTEREXAMPLE", result
    failures = [item for item in result["diagnostics"] if item["status"] == "FAILURE"]
    assert any(description in item["message"].lower() for item in failures), failures
    assert any(any(step.get("stepType") == "failure" for step in item["trace"] or []) for item in failures)


def test_heap_null_and_expired_stack_access_are_checked(tmp_path, configured_cbmc):
    _source(tmp_path, "null.c", "int entry(int value) { int *p = malloc(sizeof(int)); *p = value; free(p); return 0; }")
    _source(tmp_path, "stack.c", "int *acquire(int value) { int local = value; return &local; } int entry(int value) { int *p = acquire(value); return *p; }")

    null = check_c_safety(tmp_path, "null.c", "entry", profile="heap-callgraph", timeout_seconds=30)
    stack = check_c_safety(tmp_path, "stack.c", "entry", profile="heap-callgraph", timeout_seconds=30)

    assert null["status"] == "COUNTEREXAMPLE", null
    assert stack["status"] == "COUNTEREXAMPLE", stack
    assert any("NULL" in item["message"] and item["status"] == "FAILURE" for item in null["diagnostics"])
    assert any("dead object" in item["message"] and item["status"] == "FAILURE" for item in stack["diagnostics"])


@pytest.mark.parametrize("source", [
    "int entry(int value) { return __CPROVER_assume(value); }",
    "int entry(int value) { int *p = malloc(value); free(p); return 0; }",
    "int entry(int value) { return entry(value); }",
    "int entry(int value) { int *p = malloc(sizeof(int)); return helper(p); }",
    "void *malloc(unsigned long size) { return 0; } int entry(void) { return 0; }",
    "int entry(int value) { int **p = 0; return value; }",
    "int global = 0; int entry(void) { return global; }",
    "int entry(int *pointer) { return *pointer; }",
    "int entry(int value) { int result; return result; }",
    "int entry(int value) { int x = 0; return x++ + x++; }",
])
def test_heap_profile_rejects_harness_overrides_and_unmodeled_source(tmp_path, source):
    _source(tmp_path, "heap.c", source)

    result = check_c_safety(tmp_path, "heap.c", "entry", profile="heap-callgraph")

    assert result["status"] == "UNSUPPORTED", result


def test_heap_loop_requires_complete_unwinding(tmp_path, configured_cbmc):
    _source(tmp_path, "loop.c", "int entry(unsigned int limit) { unsigned int index = 0; while (index < limit) { index++; } return 0; }")

    result = check_c_safety(tmp_path, "loop.c", "entry", profile="heap-callgraph", unwind=2)

    assert result["status"] == "UNKNOWN", result
    assert "unwind" in result["detail"]


def test_heap_preprocessor_is_operator_pinned(tmp_path, configured_cbmc, monkeypatch):
    _source(tmp_path, "heap.c", "int entry(void) { return 0; }")
    monkeypatch.delenv("VKIT_C_PREPROCESSOR_BIN", raising=False)
    monkeypatch.delenv("VKIT_C_PREPROCESSOR_SHA256", raising=False)

    result = check_c_safety(tmp_path, "heap.c", "entry", profile="heap-callgraph")

    assert result["status"] == "UNAVAILABLE"
    assert "preprocessor" in result["detail"]


def test_c_profile_cli_and_mcp(make_project, configured_cbmc):
    from helpers import McpClient, vkit

    project = make_project({}, [], accept=False)
    _source(project, "heap.c", _heap_program("release(pointer); return *pointer;"))
    code, body = vkit(project, "compute", "check-c-safety", "--path", "heap.c", "--function", "entry",
                      "--profile", "heap-callgraph", "--timeout-seconds", "30")
    assert (code, body["status"]) == (1, "COUNTEREXAMPLE"), body
    client = McpClient(project)
    try:
        body, error = client.call("check_c_safety", path="heap.c", function="entry",
                                 profile="heap-callgraph", timeout_seconds=30)
        assert not error and body["status"] == "COUNTEREXAMPLE", body
    finally:
        client.close()

from __future__ import annotations

import sys

import pytest

from helpers import McpClient, vkit

from vkit.operations.affine_accesses import _lower_accesses, analyze_affine_accesses


def test_access_operation_cli_and_mcp(make_project):
    project = make_project({"kernel.py": "def kernel(n: int):\n    for i in range(n):\n        A[i] = A[i - 1]\n"}, [], accept=False)
    code, body = vkit(project, "compute", "analyze-affine-accesses", "--path", "kernel.py", "--function", "kernel")
    assert code == (5 if sys.platform == "win32" else 0)
    _backend_status(body)
    client = McpClient(project)
    try:
        body, error = client.call("analyze_affine_accesses", path="kernel.py", function="kernel")
        assert not error
        _backend_status(body)
    finally:
        client.close()


def _analyze(tmp_path, source: str) -> dict:
    path = tmp_path / "module.py"
    path.write_text(source, encoding="utf-8")
    return analyze_affine_accesses(tmp_path, path.name, "kernel", timeout_ms=10_000)


def _backend_status(result: dict) -> None:
    if sys.platform == "win32":
        assert result["status"] == "UNAVAILABLE"
        assert result["backend_version"] is None
    else:
        assert result["status"] == "ANALYZED"
        assert result["backend_version"] == "2026.2.2"


def test_forward_stencil_reports_replayed_raw_relation(tmp_path):
    result = _analyze(tmp_path, """def kernel(n: int):
    for i in range(n):
        A[i] = A[i - 1]
""")
    _backend_status(result)
    if sys.platform != "win32":
        raw = [item for item in result["relations"] if item["kind"] == "RAW"]
        assert raw
        assert any(item["witness"]["replayed"] for item in raw)
        assert any(item["witness"]["later"]["iterations"]["i"] ==
                   item["witness"]["earlier"]["iterations"]["i"] + 1 for item in raw)
        assert all("array" in item and "relation" in item for item in raw)


def test_read_before_store_augmented_update_is_war_with_exact_iteration_order(tmp_path):
    result = _analyze(tmp_path, """def kernel(n: int):
    for i in range(n):
        A[i] += 1
""")
    _backend_status(result)
    if sys.platform != "win32":
        assert any(item["kind"] == "WAR" and item["witness"]["earlier"]["iterations"] ==
                   item["witness"]["later"]["iterations"] for item in result["relations"])


def test_branch_guard_is_part_of_the_access_relation(tmp_path):
    result = _analyze(tmp_path, """def kernel(n: int):
    for i in range(n):
        if i > 0:
            A[i] = A[i - 1]
""")
    _backend_status(result)
    if sys.platform != "win32":
        raw = [item for item in result["relations"] if item["kind"] == "RAW"]
        assert raw
        assert all(item["witness"]["earlier"]["iterations"]["i"] > 0 for item in raw)


def test_descending_range_uses_execution_order_not_coordinate_order(tmp_path):
    result = _analyze(tmp_path, """def kernel(n: int):
    for i in range(n - 1, -1, -1):
        A[i] = A[i + 1]
""")
    _backend_status(result)
    if sys.platform != "win32":
        raw = [item for item in result["relations"] if item["kind"] == "RAW"]
        assert raw
        assert any(item["witness"]["earlier"]["iterations"]["i"] ==
                   item["witness"]["later"]["iterations"]["i"] + 1 for item in raw)


def test_different_named_arrays_do_not_alias_under_the_fixed_profile(tmp_path):
    result = _analyze(tmp_path, """def kernel(n: int):
    for i in range(n):
        A[i] = B[i]
""")
    _backend_status(result)
    if sys.platform != "win32":
        assert result["relations"] == []
        assert "different named array bases denote distinct arrays" in result["assumptions"]


@pytest.mark.parametrize("source", [
    "def kernel(n: int):\n    for i in range(n):\n        A[i] = A[i]\n    for j in range(n):\n        B[j] = B[j]\n",
    "def kernel(n: int):\n    for i in range(n):\n        A[i + j] = 0\n",
    "def kernel(n: int):\n    for i in range(n):\n        A[i:i + 1] = 0\n",
    "def kernel(n: int):\n    for i in range(n):\n        A[i] = open('input').read()\n",
    "def kernel(A: int):\n    for i in range(3):\n        A[i] = 0\n",
    "def kernel(n: int):\n    for A in range(n):\n        A[0] = 0\n",
])
def test_rejects_unmodeled_loop_or_access_semantics(source):
    with pytest.raises(ValueError):
        _lower_accesses(source, "kernel")


def test_source_capture_reports_digest_and_scope(tmp_path):
    result = _analyze(tmp_path, "def kernel(n: int):\n    for i in range(n):\n        A[i] = A[i]\n")
    assert result["source_sha256"]
    assert "distinct abstract arrays by assumption" in result["model_scope"]
    assert result["timeout_ms"] == 10_000


def test_timeout_is_validated_before_backend_start(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("def kernel(n: int):\n    for i in range(n):\n        A[i] = A[i]\n")
    with pytest.raises(ValueError, match="timeout_ms"):
        analyze_affine_accesses(tmp_path, path.name, "kernel", timeout_ms=0)


def test_failed_independent_witness_replay_never_reports_analyzed(monkeypatch):
    import vkit.operations.affine_accesses as operation

    pytest.importorskip("islpy", reason="ISL backend is not installed")

    source = "def kernel(n: int):\n    for i in range(n):\n        A[i] = A[i - 1]\n"
    monkeypatch.setattr(operation, "_check_witness", lambda *args: None)
    result = operation._worker(source, "kernel")
    assert result["status"] == "UNKNOWN"
    assert "witness failed independent replay" in result["reason"]


def test_access_relations_do_not_claim_parallel_safety(tmp_path):
    result = _analyze(tmp_path, "def kernel(n: int):\n    for i in range(n):\n        A[i] = A[i - 1]\n")
    _backend_status(result)
    assert "parallel_safe" not in result
    assert not any("parallel" in key.lower() for relation in result.get("relations", []) for key in relation)

from __future__ import annotations

import json
import os
import sys

import pytest

from helpers import DRIVER, McpClient, scenario_check, vkit


def _project(make_project, files):
    driver = DRIVER + 'report([("ok", True, "ok")])\n'
    return make_project({**files, "driver.py": driver}, [
        scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py"]),
    ])


def test_cover_cli_and_mcp_use_the_same_fixed_model(make_project):
    pytest.importorskip("ortools")
    matrix = {"version": 1, "required": ["parse", "validate"], "candidates": [
        {"id": "parse", "cost": 3, "covers": ["parse"]},
        {"id": "validate", "cost": 4, "covers": ["validate"]},
        {"id": "both", "cost": 5, "covers": ["parse", "validate"]},
    ]}
    project = _project(make_project, {"matrix.json": json.dumps(matrix)})
    code, result = vkit(project, "compute", "minimize-cover", "--path", "matrix.json")
    assert (code, result["status"], result["selected"], result["total_cost"]) == (0, "OPTIMAL", ["both"], 5)
    client = McpClient(project)
    try:
        gate_before, _ = client.call("gate")
        result, error = client.call("minimize_cover", path="matrix.json")
        assert not error
        assert (result["status"], result["selected"], result["total_cost"]) == ("OPTIMAL", ["both"], 5)
        assert client.call("gate")[0] == gate_before
        refused, error = client.call("minimize_cover", path="matrix.json", solver_script="untrusted")
        assert error and "solver_script" in refused["error"]
    finally:
        client.close()


def test_protobuf_cli_and_mcp_cannot_disable_wire_rules(make_project):
    if not os.environ.get("VKIT_BUF_BIN"):
        pytest.skip("pinned Buf binary is not configured")
    project = _project(make_project, {
        "old/api.proto": 'syntax = "proto3"; package api; message Item { string value = 1; }',
        "new/api.proto": 'syntax = "proto3"; package api; message Item { int32 value = 1; }',
        "new/buf.yaml": "version: v2\nbreaking:\n  use: []\n  except: [FIELD_SAME_TYPE]\n",
    })
    code, result = vkit(project, "compute", "check-proto-compatibility", "--old-path", "old", "--new-path", "new")
    assert (code, result["status"], result["category"]) == (1, "BREAKING", "WIRE_JSON")
    assert result["diagnostics"]
    client = McpClient(project)
    try:
        gate_before, _ = client.call("gate")
        result, error = client.call("check_proto_compatibility", old_path="old", new_path="new")
        assert not error and result["status"] == "BREAKING"
        assert client.call("gate")[0] == gate_before
        refused, error = client.call("check_proto_compatibility", old_path="old", new_path="new", ignore=["api.proto"])
        assert error and "ignore" in refused["error"]
    finally:
        client.close()


def test_affine_generator_comparison_through_cli_and_mcp(make_project):
    if sys.platform != "win32":
        pytest.importorskip("islpy")
    source = "def points(n: int):\n    for i in range(n):\n        yield (i,)\n"
    replacement = "def points(n: int):\n    for i in range(n - 1, -1, -1):\n        yield (i,)\n"
    project = _project(make_project, {"loops.py": source, "candidate.py": replacement})
    code, result = vkit(project, "compute", "compare-iteration-sets", "--path", "loops.py",
                        "--function", "points", "--replacement-file", str(project / "candidate.py"))
    expected = "UNAVAILABLE" if sys.platform == "win32" else "EQUIVALENT"
    assert (code, result["status"]) == (5 if sys.platform == "win32" else 0, expected)
    client = McpClient(project)
    try:
        gate_before, _ = client.call("gate")
        result, error = client.call("compare_iteration_sets", path="loops.py", function="points", replacement=replacement)
        assert not error and result["status"] == expected
        assert client.call("gate")[0] == gate_before
        refused, error = client.call("compare_iteration_sets", path="loops.py", function="points",
                                     replacement=replacement, solver_assertions="untrusted")
        assert error and "solver_assertions" in refused["error"]
    finally:
        client.close()


def test_c_safety_cli_and_mcp_preserve_fixed_checks(make_project):
    project = _project(make_project, {"bump.c": "int bump(int value) { return value + 1; }"})
    code, result = vkit(project, "compute", "check-c-safety", "--path", "bump.c", "--function", "bump")
    expected = "COUNTEREXAMPLE" if os.environ.get("VKIT_CBMC_BIN") else "UNAVAILABLE"
    assert (code, result["status"]) == (1 if expected == "COUNTEREXAMPLE" else 5, expected)
    if expected == "COUNTEREXAMPLE":
        assert any(item["property_class"] == "overflow" and item["status"] == "FAILURE"
                   for item in result["diagnostics"])
        assert "int bump(int value)" in result["snapshot_source"]
    client = McpClient(project)
    try:
        gate_before, _ = client.call("gate")
        result, error = client.call("check_c_safety", path="bump.c", function="bump")
        assert not error and result["status"] == expected
        assert client.call("gate")[0] == gate_before
        refused, error = client.call("check_c_safety", path="bump.c", function="bump", disable_checks=True)
        assert error and "disable_checks" in refused["error"]
    finally:
        client.close()

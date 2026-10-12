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
    code, result = vkit(project, "compute", "compare-contracts", "--old-path", "old", "--new-path", "new",
                        "--profile", "protobuf-wire-json")
    assert (code, result["status"], result["category"]) == (1, "BREAKING", "WIRE_JSON")
    assert result["diagnostics"]
    client = McpClient(project)
    try:
        gate_before, _ = client.call("gate")
        result, error = client.call("compare_contracts", old_path="old", new_path="new", profile="protobuf-wire-json")
        assert not error and result["status"] == "BREAKING"
        assert client.call("gate")[0] == gate_before
        refused, error = client.call("compare_contracts", old_path="old", new_path="new", profile="protobuf-wire-json",
                                    ignore=["api.proto"])
        assert error and "ignore" in refused["error"]
    finally:
        client.close()


def test_openapi_contracts_are_discoverable_scoped_and_switchable(make_project):
    if not os.environ.get("VKIT_OASDIFF_BIN"):
        pytest.skip("pinned oasdiff binary is not configured")
    old = {"openapi": "3.0.3", "info": {"title": "items", "version": "1"}, "paths": {
        "/items": {"post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"name": {"type": "string"}}}}}},
            "responses": {"200": {"description": "ok"}}}}}}
    new = json.loads(json.dumps(old))
    new["paths"]["/items"]["post"]["requestBody"]["content"]["application/json"]["schema"]["required"] = ["name"]
    project = _project(make_project, {"old.json": json.dumps(old), "new.json": json.dumps(new)})
    code, result = vkit(project, "compute", "compare-contracts", "--old-path", "old.json",
                        "--new-path", "new.json", "--profile", "openapi")
    assert (code, result["status"], result["profile"], result["operation"]) == (
        1, "BREAKING", "openapi", "compare_contracts")
    assert result["diagnostics"]
    client = McpClient(project)
    try:
        before, _ = client.call("gate")
        found, error = client.call("help", query="OpenAPI")
        assert not error and {tool["name"] for tool in found["tools"]} == {
            "check_api_sequences", "compare_contracts", "inspect_openapi_parser_consistency"}
        result, error = client.call("compare_contracts", old_path="old.json", new_path="new.json", profile="openapi")
        assert not error and result["status"] == "BREAKING"
        assert client.call("gate")[0] == before
        result, error = client.call("compare_contracts", old_path="old.json", new_path="new.json",
                                    profile="openapi", ignore=["request-property-became-required"])
        assert error and "ignore" in result["error"]
        code, _ = vkit(project, "tools", "--disable", "compare_contracts")
        assert code == 0
        found, error = client.call("help", query="OpenAPI")
        assert not error and {tool["name"] for tool in found["tools"]} == {
            "check_api_sequences", "inspect_openapi_parser_consistency"}
        result, error = client.call("compare_contracts", old_path="old.json", new_path="new.json", profile="openapi")
        assert error and result["error"] == "unknown or unavailable tool"
    finally:
        client.close()


def test_dependency_queries_use_shared_cli_mcp_and_do_not_change_gate(make_project):
    if not os.environ.get("VKIT_DEPENDENCY_HELPER"):
        pytest.skip("pinned dependency-cruiser helper is not configured")
    project = _project(make_project, {
        "src/entry.js": "import { value } from './model.js'; export const result = value;\n",
        "src/model.js": "export const value = 1;\n",
        "src/unused.js": "export const spare = 2;\n",
    })
    code, result = vkit(project, "compute", "analyze-dependencies", "--path", "src", "--query", "path",
                        "--sources", '["src/entry.js"]', "--target", "src/model.js")
    assert (code, result["status"]) == (0, "ANALYZED")
    assert result["result"] == {"source": "src/entry.js", "target": "src/model.js",
                                "witness": ["src/entry.js", "src/model.js"]}
    client = McpClient(project)
    try:
        before, _ = client.call("gate")
        found, error = client.call("help", query="dependency graph")
        assert not error and [tool["name"] for tool in found["tools"]] == ["analyze_dependencies"]
        result, error = client.call("analyze_dependencies", path="src", query="unreachable", sources=["src/entry.js"])
        assert not error and result["status"] == "ANALYZED"
        assert result["result"] == {"entrypoints": ["src/entry.js"],
                                    "reachable": ["src/entry.js", "src/model.js"],
                                    "unreachable": ["src/unused.js"]}
        result, error = client.call("analyze_dependencies", path="src", query="path",
                                    sources=["src/unused.js"], target="src/model.js")
        assert not error and result["status"] == "ANALYZED"
        assert result["result"]["witness"] is None
        assert client.call("gate")[0] == before
        result, error = client.call("analyze_dependencies", path="src", query="graph", config=".dependency-cruiser.cjs")
        assert error and "config" in result["error"]
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

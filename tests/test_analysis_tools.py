from __future__ import annotations

import json
import os

import pytest

from helpers import McpClient, vkit

_TOOLS = {
    "compare_authorization_policies": ("EQUIVALENT", 0),
    "compare_rational_expressions": ("IDENTICAL", 0),
    "compare_sql_queries": ("EQUIVALENT_WITHIN_BOUNDS", 0),
    "compare_wheels": ("SAME_INSTALL_MAPPING", 0),
    "find_behavioral_difference": ("COUNTEREXAMPLE", 1),
}


def _inputs(project):
    bundles = {
        "algebra.json": {"version": 1, "variables": ["x"], "before": {"var": "x"}, "after": {"var": "x"}},
        "behavior.json": {"version": 1, "function": "f", "before": "def f(x: int) -> int:\n    return x\n",
                          "after": "def f(x: int) -> int:\n    return x + 1\n"},
        "sql.json": {"version": 1, "tables": [{"name": "t", "max_rows": 2,
                     "columns": [{"name": "n", "type": "integer", "nullable": True, "min": 0, "max": 1}]}],
                     "before": "SELECT n FROM t WHERE n IS NOT NULL", "after": "SELECT n FROM t WHERE n = n"},
        "policies.json": {"version": 1, "schema": 'entity User; entity Document; action "view" appliesTo {principal: [User], resource: [Document], context: {}};',
                          "action": 'Action::"view"', "principal_type": "User", "resource_type": "Document",
                          "before": ["permit(principal, action, resource);"], "after": ["permit(principal, action, resource);"]},
    }
    for path, bundle in bundles.items():
        (project / path).write_text(json.dumps(bundle), encoding="utf-8")


@pytest.mark.parametrize("name", sorted(_TOOLS))
def test_new_analysis_tools_work_through_cli_and_real_mcp(make_project, name):
    if name == "compare_authorization_policies" and not os.environ.get("VKIT_CEDAR_PATH"):
        if os.environ.get("VKIT_REQUIRE_POLICY_ENGINE") == "1":
            pytest.fail("CI requires the policy engine")
        pytest.skip("release-pinned policy engine is not configured")
    for package in {"compare_rational_expressions": ["sympy"], "find_behavioral_difference": ["crosshair"],
                    "compare_sql_queries": ["sqlglot", "cvc5"], "compare_wheels": ["packaging"]}.get(name, []):
        pytest.importorskip(package)
    project = make_project({}, [], accept=False)
    _inputs(project)
    arguments = {"path": {"compare_rational_expressions": "algebra.json",
                          "find_behavioral_difference": "behavior.json", "compare_sql_queries": "sql.json",
                          "compare_authorization_policies": "policies.json"}.get(name)}
    if name == "compare_wheels":
        from test_wheel_artifacts import _wheel_bytes

        (project / "demo-1.0-py3-none-any.whl").write_bytes(_wheel_bytes({"demo.py": b"value = 1\n"}))
        arguments = {"before": "demo-1.0-py3-none-any.whl", "after": "demo-1.0-py3-none-any.whl"}
    argv = [item for key, value in arguments.items() for item in ("--" + key.replace("_", "-"), value)]
    code, cli = vkit(project, "compute", name.replace("_", "-"), *argv)
    expected, exit_code = _TOOLS[name]
    assert (code, cli["status"]) == (exit_code, expected)
    client = McpClient(project)
    try:
        found, error = client.call("help", query=name)
        assert not error and [tool["name"] for tool in found["tools"]] == [name]
        body, error = client.call(name, **arguments)
        assert not error and body["status"] == expected
    finally:
        client.close()


def test_all_new_tools_can_be_hidden_from_a_fresh_session(make_project):
    project = make_project({}, [], accept=False)
    for name in _TOOLS:
        code, _ = vkit(project, "tools", "--disable", name)
        assert code == 0
    client = McpClient(project)
    try:
        listed = client.request("tools/list", {})["tools"]
        found, error = client.call("help")
        assert not error and "tools" not in found
        assert not set(_TOOLS) & {tool["name"] for tool in listed}
        for name in _TOOLS:
            body, error = client.call("help", query=name)
            assert not error and body["tools"] == []
            body, error = client.call(name, path="unreachable.json")
            assert error and body == {"error": "unknown or unavailable tool"}
    finally:
        client.close()

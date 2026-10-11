from __future__ import annotations

import json

import pytest

from helpers import DRIVER, McpClient, scenario_check, vkit


def _project(make_project):
    return make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n'},
                        [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])], accept=False)


def test_help_search_and_saved_visibility_across_real_mcp_sessions(make_project):
    project = _project(make_project)
    client = McpClient(project)
    try:
        listed = client.request("tools/list", {})["tools"]
        help_result, error = client.call("help")
        assert not error and help_result == {"contract_version": 1, "tools": listed}
        result, error = client.call("help", query="regex alphabet")
        assert not error and [tool["name"] for tool in result["tools"]] == ["compare_matchsets"]
        schema = result["tools"][0]["inputSchema"]
        assert schema["required"] == ["old_pattern", "new_pattern", "alphabet"]
        assert schema["properties"]["timeout_ms"]["default"] == 2000

        for name in ("compare_matchsets", "propose"):
            code, body = vkit(project, "tools", "--disable", name)
            assert code == 0 and name not in [tool["name"] for tool in body["tools"]]
        body, error = client.call("compare_matchsets", old_pattern="a", new_pattern="a", alphabet="a")
        assert error and body == {"error": "unknown or unavailable tool"}
    finally:
        client.close()

    client = McpClient(project)
    try:
        listed = client.request("tools/list", {})["tools"]
        help_result, error = client.call("help")
        assert not error and help_result["tools"] == listed
        assert "compare_matchsets" not in json.dumps([listed, help_result, client.initialize])
        assert "propose" not in json.dumps([listed, help_result, client.initialize])
        body, error = client.call("help", query="compare_matchsets")
        assert not error and body["tools"] == []
        body, error = client.call("propose", rationale="should never be dispatched")
        assert error and body == {"error": "unknown or unavailable tool"}
        code, body = vkit(project, "mcp", "serve")
        assert code == 0 and body["tools"] == listed
        code, body = vkit(project, "compute", "compare-matchsets", "--old-pattern", "a",
                          "--new-pattern", "a", "--alphabet", "a")
        assert code == 2 and body["error"] == "unknown or unavailable tool"
    finally:
        client.close()

    code, body = vkit(project, "tools", "--enable", "compare_matchsets", "--query", "compare_matchsets")
    assert code == 0 and [tool["name"] for tool in body["tools"]] == ["compare_matchsets"]
    client = McpClient(project)
    try:
        listed = client.request("tools/list", {})["tools"]
        assert "compare_matchsets" in [tool["name"] for tool in listed]
        assert "propose" not in [tool["name"] for tool in listed]
    finally:
        client.close()


def test_old_config_is_preserved_and_visibility_changes_are_idempotent(make_project):
    project = _project(make_project)
    path = project / ".git" / "vkit" / "config.json"
    old = {"version": 1, "theme": "dark", "sidebar_collapsed": True,
           "refresh_seconds": 9, "history_limit": 17}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old), encoding="utf-8")
    for _ in range(2):
        code, _ = vkit(project, "tools", "--disable", "check_history")
        assert code == 0
        assert json.loads(path.read_text()) == {**old, "disabled_tools": ["check_history"]}
    before = path.read_bytes()
    for name in ("help", "missing_tool"):
        code, body = vkit(project, "tools", "--disable", name)
        assert code == 2 and body["error"] == "choose an installed tool other than help"
        assert path.read_bytes() == before
    for _ in range(2):
        code, _ = vkit(project, "tools", "--enable", "check_history")
        assert code == 0
        assert json.loads(path.read_text()) == {**old, "disabled_tools": []}


def test_mutation_contract_is_searchable_and_hidden_when_disabled(make_project):
    project = _project(make_project)
    client = McpClient(project)
    try:
        body, error = client.call("help", query="mutations survivors")
        assert not error and [tool["name"] for tool in body["tools"]] == ["mutation_survivors"]
        tool = body["tools"][0]
        assert tool["annotations"] == {"readOnlyHint": False}
        assert tool["inputSchema"]["required"] == ["run_id", "path"]
        assert tool["inputSchema"]["properties"]["max_mutants"]["default"] == 20
        body, error = client.call("mutation_survivors", run_id="missing", path="src.js", max_mutants=101)
        assert error and "100" in body["error"]
        body, error = client.call("mutation_survivors", run_id="missing", path="src.js", command="node anything")
        assert error and body == {"error": "Additional properties are not allowed ('command' was unexpected)"}
    finally:
        client.close()
    code, _ = vkit(project, "tools", "--disable", "mutation_survivors")
    assert code == 0
    client = McpClient(project)
    try:
        body, error = client.call("help", query="mutations survivors")
        assert not error and body["tools"] == []
        body, error = client.call("mutation_survivors", run_id="missing", path="src.js")
        assert error and body == {"error": "unknown or unavailable tool"}
    finally:
        client.close()


def test_help_is_available_when_every_other_tool_is_disabled(make_project):
    project = _project(make_project)
    client = McpClient(project)
    try:
        disabled = [tool["name"] for tool in client.request("tools/list", {})["tools"] if tool["name"] != "help"]
    finally:
        client.close()
    path = project / ".git" / "vkit" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "theme": "system", "sidebar_collapsed": False,
                                "refresh_seconds": 2, "history_limit": 50, "disabled_tools": disabled}),
                    encoding="utf-8")
    client = McpClient(project)
    try:
        listed = client.request("tools/list", {})["tools"]
        assert [tool["name"] for tool in listed] == ["help"]
        body, error = client.call("help")
        assert not error and body == {"contract_version": 1, "tools": listed}
    finally:
        client.close()


def test_disabled_protobuf_tool_stays_disabled_after_contract_consolidation(make_project):
    project = _project(make_project)
    path = project / ".git" / "vkit" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "theme": "system", "sidebar_collapsed": False,
                               "refresh_seconds": 2, "history_limit": 50,
                               "disabled_tools": ["check_proto_compatibility"]}), encoding="utf-8")
    client = McpClient(project)
    try:
        listed = client.request("tools/list", {})["tools"]
        assert "compare_contracts" not in [tool["name"] for tool in listed]
        body, error = client.call("help", query="Protobuf")
        assert not error and body["tools"] == []
    finally:
        client.close()


@pytest.mark.parametrize("disabled", [["help"], ["gate", "gate"], "gate", ["bad-name"]])
def test_invalid_visibility_fails_closed_without_rewriting(make_project, disabled):
    project = _project(make_project)
    path = project / ".git" / "vkit" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {"version": 1, "theme": "system", "sidebar_collapsed": False,
              "refresh_seconds": 2, "history_limit": 50, "disabled_tools": disabled}
    path.write_text(json.dumps(config), encoding="utf-8")
    before = path.read_bytes()
    code, body = vkit(project, "mcp", "serve")
    assert code == 2 and "invalid settings" in body["error"]
    assert path.read_bytes() == before

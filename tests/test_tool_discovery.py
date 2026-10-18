from __future__ import annotations

import json
from dataclasses import replace

import pytest

from helpers import DRIVER, McpClient, scenario_check, vkit
from vkit.mcp import Server, Tool
from vkit.paths import open_project
from vkit.settings import Settings, load_settings, save_settings


def _project(make_project):
    return make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n'},
                        [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])], accept=False)


def test_compact_discovery_exact_contract_and_invocation(make_project):
    project = _project(make_project)
    client = McpClient(project)
    try:
        listed = client.request("tools/list", {})["tools"]
        assert [tool["name"] for tool in listed] == ["help", "invoke"]
        assert len(json.dumps(listed).encode()) < 4096
        assert listed[1]["annotations"]["readOnlyHint"] is False
        categories, error = client.call("help")
        assert not error and categories["contract_version"] == 2 and "tools" not in categories
        assert {group["id"] for group in categories["categories"]} >= {"repository", "safety", "equivalence"}

        result, error = client.call("help", category="repository", query="run_cancel")
        assert not error and [tool["name"] for tool in result["tools"]] == ["run_cancel"]
        assert "inputSchema" not in result["tools"][0]
        exact, error = client.call("help", name="compare_matchsets")
        assert not error
        schema = exact["tools"][0]["inputSchema"]
        assert schema["required"] == ["old_pattern", "new_pattern", "alphabet"]
        assert schema["properties"]["timeout_ms"]["default"] == 2000
        assert exact["tools"][0]["category"] == "contracts_languages"
        mutation, error = client.call("help", name="mutation_survivors")
        assert not error and mutation["tools"][0]["annotations"] == {"readOnlyHint": False}
        assert mutation["tools"][0]["inputSchema"]["required"] == ["run_id", "path"]
        assert mutation["tools"][0]["inputSchema"]["properties"]["max_mutants"]["default"] == 20
        body, error = client.call("mutation_survivors", run_id="missing", path="src.js", max_mutants=101)
        assert error and "100" in body["error"]
        body, error = client.call("mutation_survivors", run_id="missing", path="src.js", command="node anything")
        assert error and body == {"error": "Additional properties are not allowed ('command' was unexpected)"}

        body, error = client.call("invoke", name="gate", arguments={})
        assert not error and body["verdict"] == "BLOCKED"
        raw = client.request("tools/call", {"name": "gate", "arguments": {}})
        assert raw["isError"] and json.loads(raw["content"][0]["text"]) == {
            "error": "unknown or unavailable tool"}
        for name in ("help", "invoke", "missing_tool"):
            body, error = client.call("invoke", name=name, arguments={})
            assert error and body == {"error": "unknown or unavailable tool"}
        body, error = client.call("invoke", name="check_run", arguments={"command": "anything"})
        assert error and body == {"error": "unknown argument(s): command"}
    finally:
        client.close()


@pytest.mark.parametrize("exposure", ["compact", "named"])
def test_live_category_precedence_preserves_individual_choices(make_project, exposure):
    project = _project(make_project)
    client = McpClient(project, exposure=exposure)
    try:
        assert client.call("gate")[0]["verdict"] == "BLOCKED"
        assert vkit(project, "tools", "--disable", "status")[0] == 0
        assert vkit(project, "tools", "--disable-category", "repository")[0] == 0
        body, error = client.call("help")
        assert not error and "repository" not in {group["id"] for group in body["categories"]}
        for name in ("gate", "status", "run_cancel"):
            assert client.call(name) == ({"error": "unknown or unavailable tool"}, True)
            assert client.call("help", name=name) == ({"error": "unknown or unavailable tool"}, True)
        body, error = client.call("help", query="run_cancel")
        assert not error and body["tools"] == []
        if exposure == "named":
            listed = client.request("tools/list", {})["tools"]
            assert not {"gate", "status", "run_cancel"} & {tool["name"] for tool in listed}
        assert vkit(project, "tools", "--enable-category", "repository")[0] == 0
        assert client.call("gate")[0]["verdict"] == "BLOCKED"
        assert client.call("status") == ({"error": "unknown or unavailable tool"}, True)
        assert load_settings(open_project(project)).disabled_tools == ("status",)
    finally:
        client.close()
    code, body = vkit(project, "compute", "compare-matchsets", "--old-pattern", "a",
                      "--new-pattern", "a", "--alphabet", "a")
    assert code == 0 and body["status"] == "EQUIVALENT"
    assert vkit(project, "tools", "--disable-category", "contracts_languages")[0] == 0
    code, body = vkit(project, "compute", "compare-matchsets", "--old-pattern", "a",
                      "--new-pattern", "a", "--alphabet", "a")
    assert code == 2 and body == {"error": "unknown or unavailable tool", "exit_code": 2}


@pytest.mark.parametrize("exposure", ["compact", "named"])
@pytest.mark.parametrize("scope", [[], ["repository"]])
def test_startup_scope_cannot_be_widened_by_discovery_or_known_names(make_project, exposure, scope):
    project = _project(make_project)
    client = McpClient(project, exposure=exposure, categories=scope)
    try:
        listed = client.request("tools/list", {})["tools"]
        if exposure == "named":
            assert all(tool["name"] in {"help", "status", "features", "check_run", "run_get",
                                        "run_cancel", "propose", "gate"} for tool in listed)
            if not scope:
                assert [tool["name"] for tool in listed] == ["help"]
        result, error = client.call("help", all=True, limit=25)
        assert not error and {tool["category"] for tool in result["tools"]} <= set(scope)
        for name in ("compare_matchsets", "compare_migrations"):
            assert client.call("help", name=name) == ({"error": "unknown or unavailable tool"}, True)
            assert client.call(name) == ({"error": "unknown or unavailable tool"}, True)
        settings = load_settings(open_project(project))
        save_settings(open_project(project), replace(settings, mcp_categories=("data",)))
        result, error = client.call("help")
        assert not error and {group["id"] for group in result["categories"]} == set(scope)
    finally:
        client.close()
    fresh = McpClient(project)
    try:
        result, error = fresh.call("help")
        assert not error and [group["id"] for group in result["categories"]] == ["data"]
    finally:
        fresh.close()
    code, body = vkit(project, "mcp", "serve", "--exposure", "named", "--categories", "all")
    assert code == 0 and "gate" in {tool["name"] for tool in body["tools"]}


def test_help_pagination_filters_and_invalid_selectors(make_project):
    client = McpClient(_project(make_project))
    try:
        first, error = client.call("help", all=True, limit=25)
        assert not error and first["next_offset"] == 25
        second, error = client.call("help", all=True, limit=25, offset=25)
        assert not error and second["next_offset"] is None
        tools = first["tools"] + second["tools"]
        assert len({tool["name"] for tool in tools}) == first["total"]
        assert [(tool["category"], tool["name"]) for tool in tools] == sorted(
            (tool["category"], tool["name"]) for tool in tools)
        assert all("inputSchema" not in tool for tool in tools)
        for args in ({"limit": 26}, {"offset": -1}, {"name": "gate", "query": "gate"},
                     {"category": "repository", "all": True}, {"category": "unknown"}):
            assert client.call("help", **args)[1] is True
        result, error = client.call("help", category="repository", query="no_such_operation")
        assert not error and result["tools"] == [] and result["next_offset"] is None
    finally:
        client.close()


def test_catalog_growth_does_not_inflate_advertisement_and_inherits_category_policy(make_project, monkeypatch):
    from vkit import mcp

    project = _project(make_project)
    server = Server(project)
    before = json.dumps(server.definitions())
    for i in range(500):
        tool = Tool(f"extra_{i}", "A catalog extension.", {}, lambda _server, _args: {"answer": 7},
                    category="testing")
        monkeypatch.setitem(mcp.BY_NAME, tool.name, tool)
    assert json.dumps(server.definitions()) == before
    assert server.call_mcp("invoke", {"name": "extra_499", "arguments": {}}) == ({"answer": 7}, False)
    save_settings(open_project(project), Settings(disabled_categories=("testing",)))
    assert server.call_mcp("invoke", {"name": "extra_499", "arguments": {}}) == (
        {"error": "unknown or unavailable tool"}, True)
    assert server.call("help", {"query": "extra_"})[0]["tools"] == []


def test_old_config_is_preserved_and_visibility_changes_are_idempotent(make_project):
    project = _project(make_project)
    path = project / ".git" / "vkit" / "config.json"
    old = {"version": 1, "theme": "dark", "sidebar_collapsed": True,
           "refresh_seconds": 9, "history_limit": 17}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old), encoding="utf-8")
    normalized = {**old, "disabled_categories": [], "mcp_categories": None, "mcp_exposure": "compact"}
    for _ in range(2):
        assert vkit(project, "tools", "--disable", "check_history")[0] == 0
        assert json.loads(path.read_text()) == {**normalized, "disabled_tools": ["check_history"]}
    before = path.read_bytes()
    for name in ("help", "invoke", "missing_tool"):
        assert vkit(project, "tools", "--disable", name)[0] == 2
        assert path.read_bytes() == before
    for _ in range(2):
        assert vkit(project, "tools", "--enable", "check_history")[0] == 0
        assert json.loads(path.read_text()) == {**normalized, "disabled_tools": []}


def test_disabled_protobuf_alias_stays_disabled_and_wrappers_remain_when_all_categories_are_off(make_project):
    from vkit.categories import CATEGORIES

    project = _project(make_project)
    save_settings(open_project(project), Settings(disabled_tools=("check_proto_compatibility",)))
    client = McpClient(project)
    try:
        assert client.call("help", query="Protobuf")[0]["tools"] == []
        save_settings(open_project(project), Settings(disabled_categories=tuple(CATEGORIES)))
        assert [tool["name"] for tool in client.request("tools/list", {})["tools"]] == ["help", "invoke"]
        assert client.call("help") == ({"contract_version": 2, "categories": []}, False)
        assert client.call("gate") == ({"error": "unknown or unavailable tool"}, True)
    finally:
        client.close()


@pytest.mark.parametrize("patch", [
    {"disabled_tools": ["help"]}, {"disabled_tools": ["invoke"]},
    {"disabled_tools": ["gate", "gate"]}, {"disabled_tools": "gate"},
    {"disabled_tools": ["bad-name"]},
    {"disabled_categories": ["unknown"]}, {"disabled_categories": ["safety", "safety"]},
    {"mcp_categories": ["unknown"]}, {"mcp_categories": "safety"}, {"mcp_exposure": "auto"},
])
def test_invalid_visibility_fails_closed_without_rewriting(make_project, patch):
    project = _project(make_project)
    path = project / ".git" / "vkit" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {"version": 1, "theme": "system", "sidebar_collapsed": False,
              "refresh_seconds": 2, "history_limit": 50, **patch}
    path.write_text(json.dumps(config), encoding="utf-8")
    before = path.read_bytes()
    code, body = vkit(project, "mcp", "serve")
    assert code == 2 and "invalid settings" in body["error"]
    assert path.read_bytes() == before

from __future__ import annotations

import json
import hashlib
import os
import shutil

import pytest

from helpers import ROOT, McpClient, vkit


@pytest.mark.skipif(not os.environ.get("VKIT_HISTORY_BIN"), reason="a pinned Porcupine helper is not configured")
def test_transaction_history_through_cli_and_mcp(make_project):
    project = make_project({}, [], accept=False)
    trace = {"schema_version": 1, "operations": [
        {"id": "commit", "client_id": 0, "call": 1, "return": 2,
         "input": {"actions": [{"op": "write", "key": "x", "value": 7},
                                {"op": "read", "key": "x"}]}, "output": [7]}]}
    (project / "history.json").write_text(json.dumps(trace), encoding="utf-8")
    code, body = vkit(project, "compute", "check-history", "--path", "history.json",
                      "--model", "transaction")
    assert code == 0 and body["status"] == "LINEARIZABLE"
    assert body["linearization"] == ["commit"]
    client = McpClient(project)
    try:
        body, error = client.call("check_history", path="history.json", model="transaction")
        assert not error and body["status"] == "LINEARIZABLE"
        assert body["linearization"] == ["commit"]
    finally:
        client.close()


def test_regex_cost_through_cli_and_mcp(make_project, monkeypatch):
    project = make_project({}, [], accept=False)
    helper = ROOT / "tools/regex/helper.cjs"
    lock = helper.parent / "package-lock.json"
    monkeypatch.setenv("VKIT_NODE_BIN", shutil.which("node"))
    monkeypatch.setenv("VKIT_REGEX_HELPER", str(helper))
    monkeypatch.setenv("VKIT_REGEX_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_REGEX_LOCKFILE", str(lock))
    monkeypatch.setenv("VKIT_REGEX_LOCK_SHA256", hashlib.sha256(lock.read_bytes()).hexdigest())
    code, body = vkit(project, "compute", "analyze-regex-cost", "--pattern", "^(a+)+$")
    assert code == 1 and body["status"] == "VULNERABLE_MODEL"
    client = McpClient(project)
    try:
        body, error = client.call("analyze_regex_cost", pattern="^a+$")
        assert not error and body["status"] == "SAFE_MODEL"
        assert body["diagnostics"]["complexity"]["type"] == "linear"
    finally:
        client.close()


def test_synthesis_through_cli_and_mcp(make_project):
    project = make_project({"sample.py": "def f(x: int) -> int:\n    return x + 1\n"}, [], accept=False)
    code, body = vkit(project, "compute", "synthesize-function", "--path", "sample.py", "--function", "f",
                      "--timeout-ms", "30000")
    assert code == 0 and body["status"] == "SYNTHESIZED", body
    assert body["proof"]["status"] == "PROVED"
    client = McpClient(project)
    try:
        body, error = client.call("synthesize_function", path="sample.py", function="f", timeout_ms=30_000)
        assert not error and body["status"] == "SYNTHESIZED"
        assert body["profile"] == "linear-int-v1" and body["proof"]["status"] == "PROVED"
    finally:
        client.close()


def test_configuration_generation_through_cli_and_mcp(make_project):
    project = make_project({}, [], accept=False)
    bundle = {"version": 1, "factors": {"os": ["linux", "windows"], "db": ["sqlite"]}}
    (project / "factors.json").write_text(json.dumps(bundle), encoding="utf-8")
    code, body = vkit(project, "compute", "generate-configurations", "--path", "factors.json",
                      "--timeout-seconds", "60")
    assert code == 0 and body["status"] == "OPTIMAL" and body["count"] == 2, body
    client = McpClient(project)
    try:
        body, error = client.call("generate_configurations", path="factors.json", timeout_seconds=60)
        assert not error and body["status"] == "OPTIMAL"
        assert {tuple(sorted(row.items())) for row in body["configurations"]} == {
            (("db", "sqlite"), ("os", "linux")), (("db", "sqlite"), ("os", "windows"))}
    finally:
        client.close()


def test_grammar_witness_through_cli_and_mcp(make_project):
    project = make_project({}, [], accept=False)
    bundle = {"version": 1, "start": "s", "productions": [
        {"left": "s", "right": [{"literal": "aa"}]},
        {"left": "s", "right": [{"literal": "a"}, {"literal": "a"}]}], "samples": ["aa"]}
    (project / "grammar.json").write_text(json.dumps(bundle), encoding="utf-8")
    code, body = vkit(project, "compute", "find-grammar-ambiguity", "--path", "grammar.json")
    assert code == 1 and body["status"] == "AMBIGUOUS"
    client = McpClient(project)
    try:
        body, error = client.call("find_grammar_ambiguity", path="grammar.json")
        assert not error and body["status"] == "AMBIGUOUS"
        assert body["witness_checked"] is True
    finally:
        client.close()


def test_dependency_resolution_through_cli_and_mcp(make_project):
    project = make_project({}, [], accept=False)
    environment = {
        "implementation_name": "cpython", "implementation_version": "3.12.0",
        "os_name": "posix", "platform_machine": "x86_64", "platform_release": "6.0",
        "platform_system": "Linux", "platform_version": "1",
        "platform_python_implementation": "CPython", "python_full_version": "3.12.0",
        "python_version": "3.12", "sys_platform": "linux",
    }
    bundle = {"version": 1, "roots": ["demo>=1"], "environment": environment,
              "candidates": [{"name": "demo", "version": "1.0", "requires_dist": []}]}
    (project / "deps.json").write_text(json.dumps(bundle), encoding="utf-8")
    code, body = vkit(project, "compute", "resolve-dependencies", "--path", "deps.json")
    assert code == 0 and body["status"] == "RESOLVED"
    client = McpClient(project)
    try:
        body, error = client.call("resolve_dependencies", path="deps.json")
        assert not error and body["status"] == "RESOLVED"
        assert body["resolution"] == [{"name": "demo", "version": "1.0", "requires_python": None}]
    finally:
        client.close()


def test_sqlite_migration_comparison_through_cli_and_mcp(make_project):
    project = make_project({}, [], accept=False)
    bundle = {"version": 1, "old_sql": "CREATE TABLE t(x INTEGER);", "migrations": [],
              "fresh_sql": "CREATE TABLE t(x INTEGER);"}
    (project / "migration.json").write_text(json.dumps(bundle), encoding="utf-8")
    code, body = vkit(project, "compute", "compare-migrations", "--path", "migration.json")
    assert code == 0 and body["status"] == "MATCH"
    client = McpClient(project)
    try:
        body, error = client.call("compare_migrations", path="migration.json")
        assert not error and body["status"] == "MATCH"
        assert body["schema_object_count"] == 1
    finally:
        client.close()


def test_json_containment_through_cli_and_mcp(make_project):
    project = make_project({}, [], accept=False)
    (project / "old.json").write_text('{"type":"integer"}', encoding="utf-8")
    (project / "new.json").write_text('{"type":"number"}', encoding="utf-8")
    code, body = vkit(project, "compute", "compare-contracts", "--old-path", "old.json",
                      "--new-path", "new.json", "--profile", "json-containment")
    assert code == 0 and body["status"] == "COMPATIBLE"
    client = McpClient(project)
    try:
        found, error = client.call("help", name="compare_contracts")
        assert not error
        schema = next(tool for tool in found["tools"] if tool["name"] == "compare_contracts")
        assert "json-containment" in schema["inputSchema"]["properties"]["profile"]["enum"]
        body, error = client.call("compare_contracts", old_path="old.json", new_path="new.json",
                                 profile="json-containment")
        assert not error and body["status"] == "COMPATIBLE"
        assert body["relation"] == "old_subset_new"
    finally:
        client.close()

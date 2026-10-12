from __future__ import annotations

import hashlib
import os
import sys

import pytest

from helpers import McpClient, vkit


def test_c_branch_cli_mcp_and_disabled_discovery(make_project):
    project = make_project({}, [], accept=False)
    source = b"int choose(int value) { if (value == 7) return 42; return 0; }\n"
    (project / "choose.c").write_bytes(source)
    native = sys.platform == "linux" and bool(os.environ.get("VKIT_C_BRANCH_GCC_BIN"))
    expected = "REACHABLE" if native else "UNAVAILABLE"
    arguments = {"path": "choose.c", "function": "choose", "branch_id": "if@1:25:then"}
    code, result = vkit(project, "compute", "inspect-c-branch-reachability", "--path", "choose.c",
                        "--function", "choose", "--branch-id", "if@1:25:then")
    assert (code, result["status"]) == (0 if native else 5, expected)
    assert result["source_sha256"] == hashlib.sha256(source).hexdigest()
    if native:
        assert result["witness"]["inputs"] == {"value": 7}
        assert result["witness"]["observation"] == "VKIT_BRANCH_ID:if@1:25:then"
    client = McpClient(project)
    try:
        before = client.call("gate")
        result, error = client.call("inspect_c_branch_reachability", **arguments)
        assert not error and result["status"] == expected
        assert client.call("gate") == before
        refused, error = client.call("inspect_c_branch_reachability", **arguments, command=["true"])
        assert error and "command" in refused["error"]
    finally:
        client.close()
    assert vkit(project, "tools", "--disable", "inspect_c_branch_reachability")[0] == 0
    fresh = McpClient(project)
    try:
        found, error = fresh.call("help", query="inspect_c_branch_reachability")
        assert not error and found["tools"] == []
        refused, error = fresh.call("inspect_c_branch_reachability", **arguments)
        assert error and refused["error"] == "unknown or unavailable tool"
    finally:
        fresh.close()


def test_llvm_refinement_cli_mcp_and_disabled_discovery(make_project):
    project = make_project({}, [], accept=False)
    before = b"define i8 @f(i8 %x) {\n  %y = add i8 %x, 0\n  ret i8 %y\n}\n"
    after = b"define i8 @f(i8 %x) {\n  ret i8 %x\n}\n"
    (project / "before.ll").write_bytes(before)
    (project / "after.ll").write_bytes(after)
    native = bool(os.environ.get("VKIT_ALIVE2_BIN"))
    expected = "REFINES" if native else "UNAVAILABLE"
    code, result = vkit(project, "compute", "check-llvm-refinement", "--before-path", "before.ll",
                        "--after-path", "after.ll", "--function", "f")
    assert (code, result["status"]) == (0 if native else 5, expected)
    assert result["before_sha256"] == hashlib.sha256(before).hexdigest()
    assert result["after_sha256"] == hashlib.sha256(after).hexdigest()
    client = McpClient(project)
    try:
        result, error = client.call("check_llvm_refinement", before_path="before.ll",
                                    after_path="after.ll", function="f")
        assert not error and result["status"] == expected
        refused, error = client.call("check_llvm_refinement", before_path="before.ll",
                                    after_path="after.ll", function="f", options=["--disable-undef-input"])
        assert error and "options" in refused["error"]
    finally:
        client.close()
    assert vkit(project, "tools", "--disable", "check_llvm_refinement")[0] == 0
    fresh = McpClient(project)
    try:
        found, error = fresh.call("help", query="check_llvm_refinement")
        assert not error and found["tools"] == []
        refused, error = fresh.call("check_llvm_refinement", before_path="before.ll",
                                    after_path="after.ll", function="f")
        assert error and refused["error"] == "unknown or unavailable tool"
    finally:
        fresh.close()


def test_parser_consistency_cli_mcp_and_disabled_discovery(make_project):
    if not os.environ.get("VKIT_OASDIFF_BIN"):
        pytest.skip("pinned oasdiff is needed for native parser disagreement acceptance")
    project = make_project({}, [], accept=False)
    source = b'openapi: "3.0.3"\ninfo: {title: Probe, version: "1", x-vkit-probe: 1e3}\npaths: {}\n'
    (project / "openapi.yaml").write_bytes(source)

    code, result = vkit(project, "compute", "inspect-openapi-parser-consistency", "--path", "openapi.yaml")
    assert (code, result["status"]) == (1, "DISAGREEMENT")
    assert result["entry_source_sha256"] == hashlib.sha256(source).hexdigest()
    assert result["differences"][0]["pointer"] == "/info/x-vkit-probe"
    assert result["differences"][0]["original"] == {"type": "integer", "value": "1000"}
    assert result["differences"][0]["local_json_control"]["preview"] == "1e3"
    assert result["fresh_process_replay_count"] == 1
    client = McpClient(project)
    try:
        result, error = client.call("inspect_openapi_parser_consistency", path="openapi.yaml")
        assert not error and result["status"] == "DISAGREEMENT"
        assert result["fresh_process_replay_count"] == 1
    finally:
        client.close()

    assert vkit(project, "tools", "--disable", "inspect_openapi_parser_consistency")[0] == 0
    fresh = McpClient(project)
    try:
        found, error = fresh.call("help", query="inspect_openapi_parser_consistency")
        assert not error and found["tools"] == []
        refused, error = fresh.call("inspect_openapi_parser_consistency", path="openapi.yaml")
        assert error and refused["error"] == "unknown or unavailable tool"
    finally:
        fresh.close()


def test_release_build_cli_mcp_use_existing_approval(make_project):
    from test_wheel_build import _make_project, _uv

    runtime = _uv()
    if runtime is None:
        pytest.skip("a local uv executable is needed for the approved build profile")
    project = _make_project(make_project, runtime, accept=False)
    code, blocked = vkit(project, "compute", "check-release-artifact", "--check-id", "release-wheel")
    assert (code, blocked["status"]) == (3, "BLOCKED")
    assert blocked["run"]["outcome"]["reason"] == "not_approved"
    assert vkit(project, "accept", "--yes")[0] == 0

    code, result = vkit(project, "compute", "check-release-artifact", "--check-id", "release-wheel")
    assert (code, result["status"]) == (0, "PASS")
    assert result["artifact"]["same_wheel_bytes"] is True
    assert len(result["artifact"]["assertions"]) == 6
    client = McpClient(project)
    try:
        tools = client.request("tools/list", {})["tools"]
        definition = next(tool for tool in tools if tool["name"] == "check_release_artifact")
        assert definition["annotations"]["readOnlyHint"] is False
        result, error = client.call("check_release_artifact", check_id="release-wheel")
        assert not error and result["status"] == "PASS"
        assert result["run"]["outcome"]["result"] == "PASS"
    finally:
        client.close()


def test_rust_safety_cli_mcp_and_enabled_discovery(make_project):
    project = make_project({}, [], accept=False)
    source = b"fn bump(value: u8) -> u8 { value + 1 }\n"
    (project / "bump.rs").write_bytes(source)
    native = sys.platform == "linux" and bool(os.environ.get("VKIT_KANI_BIN"))
    expected = "COUNTEREXAMPLE" if native else "UNAVAILABLE"

    code, result = vkit(project, "compute", "check-rust-safety", "--path", "bump.rs", "--function", "bump")
    assert (code, result["status"]) == (1 if native else 5, expected)
    assert result["source_sha256"] == hashlib.sha256(source).hexdigest()
    if native:
        assert result["counterexample"]["inputs"] == [{"name": "value", "type": "u8", "value": 255}]

    client = McpClient(project)
    try:
        before = client.call("gate")
        result, error = client.call("check_rust_safety", path="bump.rs", function="bump")
        assert not error and result["status"] == expected
        assert client.call("gate") == before
        refused, error = client.call("check_rust_safety", path="bump.rs", function="bump", disable_checks=True)
        assert error and "disable_checks" in refused["error"]
    finally:
        client.close()

    assert vkit(project, "tools", "--disable", "check_rust_safety")[0] == 0
    fresh = McpClient(project)
    try:
        found, error = fresh.call("help", query="check_rust_safety")
        assert not error and found["tools"] == []
        refused, error = fresh.call("check_rust_safety", path="bump.rs", function="bump")
        assert error and refused["error"] == "unknown or unavailable tool"
    finally:
        fresh.close()

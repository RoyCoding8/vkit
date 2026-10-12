from __future__ import annotations

import hashlib
import os
import sys

import pytest

from helpers import McpClient, vkit


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

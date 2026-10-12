from __future__ import annotations

import hashlib
import os
import sys

from helpers import McpClient, vkit


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

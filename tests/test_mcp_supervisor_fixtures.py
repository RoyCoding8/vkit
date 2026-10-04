"""Public MCP evidence records the fixture identity a supervisor actually ran against."""
from __future__ import annotations
import subproc

import json
import shutil
import subprocess
import time
from pathlib import Path

from vkit.manifest import parse_manifest
from vkit.mcp import Server
from vkit.paths import open_project
from vkit.storage import Store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "python-cli"
CHECK_ID = "totals-behavior"


def _repo(path: Path):
    shutil.copytree(EXAMPLE, path)
    subproc.run(["git", "init", "-q"], cwd=path, check=True)
    subproc.run(["git", "add", "-A"], cwd=path, check=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=vkit", "commit", "-qm", "example"],
        cwd=path, check=True,
    )
    manifest_path = path / "verification" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checks"][0]["inputs"] = [".env"]
    manifest["checks"][0]["expectations"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (path / ".env").write_text("FIXTURE=first\n", encoding="utf-8")
    subproc.run(["git", "add", "-A"], cwd=path, check=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=vkit", "commit", "-qm", "fixtures"],
        cwd=path, check=True,
    )
    return open_project(path)


def _begin(server: Server) -> str:
    result = server.call_tool(
        "task_begin",
        {"request_id": "fixture-task", "contract": {"description": "fixture identity"}},
    )
    assert not result.is_error and result.content["admitted"], result.content
    return result.content["task_id"]


def _run(server: Server, task_id: str, request_id: str) -> dict:
    result = server.call_tool(
        "check_start",
        {"task_id": task_id, "check_ids": [CHECK_ID], "request_id": request_id},
    )
    assert not result.is_error, result.content
    run_id = result.content["runs"][0]["run_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        polled = server.call_tool("run_get", {"run_id": run_id})
        assert not polled.is_error, polled.content
        if polled.content["lifecycle"] == "terminal":
            assert polled.content["outcome"], polled.content
            return polled.content
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not become terminal")


def test_public_mcp_fixture_digest_blocks_changes_until_a_fresh_run(tmp_path: Path) -> None:
    project = _repo(tmp_path / "repo")
    server = Server(project.root)
    task_id = _begin(server)
    manifest = parse_manifest(project, project.runs_root)
    assert manifest.require(CHECK_ID).expectations == ()

    first = _run(server, task_id, "fixture-first")
    first_digest = manifest.fixture_identity().digest
    assert first["result"] == "PASS", first
    first_report = Store(project.db_path).load(first["run_id"])
    assert first_report["configuration_digest"] == manifest.digest()
    assert first_report["fixture_digest"] == first_digest
    first_ready = server.call_tool("task_finalize", {"task_id": task_id})
    assert first_ready.content["readiness"] == "READY", first_ready.content

    (project.root / ".env").write_text("FIXTURE=changed\n", encoding="utf-8")
    changed = server.call_tool("task_finalize", {"task_id": task_id})
    assert changed.content["readiness"] == "BLOCKED", changed.content
    assert any("different fixtures" in gap for gap in changed.content["gaps"])

    fresh = _run(server, task_id, "fixture-fresh")
    refreshed = parse_manifest(project, project.runs_root)
    fresh_digest = refreshed.fixture_identity().digest
    assert fresh["result"] == "PASS", fresh
    assert fresh_digest != first_digest
    fresh_report = Store(project.db_path).load(fresh["run_id"])
    assert fresh_report["configuration_digest"] == refreshed.digest()
    assert fresh_report["fixture_digest"] == fresh_digest

    ready = server.call_tool("task_finalize", {"task_id": task_id})
    assert ready.content["readiness"] == "READY", ready.content

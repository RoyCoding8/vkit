from __future__ import annotations
import subproc

import importlib.util
import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from vkit import tasks
from vkit.claims import ResourceSpec, acquire, holders, release
from vkit.execution import run_check
from vkit.identity import compute_source_identity
from vkit.manifest import parse_manifest
from vkit.mcp._tools import Server
from vkit.paths import open_project
from vkit.storage import Store, StoreError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "python-cli"
CHECK_ID = "totals-behavior"


@pytest.fixture
def project(tmp_path: Path):
    root = tmp_path / "repo"
    shutil.copytree(EXAMPLE, root)
    subproc.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subproc.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subproc.run(["git", "config", "user.name", "vkit test"], cwd=root, check=True)
    subproc.run(["git", "add", "-A"], cwd=root, check=True)
    subproc.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    return open_project(root)


def _context(project):
    return tasks.acceptance_context(
        project, lambda: parse_manifest(project, project.runs_root)
    )


def _begin(project, request_id: str, **extra) -> str:
    result = Server(project.root).call_tool(
        "task_begin",
        {"request_id": request_id, "contract": {"description": "core regression"}, **extra},
    )
    assert not result.is_error and result.content["admitted"], result.content
    return result.content["task_id"]


def _run(project, task_id: str) -> str:
    store = Store(project.db_path)
    manifest = parse_manifest(project, project.runs_root)
    outcome = run_check(
        manifest, CHECK_ID, store=store,
        source=compute_source_identity(project), task_id=task_id, attempt=1,
    )
    assert outcome.report["outcome"]["result"] == "PASS", outcome.report
    assert outcome.report["fixture_digest"] is not None
    return outcome.report["fixture_digest"]


def _hook():
    path = ROOT / "plugin" / "scripts" / "vkit_hook.py"
    spec = importlib.util.spec_from_file_location("vkit_core_acceptance_hook", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block_release_during_decision(monkeypatch, store, task_id: str) -> list[bool]:
    connect = Store._connect

    @contextmanager
    def no_wait_connection(self):
        with connect(self) as conn:
            conn.execute("PRAGMA busy_timeout = 0")
            yield conn

    monkeypatch.setattr(Store, "_connect", no_wait_connection)
    decide = tasks._decide
    blocked = []

    def release_during_decision(*args, **kwargs):
        try:
            release(store, task_id, 1)
        except StoreError as exc:
            assert "locked" in str(exc), exc
            blocked.append(True)
        return decide(*args, **kwargs)

    monkeypatch.setattr(tasks, "_decide", release_during_decision)
    return blocked


def test_released_claim_blocks_finalize_and_reacquisition_repairs_it(project) -> None:
    task_id = _begin(project, "released", claim_resource="checkout")
    _run(project, task_id)
    store = Store(project.db_path)
    release(store, task_id, 1)

    result = Server(project.root).call_tool("task_finalize", {"task_id": task_id})
    assert result.content["readiness"] == "BLOCKED"
    assert any("checkout" in gap for gap in result.content["gaps"])

    acquire(store, task_id, 1, [ResourceSpec("checkout", "exclusive")])
    repaired = Server(project.root).call_tool("task_finalize", {"task_id": task_id})
    assert repaired.content["readiness"] == "READY", repaired.content


def test_finalize_holds_claim_through_decision_and_publication(project, monkeypatch) -> None:
    task_id = _begin(project, "release-race", claim_resource="checkout")
    _run(project, task_id)
    store = Store(project.db_path)
    blocked = _block_release_during_decision(monkeypatch, store, task_id)
    result = Server(project.root).call_tool("task_finalize", {"task_id": task_id})

    assert result.content["readiness"] == "READY", result.content
    assert blocked, "claim release was not blocked during the acceptance transaction"
    assert len(holders(store, task_id)) == 1
    release(store, task_id, 1)


def test_completion_hook_reads_readiness_under_the_same_claim_snapshot(project, monkeypatch) -> None:
    task_id = _begin(
        project, "hook-release-race", claim_resource="checkout",
        host={"session_id": "hook-race-session"},
    )
    _run(project, task_id)
    store = Store(project.db_path)
    blocked = _block_release_during_decision(monkeypatch, store, task_id)
    event = {"session_id": "hook-race-session", "hook_event_name": "Stop"}
    response = _hook().respond("Stop", event, str(project.root))[0]

    assert response.get("decision") != "block", response
    assert blocked, "claim release was not blocked during the hook decision"
    assert len(holders(store, task_id)) == 1
    release(store, task_id, 1)


def test_linked_checkout_cannot_finalize_another_checkouts_task(project, tmp_path: Path) -> None:
    second_root = tmp_path / "linked"
    subproc.run(
        ["git", "worktree", "add", "-q", str(second_root), "HEAD"],
        cwd=project.root, check=True,
    )
    second = open_project(second_root)
    assert project.db_path == second.db_path
    task_id = _begin(project, "checkout-binding")
    _run(project, task_id)

    foreign = Server(second.root).call_tool("task_finalize", {"task_id": task_id})
    assert foreign.content["readiness"] == "BLOCKED"
    assert any("pinned to checkout" in gap for gap in foreign.content["gaps"])
    local = Server(project.root).call_tool("task_finalize", {"task_id": task_id})
    assert local.content["readiness"] == "READY", local.content


def test_changed_declared_fixture_blocks_until_a_fresh_run(project) -> None:
    manifest_path = project.manifest_path
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["checks"][0]["inputs"] = [".env"]
    manifest_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    (project.root / ".env").write_text("TOKEN=old\n", encoding="utf-8")
    subproc.run(["git", "add", "-A"], cwd=project.root, check=True)
    subproc.run(["git", "commit", "-qm", "declared fixture"], cwd=project.root, check=True)
    project = open_project(project.root)

    task_id = _begin(project, "fixture-change")
    recorded = _run(project, task_id)
    (project.root / ".env").write_text("TOKEN=new\n", encoding="utf-8")
    changed = Server(project.root).call_tool("task_finalize", {"task_id": task_id})
    assert changed.content["readiness"] == "BLOCKED"
    assert any("different fixtures" in gap for gap in changed.content["gaps"])

    fresh = _run(project, task_id)
    assert fresh != recorded
    repaired = Server(project.root).call_tool("task_finalize", {"task_id": task_id})
    assert repaired.content["readiness"] == "READY", repaired.content


def test_public_task_begin_binds_main_and_subagent_completion_hooks(project) -> None:
    hook = _hook()
    session_start = hook.respond(
        "SessionStart", {"session_id": "session-main", "hook_event_name": "SessionStart"},
        str(project.root),
    )[0]
    session_context = session_start["hookSpecificOutput"]["additionalContext"]
    assert 'host={"session_id": "session-main"}' in session_context
    main_id = _begin(project, "host-main", host={"session_id": "session-main"})
    contract = tasks.get_task(Store(project.db_path), main_id).pinned()
    assert contract.declared["host"] == {"session_id": "session-main"}

    event = {"session_id": "session-main", "hook_event_name": "Stop", "cwd": str(project.root)}
    blocked = hook.respond("Stop", event, str(project.root))[0]
    assert blocked.get("decision") == "block", blocked
    _run(project, main_id)
    assert hook.respond("Stop", event, str(project.root))[0].get("decision") != "block"

    child_id = _begin(
        project, "host-child",
        host={"session_id": "session-main", "agent_id": "agent-7"},
    )
    child_event = {
        "session_id": "session-main", "agent_id": "agent-7",
        "hook_event_name": "SubagentStop", "cwd": str(project.root),
    }
    child_start = hook.respond(
        "SubagentStart", {**child_event, "hook_event_name": "SubagentStart"},
        str(project.root),
    )[0]
    child_context = child_start["hookSpecificOutput"]["additionalContext"]
    assert 'host={"session_id": "session-main", "agent_id": "agent-7"}' in child_context
    blocked_child = hook.respond("SubagentStop", child_event, str(project.root))[0]
    assert blocked_child.get("decision") == "block", blocked_child
    _run(project, child_id)
    assert hook.respond("SubagentStop", child_event, str(project.root))[0].get("decision") != "block"

    unrelated = hook.respond(
        "Stop", {**event, "session_id": "unregistered-session"}, str(project.root)
    )[0]
    assert unrelated.get("decision") != "block", unrelated
    assert "No single managed task" in unrelated.get("systemMessage", "")

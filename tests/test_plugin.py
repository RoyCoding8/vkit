from __future__ import annotations

import json
import importlib.util
import subprocess
import sys

import pytest

from helpers import DRIVER, NO_WINDOW, ROOT, scenario_check, vkit
from test_formal_policy import component
from vkit import query, state
from vkit.formal import ledger
from vkit.inputs import canonical_digest

HOOK = ROOT / "plugin" / "scripts" / "vkit_hook.py"
PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def hook(project, event: str, payload: dict) -> dict:
    done = subprocess.run([sys.executable, str(HOOK), event, "--project", str(project)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, **NO_WINDOW)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_stop_is_held_while_a_check_is_stale_and_released_when_ready(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    held = hook(project, "Stop", {})
    assert held["decision"] == "block" and "missing: ok" in held["reason"]
    assert "decision" not in hook(project, "Stop", {"stop_hook_active": True})
    vkit(project, "check", "run", "--check", "ok")
    assert hook(project, "Stop", {}) == {}
    context = hook(project, "SessionStart", {})["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("vkit gate: READY")


def test_stop_is_held_until_unaccepted_definitions_are_authorized(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])],
                           accept=False)
    response = hook(project, "Stop", {})
    assert response["decision"] == "block" and "not_approved: ok" in response["reason"]
    assert "decision" not in hook(project, "Stop", {"stop_hook_active": True})


def test_stop_hook_fails_closed_when_the_gate_cannot_be_read(make_project, monkeypatch):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])],
                           accept=False)
    spec = importlib.util.spec_from_file_location("vkit_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from vkit import query

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(query, "status", broken)
    response = module.handle("Stop", {}, str(project))
    assert response["decision"] == "block" and "boom" in response["reason"]


def test_a_forced_bypass_leaves_a_telemetry_trace(make_project):
    from vkit.paths import open_project

    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    hook(project, "Stop", {"stop_hook_active": True})
    log = open_project(project).git_common_dir / "vkit" / "telemetry.jsonl"
    assert "stop_hook_bypass" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("required,accepted,held", [(True, True, True), (False, True, False), (True, False, True)])
def test_formal_stop_observes_required_policy_and_acceptance(make_project, monkeypatch, required, accepted, held):
    root = make_project({"src/allocation.rs": "pub fn reserve() {}"}, [], accept=False)
    raw = component()
    raw["required"] = required
    raw["package"]["sha256"] = ledger.load_package().identity.sha256
    (root / "verification/manifest.json").write_text(json.dumps({
        "schema_version": 3, "checks": [], "formal": {"components": [raw]}}), encoding="utf-8")
    identity = {"semantic": {"checker": "test"}, "paths": {}}
    monkeypatch.setattr(state, "_resolve_execution", lambda _: identity)
    ctx = query.open_context(root)
    policy = ctx.require_manifest().require_component("allocation")
    if accepted:
        ctx.store.approve({policy.digest: {"kind": "formal_policy", "policy_sha256": policy.digest,
            "package_sha256": policy.package.sha256, "checker_sha256": canonical_digest(identity["semantic"]),
            "accepted_at": "now"}})
    spec = importlib.util.spec_from_file_location("vkit_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    response = module.handle("Stop", {}, str(root))

    assert (response.get("decision") == "block") is held
    if held:
        assert "allocation" in response["reason"]
        assert "decision" not in module.handle("Stop", {"stop_hook_active": True}, str(root))
    else:
        assert "decision" not in response


def test_session_start_records_a_base_commit_and_stop_scopes_to_the_changed_paths(make_project):
    from vkit.paths import open_project

    project = make_project({"ok.py": PASSES, "README.md": "run lookup\n"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    hook(project, "SessionStart", {})
    assert (open_project(project).git_common_dir / "vkit" / "session_base.txt").is_file()
    (project / "README.md").write_text("changed\n", encoding="utf-8")
    response = hook(project, "Stop", {})
    assert "decision" not in response
    assert "READY_SCOPED" in response["systemMessage"] and "README.md" in response["systemMessage"]


def test_stop_without_a_session_base_falls_back_to_the_full_gate_and_notes_it(make_project):
    from vkit.paths import open_project

    project = make_project({"ok.py": PASSES, "README.md": "run lookup\n"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    assert hook(project, "Stop", {}) == {}
    log = open_project(project).git_common_dir / "vkit" / "telemetry.jsonl"
    assert "session_base_missing" in log.read_text(encoding="utf-8")

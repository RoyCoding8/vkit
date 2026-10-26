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


def test_stop_is_not_held_for_evidence_the_agent_cannot_produce(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])],
                           accept=False)
    response = hook(project, "Stop", {})
    assert "decision" not in response and "not_approved: ok" in response["systemMessage"]


@pytest.mark.parametrize("required,accepted,held", [(True, True, True), (False, True, False), (True, False, False)])
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
        assert "missing: allocation" in response["reason"]
        assert "verify" in response["reason"] and "allocation" in response["reason"]
        assert "decision" not in module.handle("Stop", {"stop_hook_active": True}, str(root))
    elif not accepted:
        assert "not_approved: allocation" in response["systemMessage"]

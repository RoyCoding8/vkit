from __future__ import annotations

import json
import subprocess
import sys

from helpers import DRIVER, NO_WINDOW, ROOT, scenario_check, vkit

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

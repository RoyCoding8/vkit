from __future__ import annotations

from helpers import DRIVER, scenario_check, vkit
from vkit import state
from vkit.evidence import backend_identity, policy_identity
from vkit.inputs import Snapshot

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def _project(path):
    from vkit.paths import open_project

    return open_project(path)


def _manifest(project):
    from vkit.manifest import parse_manifest

    return parse_manifest(_project(project))


def _store(project):
    from vkit.store import Store

    return Store.of(_project(project))


def test_producer_identity_tracks_the_gate_not_the_console():
    keys = policy_identity()
    assert "state.py" in keys and "query.py" in keys
    assert "cli.py" not in keys and "console.py" not in keys


def test_vkit_change_stales_evidence_but_keeps_the_approval(make_project, monkeypatch):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    manifest = _manifest(project)
    store = _store(project)
    before = state.check_states(manifest, store, Snapshot(_project(project)))[0]
    assert before.freshness is state.Freshness.MISSING
    real = backend_identity

    def changed(check):
        return {**real(check), "implementation": {"changed.py": "f" * 64}}

    monkeypatch.setattr("vkit.evidence.backend_identity", changed)
    after = state.check_states(manifest, store, Snapshot(_project(project)))[0]
    assert after.freshness is state.Freshness.MISSING
    assert after.freshness is not state.Freshness.NOT_APPROVED
    assert after.key != before.key


def test_definition_change_still_requires_reaccept(make_project, monkeypatch):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    manifest, store = _manifest(project), _store(project)
    entry = dict(manifest.entries["ok"])
    entry["timeout_seconds"] = 11
    (project / "verification" / "manifest.json").write_text(
        __import__("json").dumps({"schema_version": 3, "description": "test project", "checks": [entry]}),
        encoding="utf-8")
    manifest2 = _manifest(project)
    fresh = state.check_states(manifest2, store, Snapshot(_project(project)))[0]
    assert fresh.freshness is state.Freshness.NOT_APPROVED


def test_removing_a_failing_check_cannot_reach_ready(make_project):
    import json

    from vkit import query

    project = make_project({"ok.py": PASSES, "bad.py": DRIVER + 'report([("bad", False, "nope")])\n'},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                            scenario_check("bad", "bad.py", scenarios=["bad"], inputs=["bad.py"])])
    vkit(project, "check", "run", "--check", "ok", "--check", "bad")
    (project / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 3, "description": "test project",
        "checks": [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])]}), encoding="utf-8")
    out = query.status(query.open_context(project))
    assert out["gate"]["verdict"] != "READY"
    assert any(c["state"] == "obligation_removed" and c["id"] == "bad" for c in out["checks"])


def test_renaming_a_check_is_removal_plus_addition(make_project):
    import json

    from vkit import query

    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    (project / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 3, "description": "test project",
        "checks": [scenario_check("ok2", "ok.py", scenarios=["ok"], inputs=["ok.py"])]}), encoding="utf-8")
    out = query.status(query.open_context(project))
    assert out["gate"]["verdict"] != "READY"
    states = {(c["id"], c["state"]) for c in out["checks"]}
    assert ("ok", "obligation_removed") in states and ("ok2", "not_approved") in states


def test_accepting_a_shrunk_manifest_rebinds_the_inventory(make_project):
    import json

    from vkit import query

    project = make_project({"ok.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                            scenario_check("gone", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok", "--check", "gone")
    (project / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 3, "description": "test project",
        "checks": [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])]}), encoding="utf-8")
    assert query.status(query.open_context(project))["gate"]["verdict"] != "READY"
    code, _ = vkit(project, "accept", "--yes")
    assert code == 0
    assert query.status(query.open_context(project))["gate"]["verdict"] == "READY"


def test_unbinding_a_required_formal_component_needs_authority():
    from types import SimpleNamespace

    formal = [SimpleNamespace(component=SimpleNamespace(id="alloc", digest="d"),
                              freshness=state.Freshness.FRESH_PASS, required=False, current=None)]
    out = state.gate([], formal, obligations={"checks": {}, "formal_required": {"alloc": "d"}})
    assert out["verdict"] != "READY" and "alloc" in out["reason"]

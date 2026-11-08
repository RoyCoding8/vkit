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

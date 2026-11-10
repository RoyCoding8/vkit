from __future__ import annotations

from helpers import DRIVER, NO_WINDOW, scenario_check, vkit
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


def _shrink(project, kept):
    import json

    (project / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 3, "description": "test project", "checks": kept}), encoding="utf-8")


def test_a_partial_accept_cannot_rebind_a_removed_obligation(make_project):
    from vkit import query

    project = make_project({"ok.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                            scenario_check("gone", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok", "--check", "gone")
    _shrink(project, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    assert vkit(project, "accept", "--check", "ok", "--yes")[0] == 0
    out = query.status(query.open_context(project))
    assert out["gate"]["verdict"] != "READY"
    assert any(c["id"] == "gone" and c["state"] == "obligation_removed" for c in out["checks"])


def test_dropping_a_fresh_fail_obligation_stays_rejected_until_an_acceptance_rebinds_the_slot(make_project):
    from vkit import query

    project = make_project({"ok.py": PASSES, "bad.py": DRIVER + 'report([("bad", False, "nope")])\n'},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                            scenario_check("bad", "bad.py", scenarios=["bad"], inputs=["bad.py"])])
    vkit(project, "check", "run", "--check", "ok", "--check", "bad")
    _shrink(project, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    assert vkit(project, "accept", "--drop", "bad", "--yes")[0] == 0
    assert query.status(query.open_context(project))["gate"]["verdict"] == "REJECTED"
    assert vkit(project, "accept", "--yes")[0] == 0
    assert query.status(query.open_context(project))["gate"]["verdict"] == "REJECTED"
    import json

    (project / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 3, "description": "test project",
        "checks": [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                   scenario_check("bad", "ok.py", scenarios=["ok"], inputs=["ok.py"])]}), encoding="utf-8")
    assert vkit(project, "accept", "--check", "bad", "--yes")[0] == 0
    assert vkit(project, "check", "run", "--check", "bad")[0] == 0
    assert query.status(query.open_context(project))["gate"]["verdict"] == "READY"


def test_dropping_a_passing_obligation_clears_the_gate(make_project):
    from vkit import query

    project = make_project({"ok.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                            scenario_check("gone", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok", "--check", "gone")
    _shrink(project, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    assert vkit(project, "accept", "--drop", "gone", "--yes")[0] == 0
    assert query.status(query.open_context(project))["gate"]["verdict"] == "READY"


def test_rewriting_an_evaluator_script_invalidates_the_acceptance(make_project):
    from helpers import DRIVER
    from vkit import query

    driver = DRIVER + 'import pathlib\nreport([("ok", pathlib.Path("subject.py").read_text().strip() != "", "read")])\n'
    check = scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py", "subject.py"])
    check["subject"] = {"paths": ["subject.py"], "digest": None}
    project = make_project({"driver.py": driver, "subject.py": "value = 1\n"}, [check])
    assert query.status(query.open_context(project))["checks"][0]["state"] != "not_approved"
    (project / "driver.py").write_text(driver.replace('"read"', '"read v2"'), encoding="utf-8")
    assert query.status(query.open_context(project))["checks"][0]["state"] == "not_approved"


def test_unbinding_a_required_formal_component_needs_authority():
    from types import SimpleNamespace

    formal = [SimpleNamespace(component=SimpleNamespace(id="alloc", digest="d"),
                              freshness=state.Freshness.FRESH_PASS, required=False, current=None)]
    out = state.gate([], formal, obligations={"checks": {}, "formal_required": {"alloc": "d"}})
    assert out["verdict"] != "READY" and "alloc" in out["reason"]


def test_unaccepted_definitions_are_rejected_not_merely_blocked(make_project):
    from vkit import query

    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])],
                           accept=False)
    out = query.status(query.open_context(project))
    assert out["gate"]["verdict"] == "REJECTED"


def test_uncovered_requested_paths_scope_the_ready_verdict(make_project):
    from vkit import query

    project = make_project({"ok.py": PASSES, "README.md": "hi"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    out = query.status(query.open_context(project), paths=["README.md"])
    assert out["gate"]["verdict"] == "READY_SCOPED" and "README.md" in out["gate"]["reason"]


def test_every_doubt_has_a_clearing_action():
    for doubt in state.DOUBTS:
        assert doubt.effect in ("reject", "block", "scope") and doubt.clearing and doubt.falsifier
    covered = {d.field for d in state.DOUBTS}
    assert {f.value for f in state.Freshness} - {"fresh_pass"} <= covered
    assert {"obligation_removed", "uncovered_change"} <= covered


def test_an_added_unaccepted_check_is_rejected(make_project):
    import json

    from vkit import query

    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    (project / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 3, "description": "test project",
        "checks": [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                   scenario_check("extra", "ok.py", scenarios=["ok"], inputs=["ok.py"])]}), encoding="utf-8")
    out = query.status(query.open_context(project))
    assert out["gate"]["verdict"] == "REJECTED"


def test_a_failing_check_rejects_the_gate(make_project):
    from vkit import query

    project = make_project({"ok.py": DRIVER + 'report([("ok", False, "nope")])\n'},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    out = query.status(query.open_context(project))
    assert out["gate"]["verdict"] == "REJECTED" and "fresh_fail:" in out["gate"]["reason"]
    assert out["checks"][0]["state"] == "fresh_fail"


def test_fixing_the_subject_clears_a_fresh_fail(make_project):
    from vkit import query

    driver = DRIVER + 'import pathlib\nreport([("ok", pathlib.Path("subject.py").read_text().strip() != "", "read")])\n'
    check = scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py", "subject.py"])
    check["subject"] = {"paths": ["subject.py"], "digest": None}
    project = make_project({"driver.py": driver, "subject.py": ""}, [check])
    vkit(project, "check", "run", "--check", "ok")
    assert query.status(query.open_context(project))["gate"]["verdict"] == "REJECTED"
    (project / "subject.py").write_text("value = 1\n", encoding="utf-8")
    vkit(project, "check", "run", "--check", "ok")
    assert query.status(query.open_context(project))["gate"]["verdict"] == "READY"


def test_a_blocked_run_blocks_the_gate():
    from types import SimpleNamespace

    states = [SimpleNamespace(check=SimpleNamespace(id="c"), freshness=state.Freshness.FRESH_BLOCKED, current=None)]
    out = state.gate(states, obligations={"checks": {"c": "d"}, "formal_required": {}})
    assert out["verdict"] == "BLOCKED" and out["reason"].startswith("fresh_blocked:")


def test_a_cleared_run_reaches_ready():
    from types import SimpleNamespace

    states = [SimpleNamespace(check=SimpleNamespace(id="c"), freshness=state.Freshness.FRESH_PASS, current=None)]
    out = state.gate(states, obligations={"checks": {"c": "d"}, "formal_required": {}})
    assert out["verdict"] == "READY"


def test_the_gate_never_ghosts_an_id_it_already_reports_as_a_state():
    from types import SimpleNamespace

    states = [SimpleNamespace(check=SimpleNamespace(id="bad"), freshness=state.Freshness.FRESH_BLOCKED, current=None)]
    out = state.gate(states, obligations={"checks": {"bad": "d"}, "formal_required": {}, "dropped": {"bad": "r1"}})
    assert [c["id"] for c in out["checks"]].count("bad") == 1


def test_a_blocked_formal_component_blocks_the_gate():
    from types import SimpleNamespace

    formal = [SimpleNamespace(component=SimpleNamespace(id="f", digest="d"),
                              freshness=state.Freshness.BLOCKED, required=True, current=None)]
    out = state.gate([], formal, obligations={"checks": {}, "formal_required": {"f": "d"}})
    assert out["verdict"] == "BLOCKED" and out["reason"].startswith("blocked:")


def test_a_cleared_formal_component_reaches_ready():
    from types import SimpleNamespace

    formal = [SimpleNamespace(component=SimpleNamespace(id="f", digest="d"),
                              freshness=state.Freshness.FRESH_PASS, required=True, current=None)]
    out = state.gate([], formal, obligations={"checks": {}, "formal_required": {"f": "d"}})
    assert out["verdict"] == "READY"


def test_a_formal_only_manifest_without_an_inventory_is_rejected():
    from types import SimpleNamespace

    formal = [SimpleNamespace(component=SimpleNamespace(id="alloc", digest="d"),
                              freshness=state.Freshness.FRESH_PASS, required=True, current=None)]
    out = state.gate([], formal)
    assert out["verdict"] == "REJECTED" and "obligation_inventory:" in out["reason"]


def test_registry_falsifiers_name_real_tests():
    import inspect
    import re
    import sys

    import test_cli_authority
    import test_evidence_identity
    import test_weakening

    modules = (sys.modules[__name__], test_cli_authority, test_evidence_identity, test_weakening)
    registry = {"test_registry_falsifiers_name_real_tests", "test_every_doubt_has_a_clearing_action"}
    bundles = []
    for doubt in state.DOUBTS:
        mutant, honest = doubt.falsifier
        assert mutant != honest and not {mutant, honest} & registry, doubt.field
        for name in doubt.falsifier:
            assert any(callable(getattr(m, name, None)) for m in modules), doubt.field
        bundles.append(doubt.falsifier)
    assert len(set(bundles)) == len(bundles)
    tokens = set(re.findall(r'"([a-z_]+): ', inspect.getsource(state.gate)))
    fields = [doubt.field for doubt in state.DOUBTS]
    assert len(set(fields)) == len(fields) and tokens == set(fields)


def test_the_gate_names_a_clearing_action_ordered_by_effect():
    from types import SimpleNamespace

    states = [SimpleNamespace(check=SimpleNamespace(id="a"), freshness=state.Freshness.STALE, current=None),
              SimpleNamespace(check=SimpleNamespace(id="b"), freshness=state.Freshness.FRESH_FAIL, current=None)]
    out = state.gate(states, obligations={"checks": {}, "formal_required": {}})
    assert out["verdict"] == "REJECTED" and out["clearing"]
    fields = [d["field"] for d in out["doubts"]]
    assert "fresh_fail" in fields and "stale" in fields
    effect = {d.field: d.effect for d in state.DOUBTS}
    assert fields == sorted(fields, key=lambda f: ("reject", "block", "scope").index(effect[f]))


def test_registered_bundles_actually_flip():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    nodes = {}
    for path in (root / "tests").glob("test_*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("def test_"):
                name = line[4:].split("(")[0]
                nodes[name] = f"tests/{path.name}::{name}"
    env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "PYTHONPATH": str(root / "tests"), "VKIT_FLIP_MUTANT": "1",
           "VKIT_ALLOW_YES": "1", "HOME": os.environ.get("HOME", ""), "TMPDIR": os.environ.get("TMPDIR", "")}
    for doubt in state.DOUBTS:
        mutant, honest = doubt.falsifier
        assert mutant in nodes and honest in nodes, doubt.field
        good = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", nodes[honest]],
                              cwd=root, env=env, capture_output=True, text=True, timeout=900, **NO_WINDOW)
        if good.returncode != 0:
            print("HONEST RUN FAILED", doubt.field, honest)
            print("good stdout:", good.stdout)
            print("good stderr:", good.stderr)
        assert good.returncode == 0, (doubt.field, honest, good.stdout[-2000:], good.stderr[-500:])
        bad = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "flip_plugin", nodes[mutant]],
                             cwd=root, env=env, capture_output=True, text=True, timeout=900, **NO_WINDOW)
        assert bad.returncode != 0, (doubt.field, mutant, bad.stdout[-2000:], bad.stderr[-500:])


def test_an_unpinnable_evaluator_refuses_acceptance(make_project):
    project = make_project({"ok.py": PASSES},
                           [scenario_check("ok", "absent.py", scenarios=["ok"], inputs=["ok.py"])], accept=False)
    code, body = vkit(project, "accept", "--yes")
    assert code == 2 and "absent.py" in body["error"] and "not pinned" in body["error"]


def test_an_evaluator_outside_the_check_cwd_refuses_acceptance(make_project):
    check = scenario_check("ok", "../driver.py", scenarios=["ok"], inputs=["ok.py"])
    check["cwd"] = "verification"
    project = make_project({"ok.py": PASSES, "driver.py": DRIVER + 'report([("ok", True, "x")])\n'}, [check],
                           accept=False)
    code, body = vkit(project, "accept", "--yes")
    assert code == 2 and "../driver.py" in body["error"]


def test_an_empty_oracle_never_matches_a_local_check(make_project):
    project = make_project({"ok.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])], accept=False)
    manifest = _manifest(project)
    approved = {manifest.digest("ok"): {"kind": "check_definition", "check_id": "ok", "backend": {},
                                        "accepted_at": "2024-01-01T00:00:00.000+00:00", "oracle": {"files": {}}}}
    assert not state.oracle_matches(approved, manifest.require("ok"), manifest.digest("ok"))


def test_the_gate_records_a_reason_token_for_a_non_rejecting_sentinel(make_project):
    from vkit import query

    vacuous = make_project({"driver.py": DRIVER + 'report([("ok", True, "always")])\n', "subject.py": "v\n"},
                           [dict(scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py", "subject.py"]),
                                 subject={"paths": ["subject.py"], "digest": None})])
    out = query.status(query.open_context(vacuous))
    entry = next(c for c in out["gate"]["checks"] if c["id"] == "ok")
    assert entry["sentinel"] == "vacuous"
    genuine = make_project({"driver.py": DRIVER + 'import pathlib\nreport([("ok", pathlib.Path("subject.py").read_text().strip() != "", "read")])\n',
                            "subject.py": "v\n"},
                           [dict(scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py", "subject.py"]),
                                 subject={"paths": ["subject.py"], "digest": None})])
    out = query.status(query.open_context(genuine))
    assert all("sentinel" not in c for c in out["gate"]["checks"])

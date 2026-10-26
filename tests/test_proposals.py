from __future__ import annotations

import json

import pytest

from helpers import DRIVER, McpClient, scenario_check, vkit
from vkit.formal.policy import FormalComponent
from vkit.inputs import canonical_digest
from vkit.paths import Project
from vkit.proposals import ProposalError, apply, submit
from vkit.store import Store

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'
NO_PRINT = '''
import json, pathlib, sys
results = [{"ruleId": "no-print", "message": {"text": "print() in library code"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": p.as_posix()}}}]}
           for p in sorted(pathlib.Path("lib").rglob("*.py")) if "print(" in p.read_text(encoding="utf-8")]
pathlib.Path(sys.argv[1]).write_text(json.dumps({"version": "2.1.0", "runs": [{"results": results}]}))
'''


def test_an_agent_proposes_a_rule_and_only_acceptance_makes_it_run(make_project):
    project = make_project({"ok.py": PASSES, "lib/core.py": "def f():\n    print('debug')\n"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    rule = {"id": "no-print", "kind": "static", "timeout_seconds": 60, "artifact": "report.sarif",
            "command": ["{{python}}", "verification/no_print.py", "{{run_dir}}/report.sarif"],
            "inputs": ["lib", "verification/no_print.py"], "subject": {"paths": ["lib"], "digest": None},
            "claim_id": "no-print"}
    feature = {"id": "core", "behavior": "f computes quietly", "how_to_reach": ["import lib.core"],
               "entry_points": ["lib/core.py"], "covered_by": ["no-print"], "gaps": []}
    client = McpClient(project)
    try:
        body, error = client.call("propose", checks=[rule], features=[feature],
                                  files={"verification/no_print.py": NO_PRINT},
                                  rationale="library code kept shipping debug prints")
        assert error is False
        digest = body["proposal"]
        body, error = client.call("propose", files={"ok.py": "overwrite"}, rationale="sneaky")
        assert (error, body["error"]) == (True, "file 'ok.py' already exists; proposals may only add files")
    finally:
        client.close()

    assert not (project / "verification" / "no_print.py").exists()
    code, body = vkit(project, "status")
    assert [p["checks"] for p in body["proposals"]] == [["no-print"]]

    code, body = vkit(project, "accept", "--proposal", digest[:12], "--yes")
    assert (code, body["accepted_checks"]) == (0, ["no-print"])
    assert json.loads((project / "verification" / "features.json").read_text())["features"][0]["id"] == "core"

    code, body = vkit(project, "check", "run", "--check", "no-print")
    assert (code, [s["observation"] for s in body["runs"][0]["outcome"]["scenarios"]]) == (
        1, ["lib/core.py: print() in library code"])
    code, body = vkit(project, "proposals")
    assert body["proposals"] == []


def _formal_component():
    return {"id": "allocation", "package": {"id": "vkit.resource-ledger", "version": "0.1.0",
                                                "sha256": "a" * 64},
            "contract": "ledger.source.v1", "source": {"entry": "src/allocation.rs"},
            "profile": "verus-linux-x86_64-v1", "parameters": {"max_records": 7},
            "bindings": {"new": "api::new", "reserve": "api::reserve", "release": "api::release",
                         "reserve_batch": "api::reserve_batch", "apply_command": "api::apply_command",
                         "run_commands": "api::run_commands"}, "required": True}


def test_formal_proposal_preflights_and_records_the_complete_policy(tmp_path, monkeypatch):
    project = Project(tmp_path, tmp_path / ".git")
    store = Store(tmp_path / ".vkit")
    component_json = _formal_component()
    component = FormalComponent.from_json(component_json)
    semantic = {"engine": "verus", "version": "pinned"}
    verified = []
    resolved = []

    from vkit.formal import ledger, verus

    monkeypatch.setattr(ledger, "verify_package", lambda value: verified.append(value))
    monkeypatch.setattr(verus, "resolve_execution",
                        lambda value: resolved.append(value) or {"semantic": semantic})
    proposal = submit(project, store, {"formal": {"components": [component_json]},
                                       "rationale": "accept the complete ledger policy"})

    assert json.dumps({"components": [component_json]}, indent=2) in proposal.describe()
    assert apply(project, store, proposal) == []
    assert json.loads(project.manifest_path.read_text(encoding="utf-8"))["formal"] == {
        "components": [component_json]}
    assert verified == [component]
    assert resolved == [component]
    assert store.approved()[component.digest] == {
        "kind": "formal_policy",
        "policy_sha256": component.digest,
        "package_sha256": component.package.sha256,
        "checker_sha256": canonical_digest(semantic),
        "accepted_at": store.approved()[component.digest]["accepted_at"],
    }


@pytest.mark.parametrize("failure", [ValueError, RuntimeError])
def test_formal_preflight_failure_leaves_project_and_approval_untouched(tmp_path, monkeypatch, failure):
    project = Project(tmp_path, tmp_path / ".git")
    store = Store(tmp_path / ".vkit")
    from vkit.formal import ledger, verus

    monkeypatch.setattr(ledger, "verify_package", lambda component: None)

    def fail(component):
        raise failure("pinned checker unavailable")

    monkeypatch.setattr(verus, "resolve_execution", fail)
    proposal = submit(project, store, {"formal": {"components": [_formal_component()]},
                                       "files": {"new.txt": "not written"}, "rationale": "test preflight"})
    with pytest.raises(ProposalError, match="pinned checker unavailable"):
        apply(project, store, proposal)
    assert not project.manifest_path.exists()
    assert not (tmp_path / "new.txt").exists()
    assert not (store.root / "approved.json").exists()


def test_empty_formal_replacement_removes_policy_when_checks_remain(tmp_path):
    project = Project(tmp_path, tmp_path / ".git")
    store = Store(tmp_path / ".vkit")
    project.manifest_path.parent.mkdir(parents=True)
    check = scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])
    project.manifest_path.write_text(json.dumps({
        "schema_version": 3, "description": "existing", "checks": [check],
        "formal": {"components": [_formal_component()]},
    }), encoding="utf-8")
    proposal = submit(project, store, {"formal": {"components": []}, "rationale": "remove formal policy"})

    assert apply(project, store, proposal) == []
    resulting = json.loads(project.manifest_path.read_text(encoding="utf-8"))
    assert resulting["checks"] == [check]
    assert "formal" not in resulting


def test_invalid_merged_manifest_is_rejected_before_proposal_files_are_written(tmp_path):
    project = Project(tmp_path, tmp_path / ".git")
    store = Store(tmp_path / ".vkit")
    project.manifest_path.parent.mkdir(parents=True)
    check = scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])
    project.manifest_path.write_text(json.dumps({
        "schema_version": 3, "description": "existing", "checks": [check],
        "formal": {"components": [_formal_component()]},
    }), encoding="utf-8")
    proposal = submit(project, store, {"formal": {"components": []},
                                       "files": {"new.txt": "not written"}, "rationale": "remove policy"})
    project.manifest_path.write_text(json.dumps({"schema_version": 3, "checks": []}), encoding="utf-8")

    with pytest.raises(ProposalError):
        apply(project, store, proposal)
    assert not (tmp_path / "new.txt").exists()

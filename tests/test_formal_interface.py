from __future__ import annotations

import json

import pytest

from helpers import git
from test_formal_policy import component
from vkit import cli, evidence, jobs, query, runner
from vkit.formal import ledger, service, verus
from vkit.inputs import canonical_digest
from vkit.mcp import Server
from vkit.outcome import Passed, ScenarioResult


@pytest.fixture
def formal_project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "verification").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/allocation.rs").write_bytes(b"// captured source\r\n")
    policy = component()
    policy["package"]["sha256"] = ledger.load_package().identity.sha256
    (root / "verification/manifest.json").write_text(json.dumps({
        "schema_version": 3, "checks": [], "formal": {"components": [policy]}}), encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    identity = {"semantic": {"profile": "test-checker", "tool_sha256": "b" * 64}, "paths": {}}
    monkeypatch.setattr(verus, "resolve_execution", lambda _: identity)
    calls = []

    def execute(component, snapshot, run_dir, execution, should_cancel):
        calls.append((snapshot / component.entry).read_bytes())
        obligations = ledger.obligation_ids(component)
        return Passed(tuple(ScenarioResult(item, True, "checked") for item in obligations)), {
            "method": "verus_source", "assurance": "trusted_engine",
            "category": "source_verification", "scope": "required_api", "obligations": list(obligations),
            "execution_identity": {"semantic": identity["semantic"], "digest": execution}}

    monkeypatch.setattr(verus, "execute_formal", execute)
    monkeypatch.setattr(jobs, "_launch", lambda project, store, record:
                        runner.execute(project, record["run_id"], store=store))
    return root, identity, calls


def test_cli_and_mcp_share_formal_jobs_and_receipts(formal_project, capsys):
    root, identity, calls = formal_project
    assert cli.main(["accept", "--project", str(root), "--component", "allocation", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["accepted_components"] == ["allocation"]
    ctx = query.open_context(root)
    policy = ctx.require_manifest().require_component("allocation")
    assert ctx.store.approved()[policy.digest]["checker_sha256"] == canonical_digest(identity["semantic"])
    assert cli.main(["verify", "allocation", "--working-tree", "--project", str(root), "--json"]) == 0
    cli_view = json.loads(capsys.readouterr().out)
    mcp_view, error = Server(root).call("verify", {
        "component": "allocation", "goal": {"kind": "implements"}, "candidate": {"kind": "working_tree"}})
    assert error is False
    assert mcp_view["summary"]["job_id"] == cli_view["summary"]["job_id"]
    assert mcp_view["summary"]["run_id"] != cli_view["summary"]["run_id"]
    assert len(calls) == 2
    mcp_record = ctx.store.record(mcp_view["summary"]["run_id"])
    assert mcp_record is not None
    assert mcp_record["assurance"] == "trusted_engine"
    assert mcp_record["method"] == "verus_source"
    assert evidence.validate_record(mcp_record, ctx.store.run_dir(mcp_record["run_id"]))
    rejected, error = Server(root).call("verify", {
        "component": "allocation", "goal": {"kind": "implements"},
        "candidate": {"kind": "working_tree"}, "authority": "protected"})
    assert error and "unknown argument" in rejected["error"]


def test_formal_replay_runs_checker_again_and_rejects_changed_working_tree(formal_project):
    root, identity, calls = formal_project
    ctx = query.open_context(root)
    policy = ctx.require_manifest().require_component("allocation")
    ctx.store.approve({policy.digest: {"kind": "formal_policy", "policy_sha256": policy.digest,
                                      "package_sha256": policy.package.sha256,
                                      "checker_sha256": canonical_digest(identity["semantic"]), "accepted_at": "now"}})
    record = service.verify(ctx, {"component": "allocation", "goal": {"kind": "implements"},
                                  "candidate": {"kind": "working_tree"}})
    bundle = root.parent / "bundle"
    evidence.export(ctx.store, record["run_id"], bundle)
    replay = jobs.replay(ctx.project, ctx.require_manifest(), bundle, store=ctx.store)
    assert len(calls) == 2
    assert replay["job_id"] == record["job_id"]
    assert replay["replayed_from"]["receipt"] == record["receipt"]["sha256"]
    (root / "src/allocation.rs").write_bytes(b"changed candidate")
    with pytest.raises(ValueError, match="independently captured"):
        jobs.replay(ctx.project, ctx.require_manifest(), bundle, store=ctx.store)
    assert len(calls) == 2

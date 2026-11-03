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
    monkeypatch.setattr(jobs, "_launch", lambda project, store, record, *, asset_environment=None:
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
    request = {"schema_version": 2, "claim": policy.claim.to_json(),
               "candidate": {"kind": "working_tree"}}
    request_file = root.parent / "package-request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    assert cli.main(["verify", "--request", str(request_file), "--project", str(root), "--json"]) == 0
    file_view = json.loads(capsys.readouterr().out)
    mcp_view, error = Server(root).call("verify", request)
    assert error is False
    assert mcp_view["summary"]["job_id"] == cli_view["summary"]["job_id"]
    assert file_view["summary"]["job_id"] == cli_view["summary"]["job_id"]
    assert mcp_view["summary"]["run_id"] != cli_view["summary"]["run_id"]
    assert len(calls) == 3
    mcp_record = ctx.store.record(mcp_view["summary"]["run_id"])
    assert mcp_record is not None
    assert mcp_record["assurance"] == "trusted_engine"
    assert mcp_record["method"] == "verus_source"
    assert evidence.validate_record(mcp_record, ctx.store.run_dir(mcp_record["run_id"]))
    rejected, error = Server(root).call("verify", {
        **request, "authority": "protected"})
    assert error and "unknown argument" in rejected["error"]


@pytest.mark.parametrize("pin", ("policy_sha256", "package_sha256"))
def test_public_verification_rejects_legacy_finite_and_mismatched_claims_before_submission(formal_project, pin):
    root, _, _ = formal_project
    ctx = query.open_context(root)
    policy = ctx.require_manifest().require_component("allocation")
    request = {"schema_version": 2, "claim": policy.claim.to_json(),
               "candidate": {"kind": "working_tree"}}
    wrong_claim = {**request, "claim": {**request["claim"], pin: "0" * 64}}

    with pytest.raises(ValueError, match="package claim does not match"):
        service.verify(ctx, wrong_claim)
    assert not ctx.store.runs()

    server = Server(root)
    legacy, legacy_error = server.call("verify", {
        "component": "allocation", "goal": {"kind": "implements"},
        "candidate": {"kind": "working_tree"}})
    finite, finite_error = server.call("verify", {
        "schema_version": 1, "base_oid": "a" * 40, "candidate_oid": "b" * 40,
        "intent_sha256": "c" * 64,
        "claim": {"kind": "finite_contract", "contract": {}}})
    mismatched, mismatch_error = server.call("verify", wrong_claim)
    assert legacy_error and legacy["error"] == "unknown argument(s): component, goal"
    assert finite_error and "unknown argument(s)" in finite["error"]
    assert mismatch_error and "package claim does not match" in mismatched["error"]
    assert not ctx.store.runs()


def test_cli_request_file_rejects_duplicate_json_keys_before_submission(formal_project, capsys):
    root, _, _ = formal_project
    request_file = root.parent / "duplicate-request.json"
    request_file.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")

    assert cli.main(["verify", "--request", str(request_file), "--project", str(root), "--json"]) == cli.EXIT_INVALID
    output = json.loads(capsys.readouterr().out)
    assert "duplicate JSON object key: schema_version" in output["error"]
    assert not query.open_context(root).store.runs()


def test_cli_request_file_rejects_invalid_json_and_conflicting_sugar_before_submission(formal_project, capsys):
    root, _, _ = formal_project
    request_file = root.parent / "invalid-request.json"
    request_file.write_text("not JSON", encoding="utf-8")

    assert cli.main(["verify", "--request", str(request_file), "--project", str(root), "--json"]) == cli.EXIT_INVALID
    output = json.loads(capsys.readouterr().out)
    assert "not valid UTF-8 JSON" in output["error"]

    assert cli.main(["verify", "allocation", "--request", str(request_file),
                     "--project", str(root), "--json"]) == cli.EXIT_INVALID
    output = json.loads(capsys.readouterr().out)
    assert "cannot be combined with a component" in output["error"]
    assert not query.open_context(root).store.runs()


def test_formal_replay_runs_checker_again_and_rejects_changed_working_tree(formal_project):
    root, identity, calls = formal_project
    ctx = query.open_context(root)
    policy = ctx.require_manifest().require_component("allocation")
    ctx.store.approve({policy.digest: {"kind": "formal_policy", "policy_sha256": policy.digest,
                                      "package_sha256": policy.package.sha256,
                                      "checker_sha256": canonical_digest(identity["semantic"]), "accepted_at": "now"}})
    record = service.verify(ctx, {"schema_version": 2, "claim": policy.claim.to_json(),
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

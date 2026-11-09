from __future__ import annotations

import io
import json
import sys
import types
import os
from pathlib import Path

import pytest

from helpers import DRIVER, McpClient, git, scenario_check, vkit
from vkit.paths import Project
from vkit.query import Context, doctor, open_context, run_view, run_view_full, status
from vkit.store import Store, write_json
from vkit.requests import PackageVerificationRequest, WorkingTree
from vkit.inputs import canonical_digest
from vkit.outcome import Passed
from vkit.runner import execute, prepare_formal


@pytest.mark.parametrize("run_id", ["", ".", "..", "../foreign", "nested/run", r"nested\run", "/absolute", "C:relative"])
def test_run_lookup_rejects_paths(tmp_path, run_id):
    store = Store(tmp_path / "state")

    with pytest.raises(ValueError, match="run_id"):
        store.record(run_id)


def test_run_lookup_cannot_read_a_foreign_record(tmp_path):
    store = Store(tmp_path / "state")
    foreign = tmp_path / "foreign"
    write_json(foreign / "record.json", {"run_id": "foreign", "state": "done"})

    with pytest.raises(ValueError, match="run_id"):
        store.record(str(foreign))


def test_mcp_refuses_run_paths_for_reading_and_cancellation(make_project, tmp_path):
    project = make_project({"README.md": "run lookup\n"}, [], accept=False)
    foreign = tmp_path / "foreign"
    write_json(foreign / "record.json", {"run_id": "foreign", "state": "done"})
    client = McpClient(project)
    try:
        for name in ("run_get", "run_cancel"):
            body, error = client.call(name, run_id=str(foreign))
            assert error and body == {
                "error": "run_id must contain only letters, digits, underscores, or hyphens",
            }
    finally:
        client.close()


@pytest.mark.parametrize("offset,limit,expected", [
    (2, 3, {"stream": "stdout", "offset": 2, "size": 10, "text": "234"}),
    (-3, 2, {"stream": "stdout", "offset": 7, "size": 10, "text": "78"}),
    (-20, 2, {"stream": "stdout", "offset": 0, "size": 10, "text": "01"}),
    (20, 2, {"stream": "stdout", "offset": 20, "size": 10, "text": ""}),
    (1, -1, {"stream": "stdout", "offset": 1, "size": 10, "text": ""}),
])
def test_run_log_reads_only_the_requested_window(tmp_path, monkeypatch, offset, limit, expected):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "state": "done"})
    project = Project(tmp_path, tmp_path / ".git")
    context = Context(project, store, None, None)
    log = store.run_dir("example-run") / "stdout.log"
    log.write_bytes(b"0123456789")
    original_open = Path.open

    class WindowOnly(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= max(0, limit), "log reads must be bounded by the requested window"
            return super().read(size)

    def open_log(path, *args, **kwargs):
        if path == log:
            return WindowOnly(b"0123456789")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_log)

    assert run_view_full(context, "example-run", offset=offset, limit=limit)["log"] == expected


def test_run_without_a_log_has_an_empty_window(tmp_path):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "state": "done"})
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    assert run_view_full(context, "example-run")["log"] == {
        "stream": "stdout", "offset": 0, "size": 0, "text": "",
    }


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_run_view_does_not_open_a_non_regular_log(tmp_path, monkeypatch, kind):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "state": "done"})
    log = store.run_dir("example-run") / "stdout.log"
    if kind == "directory":
        log.mkdir()
    elif hasattr(os, "mkfifo"):
        os.mkfifo(log)
    else:
        pytest.skip("named pipes require POSIX")
    original_open = Path.open

    def open_regular(path, *args, **kwargs):
        assert path != log, "non-regular logs must not be opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_regular)
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    assert run_view_full(context, "example-run")["log"] == {
        "stream": "stdout", "offset": 0, "size": 0, "text": "",
    }


def test_run_view_defaults_to_a_compact_summary_and_selects_details(tmp_path):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "job_id": "job", "state": "done",
                "outcome": {"result": "FAIL", "scenarios": [{"id": "ledger.reserve", "result": "FAIL"}]},
                "obligations": ["ledger.reserve"], "scope": "required_api", "assurance": "trusted_engine"})
    (store.run_dir("example-run") / "stdout.log").write_text("large log", encoding="utf-8")
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    default = run_view(context, "example-run")
    assert default == {"sections": ["summary"], "summary": {
        "job_id": "job", "run_id": "example-run", "subject": None, "check_id": None, "state": "done",
        "outcome": {"result": "FAIL"},
        "method": None, "category": None, "scope": "required_api", "assurance": "trusted_engine",
        "claim_outcome": None, "receipt": None,
        "counts": {"obligations": {"total": 1, "satisfied": 0, "failed": 1, "unknown": 0}},
        "failure_ids": ["ledger.reserve"], "next_action": "inspect_obligations",
    }}
    detail = run_view(context, "example-run", sections=["obligations", "log"], limit=4)
    assert detail["sections"] == ["obligations", "log"]
    assert detail["obligations"] == ["ledger.reserve"]
    assert detail["log"]["text"] == "larg"
    assert "source" not in detail


@pytest.mark.parametrize(("state", "outcome", "expected"), [
    ("queued", None, {"total": 2, "satisfied": 0, "failed": 0, "unknown": 2}),
    ("done", {"result": "BLOCKED", "scenarios": [{"id": "ledger.new", "result": "PASS"}]},
     {"total": 2, "satisfied": 1, "failed": 0, "unknown": 1}),
])
def test_run_summary_keeps_unobserved_known_obligations_unknown(tmp_path, state, outcome, expected):
    store = Store(tmp_path / "state")
    store.save({"run_id": "formal-run", "state": state, "outcome": outcome,
                "obligations": ["ledger.new", "ledger.reserve"]})
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    summary = run_view(context, "formal-run")["summary"]
    assert summary["counts"]["obligations"] == expected


def test_partial_tlc_result_does_not_claim_all_obligations_were_observed(tmp_path):
    store = Store(tmp_path / "state")
    store.save({"run_id": "tlc-failure", "state": "done", "outcome": {"result": "FAIL"},
                "obligations": {"satisfied": [], "counterexamples": [
                    {"obligation": {"obligation": "NoFalsePass"}, "trace": "bad identity"}]}})
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    summary = run_view(context, "tlc-failure")["summary"]

    assert summary["counts"]["obligations"] == {
        "total": None, "satisfied": 0, "failed": 1, "unknown": None,
    }
    assert summary["failure_ids"] == ["NoFalsePass"]


def test_run_view_reports_concrete_failed_formal_phase_without_inventing_obligations(tmp_path):
    store = Store(tmp_path / "state")
    store.save({"run_id": "formal-run", "state": "done",
                "outcome": {"result": "BLOCKED", "reason": "formal_obligation", "detail": "formal obligation failed"},
                "obligations": ["new", "reserve"]})
    run_dir = store.run_dir("formal-run")
    (run_dir / "container.stderr").write_text("formal_obligation:verified_candidate\n", encoding="utf-8")
    phase = run_dir / "formal-output" / "verified_candidate.log"
    phase.parent.mkdir()
    phase.write_bytes(b"error: postcondition not satisfied\n")
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    view = run_view(context, "formal-run", sections=["diagnostics"])
    assert view["diagnostics"]["diagnostics"] == [{
        "kind": "formal_obligation", "summary": "formal obligation failed",
    }, {
        "phase": "verified_candidate", "summary": "error: postcondition not satisfied",
        "artifact": "formal-output/verified_candidate.log", "container_log": "container.stderr",
        "size": 35,
    }]
    assert "obligation_id" not in view["diagnostics"]["diagnostics"][1]


@pytest.mark.parametrize("sections", [[], ["unknown"], ["all", "log"]])
def test_run_view_rejects_invalid_section_selectors(tmp_path, sections):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "state": "done"})
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    with pytest.raises(ValueError, match="sections"):
        run_view(context, "example-run", sections=sections)

def _install_formal_component(project, *, required=True, reserve="api::reserve"):
    path = project / "verification" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["formal"] = {"components": [{
        "id": "allocation",
        "package": {"id": "vkit.resource-ledger", "version": "0.1.0", "sha256": "a" * 64},
        "contract": "ledger.source.v1",
        "source": {"entry": "src/allocation.rs"},
        "profile": "verus-linux-x86_64-v1",
        "parameters": {"max_records": 7},
        "bindings": {"new": "api::new", "reserve": reserve, "release": "api::release",
                      "reserve_batch": "api::reserve_batch", "apply_command": "api::apply_command",
                      "run_commands": "api::run_commands"},
        "required": required,
    }]}
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _formal_identity():
    return {"semantic": {"profile": "verus-linux-x86_64-v1", "image_id": "sha256:" + "b" * 64},
            "paths": {"archive": "archive.zip"}}


def _accept_formal(store, component, identity):
    store.approve({component.digest: {"kind": "formal_policy", "policy_sha256": component.digest,
                                    "package_sha256": component.package.sha256,
                                    "checker_sha256": canonical_digest(identity["semantic"]),
                                    "accepted_at": "now"}})


def _complete_formal(context, monkeypatch, identity):
    from vkit import state

    monkeypatch.setattr(state, "_resolve_execution", lambda _: identity)
    component = context.manifest.require_component("allocation")
    _accept_formal(context.store, component, identity)
    ledger = types.ModuleType("vkit.formal.ledger")
    def verify_package(_):
        return None
    ledger.verify_package = verify_package
    verus = types.ModuleType("vkit.formal.verus")
    def execute_formal(actual, snapshot, run_dir, actual_identity, should_cancel):
        assert actual == component
        assert (snapshot / component.entry).read_bytes() == (context.project.root / component.entry).read_bytes()
        assert actual_identity == canonical_digest(identity["semantic"]) and not should_cancel()
        return Passed(()), {"obligations": {"satisfied": ["allocation:reserve"], "counterexamples": []},
                            "execution_identity": {"semantic": identity["semantic"], "digest": actual_identity}}
    verus.execute_formal = execute_formal
    monkeypatch.setitem(sys.modules, "vkit.formal.ledger", ledger)
    monkeypatch.setitem(sys.modules, "vkit.formal.verus", verus)
    record = prepare_formal(context.project, PackageVerificationRequest(component.claim, WorkingTree()),
                            component, canonical_digest(identity["semantic"]), store=context.store)
    return execute(None, record["run_id"], store=context.store)


def test_required_formal_goal_blocks_a_passing_registered_check_when_unapproved(make_project):
    project = make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n',
                            "src/allocation.rs": "pub fn reserve() {}\n"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    assert vkit(project, "check", "run", "--check", "ok")[0] == 0
    _install_formal_component(project)
    result = status(open_context(project))
    assert result["checks"][0]["state"] == "fresh_pass"
    assert result["formal"][0]["state"] == "not_approved"
    assert result["gate"]["verdict"] == "REJECTED"
    assert result["gate"]["formal"] == [{"id": "allocation", "state": "not_approved",
                                            "required": True, "run_id": None}]


def test_optional_formal_goal_does_not_block_a_passing_check(make_project):
    project = make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n',
                            "src/allocation.rs": "pub fn reserve() {}\n"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    assert vkit(project, "check", "run", "--check", "ok")[0] == 0
    _install_formal_component(project, required=False)
    result = status(open_context(project))
    assert result["formal"][0]["state"] == "not_approved"
    assert result["gate"]["verdict"] == "READY"


def test_formal_source_and_policy_changes_make_evidence_stale(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src/allocation.rs").write_bytes(b"pub fn reserve() {}\r\n")
    (project / "verification").mkdir()
    (project / "verification/manifest.json").write_text(json.dumps({"schema_version": 3, "checks": []}), encoding="utf-8")
    git(project, "init", "-q")
    git(project, "add", ".")
    git(project, "commit", "-qm", "initial")
    _install_formal_component(project)
    context = open_context(project)
    identity = _formal_identity()
    completed = _complete_formal(context, monkeypatch, identity)
    assert completed["outcome"]["result"] == "PASS"
    assert status(context)["formal"][0]["state"] == "fresh_pass"
    (project / "src/allocation.rs").write_text("pub fn reserve() { }\n", encoding="utf-8")
    assert status(context)["formal"][0]["state"] == "stale"
    (project / "src/allocation.rs").write_bytes(b"pub fn reserve() {}\r\n")
    _install_formal_component(project, reserve="api::reserve_v2")
    context = open_context(project)
    component = context.manifest.require_component("allocation")
    _accept_formal(context.store, component, identity)
    assert status(context)["formal"][0]["state"] == "stale"


def test_formal_setup_failure_is_a_blocked_status_and_doctor_finding(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src/allocation.rs").write_bytes(b"pub fn reserve() {}\r\n")
    (project / "verification").mkdir()
    (project / "verification/manifest.json").write_text(json.dumps({"schema_version": 3, "checks": []}), encoding="utf-8")
    git(project, "init", "-q")
    git(project, "add", ".")
    git(project, "commit", "-qm", "initial")
    _install_formal_component(project)
    context = open_context(project)
    identity = _formal_identity()
    component = context.manifest.require_component("allocation")
    _accept_formal(context.store, component, identity)
    from vkit import state
    def unavailable(_):
        raise RuntimeError("Docker or Verus unavailable")
    monkeypatch.setattr(state, "_resolve_execution", unavailable)
    import vkit.query as query
    monkeypatch.setattr(query, "_resolve_execution", unavailable)
    result = status(context)
    assert result["gate"]["verdict"] == "REJECTED"
    assert result["formal"][0]["state"] == "blocked"
    checked = doctor(context)
    assert checked["ok"] is False
    assert checked["findings"][-1]["kind"] == "formal_goal"
    assert "Docker or Verus unavailable" in checked["findings"][-1]["detail"]


PASSES = DRIVER + 'report([("ok", True, "ok")])\n'
GEN_DRIVER = DRIVER + 'report([("ok", open("generated/x.py", encoding="utf-8").read() == "y\\n", "gen")])\n'


def gen_check():
    return {"id": "gen", "kind": "scenario", "timeout_seconds": 60, "artifact": "result.json",
            "command": ["{{python}}", "generated/check_gen.py", "{{run_dir}}/result.json"],
            "required_scenarios": ["ok"], "inputs": ["generated/*"],
            "subject": {"paths": ["generated/x.py"], "digest": None}, "claim_id": "gen"}


def test_status_names_closure_and_scope_claim_coverage_for_requested_paths(make_project):
    project = make_project({"ok.py": PASSES, "generated/x.py": "y\n", "generated/check_gen.py": GEN_DRIVER},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]), gen_check()])
    vkit(project, "check", "run", "--needed")
    (project / "generated" / "credentials").write_text("k\n", encoding="utf-8")
    out = status(open_context(project), paths=["ok.py", "generated/credentials"])
    assert out["gate"]["verdict"] == "READY_SCOPED"
    assert out["coverage_kinds"] == {"closure": ["ok.py"], "scope_claim": ["generated/credentials"]}
    assert out["unmapped_paths"] == []
    closure = status(open_context(project), paths=["ok.py"])
    assert closure["gate"]["verdict"] == "READY"
    assert closure["coverage_kinds"] == {"closure": ["ok.py"], "scope_claim": []}


def test_session_status_computes_only_checks_reading_the_changed_paths(make_project, monkeypatch):
    from vkit import evidence

    project = make_project({"ok.py": PASSES, "other.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]),
                            scenario_check("other", "other.py", scenarios=["ok"], inputs=["other.py"])])
    vkit(project, "check", "run", "--needed")
    ctx = open_context(project)
    calls = []
    real = evidence.backend_identity
    monkeypatch.setattr(evidence, "backend_identity", lambda check: calls.append(check.id) or real(check))
    out = status(ctx, paths=["ok.py"], session=True)
    assert calls == ["ok"]
    assert [c["id"] for c in out["checks"]] == ["ok"] and out["gate"]["verdict"] == "READY"
    status(ctx)
    calls.clear()
    status(ctx)
    assert sorted(calls) == ["ok", "other"]


def test_session_status_walks_the_tree_once(make_project, monkeypatch):
    import vkit.inputs

    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    ctx = open_context(project)
    calls = []
    real = vkit.inputs.source_files
    monkeypatch.setattr(vkit.inputs, "source_files", lambda p: calls.append(p) or real(p))
    status(ctx, paths=["ok.py"], session=True)
    assert len(calls) == 1
    status(ctx)
    assert len(calls) == 2


def test_session_status_scopes_the_verdict_to_uncovered_change(make_project):
    project = make_project({"ok.py": PASSES, "README.md": "notes\n"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    out = status(open_context(project), paths=["README.md"], session=True)
    assert out["gate"]["verdict"] == "READY_SCOPED" and "README.md" in out["gate"]["reason"]


def test_session_status_treats_declared_glob_coverage_as_a_scope_claim(make_project):
    project = make_project({"ok.py": PASSES, "generated/x.py": "y\n", "generated/check_gen.py": GEN_DRIVER},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"]), gen_check()])
    vkit(project, "check", "run", "--needed")
    (project / "generated" / "credentials").write_text("k\n", encoding="utf-8")
    out = status(open_context(project), paths=["generated/credentials"], session=True)
    assert out["gate"]["verdict"] == "READY_SCOPED" and out["coverage_kinds"]["scope_claim"] == ["generated/credentials"]

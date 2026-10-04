import json
from dataclasses import replace

import pytest

from test_evidence_contract import RUNNER, a_check, parse, project
from test_tasks import contract, publish_run
from vkit.claimkind import ClaimCategory
from vkit.execution import run_check
from vkit.identity import compute_source_identity
from vkit.outcome import ScenarioResult
from vkit.storage import Store
from vkit.tasks import AcceptanceContext, Requirement, compute_readiness, expected_identities, open_task
from vkit.verifiers import CaseObligation, obligation_to_json
from vkit.verifiers.dispatch import ScenarioReading


@pytest.mark.parametrize("field,value", [
    ("required_tests", ["test_app.py::test_second"]),
    ("runner", {"executable": "other-python", "base_argv": ["-m", "pytest"]}),
    ("expect_report_version", 2),
])
def test_native_contract_edits_change_policy_identity(project, field, value):
    check = a_check(id="test", kind="pytest", required_tests=["test_app.py::test_first"],
                    runner=RUNNER, report_format="pytest_json_report", expect_report_version=1)
    before = parse(project, [check]).digest()
    assert parse(project, [{**check, field: value}]).digest() != before


def test_policy_identity_survives_checkout_relocation(project, tmp_path):
    manifest = parse(project, [a_check(id="test", kind="pytest", required_tests=["test_app.py::test_first"],
                     runner=RUNNER, report_format="pytest_json_report", expect_report_version=1)])
    moved_root = tmp_path / "moved"
    moved = replace(manifest, project=replace(project, root=moved_root),
                    checks={k: replace(c, cwd=moved_root, variant=replace(c.variant, cwd=moved_root))
                            for k, c in manifest.checks.items()})
    assert moved.digest() == manifest.digest()


@pytest.mark.parametrize("satisfied", [[], [{"garbage": "not an obligation"}],
    [{"kind": "counterexample", "obligation": {"kind": "case", "obligation": "s"}, "trace": "failed"}],
])
def test_empty_or_malformed_obligations_cannot_discharge_a_task(tmp_path, satisfied):
    store = Store(tmp_path / "state.sqlite3")
    pinned = contract()
    pinned["requirements"] = [Requirement("c1", (CaseObligation("s"),), ClaimCategory.SCENARIO).to_json()]
    open_task(store, task_id="task", contract=pinned, policy_digest="pd")
    publish_run(store, "run", "c1", "task", "PASS")
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET evidence_kind = 'scenario', satisfied_json = ? WHERE run_id = 'run'",
                     (json.dumps(satisfied),))
    result = compute_readiness(store, "task", required_check_ids=("c1",))
    assert result.readiness == "BLOCKED"
    assert result.context["unmet_obligations"]["c1"] == ["case 's'"]
    assert result.tested == {}


def test_scenario_observations_supply_typed_obligations():
    reading = ScenarioReading((ScenarioResult("pass", True, "matched"), ScenarioResult("fail", False, "different")))
    met, failures = reading.obligation_results()
    assert met == [{"kind": "case_satisfied", "obligation": obligation_to_json(CaseObligation("pass")),
                    "observation": "matched"}]
    assert failures == [{"obligation": obligation_to_json(CaseObligation("fail")), "trace": "different"}]


def test_native_challenge_bytes_are_automatic_inputs(project):
    challenge = project.root / "Case.lean"
    challenge.write_text("theorem claimed : True := True.intro", encoding="utf-8")
    check = a_check(id="proof", kind="lean", challenge={"module": "Case", "path": "Case.lean"},
                    theorems=["claimed"], profile="reviewed_proof_sources", permitted_axioms=[],
                    toolchain={"tool": "lean"})
    manifest = parse(project, [check])
    assert manifest.require("proof").inputs == ("Case.lean",)
    original = manifest.fixture_identity().digest
    challenge.write_text("theorem claimed : True := by trivial", encoding="utf-8")
    assert manifest.fixture_identity().digest != original


def test_new_run_cannot_rebind_admitted_verifier_inputs(tmp_path, project):
    store = Store(tmp_path / "state.sqlite3")
    pinned = {**contract(), "fixture_digest": "approved", "resources": [],
              "repository": {"root": str(project.root), "git_common_dir": str(project.git_common_dir)}}
    task = open_task(store, task_id="task", contract=pinned, policy_digest="pd")
    publish_run(store, "run", "c1", "task", "PASS")
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET fixture_digest = 'replacement' WHERE run_id = 'run'")
    context = AcceptanceContext(project, "pd", "source", "replacement",
                                verifier_fixture_digest="replacement")
    assert expected_identities(task, context)["verifier_fixture_digest"] == "replacement"
    result = compute_readiness(store, "task", required_check_ids=("c1",), context=context)
    assert result.readiness == "BLOCKED"
    assert any("admission" in gap for gap in result.gaps)


def test_run_lookup_selects_the_requested_identity(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    publish_run(store, "older", "c1", "first", "PASS")
    publish_run(store, "newer", "c2", "second", "PASS")
    assert [r["run_id"] for r in store.list_runs(run_id="older", limit=1)] == ["older"]
    assert store.list_runs(run_id="older", task_id="second") == []


def test_unusable_cleanup_policy_is_a_published_blocked_run(project):
    check = a_check(id="test", kind="pytest", required_tests=["test_app.py::test_first"],
                    runner=RUNNER, report_format="pytest_json_report", expect_report_version=1)
    manifest = parse(project, [check])
    (project.root / "verification/cleanup.json").write_text("not JSON", encoding="utf-8")
    store = Store(project.state_root / "audit.sqlite3")
    result = run_check(manifest, "test", store=store, source=compute_source_identity(project))
    assert result.report["outcome"]["result"] == "BLOCKED"
    assert result.report["outcome"]["reason"] == "cleanup_required"
    assert store.run_status(result.report["run_id"])["lifecycle"] == "terminal"


def test_cleanup_policy_cannot_be_disabled_after_admission(project):
    from vkit.tasks import acceptance_context, admit, finalize
    check = a_check(id="proof", kind="lean", challenge={"module": "Case", "path": "Case.lean"},
                    theorems=["claimed"], profile="reviewed_proof_sources",
                    permitted_axioms=[], toolchain={"tool": "lean"})
    (project.root / "Case.lean").write_text("theorem claimed : True := by trivial", encoding="utf-8")
    manifest = parse(project, [check])
    policy_path = project.root / "verification/cleanup.json"
    policy_path.write_text(json.dumps({"mode": "apply_verified", "enabled_rules": ["ORDINARY_TRAILING_COMMENT"]}), encoding="utf-8")
    store = Store(project.db_path)
    original = acceptance_context(project, lambda: manifest)
    admitted = admit(store, "cleanup-policy", context=original)
    policy_path.write_text(json.dumps({"mode": "off"}), encoding="utf-8")
    outcome = run_check(manifest, "proof", store=store, source=compute_source_identity(project),
                        task_id=admitted.task_id, attempt=admitted.generation)
    assert outcome.report["outcome"]["reason"] == "cleanup_required"
    assert "changed since admission" in outcome.report["outcome"]["detail"]
    result = finalize(store, admitted.task_id, context=acceptance_context(project, lambda: manifest))
    assert result.readiness == "BLOCKED"
    assert any("cleanup policy changed since admission" in gap for gap in result.gaps)

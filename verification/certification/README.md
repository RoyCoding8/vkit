# Bounded authority model

`Authority.tla` checks the package-verification decision boundary: a formal request binds a component claim, policy/package identities, checker, and candidate capture; protected CI binds the component and target from host-selected target policy; a receipt matches the sealed job; and preflight cannot become a source-proof result.

The internal submission record includes `host_target` to model authority injected by the CI host. It is not a field in the public package request schema. Local verification binds the current local component at submission. CI verification binds the required component in the captured protected target. Candidate policy is modeled separately and cannot choose CI authority.

Policy, checker, and source can drift after submission. The job and receipt keep the submitted snapshot. The historical receipt remains valid against that snapshot. A reuse attempt compares current policy, checker, source, and claim with the retained job; `reuse_checked` is only the returned state of this one request, not a cache for later requests. A receipt PASS means that attempt passed; it does not assert that the current checkout is presently gate-ready.

The good config checks ten named invariants, and the qualification requires all ten in the satisfied set. Seven separate mutant configs inject a mismatched claim, candidate-selected target, wrong source capture, swapped receipt claim, stale reuse after pin drift, valid reuse incorrectly rejected, or preflight promoted to PASS. These are constructed controls, not reports of production bugs. The model has one job, one component, two policy/package/checker/source revisions, one trusted target, and one candidate target. Existing source API tests cover these guards: `tests/test_formal_interface.py::test_public_verification_rejects_legacy_finite_and_mismatched_claims_before_submission` checks public claim rejection; `tests/test_ci_gate.py::test_gate_pins_formal_request_to_target_policy_with_mocked_engine_when_candidate_deletes_it` checks protected target policy and claim binding; `tests/test_jobs.py::test_formal_job_executes_from_captured_commit_without_opening_checkout` checks captured source, receipt validation, and reuse after policy, package, or checker drift; and `tests/test_intent_revision.py::test_preflight_binds_real_git_revisions_without_claiming_source_behavior` checks preflight scope. The CI case uses a mocked formal engine and proves only request and receipt dataflow. Hashes and Git internals are opaque identity atoms. It does not model authorization of the trusted host, human acceptance, scheduler concurrency, actual verifier correctness, or cryptographic collision resistance.

Run with the repository-pinned TLC jar and the current worktree's Python import path:

```powershell
$env:VKIT_TLA2TOOLS_JAR = 'PATH_TO_JAR_MATCHING_REPOSITORY_PIN'
$env:PYTHONPATH = (Resolve-Path '.\src').Path
.\.venv\Scripts\python.exe scripts\check_authority.py
```

The script makes disposable VKit projects and runs the existing `check run` TLC path. It keeps its report and per-case project evidence under `tmp\authority-model` by default.

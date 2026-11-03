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

## Finite difference assessment model

`Differences.tla` models raw paired observations through collection, per-input assessment, and final status. The `bounded` configuration nondeterministically chooses and orders zero, one, or two records, allowing repeated identical records, from a 13-record pool over two inputs. Records include selected and shrink cases, stable differences and repairs, failures, partial or unstable repeats, and outputs outside the observation model. Separate configurations cover guard intersections, duplicate identical selected observations, and the empty admissible domain.

Seven clean configurations check the full invariant set. Two additional reachability configurations require a counterexample to the deliberately false NoCompletedState or NoConformance property, demonstrating that empty collection reaches assessment completion and complete evidence reaches conformance. These are positive reachability witnesses, not product defects. Eight mutant configurations each require a named counterexample: shrink evidence filling selected coverage, ignoring invalid shrink evidence, hiding a stable violation behind missing or unknown evidence, using the union of overlapping guards, skipping standing intent, permitting unguarded changes, rejecting a valid standing repair, and treating an empty domain as conformance. These are constructed model controls. The bounded model explores every ordered evidence sequence, with multiplicity, of length zero through two from its 13-record pool (183 sequences); it does not explore arbitrary output strings or longer runs.

The model keeps raw records separate from the derived coverage and finding sequences, and selected-record counts preserve identical duplicates. Its invariants inspect raw observations and policy relations, and require violations to remain visible even when coverage is incomplete. Valid base outputs outside standing intent may be repaired by an allowed candidate output; a candidate outside standing intent remains a violation. Empty admissible input stays unknown. Source proof and approval authority are fixed false.

The runtime bridge in `tests/test_difference_model_bridge.py` calls the public intent and difference-policy parsers and the finite assessor with literal observations. It checks exact statuses, coverage, finding kinds, missing inputs, duplicate identical selected rows, intersected guard IDs, repair behavior, parser rejection of conflicting overlap, and the serialized no-authority fields. The status names bridge to the public evaluator (`observed_conformance`, `observed_violation`, `unknown`); they do not claim a source proof or implementation refinement.

Run either suite through the shared qualification runner:

```powershell
$env:VKIT_TLA2TOOLS_JAR = 'PATH_TO_JAR_MATCHING_REPOSITORY_PIN'
$env:PYTHONPATH = (Resolve-Path '.\src').Path
.\.venv\Scripts\python.exe scripts\check_authority.py --suite authority
.\.venv\Scripts\python.exe scripts\check_authority.py --suite differences
```

Both suites retain isolated VKit project, checker, and TLC evidence below `tmp\authority-model` or `tmp\difference-model` by default. The finite suite checks a small abstraction plus selected runtime bridge cases. It does not establish implementation refinement, that the accepted finite inputs cover arbitrary program inputs, or approval authority.

## Compare actual TLC decisions with Python

`DifferenceDecisionBridge.tla` instruments the seven clean finite configurations without changing their transition relation. `scripts/check_difference_decisions.py` runs TLC, parses its terminal decisions, and compares every emitted row with the public Python assessor. Distinct raw sequences are counted separately for coverage, preserving record order and multiplicity. The declared pools contain 211 distinct sequences across the seven configurations. Negative controls remove a modeled violation and change a terminal status while retaining the same raw sequence.

```powershell
$env:VKIT_TLA2TOOLS_JAR = 'PATH_TO_JAR_MATCHING_REPOSITORY_PIN'
$env:PYTHONPATH = (Resolve-Path '.\src').Path
.\.venv\Scripts\python.exe scripts\check_difference_decisions.py --output tmp\difference-decisions
```

Java is resolved from `JAVA_HOME` or `PATH`. The script requires the repository-pinned TLC jar, imports this checkout's evaluator, and retains the copied model, instrumented configurations, bounded logs, hashes, comparisons, and negative controls. CI runs this comparison and uploads its evidence. Agreement covers these finite record pools and the compared decision fields. It does not prove general implementation refinement, source behavior, or approval authority.

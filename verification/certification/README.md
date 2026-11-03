# Verification models

TLA+ models and qualification scripts for VKit's verification authority and finite change assessment. These are developer checks of bounded decision models.

## Authority

`Authority.tla` models submission, receipt validation, and evidence reuse. A request binds its component claim, policy, package, checker, and captured source. Protected CI derives its component and target from host-selected policy; candidate policy cannot select that authority.

Jobs and receipts retain the submitted snapshot when policy, tools, or source change. Reuse checks the current identities against that snapshot. A historical passing receipt therefore does not establish that the current checkout is ready to pass the gate.

The configuration contains one job, one component, two policy/package/checker/source revisions, one trusted target, and one candidate target. Qualification checks the invariants and requires constructed faulty variants to produce counterexamples. Runtime tests separately exercise claim rejection, protected target selection, source capture, receipt validation, and reuse after drift.

## Finite difference assessment

`Differences.tla` models the collection and assessment of paired observations. Its bounded configuration explores ordered sequences of zero, one, or two records from a finite pool over two inputs. Additional configurations cover overlapping guards, duplicate observations, and an empty admissible domain.

The model checks that selected inputs have evidence, overlapping guards intersect, and violations remain visible when other evidence is missing or unknown. It also checks repairs that satisfy standing intent. An empty admissible domain remains unknown. Reachability controls establish that completion and conformance are reachable; constructed faulty variants must produce counterexamples.

`tests/test_difference_model_bridge.py` exercises the public parsers and Python assessor with concrete observations, including duplicate records, intersecting guards, repairs, and incomplete evidence.

## TLC and Python decision comparison

`DifferenceDecisionBridge.tla` records terminal decisions from the clean finite configurations without changing their transition relation. `scripts/check_difference_decisions.py` runs TLC and compares every emitted decision with the public Python assessor. Record order and multiplicity are preserved. Negative controls alter a modeled violation or terminal status to check that disagreement is detected.

## Run qualification

Requires Java, the repository's Python test environment, and the repository-pinned TLC jar. From the repository root in PowerShell:

```powershell
$env:VKIT_TLA2TOOLS_JAR = 'PATH_TO_JAR_MATCHING_REPOSITORY_PIN'
$env:PYTHONPATH = (Resolve-Path '.\src').Path
.\.venv\Scripts\python.exe scripts\check_authority.py --suite authority
.\.venv\Scripts\python.exe scripts\check_authority.py --suite differences
.\.venv\Scripts\python.exe scripts\check_difference_decisions.py --output tmp\difference-decisions
```

Java is resolved from `JAVA_HOME` or `PATH`. The authority and difference suites create isolated VKit projects and run TLC through VKit's registered-check interface. Reports, model copies, checker output, and comparison evidence are retained under `tmp/`. CI also runs the decision comparison and uploads its evidence.

## Scope

Results cover the configured finite models and the runtime cases compared with them. Hashes and Git identities are opaque atoms. The authority model does not model host authorization, human acceptance, scheduler concurrency, verifier correctness, or cryptographic collision resistance. The difference models do not cover arbitrary program inputs or longer observation sequences. Agreement with the Python assessor is bounded evidence, not a general proof that the implementation refines the model. Constructed faulty variants are qualification controls, not reports of production defects.

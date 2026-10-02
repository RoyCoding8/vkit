# Plan 10: native verification engine

Status: ready for implementation. No milestone in this plan has shipped.
Work on `main`. Implementation baseline: `e211eb2c295bc44dd2f8c279fa5d47498ada2da7`.
Read [the master plan](../MASTER-PLAN.md) and [the proposed contract extension](NEXT-CONTRACT.md) first.

## Deliver the central capability

Make vkit execute reusable, registered verifiers through its existing CLI and MCP tools. An engineer selects a check ID, reads the result, and fixes the named failure. The engineer must not reconstruct a result parser or a verification script for every task.

Every verdict must answer a precise question about a specific subject. Tests check exercised cases. Property tests check generated cases. TLC checks a model within its recorded configuration. Lean checks a formal statement. These are distinct evidence categories. None establishes arbitrary application correctness by itself.

The project owner approves the intended requirements, tests, formal statements, and model definitions. The agent can propose additions. It cannot substitute an easier statement for its current obligation. An unsupported proof obligation returns BLOCKED, with the missing capability named.

## Reuse the implemented paths

The current app already has registered commands, manifest validation, source and fixture identities, run ownership, detached execution, durable reports, readiness, approved integration policy, CLI, and six MCP tools.

The missing boundary is native interpretation of checker evidence. `execution._scenarios_from_artifact` currently accepts scenario result strings from a driver artifact. `claimkind.py` describes evidence categories but does not enforce them in the production runner. The scripts under `formal/` check fixed examples about vkit itself.

Trace these files and their callers before editing:

- `src/vkit/manifest.py`, `schemas/manifest.v1.json`, `src/vkit/schemas.py`.
- `src/vkit/execution.py`, `src/vkit/supervisor.py`, `schemas/run-report.v1.json`.
- `src/vkit/tasks.py`, `src/vkit/storage.py`, `src/vkit/claimkind.py`.
- `src/vkit/integration/policy.py`, `oracle.py`, `verify.py`, and `launcher.py`.
- `src/vkit/cli.py`, `src/vkit/mcp/_tools.py`, and console check/run views.
- `formal/run_lean.py`, `formal/run_tlc.py`, and their actual receipts.

Keep one execution and acceptance authority. Add a small `src/vkit/verifiers/` package for the genuinely different checker adapters. Use Python, existing process ownership, SQLite, and jsonschema. Do not add a scheduler, model gateway, generic plugin loader, or second verdict database.

## Freeze the data contract first

Introduce a versioned manifest with discriminated check variants. Preserve explicit legacy scenario checks, with their actual evidence category. A legacy driver claiming `theorem_checking` must be refused.

Each native check declares:

- Its ID, verifier kind, finite timeout, registered toolchain, and prerequisites.
- The subject and claim IDs, their approved files, and the required observations or theorem names.
- Every specification, oracle, configuration, dependency lock, and input that affects the claim.
- Checker options appropriate to that kind. Validate options with a closed schema, rather than accepting arbitrary shell text or an unconstrained options object.
- Its required evidence category and execution environment.

Use separate variants for `scenario`, `pytest`, `node_test`, `lean`, and `tlc`. Classify approved Hypothesis tests as `property` when their registered obligation specifies that category. Do not infer it from a package being installed.

Do not force Lean theorem names into scenario fields. Introduce an obligation representation that can hold test IDs, theorem IDs, or model properties. Migrate the affected callers together. Keep the old schema readable as legacy evidence; old records cannot satisfy a newly required native obligation.

The core writes a typed receipt from actual checker results. The candidate does not supply its own trusted verdict. Include:

- Check, claim, task generation, and run IDs.
- Category, subject digest, specification digest, approved policy digest, source inventory, and fixture digest.
- Verifier implementation identity, tool versions, dependency identities, and supported runtime.
- Checked cases, theorem names, or model properties and bounds.
- Status, assumptions, execution limits, counterexamples, and bounded artifact references.

Hashes bind evidence to bytes. They do not establish that an oracle is correct or that a candidate cannot compromise a process. Document the approved test/checker code and isolation assumptions. A candidate-controlled JSON document or success-looking stdout cannot promote evidence to a stronger category.

## Implement the adapters

### Python and Node tests

Support pytest and the built-in Node test runner first. Reuse the project's existing test files and dependencies. Have the adapter construct argv and interpret the pinned runner's structured output. Keep runner output separate from application stdout.

Require the approved test IDs to run and pass. Refuse missing output, missing IDs, duplicate contradictory results, unexpected skips, zero collected tests, truncated output, and unsupported report versions. Distinguish failed assertions from infrastructure errors. An exit code of zero alone is insufficient.

Approved property checks also record the generator settings and replay information. Report the tested scope. Do not describe generated examples as an exhaustive theorem.

Retain custom scenario drivers for browser, API, and domain checks where the existing format is useful. Identify them as approved-driver observations. Do not pretend that framework output or driver isolation proves universal behavior.

### Lean

Support project-specific challenge and solution modules, rather than a fixed vkit example. Bind the challenge, imported definitions, permitted axioms, theorem names, and toolchain to approved policy.

Provide two explicit validation profiles:

- Reviewed proof sources: kernel checking, transitive axiom audit, and fresh rechecking. The receipt names the source-review assumption.
- Unreviewed agent proofs: trusted challenge comparison, isolated candidate build, and external checking through the official comparator tooling. Missing isolation or checker capability returns BLOCKED. Never silently use the reviewed profile instead.

Default enrollment for agent-generated proofs selects the unreviewed profile. The owner must explicitly select the reviewed profile and accept its source and dependency assumptions. The agent cannot choose the weaker profile to discharge an admitted obligation.

Reject incomplete proofs, `sorryAx`, unapproved axioms, missing theorems, and altered challenge meanings. A proof about an abstract model establishes a claim about that model. A Python or JavaScript implementation requires its own explicit correspondence obligation.

Use the [Lean proof-validation guidance](https://lean-lang.org/doc/reference/latest/ValidatingProofs/). Follow the pinned [comparator contract](https://github.com/leanprover/comparator) for challenge and solution modules, theorem names, permitted axioms, and external kernels. Check actual tool help and a real example before freezing argv. Do not use a fake sandbox in an acceptance receipt.

The stronger profile can initially require Linux. Exercise it on an Ubuntu CI runner with the actual required isolation capabilities. Missing capabilities are an environment blocker, not a passed milestone. Windows process ownership is not proof-build isolation.

### TLC

Accept project model and configuration paths. Pin the JAR, model dependencies, checked properties, constants, constraints, and bounds. Reject candidate Java overrides of trusted model operators unless explicitly approved.

Interpret the pinned TLC machine output and termination status. Require successful exhaustive exploration for the recorded configuration. A simulation, a timeout, or a completion banner without the required results cannot pass. Record state counts, constraints, fingerprint configuration, and counterexamples. Preserve the checker assumptions instead of claiming a proof of arbitrary implementation behavior.

Use the [TLC implementation](https://github.com/tlaplus/tlaplus/tree/master/tlatools/org.lamport.tlatools/src/tlc2) as the authority for output codes. Generalize useful code in `formal/run_tlc.py` only after checking its existing assumptions.

## Enforce evidence at the shared boundary

Use the same adapter dispatch in foreground runs, detached runs, and integration verification. Preserve launch fencing, descendant lifetime, cancellation, and input revalidation.

Readiness must match each required claim to evidence of the correct kind, subject, specification, policy, generation, and current source. A generic PASS must not discharge an obligation requiring a theorem. Keep the existing single transaction across ownership verification, decision, and readiness recording.

Expose categories, scope, missing tools, and exact unmet obligations through `project_inspect`, `run_get`, `task_finalize`, CLI output, and existing console views. Keep the six MCP tools unless a demonstrated operation cannot fit them. Discovery must explain the available registered checks without loading skills.

Toolchain installation is an explicit setup operation. Hooks never install packages, download binaries, or run formal checks. Default runtime execution stays local. CI is the verification environment for this build; this plan does not add remote artifact import as an acceptance shortcut.

## Build in checkpoints

1. Freeze types and schemas. Add one vertical pytest check through public CLI, detached MCP, and finalization. Keep legacy scenario behavior honest.
2. Add Node and property evidence. Verify required-case accounting and display categories in all existing result views.
3. Add project-specific Lean and TLC adapters. Run actual positive and negative checker cases in Ubuntu CI.
4. Wire approved integration policy and all acceptance paths. Package the adapters and schemas, then exercise an installed wheel outside this checkout.

Each checkpoint ends with a runnable feature and a commit. Fix failures before advancing. The worker chooses routine implementation details and documents material deviations from these requirements.

## Acceptance exercises

Run these through public entry points, with literal expected outcomes:

| Exercise | Required result |
| --- | --- |
| Required Python and Node cases execute | PASS receipts list those cases |
| Required case is skipped, absent, or no tests collect | No PASS |
| Candidate writes a fake theorem PASS artifact | Cannot satisfy a Lean obligation |
| Approved theorem has a valid proof | Correct theorem, challenge, and profile recorded |
| Proof weakens the theorem, hides `sorry` in a dependency, or adds an axiom | No PASS |
| Unreviewed profile lacks its real isolation or external checker | BLOCKED without downgrade |
| Finite model satisfies its required properties | PASS records exact model configuration and checker assumptions |
| Model violates a property or run stops before completion | FAIL with counterexample, or BLOCKED for incomplete execution |
| Source, specification, toolchain, or policy changes after a run | Old evidence cannot finalize the new obligation |
| Required claim is removed or replaced during an admitted task | Pinned obligation remains required |
| Cancellation, supervisor restart, or ownership loss | Existing lifecycle guarantees remain effective |
| Installed package runs a project-specific formal example | No source-checkout resource dependency |

Keep local checks small. Run the full suite and real formal tools in GitHub Ubuntu CI, plus the existing OS matrix where applicable. Use Python test drivers. Do not use WSL or run the full suite locally.

## Handback

Provide the final commit, changed interfaces, exact focused commands, CI links with tested SHAs, receipts, observed negative cases, and open limits. Distinguish adapter implementation from a real checker exercise. Keep unresolved stronger-profile capability explicit.

Follow the delegation policy in [the master plan](../MASTER-PLAN.md#skills-and-parallel-work). Do not implement cleanup or a new dashboard inside this plan except for the existing views needed to render native evidence. Record checkpoint evidence in the master plan.

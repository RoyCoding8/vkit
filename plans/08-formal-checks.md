# Plan 08: add narrowly scoped formal checks

Read [CONTRACT.md](CONTRACT.md). Optional for the first release. Ready after Plan 02's state and ownership operations are stable. This plan defines the properties; the engineer implements them rather than choosing an open-ended research agenda.

## Outcome

The app can retain formal-check evidence without overstating what was proved. The first useful case is ownership and stale-result rejection. Ordinary implementation tests remain required.

Two independent deliverables follow. Deliverable A is recommended. Deliverable B is an optional Lean integration example and must be labeled as such.

## A. TLA+ model of ownership and acceptance

Model these finite domains initially: two resources, two owners, two attempt generations per owner, two required checks, and two source/policy revision identities. Record the exact finite configuration with each run.

State includes current generation per owner, resource assignment to an owner/generation or free, known run evidence with generation/source/policy/check/outcome, current task contract identity, and accepted task decisions. A crashed owner's claim remains held until explicit reconciliation.

Actions:

- Acquire a free resource for the current attempt.
- Record a check result for an attempt and source/policy identity.
- Release an owned resource after confirmed cleanup.
- Crash an owner without silently releasing its resource.
- Reconcile a stopped owner and advance its attempt generation.
- Change the candidate or approved policy, invalidating old readiness.
- Submit an acceptance decision using the implementation's documented conditions.

Check these safety properties:

1. An exclusive resource has at most one accepted owner generation.
2. A superseded generation cannot create a new accepted decision.
3. Acceptance requires every required check, with PASS and matching attempt/source/policy.
4. A candidate or policy change invalidates prior readiness for the new identity.
5. Crashing alone does not transfer ownership to a competing live attempt.

Do not claim liveness unless explicit fairness and termination assumptions are modeled. A finite successful TLC run establishes the checked properties for that model/configuration, not for arbitrary numbers of workers or all Python execution.

Use the existing TLC tool, not a custom model checker. Store tool version, model/configuration hashes, explored state counts, completion status, and counterexample traces. Incomplete exploration or exhausted resources is BLOCKED.

For correspondence, map each modeled action to a public core operation. Preserve trace examples as executable tests against Plan 02. Use Hypothesis operation sequences to compare the actual core with a separate small reference model. This is evidence of correspondence, not a proof of implementation equivalence.

## B. Optional Lean policy example

Define finite required check IDs and evidence records with check ID, attempt generation, source revision, policy revision, and pass/fail outcome. Implement a pure Lean decision function that accepts exactly when all required checks have matching passing evidence for the current identities and no required check is absent. State an independent logical specification using universal quantification over required checks.

Prove both directions between the executable decision and that specification. Include nonempty required-check requirements and duplicate/conflicting evidence policy explicitly. Use the same frozen example records in the Python acceptance evaluator and the Lean executable to countercheck decisions. Do not claim these examples prove Python/Lean equivalence.

Reject `sorryAx`, unapproved custom axioms, weakened statements, and unchecked compilation paths. Record the theorem, assumptions, dependency versions, source identity, and axiom audit. Missing Lean is BLOCKED only when this check is selected as required; it must not make ordinary users install Lean.

The planner has specified a bounded policy example, not formal verification of every application. If the real acceptance contract changes, update the specification and theorem together through policy review.

## App integration

Use registered check commands and the existing check artifact format. Add focused TLC/Lean adapters only for tools actually exercised. Preserve a claim category distinguishing scenario testing, property testing, finite model checking, and theorem checking. A generic PASS summary must retain this scope.

Formal tooling is opt-in during setup or repository enrollment. Do not download a toolchain from a hook or ordinary MCP check call. Keep executable verification commands pinned and reproducible.

## Acceptance

- The intended TLC model completes and reports its finite scope.
- A mutation removing the stale-generation guard yields a counterexample.
- A mutation allowing acceptance with one missing check yields a counterexample.
- At least one counterexample action trace reproduces a failing assertion against a deliberately broken implementation fixture.
- Timeout/incomplete exploration produces BLOCKED and retains diagnostics.
- If Lean is included, the theorem checks; a false acceptance example is rejected; adding `sorry` is detected; weakening the required-check specification changes policy identity and invalidates previous evidence.
- Ordinary verification still works without optional formal tools installed.

## Finish and handoff

Report exactly which model, theorem, bounds, assumptions, and correspondence checks ran. Keep this plan optional in STATUS.md when skipped. Release notes must not call the whole app "formally verified" based on this milestone.

Primary references: [TLC](https://docs.tlapl.us/using:tlc:start), [Lean proof validation](https://lean-lang.org/doc/reference/latest/ValidatingProofs/), and [Hypothesis stateful tests](https://hypothesis.readthedocs.io/en/latest/stateful.html).

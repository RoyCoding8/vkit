# Verification infrastructure refactor

## Session status

| Item | Status | Result |
| --- | --- | --- |
| Implementation | Complete | VKit 0.3.0 uses one job and evidence path through CLI and MCP. |
| Utility catalog | Removed | Roughly 42,000 net lines removed. Git history retains the old utilities. |
| Local verification | Passed | 168 tests, 13 installed-wheel tests, 33 example checks, and 6 real formal controls. |
| Independent review | Complete | Luna reviewers' concrete findings fixed. No remaining blocker found in the final review. |
| Remote CI | Not run | Required core and formal jobs configured. |
| Git delivery | Uncommitted | No commit, push, or PR created. |
| Agent worktrees | Archived | Three recoverable snapshots retained. |

## Architecture decisions

| Area | Decision | Effect |
| --- | --- | --- |
| Utilities | Delete the catalog, helper projects, exclusive dependencies, tests, and documentation. | No second tool catalog to maintain or advertise. |
| Existing infrastructure | Extend the runner, store, manifest, and adapter interface. | Preserve useful components without a parallel execution framework. |
| Native checks | Retain Lean, TLC, scenario, pytest, property, Node, and static-analysis adapters. | Keep each check's evidence category and limits explicit. |
| External backends | Add one producer/checker protocol. | New verifiers require no CLI, MCP, or console registry changes. |
| Job identity | Bind the accepted definition, frozen inputs, declared tool identities, checker policy, baseline, and version. | A changed claim or environment cannot reuse a fresh passing result. |
| Attempts | Give every execution a separate `run_id` under its content-derived `job_id`. | Retry preserves job identity when the inputs and definition stay the same. |
| Execution | Use independent local workers with per-attempt OS locks. | Jobs survive client exit. Duplicate workers cannot execute the same attempt. |
| Evidence | Retain artifacts and hash-linked receipts. | Gates check integrity and current identity before accepting a result. |
| Replay | Launch the accepted checker against captured evidence. | A stored success flag never substitutes for checking. |
| Scope | Use local workers and explicit retries. | No distributed scheduler or generic prover checkpoint engine. |

## Completed tasks

- [x] Inspect the implementation, read the research, compare designs, and record the plan before changes.
- [x] Capture the baseline and remove the utility catalog before adding infrastructure.
- [x] Define external backend, immutable job, evidence bundle, and checker receipt contracts.
- [x] Freeze inputs and bind evidence to accepted definitions, checker identity, and declared dependencies.
- [x] Replace MCP-owned threads with persistent submissions and independent workers.
- [x] Expose submission, inspection, waiting, cancellation, retry, export, replay, and gates through CLI and MCP.
- [x] Add required real Lean and TLC controls to CI. Update examples and backend documentation.
- [x] Run the retained suite, adversarial evidence checks, installed-wheel tests, and independent review.
- [x] Structure the session report around status, decisions, evidence, and limits.
- [x] Restructure all 14 project Markdown files and validate local links, code fences, and whitespace.

## Verification results

These are the results recorded during this implementation session. Remote CI remains unverified.

| Check | Result | Evidence |
| --- | --- | --- |
| Retained baseline | 57 passed, 3 skipped, 2 console connection failures | [Baseline log](.audit/baseline-tests.txt) |
| Final full suite | 168 passed, 4 POSIX-only skips on Windows | [Final suite log](.audit/final-suite.txt) |
| Installed 0.3.0 wheel | 13 passed | [Installed-package log](.audit/wheel-tests.txt) |
| Wheel contents | Schemas present. Retired utility modules absent. | [Build log](.audit/build-final.txt) and wheel inspection |
| Repository examples | 33 of 33 workflow checks matched | [Example log](.audit/e2e-final.txt) |
| Real formal controls | All 6 matched their expected outcomes | [Formal log](.audit/formal-final.txt) |
| Source checks | Pinned Ruff, JavaScript syntax, offline lock check, and diff whitespace check passed | Commands below |
| Independent Luna review | No remaining concrete blocker found after fixes | [Decision trail](.audit/verification-refactor.tsv) |
| Documentation structure | All 14 Markdown files use tables and scannable sections. Local links, code fences, and whitespace checks passed. | README, guides, examples, tools, plugin skills, and this report. |

### Formal outcomes

| Case | Execution result | Claim outcome | Assurance |
| --- | --- | --- | --- |
| Lean proved theorem | PASS | Established | Kernel proof |
| Lean evidence export and replay | PASS | Established | Kernel proof |
| Lean unfinished theorem | BLOCKED | Unknown | Kernel proof |
| Lean unapproved axiom | FAIL | Unknown | Kernel proof |
| TLC exhaustive invariant check | PASS | Established for the finite configuration | Finite model |
| TLC counterexample | FAIL | Refuted for the finite configuration | Finite model |

Assurance identifies the checking method. A failed or blocked check establishes no proof.

### Integrity and execution checks

| Requirement | Verification |
| --- | --- |
| Removed utilities cannot be discovered, invoked, or packaged. | Discovery tests and installed-wheel inspection. |
| A job survives its submitting client. | Detached CLI and MCP tests. |
| One worker owns an attempt. Cancellation stops descendants. | Duplicate-worker and cancellation tests. |
| Dead queued workers become interrupted. | Regression using a real exited child process on Windows. |
| Changed inputs, definitions, checkers, dependencies, artifacts, or receipts invalidate passing evidence. | Identity, tampering, and gate tests. |
| Linked paths cannot redirect retained evidence. | Symlink and Windows reparse-point checks. |
| Replay performs fresh checking. | External checker replay and real native Lean replay. |
| Invalid proofs and infrastructure failures do not become mathematical refutations. | Outcome tests and real Lean rejection controls. |
| Missing formal tools fail the required CI job. | Formal script raises an error instead of skipping. |

### Source check commands

```sh
uvx ruff@0.14.0 check --isolated --select E4,E7,E9,F src tests scripts
node --check src/vkit/console.js
uv lock --check --offline
git diff --check
```

## Files to inspect

| Question | File |
| --- | --- |
| How does a backend plug in? | [Backend protocol](docs/backend-protocol.md) |
| Who owns each part of the system? | [Architecture](docs/architecture.md) |
| What does each evidence category establish? | [Verification semantics](docs/verification.md) |
| How do jobs submit, wait, and replay? | [jobs.py](src/vkit/jobs.py) |
| How are receipts sealed and checked? | [evidence.py](src/vkit/evidence.py) |
| How are external producers and checkers separated? | [external.py](src/vkit/verifiers/external.py) |
| What does required CI execute? | [CI workflow](.github/workflows/ci.yml) and [formal controls](scripts/verify_formal.py) |
| Why were implementation decisions made? | [Local decision trail](.audit/verification-refactor.tsv) |

## Limits and pending work

| Item | Current boundary |
| --- | --- |
| Mathematical guarantee | Applies to the accepted statement, assumptions, and verifier semantics. Tests remain empirical. |
| TLC scope | Applies to the checked finite configuration. |
| Checker trust | Accepted checker implementations and their declared environment remain trusted. |
| Hashes | Detect changes. They do not authenticate writers or provide a filesystem sandbox. |
| Replay portability | Requires matching accepted definitions and tool identities, including resolved paths. |
| Remote CI | Configured but not run during this session. |
| Git delivery | Changes remain in the working tree. |

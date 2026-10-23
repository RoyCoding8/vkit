# Verification semantics

## Core terms

| Term | Meaning |
| --- | --- |
| Check | Accepted entry in `verification/manifest.json`, schema version 2. Acceptance binds the definition and current checker identity. |
| Job | Content-derived identity that also binds frozen declared inputs, the baseline, and VKit's validation policy. |
| Run | Unique execution attempt for a job. |

## Read each result dimension separately

| Dimension | Values and meaning |
| --- | --- |
| Execution | `queued`, `running`, `done`, `interrupted`. |
| Check outcome | `PASS`: required evidence accepted. `FAIL`: check policy failed. `BLOCKED`: no valid decision. |
| Claim outcome | `established`, `refuted`, or `unknown`, within the recorded model and assumptions. |
| Assurance | `empirical`, `finite_model`, `kernel_proof`, or external `trusted_engine`. |
| Freshness | `fresh_pass`, `fresh_fail`, `fresh_blocked`, `stale`, `missing`, `not_approved`. |

| Example | Check outcome | Claim outcome |
| --- | --- | --- |
| Rejected Lean proof | `FAIL` | `unknown`. The theorem has not been refuted. |
| Real TLC counterexample | `FAIL` | `refuted` in the finite model. |
| External rejection or uncertainty | `BLOCKED` | `unknown`. |

## Gate decisions

| Gate | Condition |
| --- | --- |
| `READY` | All registered checks have intact, fresh passing evidence. |
| `REJECTED` | A current check failed its policy. |
| `BLOCKED` | Any other state. |

A READY gate preserves each check's evidence category. It does not promote tests into proofs.

## Supported checks

| Kind | Evidence and scope |
| --- | --- |
| `scenario` | Named observations in a structured artifact, with optional measurement budgets. |
| `pytest`, `node_test` | Reports for every required test. |
| `property` | Sampled Hypothesis tests with accepted generator settings. |
| `static` | SARIF findings compared with an accepted baseline. |
| `lean` | Kernel-checked theorems and an axiom audit. Supports reviewed-source and unreviewed-comparator profiles. |
| `tlc` | Exhaustive finite-model checking at recorded bounds and configuration. |
| `external` | Pinned producer and independently pinned checker with a claim-bound receipt. |

See the [backend protocol](backend-protocol.md) for extension. The installed package excludes the retired utility catalog and build check.

## Declare the input closure

| Rule | Behavior |
| --- | --- |
| `inputs` selectors | Files, directories, or globs from tracked and untracked nonignored source. |
| Empty `inputs` | Selects the whole nonignored tree. |
| Secret-looking paths | Excluded. |
| Content identity | Normalizes CRLF to LF. |
| Capture | Copies selected bytes before execution and verifies copied digests. |
| Missing patterns | Block execution. |
| External specification | Included automatically. |

Declare every required source, specification, model, configuration, and dependency.

| Outside the snapshot by default | How to account for it |
| --- | --- |
| Ignored files | Capture or pin separately when required. |
| Remote services and environment state | Capture or pin separately when possible. |
| Runtime libraries | Declare trusted dependencies. Native runtime libraries remain environment assumptions. |
| Arbitrary code dependencies | Declare the closure. VKit does not infer it. |

## Freshness and acceptance

| Event | Effect |
| --- | --- |
| Selected content, accepted policy, checker identity, or baseline changes | Existing evidence no longer satisfies the current gate. |
| Checker changes | Requires renewed acceptance. |
| Source changes during execution | Blocks publication as passing evidence. |
| Native Lean unreviewed profile | Also binds the challenge digest. The comparator checks the solution against accepted statements. |

## Run lifecycle

| Command or event | Behavior |
| --- | --- |
| `check run` | Submit and wait. |
| `check run --detach` | Return after capture and worker launch. |
| `run show`, `run wait`, `run cancel`, `run retry` | Operate independently of the submitting client. |
| Cancellation or deadline | Stop descendants through a Windows Job Object or POSIX process group. |
| Worker loss | Report interrupted execution. Never a pass. |
| Explicit submission | Creates a separate attempt. Identical content retains the job ID. |

## Retained evidence

| Attempt type | Retained files |
| --- | --- |
| Finished attempt | `job.json`, `record.json`, `snapshot/`, reports, logs, and `receipt.json`. |
| External attempt | Also `evidence/`, checker requests, and checker receipts. |

The integrity chain is:

1. Evidence pointer hashes the final record.
2. Record hashes the receipt.
3. Receipt hashes retained artifacts and binds input closure and backend identity.

## Export and replay

| Operation | Requirements and result |
| --- | --- |
| `evidence export` | Refuses incomplete or damaged evidence. |
| `evidence replay` | Requires matching current source, accepted definition, and checker identity. Captures the bundle and launches a fresh checker. |
| Replay output | New attempt and receipt. |
| Portability limit | Resolved tool identity and paths must match. Relocating a checker changes its identity. |

## Trust and scope

| Item | Boundary |
| --- | --- |
| Assurance labels | Describe the accepted checker contract. They do not establish that arbitrary code is a sound proof kernel. |
| Acceptance review | Review the checker, specification, and dependencies. |
| File hashes | Establish integrity relative to trusted local state. They are not signatures. |
| Installation and store | Protect them from untrusted writers. |
| Worker containment | Controls the process tree. It is not a filesystem sandbox. |
| Formal proof | Covers the accepted statement under recorded assumptions. |
| Model checking | Covers the declared finite configuration. |
| Unestablished properties | Unmodeled implementation behavior, source-to-binary correspondence, termination, and correctness of undeclared dependencies. |

## CI integration

1. Run `check run` for the required checks.
2. Export and archive evidence bundles.
3. Use `gate --json` for the strict decision against current inputs.

| Exit code | Meaning |
| --- | --- |
| `0` | PASS or READY. |
| `1` | FAIL or REJECTED. |
| `2` | Invalid request. |
| `3` | BLOCKED. |
| `4` | Internal error. |
| `5` | Unavailable interface. |

A bounded `run wait` timeout returns BLOCKED and leaves the worker running.

| Required formal CI control | Purpose |
| --- | --- |
| Real Lean proof | Accept a kernel-checked theorem. |
| Lean `sorry` and disallowed axiom | Reject unfinished or unapproved proof assumptions. |
| Exhaustive TLC check | Establish the property within the finite configuration. |
| TLC counterexample | Refute the property within that configuration. |
| Missing toolchain | Fail the script rather than skip. |

A local core test pass alone does not establish formal backend execution.

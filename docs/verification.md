# Verification semantics

## Core terms

| Term | Meaning |
| --- | --- |
| Check | Registered entry in `verification/manifest.json`, schema version 3. Acceptance binds its definition and checker identity. |
| Formal component | Candidate source and six API bindings checked against a fixed owned package and accepted policy. |
| Job | Content-derived identity binding the subject, frozen source, accepted policy/checker and any baseline. |
| Run | Unique execution attempt for a job. |

## Read each result dimension separately

| Dimension | Values and meaning |
| --- | --- |
| Execution | `queued`, `running`, `done`, `interrupted`. |
| Check outcome | `PASS`: required evidence accepted. `FAIL`: check policy failed. `BLOCKED`: no valid decision. |
| Claim outcome | `established`, `refuted`, or `unknown`, within the recorded model and assumptions. |
| Assurance | `empirical`, `finite_model`, `kernel_proof`, or `trusted_engine`, according to the method. |
| Freshness | `fresh_pass`, `fresh_fail`, `fresh_blocked`, `stale`, `missing`, `not_approved`. |

| Example | Check outcome | Claim outcome |
| --- | --- | --- |
| Rejected Lean proof | `FAIL` | `unknown`. The theorem has not been refuted. |
| Real TLC counterexample | `FAIL` | `refuted` in the finite model. |
| External rejection or uncertainty | `BLOCKED` | `unknown`. |
| Rejected Verus obligation | `BLOCKED` | `unknown`. No validated source counterexample is claimed. |

## Gate decisions

| Gate | Condition |
| --- | --- |
| `READY` | All registered checks and required formal components have intact passing evidence for the intended source. |
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

## Owned formal package

| Field | Initial contract |
| --- | --- |
| Package | `vkit.resource-ledger`, containing one generic `Ledger<N>` model and fixed expected contracts. |
| Claim | `ledger.source.v1`, method `verus_source`, assurance `trusted_engine`, scope `required_api`. |
| Required bindings | `new`, `reserve`, `release`, `reserve_batch`, `apply_command`, `run_commands`. |
| Properties | Map accounting, capacity, owner checks, exact error/result behavior, atomic batches and command traces. |
| Procedure | Fresh model verification/export, candidate verification/export, unchanged expected-client import/check. |
| Limits | No whole-application proof, kernel certificate, binary correspondence or operational resource bound. Resource aborts remain possible. |

The expected client prevents a candidate from satisfying the request by weakening its own contract. VKit supplies the model and required statements; the agent supplies an implementation and binding names. Application-to-model correspondence still needs its own evidence.

## Declare the input closure

The following selectors apply to registered local checks. Formal working-tree capture instead retains the complete nonignored tree as raw bytes; Git capture retains immutable commit blobs and their Git identities. Formal capture rejects secret-looking paths, links, submodules and unsupported files rather than silently omitting them. A CRLF-only edit changes a formal source identity.

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
| Source changes during execution | Registered local freshness changes. Formal execution continues against frozen bytes; a moved checkout cannot rewrite its committed subject. |
| Native Lean unreviewed profile | Also binds the challenge digest. The comparator checks the solution against accepted statements. |

## Run lifecycle

| Command or event | Behavior |
| --- | --- |
| `check run` | Submit and wait. |
| `check run --detach` | Return after capture and worker launch. |
| `verify COMPONENT --candidate REF --wait 0` | Capture an immutable Git subject and launch a formal worker. |
| `verify COMPONENT --working-tree --wait 0` | Capture provisional raw source and launch a formal worker. |
| `run show`, `run wait`, `run cancel`, `run retry` | Operate independently of the submitting client. |
| Cancellation or deadline | Stop descendants through a Windows Job Object or POSIX process group. |
| Worker loss | Report interrupted execution. Never a pass. |
| Formal cancellation/recovery | Stop or reconcile only the attempt's exact owned Docker container. |
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
| Portability limit | Accepted tool identities must match. Formal identities exclude host paths; registered adapters retain their own path semantics. |

## Trust and scope

| Item | Boundary |
| --- | --- |
| Assurance labels | Describe the accepted checker contract. They do not establish that arbitrary code is a sound proof kernel. |
| Acceptance review | Review the checker, specification, and dependencies. |
| File hashes | Establish integrity relative to trusted local state. They are not signatures. |
| Installation and store | Protect them from untrusted writers. |
| Worker containment | Local registered checks control the process tree. Formal and protected-CI commands use restricted containers. |
| Formal proof | Covers the accepted statement under recorded assumptions. |
| Model checking | Covers the declared finite configuration. |
| Unestablished properties | Unmodeled implementation behavior, source-to-binary correspondence, termination, and correctness of undeclared dependencies. |

## CI integration

1. Run `check run` for the required checks.
2. Export and archive evidence bundles.
3. Use `gate --json` for the strict decision against current inputs.

For protected committed checking, use `gate --candidate FULL_OID --target FULL_OID --output NEW_PRIVATE_DIR --json`. VKit loads requirements from target policy, captures the exact candidate, runs fresh jobs and seals receipts outside candidate execution. It does not combine branch receipts or trust candidate policy deletion. The [host workflow example](../examples/protected-ci/README.md) describes required authority protection; this repository has not configured enforced branch protection.

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

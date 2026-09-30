# Repair handoff

Read `AUDIT.md` and the probe receipts before editing. The goal is a kit whose acceptance and ownership rules are shared across CLI, MCP, hooks and setup. Preserve useful execution/storage code and old reports. Stop feature expansion until the milestones below pass.

The engineer owns implementation choices within these invariants. Do not add another scheduler, generic provider layer, registry or mirrored receipt store. Fix the earliest incorrect authority and migrate its callers in the same milestone. Treat an unresolved measurement as unresolved.

## R1 — One contract, admission and readiness authority

Own `tasks`, the necessary storage migration, policy/fixture identity and every caller of admission/finalization.

1. Establish a validated contract with a real repository/checkout binding, generation, approved policy, nonempty mandatory checks/scenarios, declared scope and required resources. Pin the approved floor; caller selections only add requirements.
2. Acquire required claims atomically with admission. A conflict cannot return `admitted: true`. Recheck ownership/generation before launch and before acceptance. Reuse the existing transactional claims implementation.
3. Replace delimiter policy hashing with canonical structured hashing. Preserve argv boundaries, normalize cwd relative to repository, include operative inputs/prerequisites and measure required fixture identities. Preserve schema/version provenance during migration.
4. Acceptance reads the actual tested identities and compares them with the current acceptance context. Source/policy/fixture changes invalidate old evidence. Missing/malformed policy blocks; zero-check tasks cannot be accepted.
5. Preserve previous attempts and outcomes as history. A fresh repaired attempt may satisfy its current contract. Return history/instability separately from gaps that block the current attempt; do not erase failures to achieve readiness.
6. Migrate CLI, MCP and hooks to these same core functions. Remove obsolete adapter-owned rules. Console enrollment must use the existing core proposal/accept/read implementation, and execution must enforce that approved policy condition.

Gate: turn the counterexamples in `probe_worker.py`, `probe_baseline.py` and `probe_identity.py` into concise public-behavior regressions. Check parity across CLI/MCP/hook completion; repaired attempt READY, changed identity BLOCKED, absent policy refused/BLOCKED, missing mandatory check BLOCKED, conflicting resource not launched. Fix the correspondence suite against the intended invariant, not by changing its expected answer to match the defect.

## R2 — Durable process ownership and safe recovery

Depends on R1's contract/run identities. Own execution, process launcher, supervisor, recovery and run-start/status/cancel callers.

1. Implement the planned per-run supervisor. Register launch intent and task/generation before spawning. Persist process identity and OS ownership while the process is running, then return a durable run ID promptly.
2. Wire CLI and MCP through that single path. Retain an actual Windows Job Object ownership mechanism that another supported cancellation client can address; create/persist it before resuming the child. A fabricated job name is not ownership.
3. Make status/cancel and request replay work from another client, including client disconnect, parent crash, late startup and cancellation races. Unknown launch/ownership must retain claims for explicit reconciliation. Do not infer that a missing PID proves no process exists.
4. Do not transfer/release resources until participating descendants are confirmed stopped under the supported containment profile. Retain safe refusal where the OS/backend cannot establish that fact.
5. Publish a precise POSIX capability profile. Process groups do not contain `setsid` escape. Keep that explicit limit; consider a delegated cgroup backend only when a measured target environment requires it. A pidfd solves individual process identity, not tree containment.

Gate: finite real child/grandchild test; prompt returned ID while running; second-client cancellation; client disconnect; launch crash before identity publication; supersede/release while child alive must refuse; no second owner can write concurrently. Repeat each mutation request without duplicate launches or lost receipts. Use unrelated control processes to prove cancellation does not hit the wrong owner.

## R3 — Trusted integration oracle

Own the integration boundary and its acceptance record. Pin the check implementation and expected observations as well as the manifest. Execute the candidate application under the trusted driver/runtime. A candidate edit to driver/expectations must not become an approved oracle merely by retaining the same argv and scenario IDs. Use the existing trusted launcher and checkout handling; extend that boundary rather than adding a second verification framework.

Identify the executed verifier and fixtures; fix the source-install revision lookup and actual fixture digests. Separate request replay from fresh execution. A duplicate receipt must return its authoritative stored result or report a real conflict with both observations retained; it cannot claim reuse while returning a different decision.

Gate: broken product plus forged candidate PASS driver must fail/refuse; changed expected values must require explicit policy review; ordinary good candidate accepted; semantic branch interference rejected; moved target invalidates the previous combination; returned/stored replay decisions agree. Use `probe_integration.py` and the duplicate receipt case in `probe_identity.py` as starting counterexamples. Record the actual trusted driver/verifier and tested candidate bytes. Local acceptance receipts remain evidence; protected CI independently executes the checks.

## R4 — Console boundary and installable distribution

Can proceed alongside R3 after R1 core interfaces freeze. Console and packaging may have separate owners with disjoint files.

1. Make GET read-only. Validate Host/Origin and require a session-bound mutation token. Invalid content length/JSON/size must terminate handling before dispatch. Test with actual HTTP requests and a harmless mutation witness.
2. Package the existing plugin, marketplace and hook/skill resources in the wheel. Use package resources; setup must work outside a source checkout. Resolve the installed runtime and project configuration explicitly rather than guessing from PATH.
3. Share enrollment/status with the R1 core and make host task correlation deterministic for main and subagent sessions. Do not let a session-only event select an arbitrary child task.
4. Keep installation/removal idempotent and preserve unrelated host configuration, product source and evidence. Installation success is distinct from runtime attachment and tool usability.

Gate: built wheel in a fresh isolated environment; spaces/non-ASCII path; install/inspect/repair/remove round trip; configuration preserved; foreign mutation refused; oversized body cannot invoke a mutation; console/core enrollment parity. Use a live host only for the attachment/blocked-turn checks that need it, with actual receipts and model identity.

## R5 — Evidence and release accounting

Run after accepted behavior is stable. Reconcile the original matrix in `tmp/research/KIT_ACCEPTANCE.md` with implementation milestones. Keep exact commands, revision/environment, platform, skips, failures and artifact references for each advertised guarantee. Passing a documentation-presence test is not proof of the behavior named in that documentation.

Refresh formal receipts only for the exact model/config bytes; record their hashes and bounds. Include implementation correspondence through production paths for the invariants actually claimed. Do not advertise a Lean/TLA proof as Python correctness. A required proof that did not run is BLOCKED; optional proof absence is reported clearly.

Gate: the appropriate repaired suites pass; mandatory platform gaps accounted for; fresh Claude session and subagent receive correct bindings and skill pointers; real blocked completion turn verified; hosted protected CI/pilot evidence available before claiming those features released. A 100-worker pilot follows lower-count ownership/cancellation gates and measures contention, accepted outcomes and retained uncertainty. It is not the first test of correctness.

## Parallel work policy

- One engineer owns the core identity/admission boundary in R1. Freeze its contract and examples before independent callers change.
- Use `git-worktree-discipline` for simultaneous code writers with disjoint ownership and isolated scratch state/databases. The integrator alone changes shared interfaces, migrations, lockfiles and final status.
- Use `swarm` for independent read-only coverage or bounded checks after the scope is defined. Each finding needs a concrete counterexample and an evidence path. Do not let reviewers vote a result into correctness.
- Load `architect` for boundary decisions; `principle-prove-it-works` and `principle-test-behavior-not-implementation` for milestone gates; `blast-radius` when a shared API changes. Apply `unslop` and ponytail to keep repairs small. The engineer can load another relevant skill when it solves an actual problem.
- Merge serially. After each merge, rerun the affected public-path gates at the combined tip. Preserve rejected findings with their reason and unresolved findings with their limit. Do not begin another wave to conceal a failing gate.

Each milestone handoff contains: changed behavior, removed obsolete authority, exact checks/receipts, remaining limits, and the next permitted milestone. Model selection remains the operator's decision; the kit must not depend on a particular coding model.

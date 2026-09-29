# Plan 02: own tasks, resources, and background runs

Read [CONTRACT.md](CONTRACT.md). Ready after Plan 01 passes. Inspect its actual execution and storage code before changing it.

**Delegation.** Same standing authority as Plan 01: bounded parallel subagents,
disjoint file ownership, separate worktrees, owner merges serially in the main
checkout. The storage migration and lifecycle code have one owner because they
are the shared contract the other workstreams code against.

## Owner decisions carried into this plan

- **Host scope.** Claude Code only in the first release. Host-specific code
  belongs behind an adapter so a later host is an addition, not a rewrite, but
  no abstraction is built for a host that does not exist yet.
- **Writes go to existing operations only.** A later UI or MCP tool may add no
  mutation that this plan does not already own. See
  [05-setup-app.md](05-setup-app.md) for the writable surface, which is
  deliberately closed.

## Outcome

Several participating clients can share a project without claiming the same exclusive resource. A check can outlive its initiating request, be queried or cancelled, and recover honestly after interruption. An old attempt cannot submit accepted evidence after reassignment.

Use one owner for storage migrations and lifecycle code. Plan 06's example work can proceed separately after scope assignment. Relevant skills are `poteto-mode`, `principle-separate-before-serializing-shared-state`, `principle-make-operations-idempotent`, and `principle-prove-it-works`.

## Public operations

```text
vkit task begin --project <repo> --contract <file> --request-id <id> --json
vkit check start --project <repo> --task <id> --check <id> --request-id <id> --json
vkit run show --project <repo> --run <id> --json
vkit run cancel --project <repo> --run <id> --request-id <id> --json
vkit task finalize --project <repo> --task <id> --json
vkit recover --project <repo> --json
```

`recover` inspects by default. Applying a recovery action must name the affected attempt/resource and the evidence permitting the change. The worker can choose the exact explicit apply flags, document them, and test them. Do not infer abandonment solely from elapsed time.

## Implement

1. Extend storage through versioned migrations that preserve Plan 01 runs. Pin the task contract and manifest policy digest when opening an attempt. Validate all IDs, ownership generations, source roots, required check selections, and resource keys at boundaries.
2. Claim an attempt's declared resources atomically. Use database uniqueness for exclusive resources and a transaction for bounded capacity. Claims across several resources must succeed together or leave none acquired. Acquire only named resources the task actually needs; do not lock the entire repository for every check.
3. Give each writable checkout an exclusive ownership key resolved through Git. Different linked worktrees share the project's claims database. Represent test capacity separately from writer capacity. Read-only code access does not consume a writer slot, but builds and app launches still reserve runtime resources.
4. Add a per-run supervisor which invokes Plan 01's execution code and owns the process tree. Persist launch intent before launch and attach verified process identity afterward. Handle the crash window between them conservatively. A new client can query the run even after the initiating CLI or MCP process exits.
5. Make start/cancel retries idempotent. Same request ID and same payload returns the existing operation. A conflicting payload is rejected. Store these identities for the task/run lifetime rather than expiring them while a retry can still duplicate execution.
6. Cancellation first records intent, then signals the verified owner, then confirms termination and publishes BLOCKED/cancelled. If termination is uncertain, retain claims and report recovery needed. PID reuse must not kill an unrelated process.
7. Implement attempt supersession. Reassignment advances generation only after safe resource reconciliation. Finalization checks the current generation again in the transaction that records readiness. Old owners cannot publish accepted results.
8. Compute finalization from all required check results, source/policy identity, owned scope, resources, and gaps. Missing evidence returns BLOCKED, observed defects return REJECTED, and satisfied local requirements return READY. None of these authorize a merge.

There is no host task scheduler here. Clients retry admission deliberately after capacity becomes available; do not add automatic spawning or a busy retry loop. Recovery must inspect the records and processes actually present.

## State model to implement

Keep the model small: an attempt is active, paused, superseded, or closed. Runs have the lifecycle and outcomes in CONTRACT.md. A resource refers to one valid owner generation or is free. Paused does not mean resources are safe to release. A closed task retains its reports.

Use a simple reference model in Hypothesis stateful tests for claim, start, cancel, release, supersede, and finalize. Assert observable ownership and readiness, not private function call order.

## Acceptance

| Exercise | Required outcome |
| --- | --- |
| 100 competing claim attempts across multiple OS processes | Exactly one owner for an exclusive resource; explicit conflicts for the rest |
| Two disjoint resource sets | Both owners can progress |
| Multi-resource conflict | No partial claim survives the failed acquisition |
| Transaction owner killed | Database remains usable and no partial accepted claim appears |
| Start retried before/after client disconnect | One execution and the same run identity |
| MCP-like parent process exits | Supervisor continues or reports a documented blocked launch; no fictitious success |
| Cancel repeated or races with completion | One coherent terminal outcome; no unrelated process killed |
| Supervisor dies | Descendants handled within the tested containment boundary; uncertain claims stay reserved |
| Old owner submits after supersession | Rejected even if its previous check passed |
| Changed contract or policy | Old runs cannot satisfy the new requirements automatically |
| All checks pass but one required check is absent | BLOCKED |
| Client requests fewer checks than approved policy requires | Mandatory baseline remains required; no READY from the smaller selection |
| Disk/locking failure | No fabricated READY; state and artifacts remain diagnosable |
| Stateful operation sequences | No duplicate ownership or stale-attempt acceptance |

Include the initial SQLite probe only as background. Rerun these tests against the implemented app, including separate processes and actual crash injection. Thread-only tests do not establish cross-process coordination.

## Finish and handoff

Return the migration behavior, operation schemas, concrete crash/recovery transcripts, claim contention results, and supported process semantics. Record limits on test processes separately from future AI-agent limits. Plan 03 should only need to expose these operations, not invent run lifetime or ownership rules.

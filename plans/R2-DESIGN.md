# R2 decision record: durable process ownership and safe recovery

R2 of `review/REPAIR_PLAN.md` (F05, F13). Implemented and merged. This file is
what survives of the R2 design: the decisions, the findings that changed them,
and the defaults an implementer proceeded on. The implementation itself is the
specification. Where the two used to disagree, this file records the correction
rather than the original claim.

## 1. The defect and the invariant that replaced it

`run_check` registered the run row, launched, waited for completion, then wrote
process identity. MCP bound `task_id` and `attempt` only after that returned.
Between the register and the identity write the durable record said `preparing`
with no process, which is indistinguishable from a run that never launched.
`recover._live_holder` skipped any run whose `pid is None`, so the window read as
"nothing is running" and `apply_action(RELEASE_CLAIM)` transferred the claim.

**The invariant:** at every instant after a run id exists, the durable record
answers what the run's owner is and whether it is safe to release what its task
holds. Either it names a verified owner, or it says ownership is unknown.
Missing identity is never read as proof of absence.

The release half of this was reproduced end to end before the fix, not inferred
from a code trace (`review/probe_f13_release.py`, on master). `run_check`
inserted the run row, then blocked in `_wait_windows` for the child's entire
execution, and only then called `mark_running`. `process_json` was NULL and
`lifecycle` was `preparing` for the whole run and forever after a crash. Driving
the sequence with a real child that was genuinely writing: task A admitted and
holding `scope:checkout`, A superseded to generation 2 with claims deliberately
kept, `inspect` reported `CLAIM_STALE_GENERATION`, `RELEASE_CLAIM` succeeded, a
second task was admitted at generation 1, and A's child was still alive and still
advancing its output. Two tasks held one checkout.

On POSIX the consequence is worse than on Windows. `_run_posix` uses
`start_new_session=True`, so after a supervisor crash nothing kills the orphan at
all. The child survives indefinitely, still writing, while recovery reads a
pidless row forever.

## 2. Data model: what was proposed, what shipped

The design declared a `launches` table plus `run_intents` and `cancel_intents`,
and a `runs` migration carrying the new lifecycle values.

**The proposed `launches.run_id TEXT PRIMARY KEY REFERENCES runs(run_id)` was
never shipped, and the proposal was wrong twice over.** The real schema declares
no `REFERENCES` clause anywhere: `grep -c REFERENCES src/vkit/storage.py` is `0`
and `PRAGMA foreign_key_list(runs)` returns `[]`. An earlier revision of this
document claimed that rebuilding `runs` needed a per-migration foreign-keys-off
flag in `_migrate`, because a probe invented a `claim_members` table referencing
`runs`. That claim came from this document naming such referents. Verified
against the real DDL: the rebuild succeeds with foreign keys on and the pragma
untouched. Treat any claim here that rests on a referent this schema does not
have as unverified until it has been read out of `storage.py`.

**Enforcement is on for anything R2 adds.** `storage.py:296` runs
`PRAGMA foreign_keys = ON` inside `_connect`, which is what `_Transaction` and
every `Store` method go through. The proposed ordering inserted `launches` at
step 2, before the `runs` row it references, which raises `IntegrityError` on
the first run of the new code and not deferred (`review/probe_launch_order.py`).
The shipped order is runs first, then `launches`.

The reordering is what makes a recovery distinction real. Under the original
order a `preparing` row always had a `launches` row, so "no `launches` row"
could not tell "never attempted" from "attempted and unresolved". The row
exists because the order permits that difference.

**The lifecycle change is not additive.** `runs` is created once, in migration 1,
with a hard constraint:

```sql
lifecycle TEXT NOT NULL CHECK (lifecycle IN ('preparing','running','terminal'))
```

SQLite cannot widen a CHECK constraint in place. `ALTER TABLE ... ADD COLUMN`
adds a column and leaves the constraint standing, so `launching` and `cancelling`
are rejected even after one is added. Migration 5 does the documented table
rebuild instead.

**Decision: rebuild rather than drop the constraint.** Dropping the CHECK is the
smaller diff and leaves the database unable to reject a typo'd lifecycle, which
is the class of bug this product exists to prevent.

**What the rebuild silently loses: `runs_by_task`.** It is an index, not a
table, and `DROP TABLE runs` takes it with the table. Nothing fails. The query
plan changes from `SEARCH runs USING INDEX runs_by_task (task_id=?)` to
`SCAN runs`, and `_live_holder` reads `runs` by `task_id` on every claim
release, which is the F13 path. Migration 5 re-issues the index after the
rename. **A test asserting the query plan rather than only that the migration
applied does not exist.** A test asserting "migration 5 ran" passes with the
index gone. That gap is still open.

## 3. Lifecycle ordering

Shipped ordering, every row a write that commits before the next begins.

| # | Action | Crash here leaves | Recovery concludes |
| --- | --- | --- | --- |
| 1 | Validate the task, its generation, and the caller's `required_resources` | nothing exists | none |
| 2 | `prepare_job`, create and hold the named job handle open | nothing durable | none |
| 3 | `register_run` at `lifecycle='preparing'` | a pidless run, no `launches` row | never attempted |
| 4 | `record_launch`, the `launches` and `run_intents` rows | a pidless run with a launch recorded | attempted, outcome unknown |
| 5 | `CreateProcess(SUSPENDED)`, join the job, read the creation FILETIME | the same, plus a suspended child | the OS kills the tree on handle close |
| 6 | `lifecycle='launching'`, `ownership_known: false` | identity unknown | the dangerous window |
| 7 | Publish `{pid, creation_time, job_name, ownership}`, `lifecycle='running'` | a verified owner | live, or dead if the pid names nothing |
| 8 | Publish the terminal report | `terminal` | terminal with no report if the swap did not land |
| 9 | Cancel request, `cancel_intents` plus `cancelling` | `cancelling` | refused while ownership is unknown |
| 10 | `TerminateJobObject`, wait, publish `BLOCKED/cancelled` | `terminal` | nothing outstanding |

**Crash between 5 and 6 is the one that matters, and the OS closes it without
us.** The job name is written into `launches` before `CreateProcess`, so a
`launching` row still carries a handle another process can open. Recovery and
cancellation read the name from `launches` rather than only from `process_json`,
and every reader takes the union of the two. Ownership is persisted at the two
points where it first exists: the name before the process, the pid after.

Recovery treats a `launching` run as UNCERTAIN by default. If the persisted job
name still opens and reports zero live processes, and the supervisor pid is
confirmed gone, and the recorded creation FILETIME is unreadable at that pid,
the finding is `RUN_UNRESOLVED_LAUNCH` with `action=ABANDON_LAUNCH`. Two of
three keeps the claim. That is the only path that clears an unresolved launch
without a human naming evidence.

## 4. Windows ownership, and the measurement that followed it

The old `_new_job` called `CreateJobObject(None, "")`, which is unnamed, so
`terminate_owned_tree` could never open it. That is why cancelling a live
Windows run failed, and why the supervisor wrote a job name beside a process
identity that had never been in a job.

`prepare_job` returns a `JobLease` carrying both the name and the handle. Both
halves are needed. The name is what another process passes to `OpenJobObject`.
The handle is what `AssignProcessToJobObject` takes, and holding it open across
the client's disconnect is what makes `KILL_ON_JOB_CLOSE` fire at the right
moment. Returning the name alone cannot assign the child, so the containment
rests on nothing and the tree survives cancellation. Returning the handle alone
cannot be opened by a second process, so a cancel from another terminal finds no
tree. Letting the caller reopen the job is also wrong, because the lease is
precisely what must not be reopened.

**Measured on this host, after the design was written.** A named job does not
survive its creator. Once the creating process exits, `OpenJobObject` on that
name fails with WinError 2 even though a member process is still running. So the
supervisor, not the process module, is what has to stay alive. It holds the
handle, its death fires `KILL_ON_JOB_CLOSE`, and that was measured to kill the
member too. The design assumed the name would outlive the process that created
the job; it does not.

## 5. Supervisor

One new entry point and one thin detached process. No daemon, no scheduler, no
registry. `start_run` validates, then takes one of three paths: a recorded
`launches` row is a replay and relaunches nothing; `detach=False` runs the
supervisor body in the calling process, which is what the CLI and the console
want; `detach=True` spawns `[sys.executable, "-m", "vkit.supervise", run_id,
state_root]` and returns a handoff without waiting.

`vkit/supervise.py` re-opens the child's store, reads the `launches` row, and
runs the launch and publish steps. It is the only writer of `process_json` after
launch.

**`supervise.py` needs a `__main__` guard, and describing it as "one function"
is what hid that.** `-m` only executes a module through a `__main__` guard. A
supervisor module with a single function and no guard runs, exits 0, and has
done nothing: `start_run` returns a handoff for a run that will never launch and
the row sits `preparing` forever, which is the exact window this milestone
exists to close. The fault-injection seam lives in the same module, so the two
crash gates inherited the no-op and passed or failed for the wrong reason.

**Late startup.** The detached process may not have launched when the client
asks for status. `run_get` returns the recorded lifecycle and no outcome, and
`run_cancel` records a cancel intent without erroring. The supervisor checks for
its own cancel intent at launch and again before publishing identity. A cancel
that arrives before identity is a deferred cancel, never a lost one and never a
cancelled-then-running run.

**Parent crash.** A client dying is unobservable to the supervisor, which holds
the job handle and never checks the parent. If the supervisor dies the OS kills
the tree and the row stays `running` with a dead pid. If the client dies and the
supervisor lives, the run completes. Neither case needs polling.

## 6. What was removed, and the one row that could not be

The design's deletion list was a set of deletions rather than deprecations,
because leaving any of them in place is a second authority for the same fact.
Four of them needed judgement rather than execution.

**`RUN_WITHOUT_PROCESS` was wrongly listed for deletion.** It is returned by
`_unfinished_run_finding` for a run that is unfinished with no pid. It carries
no action and is a correct thing to show an operator. The F13 defect is in the
claim-release guard `_live_holder` and in `_holder_liveness`, which return a
note where they should refuse. `RUN_UNRESOLVED_LAUNCH` replaced the guard's
refusal. Deleting the finding would have lost a real report.

**The registration in `run_check` cannot simply be deleted for the supervisor
path.** Three callers, `cmd_check_run`, `console/operations.run_check` and
`integration/sandbox.py`, call `run_check` directly and register nothing
themselves. With the row gone, `store.publish` finds no row and raises on every
`vkit check run`. Either all three route through the supervisor or the
registration stays for non-supervisor callers. The shipped choice keeps
`run_check` synchronous for those three.

**The console held a second cancel authority the list missed.** It read
`run_process_identity` and fabricated a `ProcessIdentity(pid=int(pid),
creation_time=-1)` to force the core's refusal, then called `cancel_run` with
that identity. The design gave `cancel_run` a `run_id`-only signature, so this
raises `TypeError` on every cancel and surfaces as a console 500.

**Three more sites read "no identity yet" as "nothing is happening"**, which is
the same inference F13 is about. `_Run.unfinished` covered only `preparing` and
`running`, so a `launching` run was classified finished and recovery reported a
mid-flight run as terminal-with-no-report. The console answered a cancel for an
identity-less run with a settled `BLOCKED/ownership_lost` verdict, duplicating
the core's refusal. And `store.load` raises for any run without a published
report, which under this design every in-flight run is, so `run show` on a
running check reported a valid request as invalid.

## 7. Assumptions about R1, and the result of the re-check

The design assumed six things about R1's contract. Re-checked against merged
master on 2026-09-30: A1 (no `runs` column added, so `run_intents` is not a
duplicate), A2 (the contract carries `resources` and `required_checks`, and the
wire key is still `required_resources`), A4 (admission takes claims through the
same transaction that writes the task row, so a refused claim leaves no task),
A5 (`supersede_task` is still the only writer of `generation`) and A6 all hold as
written.

Only A3 needed correction. The guessed ownership check is `tasks.verify_ownership`,
which raises `ConflictError` rather than returning `(bool, reason)`, and it reads
the task and the claims on two separate connections. The intent holds and the
shape does not, and the shape was the point. The read-then-write window is what
A3 existed to close.

**`register_run` keeps its `fixture_digest` argument.** An earlier revision said
to drop it because R1 owns the field now. The parameter is keyword-only with no
default and `register_run` is still the tree's only writer of that column; R1's
occurrence in `tasks.py` is the readiness record's identity dict, not a `runs`
write. What R1 owns is the acceptance comparison. Recording a real fixture
digest on the run is R5's evidence gap.

## 8. Storage API

Seven additive methods on `Store`: `record_launch`, `publish_identity`,
`mark_launching`, `record_cancel_intent`, `load_launch`, and `runs_for_task` for
the recovery holder query. `register_run` kept its signature and gained
`task_id` and `attempt` as direct parameters, replacing the separate
`attach_task` call.

`publish_identity` and `mark_launching` are two distinct updates, both
`WHERE lifecycle != 'terminal'`. A cancel that has already published terminal
wins over a late identity write. That is correct, because the run is cancelled
and stays cancelled.

## 9. The recovery guard

The shipped guard refuses before it asks about liveness at all.

```
for run in runs of (task_id, held_generation):
    if run.lifecycle in ('preparing', 'launching'):
        return refusal          # ownership unknown, so presumed live
    if not run.process or not run.process.get("ownership_known"):
        return refusal
    state = liveness(pid, creation_time, boot_id)
    if state in (ALIVE, UNCERTAIN):
        return refusal
return None                      # only confirmed-dead and terminal runs clear
```

`register_run`'s insert does not list `process_json`, so every run sitting in
`preparing` has None here. The `None` case is the F13 window, not an impossible
state, so it refuses.

A run at a different generation of the same task does not block. The claim being
released is the stale generation's, and the current generation's runs hold their
own claims.

`Action.ABANDON_LAUNCH` marks the `launches` row abandoned and re-runs the
step-5 liveness evidence check inside the write transaction. It never deletes
anything and never touches claims. The claim is released only when the existing
`RELEASE_CLAIM` finds no live holder. Two explicit actions, in order, each with
evidence.

**Two liveness implementations could disagree about the same pid.** `recover`
probed with `PROCESS_QUERY_INFORMATION` and its own error-code table;
`procidentity` probed with `PROCESS_QUERY_LIMITED_INFORMATION` and its own.
`recover` called `read_identity` only as a second opinion after it had already
reached a verdict, so the two could disagree, and only `procidentity`'s
measurements are documented as measured. Worse, `recover` treated WinError 6 and
1168 as DEAD alongside 87, while `procidentity` states as measured that 87 is
the one WinError `OpenProcess` returns for a pid with no process behind it. A
DEAD verdict from a code the other probe says nothing about is exactly the
premise a claim release rests on. R2's guard stops depending on it by refusing on
unknown ownership first. Collapsing the two probes into one is a separate
cleanup and was not done.

Checked and safe, so not re-investigated. `claims.acquire_in` and
`_release_claim` are atomic and cannot by themselves grant a resource twice.
`inspect()` on a pidless run is inert and no caller releases from it.
`CannotConfirm` retains the claim at all four of its recovery call sites.
`UnsupportedPlatform` is unreachable from `recover`.

## 10. The gate

Twelve public-behaviour regressions, in `tests/test_r2_ownership.py`, driven
through `supervisor.start_run`, `cancel_run`, `recover.inspect` and
`apply_action` with real processes, real SQLite and real Job Objects. All twelve
names in the design shipped unchanged:

`test_check_start_returns_a_run_id_while_the_check_runs`,
`test_second_client_cancels_a_run_started_by_another_process`,
`test_cancel_does_not_kill_an_unrelated_control_process`,
`test_client_disconnect_leaves_the_run_owned`,
`test_launch_crash_before_identity_publication`,
`test_release_while_child_alive_refuses`,
`test_parent_crash_keeps_claims_until_the_run_ends`,
`test_repeated_mutation_request_does_not_duplicate_a_launch`,
`test_no_second_owner_writes_concurrently`,
`test_cancel_races_completion_to_one_outcome`,
`test_posix_cancel_still_refuses`, and
`test_run_report_schema_accepts_ownership_known_false`.

The two crash gates need a crash at an exact point, which a `finally` block
cannot survive. One private hook in `vkit/supervise.py` reads an environment
variable and calls `os._exit(9)` between the launch step and the identity write.
That is the only test seam added.

**One acceptance row was never met.** `scripts/acceptance02.py` row 6 inverts
meaning under R2: its central claim is that an in-flight run names no pid, and
the comment and verdict prose document exactly the limitation being removed.
Those strings are interpolated into the row note, so rewriting the Python without
rewriting the prose produces a green acceptance table that lies.

## 11. Open questions, with the defaults an implementer used

Each was decided before the work started rather than during it.

| # | Question | Default | Cost of being wrong |
| --- | --- | --- | --- |
| Q1 | Does `run_cancel`'s MCP schema keep requiring `owner_pid` and `owner_creation_time` from the client? | No. Both parameters deleted. A client-supplied pid is a client-supplied identity | wire-format break for MCP callers |
| Q2 | Does `cmd_check_start` exit 0 immediately now that no outcome exists yet? | Yes, exit 0, with `lifecycle` in the payload. Exit codes are about the command, and the command succeeded | a script reading the exit code to learn the verdict must call `run show` |
| Q3 | Inline or detached by default for MCP `check_start`? | Detached. `detach=False` passes only from `cmd_check_run` and the console | none; the flag exists |
| Q4 | Supervisor module path | `[sys.executable, "-m", "vkit.supervise"]` via `subprocess`, since no standard handle inheritance is needed | none |
| Q5 | The detached supervisor's own lifetime if the run finishes in 200ms | it exits after publishing; no reaping loop | none |
| Q6 | Should `run_get` poll for the detached supervisor's startup? | No. `preparing` with no identity is a valid, honest answer | none |
| Q7 | Job name entropy and naming | `Local\vkit-run-<run_id>-<16 hex>`, minted by `prepare_job` and persisted in `launches` before the process. Not derived from `run_id` alone | none |
| Q8 | Keep `FindingKind.RUN_WITHOUT_PROCESS` as a deprecated alias? | Keep it, as a report and not as a guard. The old wording in the deletion list said "delete it" and was wrong; see section 6 | old JSON findings naming it are audit records, which is another reason to keep the enum |
| Q9 | Does `recover` get a guard so `RELEASE_CLAIM` cannot be scripted? | keep the existing explicit `evidence` string requirement | none |
| Q10 | Should `MARK_RUN_DEAD` also accept a `launching` run? | No. `ABANDON_LAUNCH` is the only action for `preparing` and `launching`, because it carries the job-name evidence `MARK_RUN_DEAD` does not have | none |

## 12. What this file no longer carries

The original held a SQL dump, a lifecycle table, a proposed function signature,
a per-function deletion list, and a migration-surface census naming the files a
grep-driven migration would break. All of it described a tree that no longer
exists, and two of its scripts, `diagnose_cancel_posix.py` and
`verify_posix_cancel.py`, were deleted. The census also asserted that no test
file imports `vkit.supervisor`; three do now.

The decision those sections carried is in the code that shipped. The knowledge a
reviewer needs from them, which is why each section above kept a paragraph
rather than a link, is why a thing was chosen over the alternative someone might
otherwise propose.

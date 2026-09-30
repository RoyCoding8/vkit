# Process and resource lifecycle audit

Scope: `src/vkit/{procs,procidentity,supervisor,recover,claims,claimkind,storage,execution,cli}.py`, MCP run entrypoints, and Plans 02/CONTRACT. Audited HEAD `ddb62a3`. No product code or installed user config was changed.

## Findings

### P1: A live run has no process identity in storage, so recovery can release its claim

`execution.run_check` inserts a `preparing` run, starts the child, waits for `run_command` to finish, and only then writes its PID and creation time with `mark_running` (`src/vkit/execution.py:277-281, 294-317`; `src/vkit/procs.py:491-562`). Until the child exits, recovery sees no PID. `recover._live_holder` explicitly skips runs without one (`src/vkit/recover.py:801-803`).

Trigger: start a check holding a resource, supersede its task while the check is still running, then apply `release_claim`. Superseding intentionally retains the old claim (`src/vkit/tasks.py:122-141`), but recovery finds no live holder and can release it; a second task can then acquire the same resource while the first child is still writing. This was reproduced with the real example driver: at release time its PID was confirmed alive, the run record had `process_identity: null` and finding `run_without_process`, recovery released the claim, and a second task acquired the same exclusive key while that PID remained alive. The first check then completed naturally with PASS. The runnable probe and captured result are [probe_recovery.py](probe_recovery.py) and [probe_recovery_result.json](probe_recovery_result.json). This also leaves a crash during execution indistinguishable from a run that never launched (`src/vkit/recover.py:541-550`).

Repair: record launch intent and the process identity before the child can execute, then bind every run to its task and generation at registration. Keep recovery conservative when launch state is uncertain.

### P1: Run entrypoints do not enforce the resource claims

`claims.acquire` correctly takes all requested resources atomically (`src/vkit/claims.py:100-135`), but neither CLI `check start` nor MCP `check_start` acquires or verifies the task's declared claims before calling execution (`src/vkit/cli.py:452-465`; `src/vkit/mcp/_tools.py:637-653`). MCP's optional claim in `_task_begin` is a separate operation (`src/vkit/mcp/_tools.py:539-551`), not a gate on starting a check. The parent’s isolated probe confirmed that a conflicting resource request was admitted and the check executed.

Repair: make the common run-start path acquire the task's required resource set before execution and refuse the run on conflict. Keep the existing transactional claim implementation.

### P1: Start is synchronous, and the Windows job cannot be opened by cancellation

`supervisor.start_run` calls `run_check` inline (`src/vkit/supervisor.py:128-139`), and `run_check` waits for the check before returning. CLI `check start` returns only after that call (`src/vkit/cli.py:461-488`). MCP bypasses the supervisor and calls the same synchronous `run_check` directly (`src/vkit/mcp/_tools.py:649`). The run ID therefore is not returned promptly, and no independent supervisor keeps the operation available after the initiating process exits.

The Windows run job is created unnamed (`src/vkit/procs.py:424-436`) and its handle is closed when `run_command` returns (`src/vkit/procs.py:559-570`). Cancellation instead calls `OpenJobObject` with a name (`src/vkit/supervisor.py:263-272`). The only name-attachment path runs after `run_check` and only when the recorded child PID equals the supervisor PID (`src/vkit/supervisor.py:141-147`), which the normal subprocess path does not satisfy. Thus a normal Windows run has no reachable named job for cancellation, even if start becomes asynchronous without fixing job creation.

Repair: use one shared start path that launches a durable per-run worker, gives the job a real per-run name (or retains an accessible owner handle), and persists that identity before resuming the child. Return only after launch is durably confirmed.

### P1: MCP binds the run to its task only after execution

MCP calls `run_check` without `task_id` or `attempt`, then calls `attach_task` after it returns (`src/vkit/mcp/_tools.py:637-654`). During the entire check, the database row is not associated with the task or generation. This means task-scoped recovery cannot find it even after a future change starts recording its PID, and a concurrent generation change can leave it attached to an obsolete attempt only after its execution completes.

Repair: pass the task ID and generation into run registration before launch, as the CLI path already does through `supervisor.start_run` (`src/vkit/supervisor.py:135-137`).

## Confirmed safeguards and report correction

- Claim acquisition/release is transactionally implemented; this audit found the admission wiring missing, not a race in the SQL arbitration (`src/vkit/claims.py:100-184`).
- POSIX zombie detection is handled: `_is_alive_posix` treats `Z`/`X` as dead, and recovery's PID identity logic distinguishes dead from uncertain (`src/vkit/procidentity.py:444-459`; `src/vkit/recover.py:192-251`). I found no zombie-specific defect.
- `REPORT.md` says "Linux has no [job-object] equivalent" (`REPORT.md:38`). That is too broad as a Linux capability statement. Linux pidfds provide stable references for signaling one process ([`pidfd_send_signal(2)`](https://www.man7.org/linux/man-pages/man2/pidfd_send_signal.2.html)); cgroup v2 provides group management and `cgroup.kill` ([kernel documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)). They do not make the current POSIX process-group implementation equivalent to a Windows job object, and pidfds alone do not contain a process tree. The report should say that this build uses process groups and does not implement/test pidfd or cgroup-v2 containment; availability and permissions were not tested here. `setsid` escape and supervisor-death measurements remain valid for the current process-group path.
- The report identifies commit `14e21cb` (`REPORT.md:10`), while this audit ran at `ddb62a3`. Treat its suite totals and acceptance counts as a snapshot of the earlier commit, not current-HEAD verification.

## Checks run

- Read the named implementation files, MCP run/cancel paths, `plans/02-ownership-and-runs.md`, `plans/CONTRACT.md`, and `REPORT.md`; traced calls and lifecycle ordering by source inspection.
- Queried repository context-mode history. It contained no earlier Poteto Style process decision that changed this audit.
- Checked Linux capability claims against the Linux man-pages and kernel cgroup-v2 documentation.
- Ran only the bounded isolated real-process probe in `review/probe_recovery.py`; the driver slept for five seconds and exited naturally. No full suite, live model, or paid call was run. No product code or parent probe/audit document was modified.

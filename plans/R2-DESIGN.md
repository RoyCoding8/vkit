# R2 design: durable process ownership and safe recovery

Implementation spec for R2 of `review/REPAIR_PLAN.md` (F05, F13). Written to be executed in one worktree. The worker does not wait for R1; it builds against the R1 contract as the repair plan states it and re-checks the assumptions in §7 after R1 merges.

## 1. The defect, stated as one invariant violation

`run_check` registers the run row, launches, **waits for completion**, then writes process identity (`execution.py:310`), and MCP binds `task_id`/`attempt` only after that returns (`mcp/_tools.py:710`). Between the register and the `mark_running` the durable record says `preparing` with no process. That is indistinguishable from a run that never launched. `recover._live_holder` (`recover.py:801-802`) skips any run whose `pid is None`, so the window reads as "nothing is running" and `apply_action(RELEASE_CLAIM)` transfers the claim. One defect, two findings.

**The invariant that replaces it:** at every instant after a run id exists, the durable record answers *"what is this run's owner, and is it safe to release what its task holds?"* Either it names a verified owner, or it says ownership is unknown. Missing identity is never read as proof of absence.

## 2. Data model

Migration 5, additive only, applied once through `storage.MIGRATIONS`. Runs already written stay readable and their reports stay schema-valid.

```sql
CREATE TABLE IF NOT EXISTS launches (
    run_id      TEXT PRIMARY KEY REFERENCES runs(run_id),
    project_root     TEXT NOT NULL,   -- absolute; the supervisor re-resolves the same project
    state_dir        TEXT NOT NULL,   -- absolute state_root, so the child opens this DB, not a cwd guess
    manifest_dir     TEXT NOT NULL,   -- absolute directory the manifest was parsed from
    check_id         TEXT NOT NULL,
    argv_json        TEXT NOT NULL,   -- resolved argv, recorded before spawning
    cwd              TEXT NOT NULL,
    stdout_path      TEXT NOT NULL,
    stderr_path      TEXT NOT NULL,
    env_json         TEXT NOT NULL,   -- RunEnvironment provenance; {} for ambient
    timeout_seconds  REAL NOT NULL,
    supervisor_pid   INTEGER NOT NULL,
    supervisor_start INTEGER,         -- Windows creation FILETIME of the supervisor
    requested_at     TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('detached','inline'))
);

CREATE TABLE IF NOT EXISTS run_intents (
    run_id     TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    generation INTEGER NOT NULL,
    operation  TEXT NOT NULL,   -- 'check_start' | 'run_cancel'
    requested_at TEXT NOT NULL,
    PRIMARY KEY (run_id, operation, task_id, generation)
);

CREATE TABLE IF NOT EXISTS cancel_intents (
    run_id    TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0   -- 0 pending, 1 resolved by whoever published the report
);
```

`runs.lifecycle` gains two values, `launching` and `cancelling`:

| value | written by | meaning |
| --- | --- | --- |
| `preparing` | start request | intent recorded, no process created yet |
| `launching` | supervisor | `CreateProcess` returned; job + pid not yet published |
| `running` | supervisor | job name and verified identity are in `process_json` |
| `cancelling` | cancel request | termination asked for, terminal state not yet known |
| `terminal` | whoever finishes | one outcome published, exactly once |

`process_json` gains `"ownership_known": false` and `"launch_state": "started" | "identity_published"`. `additionalProperties: false` in `schemas/run-report.v1.json` means a new key there forces a matching schema edit in the same commit; the report carries the two keys only when the value is false.

The R1 contract may add columns to `tasks`. §7 names that as assumption A1.

## 3. Lifecycle ordering and what each crash window concludes

Every row below is a write that commits before the next step begins. "Recovery concludes" is what `recover.inspect` reports and what `apply_action` is allowed to do.

| # | Action (committed before the next) | Crash here ⇒ durable state | Recovery concludes | Claim |
| --- | --- | --- | --- | --- |
| 1 | Validate task open, generation current, and caller still holds every `required_resources` key (R1's contract field), inside one `BEGIN IMMEDIATE` | nothing exists | — | — |
| 2 | `prepare_job` — create the **named** job (`CreateJobObject(None, f"Local\\vkit-run-{run_id}-…")`) and hold the handle open. No row yet, so a crash here leaves nothing durable and nothing to reconcile | nothing exists | — | — |
| 3 | `store.register_run` → `lifecycle='preparing'`, `task_id`/`attempt` set **by the supervisor, not the caller** | `preparing`, no identity, no `launches` row | `RUN_UNRESOLVED_LAUNCH`: no process was created — the row is `preparing` and no `launches` row says a launch was ever attempted. Reconcilable only by `ABANDON_LAUNCH` with evidence. | held |
| 4 | `record_launch` — insert the `launches` row (`run_id`, job name, resolved argv, cwd, supervisor pid), plus the `run_intents` row | `preparing`, identity unknown, `launches` present | `RUN_UNRESOLVED_LAUNCH`: a launch was attempted and its outcome is unknown. Claims held, no release, no auto-abandon. | held |
| 5 | Supervisor `CreateProcess(SUSPENDED)`, `AssignProcessToJobObject`, read creation FILETIME | `preparing`, `process_json` null | `RUN_UNRESOLVED_LAUNCH`: the supervisor died holding a suspended child. `KILL_ON_JOB_CLOSE` fired on handle close, so the tree is gone. That is the *default* OS behaviour rather than a recorded fact, so the claim is still held until `ABANDON_LAUNCH` is applied. | held |
| 6 | `lifecycle='launching'`, `process_json={"ownership_known": false, "launch_state":"started", "check_id":..., "command":...}` | `launching`, identity unknown | `RUN_UNRESOLVED_LAUNCH`: a process was created and its identity was never published. **The dangerous window.** Claims held, no release, no auto-abandon. | held |
| 7 | Write `{"pid","creation_time","job_name","ownership"}` into `process_json`, `lifecycle='running'` | `running`, verified owner | `RUN_LIVE_PROCESS` if the handle opens; `RUN_DEAD_PROCESS` if the pid names nothing and the creation FILETIME matches nothing. Both existing kinds, both correct here. | held |
| 8 | Supervisor publishes the terminal report (`store.publish`) | `terminal` | `RUN_TERMINAL_NO_REPORT` if the swap did not land. Existing behaviour, already correct. | R1 releases on task lifecycle |
| 9 | Cancel request → insert `cancel_intents`, `lifecycle='cancelling'` | `cancelling` | `RUN_UNRESOLVED_LAUNCH` if `ownership_known` is false: cancellation cannot proceed and the claim is retained with a named reason. | held |
| 10 | Cancel resolves: `TerminateJobObject`, wait for all job processes to exit, publish `BLOCKED/cancelled` | `terminal` | nothing outstanding | held until R1 supersedes |

**Crash between 5 and 6 is the one that matters, and the OS closes it without us.** The job name is written into `launches` at step 2, before `CreateProcess`, so a `launching` row still has a durable handle another process can open. `recover` and `cancel_run` read the name from `launches` rather than only from `process_json`, and every reader takes the union of the two. The two pieces of ownership are persisted at the two points where they first exist: the name at step 2, the pid at step 6.

`recover` treats a `launching` run as UNCERTAIN by default. If the persisted job name still opens and reports zero live processes **and** the supervisor pid is confirmed gone **and** the recorded creation FILETIME is unreadable at that pid, the finding is `RUN_UNRESOLVED_LAUNCH` with `action=ABANDON_LAUNCH`. Two of three is UNCERTAIN and keeps the claim. This is the only path that clears an unresolved launch without a human naming evidence.

### 3.1 Steps 3 and 4 were the wrong way round (corrected 2026-10-01)

An earlier revision inserted `launches` at step 2, **before** the `runs` row it
references. §2 declares `launches.run_id TEXT PRIMARY KEY REFERENCES
runs(run_id)`, and every connection enforces it: `storage.py:296` runs
`PRAGMA foreign_keys = ON` inside `_connect`, which is what `_Transaction` and
every `Store` method go through. Step 2 as written therefore raises
`IntegrityError: FOREIGN KEY constraint failed` on the first run of the new
code, immediately and not deferred (`review/probe_launch_order.py`):

```
foreign_keys pragma : 1
foreign_key_list(launches) = [(0, 0, 'runs', 'run_id', 'run_id', ...)]
design's order (launches first) -> REJECTED: FOREIGN KEY constraint failed
swapped order (runs first)     -> SUCCEEDED
orphan launches row            -> REJECTED immediately, so there is no later window
```

This is not the migration-5 finding, and the two were conflated until they were
both checked. That finding is that this schema declares **no** `REFERENCES`
clause today, so rebuilding `runs` has nothing to violate. This one is that
**enforcement is on** for any constraint R2 adds. Both are true; only the first
bears on the rebuild.

Step 3's recovery column depended on the wrong order too: under it a `preparing`
row always had a `launches` row, so "no `launches` row" could not distinguish
"never attempted" from "attempted and unresolved". The reordering is what makes
that distinction real, which is the reason the row exists.

## 4. The Windows ownership mechanism

`_new_job()` (`procs.py:432`) calls `win32job.CreateJobObject(None, "")`, which is unnamed, so `terminate_owned_tree` can never open it. That is why cancel on a live Windows run fails today. `supervisor.start_run` papers over this at `supervisor.py:146` by writing `job_name_for(run_id)` beside a process identity that was never in a job.

Replace with a two-phase launcher in `procs.py`:

```python
@dataclass(frozen=True)
class JobLease:
    """The named job, and the handle that keeps it alive.

    Both halves are needed and neither substitutes for the other. The name is
    what another process passes to `OpenJobObject`, which is the whole reason
    the job is named rather than anonymous; the handle is what
    `AssignProcessToJobObject` takes, and holding it open across the client's
    disconnect is what makes `KILL_ON_JOB_CLOSE` fire at the right moment.

    The earlier form of this function was annotated `-> str` and returned the
    handle. Either reading breaks something: the name alone cannot assign the
    child to the job, so the containment the whole design rests on is never
    established and the tree survives cancellation; the handle alone cannot be
    opened by a second process, so G2 fails. Returning the name and letting the
    caller reopen the job is also wrong — the lease is precisely what must not
    be reopened, because the last handle closing is what fires the kill.
    """
    name: str
    handle: Any


def prepare_job(run_id: str) -> JobLease:
    """Create the named job. No process exists yet."""
    name = f"Local\vkit-run-{run_id}-{secrets.token_hex(8)}"
    job = win32job.CreateJobObject(None, name)
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return JobLease(name=name, handle=job)
kit-run-{run_id}", handle=job)
```

`prepare_job` is Windows-only and is the only place `CreateJobObject` is called. `run_command` splits into `launch(...)` returning immediately after `ResumeThread` with the job handle still open, and `await_exit(...)`. The handle must not close between them: `KILL_ON_JOB_CLOSE` turns the ordinary finally-block into a kill. Therefore:

| Function | Keep / change | Note |
| --- | --- | --- |
| `run_command` | keep signature, keep full body | still used by `cmd_check_run`, console `operations.run_check`, and the integration sandbox. Unchanged behaviour. |
| `_run_windows` | keep | becomes the reference implementation for `launch` |
| `_new_job` | **delete** | replaced by `prepare_job` |
| `ExecutionResult.to_json` | keep | — |
| new `JobLease` | add | holds the job handle, pid, creation FILETIME, job name; `close()` terminates and closes |
| new `launch(argv, cwd, stdout, stderr, timeout) -> JobLease` | add | Windows named; POSIX returns a lease holding the pid + pgid with no handle |
| new `await_exit(lease, timeout) -> ExecutionResult` | add | the wait + timeout half, currently `_wait_windows`/`_terminate_job` |

The supervisor, not `procs`, owns the lease. It is the only caller that must keep the handle open across the client disconnect.

`terminate_owned_tree(job_name)` stays as the second-client cancel primitive and its signature is unchanged. On POSIX it keeps raising `OSError` with the existing message verbatim; the measured basis is unchanged and R2 adds no POSIX containment.

## 5. The supervisor

`start_run` currently returns only after the check finishes, and cannot be called by MCP because it double-registers (`_tools.py:634-642` documents this and it is accurate). The replacement is one new entry point plus a thin detached process. There is no daemon, no scheduler, no registry.

`RunHandoff` is a frozen dataclass with `run_id`, `check_id`, `task_id`, `generation`, `lifecycle`, `started_at`, and `replayed`.
`start_run(...) -> RunHandoff` becomes: validate (§3 steps 1-2), then:

- **Replay.** `launches` row exists → do not relaunch. Return the handoff with `replayed=True` and the row's current lifecycle, or the published outcome if `terminal`.
- **Inline.** `kind='inline'`: the caller passed `detach=False` (only `cmd_check_run` and the console do). Run the supervisor body in this process and still return the handoff; the terminal outcome comes from `run_get`/`run show` afterwards.
- **Detached.** Spawn `[sys.executable, "-m", "vkit.supervise", run_id]` with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, stdout/stderr to `runs/<run_id>/supervisor.log`, `cwd` = project root. Write `kind='detached'` into `launches`. Return the handoff **without waiting**.

`vkit/supervise.py` (new, one function) re-opens `Store(Path(state_dir)/db)`, reads the `launches` row, and runs steps 3-9. It is the only writer of `process_json` after step 5.

**Late startup.** The detached process may not have reached step 5 when the client asks for status. `run_get` returns `lifecycle='preparing'` and no outcome, and `run_cancel` records a cancel intent without erroring. The supervisor, at step 5 and again at step 7, checks `cancel_intents` for its own run id and if one is present goes straight to step 9. That is the only ordering the race needs: cancel-before-identity is a *deferred* cancel, never a lost one and never a cancelled-then-running run.

**Parent crash.** The client dying at any point is unobservable to the supervisor, which holds the job handle and never checks the parent. `KILL_ON_JOB_CLOSE` then only fires if the *supervisor* dies. If the supervisor is killed, the OS kills the tree and the row stays `running` with a dead pid; `recover` reports `RUN_DEAD_PROCESS` and `mark_run_dead` reconciles it. If the client dies but the supervisor lives, the run completes normally. Neither case needs polling.

## 6. Functions that die

Every row is a deletion, not a deprecation. Leaving any of these in place is a second authority for the same fact.

| Dies | Where | Replaced by |
| --- | --- | --- |
| `run_check`'s register-inside-the-execute function — **only for the supervisor path** | `execution.py:277` | `supervisor` registers. **The registration cannot simply be deleted**: `cmd_check_run` (`cli.py:324`), `console/operations.run_check` (`operations.py:385`) and `integration/sandbox.py` all call `run_check` directly and register nothing themselves, so with the row gone `store.publish` finds no row and raises `cannot publish: <id> is unknown or already terminal` on every `vkit check run`. Either route all three through `supervisor.start_run(..., detach=False)`, or keep the registration in `run_check` for non-supervisor callers. |
| `store.attach_task` and its call at `_tools.py:710` | storage, MCP | the `run_intents` row written at §3 step 2; `register_run` receives `task_id`/`attempt` directly from the supervisor |
| `supervisor.start_run`'s `_is_registered` + "registered but no report" branch (`supervisor.py:118-126`) | supervisor | the `launches` row is the retry authority. A run with a `launches` row and no terminal outcome is *live or unresolved*, never "refuse to re-execute" |
| `start_run`'s `attach_job_name` call at `supervisor.py:146` and the fake-name write it makes | supervisor | `prepare_job` names a real job; the name is written into `launches` at step 2 |
| `Store.attach_job_name` | storage | `publish_identity` (§8) writes the job name with the pid |
| `procs._new_job` | procs | `prepare_job` |
| `_check_start`'s declared limit docstring at `_tools.py:634-642` | MCP | the limit is false after this change; the docstring is removed with the fix, not left as history |
| `console/operations.run_check`'s "the console is not a client that disconnects" branch | console | still correct. The console keeps `run_check` with `detach=False`. **Only the comment changes.** |
| `FindingKind.RUN_WITHOUT_PROCESS` | recover | replaced by `RUN_UNRESOLVED_LAUNCH` (§9). The old name encodes the bug: "no process recorded" as a distinct, calm state |
| `recover._live_holder`'s `if run.pid is None: continue` | recover | **the F13 line.** Replaced by `_live_holder` refusing on any unfinished run whose ownership is unknown |
| `recover._holder_liveness`'s "No run of that task has a recorded process to check" | recover | becomes a refusal, not a note. Unknown is not a clearance |
| `cmd_run_cancel`'s pre-flight `store.run_process_identity` and `_identity_of` | cli | `cancel_run` reads the record itself; the CLI passes only `run_id`. Today the CLI, not the supervisor, decides a run is uncancellable |

`cmd_check_start`, `_check_start`, and `_run_cancel` bodies are rewritten to call `supervisor.start_run` / `cancel_run` and return. `run_check` survives for `cmd_check_run`, `console/operations.run_check`, and `integration/sandbox.py` — and whichever of the two options the row above takes, all three must still end up with a registered row. `store.publish` requires one.

## 7. Assumptions about R1 that must be re-verified after R1 merges

| # | Assumption | If wrong |
| --- | --- | --- |
| A1 | R1 adds no column to `runs` that migration 5 must read; `run_intents` is not duplicated by a `runs.task_generation` column | fold the new column into migration 5 and delete `run_intents` |
| A2 | R1's contract field naming: `required_resources` (list of `ResourceSpec`-shaped entries) and `required_checks` | change the §3 step 1 validator; no other part moves |
| A3 | R1 exposes an ownership check R2 does not re-implement. **Checked 2026-09-30, and the name is not the one guessed here:** the function is `tasks.verify_ownership(store, task_id, generation)`, which raises `ConflictError` rather than returning `(bool, reason)`. Its *intent* holds — R2 calls this instead of re-deriving generation and claim predicates — but its *shape* does not: it reads the task through `get_task` and then the claims through `holders`, on two separate connections | either catch `ConflictError` at the §3 step-1 call site, or add a `conn`-taking form of `verify_ownership` so the generation read and the claim read share one transaction. The second is the one this spec wanted; the first leaves the read-then-write window A3 was written to close |
| A4 | R1's admission is atomic with claims, so a `check_start` refusal leaves no claim to release | if R1 leaves the current behaviour (claim conflict reported, `admitted: true`), §3 step 1 must also refuse |
| A5 | R1 stores a per-task `generation` that advances only on supersede, unchanged from `tasks.supersede_task` | `run_intents.generation` becomes wrong; §9's stale-holder query needs R1's new predicate |
| A6 | `open_task`/`get_task` in R1 still raise `TaskError` with the same message shape | trivial |

Re-check before writing the step-1 validator. Everything else in this spec is independent of R1's internals.

**Result of that re-check (2026-09-30, against merged master):** A1, A2, A4, A5 and A6 hold as written. A1 — R1 added no `runs` column, so `run_intents` is not a duplicate. A2 — the contract carries `resources` and `required_checks`; only the wire key the caller sends is still `required_resources`, and §3's validator reads the wire form. A4 — `tasks.admit` takes claims through `claims.acquire_in` inside the same `store.transaction()` that writes the task row, so a refused claim leaves no task. A5 — `supersede_task` is still the only writer of `generation`. A6 — `TaskError` and its messages are unchanged. Only A3 needs the correction recorded above, and it is a shape difference, not a missing capability.

## 8. Storage API

New on `Store`, all additive, all using the existing `transaction()` helper:

```python
def record_launch(self, *, run_id, task_id, generation, plan: LaunchPlan) -> None
def register_run(..., task_id=..., attempt=...)            # unchanged signature, existing columns
def publish_identity(self, run_id, identity: dict) -> None  # lifecycle -> 'running'
def mark_launching(self, run_id, process: dict) -> None     # lifecycle -> 'launching', ownership_known False
def record_cancel_intent(self, run_id, requested_by: str) -> None
def load_launch(self, run_id) -> dict | None
def runs_for_task(self, task_id, generation) -> tuple[dict, ...]   # for recover's holder query
```

`publish_identity` and `mark_launching` are two distinct UPDATEs, both `WHERE lifecycle != 'terminal'`. A cancel that has already published terminal wins over a late identity write, which is the correct outcome: the run is cancelled and stays cancelled.

`register_run` keeps its `fixture_digest=None` argument. An earlier revision said to drop it because R1 owns the field now; that is a no-op at best and a `TypeError` at worst, because the parameter is keyword-only with no default (`storage.py:345`) and `register_run` is still the tree's only writer of `runs.fixture_digest` — R1's `tasks.py` occurrence is the readiness record's identity dict, not a `runs` write. What R1 owns is the acceptance *comparison*. Recording a real fixture digest on the run is R5's evidence gap, not a change to make here. Do not add a second writer.

## 9. Recovery changes (F13)

`_live_holder` becomes the whole guard:

```
for run in runs of (task_id, held_generation):
    if run.lifecycle in ('preparing', 'launching'):
        return refusal   # ownership unknown -> presumed live
    # `process` is `dict | None`, and `register_run`'s INSERT does not list
    # `process_json`, so every run sitting in `preparing` has None here. The
    # `None` case is the F13 window, not an impossible state, so it refuses.
    if not run.process or not run.process.get("ownership_known"):
        return refusal
    state = liveness(pid, creation_time, boot_id)
    if state in (ALIVE, UNCERTAIN):
        return refusal
return None   # only confirmed-dead and terminal runs clear
```

A run at a *different* generation of the same task does not block; the claim being released is the stale generation's, and the current generation's runs hold their own claims. `run_intents` supplies that membership because `runs.task_id` is still set.

New `FindingKind.RUN_UNRESOLVED_LAUNCH` with `action=ABANDON_LAUNCH`, replacing the **guard** at `recover.py:802` — not the report at `recover.py:541`. `Action.ABANDON_LAUNCH` marks the `launches` row `abandoned=1` and re-runs the §3-step-5 liveness evidence check inside the write transaction. It never deletes anything and never touches claims itself. The claim is released only when the existing `RELEASE_CLAIM` finds no live holder. Two explicit actions, in order, each with evidence. That is what "retain claims for explicit reconciliation" means operationally.

### 9.1 Three sites the design did not account for

Found 2026-10-01 while tracing §9 against merged master. Each is a place where "no identity yet" is currently read as "nothing is happening", which is the same inference F13 is about — in three more places.

**1. `recover.py:431` `_Run.unfinished` is `lifecycle in ("preparing","running")`.**

A `launching` or `cancelling` run is classified **finished**, so `_run_findings` (`:529-532`) skips it and `_terminal_run_findings` runs instead: recovery reports a mid-flight run as terminal-with-no-report. Reachable from `operations.py:254` and `cli.py:656`. This must become "everything except `terminal` is unfinished" — which is the existing meaning of the name, currently under-applied.

**2. `console/operations.py:419-431` answers a cancel for an identity-less run with a *settled* verdict.**

```python
recorded = context.store.run_process_identity(run_id) or {}
pid = recorded.get("pid")
if not pid:
    return {..., "outcome": {"result": "BLOCKED", "reason": "ownership_lost",
             "detail": f"run {run_id} recorded no process, so there is nothing to cancel"}}
```

This both guesses early and duplicates the core's own refusal at `supervisor.py:185-197`. Under R2 it must record a `cancelling` intent and report that the cancellation is pending, not that the run has a settled `BLOCKED/ownership_lost` outcome. A cancel that is still arriving is not an answer about the run.

**3. `store.load` raises for any run without a published report, and both read paths surface that as a malformed request.**

`storage.py:480-481` raises `StoreError("no published report for run ...")`. `cli.py:355` turns it into `EXIT_INVALID` and `operations.py:231` into `Refused`. Under R2 *every* in-flight run is report-less by design, so `vkit run show <id>` on a running check — and the console's run detail view — report a valid request as invalid.

Q6 already settles the principle for `run_get` ("`preparing` with no identity is a valid, honest answer"), so this is the same answer applied consistently: a status read of an unfinished run returns the recorded lifecycle and the absence of an outcome, and reserves its error for a run id that does not exist at all. This was left UNKNOWN in the census; it is now decided.

### 9.2 Further corrections from the design audit (2026-10-01)

Verified against merged master before being written down. These are
corrections to the document, not to the code.

**`vkit/supervise.py` needs a `__main__` guard, and §5 calling it "one function"
is what hides that.** `-m` only executes a module through a `__main__` guard.
Of every `.py` under `src/`, only `cli.py:1069` and
`integration/launcher.py:94` have one, and there is no `__main__.py` anywhere.
A supervisor module with a single function and no guard runs, exits 0, and has
done nothing: `start_run` returns a handoff for a run that will never launch,
the driver is never spawned, and the row sits `preparing` forever — the exact
F13 window this milestone exists to close. The `VKIT_FAULT` seam at §10 lives
in this same module, so G5 and G7 inherit the no-op and pass or fail for the
wrong reason.

**G12 tests a key no listed writer emits.** `ownership_known` is tied to the
report schema, but `process_json` is a `runs` column (`storage.py:46`), not the
report's `process` object. The report's `process` is built at
`execution.py:438-446` and `supervisor.py:305-315` (`_cancel_report`), and
neither is in §6's rewrite list. G12 as written passes vacuously because
nothing ever adds `ownership_known` to a report. Either name `_cancel_report`
as a writer, or drop the key from the report and assert it on `process_json`
via `run_process_identity`.

**The console holds a second cancel authority that §6 does not list.**
`operations.py:419` reads `run_process_identity` and `:446` fabricates
`ProcessIdentity(pid=int(pid), creation_time=-1)` to force the core's refusal,
then calls `cancel_run(..., identity=identity)` at `:449`. §6 gives
`cancel_run` a `run_id`-only signature, so this raises `TypeError: unexpected
keyword argument 'identity'` on every cancel, surfacing as a console 500. §6's
claim that leaving any row in place "is a second authority for the same fact"
was true and this row was the one left out. (This is the same site as §9.1 item
2, seen from the deletion side rather than the behaviour side.)

**§5's `Store(Path(state_dir)/db)` names a path component that does not exist.**
The real names are `paths.py:19` `DB_NAME = "state.sqlite3"` and
`Project.db_path` (`paths.py:65`). The supervisor re-opens the child's store
with one of those.

**Q3 names a call site §6 does not create.** `cmd_check_run`
(`cli.py:300-345`) calls `run_check`, not `start_run`, and `detach` appears
nowhere in `src/` today. Console only, unless the `run_check` deletion row is
resolved by routing `cmd_check_run` through the supervisor too.

**G5's crash is described two ways.** §10's setup says the supervisor is
`taskkill`'d after step 5 while §10's fault-injection note specifies
`os._exit(9)`. `os._exit` does close handles so `KILL_ON_JOB_CLOSE` still fires
and G7 works, but "taskkill'd" reads as a second mechanism to build. Use the
`VKIT_FAULT` exit.

**`supervisor.py:118-126` is the call site of `_is_registered`, not its
definition** (which is `:150-154`). Matters only to a reader searching by
definition, but a deletion list should name the thing being deleted.

Verified accurate and left alone: §4's claim that `_new_job`
(`procs.py:432`) creates an *anonymous* job — `CreateJobObject(None, "")` — and
that the name recorded at `supervisor.py:146` was never given to
`CreateJobObject`, so no second process can address today's job. §1's
citations (`execution.py:310`, `recover.py:801-802`). §10's gate vocabulary,
all twelve resolving. And the current lifecycle values are exactly the three
`storage.py` writes.

## 10. The gate

Each row is a public-behaviour regression in `tests/`, driven through `supervisor.start_run` / `cancel_run` / `recover.inspect` / `apply_action` and the CLI + MCP surfaces. No stubs of vkit functions; real processes, real SQLite, real Job Objects.

| # | Test | Setup on Windows | Expected |
| --- | --- | --- | --- |
| G1 | `test_check_start_returns_a_run_id_while_the_check_runs` | example driver instrumented with a 5s sleep and a marker file (as `probe_recovery.py` does) | `check_start` returns in < 1s with `run_id` and `lifecycle` in `preparing`/`running`; the driver is alive; `run_get` shows the same `run_id` |
| G2 | `test_second_client_cancels_a_run_started_by_another_process` | client A starts; client B (separate process) runs `run cancel` | `TerminateJobObject` reached the tree; driver and a grandchild both dead within 5s; `cancelled: true`; unrelated control processes spawned by the test are alive |
| G3 | `test_cancel_does_not_kill_an_unrelated_control_process` | G2 plus 3 control `cmd /c ping -n 60` processes not in any job | all 3 alive after the cancel |
| G4 | `test_client_disconnect_leaves_the_run_owned` | client A starts, then `TerminateProcess` on A only | the run reaches terminal PASS on its own; `run_get` from a fresh client sees the same `run_id` and PASS; no orphan job handle |
| G5 | `test_launch_crash_before_identity_publication` | supervisor is `taskkill`'d after step 5 (`lifecycle='launching'`, `ownership_known: false`) and before step 6, driven by a fault-injection env var read in the supervisor only | `recover.inspect` reports `RUN_UNRESOLVED_LAUNCH`; `apply_action(RELEASE_CLAIM)` **refuses**; a second `acquire` of the same exclusive key **fails**; then `apply_action(ABANDON_LAUNCH, evidence=...)` followed by `RELEASE_CLAIM` succeeds |
| G6 | `test_release_while_child_alive_refuses` | `probe_recovery.py` verbatim as a test | `RELEASE_CLAIM` raises `RecoveryRefused`; second `acquire` fails; the driver is still alive; it finishes naturally PASS afterwards and the row is terminal with the claim still held |
| G7 | `test_parent_crash_keeps_claims_until_the_run_ends` | supervisor killed at step 5 as in G5 but with the job's `KILL_ON_JOB_CLOSE` observed | descendants are gone within 5s (OS guarantee, asserted not assumed); claim retained; row reconcilable |
| G8 | `test_repeated_mutation_request_does_not_duplicate_a_launch` | same `request_id` + payload 5×, alternating CLI and MCP | one `launches` row, one run, one report; 4 replays return the recorded outcome; a differing payload under the same id is refused |
| G9 | `test_no_second_owner_writes_concurrently` | two processes each `claim_request` the same key and `acquire` | exactly one `admitted`, the other refused by `ConflictError`; one `launches` row |
| G10 | `test_cancel_races_completion_to_one_outcome` | cancel fired at 0.99× the driver's remaining time, 20 iterations | every run is terminal with exactly one of `PASS` or `BLOCKED/cancelled`; no run is both; no row left `cancelling` |
| G11 | `test_posix_cancel_still_refuses` | `pytest.mark.skipif(sys.platform != "linux")` | `terminate_owned_tree(None)` raises `OSError` whose message still names setsid escape and kill-on-last-handle-close. Unchanged text, asserted |
| G12 | `test_run_report_schema_accepts_ownership_known_false` | a cancel report on a run that never launched | `validate("run report", RUN_REPORT, report)` passes; `Store.load` round-trips |

**Fault injection.** G5 and G7 need a crash at an exact point. Add one private hook in `vkit/supervise.py`: `if os.environ.get("VKIT_FAULT") == "after_launching": os._exit(9)`, placed between §3 steps 5 and 6. That is the only test seam added, it is one line, and it is removed by the same commit that removes the two tests if the tests move elsewhere. A subprocess that calls `os._exit` is the only way to test a crash that a `finally` block cannot survive, which is the whole point of these two cases.

### 10.1 What the gate does not cover, and the migration surface

The gate is the new behaviour. The migration surface is larger and is easy to under-count, so it is counted here rather than discovered.

**`tests/test_recover.py` is a false-census trap.** It defines its own `start_run()` at line 218; all 24 `start_run` hits in that file are that fixture, not the supervisor. A grep-driven migration that rewrites them is chasing the wrong symbol. Its `RUN_WITHOUT_PROCESS` assertions (`:327`, `:583-592`) pass today and will flood under R2, because every fresh run is briefly pid-less by design.

**No test file imports `vkit.supervisor`.** The only real imports are `scripts/acceptance02.py`, `scripts/diagnose_cancel_posix.py` and `scripts/verify_posix_cancel.py`. Every test reaches identity through `execution.run_check` or `console.operations.run_check`. So the test blast radius is `run_check`'s synchronous contract plus lifecycle literals — not `StartedRun` going away — because §6 keeps `run_check` synchronous for `cmd_check_run`, the console and `sandbox.py`. Roughly half the grep hits drop out on that basis.

**`scripts/acceptance02.py` is the single largest block: 30 of the ~59 breaking sites, across rows 5, 6, 7, 8 and 14.** Row 6 (`:903-925`) *inverts meaning*: its central claim is that an in-flight run names no pid, which R2 converts into "a pid is published later". Its comment at `:811-816` and verdict prose at `:944-946` document exactly the limitation being removed, and those strings are interpolated into the row note — so rewriting the Python without rewriting the prose produces a green acceptance table that lies. That is the same failure mode R1 found with `unverified_identities`: the record green, the claim false.

**`tests/test_storage.py:209-232` is the canary.** It issues a raw `UPDATE runs SET lifecycle='running'` and asserts lifecycle literals, so it is the one site that fails *at the database* rather than at an assertion. Run it first; if migration 5's rebuild is wrong, it says so before anything else does.

**`schemas/run-report.v1.json:13` pins the lifecycle enum** and is `additionalProperties: false`, so `launching`/`cancelling` force a schema edit in the same commit (G12 covers this).

**`console/static/app.js:309-315`** reads `result.outcome` on cancel. A deferred cancel has no outcome; the view must show a pending state.

**What the probe's behaviours become.** `probe_recovery.py`'s assertions invert: `process_identity` before recovery becomes non-null and `ownership_known: true`; `finding_kinds` becomes `["run_with_live_process"]`; `release_claim.succeeded` becomes `false` with a `RecoveryRefused`; `second_task_claim.acquired` becomes `false`; `driver.alive_after_release_and_second_claim` becomes `false` because the release never happened and the child completed normally. Keep the probe script itself in `review/` as the record.

## 11. Open questions, with defaults

The worker proceeds on the default and does not block.

| # | Question | Default | Cost of being wrong |
| --- | --- | --- | --- |
| Q1 | Does `run_cancel`'s MCP schema keep requiring `owner_pid`/`owner_creation_time` from the client? | **No.** Delete both parameters; the run record is the only source. A client-supplied pid is a client-supplied identity | wire-format break for MCP callers; announce it in the R2 handoff |
| Q2 | Does `cmd_check_start` exit 0 immediately now that no outcome exists yet? | **Yes, exit 0**, with `lifecycle` in the payload and the summary naming the run. Exit codes stay about the *command*, and the command succeeded | a script that read `check start`'s exit code to learn the verdict must now `run show`. Stated in the R2 handoff |
| Q3 | Inline or detached by default for MCP `check_start`? | **Detached.** `detach=False` is passed only by `cmd_check_run` and the console | none; the flag exists |
| Q4 | Supervisor binary/module path | `[sys.executable, "-m", "vkit.supervise"]`, launched via `subprocess` rather than `win32process`, since no std handle inheritance is needed | none |
| Q5 | Detached supervisor's own lifetime if the run finishes in 200ms | it exits after publishing; no reaping loop | none |
| Q6 | Should `run_get` poll for the detached supervisor's startup? | **No.** `preparing` with no identity is a valid, honest answer | none |
| Q7 | Job name entropy / naming | `Local\vkit-run-<run_id>-<16 hex>`, minted by `prepare_job`, persisted in `launches` at step 2 | reuse of `supervisor.job_name_for` is also fine; do not derive it from `run_id` alone |
| Q8 | Do we keep `FindingKind.RUN_WITHOUT_PROCESS` as a deprecated alias? | **Keep it, as a report — not as a guard.** This row previously said "delete it (§6)", which was wrong: `RUN_WITHOUT_PROCESS` is returned by `_unfinished_run_finding` (`recover.py:541`), which carries **no action** and is a correct thing to show an operator. The F13 defect is in the *claim-release guard* `_live_holder` (`recover.py:802`) and in `_holder_liveness` (`:671`), and those are what R2 changes. What `RUN_UNRESOLVED_LAUNCH` replaces is the guard's refusal, not this report. If §6 and this row are read together the report survives; do not delete it on the strength of the old §6 wording | old JSON findings naming it are audit records, not live state — which is another reason to keep the enum rather than churn it |
| Q9 | Does `recover` get a `--apply` guard so `RELEASE_CLAIM` cannot be scripted? | keep the existing explicit `evidence` string requirement | none |
| Q10 | Should `MARK_RUN_DEAD` also accept a `launching` run? | **No.** `ABANDON_LAUNCH` is the only action for `preparing`/`launching`, because it carries the job-name evidence that `MARK_RUN_DEAD` does not have | none |

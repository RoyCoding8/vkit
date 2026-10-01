# vkit — status report

Written 2026-09-30. Revised 2026-10-01 against `59a4eca`. Everything here is
measured, not planned; where something is unproven it says so, and where a
number belongs to an earlier revision it names that revision rather than
presenting it as current.

The revision pin on this report was `14e21cb` and its counts were read as
current. Three of them were not current and one did not exist, so they now name
what revision they belong to. The suite row is the honest shape now: a
collection count with no verdict attached.

## Where the work stands

| | |
|---|---|
| Repo | `D:\AI\Poteto's Style`, branch `master`, commit `59a4eca` |
| Suite collected | 646 collected, 0 run. Collected with `--collect-only`; no full execution at this revision. |
| Windows suite passed | Unmeasured at this revision. The last observed full runs were 508 passed, 20 skipped at `14e21cb` and 444 passed, 2 skipped at `5087624`. Neither is a claim about `59a4eca`. |
| Linux (WSL2) suite | No receipt in this repository. `scripts/posix-*.sh` write their reports under the invoking user's home directory and no run artifact is tracked. See the POSIX section below. |
| Acceptance rows | 22/22, observed on this host at `59a4eca` and re-derived on every run of `tests/test_release_docs.py` |
| Release gate | Not a number. `docs/RELEASE-CHECKLIST.md` carries no enumerated gate and this report invented the 26. It carries a gap register and a nine-row may-not-say table, of which four rows read blocked or not verified. |

Uncommitted at the time of writing: a new measurement script and docstring edits
(see "Latest change"). Not yet committed.

## The product

vkit runs registered checks in a Git repository and keeps durable PASS / FAIL /
BLOCKED evidence. The point is that an AI agent can move fast because the
mechanical checking is already done and recorded.

Nine plans in `plans/`, all written. 01–04 implemented and verified. 05's console
exists. 06–08 in progress. 09's release checklist exists and, honestly, **the
release does not pass its own checklist**.

## Latest change: the Linux cancellation limit is now measured, not asserted

### The question

vkit refuses to cancel a run from another process on Linux. The code justified
this with one reason: a Linux process group is just a list of pids, the kernel
keeps the list after its leader dies, so the group outlives whoever made it.

Windows has no such hole because it uses a **job object** — a container held by a
handle, and closing the last handle kills everything inside. Linux has no
equivalent.

### What I added

A second, independent hole, which nobody had measured: **a program that calls
`setsid` leaves the process group entirely.** Any daemonising program does this.
After it, a group signal cannot reach that process at all.

New file: `scripts/measure_posix_escape.py`. One tree, one signal, two
descendants — one that stays in the group, one that calls `setsid`. Both outcomes
observed in the same run, under the product's own `_kill_process_group`, which is
the function `_run_posix` calls on timeout.

Both measurements below are quoted from their scripts, which are committed. The
suite run they belong to is not: `scripts/posix-suite-verbatim.sh` writes its
per-file output under the invoking user's home directory, so nothing about it is
in this repository. That gap is GAP-2, and it is the one number in the table
above with no receipt at all.

Result on WSL2 Ubuntu, kernel 6.18.33.2:

```
child (group leader)    dead=True   after=0.020s
ordinary grandchild     dead=True   after=0.000s
setsid grandchild       dead=False  after=3.003s
census of the group:    none
```

So: containment holds for descendants that stay in the group, and a descendant
that leaves it is not reached. Both reasons now have a committed script, matching
how every neighbouring claim in these files is sourced.

### The measurement caught me being wrong three times

Worth passing on, because the guards are the valuable part:

1. **It killed its own shell.** The probe signalled the process group it was
   standing in. Real supervisors are *outside* the group they signal. The script
   now puts the victim in its own group first.
2. **It raced `setsid`.** It read `/proc` as soon as the pid was written down,
   which is before the child has run its first statement. It concluded no escape
   had happened. An isolated test proved `setsid` works; the measurement was the
   thing at fault. It now waits for the property (a different session id in
   `/proc`), not for the source to have run.
3. **It reported a zombie as alive.** A process that has exited but not been
   reaped keeps a `/proc` entry and still answers `kill(pid, 0)`. So the leader
   read as alive while the census said `Z`. This would have made every
   containment verdict wrong. Liveness now treats a zombie as dead, and the
   leader is reaped.

Each time, a guard I had already written fired and I fixed the measurement, not
the guard. The lesson for whoever maintains this: **trust the witness over the
expectation.** A confident wrong answer here means releasing a job that never
died.

## Things that cannot be done, stated plainly

1. **Cross-process cancellation on Linux.** Not "untested" — no mechanism
   exists. Two independent measurements above. `terminate_owned_tree` refuses and
   says why in its error message. Windows never carries a claim across to Linux.

2. **A `.cmd` launcher with a non-ASCII path.** Batch files are read in the
   active ANSI code page, so arguments mangle. Affects a check registered as
   `.cmd` under a non-ASCII repository path.

3. **Detecting an edit that was made and reverted during a run.** The source
   digest compares before and after; a change that cancels out is invisible to
   it.

4. **A kill that is atomic against pid recycling.** The check "is this still the
   same process" is a read, not a hold. A pid can be handed to another process
   between the check and the next action. Identity verification makes a reattach
   safe to *report*; it cannot make it atomic. Only a job object gives that.

5. **Console layout regression is currently silent.** The fixes live in
   `app.js` and `style.css` and no test pins them. Eight defects were found by
   driving the page by hand; nothing stops a ninth. This is GAP-5's real shape.

6. **Never claimed anywhere, needs its own live session:** a subagent inheriting
   tool access, and a BLOCKED run actually blocking an agent's turn.

## Defects found and fixed (so nobody re-investigates them)

- **Recovery judged a process by pid alone.** Recycled pids read as DEAD and
  released a claim — two workers, one checkout. Now identity is
  `(pid, creation_time)`, and a mismatch reads UNCERTAIN, never DEAD.
- **The capacity pool leaked every slot but the first holder's.** `release` was
  scoped by `task_id` on a table keyed by resource. A decrement was tried and
  measured: it fails 8 tests, because a counter cannot say *which* tasks hold
  slots. `claim_members` now records holders and the count is recomputed.
- **A reassigned task inherited its predecessor's pass.** `compute_readiness`
  filtered on task id, not attempt generation. A retry is a new run, not a
  continuation.
- **A test fixture wrote to the real repository.** A stray `GIT_DIR` was
  inherited by every child process, so the fixture's `git config` wrote
  `core.worktree` into the actual repo and killed git on this machine. The
  fixture now strips those variables.
- **A POSIX wrapper reported success while measuring nothing.** It exec'd
  `python` instead of `python -m pytest`, so every Linux run "passed" having run
  no tests.

## In flight

Nothing. The Windows suite finished green against the edits described above, so
the measurement and the docstrings it backs are consistent with the code.

## Open work, not started

**Task #28 — justify or close the 42 Linux skips.** A test that skips without
saying why is a hole in the evidence, because a reader cannot tell a real
platform limit from a missing tool. This is a demonstrated category here, not a
hypothetical: 8 of the console skips turned out to be a missing tool rather than
a real limitation. A subagent was given this and ran 26 minutes without
committing anything or writing a line of output, so it was stopped. The number
42 is a raw count and has not been explained; treat it as unexamined, not as
"42 known-good skips".

Nothing else is in progress.

## Files worth reading first

| File | What it is |
|---|---|
| `plans/CONTRACT.md` | The product's actual contract |
| `plans/STATUS.md` | Per-plan status, evidence, and every known limit |
| `src/vkit/procs.py` | Process ownership. The Linux/Windows asymmetry, lines 29–50 |
| `scripts/measure_posix_escape.py` | Today's measurement |
| `docs/RELEASE-CHECKLIST.md` | What the release still owes |

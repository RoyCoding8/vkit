# vkit status report

Written 2026-09-30. Repair review updated 2026-10-02. Historical measurements below retain their original scope.

## Where the work stands

Repairs are being integrated on `codex/vkit-recheck-repairs`. Release readiness remains open: fresh host completion gating, protected hosted integration, operator enrollment and a large-worker pilot still need direct verification.

The baseline `76aef82dcb28649bcc076646c9bd0f45293b2d79` passed GitHub CI across three operating systems: Windows 696 passed / 44 skipped, Ubuntu 662 passed / 78 skipped, and macOS 641 passed / 99 skipped; each collected 740 tests. [Baseline CI receipt](https://github.com/RoyCoding8/vkit/actions/runs/36945403170). This corrects the earlier report's claim that POSIX CI was red and Windows had never run. Those results cover the baseline. The repaired implementation passed [three-OS CI](https://github.com/RoyCoding8/vkit/actions/runs/37025668819) at `52fae31`: Windows 713 passed / 44 skipped, Ubuntu 679 / 78, macOS 658 / 99, each with 757 collected. Two Linux-only regressions were no-ops on Windows and macOS at that commit; they executed on Ubuntu. The final follow-up marks those branches as skips and changes documentation, without changing implementation source. See [repair verification](docs/REPAIR-VERIFICATION.md) for scope and follow-up checks.

The acceptance matrix still has 57 rows without specific evidence mappings. A passing suite does not establish the missing live capabilities or those row mappings.

## The product

vkit runs registered checks in a Git repository and keeps durable PASS / FAIL /
BLOCKED evidence. The point is that an agent can move fast because the
mechanical checking is already done and recorded.

Nine plans in `plans/`, all written. Plans 01 to 04 implemented and verified.
Plan 05's console exists. Plans 06 to 08 in progress. Plan 09's release
checklist exists and the release does not pass it.

## Latest change: which Windows errors OpenProcess actually returns

`vkit.procidentity.openprocess_failure_is_gone` rests on one claim. Error code
87 is the only code that establishes no process carries a pid, so it is the
only code a claim release may be founded on. `vkit.recover` once read 6 and
1168 as death too, and a DEAD verdict is a permission to delete a claim row, so
the claim needed a measurement rather than an argument.

`scripts/measure_openprocess_errors.py` is the measurement. It drives
`OpenProcess` over every category of pid a caller can hold and prints the
histogram of codes that come back. The category list is chosen to try to make
the module wrong: pids above the maximum, the sentinels, a reclaimed pid, a
live pid under access masks the caller never meant to send, and thread ids
passed where a process id belongs.

Measured on the development host across roughly 700 probes, exactly three
outcomes occurred. The call opened, or it failed with 5, or it failed with 87.
Nothing else occurred, for a live pid and not for a dead one, so 6 and 1168 are
absent from the reachable set rather than merely rare within it. A code that
appears only under a malformed request is a statement about the request, not
about the pid, and the classifier refuses to read it as death.

That is why the classifier takes the whole code and falls to the safe side
rather than enumerating codes known to be safe. A host where a new code appears
is a host where it owes a decision first.

## The previous change: the Linux cancellation limit is measured, not asserted

### The question

vkit refuses to cancel a run from another process on Linux. The code justified
this with one reason: a Linux process group is just a list of pids, the kernel
keeps the list after its leader dies, so the group outlives whoever made it.

Windows has no such hole because it uses a job object, a container held by a
handle, and closing the last handle kills everything inside. Linux has no
equivalent.

### The second hole, and the measurement

A program that calls `setsid` leaves the process group entirely. Any
daemonising program does this, and after it a group signal cannot reach that
process at all. Nobody had measured this.

`scripts/measure_posix_escape.py` is the measurement. One tree, one signal, two
descendants: one that stays in the group, one that calls `setsid`. Both outcomes
observed in the same run, under the product's own `_kill_process_group`, which is
the function `_run_posix` calls on timeout. The escaper forks after its
`setsid`, so the escape is structural rather than a race the signal happened to
win.

Result on WSL2 Ubuntu, kernel 6.18.33.2:

```
child (group leader)    dead=True   after=0.020s
ordinary grandchild     dead=True   after=0.000s
setsid grandchild       dead=False  after=3.003s
census of the group:    none
```

So containment holds for descendants that stay in the group, and a descendant
that leaves it is not reached.

`src/vkit/supervisor.py::terminate_owned_tree` is where the refusal lives, and
its docstring carries all three measurements: the group reach from
`scripts/measure_posix_group.py`, the escape from
`scripts/measure_posix_escape.py`, and the supervisor's own death from
`scripts/measure_supervisor_death.py`.

All three scripts are committed. The suite run they belong to is not, and the
local POSIX harness that would have carried it is gone.
`.github/workflows/ci.yml` runs this suite on `ubuntu-latest` now, and its
receipt is the forge's run log rather than a file in this repository. That gap
is GAP-2.

### The measurement caught its author being wrong three times

1. **It killed its own shell.** The probe signalled the process group it was
   standing in. Real supervisors are outside the group they signal. The script
   now puts the victim in its own group first.
2. **It raced `setsid`.** It read `/proc` as soon as the pid was written down,
   which is before the child has run its first statement. It concluded no escape
   had happened. An isolated test proved `setsid` works; the measurement was the
   thing at fault. It now waits for the property, a different session id in
   `/proc`, not for the source to have run.
3. **It reported a zombie as alive.** A process that has exited but not been
   reaped keeps a `/proc` entry and still answers `kill(pid, 0)`. So the leader
   read as alive while the census said `Z`. This would have made every
   containment verdict wrong. Liveness now treats a zombie as dead, and the
   leader is reaped.

Each time, a guard already written fired and the measurement was fixed rather
than the guard. The lesson for whoever maintains this: trust the witness over
the expectation. A confident wrong answer here means releasing a job that never
died.

## Things that cannot be done, stated plainly

1. **Cross-process cancellation on Linux.** Not "untested": no mechanism exists.
   Two independent measurements above, plus
   `scripts/measure_supervisor_death.py` for the group outliving its owner.
   `src/vkit/supervisor.py::terminate_owned_tree` refuses and names the real
   reason. Windows never carries a claim across to Linux.

2. **A `.cmd` launcher holding a non-ASCII path in its own bytes.** The trigger
   is the code page, not the character. Batch contents are read in the active
   ANSI code page, so the path reads back as different characters and the check
   never runs. A repository path that is non-ASCII is fine as long as the
   launcher does not repeat it.

3. **Detecting an edit that is made and reverted during a run.** The source
   digest compares before and after, so a change that cancels out is invisible
   to it. `tests/test_identity.py` demonstrates it through the product's own
   report, and the contrast test beside it holds the edit in place and reads
   BLOCKED.

4. **A kill that is atomic against pid recycling.** Checking whether a pid still
   carries the same process is a read, not a hold, and a pid can be handed to
   another process between the check and the next action. Identity verification
   makes a reattach safe to report. It cannot make it atomic. Only a job object
   gives that.

5. **Secret redaction in check output.** The report's `environment` block
   carries no credential and a test says so. A secret the check itself prints
   reaches the logs and is served back to the caller unfiltered, and no
   redaction routine exists to stop it. `formal/RESULTS.md` records the row as
   two clauses with different answers.

6. **Console layout regression is silent.** The fixes live in
   `src/vkit/console/static/app.js` and `style.css` and no test pins them. Eight
   defects were found by driving the page by hand. Nothing stops a ninth. That is
   GAP-5's real shape.

7. **Never claimed anywhere, needs its own live session.** A subagent inheriting
   tool access, and a BLOCKED run actually blocking an agent's turn.

## Defects found and fixed, so nobody re-investigates them

`plans/STATUS.md` carries these with their commits and the measurement behind
each. The three that most changed how the product decides:

- **Recovery judged a process by pid alone** (`2894252`). Recycled pids read as
  DEAD and released a claim, which is two workers on one checkout. `liveness`
  now takes the creation time the run record holds, and a mismatched pair reads
  UNCERTAIN and never DEAD. A pid no process carries is still DEAD, or every
  crashed run's claims would be stranded.
- **A cancellation record with a pid but no creation time** (`7611ada`).
  `cli._identity_of` passed a missing `creation_time` into a field typed `int`,
  so a partial record reported `ownership_lost` about a process that was
  genuinely ours. It failed safe, so nothing was released, but the report was
  wrong at the one boundary whose job is checking the record.
- **A capacity pool leaked every slot but the first holder's** (`2aaab2d`).
  `release` was scoped by `task_id` on a table keyed by resource, so it only
  ever matched the task that acquired the pool first. A decrement was tried and
  measured: it fails eight tests, because a counter cannot say which tasks hold
  slots. `claim_members` now records holders and the count is recomputed.

Two more were found by the same class of mistake, where a test passed without
exercising what it claimed. A fixture guaranteed less than its tests asserted
(`44eb95b`), and `compute_readiness` filtered runs on `task_id` alone, so a
reassigned task inherited its predecessor's acceptance (`b3c52d5`).

## Open work, not started

**Justify or close every skip in the suite.** A test that skips without saying
why is a hole in the evidence, because a reader cannot tell a real platform
limit from a missing tool. That is a demonstrated category here, not a
hypothetical: eight of the console skips turned out to be a missing tool rather
than a real limitation.

An earlier revision of this report put the count at 42 and named the task
number. Neither resolves against this repository, so no number is quoted. The
count has not been examined. Treat it as unexamined, not as known-good skips.

Nothing else is in progress.

## Files worth reading first

| File | What it is |
|---|---|
| `plans/CONTRACT.md` | The product's actual contract |
| `plans/STATUS.md` | Per-plan status, evidence, and every known limit |
| `src/vkit/supervisor.py` | `terminate_owned_tree`, which is where the Linux refusal and all three POSIX measurements meet |
| `src/vkit/procs.py` | Process ownership. The POSIX boundary is stated at the top |
| `scripts/measure_openprocess_errors.py` | This revision's measurement |
| `docs/RELEASE-CHECKLIST.md` | What the release still owes |

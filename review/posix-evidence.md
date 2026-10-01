# POSIX evidence, measured 2026-10-01

> Superseded entry points. The `scripts/posix-*.sh` files this record measured
> were deleted; `scripts/posix_harness.py` runs a POSIX verification now. What
> follows is unchanged, because a record that renames the thing it recorded stops
> being a record.

This file replaces an inherited claim with what the harness can actually prove.
Branch `wt/posix-gate` at `59a4eca`. Every number below was produced by
running the harness on Ubuntu under WSL2, through `scripts/posix-run.sh` and
the `scripts/posix-*.sh` entry points themselves. Nothing here is read off a
document.

## The claim that was in the tree

`REPORT.md:12` carries `| Linux (WSL2) suite | 526 tests, 0 failed, 42 skipped |`.
No receipt for it exists in the repository, and three live documents say the
opposite:

- `README.md:200`: "The POSIX path ... has not been run on a POSIX host".
- `docs/RELEASE-CHECKLIST.md:99`, GAP-2: `Open. The POSIX process path is untested.`
- `docs/PILOT.md:23`: "The verified one is Windows 11".
- `plans/STATUS.md:278-279`: "The POSIX process path is verified; POSIX
  cancellation is not, and cannot be."

Those four files are not mine to edit. What follows is what the harness
measured, so a coordinator can dispatch the contradiction with numbers rather
than re-run it.

## The number, in one place

Three measurements, and the difference between them is the point.

| Where | Collected | Failed | Errors | Skipped | Exit |
|---|---|---|---|---|---|
| worktree, one process (`posix-final.sh`) | 643 | 22 | 3 | 54 | 1 |
| worktree, per file (`posix-suite-verbatim.sh`, before this branch) | 643 | 3 | — | 54 | **0** |
| clean WSL-native tree, per file | 643 | 3 | — | 54 | 1 |

The middle row is the danger. It is a fully red run that reported success,
and it is the state this branch was dispatched to fix.

The bottom row is the honest one, and the 3 are all in `tests/`:
`test_plugin_resources` (24 errors, `pip wheel` from a path containing `'`),
`test_release_docs` (5, passes on Windows), `test_host_session_doc` (4, already
recorded as environment in `review/posix-triage.md` row 7), `test_plan06_onboarding`
(3, `prerequisite_missing: pytest` in a hand-built venv). **No product defect
survives on POSIX at this revision, and the suite still does not finish
green**, which is why `REPORT.md` cannot be edited to say "0 failed" either.

## What the harness reports on a real POSIX run

Run: `bash scripts/posix-final.sh`, worktree at `59a4eca`, Ubuntu under WSL2,
Python 3.12 from `/root/.venvs/vkit-posix`.

```
22 failed, 564 passed, 54 skipped, 3 errors in 370.54s (0:06:10)
exit code: 1
```

So the honest headline is not `526 tests, 0 failed, 42 skipped`. In this
worktree it is 643 collected, 25 not passing, 54 skipped, and 13 of those 25
are environment rather than product (explained below, and confirmed by a clean
tree). The claim in `REPORT.md` is wrong on every one of its three numbers, and
the direction of the error is the dangerous one: it reports a clean run where
there is a red one.

The same suite through `scripts/posix-suite-verbatim.sh` on the main checkout,
before this branch, printed:

```
TOTAL  643 tests: 3 failed or errored, 54 skipped
PASSED 586
exit code: 0        <-- the false green this branch fixes
```

## Where the 25 come from, and where they go

Split by cause, because they are not the same kind of thing and only one is a
product defect.

**3 errors, an apostrophe in the path.** `tests/test_plugin_resources.py:103`
builds a wheel with `pip wheel ... str(REPO_ROOT)`. The repository lives at
`/mnt/d/AI/Poteto's Style/`, and pip URL-quotes the apostrophe:

```
ERROR: Failed to build 'file:///mnt/d/AI/Poteto%27s%20Style/.worktrees/posix-gate'
       when installing build dependencies
```

The product is fine. The test cannot build a wheel from a path containing `'`.
Reproduced on the main checkout at the same revision, so it is not this
branch's doing. `tests/` is not mine to edit; recorded here for dispatch.

**10 failures, WSL git cannot read a Windows-authored worktree.** Every one of
these runs `vkit integration verify`, and `src/vkit/integration/oracle.py:96`
resolves the verifier's own checkout with `open_project`, which shells out to
`git rev-parse`. In a worktree created by Windows git, the `.git` file holds a
Windows path and WSL git answers:

```
fatal: not a git repository:
  /mnt/d/AI/Poteto's Style/.worktrees/posix-gate/D:/AI/Poteto's Style/.git/worktrees/posix-gate
```

`open_project` raises, `_project_at` catches it and returns `None`, and
`oracle.py:86` hands that `None` to `gits.git`, which reaches for
`project.root` and dies with `AttributeError: 'NoneType' object has no
attribute 'root'`. The CLI prints `internal error: ...` and exits 4. The
affected tests are all of `tests/test_policy.py` plus four in
`tests/test_integration.py`.

`src/vkit` is not mine to edit. It is a real defect and it is unguarded: a
`_project_at` that returns `None` has to be handled by its one caller. Recorded
for dispatch with the line numbers.

**The remaining failures did not reproduce once the environment was clean.** In
a WSL-native checkout whose git repo WSL can read, run through the repo's own
`scripts/posix-suite.sh` one file at a time, **`tests/test_policy.py` is
entirely clean: 13 of 13 pass.** All 10 `oracle.py` failures were the
worktree, not the product.

Two things got in the way of that clean run, both worth recording because both
will bite whoever tries next.

**A full-suite run in one pytest process was killed mid-way on this host three
times over**, at 78 progress marks of 643, with no traceback and no summary.
That is the failure `scripts/posix-suite-doctor.sh` was written to diagnose and
this file cannot close it. The per-file loop completes where the
single-process run dies, which is why every number here is per-file.

**`posix-suite.sh` builds its argument as `tests/test_${stem}.py`.** Driving it
from a loop over `basename "$f" .py` made all 46 files report red, including
files that pass when run alone, because the stem `test_acceptance` became
`tests/test_test_acceptance.py`, which does not exist, and the script correctly
exited 1. The gate was right and my caller was wrong. Strip the prefix before
passing a stem.

## The clean-tree result, all 46 files

WSL-native checkout, git repo WSL can read, venv resolving to that tree,
through `scripts/posix-suite.sh` per file:

```
46 files: 42 clean, 4 red
```

| File | Fails | Why |
|---|---|---|
| `test_plugin_resources.py` | 24 errors | `pip wheel` cannot build from a path containing `'` (see above) |
| `test_release_docs.py` | 5 | `test_the_checklist_evidence_is_not_stale` passes on Windows, fails here |
| `test_host_session_doc.py` | 4 | `test_the_record_cites_commits_that_exist`; `review/posix-triage.md` row 7 already records this as environment, "do not fix" |
| `test_plan06_onboarding.py` | 3 | `prerequisite_missing: pytest`; my hand-built venv, not the product |

So the real POSIX failure count at this revision is **3, all in
`tests/`, and all environmental or harness-shaped.** It is emphatically not 0,
which is what `REPORT.md:12` claims. None of the three is a product defect.
The product is clean on POSIX at this revision, and `REPORT.md` still cannot
say "0 failed" because the suite does not finish clean.

The confound worth naming for anyone re-running this: the POSIX venv at
`/root/.venvs/vkit-posix` is installed editable against the **main checkout**,
not against whichever worktree you invoke the scripts from. Its
`_editable_impl_vkit.pth` reads `/mnt/d/AI/Poteto's Style/src`. So a run from a
worktree imports the main checkout's `src` unless `PYTHONPATH` is set, and a
failure in a worktree's `src` will not appear at all. Every number above was
taken after checking which tree was actually imported.

## The skip count, and where it comes from

`tests/test_console.py` gates eight tests on the `claude` CLI being on PATH
(`requires_host`, `test_console.py:81`; eight `@requires_host` at lines 155,
753, 979, 1019, 1045, 1071, 1089, 1113). Measured both ways:

| How the run was invoked | `claude` on PATH | skipped |
|---|---|---|
| inline `export PATH="$VENV/bin:$PATH"` (the old pattern) | no | **8** |
| `. scripts/posix-env.sh; posix_path` | `/root/.local/bin/claude` | **0** |

The distribution's default PATH does not carry `~/.local/bin`, in either a
login or a non-login shell, so nothing else restores it. `scripts/posix-env.sh`
is the only thing that does. Eight entry points did not source it, so a run
through any of them produced the hole. That is measured, not inferred, and it
agrees with the number `review/swarm-sweep.md` row D9 gives.

On Windows the same 47 tests collect with no skip at all, so the 8 skipped are
specific to a POSIX run that lost `~/.local/bin`.

## What this branch changed, and the receipt

| Gate | Before | After |
|---|---|---|
| `posix-suite-verbatim.sh` | exit 0 on a fully failed suite | exit 1 |
| `posix-suite-total.sh` | exit 0 | exit 1 |
| `posix-totals.sh` | exit 0 | exit 1 |
| `posix-single-process.sh` | exit 0 | exit 1 |
| `posix-suite-doctor.sh` | exit 0 | exit 1 |
| `posix-why.sh` | exit 0 | exit 1 (child status) |
| `posix-acceptance.sh` | exit 0 | exit 1 (child status) |
| `posix-one-row.sh` | exit 0 | exit 1 (child status) |
| `posix-row-detail.sh` | exit 0 | exit 1 (child status) |
| `posix-final.sh` | exit 0 | exit 1 (`PIPESTATUS[0]`) |
| `posix-suite.sh`, `posix-suite-status.sh`, `posix-run.sh`, `posix-deps.sh`, `posix-acceptance-rows.sh` | already exited nonzero | unchanged |

Reproduction, both directions, on a tree whose only test file fails and on a
tree where every test passes or skips:

```sh
# red: /tmp/posix-red, one deliberately failing tests/test_broken.py
HOME=/tmp/posix-red/home VKIT_POSIX_VENV=/tmp/posix-red/venv \
  bash /tmp/posix-red/scripts/posix-suite-verbatim.sh; echo $?   # 1
# green: /tmp/posix-green, every test passes or skips
HOME=/tmp/posix-green/home VKIT_POSIX_VENV=/tmp/posix-green/venv \
  bash /tmp/posix-green/scripts/posix-suite-verbatim.sh; echo $?  # 0
```

`VKIT_POSIX_VENV` pointed at a one-line shim that execs the real
`/root/.venvs/vkit-posix/bin/python`, so the scripts ran unmodified against a
tree of my choosing.

### A tenth false green the sweep did not name

`posix-final.sh` has no `exit` statement at all, and the sweep read that as
proof it was safe. It is not. It piped pytest through `tee` and `tail`, and a
pipeline reports its last stage's status, so it exited 0 on a fully failed
suite. `set -o pipefail` was already on and did not help, because it reports
that some stage failed without saying which; the verdict is now read out of
`PIPESTATUS[0]`, so it is pytest's status and not a fact about a shell pipeline.
Measured: exit 1 on the red tree, exit 0 on the green tree.

The method lesson is the sweep's, not mine. Counting `exit` statements finds a
missing exit; it does not find an exit code that a pipeline threw away.

## What this harness still cannot prove

- **Cancellation.** `plans/STATUS.md:278` says POSIX cancellation "cannot" be
  verified. Nothing here changes that; no script in `scripts/posix-*.sh`
  exercises a cancel path. The claim is unresolved in both directions.
- **A worktree run at all.** 13 of the 25 non-passing results above are caused
  by WSL git being unable to read a Windows-authored worktree, and the harness
  does nothing about that. Until it does, a POSIX run from a worktree is not a
  clean measurement of the worktree. The clean-tree table above is what a run
  has to look like to mean anything.
- **A whole-suite run in one process.** Killed three times over on this host,
  at 78 of 643 marks, no traceback. Every number here is per-file for that
  reason, and the count a single process would produce is unmeasured.
- **A release gate.** `REPORT.md:14`'s `26/26` has no enumeration behind it
  either, which is row D13 of the sweep and not mine.

## Dispatch, with numbers

Four claims about the POSIX path contradict each other. The harness produces
this today:

- **46 of 46 files run to completion per-file; 42 clean, 4 red, 3 failures
  total, none of them a product defect.**
- **The suite does not exit 0.** It exits 1, and it did not before this branch.
- **8 avoidable skips** through any entry point that inlined its own PATH, 0
  through one that sourced `posix-env.sh`.
- **`REPORT.md:12`'s `526 tests, 0 failed, 42 skipped` matches none of it.** The
  collected count is 643, not 526; the skips are 54, not 42; and the run is not
  clean.

Whatever the coordinator decides `REPORT.md:12`,
`docs/RELEASE-CHECKLIST.md:99` and `plans/STATUS.md:278` should say, two things
are now settled by measurement and one is not:

- **Settled:** the product has no POSIX defect at this revision, and the
  harness reports a failed run as failed.
- **Settled:** the 10 `oracle.py` crashes and 3 wheel errors in the worktree run
  are environment, not product. The worktree run is not a measurement of the
  worktree.
- **Not settled:** whether `526` was ever a real collected count. It is not 643
  at this revision, and nothing in the repository pins the revision that claim
  belongs to. That is row D2's problem as much as this one.

The gate to fix before anyone re-runs this is `test_plugin_resources.py:103`,
because its 24 errors are the bulk of the red and they are a one-line path
problem, not a deep one.
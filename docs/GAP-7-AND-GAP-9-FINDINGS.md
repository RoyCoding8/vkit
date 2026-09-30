# GAP-7 and GAP-9: what was wrong, and what each fix proves

Written by a worktree worker. The owner owns `plans/STATUS.md` and
`docs/RELEASE-CHECKLIST.md`; this file is the receipt for those two entries and
edits neither.

Every claim below is a measurement on this host: Windows 11, Python 3.13.14,
Git 2.54.0, in worktree `agent-aaf590e67127dff7e`. Nothing here was verified on
any other host or operating system.

## GAP-7: the acceptance script proved less than it appeared to

**What was wrong.** `scripts/acceptance.py` named its command as
`Path(sys.executable).parent / "vkit.exe"`. That is the virtualenv. A virtualenv
shared between workers holds an editable install, and this one does: the main
checkout's `.venv/Lib/site-packages/_editable_impl_vkit.pth` contains exactly
one line, `D:\AI\Poteto's Style\src`. So a worktree's 22 rows could all pass
against the main checkout's modules.

**Measured, not assumed.** Running the pre-fix resolution with this worktree's
`src` removed from the environment lands elsewhere:

```
C:\...\Poteto's Style\.venv\Scripts\python.exe -c "import vkit.cli; print(vkit.cli.__file__)"
-> D:\AI\Poteto's Style\src\vkit\cli.py
   D:\AI\Poteto's Style\.claude\worktrees\agent-aaf590e67127dff7e\src\vkit\cli.py
```

That is `tests/test_acceptance.py::test_the_reverted_resolution_reaches_another_tree`.
It skips rather than fails where the venv happens to hold an install of the tree
under test, because there is then no second checkout to reach.

**The fix.** The rows run `[python, "-m", "vkit.cli"]` with the child's
`PYTHONPATH` pinned to this tree's `src`. `-m` rather than the console script,
because the console script beside the interpreter is a launcher for whatever
`vkit` is installed there, so naming it reopens the gap one layer up.

`main` then spawns a real child, asks where `vkit` resolves, and refuses to report
a count if the answer is not this tree, naming both paths and exiting 4.

**What that guard covers.** The command cannot be diverted from the environment
any more, because the script builds the child's environment itself. What remains
reachable is the script sitting somewhere its own `src` is absent, which a copy
outside the repository reproduces.

**The suite, seven tests, `tests/test_acceptance.py`.**

| Test | Claim |
| --- | --- |
| `test_the_rows_run_this_tree_not_the_installed_one` | the command and the child's path are this tree's |
| `test_a_row_would_import_this_tree` | a spawned child prints this tree's `cli.py` |
| `test_the_script_names_the_tree_it_ran_in` | the count says which tree produced it |
| `test_a_broken_tree_yields_a_failing_row` | a broken example yields FAIL and exit 1 |
| `test_the_reverted_resolution_reaches_another_tree` | the old resolution lands on another tree |
| `test_a_script_with_no_tree_of_its_own_refuses_rather_than_counting` | a detached script refuses with exit 4 |
| `test_no_row_uses_a_command_resolved_outside_the_tree` | no second resolution path, checked by parsing |

`test_a_broken_tree_yields_a_failing_row` is the one that keeps the rest
honest. Without it, "the number means this tree" is indistinguishable from "the
number is always 22".

**Revert checks, each run on this host.**

| Reverted | Result |
| --- | --- |
| `CHILD_ENV` stops pinning `PYTHONPATH` | 5 of 7 fail |
| `COMMAND` back to `vkit.exe` beside the interpreter | 1 fails |
| Both, and the guard deleted (full pre-fix state) | 5 of 7 fail |
| Fix restored | 7 pass; the script reports 22/22 |

**`scripts/acceptance02.py` was checked and does not share the weakness.** It
already did `sys.path.insert(0, str(SRC))` at line 48 with a comment naming this
exact hazard, and its child processes re-insert the same path via
`CHILD_PREAMBLE`. `tests/test_acceptance02.py` already asserted both. It imports
the library directly rather than shelling out to a console script, so there was
no installed entry point to resolve wrongly. No change was needed and none was
made.

**One renames-only consequence.** Row one read "Installed package drives the
example", which asserted something the script no longer does. It now reads "This
checkout drives the example".

## GAP-9 part 1: the `.cmd` launcher limit

**The documented limit was two limits wearing one sentence, and one of them was
not a Windows limit at all.** It says a `.cmd` launcher "cannot carry a non-ASCII
path, because batch contents are read in the active ANSI code page. A check
registered as a `.cmd` under a non-ASCII repository path will mangle its
arguments."

Measuring the four combinations of launcher path (ASCII / space / non-ASCII /
both) against the argument, on the pre-fix code:

| Launcher path | `cmd.exe /c` (pre-fix) | direct (fixed) |
| --- | --- | --- |
| `plain` | argument arrives exactly | argument arrives exactly |
| `plain with space` | **fails**, exit 1 | argument arrives exactly |
| `répertoire ünïcødé` | **fails**, exit 1 | argument arrives exactly |
| `ünïcødé with space` | **fails**, exit 1 | argument arrives exactly |

The argument never mangled. What failed was the *launch*.

**Root cause.** `procs._command_line` wrapped a batch launcher as
`cmd.exe /c <launcher> <args>`. `list2cmdline` quotes the launcher path because it
contains a space. `cmd.exe /c` then strips one layer of those quotes and re-splits
the remainder at the first space, producing
`'C:\...\plain with space' is not recognized as an internal or external command`.

The space is the trigger, not the non-ASCII character. Measured through the
pre-fix `_command_line` and `_application_name` themselves, since a hand-written
approximation of the old shape would not be evidence:

| Launcher directory | pre-fix `/c` form |
| --- | --- |
| `plain` | argument arrives exactly |
| `withspace` | argument arrives exactly |
| `répertoire` | argument arrives exactly |
| `ünïcødé` | argument arrives exactly |
| `with space` | **fails**, exit 1 |
| `répertoire ünïcødé` | **fails**, exit 1 |

So two separate things were true at once. A non-ASCII path with no space worked
fine, which is why the documented limit read as a code-page problem. And a plain
ASCII path containing a space was broken, which no document had recorded. The
documented limit and this defect overlapped on exactly one case, a repository
path with both a space and a non-ASCII character, and that overlap is why the
defect read as the limit for as long as it did.

**This was vkit's own bug, not the documented limit.** The fix is deletion.
CreateProcess already routes a `.cmd` to the command interpreter by itself when
`lpApplicationName` is NULL. So `_command_line` now returns `list2cmdline(argv)`
and `_application_name` always returns None. One authority for launching, the
OS's own, instead of a wrapper that had to be quoted against itself.

Verified on this host across the four paths above after the change: all four
deliver the argument and the working directory character for character.

**The real limit remains, and it is demonstrated.**
`cmd.exe` reads a batch file's own bytes in the active code page, so a
non-ASCII path written *into* the file is read back as different characters.
`tests/test_procs.py::test_a_non_ascii_path_inside_a_cmd_launcher_is_mangled`
forces the console to code page 437 and shows the payload never running.

The test forces the code page because this host's console runs at 65001, where
UTF-8 batch contents are read correctly. That is the honest shape of the limit.
It depends on the code page the user's shell happens to be running, which is
precisely why it cannot be detected from inside a repository.

Measured stderr under code page 437, showing the mojibake and the ASCII tail
surviving:

```
can't open file 'C:\...\repo ├⌐\g├⌐n├⌐rateur.py'
```

**Judgement: fix the bug, keep the limit documented.** The two cases are
different in kind. The launch failure was vkit constructing a broken command
line, so repairing it is deletion rather than a workaround, and a check that
cannot run at all is a defect regardless of whose fault it is. The code-page
round trip is `cmd.exe`'s behaviour on a file vkit never parses, so there is
nothing to repair here. Any mechanism to "detect" it would have to guess the
user's console code page, and a guess that reports a limit that is not present
would block a check that works, which is worse than the silent case.

**What the failure looks like to a user.** A mangled batch launcher exits
nonzero, writes no artifact, and the run is BLOCKED with `artifact_missing`. The
limit is loud. It never reads as a passing check, so it does not need a detector
to keep it from being mistaken for evidence.

**Corrected wording for `plans/STATUS.md`.** The current sentence should say the
non-ASCII path *inside* the batch file mangles, not that the launcher "will
mangle its arguments". The argument is fine.

## GAP-9 part 2: the source digest cannot see a reverted edit

**Demonstrated end to end through the product's own seam**, in
`tests/test_identity.py::test_an_edit_made_and_reverted_during_a_run_escapes_the_digest`.

The check under test is real. It edits the application to return the wrong total,
runs a real interpreter against the mutant, restores the original bytes, and only
then writes a PASS artifact and exits zero.

Observed, asserted by the test:

- `after.inventory_digest == before.inventory_digest`
- `after.dirty` is False
- the outcome is `Passed`, and the scenario observation reads
  `the mutant printed 4 for [1, 2]`
- `report["source"]["inventory_digest"]` equals the before digest, and
  `report["source"]["dirty"]` is False

So the report says PASS, names a clean tree, and the run did not execute the code
the report names. That is the limit, and it is asserted so it cannot quietly
become untrue.

A contrast test sits beside it,
`test_the_run_would_have_caught_the_edit_that_stayed`, holding the same edit in
place and asserting BLOCKED with `source_changed`. Without the pair, a reader
could not tell whether the first result was a defect in the fingerprint or the
deliberate property of hashing content at two instants.

**Judgement: documented limit, now proven. No mechanism.** The owner's read is
right here, and I would not add one.

Detecting this needs continuous observation of the tree while the check runs:
filesystem notification, polling, or a filesystem-level lock. Consider what each
costs. A watcher is a second authority over "what the source was", and it can
be wrong in ways a hash cannot, because it observes events rather than bytes. A
poll at any finite interval misses an edit made and reverted inside the gap, so
it does not actually detect the case it is built for; it only narrows it. A
lock held across the run would prevent the edit rather than detect it, which
changes the product from "verify a tree" to "own a tree", and the product
explicitly does not own checkouts.

There is also a boundary problem that decides it. `identity.py` already declines
to hash secret files, and the stated reason is that a check whose behaviour
depends on a secret's content is not covered. A transient-edit detector would
have the same shape: it watches paths and reports activity, and for a repository
that legitimately rebuilds generated files during a check, it would fire on
activity that never affected the executed code. The detector's false-positive
rate is not a tuning problem, because the natural signal is "a file changed", and
the product needs "the code I ran changed".

So: the honest response is the test. It pins the limit to the product's own
report, so a future change to `identity.py` that claims to close this gap fails
a test that says what the gap was.

**Revert check.** Making `source_unchanged` return False, so the digest appears to
notice everything, fails `test_an_edit_made_and_reverted_during_a_run_escapes_the_digest`
along with two existing digest tests. Restored, all 22 identity tests pass.

## What was not verified

- **No POSIX host.** `_run_posix` was not touched, and nothing here was run
  anywhere but Windows 11. GAP-2 is unaffected by this work and still open.
- **The `.cmd` behaviour was measured at console code page 437 and at the
  default 65001.** No host with a Japanese, Chinese, or Korean legacy code page
  was available, so the mangling was not observed at a code page where an
  ordinary CJK path is unrepresentable. The mechanism is the same one.
- **`lpApplicationName` was measured, not read from documentation.** All four
  launcher paths behave identically whether it is None or names COMSPEC, and
  naming a `.cmd` there fails outright. A host whose CreateProcess differs would
  not be covered.
- **The acceptance script now runs `-m vkit.cli` rather than the installed
  console script.** That is a deliberate change to what the release checklist's
  "verify the acceptance script" step exercises. The row that checked "installed
  package" was renamed rather than kept, because the script no longer tests an
  installed package. Whether the release should also keep a wheel-install check
  is the owner's call; `pip wheel` and the wheel install are still separate
  steps in the checklist and were not touched.
- **`test_the_reverted_resolution_reaches_another_tree` skips** where the
  environment's `vkit` is an install of the tree under test. On this host it
  runs. On a release host holding a built wheel, it would skip, because there
  would be no second checkout to reach.
- **The full suite number is at the commit named in the branch**, not a running
  total. The baseline before any change here was 453 passed, 2 skipped.
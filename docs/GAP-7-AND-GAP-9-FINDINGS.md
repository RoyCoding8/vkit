# GAP-7 and GAP-9: what was found, and where each answer lives

An audit found two defects and one limit. All three were closed in the month
after it was written. This file records what each one was, so a reader who
arrives later can tell a settled question from an open one. The reasoning lives
in the code that implements it, and each entry below names that code rather than
restating it.

Written by a worktree worker. The owner owns `plans/STATUS.md` and
`docs/RELEASE-CHECKLIST.md`; this file is the receipt for those two entries and
edits neither.

Every measurement below was taken on Windows 11, Python 3.13.14. Nothing here
was verified on any other host or operating system.

## GAP-7: the acceptance script proved less than it appeared to

**What was wrong.** `scripts/acceptance.py` resolved its command as
`Path(sys.executable).parent / "vkit.exe"`. That is the virtualenv, and a
virtualenv shared between workers holds an editable install pointing at another
checkout's `src`. All 22 rows could pass against code this revision does not
contain.

**Where the answer lives.** The module docstring of `scripts/acceptance.py`
carries the reasoning and the two lines that implement it: `COMMAND` runs
`[python, "-m", "vkit.cli"]`, and `CHILD_ENV` pins the child's `PYTHONPATH` to
this tree's `src`. `main` spawns a real child, asks where `vkit` resolves, and
returns 4 without printing a count if the answer is not this tree. The choice of
`-m` over the console script is in the comment on `COMMAND`: the console script
beside the interpreter is a launcher for whichever `vkit` is installed there, so
naming it reopens the gap one layer up.

**What it proves.** Seven tests in `tests/test_acceptance.py`. The one that keeps
the rest honest is `test_a_broken_tree_yields_a_failing_row`, without which "the
number means this tree" and "the number is always 22" are indistinguishable.

**The limit that remains.** A copy of the script outside the repository has no
`src` of its own and refuses rather than counting.
`test_a_script_with_no_tree_of_its_own_refuses_rather_than_counting` covers it.
`test_the_reverted_resolution_reaches_another_tree` skips where the environment's
`vkit` is an install of the tree under test, because there is then no second
checkout to reach.

**One consequence, recorded because a rename can look like a regression.** Row
one read "Installed package drives the example", which asserted something the
script no longer does. It now reads "This checkout drives the example". The
release checklist's acceptance step exercises `-m vkit.cli` and not an installed
console script. The wheel build and wheel install remain separate steps.

**`scripts/acceptance02.py` was checked and shares none of the weakness.** It
already inserted `SRC` on `sys.path` with a comment naming this hazard, and its
child processes re-insert the same path. It imports the library directly rather
than shelling out, so there was no installed entry point to resolve wrongly.

## GAP-9 part 1: a `.cmd` launcher under a path containing a space

**What was wrong, and what was not.** The documented limit said a `.cmd`
launcher cannot carry a non-ASCII path, because batch contents are read in the
active ANSI code page. Measured across four launcher paths, the argument never
mangled. What failed was the launch, and only when the launcher path contained a
space. A non-ASCII path with no space worked fine, which is why the defect read as
the documented limit. The two overlapped on exactly one case, a path with both.

**The cause.** `procs._command_line` wrapped a batch launcher as
`cmd.exe /c <launcher> <args>`. `list2cmdline` quotes a launcher path containing
a space, `cmd.exe /c` then strips one layer of those quotes and re-splits at the
first space, and the truncated prefix is reported as an unrecognized command.

**Where the answer lives.** The fix was deletion. `_command_line` returns
`list2cmdline(argv)` and `_application_name` always returns `None`, because
CreateProcess routes a `.cmd` to the command interpreter itself when
`lpApplicationName` is NULL. Both docstrings in `src/vkit/procs.py` carry the
measurement and the reasoning.

**The limit that remains, and it is demonstrated.**
`cmd.exe` reads a batch file's own bytes in the active code page, so a
non-ASCII path written *into* the file is read back as different characters.
`tests/test_procs.py::test_a_non_ascii_path_inside_a_cmd_launcher_is_mangled`
forces the console to code page 437 and shows the payload never running. What
decides the outcome is representability in the active code page, not
non-ASCII-ness: `é` is inside cp1252 and UTF-8 and outside cp437, and a control
test runs the identical launcher under the host's own code page and asserts it
works. Without that control a reader could take the first test for "non-ASCII
paths in a `.cmd` always fail", which is not what happens on this machine.

**Judgement: repair the launch, keep the limit documented.** The launch failure
was vkit constructing a broken command line, so the repair is deletion rather
than a workaround, and a check that cannot run at all is a defect regardless of
whose fault it is. The code-page round trip is `cmd.exe` behaving on a file vkit
never parses, so there is nothing to repair. Any detector would have to guess
the user's console code page, and a guess reporting a limit that is not present
would block a check that works, which is worse than the silent case. The failure
is loud instead. A mangled launcher exits nonzero, writes no artifact, and the
run is BLOCKED with `artifact_missing`.

**The corrected wording for `plans/STATUS.md`.** The limit that remains is that
`cmd.exe` reads a batch file's own bytes in the active ANSI code page, so a
non-ASCII path written *inside* the file mangles. The launcher path and the
arguments do not mangle, and did not, as of `e4634e3`. `9a4a33b` then corrected
the trigger from the non-ASCII character to the space.

## GAP-9 part 2: the source digest cannot see a reverted edit

**What was found.** An edit made to the application during a run and reverted
before the artifact is written leaves the report saying PASS over a clean tree,
while the run executed code the report does not name. `identity.py` hashes the
tree at two instants, and the edit is inside the window between them.

**Where the answer lives.** `tests/test_identity.py::test_an_edit_made_and_reverted_during_a_run_escapes_the_digest`
demonstrates it end to end through the product's own seam, and
`test_the_run_would_have_caught_the_edit_that_stayed` holds the same edit in
place and asserts BLOCKED with `source_changed`. Without the pair a reader could
not tell a defect in the fingerprint from the deliberate property of hashing
content at two instants. The limit is stated in the `identity.py` module
docstring, which `plans/CONTRACT.md` requires of stated limits.

**Judgement: proven limit, no mechanism.** Detecting this needs continuous
observation of the tree while the check runs, and each option costs something
specific. A watcher is a second authority over "what the source was" and can be
wrong in ways a hash cannot, because it observes events rather than bytes. A
poll at any finite interval misses an edit made and reverted inside the gap, so
it narrows the case rather than detecting it. A lock held across the run would
prevent the edit instead of detecting it, which changes the product from "verify
a tree" to "own a tree", and this product explicitly does not own checkouts.

There is also a boundary problem that settles it. `identity.py` already declines
to hash secret files, for the stated reason that a check whose behaviour depends
on a secret's content is not covered. A transient-edit detector has the same
shape. It watches paths and reports activity, and a repository that legitimately
rebuilds generated files during a check would trigger it on activity that never
affected the executed code. That false-positive rate is not a tuning problem,
because the natural signal is "a file changed" and the product needs "the code I
ran changed".

So the honest response is the test. It pins the limit to the product's own
report, and a future change to `identity.py` claiming to close this gap fails a
test that says what the gap was.

## What was not verified

- **No POSIX host.** Nothing here was run anywhere but Windows 11.
- **The `.cmd` limit was measured at code page 437 and 65001 only.** No host
  with a Japanese, Chinese, or Korean legacy code page was available, so the
  mangling was not observed where an ordinary CJK path is unrepresentable. That
  case is inferred rather than observed here.
- **`lpApplicationName` was measured, not read from documentation.** A host whose
  CreateProcess differs would not be covered.
- **The suite counts in the original draft are not carried here.** They were
  per-commit and measured before this file's fixes landed. A count taken from a
  narrative rather than re-measured on its own tree is unverified, which is what
  that draft said about its own numbers.
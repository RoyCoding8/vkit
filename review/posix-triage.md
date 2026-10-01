# POSIX triage, verified 2026-10-01

**This is a historical triage, re-verified against current master.** It is a
record of what a POSIX host agent found on 2026-09-30 at baseline `d198e88`, and
of what each finding measures on master `2c91096` now. The findings are not
presented as live defects. Four of the eight are already fixed, and the fix for
each is named so a reader can check it rather than take this file's word.

The original triage lived at `tmp/posix-triage.md`. `tmp/*` is gitignored
(`.gitignore:19`, with only `!tmp/research/` and `!tmp/source/` excepted), so the
only record of a real POSIX run was one `rm` away from being lost. That is why
this file exists. It is committed under `review/` for exactly that reason: it is
evidence, not documentation.

Verification method, so the status column can be trusted. Every entry was
re-read at the file and line named, and every claim about a test's outcome was
produced by running that test on this host, not by reading it. Where a finding
was fixed, the fixing commit is named and `git show <sha> -- <file>` shows the
change. Baseline `d198e88` is an ancestor of `2c91096` (verified with
`git merge-base --is-ancestor`), and 156 non-merge commits separate them, so
"moved since the triage" is measured rather than assumed.

| # | Finding at baseline | Status at `2c91096` | Fixed by |
|---|---|---|---|
| 1 | `tests/test_cli.py` asserts the constant `windows_job_object` | ALREADY FIXED | `01d9521` |
| 2 | `tests/test_mcp_stdio.py` asserts the same constant | ALREADY FIXED | `01d9521` |
| 3 | `tests/test_procs_posix.py` fixture shells out to `taskkill` | ALREADY FIXED | `01d9521`, then `efee154` |
| 4 | `tests/test_storage.py:149` treats `C:/...` as absolute | ALREADY FIXED | `01d9521` |
| 5 | `console.cancel_check_run` leaks `UnsupportedPlatform` | ALREADY FIXED (source) | `01d9521` |
| 6 | `test_formal_correspondence` fails on Windows, not platform | ALREADY FIXED | `ea6319b` |
| 7 | `test_host_session_doc` fails in a worktree | NOT A DEFECT, environment | do not fix |
| 8 | `test_plan06_onboarding` strips git along with node | ALREADY FIXED | `01d9521` |

One of the eight was never a defect, one is fixed but unmeasured, and one defect
in the same area survives in a different file. Those are below the table.

## 1 and 2. The provenance constant

The triage was right about the schema and wrong about the file it would live in
later. `schemas/run-report.v1.json:84` does admit both values, so
`ownership` is a fact about where the process ran and not a contract vkit
controls. Both sites now compare against the host's own value:

- `tests/test_cli.py:27` defines `EXPECTED_OWNERSHIP`, used at line 114.
- `tests/test_mcp_stdio.py:51` defines the same, used at line 345.

Both derive it from `sys.platform` at import time rather than from the value the
report carries, which is the correct direction: the test says what this host
should have produced, then compares. The commit message for `01d9521` claims the
new form "fails on either drift direction, which the previous literal could not
do: it passed on Windows for the wrong reason". That is accurate, and it is
checked rather than argued: negating `EXPECTED_OWNERSHIP` fails both tests.

One correction to the triage's own framing. It reported these as failing
assertions. They were not failing on Windows, which is where they were run. The
defect was that they passed for the wrong reason on the host that ran them, and
would fail on the other host for a report that was right. Same fix, different
diagnosis than the triage gave.

## 3. The `taskkill` fixture, which the brief asked me to check

**ALREADY FIXED, and then fixed harder.** The brief's read was correct.

`tests/test_procs_posix.py:33` now carries a module-level

```python
pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="this harness reaches the POSIX branch by stubbing, and its kill stub "
           "shells out to taskkill; on a POSIX host the real path is covered by "
           "tests/test_procs_posix_real.py",
)
```

and `_force_posix` no longer shells out. `fake_kill` now calls `os.kill` directly
and the file's own docstring records why. Two commits did this in order, which is
worth recording because the first fix was incomplete:

- `01d9521` replaced `subprocess.run(["taskkill", ...])` with a direct
  `os.kill`, which is what removed the `FileNotFoundError`.
- `efee154` ("Establish the POSIX process identity as (pid, start time, boot
  id)") added the module-level `pytestmark`, which is what stopped the file from
  pretending to cover the real POSIX path from a Windows host.

So the defect named in the triage is fixed, and the reason `plans/R5-GAPS.md`
W1 calls this file "worse than skipped" is also addressed: the gate is no longer
inverted. The file is honestly Windows-only and says so.

The triage's mechanism was correct and is worth repeating because it is the kind
of thing that reads as a flaky test: `taskkill` raised `FileNotFoundError` inside
`_kill_process_group`, the product swallowed it as a benign race, and the run
reported a launch failure instead of a timeout. The fixture was masking the
regression it existed to pin.

## 4. The absolute-path test

`tests/test_storage.py:182` now builds the path from the host's own anchor:

```python
absolute = str(Path(sys.executable).anchor or os.sep) + "escape.json"
```

which is `D:\escape.json` here and `/escape.json` on POSIX. The triage's
diagnosis was right: `str(Path("C:/Windows/System32/config"))` is a relative
filename to a POSIX resolver, so `resolve_artifact` joined it onto the run
directory, resolved it inside, and correctly refused nothing.

## 5. The uncaught `UnsupportedPlatform`

**The source fix is in, at `src/vkit/console/operations.py`, and the triage's
reasoning about it was correct.** `01d9521` added `UnsupportedPlatform` to the
`except` beside `CannotConfirm` in `cancel_check_run`, with a comment saying both
mean ownership is unprovable and take the same path.

**But the named test site no longer exists, and nothing covers the fix.** This is
the one finding from the triage that is fixed in source and unmeasured, and it is
the item I would escalate.

The triage named `tests/test_console.py::test_cancel_reports_the_core_reason_when_there_is_no_process`
as the test that should catch it. Measured: that test does not exist. The
`cancel_check_run` body at `operations.py:446` also no longer contains the
`read_identity` call the triage described; a grep for `read_identity`,
`UnsupportedPlatform`, and `CannotConfirm` across the module returns nothing, and
the function's own docstring says "**This module no longer reads the identity or
decides anything about it.**" R2 (`ef1aed2`, "Make MCP and the console one
authority, not one, over a run's identity") moved identity handling out of the
console entirely.

So the boundary the triage found no longer has the leak, for a different and
better reason than the one it was fixed for. The specific behaviour the commit
message claims is verified — "Reverting each covered behaviour reproduces its
failure: ... the console boundary" — is not reproducible against current code,
because there is no console identity read left to revert. **The claim that this
specific `except` clause is covered is now stale.** Nothing observable regressed;
a guarantee moved rather than vanished. But a receipt that cites a test which no
longer exists is exactly the failure shape R5 exists to catch, so it is recorded
here rather than left to be discovered again.

## 6. The correspondence test, which failed on Windows too

The triage is right that this was not a platform defect: hypothesis had never
been installed on the Windows host, so `tests/test_formal_correspondence.py`
skipped its whole module via `importorskip` and the property test had never run
there. Both its tests are `@given`, contradicting the file's own claim that two
non-Hypothesis tests below the importorskip still ran.

`ea6319b` added hypothesis to the test extra and fixed what the property test
found on its first execution. The counterexample in the triage,
`[('record','w1','unit','rev1','FAIL'), ('crash','w1')]`, is the one the commit
message names: "the counterexample was two steps: record FAIL, then crash", with
the reference model wrong, because `acceptable` ignored its generation argument.

Measured on this host: `2 passed in 127.46s`. Hypothesis is now installed here,
which is itself the point — the test went from never having run to running.

## 7. The worktree git pointer, which is an environment fact and must not be fixed

The triage was correct and its instruction ("The test is correct; the worktree
pointer is not portable. Do not 'fix' it") still holds. `tests/test_host_session_doc.py:117`
is unchanged. This worktree's `.git` is a file pointer holding a Windows path,
`gitdir: D:/AI/Poteto's Style/.git/worktrees/r5d-refusals`, which a Linux git
cannot resolve. Measured on this host: `7 passed in 0.51s`. The defect is
invisible here by construction, which is why it can only ever be fixed by not
fixing it.

## 8. The onboarding PATH strip

`tests/test_plan06_onboarding.py:306-340` now records the shared-directory case
explicitly. Where node and git live in one directory, `_shim_directory_for`
supplies a `git` that execs the real one by absolute path, and the probe asserts
both halves of the claim: `probe_node == "NONE"` and `probe_git != "NONE"`. The
triage's diagnosis was exactly right, including that Windows separates them so the
original stripping was a no-op there and this only ever failed on Linux.

Measured: `1 passed, 11 deselected`.

## The one that is still wrong: two diagnostic scripts call a signature that no longer exists

Not in the triage. Found while verifying item 5, and reported here because
`scripts/` is owned elsewhere and this record should not lose it.

`cancel_run`'s signature is now
`(store: Store, run_id: str, *, requested_by: str = "cli")`. The
client-supplied `identity` parameter was removed on purpose; the docstring says
why, and the reasoning is sound (a second authority for a fact the record already
holds). Three call sites were updated. Two were not:

- `scripts/diagnose_cancel_posix.py:75` and `:88`
- `scripts/verify_posix_cancel.py:120`

All three pass `identity=ProcessIdentity(...)`. Measured:

```
TypeError: got an unexpected keyword argument 'identity'
```

Both scripts are wrapped in `except BaseException`, which prints a traceback and
carries on, so they do not crash. They do something worse: **they report a false
negative as a finding.** `verify_posix_cancel.py:129` records
`check("cancel_run returned rather than raising", False, ...)`, so a script whose
job is to prove the product does not raise would print a FAIL for a `TypeError`
raised by the script's own stale call. A reader cannot tell that from a real
product defect.

The parameter removal is `207a2fa`; the last commit to either script is `df29caa`,
which predates it.

### The required change

Drop the `identity=` keyword at all three sites and pass nothing. The run's
identity now comes from the record, which is the entire point of the change, so
the scripts must stop trying to supply one. Concretely:

- `scripts/diagnose_cancel_posix.py:75-77` and `:88-90`: delete the `identity=`
  argument. **The two probes then test the same case and the script loses its
  point.** The "cannot match" probe and the "matches the bystander" probe were
  distinguished only by the identity passed. To keep both, the mismatch has to be
  created in the record instead of the call: `store.mark_running` already writes
  `creation_time`, so a second run registered with a deliberately wrong
  `creation_time` (as the first half of this same script already does, at line 62,
  with `"creation_time": 0`) is what produces `ownership_lost` under the current
  signature.
- `scripts/verify_posix_cancel.py:120`: delete `identity=stranger_identity`, and
  delete the now-unused `stranger_identity` construction at lines 115-119. The
  recorded `creation_time` at line 106 already supplies the mismatch: it is set
  from the live process, so to make it a stranger the record needs a
  `creation_time` that is off by one rather than correct.

Both scripts also import `ProcessIdentity` from `vkit.procidentity`, which is
still correct for `verify_posix_cancel.py` (it uses `read_identity` too) and
becomes unused in `diagnose_cancel_posix.py` if the calls go as above.

I have not made this change. `scripts/` is not mine.

## What the status column above does not tell you

The triage found nine things. Six are fixed, one was never a defect, one is a
structural fact about worktrees that must stay broken, and one is a fix whose
named receipt has since rotted. That is a good outcome for the code and a poor
outcome for the record, which is the pattern `plans/R5-GAPS.md` names when it
says the repair wave "wrote the *narrative* of correctness more reliably than it
verified it". Finding 5 is that pattern exactly: the fix is real, the commit
message is accurate about what it did, and the sentence about verification is now
unfalsifiable because the thing it verified is gone.

The original triage remains at `tmp/posix-triage.md` on the machine that produced
it. This file is the version that survives.
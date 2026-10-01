# R5h: three advertised guarantees, measured against master `30b2679`

The repair mandate for this wave was that a condition with no receipt is not met.
This file closes the other half of the same sentence: an advertised guarantee with
no implementation has to become visible, and then be either built or explicitly
refused. One of the three is now covered by a test. Two are not implemented and are
not going to be, and the reason is recorded here so nobody has to rediscover it.

Measured at commit `30b2679` on this branch's base. Every claim about a file is a
line reference re-read at that commit. Every claim about a test outcome was
produced by running it on this host.

## Row 1. Secret redaction: REFUSED, not built

### The row

`tmp/research/KIT_ACCEPTANCE.md:67` reads "Secret appears in command output | Apply
the project's redaction rules before sharing evidence; do not dump full environment
variables". Two clauses. The second is implemented and tested. The first is not
implemented at all.

### What exists

The second clause is `execution._environment_facts`, asserted by
`tests/test_storage.py:361` `test_a_reported_environment_carries_no_credential`. It
records exactly three keys, and the test asserts the key set literally rather than
asserting the absence of a substring alone, so a report that recorded nothing
would fail it. `integration/oracle.py:100` `environment_identity()` is the same
choice in the integration path, for the same reason, and its docstring names the
failure it avoids.

So the matrix row is half implemented and half unimplemented, and the
`test_a_reported_environment_carries_no_credential` docstring at
`tests/test_storage.py:376-381` already says so in its own words, pointing here.
That note is accurate. What follows is why the other half should not be built.

### Why there is no boundary to put it at

A redaction boundary has to be a point every byte passes through. There is no such
point, and the reason is structural rather than a matter of effort.

The child process writes the log files itself, through an inherited file
descriptor. There are two platform paths and both bypass Python entirely after the
launch:

- POSIX, `src/vkit/procs.py:421`: `open(stdout_path, "wb")` hands the descriptor to
  `subprocess.Popen(stdout=out)`. The kernel does the writing.
- Windows, `src/vkit/procs.py:749`: `_open_inheritable` plus
  `STARTUPINFO.dwFlags = STARTF_USESTDHANDLES`. `CreateProcess` does the writing.

Any filter placed after the child exits would have to read the whole file back and
rewrite it. `scripts/acceptance.py:363` already pins a check that writes 5 MB to
each stream and asserts the file is exactly 5,000,000 bytes, so a rewrite would
have to reproduce arbitrary binary output exactly, and the cost is no longer
bounded by the child's own write. `procs.run_command`'s docstring at
`src/vkit/procs.py:346` states the design intent outright: "the bytes on disk are
the child's, verbatim, whether it wrote 4 KB or 4 GB. This function buffers
nothing on either stream."

Filtering at write time would mean piping the child's output through a parent-side
relay, which contradicts that contract and would put an unbounded buffer in the
parent, on both platforms, for every check.

### Why "what counts as a secret" cannot be derived here

The brief asks what counts as a secret, and prefers a declared list over a
heuristic. There is no declared list to read one from. `CheckSpec`
(`src/vkit/manifest.py:72`) has no redaction field, `schemas/manifest.v1.json`
sets `additionalProperties: false` at the top level and carries no such key, and
`schemas/run-report.v1.json:118` gives `logs` exactly two properties, `stdout` and
`stderr`, as names.

`identity.py`'s `_SECRET_NAMES` and `_SECRET_SUFFIXES`
(`src/vkit/identity.py:52-65`) are the closest thing, and they are about the wrong
axis. They name *paths* whose content is never opened, which is a statement about
what the source fingerprint excludes. They are not secrets-in-output, and reusing
them would produce a filter keyed on a concept that does not answer this question.

A substring heuristic is the alternative and it is worse than nothing. "What counts
as a secret must not be guessable from a substring of arbitrary output" is the
brief's own constraint. A heuristic that scans for high-entropy runs, or for
assignments shaped like `KEY=value`, cannot tell a real credential from a base64
digest in a test fixture, and it will redact the artifact contents that a FAIL
report exists to show a human. A filter that mangles evidence is a worse failure
than an honest gap, because the reader can no longer trust what they are looking
at.

### Why the row's own wording points somewhere else

`plans/CONTRACT.md:57` is the actual requirement, and it is narrower than the
matrix row: "Keep raw logs locally with a documented retention policy; shared or
exported output follows declared redaction rules."

Two things follow. The scope is the *sharing* step, not the write step. And
"declared" means the project declares them. There is nothing to declare today:
there is no export, share, or publish-a-report surface in the product. The CLI's
subcommands are doctor, project inspect/enroll, features, check run/start, run
show/cancel, task begin/finalize, recover, mcp serve, integration verify
(`src/vkit/cli.py:935-1016`). Nothing in the tree writes a report anywhere outside
its run directory. `run show` prints a published report from disk and never leaves
the machine.

So the requirement is scoped to a boundary that does not exist yet. Building
redaction now would put a filter on a boundary that has no traffic, in front of a
manifest that cannot declare a policy, using a heuristic the brief already told me
not to trust. That is the half-built redaction that would be worse than a refusal.

### The shape of a correct fix, when the boundary exists

Recorded so this is actionable rather than merely declined.

1. Add a redaction policy to the manifest as a declared list of literal values or
   of env var names to resolve at run time. The schema change is a manifest change,
   so it is a schema change, and it needs review of its own.
2. Apply it once, at the moment an artifact leaves the run directory. One
   function, called by the exporter, that every shared-output path must go
   through. Not a per-reader filter in the console and the MCP tool and the CLI.
3. Keep raw logs on disk unfiltered, per CONTRACT.md:57, and let the exporter be
   the only place they are touched.
4. Test it against a check that prints a declared secret, and test that an
   undeclared high-entropy string survives, so the test cannot pass by redacting
   everything.

Item 1 is a schema change, and `schemas/**` is outside this worker's scope. That is
an independent reason the work cannot land here even if it were wanted.

## Row 2. Interrupted report write: the swap step was NOT covered. It is now.

The brief is right, and the gap was worth closing because it turned out to be a
real defect rather than only a missing test.

`publish` (`src/vkit/storage.py:903`) does four things in order: stage the report,
fsync it, claim the row, swap the temp into place. R5d's
`test_an_interrupted_report_write_leaves_no_acceptance_record` (`tests/test_storage.py:229`)
induces ENOSPC at `fsync`, which is the last call inside the `try`. The swap was
the one step the sequence performed afterwards that nothing covered.

Measured before the fix, with `os.replace` raising `OSError(28)` at the then-current
`storage.py:949`:

```
raised: OSError [Errno 28] No space left on device
run_dir contents: ['report.json.25716.tmp']
report.json exists: False
load raised StoreError: no published report for run r1
run_status: {'lifecycle': 'terminal', 'result': 'PASS'}
```

The first two lines and the `load` refusal are correct behaviour, and they are
what `publish`'s docstring means by "absent evidence, never a fabricated PASS".
The third is the defect. The staged file had already been written in full and
fsynced, so what survived is a complete, schema-valid report sitting in the run
directory under a temp name, on a row that is already terminal. Nothing would ever
publish it and nothing would ever remove it. The cleanup at the then-current `storage.py:946` is keyed on the `try`, and the
swap was outside it. After the fix both live inside it, at `storage.py:955` and
`storage.py:957`.

### The fix

The swap moved inside the existing `try`, so the existing `unlink` covers it. One
line of control flow, no new branch, no new state, no fallback. The docstring
records why, including the distinction that keeps this from over-reaching: a
raised failure cleans up, a crash leaves the temp, and the crash case is exactly
what makes `terminal_run_without_report` (`src/vkit/recover.py:691`) a state
recovery can see and report rather than a silent hole.

The alternative considered and rejected: catch the failure and roll the row back
out of terminal. That would be a second authority over a fact the row already
records, it would need its own compensation path, and it would make the
claim-then-swap window disappear from recovery, which is a real, named, useful
state. The window is the design. The debris was not.

### The test

`tests/test_storage.py::test_a_failed_report_swap_leaves_no_acceptance_record`.
It raises ENOSPC at `storage.os.replace`, which is the real kernel error at the
real line, and asserts what a reader observes rather than the exception class:
`load` refuses; the row is terminal with a result and no `report_path`; recovery
reports exactly `terminal_run_without_report`; and the run directory is empty.

The last assertion is the one that failed before the fix, and it is the one the
brief's warning is about, so it is worth being explicit about what it is not. It
would not pass if `publish` were deleted, because the preceding assertions would
fire first, and the mutation run below confirms it does not.

## Row 3. Agent teams: CONFIRMED OUT OF SCOPE

Confirmed, and the correct outcome is a record rather than code.

`KIT_ACCEPTANCE.md:52` carries "Agent teams enabled" as a matrix row whose own
acceptance condition is "Dedicated compatibility tests pass before this mode is
advertised". Nothing advertises it. `KIT_RESEARCH.md:236` says "Agent teams remain
experimental ... Add support when a peer-to-peer workflow needs it and its
acceptance tests pass." Both clauses say the same thing: the row is a placeholder
for a future gate, not a claim about the product.

Measured: no occurrence of anything team-related in `src/vkit/**`, `tests/**`,
`schemas/**`, or `scripts/**`. The only occurrences are the two research documents
above and a passing mention of subagent limits in `KIT_RESEARCH.md:220`.

Nothing is broken here. The risk would have been building a team-claiming
mechanism to satisfy a row that describes work not yet started. That is the
advertised-guarantee failure in its purest form, and the fix is the sentence in
this section.

## Mutation verification

Every test written this wave was checked by breaking the code it tests.

**Mutation 1, revert the fix.** Moved `os.replace` back outside the `try`,
restoring the exact pre-fix shape.

```
FAILED tests/test_storage.py::test_a_failed_report_swap_leaves_no_acceptance_record
  AssertionError: the staged report outlived the failed swap: ['report.json.38892.tmp']
1 failed in 0.60s
```

**Mutation 2, delete the function.** Replaced the entire body of `publish` with
`return None`, which is the vacuity trap the brief names: a test that would still
pass if the function under test were deleted is worthless.

```
10 failed, 11 passed in 8.66s
FAILED tests/test_storage.py::test_an_interrupted_report_write_leaves_no_acceptance_record
FAILED tests/test_storage.py::test_a_failed_report_swap_leaves_no_acceptance_record
FAILED tests/test_storage.py::test_a_terminated_store_keeps_the_report_readable
FAILED tests/test_storage.py::test_report_for_a_different_run_is_refused
... 6 more
```

Both publish tests are among the ten, so neither is vacuous. The source was
restored after each mutation and the suite re-run green.

## Counts

```
tests/test_storage.py                                    21 passed in 9.01s
tests/test_storage.py tests/test_procs.py tests/test_recover.py
  tests/test_cli.py tests/test_acceptance02.py            97 passed, 2 skipped in 43.59s
tests/test_mcp.py tests/test_console.py
  tests/test_integration.py                               79 passed in 103.76s
```

`tests/test_formal_correspondence.py` was skipped; it takes 155s and nothing read
by it was touched. `tests/test_execution.py` named in the brief does not exist;
execution coverage lives in `tests/test_integration.py` and `tests/test_acceptance02.py`,
both of which were run.
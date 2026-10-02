# Release checklist

Historical release reference with revision-bound regression assertions.
It is not the current implementation queue or a current release verdict.
Use the root master plan for current work and the repair-verification record
for later receipts. This document retains historical regression inputs.

Run every command in this document yourself and paste the output into the
release record. A step you did not run is a step you did not do. The release is
described by what ran, never by what this checklist says can run.

The commands here are the ones that exist in this repository at revision
`2f1749e`. `tests/test_release_docs.py` resolves every command below against
`build_parser()` and the working tree, so a step that stops existing fails the
suite instead of quietly misleading a maintainer. It also resolves every file
this document cites as evidence *at that revision*, so a pin left behind HEAD
fails the gate rather than describing a build nobody can check out. That check
caught this pin itself: it named `de96390`, fifty-nine commits back, at which
three of the cited files did not exist. When the test fails, the document is
the wrong thing to edit. Change the code, or delete the step.

These are the files this document relies on. The gate checks each one.

```text files
# Command and entry point sources.
src/vkit/cli.py
src/vkit/mcp/__init__.py
plugin/.mcp.json
plugin/hooks/hooks.json
plugin/scripts/vkit_hook.py
plugin/.claude-plugin/plugin.json

# The MCP transport and the client that drives it over the wire. The client
# imports neither `mcp` nor `vkit`, which is what makes it evidence.
tests/test_mcp_stdio.py
tests/mcp_client.py

# GAP-3's receipt, and the driver that produced it. The session transcript is
# the evidence; the driver is how a reader would re-run it.
docs/HOST-SESSION.md
tests/test_plugin_host.py

# The harness, the examples, and the acceptance walker. The walker carries its
# own tests, which are where GAP-7's evidence lives.
scripts/acceptance.py
tests/test_acceptance.py
examples/python-cli
examples/node-cli

# The boundaries the schemas and the identity digest are enforced at, and the
# tests that call both.
src/vkit/identity.py
tests/test_identity.py
tests/test_r2_ownership.py

# GAP-9's two proven limits, and the findings document that separates the bug
# that was fixed from the limit that remains.
tests/test_procs.py
docs/GAP-7-AND-GAP-9-FINDINGS.md

# What proves the plugin's resources survive packaging, and the one half of the
# schema claim it does not cover.
tests/test_plugin_resources.py

# GAP-2 and GAP-6's named test files, so a row that totals their counts is
# backed by the files it adds up rather than by a guess at which files it meant.
tests/test_enroll.py
tests/test_integration.py
tests/test_plan06_onboarding.py

# GAP-2's POSIX entry points. The harness returned the exit status of the
# process it ran, so a verdict was a status and not a number parsed out of
# output, which was the property the shell scripts lacked. Both are deleted
# now, and `.github/workflows/ci.yml` runs this suite on ubuntu-latest in
# their place, so the row below cites the workflow rather than a script. The
# property the row turned on holds in CI too: pytest's exit status is the
# verdict a job reads, and no number is parsed out of its output.
.github/workflows/ci.yml

# The boundary schemas, and the test that loads them from the package.
schemas/manifest.v1.json
schemas/check-artifact.v1.json
schemas/run-report.v1.json
src/vkit/schemas.py
tests/test_claims.py

# The suite's skips, Plan 08's property tests, and this document's own gate.
# The correspondence file skips at import time without hypothesis, so it is
# listed here rather than described only in prose: a path this document names
# but no block checks is a path nothing verifies.
tests/test_procidentity.py
tests/test_formal_correspondence.py
tests/test_optional_toolchain.py
formal/reference.py
tests/test_release_docs.py
README.md
```

These paths are named in this document because they do not exist. The gate
fails if one of them appears, which is the moment a gap closes.

```text absent
# vkit's own repository is not an enrolled project, so it has no manifest and
# `vkit doctor` reports exactly that. The gate checks this stays absent, which
# is the moment it stops being true.
verification/manifest.json
```

```text files
# GAP-8's acceptance matrix, tracked under `docs/`. Its 59 data rows
# are unmapped, which is what GAP-8 now says, and the mapping the plan asks
# for has not been produced.
docs/ACCEPTANCE-MATRIX.md
docs/PILOT.md

# GAP-2's POSIX record. The local POSIX entry points this row used to name were
# deleted, and with them the two review/ notes that recorded what a local POSIX
# run produced. Those were session scratch under a directory .gitignore keeps
# out of every clone, so a `text files` block could never have named them: a
# reader checking out any revision of this repository finds no such file. What
# exercises the path now is the workflow, and what is not verified is a receipt.
.github/workflows/ci.yml
scripts/measure_posix_escape.py
README.md
```

```text absent
# The POSIX notes the block above used to name. They are session scratch under
# a directory .gitignore excludes, so no checkout of this repository has them
# and no reader can open them. Declared absent so the gate checks the moment one
# of them is committed, which is the moment the row above would be citing
# evidence a reader can actually re-check.
review/posix-triage.md
review/posix-evidence.md
```

CI runs this suite on ubuntu-latest, windows-latest and macos-latest, so the
POSIX path is exercised by a job rather than by a script in this tree. What a
reader cannot check from a checkout is whether that job has ever passed, because
its receipt lives on the forge and not in the repository. GAP-2 records that
distinction rather than collapsing it.

## Read the verdict first

This build does not pass its own checklist. The table at the end of this
document carries the blocked rows. The open gaps name the ways this release can
be wrong in front of a user while every test in this repository is green.
GAP-1, GAP-3 and GAP-4 are closed and say which receipt closed them. The rest
are open and say what is missing.

| Verdict | Meaning |
| --- | --- |
| blocker | A step that fails makes the release unsafe. Fix it or do not ship. |
| verified | The command ran on the stated host and produced the stated result. |
| not verified | The command exists and is believed to work, but was not run. |
| not run | The command cannot run on this build. The gap names the reason. |

A maintainer reading a green test suite has verified the code that exists. The
gaps below name the code that does not exist yet.

## The gap register

Each gap carries the evidence that produced it. Delete a gap only when the
work that closes it has run and left a receipt.

| Gap | Status and what is unverified | Evidence for the claim |
| --- | --- | --- |
| GAP-1 | Closed. The `mcp` SDK stdio adapter was executed. | `mcp` 2.2.0 installed and `serve_stdio` run as a real subprocess. `tests/test_mcp_stdio.py` drives it with hand-written JSON-RPC frames. |
| GAP-2 | Open. The POSIX code path has been exercised, and no run of it is recorded in this tree. | POSIX-CLAIM: no-receipt, derived by `tests/test_release_docs.py` from what this tree holds and not from anything written here. What is verified is the path, not a receipt. A reader of this checkout can open `scripts/measure_posix_escape.py` and its three siblings, which are tracked measurement scripts a POSIX host runs to produce numbers, and `.github/workflows/ci.yml`, which runs this suite on `ubuntu-latest`, `windows-latest` and `macos-latest` and takes pytest's exit status as the verdict. What no reader can check is the run itself. A CI job's receipt is the forge's run log rather than a file in this tree, so nothing here records that a POSIX run has ever passed, and the record of the local WSL run that exercised this suite was written to gitignored scratch outside the repository. The eighteen shell entry points that run used are gone, and so is the Python harness that replaced them. One failure is a real limit of a hosted POSIX runner rather than a gap in this repository: `test_a_recycled_pid_carries_a_new_start_time` needs `unshare --fork --pid`, which hosted runners refuse with `Operation not permitted`. That is why GAP-2 stays open rather than closing on the existence of the workflow. A reader who wants the position in one sentence should read `_posix_position_derived_from_the_tree`, which is where it lives; this row does not restate it, because a restatement is a second thing that can be wrong. |
| GAP-3 | Closed. A live Claude Code host session installed the plugin, attached the six tools, and delivered three hook events. | `docs/HOST-SESSION.md` quotes the session: Claude Code 2.1.285, driver `tests/test_plugin_host.py`, 16 tests, 57s. Every test in that driver skips rather than fails when the host cannot run, so the receipt is the transcript, not a red suite. |
| GAP-4 | Closed. The `mcp serve` subcommand exists and both halves of its surface have run. | `vkit mcp serve --project . --json` lists the six tools, exit 0, and `tests/test_mcp_stdio.py` drives that server over real JSON-RPC frames, which is GAP-1's receipt. This row used to say `serve_stdio` had still not been driven by a client while GAP-1's row said it had; the two contradicted each other in the same table. |
| GAP-5 | Open. The setup console is incomplete. | Plan 05 is the last build step and is still being built in parallel. |
| GAP-6 | Open. Guided enrollment and integration verification exist, but a pilot operator has no finished flow to drive them. | `vkit project enroll` and `vkit integration verify` are defined in `src/vkit/cli.py`. The 31 collected tests behind them are named rather than totalled, because a total with no filenames behind it can only be checked by guessing which three files were meant: `tests/test_enroll.py` (9), `tests/test_integration.py` (8), `tests/test_plan06_onboarding.py` (14). What is missing is the surface, which is GAP-5. |
| GAP-7 | Open. The acceptance script cannot prove what it would prove from a foreign tree. | The rows used to run the console script beside `sys.executable`, which in an environment shared with another checkout is an editable install pointing at that checkout. They now pin the child's `PYTHONPATH` to the `src` tree under test, and `main` asks a real child where `vkit` resolves, exiting 4 when the answer is not the `src/vkit/cli.py` beside the script. The script names the tree it tested on its first line. `tests/test_acceptance.py` runs it against a deliberately broken copy of this tree and asserts the count falls and the defect row is reported, and `test_a_script_with_no_tree_of_its_own_refuses_rather_than_counting` exercises the exit 4 guard. What stays unproved is the old resolution itself. `test_the_reverted_resolution_reaches_another_tree` is the test that would show the pre-fix expression reaching a second checkout, and it skips on this host because this environment's `vkit` is an editable install of the tree under test, so there is no second checkout for it to reach. A release host holding a built wheel skips it for the same reason. |
| GAP-8 | Open. The acceptance matrix exists and 57 of its rows have no receipt. | `docs/ACCEPTANCE-MATRIX.md` is tracked and holds 59 data rows across six sections. Its own preamble marks 2 as observed, so those two have receipts and the other 57 do not. A row is unmapped when no receipt names it, so this gap closes on evidence rather than on the arrival of the file, which already happened. The mapping `docs/PILOT.md` asks for, to an executed test, an optional capability, or an explicit unsupported boundary, has not been produced. |
| GAP-9 | Open. Two documented limits are proven rather than detected. | Part 1 is a `.cmd` launcher whose own bytes the active ANSI code page cannot represent, so a non-ASCII path written inside the file reads back as different characters. `tests/test_procs.py` forces code page 437 and asserts the payload never runs, and a control test runs the identical launcher under this host's own code page and asserts it works. The trigger is the code page, not the non-ASCII character. The launch bug that hid behind this limit is fixed: `procs._command_line` no longer wraps a batch file in `cmd.exe /c`, so all four launcher paths deliver their arguments, and the corrected wording is in `docs/GAP-7-AND-GAP-9-FINDINGS.md`. Part 2 is an edit made and reverted during a run, which escapes the source digest. `tests/test_identity.py` demonstrates it through the product's own report and asserts the digest is unchanged and the outcome still reads PASS, with a contrast test beside it holding the same edit in place and asserting BLOCKED. Both parts are demonstrated and neither has a detector, because a watcher would be a second authority over what the source was and a poll cannot catch an edit inside its own gap. |
| GAP-10 | Open. The parts of the host question GAP-3's session did not reach. | A subagent inheriting the six tools, and a BLOCKED run stopping a real turn, need their own live sessions. No test in `tests/test_plugin_host.py` drives either. |

The first word in a gap row's status cell is the row's verdict, and
`tests/test_release_docs.py` holds a status for every gap here. Editing a row to
say the opposite of what the gate holds fails the suite, which is why GAP-3's
row had to be rewritten rather than left to disagree with `HOST-SESSION.md`.
That test previously checked that the tokens `validate` and `host session`
appeared somewhere in this file, which both the stale row and a closed receipt
satisfied; it read the document's vocabulary rather than its claim.

GAP-3 and GAP-10 are one question asked at two depths, which is why they are
two rows rather than one row with a hedge in it. The session closed what it ran:
install, component load, MCP connection, six tools, three hook events. It did
not reach a subagent, and it did not put a BLOCKED run in front of a real turn,
and neither is inferred from the part that did run.

GAP-4 was a missing command and no longer is. It was the one a user would have
hit first: `plugin/.mcp.json` starts the server with `vkit mcp serve
--project`, and `build_parser()` had no such subcommand, so a fresh install of
the plugin started a host that could not reach the tool surface. The subcommand
now exists, and both halves of that surface have since been exercised against
each other, which is the receipt for GAP-1: the six tools and the stdio
transport both run on this host.

What no test here covers at the time was the third participant. Nothing in this
repository used to start Claude Code and ask it to load the plugin. That is what
GAP-3 was, and it is now closed by the session in `HOST-SESSION.md`: the host
installs the plugin, connects the server, attaches the six tools, and delivers
hooks. The protocol suite and a host session answer different questions, and
only one of them needed running.

## Build the artifacts

Every command below was run on Windows 11 with Python 3.13.14 at revision
`2f1749e`. The wheel size and the protocol run were measured at that revision.
The suite is reported as a collected count because no full execution of
`tests/` was run at it.

```console command
$ uv venv --seed <venv>
$ <venv>/Scripts/python.exe -m pip install .
$ <venv>/Scripts/python.exe -m pytest tests/
$ <venv>/Scripts/python.exe scripts/acceptance.py
```

The wheel builds from the same revision.

```console command
$ <venv>/Scripts/python.exe -m pip wheel . --no-deps -w <dist>
```

On this host the wheel build printed
`Created wheel for vkit: filename=vkit-0.1.0-py3-none-any.whl size=260697`,
rebuilt at revision `2f1749e`. The size is what makes this a receipt rather than
a claim that the wheel builds, and `tests/test_release_docs.py` checks it has a
byte count without checking the value, because a wheel's size moves with the
source it packages.
`pip` fetches the build backend into an isolated environment, so this command
reaches a package index. A release host that blocks network access cannot run it.

## Verify the test suite

Install the test extra first. Without it the suite is still green, and that is
the problem this section exists to make visible.

```console command
$ <venv>/Scripts/python.exe -m pip install -e ".[test]"
$ <venv>/Scripts/python.exe -m pytest tests/
```

Collected on 2026-10-02 for the repair branch: `797 tests collected`. That is the collected count, not a
passed count. Full execution is delegated to GitHub CI; no full local suite was run during this repair. A
passed count carried forward from a revision where fewer tests existed would be a
count measured about a different tree.

Collection is the number that can be measured without a fifteen-minute run and
without claiming any test passed, so it is the number that is kept current.
`tests/test_release_docs.py` recomputes it on every run of this gate and fails
when the two disagree, so this line cannot drift the way an inherited number
drifts.

What collection does not say is the part a reader most wants. A test that
collects and skips is collected. A test that collects and fails is collected. A
test whose module fails to import is not collected at all, which is a count that
quietly shrank. Two of those three read as a smaller number rather than as a
defect, which is why an unrun suite reports its collection total with no verdict
attached.

The skips this suite can take are named rather than counted, because a skip
count is not a rounding error. Two tests that never run and two that run and
pass are the same green. One skip is the POSIX-only case in
`tests/test_procidentity.py`. The other is a module-level
`importorskip("hypothesis")` at the top of
`tests/test_formal_correspondence.py`, which meant Plan 08's property tests had
never executed on a host that lacked the package while the suite still read
green. The test extra carries `hypothesis`, so the step above runs them; a bare
`pip install vkit` still skips, which is what CONTRACT.md requires of formal
tooling. `tests/test_optional_toolchain.py` is the receipt that this skip
behaves, because it blocks the import in a child and asserts the module skips
rather than erroring.

That count is evidence about the code in this repository, and only that. It is
not evidence about the gaps below, because the tests that would cover them are
either stubbed, skipped, or not written.

## Verify the acceptance script

```console command
$ <venv>/Scripts/python.exe scripts/acceptance.py
```

Observed on this host: `22/22 acceptance rows pass`.

GAP-7 was this number proving less than it appeared to. The rows used to resolve
the command under test from the console script beside `sys.executable`, so in a
checkout sharing a virtualenv they exercised an editable install pointing at
another tree. They now pin the child's `PYTHONPATH` to the tree under test, and
`main` asks a real child where `vkit` resolves, refusing with exit 4 when it is
not this tree. The script names the tree it tested in its own output.

The rows now run the module rather than the console script, which means this
step no longer exercises the installed entry point. The next step does, against
the built wheel, because those are two different things and a release that only
did the first would ship an entry point nobody ran.

## Verify the installed entry point

This is the command a user types. The rows above run the module. This one runs
the script the wheel installs, which is the only thing that proves the packaging
works, because a `pyproject.toml` entry point naming a function that no longer
exists fails only here.

```console command
$ <venv>/Scripts/vkit.exe --help
$ <venv>/Scripts/vkit.exe mcp serve --project . --json
```

Observed on this host: both exit 0, and the second lists the six tools bound to
this project. It prints the catalogue rather than opening the transport, so it
can be read instead of watched.

`doctor` is deliberately not in this list. Run `vkit doctor --project .` and
read the output rather than trusting its exit code. On this repository it exits
3, correctly, because there is no manifest here. vkit's own source tree is not
an enrolled project. A release step that passed on a non-zero exit would be
asserting that this repository is verified, which it is not and does not need to
be.

## Verify the CLI surface

This proves which commands exist. It is the check that catches GAP-4.

```console command
$ vkit --help
$ vkit doctor --project <repo>
```

`vkit doctor` exits 3 on a repository with no manifest, which is the correct
answer for a repository that has not enrolled a check. The exit code table is
in `README.md`.

## Verify the MCP protocol

This is the step that closes GAP-1. The transport is an optional dependency, so
the environment has to ask for it; the protocol suite then drives the shipped
command as a subprocess.

```console command
$ <venv>/Scripts/python.exe -m pip install ".[mcp]"
$ <venv>/Scripts/python.exe -m pytest tests/test_mcp_stdio.py
```

Observed on this host: `25 tests collected` in this file, and `25 passed in
69.96s`. An earlier revision of this document recorded a run of `1 failed, 23
passed` here and named
`test_a_client_that_disconnects_mid_lifecycle_loses_no_evidence` as a flake. That
run did not reproduce, and the collected count is 25 rather than 24 because one
test is parameterised over five explicit ids. Re-run this step before a release
and treat a repeat of that name as a real failure rather than as noise.

The count was the second stale number in this document. It read 23, which
matched neither the 21 module-level `def test_` in this file nor what pytest
collects. The collected count is the one a reader can reproduce with
`--collect-only`. The pinned SDK is `mcp` 2.2.0, and the negotiated protocol
version on the wire was `2025-06-18`.

The boundary here is worth stating exactly. A real client process, one that does
not import `mcp` or `vkit`, wrote JSON-RPC frames to the server's stdin and read
frames off its stdout. It listed the six tools, called all of them, drove
`examples/python-cli` to `READY`, introduced a real defect and got `REJECTED`,
and confirmed a stored `FAIL` still reads as `FAIL` on a second connection. It
proved a malformed frame is survivable, an unknown method returns `-32601`, a
refused request returns `isError: true` rather than a protocol error, an internal
fault returns `-32603` with no verdict-shaped body, a killed server loses no
evidence, a 200 KB log comes back one bounded page at a time, and a missing SDK
exits 5 with a message naming the extra rather than hanging.

What it does not prove is that Claude Code loads the plugin. That is a different
program and a different question, and it was GAP-3. It is closed now, on the
session recorded in `HOST-SESSION.md` rather than on this suite, which is the
whole reason the two are separate records.

The SDK pin matters to a reader. 2.x rewrote the server API this binding is
written against, so a 1.x pin and a 2.x pin are different bindings rather than
two builds of one. `pyproject.toml` names `mcp>=2.2,<3`, and a release inside
that range has not been verified here. The protocol suite is what would catch a
contract change rather than leaving it to a user's host.

## Verify the plugin manifest

```console command
$ claude plugin validate --strict plugin
```

Observed on this host: `Validation passed` for
`plugin/.claude-plugin/plugin.json`.

This proves the manifest parses and its fields are well formed. It does not
prove the plugin runs. It does not start the MCP server named in
`plugin/.mcp.json`, and it does not deliver a single hook. That host question
was GAP-3 and this step never closed it; the live session in
`HOST-SESSION.md` did, and it is a separate command from this one.

## Verify the hook adapter

The hook script is a plain Python entry point and can be run directly.

```console command
$ <venv>/Scripts/python.exe plugin/scripts/vkit_hook.py SessionStart --project <repo>
```

Observed on this host: a JSON object on stdout carrying `additionalContext`
and the three skill names, exit code 0. This proves the adapter starts, reads
its arguments, and emits the documented shape.

It does not prove the host ever runs it, because the payload above is
synthesized here rather than written by a host. Hook delivery was GAP-3, and it
is closed on `HOST-SESSION.md`, which quotes the payload a live host wrote to
stdin for SessionStart, PreToolUse and Stop.

## Verify the schemas

The schemas are enforced at every boundary a check crosses, inside the running
code rather than in a schema test file.

```console command
$ <venv>/Scripts/python.exe -m pytest tests/test_identity.py tests/test_r2_ownership.py
```

The three authoritative schemas are `schemas/manifest.v1.json`,
`schemas/check-artifact.v1.json`, and `schemas/run-report.v1.json`. They are
force-included into the wheel as `vkit/_schemas`, so validation works from an
installed package with no source tree present. `src/vkit/schemas.py` finds them
through `importlib.resources`, which is what makes the packaging the thing being
proven rather than the files merely existing.

Measured on this host, installing the wheel built above into a clean
environment and running from a directory holding no source tree: the schemas
resolve out of the installed package's `_schemas` directory, a manifest with no
checks is rejected, and a run report and a check artifact that are missing their
required fields are each rejected with the field named. That was an ad-hoc
probe rather than a gate, so a reader cannot re-run it from this document
alone.

Two boundaries carry a test that calls `src/vkit/schemas.py` directly, which is
where the gate stands today. `tests/test_r2_ownership.py` rejects a report that
does not match `RUN_REPORT`, and `tests/test_identity.py` validates the report
the run writes. `tests/test_plugin_resources.py` proves the plugin's own
resources survive packaging by building and installing a wheel, and it does not
look at `vkit/_schemas`. No test asserts that the three schema files are inside
the wheel, so a packaging change that dropped them would pass every gate in this
suite and fail only at a user's first malformed document.

## What the release may and may not say

| Claim | Verdict | Why |
| --- | --- | --- |
| Runs a registered check and records a durable outcome | verified | `scripts/acceptance.py`, 22 of 22 rows. |
| Installs from a wheel and validates its schemas outside the source tree | verified | Measured by hand at this revision: the wheel installed into a clean `<venv>` holding no source tree resolved the three schemas out of the installed package, and rejected a manifest with no checks, a run report missing `run_id`, and a check artifact missing `scenarios`. Those boundaries are enforced in the running code and exercised from the checkout by `tests/test_r2_ownership.py` and `tests/test_identity.py`, which both call `src/vkit/schemas.py`. No test asserts the wheel ships the files, so the packaging half is a recorded observation rather than a gate. |
| Exposes six MCP tools to an agent | verified | Six tools listed and called over real JSON-RPC frames on a real subprocess, `tests/test_mcp_stdio.py`. |
| Installs into Claude Code as a plugin | verified | A live host session installed it and recorded it enabled, `HOST-SESSION.md`. The receipt is a transcript; the driver skips rather than fails where the host cannot run. |
| Supplies the plugin's MCP server | verified | The host connected `plugin:vkit:vkit` and the model called a vkit tool through it, `HOST-SESSION.md`. The health check a session disagrees with is recorded there too. |
| Holds a run in a subagent's turn | not verified | A subagent inheriting the six tools, and a BLOCKED run stopping a real turn, are GAP-10. The session that closed GAP-3 drove neither. |
| Works on macOS or Linux | blocked | Baseline three-OS CI passed at `76aef82`; the repair branch needs its own receipt. Ordinary suite results do not establish every POSIX process capability. GAP-2. The process path itself has been exercised on a POSIX host, but the receipt lives outside the repository. |
| Enrolls a repository through a user flow | blocked | The command exists and the console operation exists, but no finished flow walks an operator through it, GAP-5 and GAP-6. |
| Is ready for a pilot | blocked | The enrollment flow does not exist and no host session has run a BLOCKED run through an agent's turn, GAP-5, GAP-6 and GAP-10. See [PILOT.md](PILOT.md). |

## Release gate

Do not release while any row above reads blocked. When the work that closes a
gap lands, run the command that closed it, record the output, and update this
document in the same commit. A gap that closes without a receipt is a gap that
moved.

# Implementation status

All plans are written. Plans 01, 02, and 09 are implemented and verified on
the host named below. Plans 03 and 04 are implemented but carry unexecuted
paths. Plans 05, 06, 07, and 08 are in progress; the console exists but four of
its operations refuse. Counts below are at the revision named in each row, not
a running total — an earlier number is not evidence about a later one.

| Plan | Status | Evidence |
| --- | --- | --- |
| 01 | Implemented and verified on Windows | 22/22 acceptance rows, `README.md` |
| 02 | Implemented; merged to master | Supervisor, claims, recovery. 14/14 acceptance rows in `scripts/acceptance02.py`, plus 17 tests over the harness itself |
| 03 | Tool layer implemented; stdio adapter unexecuted | `src/vkit/mcp/`, six tools tested through `Server.call_tool`; GAP-1 |
| 04 | Plugin packaged as records | `plugin/` manifest validates under `claude plugin validate --strict`; GAP-3 |
| 05 | Console exists as a view over the core; `install`, `repair`, `remove`, `enroll` refuse | `src/vkit/console/`. The rendered page has never been looked at; GAP-5 |
| 06 | In progress | GAP-6 |
| 07 | In progress | — |
| 08 | In progress | — |
| 09 | Release checklist and pilot gate merged; the release does not pass its own checklist | `docs/RELEASE-CHECKLIST.md`, `docs/PILOT.md`, `tests/test_release_docs.py` (10 tests) |


## Build order agreed with the owner, 2026-09-29

Components are built one at a time, then integrated. The setup console is the
final integration, not an early one.

| Step | Plan | Result |
| --- | --- | --- |
| 1 | 02 | Ownership, background runs, cancellation, recovery |
| 2 | 06 | Repository onboarding and feature maps |
| 3 | 03 | MCP tools for agents |
| 4 | 04 | Claude Code plugin, skills, lifecycle hooks |
| 5 | 07 | Combined-candidate checks and parallel workflow |
| 6 | 08 | Optional formal checks |
| 7 | 09 | Release, fresh-session install, pilot |
| 8 | 05 | Setup console: install, enroll, monitor, cancel |

Plan 05 is the last step. It absorbs the former Plan 10, so there is exactly one
setup surface: a local browser console. The owner asked for both a terminal
wizard and a management console; building both would mean two renderers over one
core and the second would rot. Its writable surface is a fixed list, because the
manifest is executable policy that must stay reviewed in a diff. See
[05-setup-app.md](05-setup-app.md).

The console is local-only: loopback bind, refused at the socket if the address is
anything else, single user, no authentication because there is no remote peer.

First release targets Claude Code only. Host-specific code stays behind adapters
so a later host is an addition, not a rewrite, but no abstraction is built for a
host that does not exist yet.

Workers update their milestone row only with actual evidence. Preserve failed
checks and blockers. Add a short entry below for any public interface change,
including affected callers and migration evidence. This file is the
implementation task list, not a runtime state database.

## Plan 01 evidence

Verified on Windows 11, Python 3.13.14, pywin32 via pip, Git 2.54.0.

- `pytest tests/`: 52 passed.
- `scripts/acceptance.py`: 22 of 22 acceptance rows pass against the installed
  console command.
- Defect countercheck: changing `running += amount` to `+= amount + 1` yields
  `rc=1` with five of six scenarios FAIL and expected-versus-actual
  observations; restoring the line returns `rc=0` with all six PASS.
- Containment measured directly: after `TerminateJobObject` a child and its
  grandchild are both dead, while the same grandchild survives a child-only
  kill.
- Clean-environment install: the wheel builds and installs into a fresh virtual
  env outside the source tree, and the full command sequence in `README.md`
  runs there.

## Interface frozen by Plan 01

Later plans must code against these, not against a guess.

| Interface | Where |
| --- | --- |
| `execution.run_check(manifest, check_id, store=..., source=...) -> RunOutcome` | `src/vkit/execution.py` |
| `outcome.Passed` / `Failed` / `Blocked`, `BlockedReason` | `src/vkit/outcome.py` |
| `manifest.parse_manifest(project, run_dir) -> Manifest`, `CheckSpec` | `src/vkit/manifest.py` |
| `identity.compute_source_identity(project)`, `source_unchanged(a, b)` | `src/vkit/identity.py` |
| `procs.run_command(argv, *, cwd, stdout_path, stderr_path, timeout_seconds)` | `src/vkit/procs.py` |
| `storage.Store(db_path)` with `register_run`, `mark_running`, `publish`, `load` | `src/vkit/storage.py` |
| `paths.open_project(path) -> Project` | `src/vkit/paths.py` |

`run_check` is the seam Plan 02's per-run supervisor calls. It never invokes the
CLI, and neither should Plan 02.

Schema version 1 is defined for the manifest, the check artifact, and the run
report, and those schemas are the authoritative boundary. The internal Python
records are not a second schema. The files ship inside the wheel, so validation
works from an installed package outside this source tree.

State lives at `<git-common-dir>/verification-kit/`, never in the working tree,
so two clones of one repository share evidence and retiring a worker's checkout
cannot delete the record of what ran in it. Verified with a linked Git worktree:
removing the worktree leaves the report readable.

## Clarifications to CONTRACT.md

One justified clarification, made during implementation.

**Placeholders.** CONTRACT permits "a small documented set of placeholders only
if needed, such as the run artifact directory and resolved Python interpreter"
without naming them. Plan 01 implements exactly two, `{{run_dir}}` and
`{{python}}`, substituted literally with nothing evaluated and no shell
involved. A check that names neither writes its artifact by a fixed name inside
the run directory. The manifest schema rejects an absolute artifact path and any
name that escapes the run directory.

**Evidence location.** CONTRACT says evidence must survive removal of a worker
checkout. That is implemented by storing state under the Git common directory.
Deleting an entire repository, common directory included, deletes the evidence
with it, and that is a deliberate property rather than a gap: a separate clone
has separate state, and a repository is the unit of ownership.

No other public semantics changed. No caller migration is outstanding, because
Plan 01 is the first milestone to consume these interfaces.

## Plan 09 evidence

Verified on Windows 11, Python 3.13.14, Git 2.54.0, Claude Code 2.1.285. The
revision moves with each merge, so the counts below belong to the commit named
beside them and are not a claim about a later one.

- `pytest tests/`: 244 passed, 1 skipped at the Plan 09 merge. The skip is
  `tests/test_procidentity.py:440`, a POSIX-only case the Windows host cannot
  exercise. After the two defect fixes and the acceptance-harness merge the
  same suite is 320 passed, 1 skipped at `2199355`.
- `scripts/acceptance.py`: 22 of 22 rows pass. GAP-7 qualifies this number.
- `claude plugin validate --strict plugin`: validation passed.
- `plugin/scripts/vkit_hook.py SessionStart`: emits the documented JSON
  response, exit code 0.
- `pip wheel . --no-deps`: built `vkit-0.1.0-py3-none-any.whl`, with the three
  schemas present as `vkit/_schemas`.
- `pytest tests/test_release_docs.py`: 10 passed.

### The release does not pass its own checklist

GAP-1 is that the `mcp` SDK is not installed on this
host, so the stdio adapter in `src/vkit/mcp/__init__.py` has never been
executed. GAP-2 is that the POSIX process path is untested. GAP-3 is that no
live Claude Code host session has installed the plugin or delivered a hook.
GAP-4 was that `build_parser()` had no `mcp serve` subcommand while
`plugin/.mcp.json` named one. That is closed by `f45b825` and restated as the
transport still being unexecuted.

`vkit mcp serve --project . --json` now exits 0 and lists the six tools bound to
the project, so `plugin/.mcp.json` points at a command that exists. What is
still unexecuted is the transport: `serve_stdio` has never been driven by a
client, so no claim of protocol compliance is made. GAP-4 is restated in those
terms rather than deleted, because the manifest pointing at a real command is
not the same thing as that command having run. The six MCP tools are
implemented and covered by `tests/test_mcp.py`, which drives
`Server.call_tool`, the same entry point an SDK adapter forwards to.

`KIT_ACCEPTANCE.md`, which plan 09 asks to map row by row, is absent. See
GAP-8 in `docs/RELEASE-CHECKLIST.md`.

## Defects found and fixed after the plans were marked done

Recorded because a status file that lists only completed work reads as a
product with no known defects, which is the one claim this file must never make
by omission.

**Recovery judged a process by its pid alone** (`2894252`). `recover.py` asked
`liveness(run.pid)` in five places while the run record already carried
`creation_time` and nothing read it. `vkit/procidentity.py` documents at length
that a pid alone cannot answer liveness, because Windows recycles process
identifiers. A pid left behind by an exited run eventually names an unrelated
process, and recovery read that stranger as DEAD and released the claim. That is
two workers on one checkout. `liveness` now takes the creation time the record
holds, and a mismatched pair reads UNCERTAIN and never DEAD, so the claim is
retained. A pid no process carries is still DEAD, or every crashed run's claims
would be stranded forever.

The reason 295 green tests missed it: the test fixture never wrote
`creation_time`, so every test exercised the weaker bare-pid path. A fixture
gap, not a coverage gap.

**A cancellation record with a pid but no creation time** (`7611ada`).
`cli._identity_of` passed `recorded.get("creation_time")` into a field typed
`int`, so a partial record produced `creation_time=None`, which fails every
comparison in `still_the_same_process` and reported `ownership_lost` about a
process that was genuinely ours. It failed safe, so nothing was released, but
the report was wrong and the type contract was broken at the one boundary whose
job is checking the record. Now refused with a message naming the actual
defect. An audit of every other consumer of a run record's pid found
`mcp/_tools.py` and the console already correct.

## Known limits carried forward

- The POSIX process path is untested. Plan 01 was verified on Windows only, and
  Plan 09 must treat an untested other OS as a release limitation.
- A `.cmd` launcher cannot carry a non-ASCII path, because batch contents are
  read in the active ANSI code page. A check registered as a `.cmd` under a
  non-ASCII repository path will mangle its arguments.
- The source digest cannot detect an edit made and reverted while a check runs.
- The check that a pid still names the same process is a read, not a hold. A
  recycled pid can be handed out between the check and the caller's next act.
  Cancellation that must be atomic is a job object keyed to a handle, which is
  what `vkit.procs` creates; identity verification is what makes a reattach
  safe to *report*, and `procidentity` says so in its own docstring.
- There is no surviving supervisor process. `supervisor.start_run` executes in
  the calling process and `execution.run_check` attaches the pid only after the
  command returns, so an in-flight run names no process a second process could
  continue. Measured as row 6 of `scripts/acceptance02.py`, not assumed.


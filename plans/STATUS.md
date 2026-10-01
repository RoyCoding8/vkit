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
| 03 | Tool layer implemented; stdio transport verified on the wire | `src/vkit/mcp/`, six tools through `Server.call_tool` plus 24 protocol tests driving a real subprocess (20 module-level test functions in `tests/test_mcp_stdio.py`, one parameterised over five ids, so 24 collected); GAP-1 closed. `tests/test_mcp.py` collects 24 more and drives `Server.call_tool` directly |
| 04 | Plugin installed and driven in a real Claude Code host | `docs/HOST-SESSION.md` transcript, 16 host tests against a scratch config; GAP-3 closed |
| 05 | Console exists as a view over the core; all four setup operations implemented; page driven and layout repaired | `src/vkit/console/`. Driven with headless Chrome at 1440/1024/768/480/360px; eight defects found and fixed. **The layout fixes live only in `app.js` and `style.css` and no test pins them, so a regression is silent** — that is GAP-5's real shape, and it is stronger than "never been looked at" |
| 06 | In progress | GAP-6 |
| 07 | In progress | — |
| 08 | In progress | — |
| 09 | Release checklist and pilot gate merged; the release does not pass its own checklist | `docs/RELEASE-CHECKLIST.md`, `docs/PILOT.md`, `tests/test_release_docs.py` (32 collected) |


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
| `manifest.parse_manifest(project, run_dir, path=None) -> Manifest`, `CheckSpec` | `src/vkit/manifest.py` |
| `manifest.parse_manifest_bytes(blob, *, project, run_dir, origin) -> Manifest` | `src/vkit/manifest.py` |
| `tasks.compute_readiness(store, task_id, *, required_check_ids) -> ReadinessResult` | `src/vkit/tasks.py` |
| `identity.compute_source_identity(project)`, `source_unchanged(a, b)` | `src/vkit/identity.py` |
| `procs.run_command(argv, *, cwd, stdout_path, stderr_path, timeout_seconds)` | `src/vkit/procs.py` |
| `storage.Store(db_path)` with `register_run`, `mark_running`, `publish`, `load` | `src/vkit/storage.py` |
| `paths.open_project(path) -> Project` | `src/vkit/paths.py` |

Later plans added to this table rather than changing what Plan 01 froze.
`parse_manifest` gained an optional `path` so a proposal can be parsed for
review without ever being readable as policy, and every existing caller is
unaffected. `parse_manifest_bytes` exists so approved bytes read out of a commit
go through the one parser instead of a second implementation free to disagree
about the same rules. `compute_readiness` is listed because the attempt
generation is part of its contract, not an implementation detail: a caller
cannot obtain an acceptance that ignores staleness.

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
  schemas present as `vkit/_schemas`. Rebuilt at `59a4eca` for the size the
  release checklist quotes: 255418 bytes.
- `pytest tests/test_release_docs.py`: 10 passed. At `59a4eca` the same file
  collects 32 and passes 32; the ten was true when this section was written.

At `59a4eca`, which is the revision the release checklist pins,
`pytest tests/ --collect-only -q` collects **644** across 46 files. That is a
collection count and not a pass count. The last full execution at that revision
has not been run, so no passed figure is quoted here. Both stale whole-suite
numbers that used to sit in this file's neighbours were inherited from earlier
revisions, which is what the sentence above the list is warning about.

### The release does not pass its own checklist

GAP-1 is closed. The `mcp` SDK 2.2.0 is installed and `serve_stdio` has been run
as a real subprocess, spoken to with hand-written JSON-RPC frames by
`tests/mcp_client.py`, which imports neither `vkit` nor `mcp` and therefore
disagrees with the server rather than agreeing with it by construction.

The binding was wrong and not merely untested. The code assumed a decorator API
that `mcp` 2.x removed in a rewrite, so `serve_stdio` would not have imported at
all. It was rewritten against the real API rather than the test weakened.

GAP-2 is that the POSIX process path is untested. GAP-3 was that no live Claude
Code host session had installed the plugin or delivered a hook — a different
program from an MCP client that agrees with the server, so GAP-1 being closed
said nothing about it. GAP-3 is now closed for the three surfaces it named:
`docs/HOST-SESSION.md` is the verbatim transcript of a real Claude Code
2.1.285 host installing the plugin, attaching all six tools, and delivering
SessionStart, PreToolUse and Stop. `tests/test_host_session_doc.py` checks that
transcript against the code, so a record that drifts fails rather than reading
as a run someone did. GAP-4 was that `build_parser()` had no `mcp serve`
subcommand while `plugin/.mcp.json` named one, closed by `f45b825`.

`vkit mcp serve --project . --json` exits 0 and lists the six tools bound to the
project, the transport runs, and a real host now attaches all six. Still not
claimed anywhere: a subagent inheriting tool access, and a BLOCKED run actually
blocking a turn. Both need their own live sessions.

Three host behaviours are recorded because each silently disables something.
Installing without `--config` attaches zero tools and reports only `pending`.
`claude mcp list` can print `Status: Connected` while a real session's `init`
reports the same server `failed`, which is why the tests read `init`. And `PATH`
must be in the form `cmd.exe` reads, since Git Bash exports `/d/...` and
`cmd.exe` does not search it.

`mcp` is a pinned optional extra rather than an install requirement, because the
six tools are useful without a transport and a missing optional package must not
break the CLI, the console or the hook. The protocol suite collects 24, plus
`tests/test_mcp.py`'s 24, which drives `Server.call_tool`, the one entry point
an SDK adapter forwards to.

A full-suite run without the extra installed reports the protocol suite as
errors rather than skips. That is a defensible shape — a test that cannot reach
the transport is not testing the transport — but it means a missing optional
dependency reads as a red suite rather than an unmet extra.

`tmp/research/KIT_ACCEPTANCE.md` is tracked, and `plans/09-release-and-pilot.md`
asks for every one of its 59 data rows to be mapped. None is. The mapping is
GAP-8, and the gap is the mapping rather than the file: an earlier version of
this line called the matrix absent, which was false, because the file is tracked
under `tmp/research/` and `git ls-files` returns it.

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

**A capacity pool leaked every slot but the first holder's** (`2aaab2d`), found
by the Plan 07 worker. `claim_holders` has `resource_key` as its primary key, so
a pool is one row with a counter, and `release` deleted that row scoped by
`task_id` — which only ever matched the task that acquired the pool first.
Measured: capacity 3, three holders, `release(t2)` left `held=3`. The
single-holder test passed throughout, because with one holder the first holder
is the only holder.

A decrement was tried and measured before settling: it fails eight tests, three
of which predate that work, because a counter cannot say which tasks hold slots.
A task holding nothing could then free someone else's, and a task releasing
twice could take a slot it never paid for. `claim_members` now records
`(resource_key, task_id, generation)` and `held` is the count of those rows,
recomputed on every release, so a drifted counter cannot be laundered.

Fixing it exposed a second defect before it shipped: `recover.py` deleted from
`claim_holders` directly, which with membership frees the resource key while
the member row survives — a second task acquires an exclusive resource the first
still holds. Recovery now delegates to one shared function.

**A reassigned task inherited its predecessor's acceptance** (`b3c52d5`), found
by the Plan 08 worker. `compute_readiness` filtered runs on `task_id` alone,
never on the attempt generation, so superseding a task handed its successor a
free READY: generation 2 had produced nothing and inherited every pass.

The TLA+ model forbids this in Properties 3 and 4, so the specification and the
implementation disagreed and one of them had to be wrong. CONTRACT.md settles
it — advancing the generation invalidates the previous attempt's authority, and
a retry is a new run linked to its predecessor rather than a continuation of
it. A guard already stopped a superseded attempt publishing a verdict it had
*already computed*; it did not stop a new attempt computing one from the old
attempt's evidence, which is the case that mattered.

**A test fixture guaranteed less than its tests asserted** (`44eb95b`). The
`dead_pid` fixture waited for `liveness` to report DEAD, but DEAD has two
correct shapes: a process that exited while a handle still opens reports its
exit code, and one whose object has been reclaimed reports that no process
carries the pid. The fixture accepted the first; two tests asserted the second.
Roughly one run in four failed.

The first diagnosis was wrong. I read it as pid recycling, and a 60-trial probe
under deliberate load reproduced it zero times. The failure message named both
shapes, and reading it would have answered the question immediately. Two
workers had also reported it as a pid-reuse flake, which made the wrong story
more likely rather than less.

## Known limits carried forward

- The POSIX process path has been exercised on Ubuntu through WSL2, but no run
  of this suite is recorded in this repository, so the claim rests on the
  measurement scripts rather than on a receipt a reader can re-check.
  `scripts/measure_posix_escape.py` and its siblings exist and are committed,
  and `review/posix-triage.md` is the POSIX host agent's own record, but the
  suite run wrote to `$HOME/vkit-posix-reports/`, which is outside the tree.
  The identity is `(pid, starttime, boot_id)` proven against real pid reuse,
  and a process-group signal is measured
  to kill a three-level tree. Two separate measurements say the rest cannot be
  built from a group. A descendant that calls `setsid` is not in the group, so
  the signal cannot reach it: `scripts/measure_posix_escape.py` ran one tree in
  which an ordinary grandchild died within 20ms under the product's own
  `_kill_process_group` while a `setsid` sibling survived, with no running
  process left in the group. And a group is a set of pids the kernel keeps after
  its leader exits, so it outlives the process that created it. A job object
  closes both gaps at once — it is held by a HANDLE rather than by group
  membership, so an escaping descendant is still in the job.
  `terminate_owned_tree` refuses and names that reason, and a Windows claim never
  carries across to POSIX. GAP-2 carries the missing receipt, which is what a
  release would need and this line is not.
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


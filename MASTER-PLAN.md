# Build vkit from this entry point

Status: ready for the implementation worker. The next capabilities are unbuilt.
Work on `main`. Confirm the current checkout and commit before editing.
The implementation baseline is `e211eb2c295bc44dd2f8c279fa5d47498ada2da7`.
The cleanup after it changes documentation, test references, and CI configuration.

## Goal

Extend the existing vkit application with reusable native verifiers, checked
automatic cleanup, and a dashboard that gives the operator direct control.
Keep standalone MCP and the CLI usable across agent harnesses. The Claude
plugin packages its connection, hooks, and optional workflow skills.

The worker is an engineer. Make routine implementation decisions. Reuse the
existing core and verify public behavior. Do not rebuild the application or
treat old worker narratives as evidence.

## Read this, then the current checkpoint

Locate and read both project-specific memories available in the worker's
environment before dividing the work. Record their source paths and verify
their guidance against this checkout. If an expected memory is unavailable,
report that gap rather than inventing its contents.

1. Read [the existing contract](plans/CONTRACT.md).
2. Read [the next contract requirements](plans/NEXT-CONTRACT.md).
3. Read only the detailed plan for the checkpoint below.
4. Trace that plan's named source files and callers. Source and observed behavior
   determine what exists.

[Repair verification](docs/REPAIR-VERIFICATION.md) records the starting evidence
and remaining limits. Historical release and formal measurements are reference
material, not the current work queue. Completed handoffs were removed; Git
history preserves them at the baseline commit.

## Execute in this order

| Step | Detailed plan | Deliverable |
| --- | --- | --- |
| 1 | [Plan 12, checkpoint 12.1](plans/12-operator-console-and-portability.md#checkpoint-121-launch-the-existing-app) | Launch the existing local dashboard with `vkit console` |
| 2 | [Plan 10](plans/10-native-verifiers.md) | Native verifier evidence, registered adapters, and shared acceptance enforcement |
| 3 | [Plan 11](plans/11-checked-cleanup.md) | Verified Python cleanup, guarded application, and fast hooks |
| 4 | [Plan 12, checkpoints 12.2-12.4](plans/12-operator-console-and-portability.md#checkpoint-122-show-the-actual-engineering-state) | Evidence views, validated configuration editing, and portable MCP setup |

Start with step 1. Do not implement the rest of Plan 12 early. Complete and
verify each detailed checkpoint before advancing. The checkpoint order defines
dependencies and integration order. Independent subtasks can run concurrently
under the slot policy below. Do not wait for permission on routine reversible
engineering decisions.

## Keep this task list current

- [x] 12.1: installed dashboard launcher and existing page work.
- [x] 10.1: shared evidence types and a public pytest vertical path work.
- [x] 10.2: Node and property evidence retain their precise scope.
- [x] 10.3: actual Lean and TLC positive and negative cases run in CI.
- [ ] 10.4: approved integration, finalization, and installed packaging are verified.
- [x] 11.1: Python cleanup preview protects directives and executable content.
- [x] 11.2: guarded apply, restricted logic rules, conflicts, and retries are verified.
- [ ] 11.3: hooks and shared verification enforce freshness without mutating protected candidates.
- [ ] 11.4 and 12.2: dashboard displays cleanup and actual task/evidence state.
- [x] 12.3: configuration preview/save preserves pinned obligations and protected policy authority.
- [x] 12.4: standalone MCP works without a Claude plugin or installed skills.
- [ ] Final handback includes exact candidate, CI receipts, negative cases, and unresolved limits.

Three are left unchecked on purpose, not overlooked. 10.4 and 11.3 are built and
merged, but each left a gap in the same file: `integration/verify.py` builds its
acceptance from `policy.compare` rather than `tasks.finalize`, and never calls
`cleanup.hooks.candidate_gaps`. A protected candidate therefore does not yet
weigh evidence kind or obligations, and required cleanup does not yet reject it.
That work is in flight. 11.4 is not a separate checkpoint; it is the console
exposure of 11.3's receipts, which lands with that gap.

After each checkpoint, append a short evidence entry below with its commit,
commands, CI tested SHA, artifacts, and limitations. Mark it complete only from
actual execution. Avoid a second status document with different completion claims.

Break each checkpoint into smaller disjoint subtasks. Record dependencies,
owned paths, acceptance gates, assigned agents, and current status in this
task list. Dispatch a ready subtask whenever a permitted slot becomes free.

## Engineering rules

- Use the existing execution, supervisor, ownership, storage, and acceptance
  paths. Every interface calls the same core.
- Freeze shared types before implementing independent adapters. Migrate
  affected callers together and remove obsolete logic.
- Keep checks registered and typed. The candidate cannot invent its own trusted
  verdict, weaken its obligation, or label tests as a theorem.
- Show the exact claim and assumptions. Unsupported stronger verification is
  BLOCKED. A passing abstract model does not prove arbitrary application code.
- Cleanup requires an independent preservation check. Apply it before source
  capture. Finalization and protected integration must not silently modify source.
- Extend the existing Python console and plain browser assets. Keep its loopback
  and request guards. Do not add a permanent daemon, scheduler, or replacement UI stack.
- Follow current repository guardrails and user instructions. Do not install
  plugins, change global harness settings, call external paid model APIs for
  acceptance, or run a live product pilot just to produce a completion claim.
  Authorized engineering subagents use the worker harness's configured models.

## Verification and Git

Use small focused Python checks locally. Run full suites, heavy formal checks,
and browser acceptance in GitHub CI. Do not use WSL or shell-based test drivers.
Record the SHA actually tested. A green previous revision is not a green current one.

Commit each verified checkpoint on `main`. Push ordinary commits when needed
for GitHub CI. Do not force-push or leave permanent worker branches. Preserve
failed runs and diagnostics as runtime evidence, rather than committing scratch
logs or another report claiming the whole product is complete.

Keep meaningful behavior tests. The ownership acceptance reference is now
[OWNERSHIP-ACCEPTANCE.md](docs/OWNERSHIP-ACCEPTANCE.md). Some historical release
documents are read by regression tests. If a new feature closes a stated gap,
update those assertions and documents honestly. Do not weaken a test merely
to make a new command pass an old absence check.

## Skills and parallel work

Use ponytail and unslop for minimal code and clear writing. Poteto Mode is
engineering guidance. Use prove-it-works for acceptance and blast-radius for
affected callers. Load further skills when their instructions fit the actual task.
Skill directions do not override these limits.

The human has explicitly authorized this implementation worker to use up to
six active subagents. No further spawning confirmation is required for this
build. This authorization supersedes the earlier ask-first and serial-default
clauses for the worker. Use the worker harness's configured models and respect
its actual concurrency limit. If it counts the coordinator as a slot, account
for that rather than claiming six available child slots.

Whenever a slot is available and an eligible subtask is unblocked, dispatch it
immediately. Do not invent busywork to fill all six slots. Blocked dependencies
and overlapping write scopes are not eligible work. The coordinator can work
alongside agents on its own disjoint scope. When only delegated work remains,
wait for it instead of duplicating or editing those agents' unfinished changes.

Use swarm for independent search, coverage, and stubborn debugging. Use
git-worktree-discipline for parallel implementation writers. Every subagent
must read the relevant skills and principles, receive a bounded task with
owned paths and an acceptance gate, and return evidence. One integrator owns shared
manifest, storage, execution, and acceptance files. Freeze the contract before
splitting adapters. Give each writer an explicit file scope and acceptance gate.
Use temporary worktrees, integrate serially, check the combined result, and
remove merged temporary branches and worktrees. Do not edit a worker's unfinished
scope while waiting for that worker.

## Stop and hand back when necessary

If a required tool or isolation capability is unavailable, report the exact
failed check and supported alternatives. Complete independent work, but do not
claim the blocked checkpoint is done or silently reduce its guarantee.

At the end, report the final commit, implemented operations, focused checks,
CI links and tested SHAs, receipts, observed counterexamples, and remaining
live-host or scale limits. The reviewer will audit this evidence independently.

## Checkpoint evidence

Recorded 2026-10-03 from actual execution. Full run-by-run detail is in
`orchestrate/vkit-program/overview.md` under CI history.

### What CI proved, and what it cost to get there

Five suite runs and two formal-verifier runs were needed. Nothing in this
program was verified by more than one platform until run four.

| Run | SHA | Result | What it found |
| --- | --- | --- | --- |
| 37095912191 | ffa2346 | fail, 23 | `TESTED_CPYTHON_VERSIONS` held only 3.13.14 while the runner resolved 3.13.15. The guard refusing an unmeasured compiler was correct; the workflow pin was wrong. |
| 37101072951 | d4308a3 | fail, 11 | Node 22 spells the isolation flag differently; a Windows-authored payload path split with POSIX rules; a symlink guard ordered after containment so its token was unreachable; and a POSIX **false pass** where a Windows absolute path was treated as relative and passed containment. |
| 37102550231 | 15c77e8 | fail, 2 | A test fixture seeded a run with no receipt, and acceptance correctly refused a run that established nothing about what kind of evidence it was. |
| 37108770250 | 616efa4 | **green** | 1143 passed, 80 skipped on Ubuntu; macOS and Windows also green. |
| 37127195856 / 37127367490 | 1c3f817 / eaab533 | fail | The formal workflow's Lean job verified its own install before `GITHUB_PATH` applied, and both jobs pinned a patch release the runner manifest had dropped. |
| 37127367490 | eaab533 | Lean green, TLC fail | Lean: 30 passed on the real kernel. TLC's guard fired correctly, because it named Lean cases inside the TLC job. |
| 37127872623 | 5bed3a6 | **formal green** | Lean and TLC both pass on Ubuntu with real toolchains, positive and negative cases. |

### Verified per checkpoint

- **12.1** — `vkit console --json --port 0 --no-browser` driven outside the
  suite: printed `http://127.0.0.1:53887/`, served a 3934-byte page opening on
  `home`, all four landing panels present, API reads 200, session token
  substituted and absent from the URL. The misleading `readiness` view is now
  `setup` and no longer renders ready/not-ready.
- **10.1** — evidence category is a total function over six keyword-only
  variants with no declared category field. A `lean` check has no
  `required_scenarios` to be empty, so the refusal follows from the shape.
- **10.2** — Node and property receipts list real checked cases. The same file
  declared `pytest` yields `scenario`; only a `property` variant with a
  generator block yields `property`.
- **10.3** — Lean 30 passed and TLC 34 passed on Ubuntu with the real kernel
  and a pinned tla2tools jar. Negative cases fail: `sorry` → `incomplete_proof`,
  an added axiom → a counterexample, a violating model → FAIL with states left
  on the queue.
- **10.4** — a requirement is a frozen typed value carrying its check id,
  evidence kind and obligations, derived at admission. Installed wheel outside
  the checkout runs a verifier with no source dependency.
- **11.1** — 18 directive rules, each verified against a fetched primary source.
  Zero directives wrongly removable and zero ordinary comments wrongly kept.
- **11.2** — `compile('x = 0.0')` and `compile('x = -0.0')` produce identical
  bytecode and `==` says equal; only the constant's exact form separates them.
  Of two logic rules, only trailing `else: pass` can apply automatically.
- **12.2** — five evidence sections render real records. Viewing a page does
  not mutate task state. Four cleanup panels are named as unavailable rather
  than rendered empty.
- **12.3** — one operation that can name no path. Saving a proposal that drops
  four of six obligations leaves the pinned contract byte-identical and
  readiness never improves.
- **12.4** — `shutil.which('vkit')` resolves to a *different* install than the
  venv serving the console, so the panel reads the installed script from
  `importlib.metadata`. Configured and connected are separate facts.

### Unresolved limits, recorded rather than smoothed over

1. `integration/verify.py` builds acceptance from `policy.compare`, not
   `tasks.finalize`, and never calls `cleanup.hooks.candidate_gaps`. A protected
   candidate does not yet weigh evidence kind or required cleanup.
2. The Lean unreviewed profile's **comparator is not discharged**. The schema
   constrains `toolchain.comparator` to a bare string and no evidence settled
   whether it names a binary or a lake workspace.
3. `reviewed_proof_sources` requires Linux isolation; BLOCKED on Windows.
4. Browser acceptance is unverified everywhere: computed contrast, real focus
   rings, narrow-window layout and live-region quality. No browser stack is
   permitted locally and none exists in CI.
5. The AST guard over the console package does not catch
   `Path("verification/manifest.json").write_text(...)`, because the protected
   part lands in the `Path` call rather than the writer call. Pre-existing, and
   measured against the base commit by the checkpoint 12.3 worker.
6. `pyproject.toml` pins `mcp>=2.2,<3` as a float. The SDK stopped serializing
   an `experimental` capability in 2.3.0 and a test was corrected for it.
7. Roughly 80 tests are platform-gated and skip on Windows. The POSIX real-process
   and process-identity tests have only ever executed on the Ubuntu runner.

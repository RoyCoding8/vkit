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
verify each detailed checkpoint before advancing. Continue sequentially while
the requirements can be met. Do not wait for permission on routine reversible
engineering decisions.

## Keep this task list current

- [ ] 12.1: installed dashboard launcher and existing page work.
- [ ] 10.1: shared evidence types and a public pytest vertical path work.
- [ ] 10.2: Node and property evidence retain their precise scope.
- [ ] 10.3: actual Lean and TLC positive and negative cases run in CI.
- [ ] 10.4: approved integration, finalization, and installed packaging are verified.
- [ ] 11.1: Python cleanup preview protects directives and executable content.
- [ ] 11.2: guarded apply, restricted logic rules, conflicts, and retries are verified.
- [ ] 11.3: hooks and shared verification enforce freshness without mutating protected candidates.
- [ ] 11.4 and 12.2: dashboard displays cleanup and actual task/evidence state.
- [ ] 12.3: configuration preview/save preserves pinned obligations and protected policy authority.
- [ ] 12.4: standalone MCP works without a Claude plugin or installed skills.
- [ ] Final handback includes exact candidate, CI receipts, negative cases, and unresolved limits.

After each checkpoint, append a short evidence entry below with its commit,
commands, CI tested SHA, artifacts, and limitations. Mark it complete only from
actual execution. Avoid a second status document with different completion claims.

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
  plugins, change global harness settings, invoke paid models, or run a live
  multi-agent pilot just to produce a completion claim.

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

Work serially by default. Ask the human before spawning subagents, and follow
the model restriction in the current guardrails if allowed. A master plan does
not grant delegation permission.

If parallel work is authorized, use swarm to coordinate disjoint assignments
and git-worktree-discipline to isolate writers. One integrator owns shared
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

No implementation checkpoint above has completed yet.

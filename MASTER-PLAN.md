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

## Project memories, read and verified against this checkout

Both project-specific memories were read in full. Each is a point-in-time
observation, so its claims were checked rather than carried forward:

- `escalate-after-two-tries` (`type: feedback`) — the ladder is two dispatched
  attempts, then a swarm, then an honest negative recorded where a reader will
  find it. It governed this session in three places: the Node/TLC defect that
  took two attempts was escalated to `/swarm` rather than retried; limit 9, the
  protected-integration routing, is written up as a measured BLOCKED with the
  two digests rather than quietly attempted a third time; and the `.worktrees/
  12-1a` removal, which resisted `rmdir`, is reported as unremoved after two
  tries rather than forced.
- `vkit-repair-wave-verification` (`type: project`) — records R1–R4b merged and
  R2/R5 outstanding, and two defect classes that survived worker self-report:
  a suite the worker never ran, and an intermittent Windows socket race that a
  6-attempt regression test passed against. Its operative guidance held. The
  repair baseline it points at is `docs/REPAIR-VERIFICATION.md`, which records
  `e211eb2` and a 757-test collection; this branch collects 1316, so that
  figure is superseded rather than contradicted. `review/AUDIT.md` no longer
  exists in this checkout, so its `review/probe_*.py` gates cannot be run and
  the merged-tip discipline it prescribes was met instead by CI on three
  platforms. Its "write tooling in Python, not shell" ruling is intact: zero
  `.sh` files remain and both workflows are YAML over Python entry points.

One caution the memory itself carries, honoured here: it says a green branch
proves nothing about the merge. That is why every checkpoint in this list was
recorded from a run on the merged tip rather than from a worker's own report,
and why the two audit agents in this session were worth their cost — they found
four errors in this document's own CI table, including two I introduced.

## Keep this task list current

- [x] 12.1: installed dashboard launcher and existing page work.
- [x] 10.1: shared evidence types and a public pytest vertical path work.
- [x] 10.2: Node and property evidence retain their precise scope.
- [x] 10.3: actual Lean and TLC positive and negative cases run in CI.
- [x] 10.4: approved integration, finalization, and installed packaging are verified.
- [x] 11.1: Python cleanup preview protects directives and executable content.
- [x] 11.2: guarded apply, restricted logic rules, conflicts, and retries are verified.
- [x] 11.3: hooks and shared verification enforce freshness without mutating protected candidates.
- [x] 11.4 and 12.2: dashboard displays cleanup and actual task/evidence state.
- [x] 12.3: configuration preview/save preserves pinned obligations and protected policy authority.
- [x] 12.4: standalone MCP works without a Claude plugin or installed skills.
- [x] Final handback includes exact candidate, CI receipts, negative cases, and unresolved limits.

Every checkpoint in this list is now built and merged. What follows records what
each one actually proved, including three places where a worker was briefed
about a seam that did not exist and the work turned out to be already done.

10.4 and 11.3 are closed, but not the way they were briefed. Both briefs named a
seam in `integration/verify.py` and both were wrong. `candidate_gaps` reads
`git status --porcelain`, and `assert_clean` raises unless that status is empty,
so the gate could only ever return nothing inside a protected run; it now reads
`git diff --name-only --diff-filter=ACMR <target>..<candidate>` instead, which
is the only comparison a clean checkout can answer that means something. And
`evidence_kind_downgraded` already refused a category a variant cannot license,
so the evidence-kind half needed no new code. The remaining half, routing the
protected decision through `tasks.finalize`, is BLOCKED and recorded as limit 9.

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

Thirteen suite runs and eight formal-verifier runs were needed. Nine of the
thirteen suite runs failed or were cancelled; nothing in this program was green
on more than one platform until `616efa4`.

Counts below are the real per-platform pytest summary lines, re-read from the
run logs. `fail, N` is the ubuntu count, and the platforms that differed are
named.

| Suite run | SHA | Result | What it found |
| --- | --- | --- | --- |
| 37095912191 | ffa2346 | fail: 48 / 48 / 47 | `TESTED_CPYTHON_VERSIONS` held only 3.13.14 while the runner resolved 3.13.15. The guard refusing an unmeasured compiler was correct; the workflow pin was wrong. |
| 37097709938 | b3cc7fd | fail: 11 / 4 / 10 | The run that found most of the cross-platform defects. Node 22 spells the isolation flag differently, so `node-report.tap` was never written and all 7 Node cases read BLOCKED; a symlink guard named `path_escape` instead of `symlink_or_escape`; a Windows-style payload path did not resolve to its repository-relative form; the MCP handshake asserted `{'experimental','tools'}` where the server now makes `{'tools'}`; and a plugin fixture passed a `SimpleNamespace` with no `project`. macOS failed fewest (4), ubuntu most (11). |
| 37101072951 | d4308a3 | fail: 1 / 1 / 1 | One failure, and it is not the one previously recorded here: `test_no_module_decodes_subprocess_output_with_the_locale_code_page`. The payload-path, symlink and Node defects above were **fixed** by this SHA, not found by it. |
| 37102550231 | 15c77e8 | fail: 2 / 2 / 2 | A test fixture seeded a run with no receipt, and acceptance correctly refused a run that established nothing about what kind of evidence it was. |
| 37108770250 | 616efa4 | **green** | 1143 passed / 80 skipped on Ubuntu; 1120 / 103 on macOS; 1175 / 48 on Windows. The first run green on all three. |
| 37127872586 | 5bed3a6 | fail: 31 / 31 / 31 | Checkpoint 10.3 deleted `manifest.DECLARED_BUT_UNAVAILABLE`; about 25 tests imported it and 5 more hit `KeyError: 'result'`. Identical on every platform, because the code was wrong rather than the host. |
| 37131714102 | 5e7d003 | **green** | Capabilities derived from the adapter table instead of a stale refusal list. 1200 / 89 Ubuntu, 1177 / 112 macOS, 1232 / 57 Windows. |
| 37134443260 | 7496674 | **green** | 1212 / 89 Ubuntu, 1189 / 112 macOS, 1244 / 57 Windows. |
| 37142461769 | b1e7a15 | **green** | The final tip, documentation-only commits: 1227 / 89 Ubuntu, 1204 / 112 macOS, 1259 / 57 Windows. |

Three cancelled suite runs (`1c3f817`, `eaab533`, `ed2a6ce`) plus two more
cancelled by the handback's own pushes (`6758aa6`, `8310263`) are omitted: a
cancelled run records no verdict, and listing it as a failure or a success would
be a claim about a result that does not exist. Those last two were cancelled by
me, not by a failure.

| Formal run | SHA | Result | What it found |
| --- | --- | --- | --- |
| 37127195833 | 1c3f817 | fail | `lean: command not found`, exit 127. The Lean job verified its own install before `GITHUB_PATH` applied, and both jobs pinned a patch release the runner manifest had dropped. |
| 37127367490 | eaab533 | Lean green, TLC fail | TLC's guard fired correctly, because it named Lean cases inside the TLC job. |
| 37127872623 | 5bed3a6 | **green** | Lean and TLC both pass on Ubuntu with real toolchains, positive and negative cases. |
| 37140929908 | 6758aa6 | **green** | Lean 30 passed / 1 skipped; TLC 34 passed, and a second guard step ran the 3 `real` TLC cases with no skip. |
| 37142461762 | b1e7a15 | **green** | The final tip, with the suite run above. Same receipts, which is expected: the intervening commits changed documentation only. |

### Corrections made to this table while writing the handback

Every number above was re-read from the run logs. The first draft of this
section was wrong in ways worth recording, because a status document that is
corrected quietly is a status document nobody can audit:

- `5bed3a6` was recorded as green. Its **suite** run failed 31 tests on every
  platform; only the formal workflow was green at that SHA.
- Two suite runs were absent entirely, including `b3cc7fd`, which found most of
  the cross-platform defects.
- Failure counts were wrong on three rows: `ffa2346` was "23" against a real 48,
  and `d4308a3` was "11" against a real 1.
- **Two of those errors were introduced while fixing the first batch.** The
  Node-isolation, symlink and payload-path findings were attributed to
  `d4308a3` and the false-pass claim was carried forward from an older draft,
  when all four belong to `b3cc7fd`. An independent audit of the committed file
  caught both; I re-read the failing-test lists to confirm before changing them.
- "Nothing was verified by more than one platform until run four" was false as
  written: every run executed all three platforms concurrently from the first.
  What was true is that none was *green* on more than one until `616efa4`.

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
- **12.2** — `test_every_planned_section_is_reachable_and_renders_real_data`
  (`tests/test_console_evidence_views.py:157`) walks the sections, and
  `test_viewing_a_page_does_not_mutate_task_state` (:350) holds that reading is
  not writing. `test_the_cleanup_section_reports_the_policy_and_names_no_absent_panel`
  (:628) asserts `"unavailable" not in section`, and its docstring records why
  that is the stronger assertion: a test asserting the key *equals* an empty
  list would also pass for a section that had deleted the panels along with it.
- **12.3** — `test_saving_configuration_does_not_release_a_pinned_obligation`
  (`tests/test_console_config_editing.py:962`) proves the pinned contract
  survives a save that drops obligations, and
  `test_a_proposal_that_removes_an_obligation_cannot_read_as_a_routine_edit`
  (:874) refuses to let that save pass as routine.
- **12.4** — `shutil.which('vkit')` resolves to a *different* install than the
  venv serving the console, so the panel reads the installed script from
  `importlib.metadata`. Configured and connected are separate facts.

### Unresolved limits, recorded rather than smoothed over

1. `integration/verify.py` builds its acceptance from `policy.compare`, not
   `tasks.finalize`, and the protected decision does not weigh the obligations a
   receipt recorded. The cleanup half IS closed; see 10.4 below.
2. The Lean unreviewed profile's **comparator is not discharged**. The schema
   constrains `toolchain.comparator` to a bare string and no evidence settled
   whether it names a binary or a lake workspace.
3. `reviewed_proof_sources` requires Linux isolation and is BLOCKED on Windows.
   It is weaker than "BLOCKED on Windows" makes it sound. On this branch the
   reviewed profile is exercised in exactly two places, both synthetic:
   `tests/test_lean_verifier.py:523` and `:547` call `interpret()` on a
   hand-built report. Every `@needs_lean` real build runs with
   `LeanProfile.UNREVIEWED`. The Linux job asserts that
   `requires_isolation()` is True and prints "reviewed profile gate: satisfied on
   this runner", which is a statement about the flag, not about a proof. So the
   recheck-difference logic is tested against a fabricated olean digest and
   never against a real second elaboration of a real module. Closing this needs a
   real Lean build under `REVIEWED` in CI, which is real work, not a flag.
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
8. CLOSED at 11.4. The four panels now list records from a real backend:
   migration 7's `cleanup_records` table and `cleanup/records.py` persist every
   outcome — applied, already applied, refused — with the rule id, request and
   proposal ids, generation, policy digest, before/after digests and the
   receipt. The earlier `unavailable` key was deleted rather than left as an
   empty list, because three tests asserted `section["unavailable"] == []`, and
   an assertion comparing a literal to an empty list cannot fail; they now
   assert the key's absence. What remains is a scale limit, not a missing
   backend: `MAX_RECORDS` is 500 per project and the newest are trimmed, so a
   project cleaned more than 500 times cannot list its whole history.
9. Routing the protected integration decision through `tasks.finalize` is
   BLOCKED, measured not assumed. `oracle.repoint_approved` writes the approved
   script's absolute path under `<run_dir>/approved/` and `manifest.digest()`
   covers `argv`, so two executions of one candidate under one policy recorded
   `a8f305c6…` and `fbaf2e57…`. `tasks._identity_gaps` compares that digest
   against the one pinned at admission, so a routed call raises a policy-identity
   gap on EVERY run and refuses every candidate, including clean ones. Shipping
   it would replace "accepts a candidate whose evidence kind it never weighed"
   with "refuses every candidate", which is a broken gate rather than a stricter
   one. A fix belongs at the digest's construction, not at the call site.
10. `verify.py` reaches into `cleanup/hooks.py` privates (`_read_only`,
    `_propose`, `_sites`, `all_lines`, `RULE_ORDER`, `TRAILING_RULE`). A rename
    there now breaks `verify.py`. Precedent exists for this shape, so it was
    accepted rather than redesigned under a deadline.

## Final handback

Recorded 2026-10-03. The candidate is `b1e7a15`, which is `main`, pushed, with
a clean tree. The last commit that changed product code is `6758aa6`; `8310263`
and `b1e7a15` changed documentation only. **Both workflows are green at
`b1e7a15` itself**, so no receipt in this document refers to a superseded
revision.

### What was built

| Step | Deliverable | Where |
| --- | --- | --- |
| 1 / 12.1 | `vkit console` launches the existing dashboard; the misleading `readiness` view is now `setup` | `src/vkit/console/` |
| 2 / 10 | Native verifier evidence, six registered adapters, shared acceptance enforcement | `src/vkit/verifiers/` |
| 3 / 11 | Checked Python cleanup, guarded apply, fast hooks | `src/vkit/cleanup/` |
| 4 / 12.2-12.4 | Evidence views, validated configuration editing, portable MCP | `src/vkit/console/static/`, `src/vkit/mcp/` |

The candidate collects 1316 tests locally. The last fully green suite run
(7496674) collected 1301 on Ubuntu, and this final unit adds 15. Every worker
branch is merged and deleted; `main` is the only branch and the only remaining
worktree.

### Receipts

Suite CI, three platforms, plus the formal-verifier workflow, all at `6758aa6`:

- **Suite run 37142461769 at `b1e7a15`** — green on all three platforms:
  1227 passed / 89 skipped on Ubuntu, 1204 / 112 on macOS, 1259 / 57 on
  Windows.
- **Formal verifiers 37142461762 at `b1e7a15`** — green. Lean 30 passed /
  1 skipped, TLC 34 passed, "reviewed profile gate: satisfied" printed, and the
  3-case `real` TLC guard step ran with no skip. Both toolchains pinned: Lean
  `v4.34.1` exactly, and `tla2tools.jar` 1.7.4 verified against sha256
  `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88` before use.
- The last fully green pair before these was `37134443260` / `37134443279` at
  `7496674`, with 1212 / 1189 / 1244 passed. The prior formal pair at `6758aa6`
  (37140929886 cancelled, 37140929908 success) records why the two earlier
  pushes happened: each one superseded the suite run testing the previous SHA,
  because both workflows set `cancel-in-progress`. Those supersessions cost two
  suite runs and taught the obvious thing late — batch the corrections, then
  push once and let the run finish.

The TLC figure needs stating precisely, because two different numbers are both
true. The adapter file collects 34 cases and all 34 pass. A second step then
runs `-k real`, which selects 3 and deselects 31, and asserts no skip occurred.
An earlier draft of this document said "TLC 34 passed" without that second
figure, which would have hidden how little of the file the real-checker guard
actually covers.

### Negative cases

These are the refusals that matter, and each is a test that passes *because* the
product rejected something. The Lean and TLC lines below are confirmed present
and passing in run 37140929908, not asserted from the test names.

- A Lean `sorry` is refused as an incomplete proof — `test_a_real_sorry_is_refused`
  and `test_a_sorry_declaration_is_an_incomplete_proof`.
- An added, unapproved axiom yields a counterexample
  (`test_a_real_added_axiom_is_a_counterexample`,
  `test_an_unapproved_axiom_is_a_counterexample`), and a theorem depending on
  `sorryAx` is reported as one
  (`test_a_theorem_depending_on_sorry_ax_is_a_counterexample`).
- A real unprovable statement and a missing theorem are both refused
  (`test_a_real_unprovable_statement_is_an_incomplete_proof`,
  `test_a_real_missing_theorem_is_refused`).
- A Lean report carrying no axiom audit is refused rather than accepted
  (`test_a_report_with_no_axiom_audit_is_refused`), as are a report with no
  elaboration, a truncated report, and non-JSON bytes.
- An axiom the check explicitly permits is **accepted**
  (`test_an_axiom_the_check_permits_is_accepted`), so the refusal is a reading of
  the declared policy rather than a blanket rejection of non-empty axiom sets.
- A TLC model violating its invariant FAILS with a counterexample
  (`test_a_real_violating_model_fails`, `test_a_violation_is_a_fail_with_a_counterexample`),
  and a run left with states on the queue is refused
  (`test_a_run_with_states_left_on_the_queue_is_refused`).
- A TLC candidate Java override is refused before the run
  (`test_a_candidate_java_override_is_refused_before_the_run`), so a project
  cannot point the checker at a jar of its own choosing.

Every one of these asserts a **specific** outcome, not merely "not passed". The
Lean refusals pin a typed detail string — `incomplete_proof`, `theorem_absent`,
`report_malformed` — and the elaboration case additionally pins
`BlockedReason.INTERNAL_ERROR`. The axiom cases go through the non-Blocked FAIL
path and pin the counterexample contents: `"cheat"` and `"sorryAx"` appear in
`counterexamples[0]["trace"]`. The TLC violation pins the serialized result
`FAIL`, `satisfied == []`, `left_on_queue > 0`, the text
`Invariant NoDoubleCheckout is violated`, and the exact violating state
`held = [a |-> "w1", b |-> "w1"]`. A test asserting only `not is_pass` would
pass for a product that refused everything for the wrong reason; none of these
do.

`test_the_reviewed_profile_is_refused_without_isolation_and_is_not_downgraded`
is the single skip in the Lean job, and it skips for the right reason: on a host
that isolates a proof build the reviewed profile is legitimately accepted, so
asserting the refusal would assert the wrong thing. Its skip message points at
a CI job for the reviewed profile's real run, and as limit 3 records, that job
does not run one.
- A tampered pytest report claiming `theorem_checking` is refused at the kind,
  and a Node report claiming a stronger category is refused the same way
  (`tests/test_pytest_verifier.py:742`, `tests/test_node_verifier.py:920`).
- A cleanup candidate whose compiled representation differs is refused: a
  changed constant, exception table, closure, binding or deleted definition
  (`tests/test_cleanup_logic.py:169-214`), while `test_nothing_that_compiles_the_same_is_refused`
  holds that equal code is not refused, so the gate is not simply refusing
  everything. `test_zero_and_negative_zero_are_not_the_same_constant` is the
  sharp case, with `test_the_zero_case_would_pass_against_a_plain_equality_checker`
  demonstrating that a `==` comparison would have accepted it.
- A pytest report that collected no tests is BLOCKED as empty rather than passed
  (`tests/test_pytest_verifier.py:649`), so a runner that observed nothing
  cannot report a pass.

### Honest summary

Ten checkpoints are built, merged and pushed. Nine defects were found only by
CI and never by local Windows runs, which is the argument for having run the
suite in Actions rather than on this machine.

Writing this handback turned up three more defects, all in the evidence rather
than the product, and all corrected above rather than quietly dropped:

- The claim that `5bed3a6` was green. Its **suite** run failed 31 tests on
  every platform; only the formal workflow was green at that SHA. A green row
  next to a red one is the exact shape this contract exists to prevent.
- Two suite runs were absent from the table entirely, including the one that
  found the Node isolation-flag and fixture defects.
- Two negative-case claims did not match any test in the repo when checked by
  name. The underlying behaviours were real and the tests exist
  (`test_zero_and_negative_zero_are_not_the_same_constant`,
  `test_a_report_with_no_tests_is_blocked_as_empty`); my descriptions of them
  were not. That is a warning about the rest of the prose in any status
  document, including this one.

Nine limits are recorded above and one is closed; **the program is not complete
in the sense the word usually implies.** Three are structural, not cosmetic:

- The protected-integration decision still does not weigh recorded obligations,
  and routing it through `tasks.finalize` is BLOCKED by a digest that is
  non-deterministic across two runs of one candidate (limit 9).
- The reviewed Lean profile has never compiled a real proof (limit 3).
- Browser acceptance is unverified on every platform (limit 4).

A reviewer should treat those three as the real remaining work. The other six
are sharp edges that a careful caller will not hit.

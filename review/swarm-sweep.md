# Swarm sweep, 2026-10-01

> Superseded entry points. The `scripts/posix-*.sh` files this sweep read were
> deleted; `scripts/posix_harness.py` runs a POSIX verification now. What follows
> is unchanged, because a record that renames the thing it recorded stops being a
> record.

Five read-only partitions, one per layer, hunting a single defect class: **something
asserts a guarantee that no check stands behind.** Nine instances of it landed in this
repository this month, in different clothes. This file is the evidence for the ones
still present at `wt/swarm-sweep`.

Every row carries `file:line`, the claim quoted from the tree, and the check that was
run to establish it. A row without a check does not belong here. Several children
cleared their own candidates after reading them, and those corrections are recorded,
because a sweep that only reports hits is not a sweep.

Severity is one of three things.

- **live defect** — the product or harness behaves wrongly, or a gate reports a
  result no check produced.
- **false record** — a document, docstring, or comment asserts something untrue of
  the code it names. Behaviour may be fine; the receipt is not.
- **weakness** — the guarantee is real and the code may be right, but nothing
  exercises it.

Ranked most severe first. Thirty-nine findings: 11 live defects, 10 false records,
18 weaknesses.

| # | id | file:line | claim | what is actually true | severity |
|---|---|---|---|---|---|
| 1 | S1 | `src/vkit/mcp/_tools.py:453-454` | `"Read-only; launches nothing and installs nothing."` and `read_only=True` on `project_inspect` (`_tools.py:996`) | The handler calls `state_root.mkdir(parents=True, exist_ok=True)` then `Store(server.project.db_path)`. `Store.__init__` (`storage.py:488-492`) runs `_migrate()`, which issues `CREATE TABLE IF NOT EXISTS` plus one committing `executescript` per pending version (`storage.py:531-542`). `storage.probe_state`'s own docstring (`storage.py:354`) concedes *"Opening a `Store` migrates it, so this is a write."* This worktree has no `.vkit/` at all, so the write is a real directory creation, not a no-op. `readOnlyHint` is the protocol signal an agent uses to decide auto-approval. The only test on the annotation, `tests/test_mcp_stdio.py:242`, asserts `annotations["readOnlyHint"] is True` against the advertised value, so it passes unchanged if the handler is relabelled. Verified at source by the coordinator, with the child's line numbers corrected from 388-394. | live defect |
| 2 | D7 | `docs/RELEASE-CHECKLIST.md:105` | `GAP-8 \| Open. There is no acceptance table to map. \| KIT_ACCEPTANCE.md is absent from this repository.` | `tmp/research/KIT_ACCEPTANCE.md` is **tracked** (`git ls-files` returns the path) and present, 101 lines, first line `# Implementation acceptance matrix`. The gate backing the row is vocabulary: `tests/test_release_docs.py:110` holds `("GAP-8", ("KIT_ACCEPTANCE.md", "absent"))` and greps that those two tokens appear. Rewriting the file to say the matrix is present would fail nothing. | live defect |
| 3 | D9 | `scripts/posix-env.sh:19-21` | *"It was duplicated across three scripts and each copy was missing one of the two, so a run through the wrong entry point silently lost a directory. One definition, sourced everywhere."* | Four scripts source it. **Eight do not** and inline `export PATH="$VENV/bin:$PATH"` instead: `posix-acceptance-rows.sh`, `posix-acceptance.sh`, `posix-suite-detached.sh`, `posix-suite-doctor.sh`, `posix-suite-total.sh`, `posix-suite.sh`, `posix-totals.sh`, `posix-why.sh`. None carries `~/.local/bin`, which `posix-env.sh:14-17` names as holding the `claude` CLI, with the measured consequence *"8 tests skipped, which is a hole rather than a reason"*. The skip mechanism exists (`tests/test_console.py` `skipif(_claude_cli() is None)`), so a run through any of those eight entry points loses the directory and reproduces the exact hole the file claims to have closed. | live defect |
| 4 | D10 | `scripts/posix-suite-verbatim.sh` | The script reports `TOTAL` and `PASSED` for a whole-suite run | Zero `exit` statements in the file (grep for `exit` returns nothing; the last lines are a closing heredoc `PY`). A suite in which every file fails still exits 0. Same in `posix-suite-total.sh` and `posix-totals.sh`, the latter printing `PASSED {tests - bad - skipped}` with no failure exit. `posix-suite.sh` by contrast has `exit 1` at lines 27 and 73, and `posix-acceptance-rows.sh` sets `overall=1` and exits with it. | live defect |
| 5 | D1 | `REPORT.md:12` | `\| Linux (WSL2) suite \| 526 tests, 0 failed, 42 skipped \|` | No receipt. `git ls-files` finds no WSL or Linux run artifact; the only POSIX files are `scripts/posix-*.sh` and `review/posix-triage.md`. Three in-repo documents assert the opposite: `README.md:200` *"The POSIX path ... has not been run on a POSIX host"*, `docs/RELEASE-CHECKLIST.md:99` (GAP-2 Open), `docs/RELEASE-CHECKLIST.md:336`. `plans/STATUS.md:278-279` asserts the third position. The repo holds both "verified on Linux" and "never run on Linux" with nothing settling it. | live defect |
| 6 | D2 | `docs/RELEASE-CHECKLIST.md:172` | `Observed on this host: 444 passed, 2 skipped` | `tests/` holds **590** `def test_` at HEAD, plus parametrize expansion. 446 collected cannot be a count at this revision. This is the release gate and the line carries no revision pin. `plans/R5-GAPS.md:99` already indicts it (*"stale by ≥15"*) at its old line 148; the staleness was never repaired in the file it indicts. | live defect |
| 7 | D3 | `README.md:177` | `$ pytest tests/` → `404 passed, 1 skipped` | Same recount: 590 test defs at HEAD, of which 42 sit in files gated by module-level `pytestmark = pytest.mark.skipif`. The README reports one skip, so roughly 185 collected tests have no explanation in that file. No commit is pinned in the README's verification section, so the number binds to no revision. | live defect |
| 8 | D8 | `docs/RELEASE-CHECKLIST.md:99` vs `plans/STATUS.md:278` | checklist: `GAP-2 \| Open. The POSIX process path is untested.` / STATUS: *"The POSIX process path is verified; POSIX cancellation is not, and cannot be."* | Two live documents make opposite claims about the same subsystem, neither with a run artifact. `docs/PILOT.md:23` sides with the checklist (*"The verified one is Windows 11"*), so the pilot gate rests on the claim STATUS denies. | live defect |
| 9 | A4 | `src/vkit/execution.py:453-457` | *"Guarding on the object left process.pid = None in the report, which violates the schema, so publish never ran and the row stayed 'running' with no result forever."* | `schemas/run-report.v1.json` declares `properties.process` as `{"type": ["object","null"]}`. It is nullable, so a report carrying `"process": null` does **not** violate the current schema. The design choice at line 458 may still be right; the stated reason is not true of the file it names. The guard itself is also untested: filtering for the exact identifier `_terminal_report` returns zero tests (the apparent hit at `test_storage.py:74` is the substring inside a test *name*). | false record |
| 10 | C3 | `src/vkit/manifest.py:126-142` | `with_root` exists *"because `execution.run_check` re-derives the source identity from `manifest.project` after the process exits, and it must re-derive it for the tree that actually ran."* | `git grep -E 'with_root' -- .` across the whole repo returns **2 hits**: the definition, and a *docstring* inside `tests/test_core_admission.py:597` — `"the property Manifest.with_root exists to keep."` The test that line sits in re-parses the manifest per checkout instead of calling it. A method documented as load-bearing, cited by a test as the reason that test exists, with zero call sites. | false record |
| 11 | C2 | `src/vkit/integration/oracle.py:163-168` | `repoint_approved`: *"A check whose script is not in `scripts` ... is left exactly as the approved manifest declared it. That is a refusal by the caller rather than a guess here."* | The code does what the docstring says, but the branch is unreachable from the only caller. `git grep -E 'repoint_approved'` returns 2 hits: the definition and one call at `verify.py:313`, which sits inside `if worst_finding != "REJECT"`. By then an unpinned check has already produced a REJECT `checker_not_identifiable` finding at `verify.py:256-262`, so the run refuses before this code is reached. | false record |
| 12 | S4 | `src/vkit/console/plan.py:12` | *"`PROTECTED_PATHS` names the paths that are policy rather than state, and a test asserts no handler in this package writes anything under them."* | The constant is `PROTECTED_PATH_PARTS`. `grep -rn "PROTECTED_PATHS\b" src tests docs` returns **exactly one hit: the docstring itself.** Nothing references that name. The pointer also misdirects about what is enforced: `api.py:245-256` rejects a *request parameter string* containing a protected component. That is request validation, not a write-guard over handlers. The real protection is structural (`WRITABLE` has no manifest name), stated separately and correctly at `plan.py:60-61`. | false record |
| 13 | S2 | `src/vkit/mcp/_tools.py:341` | *"(the same probe the core uses, so inspection and a real run cannot report different environments)"* | The handler recomputes prerequisites itself at `_tools.py:465-470`, collapsing `(check, prerequisite)` pairs to a **set of executable names**. `cmd_doctor` (`cli.py:238-247`) emits one finding per pair. Two checks requiring the same tool yield two doctor findings and one inspect gap. The adjacent `execution_available` field is a *third* spelling of "ready" and lacks the `<source>` term `cmd_doctor` has. No test diffs inspect against doctor: 26 `project_inspect` hits in `tests/`, every one asserting a field in isolation. | false record |
| 14 | D11 | `formal/RESULTS.md:191` | `MUTANT_MISSING_CHECK \| ... \| assert 'READY' == 'BLOCKED'` | `formal/results/python-core-mutants-receipt.json` records for that mutant: `"counterexample":"E       AssertionError: acceptance succeeded with lint absent; absent evidence is never success"`. The document quotes a counterexample the receipt does not contain. (The `MUTANT_STALE_GENERATION` row does match its receipt.) | false record |
| 15 | D4 | `plans/STATUS.md:13`, `:187` | *"six tools through `Server.call_tool` plus 23 protocol tests driving a real subprocess"* | `tests/test_mcp_stdio.py` has 20 module-level `def test_`, one of which is `@pytest.mark.parametrize` with 5 explicit ids. Collection is 19 + 5 = **24**, not 23. The same stale 23 appears at `docs/RELEASE-CHECKLIST.md:256`. No test gates either number. | false record |
| 16 | D5 | `plans/STATUS.md:19`, `:149` | *"`tests/test_release_docs.py` (10 tests)"* / *"`pytest tests/test_release_docs.py`: 10 passed"* | The file has **23** test defs at HEAD. At the commit STATUS.md pins its evidence to (`2199355`), it has 6. The claimed 10 matches neither the current file nor the pinned revision. | false record |
| 17 | D13 | `REPORT.md:14` | `\| Release gate \| 26/26 \|` | No 26-item gate exists in the tree. `docs/RELEASE-CHECKLIST.md`'s `## Release gate` section (lines 340-346) is prose with no enumeration and the document has zero numbered steps. `review/AUDIT.md:195` itself says *"`22/22` and `26/26` are results of particular harnesses, not completion of every row."* | false record |
| 18 | T1 | `tests/test_console.py:949` | `assert report["configuration_digest"] == report["configuration_digest"]` | Both sides are the same subscript on the same dict. The value can be `None`, `""`, or the wrong digest and the assertion passes. Delete the `configuration_digest` column from `storage.py:46` and the statement still evaluates true for any present value. It checks key presence, nothing else. The neighbouring asserts carry the test's real claim, so the test is not vacuous overall. | false record |
| 19 | T2 | `tests/test_console.py:108-122` | *"The refusal happens before the socket exists, so nothing is reachable. ... This proves the port is free afterwards."* | The test passes `port=0`, so the OS chooses and there is no port to probe, then hardcodes **port 1** in the probe. Measured on this host with a real `HTTPServer` bound to `0.0.0.0:54006`: `connect_ex(('127.0.0.1', 1))` returns 10061 (refused) while `connect_ex(('127.0.0.1', 54006))` returns 0. The assertion holds while a routable socket listens. Move the host check at `console/server.py:247` after the bind and re-raise, and this test stays green. The `pytest.raises` does carry the guard-removal case. | false record |
| 20 | D6 | `docs/RELEASE-CHECKLIST.md:103`, `docs/PILOT.md:22` | *"`vkit project enroll` and `vkit integration verify` ... carry 29 tests."* | The number happens to be right: `test_enroll.py` 9 + `test_integration.py` 8 + `test_plan06_onboarding.py` 12 = 29. But `test_plan06_onboarding.py` is named nowhere in either row and no test is named for integration, so 29 is only reconstructible by guessing which three files were meant. Unreceipted, and wrong under a different reading. | false record |
| 21 | S3 | `src/vkit/console/operations.py:122-128` | *"`storage.probe_state`, which `cmd_doctor` also calls, so the two surfaces run one probe rather than each carrying a copy that can drift."* | The probe claim is true and tested (`tests/test_console.py:415-458` drives the real `cmd_doctor` and compares `state_detail` strings). But the docstring scopes the shared rule to `state_writable` while the `ok` rule is two copies: `readiness_view` (`operations.py:155`) folds in `context.manifest is not None`, `cmd_doctor` (`cli.py:263`) gets it via a `<manifest>` finding and *additionally* appends a `<source>` finding when source identity fails (`cli.py:258-260`) — a term `readiness_view` has none of. On such a repo doctor says false and readiness says true. The parity test only exercises the state-store branch. | weakness |
| 22 | A1 | `src/vkit/procs.py:352,355,357` and `:701,704,706` | Six `LaunchError` pre-flight guards: empty argv, missing cwd, non-positive or infinite timeout | Literal grep for each message across all 48 test files returns **zero hits in `tests/`** in all three cases. `LaunchError` has 10 non-test references, all inside `procs.py`; no product module catches it. Every product call site feeds argv from a checked source. Reachable only by an out-of-repo caller or a corrupt `argv_json` row. The `run_command` docstring (`:340-344`) enumerates one non-raising outcome and never mentions the three raises. | weakness |
| 23 | A5 | `src/vkit/storage.py:922`, `:696`, `:581`, `:1049` | Four refusals | `tests/test_network_path_refusal.py` covers exactly 2 of the 20 raises in `storage.py`, and does so well (both branches of `_reject_network_path`, positive paired with negative). Beyond it: `storage.py:922` *"refusing to publish ... with no outcome"* — grep zero, plus a structural probe matching `"outcome": {}` / `del outcome` / `pop("result")` also zero; `test_storage.py:88` drives line 924 instead. `storage.py:696` *"no launch is recorded for run ... to supervise"* — grep zero. `storage.py:581` and `:1049` — "already registered" and "already recorded" both grep zero. | weakness |
| 24 | A3 | `src/vkit/execution.py:37-39` | *"The run could not be set up at all. Distinct from a BLOCKED outcome, which is a real, recorded answer about the check rather than a failure to try."* | `ExecutionError` has **zero** references in any test file. Six non-test references, all in `execution.py` and `cli.py`. The two raise sites have no receipt, and no receipt that any caller tells `ExecutionError` from a BLOCKED outcome. `cli.py:521` catches `(StoreError, ExecutionError)` as one clause, so the distinction is invisible on the one CLI path handling both. | weakness |
| 25 | C5 | `src/vkit/tasks.py:437-453` | *"...validated into one shape. A list of objects is the wire form an agent can write; a mapping is accepted because an MCP client naturally has one."* | Grep for all four refusal messages across `tests/` returns **0 hits**; the only source hits are the raises themselves. Every `resources=` call site in `tests` uses well-formed input. Untested: the dict form the docstring calls out, unknown keys, duplicate keys, a blank key, and an unknown `kind`. | weakness |
| 26 | C6 | `src/vkit/manifest.py:234-251` | *"Containment is checked after resolution, for the reason every other path in this codebase takes one: a prefix test loses to `..` and to an absolute path."* | Grep for `escapes the repository root` and `must be a repository-relative path` across `tests/` returns **0 hits**. The three raises are reached in tests only via the third (`does not exist`, driven by `test_core_admission.py:267`). The absolute-path and `..`-escape refusals are never exercised, so the argument the code makes for this ordering stands on no executed evidence. | weakness |
| 27 | C1 | `src/vkit/integration/oracle.py:139-143` | *"A check that names a module or an absolute path outside the tree names no file the approved revision can pin, and is reported as pinning nothing."* | The `break` is correct and *is* covered, indirectly: `verify.py:254` raises `checker_not_identifiable` for an unpinned check. But `git grep -E 'checker_not_identifiable' -- tests` returns **zero hits** (the 3 hits are all `checker_bytes_changed`). So the whole empty-scripts branch of the oracle boundary is unexercised. Behaviour is right; the guarantee has no evidence. | weakness |
| 28 | C7 | `src/vkit/identity.py:136-142` | *"a tracked path with no worktree file (a submodule) is recorded by kind rather than by content"* | `git grep -E 'gitlink|submodule' -- .` returns 5 hits, **none in `tests/`**. The `absent` branch (`:141`) and the `gitlink` kind (`:139`) are both unexercised. Both exist so the digest distinguishes "tracked, content X" from "tracked, no worktree file"; nothing verifies that distinction survives into the digest. `test_identity.py` has 22 tests, none naming a submodule. | weakness |
| 29 | C4 | `src/vkit/manifest.py:90-101` | *"`python` defaults to the interpreter running vkit ... Plan 07's trusted integration path does [pass it explicitly], so the checks of a candidate checkout are executed by the approved verifier revision."* | `git grep -E 'resolved_argv\b'` returns **1 hit**, the definition. Both real callers use the sibling `resolved_argv_for` (`execution.py:300`, `supervisor.py:190`). The half of the claim about `verify.py:212` setting `python=sys.executable` holds through the sibling; the method the docstring points Plan 07 at is never called. | weakness |
| 30 | A2 | `src/vkit/procs.py:356`, `:705` | the `timeout_seconds == float("inf")` half of the timeout guard | Grep for `math.inf` and `float(".inf` across every `.py` in the repo, including `tests/` and `scripts/`, returns **2 hits, both the guard lines themselves.** No producer of an infinite timeout exists anywhere, so this half has nothing that can reach it. | weakness |
| 31 | T3 | `tests/` (12 sites) | product imports that are never a call | Same class as the `record_readiness` example. Full-file occurrence counts of 1: `test_formal_correspondence.py:85` `get_task`, `test_r2_ownership.py:65` `prepare_job`, `test_console.py:41` `ManifestError`, `test_console.py:26` `sys`, `test_recovery07.py:43` `one_json_object`/`vkit`, `test_recovery07.py:182` `ConflictError`, `test_policy.py:52,59` `pytest`/`EXIT_BLOCKED`, `test_enroll.py:22` `enrollment`, `test_plan06_mcp_inspect.py:24,29` `pytest`/`accept`, `test_storage.py:11` `sqlite3`, `test_plan06_features.py:23` `Callable`. Two point at real uncovered branches rather than dead imports: `ManifestError` (`operations.py:92`, `:410` — no test drives a malformed manifest through the *console* context; `test_core_admission.py:227` uses `AdmissionContext`), and `prepare_job` (`procs.py:231` — R2's module docstring claims *"Every test here drives the real thing"*, but the direct API is never called from a test). | weakness |
| 32 | T4 | `tests/test_storage.py:318` | `assert all(isinstance(v, str) for v in facts["tool_versions"].values())` | `_environment_facts` (`execution.py:173-201`) builds `tools` by resolving `git` and `python` and `continue`s past any that fail. If both fail, `tools` is `{}` and `all(...)` over an empty dict is true. The comment on line 313 names this exact failure mode and guards the assertion above it; this one is unguarded. | weakness |
| 33 | T5 | `tests/test_acceptance02.py:107` | `test_the_plan_text_is_carried_verbatim_into_every_title` | `plan_table()` (line 39) parses the plan's markdown table and skips any line whose first cell is exactly `Exercise`. Rename that header cell and the function returns `[]`, the loop body never runs, and the test passes with **zero assertions**. The sibling at line 102 pins `ROW_TITLES` against `ROWS`, not against `plan_table()`'s length, so nothing else catches it. | weakness |
| 34 | T6 | `tests/test_plugin_host.py:824-825` | bare `return` in a test body: `if not registry.is_file(): return` | On a host with no real user config the test exits silently, and the skip is invisible in the output rather than reported. The registry does exist on this machine so it does not fire here, but the adjacent `pytest.skip` at line 821 is the honest form for the same situation. | weakness |
| 35 | A5b | `src/vkit/storage.py:466`, `:920`, `:944`, `:955`, `:969` | five further refusals | Census detail from the same partition: `_Transaction.__enter__` write-lock refusal (covered), `publish` non-terminal (covered, and `test_storage.py:62-100` also asserts no `report.json` is left behind, which is the ordering property the docstring at `:904-917` claims), `publish` already-terminal (covered), `load` no-report (covered), `resolve_artifact` escape (covered — this one **looked** untested on a message-substring grep that returned zero, and reading `test_storage.py` disproved it). Recorded so the census is complete; see the corrections below. | weakness |
| 36 | D12 | `formal/RESULTS.md:13` | *"`python review/probe_formal_state.py` recomputes the digest each receipt records and prints the state of all four artifacts; it is the check that keeps this table honest."* | Grep for `probe_formal_state` across every `.py`, `.md`, `.sh`, `.toml`, `.yml`, `.cfg` in the tree returns **exactly one hit: this sentence.** No test, no CI config, no script invokes it. Editing Artifact D's row from BLOCKED to VERIFIED would fail nothing. Separately the probe's own contract is `exit 0 when every artifact is either VERIFIED or BLOCKED with a stated reason`, so a green exit is compatible with all four having no result. | weakness |
| 37 | D14 | `plans/STATUS.md:11-12` | `22/22 acceptance rows` and `14/14` | Checked and **clean**. `scripts/acceptance.py`'s `main()` enumerates exactly 12 `row_*` functions producing 22 results (17 top-level `check()` sites, 4 iterations of the 4-way `expected.items()` loop in `row_malformed_artifact`, 3 in `row_evidence_survives_checkout` of which 2 are conditional). `14/14` matches the 14 `ROW_FUNCTIONS` entries in `scripts/acceptance02.py`. Listed to show which of STATUS.md's numbers were checked. | weakness |
| 38 | C8 | `src/vkit/tasks.py:63-72` | `AdmissionRefused` *"lets an adapter answer BLOCKED for a missing required check while answering INVALID for a malformed request, without either of them deciding the question itself."* | `AdmissionRefused` and `TaskError` are both subclasses of `TaskError` (`tasks.py:60-64`), raised from `admit` at `:508` and `:122,149,156,162,167,172` respectively. A caller catching `TaskError` cannot tell them apart; the distinction holds only if every adapter catches `AdmissionRefused` **before** `TaskError`. Partition 1 did not grep the adapters and explicitly declined to claim it. Carried as open. | weakness |
| 39 | A6 | `src/vkit/storage.py:466` | see row 35 | Reserved. Partition 2 left two questions open rather than guessing: whether `schemas/manifest.v1.json` constrains `argv` to a minimum length (which decides whether the `procs.py:352` guard is reachable through a hand-written manifest), and whether `register_run`/`record_acceptance` are ever called with a duplicate identifier in tests (which decides whether rows 23's last two entries are a coverage gap or a dead refusal). | weakness |

## The three the repo should look at first

1. **S1** (`mcp/_tools.py:453`) — a `readOnlyHint: true` tool that creates a directory and runs SQLite migrations. This one changes agent behaviour, not just the record.
2. **D7** (`RELEASE-CHECKLIST.md:105`) — a release gate that greps for the word "absent" about a file that is tracked in the tree.
3. **D9/D10** (`posix-env.sh`, `posix-suite-verbatim.sh`) — eight of twelve suite entry points lose a PATH directory the file claims is universal, and three of them report PASS while exiting 0 on a fully failed suite.

## Corrections the children made to their own candidates

A sweep that only reports hits is not a sweep. These were flagged by a script or a
weak grep, then cleared by reading the file.

- **Partition 2** suspected `storage.py:969` (`resolve_artifact` escape) and
  `execution.py:_terminal_report` of being untested on message-substring greps that
  returned zero. Reading `test_storage.py` disproved the first. The second was a false
  match: `test_storage.py:74` contains the substring only inside a test *name*.
- **Partition 3** cleared the `readiness_view` hardcoding defect named in its own
  brief. `operations.py:152` and `cli.py:255` both call `storage.probe_state`, and
  `tests/test_console.py:415` compares the two shipped answers through the real
  `cmd_doctor` entry point. It is fixed, and fixed with evidence.
- **Partition 4** cleared the file its brief used as the example of an overclaiming
  test. `test_network_path_refusal.py` covers both branches of `_reject_network_path`,
  pairs every positive with a negative, and stands in a POSIX host so the
  `startswith("//")` branch is not dead on Windows. It is not vacuous.
- **Partition 4** cleared 15 `all()`/`any()` generator asserts, one zero-assert
  parametrised test (all six dispatched checks are real, one runs `git cat-file -e`),
  and confirmed no `try/except ImportError: pytest.skip` exists anywhere.
- **Partition 5** cleared the exact bug named in its brief. `formal/run_python_mutants.py`
  maps `_PASSED, FAILED, ERROR, USAGE, NOTHING_COLLECTED = 0, 1, 2, 4, 5` separately
  and returns `CAUGHT` only for `FAILED`; 2, 4 and 5 become `BLOCKED` with
  *"A test that did not run is not a counterexample."* Both node ids in the receipt
  resolve. Confirmed independently by the coordinator before drafting.
- **Partition 5** cleared every `docs/PILOT.md` status row, the GAP-7 fix
  (`acceptance.py` refuses to start unless the child resolves to this tree, so it is
  not a vocabulary gate), the `formal/` digests (recomputed by hand and matching), and
  all 18 `posix-*.sh` probes for unconditional PASS (none contains a bare `echo PASS`).
- **Partition 1** caught that three of its own early greps passed a string where a
  path list was expected, returned zero rows, and re-ran them correctly.

## Searched and clean

Each partition names what it covered and what it cleared, so a reader can re-check the
absence as well as the presence.

- **1 — `src/` core** (`tasks.py`, `claims.py`, `integration/oracle.py`, `identity.py`,
  `manifest.py`). `claims.py` came back **clean with no finding**: every guard has a
  test asserting the exact message, including the double-take rollback, release by a
  non-holder, repeat release, and the middle-holder resync, and both production call
  sites are covered. `identity.py` and `manifest.py` clean apart from rows 10, 26, 28,
  29. The oracle's central claim, that a candidate cannot supply the bytes that judge
  it, is genuinely implemented: approved side read at the pinned revision, candidate
  side at the candidate commit, compared by set union, refused on any difference, and
  asserted both ways end to end at `test_policy.py:463,472,493,513`. The
  `compute_readiness` BLOCKED-not-dropped claim is backed by
  `test_core_admission.py:480-505` and `test_identity_invalidation.py:274-330`.
- **2 — `src/` adapters** (`supervisor.py`, `execution.py`, `procs.py`,
  `procidentity.py`, `recover.py`, `storage.py`). `supervisor.py` clean on raises
  (`SupervisorError` 4 test hits, `OSError` 8, and the failed-terminate path converts
  to a retained claim via `_blocked_publish` rather than swallowing). `recover.py`
  clean (19 raises, 18 test hits). `procidentity.py` clean (12 raises, 8 hits, plus
  `UnsupportedPlatform`). The `LaunchError`-as-`reason`-value path is exercised.
- **3 — surfaces** (`cli.py`, `console/`, `mcp/`, `pluginres.py`, `plugin/`,
  `formal/reference.py`). All six MCP tool descriptions were extracted with
  description, schema, annotations and handler side by side; the five besides S1 are
  **backed**. `task_begin`'s "derived rather than supplied" digest, `check_start`'s
  "there is no way to pass a command" (schema has no command property and `call_tool`
  rejects unknown args), its idempotency on `request_id`, `run_cancel`'s "client
  supplies no pid", `run_get`'s byte-offset paging via `_read_page`, and
  `task_finalize`'s "cannot reduce it" all hold. `Server._own_run`'s id-containment
  claim and `resolve_artifact`'s containment are real. `pluginres.py`, the `plugin/`
  bundle, and the `WRITABLE` handler table are clean. `formal/reference.py` is
  notably honest: it states it does *not* establish implementation equivalence and
  names its own shared-bug blind spot.
- **4 — `tests/`** (48 files, 590 test functions). AST pass over every file, then
  hand reads of ~30 functions and their product counterparts. 11 `pytest.skip` sites
  and 4 module-level `pytestmark`s are each narrow and state a real host limitation.
  The one skip worth naming, `test_formal_correspondence.py:70`'s
  `importorskip("hypothesis")`, is documented correctly in the file including a
  correction to an earlier false claim about the same tests.
- **5 — docs and harness** (`docs/`, `plans/`, `scripts/`, `formal/`, root README and
  REPORT). Every `docs/PILOT.md` status row carries a receipt or says "Not met" with
  the reason. `docs/GAP-7-AND-GAP-9-FINDINGS.md` is honest about its own numbers and
  instructs the reader to treat carried-over counts as unverified. `formal/`
  digests reproduce, including a hand-recomputed `sha256` of `tasks.py` against the
  receipt's `target_sha256`, and `states_generated: 1935362` /
  `distinct_states: 207360` match `RESULTS.md` verbatim.

## Method and limits

Five partitions, one per layer, run in parallel as read-only agents. Each was told to
stop after roughly 15 tool calls and return partial findings rather than time out. None
edited a file, committed, or ran the test suite. Large sweeps went through
`ctx_execute` / `ctx_batch_execute` so raw bytes stayed out of the agents' contexts.

The coordinator independently re-verified the four rows that head this table at source
and corrected one set of line numbers: S1 (handler write at `_tools.py:453-454`, not
388-394, plus the absent `.vkit/` on this checkout that makes the counterexample real),
D7 (`git ls-files` confirms `tmp/research/KIT_ACCEPTANCE.md` is tracked), D10 (zero
`exit` statements in the three named scripts against two in `posix-suite.sh`), and the
`formal/run_python_mutants.py` exit-code table.

Limits worth stating. No finding here was confirmed by running the product; the
`console/server.py:247` reachability in row 19 and the `Procs` guard reachability in
row 22 are argued from the code and greps, not executed. The eight POSIX entry points
in row 3 were identified by grep and reading, not by running them on a POSIX host,
which this machine is not. Rows 38 and 39 are carried open on purpose: they are the
questions a child declined to answer, and answering them wrong would cost more than
leaving them visible.

**Model note.** The first spawn of all five partitions failed immediately with
`400 unknown provider for model claude-fable-5-1`. They were relaunched on `opus` and
all five completed. No finding depends on the failed attempt.

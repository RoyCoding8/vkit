# Implementation acceptance matrix

This is the acceptance research for the reusable kit. It is not an implementation task list and does not claim these checks have passed. Only rows marked observed were exercised during research.

A required outcome is what a check has to produce. Most rows say nothing about how that is proved, so most have no receipt naming them. GAP-8 in the release checklist is the register entry for that, and it is open. Two rows are marked observed because a run produced the numbers in the cell, and those two rows are the only ones here with a receipt.

## Compatibility and installation

| Check | Required outcome |
| --- | --- |
| Fresh Claude Code session | Enrolled repository is detected and the relevant kit instructions are discoverable |
| Fresh subagent | Its own context contains the task contract and required skill pointers |
| Two configured models | The same contract and checks work without changes to the core; actual model selection is reported correctly |
| Missing pstack skill | Clear compatibility failure or an explicitly reduced workflow; no false claim that the missing procedure ran |
| Unsupported hook event or schema | Doctor reports the unsupported capability; protected integration remains effective |
| Install path contains spaces and non-ASCII text | Hook and CLI invocations resolve exact paths |
| Update during an active campaign | Existing campaign retains its pinned contract and tool version or requires explicit revalidation |
| Plugin removal | Product source and required evidence remain recoverable; runtime cache cleanup is distinct |
| Unenrolled repository | Plugin does not impose completion gates on unrelated work |

## Verification semantics

| Check | Required outcome |
| --- | --- |
| Known good scenario | PASS with command, source identity, expected observations, and evidence |
| Known product defect | FAIL; failing output is retained |
| Missing dependency, credential, or app instance | BLOCKED with the exact prerequisite |
| Unknown scenario or zero collected tests | No PASS |
| Tool exits zero without required result artifact | No PASS |
| Source changes during a run | Result is invalidated or the immutable tested snapshot is identified |
| Required check is skipped | Task cannot be accepted |
| Check passes only after retry | Initial failure and flaky classification remain visible |
| Acceptance threshold or theorem changes | Old evidence cannot satisfy the new contract automatically |
| Local report is edited to claim success | Protected CI executes checks again and does not trust the edited local report |
| Changed file has no coverage mapping | Run the broader relevant suite or report the gap |
| Driver changes | Re-exercise the real application path, including failed setup and cleanup |

## Parallel work and integration

| Check | Required outcome |
| --- | --- |
| Many contenders claim one resource | At most one active owner; conflict is explicit |
| Research SQLite probe, observed | 100 attempts with eight processes produced one successful claim and 99 conflicts |
| Process dies during claim transaction | No partial active claim is accepted |
| Old owner resumes after reassignment | Superseded attempt cannot publish an accepted result |
| Agent limit reached | Stop admission; do not retry-spawn in a loop |
| Several Claude sessions share a campaign | They obey the same registered resource limits through the supported kit path |
| Verification capacity is exhausted | Keep tasks queued and reserve capacity to drain completed engineering work |
| Two independent changes interfere semantically | Combined-candidate checks reject integration |
| Research Git probe, observed | Both branches passed alone; a clean merge failed; the old worker checkout still passed |
| Target branch moves after verification | Rebuild and check the integration candidate |
| Dirty or unfinished checkout is retired | Preserve work and evidence before safe removal or archive |
| Native Claude task state disagrees with artifacts | Reconcile actual execution and evidence; do not infer acceptance from a completed badge |
| Agent teams enabled | Dedicated compatibility tests pass before this mode is advertised |

## Processes, storage, and evidence

| Check | Required outcome |
| --- | --- |
| Timeout with child and grandchild processes | Participating descendants terminate; report stays readable |
| Cancellation while output is large | No deadlock; bounded report and retained raw logs |
| Native Windows launch through a `.cmd` wrapper | Argument and exit semantics are deliberate and tested |
| Windows Job Object assignment fails | No claim of contained execution; stop before unmanaged launch |
| Process exits but leaves a database or profile locked | Diagnose the owner; cleanup must not report success prematurely |
| Evidence directory lies inside a disposable worktree | Reject or relocate it before the run |
| Disk full or interrupted report write | No complete acceptance record; partial artifacts remain diagnosable |
| Path traverses a symlink outside its assigned root | Boundary validation catches it where path confinement is required |
| Network filesystem for local SQLite coordination | Refuse unsupported shared coordination or use a separately specified backend |
| Secret appears in command output | Apply the project's redaction rules before sharing evidence; do not dump full environment variables. The environment half is enforced and holds. The output half is not implemented: the report's `environment` block is written by `_environment_facts` and never carries the process environment, which `tests/test_storage.py` asserts. Nothing between the check and the disk filters `stdout.log` or `stderr.log`, and there is no redaction routine anywhere under `src/`, so a secret the check itself prints is persisted verbatim. |

## Hooks and trust boundaries

| Check | Required outcome |
| --- | --- |
| Managed task stops without evidence | Hook returns the correct event-specific blocking response |
| Managed task is truly blocked | Agent can report BLOCKED without an endless continuation loop; task remains unaccepted |
| Hook executable crashes or times out | Failure is visible; no accepted verdict is manufactured |
| TaskCompleted is absent in this session type | Other supported task-finalization checks still apply |
| Internal host agent or read-only answer stops | Product verification gate does not activate without a managed task binding |
| Hook receives duplicate events | Same registration or outcome; no duplicate ownership |
| Worker changes gate or policy files | Protected policy review and trusted CI prevent self-approval |
| Worktree exists without OS sandbox | Report isolation accurately; do not label it a security sandbox |

## Formal checks

| Check | Required outcome |
| --- | --- |
| Property test finds a counterexample | Preserve a reproducible seed or minimized failing case |
| Symbolic search finds no counterexample | Record search scope; do not report a universal proof |
| TLC run is incomplete or resource-limited | No claim that the entire selected model was checked |
| TLC verifies a finite model | Report model revision, bounds, properties, and implementation correspondence limits |
| Lean theorem depends on `sorryAx` | Reject as an incomplete proof |
| Lean theorem gains an unapproved axiom or weakened assumption | Require review; previous acceptance is invalid |
| Formal tool is not installed | BLOCKED, not a silently skipped required proof |

## Measure whether the kit earns its cost

Compare existing pstack use with pstack plus the kit on a fixed small set of useful tasks. Include a bug, a feature, a refactor, a combined-change conflict, and an interrupted run. Keep task requirements, environment, and model settings comparable. Run each condition from fresh task state so earlier fixes do not leak into later attempts.

Record accepted task outcomes, false passes, failures caught before integration, human interventions, repair attempts, elapsed time, verification time, and cost when available. Include setup and maintenance effort. Do not select only successful runs or use PR count as the main outcome.

Synthetic 100-client tests exercise bookkeeping. They do not establish throughput or quality for 100 model agents. A live concurrency increase needs explicit authorization, capacity limits, and measurements at the proposed scale.

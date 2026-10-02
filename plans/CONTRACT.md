# Shared implementation contract

Status: the required design baseline for Plans 01-09, and still the authority
the product cites. Names were provisional until Plan 01 froze its public
interface. This file states meanings and invariants, not an implementation
status, so it does not carry per-plan state. Three clauses have been amended as
the code settled them; each amendment says what changed and why, because a
reviewer needs to know the contract moved rather than was always this way.

## Amendments

1. **Run lifecycle gained two values.** This section originally said a run is
   preparing, running, or terminal. R2 added `launching` and `cancelling`, so the
   full set is preparing, launching, cancelling, running, terminal. The original
   three were a description of what a foreground check did, and the record could
   no longer say what was happening between register and identity publish. See
   [R2-DESIGN.md](R2-DESIGN.md).
2. **`integration verify` is a public command.** The command table below
   assigns it to Plan 07 and it shipped as `vkit integration verify`, wired
   through the same dispatch table as every other verb.
3. **A lost connection does not mean the work stopped.** "Run lifetime and
   recovery" originally said Plan 01 runs in the foreground and Plan 02 adds a
   per-run supervisor, which left the foreground path as the only described one.
   Both paths ship. A detached supervisor outlives its initiating request; the
   foreground path is for callers that want the verdict when the call returns.

## Scope and ownership

The application manages verification, evidence, and declared resources. Claude Code owns model execution and conversation task status. Git owns source history. The repository defines intended behavior and required checks. Protected CI makes the final integration decision.

The app must not call a model, grade prose as proof, or decide that two reviewers agreeing makes code correct. MCP does not replace source editing or debugging tools. It centralizes the repeated setup, execution, status, evidence, and completion operations that agents otherwise reconstruct.

Use Python 3.11+, standard-library CLI/process/SQLite facilities, `jsonschema` for boundary validation, and `pywin32` for required Windows process ownership. Plan 03 adds the official `mcp` SDK. Existing Node/browser tooling stays in its own language. Use pytest for the necessary integration checks and Hypothesis in Plan 02 for stateful ownership checks. Do not add a framework for each module.

One Python package can contain contracts, execution, storage, CLI, and adapters. Keep adapters thin. Separate Windows process code where the OS requires it. No generic plugin registry, event bus, dependency-injection container, or second scheduler is required.

Suggested layout, adjustable by the engineer:

```text
pyproject.toml
src/vkit/              # core, CLI, MCP and hook entry points
schemas/              # authoritative versioned boundary schemas
plugin/               # Claude plugin distribution, introduced in Plan 04
examples/             # actual Python and Node applications
tests/                # behavior and failure checks
plans/                # implementation handoffs and acceptance receipts
```

Create files when their milestone needs them. Do not scaffold later modules with placeholder implementations.

## State and identity

Resolve repository root and Git common directory using Git commands. Runtime state lives below `<git-common-dir>/verification-kit/`; evidence must survive removal of a worker checkout. Separate clones have separate state. Non-Git enrollment and distributed coordination are outside the first release.

Store small authoritative records in SQLite and bulk output in per-run artifact directories. Use short transactions, uniqueness constraints, a bounded busy timeout, explicit connection closure, and atomic final report replacement. Do not hold a transaction during subprocess execution. Start with SQLite's default journal mode. Reject network-backed shared coordination as unsupported.

| Record | Required facts |
| --- | --- |
| Contract | Schema version, intended outcome, base revision, allowed write scope, required check IDs, required resources, policy digest |
| Attempt | Task ID, attempt generation, owner reference, checkout, native session/agent references when available, status |
| Run | Run ID, task/attempt identity, check selection, source/policy/fixture identities, lifecycle, process identity, artifact references |
| Claim | Resource key, owner attempt, capacity where applicable, acquisition and release facts |
| Acceptance | Computed decision, exact source and policy identities, required checks and gaps, context of decision |

Do not duplicate Claude's task queue. `task_id` identifies a kit contract, not an assertion that the host task is complete. Native IDs are correlations, not cryptographic authentication. Random unique run IDs and monotonic attempt generations serve different purposes.

The first milestone may store runs before task attempts exist. Later migration must preserve old reports as standalone evidence; it must not invent task acceptance for them.

## Manifest and checks

Repository configuration lives in `verification/manifest.json`. Use schema version 1. It defines check IDs, descriptions, argument arrays, repository-relative working directories, finite timeouts, declared inputs/resources, prerequisites, and expected result format. Features reference check IDs and concise feature documents. An approved policy defines the mandatory check set or profile. A task may add checks but cannot remove that baseline by selecting a smaller list through MCP. A verification task requires at least one check and one required scenario.

Agent-facing APIs accept registered IDs, not arbitrary shell commands. Configuration is executable repository policy and must be reviewed as such. A local user explicitly enrolls a repository before the app runs its configured commands.

Use argument arrays, explicit executables, and path normalization. Define a small documented set of placeholders only if needed, such as the run artifact directory and resolved Python interpreter. Never evaluate template code or interpolate task prose into shell text. Treat `.cmd` launchers as a deliberate Windows case with tested argument handling.

A check emits a versioned result artifact containing a nonempty list of scenario IDs, each with PASS or FAIL and concise observations. Command success and artifact validity are both necessary for PASS. Missing artifacts, zero scenarios, unknown required scenario IDs, malformed data, timeouts, and incomplete execution cannot pass. Adapters translate existing test outputs into this format; the core must not scrape arbitrary success prose.

Results include the executed argv, cwd, start/end, process exit, selected scenarios, source identity, configuration digest, fixture identity, tool versions, and artifact digests. Capture only necessary environment facts. Do not persist credentials or full environments. Keep raw logs locally with a documented retention policy; shared/exported output follows declared redaction rules.

## Outcome and lifecycle semantics

Keep lifecycle separate from outcome. A run is one of preparing, launching,
cancelling, running, or terminal, and a terminal check has one outcome:

| Outcome | Meaning |
| --- | --- |
| PASS | Required observations succeeded under the recorded contract |
| FAIL | A valid check observed a product or required-policy failure |
| BLOCKED | Execution or evidence was insufficient to decide, with a specific reason |

Timeout, cancellation, missing tools, malformed reports, stale source, lost ownership, and interrupted execution are BLOCKED reasons. Acceptance rejects both FAIL and BLOCKED. A retry is a new run linked to its predecessor. Preserve the original failure and flag instability; never overwrite it with the later pass.

CLI exit codes: 0 for successful command/PASS, 1 for a completed failing check, 2 for invalid invocation, 3 for BLOCKED, 4 for an internal application error. Structured output disambiguates operational errors from check outcomes. Claude hook adapters must map these to each hook's required response; core exit code 2 is not a hook policy decision.

`task_finalize` computes READY, REJECTED, or BLOCKED and lists gaps. READY is local engineering readiness, never merge permission. Only a protected integration run can report readiness for the tested integration context. No client-supplied verdict can finalize a task.

READINESS establishes satisfaction of the explicit check contract. It does not prove that the contract fully captures a natural-language feature request. Contract quality remains part of planning and review.

## Source and policy freshness

Record HEAD and an inventory digest of tracked files, relevant untracked source, manifest, drivers, and declared fixture inputs. Exclude only declared generated output and known secret inputs; record the exclusion policy. Hash before and after execution. A persistent change invalidates the run. Development evidence records whether the tree was dirty and cannot be reused as clean integration evidence.

Before/after hashes do not detect transient edits that are restored. Describe this limit. Trusted integration checks run in a clean candidate checkout with exclusive ownership and policy/verifier code from an approved revision. Required source paths and external inputs must be accounted for; unknown coverage is a gap, not success.

A worker may propose policy changes but cannot silently weaken its own acceptance contract. Existing tasks keep their pinned contract. Revalidation under an approved changed policy produces new evidence.

## Run lifetime and recovery

Checks run in the foreground for the CLI and the console, which expect the
verdict when the call returns. MCP starts a detached supervisor and returns a
run id promptly, then queries the durable record later. No permanent scheduler
daemon is needed.

A lost MCP connection does not imply cancellation. A cancel request names a run and attempt, verifies process identity beyond a bare PID, and requests shutdown of the owned process tree. On uncertain liveness, preserve the resource claim and mark recovery needed. Time expiry alone does not justify assigning a live worker's writable resource to someone else.

Use Windows Job Objects and tested POSIX process-group handling. If safe process ownership cannot be established, fail before launching uncontrolled work. Do not claim containment of arbitrary hostile code.

## Public commands and MCP tools

Introduce commands only in their owning plan:

| Plan | Commands |
| --- | --- |
| 01 | `vkit doctor`, `vkit check run`, `vkit run show`, with `--project` and `--json` |
| 02 | `vkit task begin`, `vkit task finalize`, `vkit check start`, `vkit run cancel`, `vkit recover` |
| 03 | `vkit mcp serve --project <root>` |
| 04 | `vkit hook <event>` as an internal integration entry point |
| 05 | `vkit setup`, `vkit integration status`, `repair`, `remove` |
| 06 | `vkit project inspect`, `enroll`, `vkit features` |
| 07 | `vkit integration verify` |

Plan 01 enrolls its example through an explicit test configuration, not a fake setup wizard. Production guided enrollment arrives in Plans 05-06.

Initial MCP tools are `project_inspect`, `task_begin`, `check_start`, `run_get`, `run_cancel`, and `task_finalize`. Six tools are sufficient. `project_inspect` returns selected capabilities/features and missing prerequisites. Optional filters prevent dumping the whole repository map. Add tools only for a demonstrated missing user operation.

Bind each MCP process to one configured project root. Before enrollment, expose read-only inspection with enrollment instructions; execution and task mutation remain unavailable. No tool accepts a new arbitrary root, raw command, integration approval, or plugin-install request. IDs must belong to that project. Return structured results, concise summaries, and artifact references; paginate logs. State-changing requests carry idempotency keys where client retries could duplicate work. Same key plus different payload is an error.

## Authority and installation

Installing the plugin is not blanket permission to spawn agents, install more packages, or merge. Follow the user's active session and project policy. The planning session that produced these documents authorized no delegation; the repository owner has since authorized bounded parallel subagents for implementation, recorded per plan. Future builders can use additional skills, but skill instructions do not override explicit user limits. A plan's own delegation paragraph is the authority for that plan and outranks any earlier planning-session default.

The setup app asks the user to select components and scope, then displays exact changes before applying them. Subsequent operations already authorized by that selection should finish without repeated questions. Use official host installation mechanisms where available. Protect unrelated settings and preserve recoverable prior values.

MCP, CLI, and hooks share the core. Hooks are fast local checks over existing records. They must not execute long tests, invoke a model, or contact the network. A hook error never creates acceptance.

## Completion evidence

For each milestone provide actual commands, exit codes, expected-failure observations, artifact paths, supported environments, and unresolved limits. Record the implemented interface changes in plans/STATUS.md. Do not claim Windows, Linux, Claude hook, live-model, or high-concurrency support from a test double alone.

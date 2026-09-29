# Plan 01: ship the executable verifier

Read [CONTRACT.md](CONTRACT.md) first. Status: ready to implement now. No other product milestone is required.

## Outcome

A user can install the Python package in an isolated environment, invoke a registered check in a Git repository, and inspect a durable PASS, FAIL, or BLOCKED report. A real defect fails. Missing evidence cannot pass. This is the first usable application capability.

Use one engineering owner for integration. Apply `poteto-mode`, `ponytail`, and `principle-prove-it-works`; use `tdd` for the cheap failure cases below.

**Delegation (owner-corrected 2026-09-29).** The original text here read "No
subagents are authorized by this plan." That is superseded. The repository owner
authorized up to six parallel subagents, writing in separate git worktrees under
the `git-worktree-discipline` skill, with disjoint file ownership decided before
the first worktree opens. The owner merges serially in the main checkout and
owns acceptance. This paragraph is the standing authority; a fresh session or a
compacted context that reads only this plan must find it here rather than
re-derive permission from a transcript. `poteto-mode` requires asking before
fanning out, and the answer for this plan is already recorded as yes.

`ponytail` is not in `~/.claude/skills`. It ships inside the Codex plugin cache at
`~/.codex/plugins/cache/ponytail/ponytail/<version>/skills/ponytail/SKILL.md`.
Read it from there or skip it; do not report it as missing.

## Starting work

Inspect the actual workspace and preserve the research documents. If application code now exists, trace and reuse it before creating anything. Add the Python package, console entry point, dependency declaration, package build configuration, and necessary behavior tests. Choose a maintained build backend and record tested dependency versions. No MCP, hooks, setup wizard, or speculative adapter interfaces in this milestone.

The public commands are:

```text
vkit doctor --project <repo> --json
vkit check run --project <repo> --check <id> --json
vkit run show --project <repo> --run <id> --json
```

Human output and JSON output describe the same result. JSON mode writes one structured response to stdout; diagnostic logs go to stderr. Exit codes follow CONTRACT.md.

## Implement

1. Resolve the repository root, Git common directory, manifest, and evidence location. Reject unsupported schema versions, duplicate check IDs, unknown check selections, path escapes, invalid argument arrays, and nonpositive or unbounded timeouts before execution.
2. Define schema version 1 for the manifest, check artifact, and run report. Package the schemas so validation works after installation outside the source checkout. Internal Python records must not become a second incompatible schema.
3. Implement explicit prerequisite checks. `doctor` reports missing tools/configuration without launching application checks or installing anything.
4. Register the run and create its artifact directory before launching a subprocess. Use SQLite only for run registration at this stage. Capture source/configuration identities, declared fixture identity, exact argv, cwd, and necessary environment facts.
5. Execute the registered command. Stream stdout/stderr to owned files, bound in-memory output, enforce timeout, and own descendants. On Windows, establish the Job Object before descendants can escape; test the suspended-launch/resume sequence or another demonstrably correct native mechanism. On POSIX, use process-group handling and report the supported boundary.
6. Validate the check artifact and required scenarios, then recompute source identity. Derive outcome from the actual process and artifact. An internal exception must preserve diagnostic evidence and leave no successful terminal record.
7. Atomically publish the final report and make it readable by run ID. Artifact references must resolve inside the run directory after canonicalization; reports must not reference a worker's temporary checkout for their retained logs.

The function that executes a registered check should be usable directly by the foreground CLI and the later per-run supervisor. Do not make the core call its own CLI to perform ordinary internal operations.

## Concrete example

Add a small Python application under `examples/python-cli/` that totals integer item amounts. Its real CLI prints a total. A verification driver invokes that CLI and compares the actual output with literal expected totals for at least an empty input, positive inputs, and mixed-sign inputs. The driver writes the versioned check artifact.

The example has a real `verification/manifest.json`, a short behavior description, and reproducible setup. Tests may copy it into a temporary Git repository. A test introduces an arithmetic defect into the application and demonstrates FAIL through `vkit check run`; do not make the driver simply emit a requested pass/fail flag.

The implementation should document one exact command sequence to build/install the package, initialize the example repository, run the check, and inspect the result. Execute that sequence from outside the package source directory.

## Acceptance

| Exercise | Required evidence |
| --- | --- |
| Installed package drives the example | All expected scenarios appear with PASS and actual command provenance |
| Introduced arithmetic defect | FAIL and an observation showing expected versus actual behavior |
| Tool missing | BLOCKED naming that prerequisite; no subprocess claimed as executed |
| Command returns zero but omits artifact | BLOCKED, never PASS |
| Empty or malformed artifact | BLOCKED with validation diagnosis |
| Unknown check or schema | Rejected before launch |
| Source changes during execution | BLOCKED with stale-source reason |
| Dirty development tree | Digest and dirty status reported; no integration-readiness claim |
| Timeout with child and grandchild | Owned descendants stopped and readable terminal report retained |
| Large output | No pipe deadlock or unbounded in-memory accumulation |
| Repository path has spaces/non-ASCII text | Exact executable, cwd, and artifact paths preserved |
| Retire the copied test checkout | Evidence remains available in retained state storage, or explicit export precedes deletion of the entire temporary repository |
| Interrupted final report write | No complete PASS inferred from partial output |

Failure tests may use tiny purpose-built process helpers. They must exercise the actual installed command path rather than only imported helper functions. Do not require a live model session for this milestone.

## Finish and handoff

Return the package layout, frozen schema examples, actual command results, Windows/POSIX support actually tested, and known limits. Include the defect countercheck. Update Plan 01 in STATUS.md only after the mandatory tests pass on the implementation host. An untested other OS is a declared release limitation for Plan 09.

Plan 02 consumes the execution function, run registration, artifact layout, and result semantics. Record any justified change to CONTRACT.md now, with its reason, so later owners do not build against an obsolete interface.

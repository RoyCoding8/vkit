# Verification reference

## Model

A check is an entry in `verification/manifest.json` (schema version 2, [schema](../schemas/manifest.v2.json)).
It runs only after a human accepts its exact definition with `vkit accept`. Any change to the entry changes its
digest, and the changed check is BLOCKED with `not_approved` until it is accepted again.

Each run records evidence under a key made of three digests:

- the check's definition,
- the content of its inputs (CRLF folded to LF),
- the pinned baseline, when the check has one.

`inputs` lists files, directories or globs. An empty list means every tracked and untracked-but-not-ignored file,
which is always safe. Files that look like secrets (`.env*`, private keys, credential files) are never read.

A check's state is computed from the store and the files on disk:

| State | Meaning |
| --- | --- |
| `fresh_pass`, `fresh_fail`, `fresh_blocked` | The latest run for the current key had this result |
| `stale` | The check has run, but not against the current inputs |
| `missing` | The check has never run |
| `not_approved` | The current definition has not been accepted |

The gate is READY when every check is `fresh_pass`, REJECTED when any is `fresh_fail`, and BLOCKED otherwise.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| PASS | The check's own report satisfied every required obligation |
| FAIL | The report shows a required obligation failed, or a budget was exceeded |
| BLOCKED | No decision. The reason is one of `not_approved`, `input_missing`, `prerequisite_missing`, `tool_missing`, `timeout`, `cancelled`, `launch_failed`, `artifact_missing`, `artifact_malformed`, `artifact_empty`, `scenario_unknown`, `source_changed` |

An exit code alone never establishes PASS. Each kind writes a structured report that its adapter reads. A run
whose inputs change while it executes is BLOCKED with `source_changed`.

## Kinds

| Kind | Report | Obligations |
| --- | --- | --- |
| `scenario` | [check-artifact.v1.json](../schemas/check-artifact.v1.json): each scenario's id, PASS or FAIL, observation, and optional measurements | `required_scenarios`, `budgets` |
| `pytest`, `property` | JSON from vkit's pytest plugin | `required_tests`; `property` also pins Hypothesis settings |
| `node_test` | TAP from vkit's Node reporter | `required_tests` |
| `static` | SARIF 2.1 | no finding outside the pinned baseline |
| `lean` | the Lean runner or comparator report | `theorems`, `permitted_axioms` |
| `tlc` | TLC output | `properties` at the declared `bounds` |

Commands are argument arrays with two placeholders, `{{run_dir}}` and `{{python}}`, and are never passed to a
shell. A check runs with vkit's source on `PYTHONPATH`, inside a Windows Job Object or a POSIX process group, so
timeout and cancel stop everything it started.

### Budgets and baselines

A scenario artifact may report `measurements` (`name`, `value`, `unit`, `better`). A `budgets` entry fails the
run when a measurement passes its `limit` in the worse direction, or regresses more than `max_regression_pct`
from the pinned baseline. A regression budget with no baseline is reported in `unchecked_budgets`, not failed.
`vkit baseline --check ID` pins the measurements and static findings of a finished run.

### Lean

The `reviewed_proof_sources` profile rechecks human-reviewed sources with the kernel and audits axioms. The
`unreviewed_agent` profile checks an agent's `solution` module against the `challenge` with
[comparator](https://github.com/leanprover/comparator). It also requires `challenge_sha256`, so editing the
statements blocks the check until a human accepts the new digest. See [examples/lean-proof](../examples/lean-proof/README.md).

## Proposals

Agents call the MCP `propose` tool with manifest entries, feature entries and new files. A proposal may only add
files. `vkit proposals` lists pending ones. `vkit accept --proposal <digest>` writes them into the working tree
and accepts their check definitions. `vkit reject` discards one.

## Feature map

`verification/features.json`, schema version 2. Each feature has an `id`, a `behavior`, `how_to_reach` steps,
`entry_points`, the checks that cover it (`covered_by`), and known `gaps`. A feature is verified when it has no
gaps, its entry points exist, and every covering check is `fresh_pass`. The map holds no expected results;
those live in the checks.

## Storage

Everything lives under `<git common dir>/vkit/`, so every worktree of a repository shares it. That covers run
records and logs, evidence pointers, accepted digests, baselines and proposals. A run holds an OS file lock while
it lives. A run that reads as `running` but whose lock is free is reported as `interrupted`.

## Local console

`vkit console --project <repo>` serves the console on `127.0.0.1`. It can start registered checks,
request cancellation, and invoke installed computations. Starting a check preserves the CLI/MCP
acceptance requirement. Computation results do not create gate evidence. Accepting definitions and
applying proposals remain CLI actions.

Setup & tools saves display settings in `<git common dir>/vkit/config.json`. The file is local Git
metadata, shared by the repository's worktrees and excluded from commits. It controls the theme,
collapsed sidebar, refresh interval, and run-history limit. It cannot change verifier rules, executable
paths, check definitions, or accepted digests.

Write requests require the console's same-origin session token. The server binds only to loopback;
it is a local operator interface, not a remote multi-user administration service.

## Exit codes

0 PASS or READY, 1 FAIL or REJECTED, 2 invalid request, 3 BLOCKED, 4 internal error, 5 unavailable.

---
name: vkit-work
description: Make a change in a repository vkit verifies and prove it with recorded evidence. Use when implementing, fixing, or reviewing code in a repository that has verification/manifest.json, or when the vkit Stop hook says checks are stale.
---

# Work under VKit

## Evidence model

| Item | Meaning |
| --- | --- |
| Registered check | Records scoped evidence about the current accepted definition and inputs. |
| Formal component | Checks declared source APIs against an accepted owned mathematical package. |
| Input edit | Makes evidence stale for checks that read those inputs. |
| Checker or policy change | Can also invalidate evidence and require renewed acceptance. |
| Passing evidence | Retains the check's category and scope. It does not establish general correctness. |

## Discover the interface

| Step or mode | Action |
| --- | --- |
| Discovery | Call `help` to find enabled categories and tools. |
| Exact contract | Call `help(name=...)` before using the tool. |
| Compact exposure | Use the names below as `invoke(name=..., arguments={...})` targets. |
| Named exposure | Call the tool directly. |
| Unavailable repository category | The work loop cannot run. |

## Work loop

1. Before editing, call `status` with the paths you expect to touch. Read the affected checks, evidence limits, and covered features.
2. If the report is vague, use `features` with a query to find the behavior and how a user reaches it.
3. Make the change.
4. Call `check_run` with `needed: true` to run the needed checks.
   For required components listed in `formal_needs_run`, call `verify` with the component, the `implements` goal and the intended source selector. Read its exact schema first.
5. Read each attempt with `run_get`. A FAIL reports the failed scenarios or tests and their observations.
6. Fix failures and repeat until `gate` returns READY.

## Read gate results

| Gate | Meaning | Action |
| --- | --- | --- |
| READY | All registered checks and required formal components have current passing evidence. | Report the evidence category and scope of each result. |
| REJECTED | A check ran and failed. | Fix the code. Do not weaken the check. |
| BLOCKED | No valid answer yet. | Read the reason and resolve it before claiming a pass. |

| Blocked reason | Response |
| --- | --- |
| `stale`, `missing` | Run the check. |
| `not_approved` | A human must accept the definition through `vkit accept`. |
| `prerequisite_missing` | Report the missing tool. |
| `timeout` | Report that the run did not reach a valid decision. |
| `source_changed` | Files changed during the run. Run again. |

A scenario PASS establishes the observed results for those cases. It does not establish correctness for all inputs.

## Rules

- A check that has not run has not passed. Never report an attempt you have not read.
- Do not add or change what a check runs. Changed definitions remain BLOCKED until a human accepts them.
- If a check is wrong, explain the problem and use `propose`. Do not edit the manifest to force a pass.
- When a fix prevents a recurring mistake, propose a check that would have caught it. Use a static rule for a banned pattern or a scenario for behavior.
- READY is evidence, not permission to commit, push, or merge.

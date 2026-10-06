---
name: vkit-work
description: Make a change in a vkit-enrolled repository and prove it with recorded evidence. Use when implementing, fixing, or reviewing code in a repository that has verification/manifest.json, or when the vkit Stop hook says checks are stale.
---

# Work under vkit

vkit records what each registered check established about the code as it is now. Evidence is keyed by the
content of each check's inputs, so an edit makes the evidence for that check stale and nothing else.

## Loop

1. Before editing, call `status` with the paths you expect to touch. It lists the checks that read those paths,
   what each kind of evidence establishes and does not, and the features those checks cover. `features` with
   a query finds what a vague report refers to and how a user reaches it.
2. Make the change.
3. Call `check_run` with `needed: true`. It runs exactly the checks whose inputs changed. Read each run with
   `run_get`; a FAIL lists the scenarios or tests that failed and what was observed.
4. Fix what failed and repeat until `gate` returns READY.

## Reading results

- READY: every registered check passed against the current inputs. Report it with the category of each check:
  a scenario PASS says these cases produced these results, not that the code is correct in general.
- REJECTED: a check ran and failed. That is an answer; fix the code, not the check.
- BLOCKED: no answer yet. The reason says why: `stale` or `missing` (run it), `not_approved` (a human must run
  `vkit accept`), `prerequisite_missing` (a tool is not installed), `timeout`, `source_changed` (files changed
  during the run; run again).

## Rules

- A check that has not run has not passed. Never report a run you have not read.
- You cannot add or change what a check runs. A changed definition is BLOCKED until a human accepts it. If a
  check is wrong, say so and propose the change; do not edit the manifest to make it pass.
- Do not commit, push, or merge because the gate is READY. READY is evidence, not permission.

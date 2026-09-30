---
name: vkit-status
description: Report the state of a managed vkit task and its outstanding evidence. Use when asked how a task is doing, what evidence is missing, or why a gate is not satisfied. Read-only; it does not run checks or change anything.
---

# Status of a managed task

Report what the store records. This skill reads and does not act.

## Steps

1. Identify the task. Use the task id from `task_begin` or from the user's
   message. Do not pick the most recent task because it looks relevant.
2. `task_finalize` for that task id. It returns the computed readiness and the
   gaps, from recorded runs only.
3. `run_get` for the runs the gaps name, when the user wants the detail behind
   a specific gap.

## Reporting

Lead with the computed value: READY, REJECTED, or BLOCKED, and the task id it
belongs to.

Then the gaps, each with what would clear it. A required check with no
completed run needs `check_start`. A required check that ran and failed needs
the failure fixed. A required check that is BLOCKED needs the reason addressed
first, because the failure the check would have reported is still unknown.

## Rules

- READY is local readiness under the recorded contract. It is not merge
  permission and it is not a claim about the whole task.
- Do not run a check from this skill. It is a status report. If the user wants
  evidence produced, that is `/vkit:verify` or `/vkit:work`.
- Do not change a task's contract, close it, or reassign it. A contract is
  pinned when the task opens.

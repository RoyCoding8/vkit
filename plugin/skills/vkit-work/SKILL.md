---
name: vkit-work
description: Work a managed engineering task through the vkit app. Use when the user opens a task contract, or asks to implement a change under verification. Inspects the project, implements, produces recorded evidence through check_start, and finalizes with task_finalize.
---

# Work a managed task

A vkit task is a contract with a fixed set of required checks. The app decides
whether the task is complete, from recorded runs, and nothing here decides it.

## Order

1. `project_inspect` on the vkit MCP server. It reports the enrolled project,
   its registered check ids, and anything missing. If it reports that the
   project is not enrolled, stop and say so. Enrolling is an explicit user
   action this skill does not perform.
2. `task_begin` with the contract the user supplied. When the SessionStart hook
   supplies a host binding, pass that object as `host` so Stop checks this task;
   for a subagent, use the session_id and agent_id from SubagentStart. Omit it
   for work outside a managed host session. It returns a task id,
   a generation, and the required check ids. Keep both. A later call that
   returns a different generation is a different attempt, and the earlier one
   can no longer finalize.
3. Implement the change with the normal editing tools and the pstack guidance
   that fits the work. Check that the guidance you relied on was actually
   loaded. If a pstack skill you needed was missing or disabled, say that in
   your final report rather than describing a procedure you did not run.
4. Produce evidence with `check_start`, once per required check id. Pass a
   registered check id. The server refuses a raw command, and that refusal is
   the guarantee this skill relies on.
5. Read each run with `run_get` and inspect the failures it names. A run that
   is BLOCKED says why, and the reason, not the check name, is what you fix.
6. `task_finalize`. It returns READY, REJECTED, or BLOCKED, and the gaps when
   it does not return READY.

## What the verdict means

READY is local engineering readiness under the recorded contract. It is not
merge permission, not a statement that the feature is complete, and not a claim
that the contract captured everything the user asked for. Report it as the
computed value it is.

REJECTED means a required check ran and failed. That is an answer, not a gap.

BLOCKED means the evidence is insufficient to decide. A missing run, a
timeout, a missing tool, a source change mid-run. Fix the reason, or say the
task is blocked and stop. Do not describe a BLOCKED task as nearly done.

## Rules

- A check that has not run is not a check that passed. Never report a run id
  you have not read with `run_get`.
- Do not re-derive readiness. The app computes it. If the hook gate reports a
  task as not complete, that is the same computation, not a second opinion.
- A retry is a new run. Keep the original failure; a later PASS does not erase
  an earlier FAIL, and the app flags that instability itself.
- Do not merge, push, or open a pull request. Accepting the plugin did not
  authorize any of them.

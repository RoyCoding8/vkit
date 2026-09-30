---
name: vkit-verify
description: Run a registered verification path and report the evidence. Use when asked to verify, run the checks, prove a change works, or show why something passes. Reports what a run observed and never claims the whole task is complete.
---

# Verify with a registered check

Run a named check and report what it actually observed.

## Steps

1. `project_inspect` for the registered check ids. Never invent a check name.
2. `check_start` with one registered check id. If the user named a check that
   is not registered, say that rather than substituting a nearby one.
3. `run_get` on the run id `check_start` returned. Report from the record:
   the run id, the outcome, and each scenario's id and result.

## Reporting

Quote the evidence. For each failing scenario give the scenario id and its
observation. For a BLOCKED run give the reason verbatim; a timeout, a missing
tool, and a missing artifact are different problems with different fixes.

Then state the limit of what you ran. One check passing is one check passing.
It says nothing about the required checks you did not run, and nothing about
whether the task as a whole is complete. If the user asked whether the work is
done, that answer is `task_finalize`, not this skill.

## Rules

- Never report a check as passing that you did not read from `run_get`. A
  check that was never run, or whose run id you are guessing at, is not a pass.
- Do not describe a BLOCKED run as a pass because its command exited zero. The
  outcome is the record's, and the record distinguishes those two cases.
- Do not install a tool, enroll a project, or change repository policy to make
  a check pass. A missing prerequisite is BLOCKED, and the fix belongs to the
  user.

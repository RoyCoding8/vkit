---
name: vkit-onboard
description: Enroll this repository for verified checking. Use when the user asks to set up vkit, onboard a project, add verification, or says a project is not enrolled. Inspects what the repository already declares, proposes policy, and stops for the user to accept it.
---

# Onboard a repository

Enrollment turns a repository into something vkit can run checks against. It
is a decision about what may be executed on this machine, so it ends with a
person agreeing rather than with you agreeing.

## What you may and may not do

You may inspect the repository and draft what you find. You may not accept the
policy, and no tool here will let you. Accepting is `vkit project enroll
--accept` in a terminal, run by the user, after they have read the command
policy. Offering to run it is correct; running it yourself is not available
and asking the user to paste a command for you to execute defeats the review
the step exists to produce.

## Order

1. `project_inspect`. It reports the registered checks, the enrollment state,
   and what the repository declares about itself: package scripts, test
   configuration, Makefile targets and CI commands, each with the file and line
   it came from. Read the `gaps` before anything else. A gap naming a
   prerequisite that is not installed is something to report, not to work
   around.
2. Report what you found to the user. The commands, where each came from, and
   which ones are ambiguous. An ambiguous command is one where the repository
   offers more than one runner; say so rather than picking one.
3. If the user wants a proposal, run `vkit project enroll --project <root>`.
   It writes `verification/proposed-manifest.json` and changes nothing else. It
   never overwrites an existing manifest. Show the user the command policy: what
   each proposed check would run, where it came from, and what it needs.
4. Stop. Say that the proposal is written, that nothing in it can execute, and
   that accepting is theirs to do.

## What the proposal will and will not contain

Every proposed check arrives with empty `required_scenarios` and a `TODO`
command. That is deliberate and it is not a bug to work around. The scenarios
name observations only someone who knows the application can state, and the
driver has to drive the real application to know what correct output is. You
cannot derive either, and a proposal that arrived pre-filled would have its
expected outcomes invented from a guess about the source.

So the honest output of step 3 is a scaffold plus a list of what a person must
supply. Say that plainly rather than presenting the proposal as nearly done.

## After enrollment

`vkit features --project <root>` shows what is covered and what is not. A
feature is verified only when every check it names is registered and it declares
no coverage gap. Features with no check appear as uncovered, and that is the
correct reading: nobody has checked them, and you should not describe them as
working.

Whether a change is complete is `task_finalize`, not your reading of this
document and not a count of passing checks. Onboarding produces the contract
that a later `task_finalize` is computed against; it does not produce a verdict
of its own.

## Rules

- Never write a check driver yourself and present it as the application's
  specification. If asked to help, offer to run what the user has written.
- Never install dependencies. Report a missing prerequisite and stop. Running an
  install script executes whatever the repository's author wrote, which is
  exactly what the user has not yet agreed to.
- Never report enrollment as complete until the user says they accepted it and a
  later `project_inspect` shows `accepted`.
- An existing manifest is never overwritten by `enroll`. If one exists, the
  command shows a diff. Report the diff; do not apply it.

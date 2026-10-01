# Integration CI, and what has actually been run

Two different CI stories live in this repository and only one of them has run.
`.github/workflows/ci.yml` runs the suite on three operating systems and is
green. `examples/ci/integration.yml` is Plan 07's integration example and has
never run on the forge. The two are unrelated files, and this document is about
both, so it separates them before it says anything else.

Plan 07 requires a CI integration example for the repository's existing
provider, and requires that a demonstration repository name the pinned baseline
its tests use. Both are here. Neither has been executed on a forge.

## The integration example is not executed

`examples/ci/integration.yml` has never run on the forge. No workflow on the
remote references it, no pull request or merge group has triggered it, and the
repository's branch protection is unconfigured, so nothing gates a merge on it.
Reading it as a working integration is exactly the failure this product exists
to prevent, so:

- The workflow's syntax, its expression evaluation, and its behaviour on a
  `pull_request` and on a `merge_group` are **unverified**.
- The branch protection rules, the required-check list, and the merge-queue
  setting are **unconfigured**. They have to be set in the repository's settings,
  by a person, and nothing in this repository can set them.
- The claim "a protected job may claim protected integration context" rests on
  two things this plan verified locally: a local policy is refused for protected
  context, and an approved reference is the only spelling that confers it. What
  it has *not* verified is that a GitHub `pull_request` event from a fork is
  untrusted by default, which is the forge property the whole design leans on.

The command the workflow runs, in this exact form, was executed many times against
a real repository. See "What was run" below.

## The suite workflow has run and is green

This workflow is separate from the example above and does something narrower. It
runs `pytest tests/` on `ubuntu-latest`, `windows-latest` and `macos-latest`, and
its verdict is pytest's exit status. It says nothing about integration context,
branch protection, or `vkit integration verify`, and it makes no claim about any
of them.

At revision `2f1749e` the run is green on all three operating systems. Earlier
runs on the same workflow were red, and the reasons are recorded in the GAP-2 row
of the release checklist rather than here. The receipt is the forge's run log, not
a file in this tree, so `tests/test_release_docs.py` derives the POSIX position
from what this repository holds and records `no-receipt`.

## What was run

Everything in the acceptance table was exercised through the public interface, as
real processes, against a real throwaway Git repository with real code and a real
driver. `tests/fixtures.py` builds that repository; `tests/test_integration.py`,
`tests/test_policy.py`, `tests/test_concurrency.py` and `tests/test_recovery07.py`
drive it.

The demonstration repository's pinned baseline is the commit its own
`policy.json` names, and every test reads it from there rather than hard-coding a
sha:

```python
def baseline_revision(repo):
    return json.loads(git(repo, "show", f"main:{POLICY_NAME}"))["manifest_revision"]
```

## The two decisions a fork has to get right

**The candidate is a commit, not a label.** The workflow passes
`github.event.pull_request.merge_commit_sha`, the merge GitHub actually built.
That is the commit that would land, and it is the commit the checks run against.
A branch name or a PR label is not identity and is not used as one anywhere in
the product.

**The policy is a ref, not a file.** `--policy @<ref>` is the only spelling that
confers protected context. A local path is local evidence, the command says so
in its output, and a local file that *claims* `"context": "protected"` is refused
with the way to actually ask named. That refusal is tested
(`tests/test_policy.py`); its enforcement by a forge on an untrusted fork is not,
and cannot be from here.

## What the command does not do

`vkit integration verify` computes and records a decision. It does not push,
merge, tag, or open a pull request. The record says so, the human-readable
output says so, and the workflow's publish job is only a required check that
fails -- the forge's own branch protection and merge queue decide what lands.

## Where the enforcement boundary is

The plan asks for this to be stated explicitly, because a plugin cannot constrain
every arbitrary host tool or external session. What the app enforces:

- Admission to a writer slot, a verification slot, and a named write scope, all
  through one claim table. A caller that does not go through the app is not
  admitted by the app; it is simply not counted.
- One integration verification per repository at a time.
- The executed check set and the executed manifest, both from the approved
  revision.

What the app cannot enforce is a process that ignores it. A second checkout, a
second SQLite file, or an agent with its own tools can run the same checks twice
and reach its own answer. The forge is the layer that refuses to act on anything
but the recorded decision, and that refusal is a branch-protection setting rather
than anything in this repository.

## Known limits, stated rather than implied

- **The driver is repository code.** The approved manifest decides the *command*,
  so a candidate cannot repoint the check at something weaker. It cannot stop the
  candidate from shipping a different `verify_*.py`, because the driver is the
  repository's own code and the repository is what is under test. A driver that
  fabricates a pass is caught by review of the driver, not by the policy layer.
  This is the honest edge of the design, and
  `test_the_approved_manifest_selects_the_command_not_the_candidate` asserts
  exactly the guarantee that does hold rather than a stronger one.
- **A rewrite before the check exits is not detectable from outside it.** The
  trusted launcher captures the artifact after the last process to write it
  exits, so that is the strongest statement available without a sandbox. This is
  not a sandbox against hostile code and is not claimed to be.
- **The POSIX process path has a run, not a receipt.** `.github/workflows/ci.yml`
  runs the suite on `ubuntu-latest` and that job is green at revision `2f1749e`,
  so the path is exercised. No run artifact is tracked in this tree, so the
  record is the forge's run log rather than something a reader of this checkout
  can open. `tests/test_release_docs.py` derives the position as
  `no-receipt` and GAP-2 stays open on exactly that. The launcher inherits the
  same path and the same absence.

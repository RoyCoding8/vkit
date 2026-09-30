# Pilot plan

A pilot is the first real repository, not a demo. It exists to find out
whether this product helps anyone, and it can only answer that if the thing
underneath it works. This document states what must be true before a pilot
starts, and it is deliberately more restrictive than the release checklist.

A pilot on a repository where the tool surface is unreachable teaches you
nothing about the tool. It teaches you that the tool does not start.

## Entry conditions

Every condition below must be true, with a receipt, before a pilot repository
is selected. A condition with no receipt is not met.

| Condition | Why it gates the pilot |
| --- | --- |
| GAP-1 closed | Met. The stdio adapter runs, and `tests/test_mcp_stdio.py` drives it with real JSON-RPC frames. |
| GAP-4 closed | Met. `plugin/.mcp.json` starts `vkit mcp serve`, and `build_parser()` defines it. `serve_stdio` is the adapter that command reaches. |
| GAP-3 closed | The manifest passes `claude plugin validate --strict`, but no live host session has installed it or run a hook. |
| GAP-5 and GAP-6 closed | A pilot operator must be able to enroll a repository. The setup console is Plan 05, still being built, and no `enroll` or `integration verify` subcommand exists. |
| GAP-2 declared | A pilot runs on one named OS. The verified one is Windows 11. |
| GAP-7 and GAP-8 handled | The pilot baseline and report are written around a missing acceptance table. |
| An owner-approved repository | The implementation worker does not pick this. |
| Authorized live-model usage | A pilot spends tokens. The limit is set before the run. |

GAP-1 and GAP-4 are met, and their receipts are in `RELEASE-CHECKLIST.md`. The
rest are not. The list is the entry gate, not a progress report, and a met row
is as much a claim to re-check as an unmet one.

## The two gates, and why a pilot needs both

A pilot can proceed while GAP-2 stays open, because a pilot can be run on the
one operating system that was verified. It cannot proceed while GAP-3, GAP-5,
or GAP-6 stay open, because each of those removes the pilot's subject: the
plugin is never shown to load in a live host, enrollment does not exist, and
the operator cannot set the pilot up through a real flow. The tool surface the
pilot agent would call is now reachable; whether the host reaches it is GAP-3,
and that is the question a pilot exists to answer.

The POSIX gap is a release limitation, because the POSIX process path is
unverified and the whole package was exercised on Windows 11 only. The rest
are pilot blockers. That distinction is the reason this document is separate
from the checklist.

## Selecting the repository

The repository owner selects it. An implementation worker does not choose an
unrelated personal project, and no worker inspects credentials to make a pilot
proceed.

The repository must have a normal test path that runs today, before vkit is
involved. A repository with no runnable test command cannot demonstrate a
caught failure, and a caught failure is the whole product. It must be a
repository where a bug fix or a small feature is genuinely useful, so a
reviewer can tell whether the result was worth the effort.

## Measuring the baseline without lying about it

GAP-7 applies to the pilot baseline. `scripts/acceptance.py` resolves the
command under test from the interpreter that runs it, so a rehearsal in a
developer's virtualenv measures whichever `vkit` that environment holds. In a
worktree that is an editable install pointing at another checkout.

Run the pilot baseline from a virtualenv holding the built wheel, and record
the revision it reports. A baseline measured against the wrong build produces
a comparison that looks like an improvement and is a measurement error.

GAP-8 applies to the reporting shape. The plan asks for every row of an
acceptance table to be mapped, and `KIT_ACCEPTANCE.md` is absent, so there is
nothing to map yet. The pilot report records the gaps that were open instead,
and the acceptance table is written when the release candidate is assembled.

## What a pilot run does

Establish the baseline first. Run the repository's own test path and record
what it does today, including how long it takes and what it reports. Without a
baseline, a later improvement is an opinion.

Then ground the feature map in the actual source, choose one useful bug fix or
small feature, and implement it through the kit's own workflow. Record the
feature document, the check contract, and the runs that backed it.

Stop at a reviewable result. Merging and publishing are separate authorized
actions, and a pilot that merges on its own has exceeded what it was asked.

The comparison runs the same bounded task set with existing pstack alone and
with pstack plus vkit, from fresh starting states and comparable settings. The
task set covers a bug, a feature, a refactor, a combined change that
conflicts, and an interruption. Record every outcome, including the failures.

## What to measure

| Measure | Why it is here |
| --- | --- |
| Accepted outcomes | Whether the result was right, decided by the recorded evidence. |
| False passes | A check that passed a broken result is worse than no check. |
| Failures caught before integration | The value of running the check early. |
| Human interventions | What the kit did not remove from the human's job. |
| Repair attempts | Whether an interrupted run is recoverable in practice. |
| Elapsed time | Split into total and verification time. |
| Usage and cost | Only where the provider actually reports it. |

Do not use agent count or pull-request count as a success measure. Both reward
turning one problem into many pieces, which is the opposite of the product.

Unknown cost stays unknown. Writing an estimate where no provider reported a
figure turns a measurement into a claim.

GAP-9 bounds the false-pass count. A `.cmd` launcher cannot carry a non-ASCII
path, and a source digest cannot see an edit that is reverted while a check
runs. A pilot repository whose path is non-ASCII must not register a `.cmd`
check, or its results are untrustworthy in a way the report will not announce.

## Scale is not a pilot result

Synthetic process contention establishes local coordination behavior on this
host. It does not establish engineering quality at 100 agents, and the two
forms of evidence must be reported separately. An agent count is a load
setting, not a quality result.

If a larger run is authorized later, declare the host agent limit, the
registered writer limit, the verification capacity, the spending limit, and
the stop condition before it starts. Increase only when accepted throughput
improves without unexplained false passes, resource leakage, or a growing
integration backlog. There is no automatic jump to 100 or to 1000.

## Reporting

The pilot report names the actual platforms, host versions, SDK versions,
models exercised, plugin version, and candidate revision. It states which
gaps from [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) were still open during
the run, because a pilot performed on a build with GAP-1 open says nothing
about the build that closes it.

Where a result is unavailable, the report says so. "Not measured" is a
complete and acceptable entry. A filled-in guess is not.

## Files this plan relies on

```text files
# The plan this pilot implements, and the release gate it depends on.
plans/09-release-and-pilot.md
plans/CONTRACT.md
plans/STATUS.md
docs/RELEASE-CHECKLIST.md

# The plugin manifest whose server command is GAP-1 and GAP-4.
plugin/.mcp.json

# The receipt that GAP-1 is met, and the client that produced it.
tests/test_mcp_stdio.py
tests/mcp_client.py

# The two applications a pilot can be rehearsed against.
examples/python-cli
examples/node-cli

# The acceptance walker, and this document's own gate.
scripts/acceptance.py
tests/test_release_docs.py
```

These paths are named here because they do not exist. The gate fails if one
appears, which is the moment a gap closes.

```text absent
KIT_ACCEPTANCE.md
```

## Commands available for a rehearsal

These run today against the examples. A rehearsal exercises the harness, not
the agent integration, so it does not satisfy any entry condition.

```console command
$ vkit doctor --project <enrolled-repo>
$ vkit check run --project <enrolled-repo> --check <check-id>
$ <venv>/Scripts/python.exe -m pytest tests/test_release_docs.py
```

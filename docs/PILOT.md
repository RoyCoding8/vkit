# Pilot plan

Historical release reference with revision-bound regression assertions.
It is not the current implementation queue or a current release verdict.
Use the root master plan for current work and the repair-verification record
for later receipts. This document retains historical regression inputs.

A pilot is the first real repository, not a demo. It exists to find out whether
this product helps anyone, and it can only answer that if the thing underneath
it works. This document states what must be true before a pilot starts.

A pilot on a repository where the tool surface is unreachable teaches you
nothing about the tool. It teaches you that the tool does not start.

## Entry conditions

Every condition below must be true, with a receipt, before a pilot repository
is selected. A condition with no receipt is not met.

| Condition | Why it gates the pilot |
| --- | --- |
| GAP-1 closed | Met. The stdio adapter runs, and `tests/test_mcp_stdio.py` drives it with real JSON-RPC frames. |
| GAP-4 closed | Met. `plugin/.mcp.json` starts `vkit mcp serve`, and `build_parser()` defines it. `serve_stdio` is the adapter that command reaches, and `tests/test_mcp_stdio.py` drives it. |
| GAP-3 closed | Met, on the receipt rather than on a green suite. A live Claude Code host session installed this plugin, connected its MCP server, attached the six tools, and delivered SessionStart, PreToolUse and Stop. `docs/HOST-SESSION.md` quotes it; `tests/test_plugin_host.py` drives it. |
| GAP-10 met | **Not met.** No live session has put a subagent behind the six tools, or stopped a real turn with a BLOCKED run. `tests/test_plugin_host.py` drives neither, so a pilot is still the first thing that would. |
| GAP-5 and GAP-6 met | **Not met.** A pilot operator must be able to enroll a repository. `vkit project enroll` and `vkit integration verify` exist, with 31 collected tests behind them in `tests/test_enroll.py`, `tests/test_integration.py` and `tests/test_plan06_onboarding.py`, so the commands are not the obstacle; the console that walks an operator through them is Plan 05, and no finished flow drives them yet. |
| GAP-2 declared | A pilot runs on one named OS. POSIX-CLAIM: no-receipt, derived by `tests/test_release_docs.py::_posix_position_derived_from_the_tree`. No run of this suite is recorded in this repository for either family. The package was executed on Windows 11 with Python 3.13.14, so name Windows 11, and record that no full suite run backs the name. |
| GAP-7 and GAP-8 handled | The pilot baseline must be measured against a built wheel, and the report is written around 59 acceptance rows, none of which is unmapped. |
| An owner-approved repository | The implementation worker does not pick this. |
| Authorized live-model usage | A pilot spends tokens. The limit is set before the run. |

GAP-1, GAP-3 and GAP-4 are met, and their receipts are in
`RELEASE-CHECKLIST.md`. The rest are not. The list is the entry gate, not a
progress report, and a met row is as much a claim to re-check as an unmet one.

## Why this document is separate from the checklist

GAP-2 is a release limitation, not a pilot blocker. No POSIX suite run is
recorded in this repository, but a pilot can run on Windows 11, which is the
host the package was executed on. GAP-5, GAP-6 and GAP-10 are pilot blockers,
because each removes the pilot's subject: enrollment has no flow to drive, the
operator cannot be set up, and no session has yet run a BLOCKED run through an
agent's turn.

The tool surface the pilot agent would call is reachable, and the host reaches
it, which GAP-3's session settled. Whether a managed task constrains a real turn
is GAP-10, and that is the question a pilot exists to answer.

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

GAP-7 applies to the pilot baseline. `scripts/acceptance.py` pins its child's
`PYTHONPATH` to the `src` tree beside the script, and exits 4 when `vkit`
resolves anywhere else, so it measures the tree it was read from. Before that
guard, a rehearsal in a developer's virtualenv measured whichever `vkit` the
environment already held, which in a worktree is an editable install pointing at
another checkout. What stays unproved is the old resolution itself, because the
one test that would show the pre-fix expression reaching a second checkout skips
on any host whose `vkit` is an editable install of the tree under test.

Run the pilot baseline from a virtualenv holding the built wheel anyway, and
record the revision it reports. The script's own guard and a wheel are
different checks, and only the second exercises the artifact a pilot ships.

GAP-8 applies to the reporting shape. `docs/PILOT.md` asks for
every row of an acceptance table to be mapped, and the matrix is tracked at
`docs/ACCEPTANCE-MATRIX.md` with 59 data rows, none of which is unmapped. So
there is nothing to map against yet, and the pilot report records the gaps that
were open instead. The mapping is written when the release candidate is
assembled.

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

GAP-9 bounds the false-pass count. A `.cmd` launcher whose own bytes the active
ANSI code page cannot represent does not run, so a pilot repository whose path
is non-ASCII must not register a `.cmd` check. A source digest cannot see an
edit that is reverted while a check runs. Both are demonstrated rather than
detected, and the report has to say so, or its results are untrustworthy in a
way nothing else will announce.

## Scale is not a pilot result

Synthetic process contention establishes local coordination behavior on this
host. It does not establish engineering quality at 100 agents, and the two forms
of evidence must be reported separately. An agent count is a load setting, not a
quality result.

A pilot report names the limits it ran under, so a later authorized increase is
compared against something: the host agent limit, the registered writer limit,
the verification capacity, the spending limit, and the stop condition. Plan 09
sets the rule for raising them, which is accepted throughput improving with no
unexplained false passes, no resource leakage, and no growing integration
backlog.

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
docs/PILOT.md
plans/CONTRACT.md
README.md
docs/RELEASE-CHECKLIST.md

# GAP-8's acceptance matrix, whose 59 data rows the report is written around.
docs/ACCEPTANCE-MATRIX.md

# The plugin manifest whose server command is GAP-1 and GAP-4.
plugin/.mcp.json

# The receipt that GAP-1 is met, and the client that produced it.
tests/test_mcp_stdio.py
tests/mcp_client.py

# The receipt that GAP-3 is met, and the driver that produced it.
docs/HOST-SESSION.md
tests/test_plugin_host.py

# Two applications a pilot can be rehearsed against.
examples/python-cli
examples/node-cli

# The acceptance walker, and this document's own gate.
scripts/acceptance.py
tests/test_release_docs.py

# GAP-5 and GAP-6's named test files. A total with no filenames behind it can
# only be checked by guessing which files the author meant, so the files are
# listed and the gate resolves each one.
tests/test_enroll.py
tests/test_integration.py
tests/test_plan06_onboarding.py
```

These paths all exist in this repository. The gate resolves each one against
the committed tree, so an entry here cannot name a file no reader can open.

```text absent
# GAP-8's mapping has not been written. The matrix is listed under `text files`
# above; this is the artifact the mapping would produce, and the gate checks it
# stays absent until the mapping does.
GAP-8-MAPPING.md
```

## Commands available for a rehearsal

These run today against the examples. A rehearsal exercises the harness, not the
agent integration, so it does not satisfy any entry condition.

```console command
$ vkit doctor --project <enrolled-repo>
$ vkit project enroll --project <enrolled-repo>
$ vkit integration verify --project <enrolled-repo>
$ vkit check run --project <enrolled-repo> --check <check-id>
$ <venv>/Scripts/python.exe -m pytest tests/test_release_docs.py
```

`vkit project enroll` and `vkit integration verify` are the two commands the
console would drive for an operator. They are listed here because GAP-6 is
about the flow around them, and a rehearsal has to be able to run the commands
it is being blocked on.

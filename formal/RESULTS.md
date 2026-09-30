# Plan 08 results: what was checked, at which bounds, and what that does not cover

This file is the receipt for Plan 08. Read the scope sentences. A run that
produced no counterexample is a real result, but it is a result about a bounded
thing, and the bound is the interesting part.

Everything here was measured on the host named below. Nothing is quoted from a
document, a plan, or a memory of a previous run.

| | |
| --- | --- |
| Host | Windows 11 10.0.26200, x86_64 |
| Python | 3.13.14 |
| Git | 2.54.0 |
| JRE | Eclipse Adoptium 17.0.20.1+1 (portable, under `tmp/`, not committed) |
| TLC | 2.19, rev 5a47802, `tla2tools.jar` sha256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88` |
| Lean | 4.32.2, commit f3b06c705e6c85f5314019d5d3baab0fec5b580c |
| Hypothesis | 6.168.3 |

## Deliverable A: finite model checking. Ran. PASS.

`formal/tla/OwnershipAcceptance.tla`, run by `formal/run_tlc.py`.

**The configuration the result is scoped to.** Two owners, two exclusive
resources, two required checks, two source+policy revision identities, two
attempt generations.

**What was explored.** Every reachable state.

```
1935362 states generated, 207360 distinct states found, 0 states left on queue.
The depth of the complete state graph search is 19.
Model checking completed. No error has been found.
```

**The five properties, all held in every reachable state.**

1. An exclusive resource is free or held at exactly one generation.
2. No accepted decision is appended on behalf of a superseded generation.
3. No decision is appended unless every required check has passing evidence
   under the exact attempt and revision identity it names.
4. No accepted decision names a source or policy revision other than the
   current one.
5. No crash step changes any ownership fact.

### What this does not establish

Not any other number of workers. Not any other number of resources, checks or
revisions. Not any Python implementation, or any implementation in any
language. A run over two workers is a statement about two workers.

No liveness claim is made. No fairness condition and no termination
assumption is modelled, so "an attempt eventually finishes" is not established
and is not claimed.

Three modelling choices were forced by the state count, and each is recorded in
the module header because a bound that cannot be seen cannot be checked. The
source and policy revisions are modelled as one combined identity, which is
what the core and CONTRACT.md treat them as. Evidence is indexed by identity
rather than stored as a set of records, because a set of records made every
subset a distinct state and the run went past a billion states and 1 GB of
queue before the fifth property was reachable. A failing run and an absent run
are one value, `notAPass`, because the acceptance condition draws no
distinction between them; anything downstream that does care belongs in the
Python tests, where the reasons are recorded.

### The mutations

`formal/run_mutants.py` removes one guard and requires a counterexample. A
mutant that still passes means the property is not checking that rule, and the
harness fails over it.

| Mutant | Guard removed | Caught by |
| --- | --- | --- |
| MUTANT_STALE_GENERATION | `g = genOf[o]` in Accept | Property2 |
| MUTANT_STALE_IDENTITY | `r = curRevision` in Accept | Property4 |
| MUTANT_MISSING_CHECK | the full-coverage requirement in Accept | Property3 |
| MUTANT_CRASH_RELEASES | `owner' = owner` and `owned' = owned` in Crash | Property5 |

All four caught, each by its intended property.

## Correspondence testing. Ran. PASS, and it is the weaker claim.

`formal/reference.py` is a second implementation of the ownership and
acceptance rules, written from the documentation, holding everything in
dictionaries with no database, no transaction and no process.
`tests/test_formal_correspondence.py` drives both it and the real core with
Hypothesis-generated operation sequences and compares them after every step.

**What this establishes.** Correspondence: no disagreement was found in the
sequences tried.

**What this does not establish.** Implementation equivalence. The two differ
mechanically everywhere, the reference is finite where the core is not, and
Hypothesis samples a bounded family of sequences rather than exhausting it. "No
disagreement was found" is a weaker sentence than "they agree", and only the
weaker one is supported.

It also establishes nothing about the TLA+ model. The TLA+ model and the
reference model were written from the same documented rules, so a
misunderstanding of those rules shared between them is invisible to every test
in the repository. The Python core is the only one of the three under test.

**Writing the reference honestly meant correcting it four times.** Each
correction is a fact about the core that the model had guessed wrong, and each
was found by the comparison rather than by reading.

- A superseded task can re-acquire. The reference refused a dead owner; the
  core has no liveness flag, so the generation is the only identity and a
  reassigned task legitimately takes its resources again.
- Generation is unbounded. The reference capped at 2 to match the TLA+
  configuration, which invented a divergence rather than observing one.
- A failing check is REJECTED, not BLOCKED. A recorded failure is an answer,
  not an absence, and it outranks a missing check.
- `release` refuses the whole call when the owner holds anything at an older
  generation. A superseded attempt cannot release even a claim it took at the
  new generation. Hypothesis found this one: acquire, crash, acquire a second
  resource, release it. The core keeps it held.

The last two are the core being right and the reference being wrong.

### The mutations

`formal/run_python_mutants.py` removes each guard from a copy of the core and
runs the named test against the copy.

| Mutant | Guard removed | Test that goes red |
| --- | --- | --- |
| MUTANT_STALE_GENERATION | the generation comparison in `record_readiness` | `test_a_superseded_attempt_cannot_publish_ready` |
| MUTANT_MISSING_CHECK | the last entry of the readiness loop | `test_one_missing_required_check_is_never_ready` |

Both caught. The counterexample for the first is a task that reads
`readiness is None` against a mutated core that leaves it READY.

## Deliverable B: theorem checking. Optional. See below.

The plan labels Deliverable B optional and requires it be labelled so. Lean
4.32.2 is present on this host. The status of the Lean deliverable is recorded
in `formal/lean/RESULTS.md`, which states plainly what compiled and what did
not. It does not block Deliverable A, and an absent Lean blocks nothing at all:
no ordinary verification path requires it.

## A finding about the core, not a test failure

`compute_readiness` reads every run attached to the task and filters by
neither the attempt generation nor the source and policy revision. A
generation 2 attempt therefore reaches READY on generation 1 evidence alone.
Measured directly against the current code:

```text
open_task(t1)                          -> generation 1, readiness None
run c1 at attempt 1, PASS              -> compute_readiness == READY
supersede_task(t1)                     -> generation 2
compute_readiness, no new evidence     -> READY
record_readiness                       -> task row reads READY at generation 2
```

The TLA+ model's Property 3 and Property 4 both forbid this, because its
`RecordCheck` binds evidence to the generation its owner currently has and its
`Accept` requires the current generation and the current revision.

This is a divergence between the model and the core, and it is reported rather
than tested around. There is no test in this repository asserting that the core
invalidates a task's readiness on a revision or generation change, because the
core does not do that. Whether it should is a question about the acceptance
contract, and CONTRACT.md says the contract is reviewed policy. The fix is a
change to the core's documented condition, not a change to a test.

Note that the stale-generation guard in `record_readiness` is intact and does
work. It refuses a verdict computed at an older generation. What is missing is
the earlier guard: a verdict computed at a current generation from evidence
that an older generation produced.

## What a summary of this work is allowed to say

The strongest accurate sentence is:

> The ownership and acceptance rules behaved correctly on every case the test
> suite runs, agreed with an independent reference model over 200 generated
> operation sequences, and satisfied five safety properties in every one of the
> 207,360 reachable states of a finite model of them at a recorded
> configuration of two owners, two resources, two checks, two revisions and
> two generations.

"Formally verified" is not available and is not claimed.

## Ordinary verification does not need any of this

Neither `formal/run_tlc.py` nor `formal/run_mutants.py` nor
`formal/run_python_mutants.py` downloads anything. They look for a JRE and a
`tla2tools.jar` the operator has already placed under `tmp/formal-tools/`, and
report BLOCKED when they are absent. `tmp/` is gitignored, so no toolchain is
committed. No hook and no ordinary MCP check call reaches this code, which is
what CONTRACT.md requires.

An absent toolchain is a BLOCKED result for its own category and no effect at
all on any other. `python -m pytest` does not import `formal/` and does not
need Lean, Java, or Hypothesis for anything except the correspondence tests,
which skip cleanly if Hypothesis is missing.

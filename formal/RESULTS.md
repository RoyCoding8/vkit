# Plan 08 results: what was checked, at which bounds, and what that does not cover

This file is the receipt for Plan 08. Read the scope sentences. A run that
produced no counterexample is a real result, but it is a result about a bounded
thing, and the bound is the interesting part.

Nothing here is quoted from a document, a plan, or a memory of a previous run.
Each section names the run it rests on, and where that run happened: artifact A
was measured on an earlier host, because this one has no JRE, and its receipt is
the evidence for it rather than a rerun.

**Every claim below is either VERIFIED against a receipt or BLOCKED with a
reason.** `python review/probe_formal_state.py` recomputes the digest each
receipt records and prints the state of all four artifacts; it is the check that
keeps this table honest. The table is per artifact, not per section, because
which toolchain a section needs is what decides whether its claim is a result or
an absence.

| Artifact | State | Receipt | Tool and version |
| --- | --- | --- | --- |
| A. TLC finite model check | VERIFIED | `formal/results/OwnershipAcceptance-receipt.json` | TLC 2.19 (08), jar sha256 `936a2620…` |
| B. Lean theorem check (optional) | VERIFIED | `formal/results/Acceptance-lean-receipt.json` | Lean 4.34.1, commit 5045d00 |
| C. Python core mutation run | VERIFIED | `formal/results/python-core-mutants-receipt.json` | pytest against the mutated copy |
| D. TLA+ mutation run | BLOCKED | none | needs a JRE and `tla2tools.jar` |

Artifact D is BLOCKED, not because the model is wrong and not because a counterexample
was missed, but because `formal/run_mutants.py` writes no receipt at all and
because this host has no JRE, so no result about it can be verified or repeated
here. What would unblock it: a portable JRE and `tla2tools.jar` under
`tmp/formal-tools/`, and a receipt written by that harness. The four TLA+ mutant
results that used to be stated in this file without one are therefore not
repeated as findings here. Section "The mutations" below says what it can.

| | |
| --- | --- |
| Host | Windows 11 10.0.26200, x86_64 |
| Python | 3.13.14 |
| Git | 2.54.0 |
| JRE | absent on this host, so TLC did not run here |
| TLC | 2.19, rev 5a47802, `tla2tools.jar` sha256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88` |
| Lean | 4.34.1, commit 5045d0056413266e57c625dcd7c365b10e377c52 |
| Hypothesis | 6.168.3 |

The Lean row is 4.34.1 because that is the toolchain the receipt records and the
one this file's Lean section was measured with. An earlier run of this plan
recorded 4.32.2 (commit f3b06c7); the difference is a toolchain upgrade between
two runs, and the earlier number is not a second result, only an older one.

**How the digests in these receipts are computed.** sha256 over the file's bytes
with CRLF folded to LF, which is the form git stores and the form a POSIX
checkout reads. `.gitattributes` pins `eol=lf` for `*.py`, `*.json`, `*.md` and
friends but not for `*.tla` or `*.lean`, so on a `core.autocrlf=true` checkout
those two files arrive with CRLF. Hashing raw bytes there made a receipt
written on this host unverifiable on any other, because the committed bytes and
the working bytes differed without a single character of the artifact having
changed. The bound the normalization leaves is stated in every receipt as
`digest_normalization`: a line-ending-only change does not alter the digest. The
jar is digested over exact bytes instead, because a binary never passes through
git's filter and folding a byte pair out of a compressed stream would be hashing
something other than the file.

## Deliverable A: finite model checking. Ran on an earlier host. VERIFIED.

`formal/tla/OwnershipAcceptance.tla`, run by `formal/run_tlc.py`.
`formal/results/OwnershipAcceptance-receipt.json`, recorded 2026-09-30,
reports `TLC 2.19 (08)` and both digests reproduce against the committed model
and config. It did not rerun on this host, which has no JRE; the run it records
stands on its receipt, not on a rerun.

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

**BLOCKED. No receipt exists for the TLA+ mutation run.**

`formal/run_mutants.py` removes one guard from the model and requires TLC to
report a counterexample. That is the right shape for the check, and the harness
does fail over a mutant that still passes. It writes no receipt, so nothing it
prints can be checked by anyone else, and it cannot run on this host, which has
no JRE and no `tla2tools.jar`.

So the four guards below are named as what the harness is written to break, and
nothing is claimed about whether it caught them. The earlier revision of this
file stated that all four were caught, each by its intended property. That
sentence had no receipt behind it, and it is withdrawn rather than repeated.

| Mutant | Guard the harness targets | Property it would have to break |
| --- | --- | --- |
| MUTANT_STALE_GENERATION | `g = genOf[o]` in Accept | Property2 |
| MUTANT_STALE_IDENTITY | `r = curRevision` in Accept | Property4 |
| MUTANT_MISSING_CHECK | the full-coverage requirement in Accept | Property3 |
| MUTANT_CRASH_RELEASES | `owner' = owner` and `owned' = owned` in Crash | Property5 |

To close this: put a portable JRE and `tla2tools.jar` under
`tmp/formal-tools/`, run `python formal/run_mutants.py`, and have it write a
receipt naming the model digest it mutated. Until then artifact D in the table
above is BLOCKED, and a reader should not treat Properties 1 through 5 as
mutation-tested.

## Correspondence testing. Ran. VERIFIED, and it is the weaker claim.

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

`formal/run_python_mutants.py` removes one guard from a copy of `src/vkit/tasks.py`
and runs the test that is supposed to cover it against the copy. Both were
caught, and the receipt is
`formal/results/python-core-mutants-receipt.json`.

| Mutant | Guard removed | Test that went red | Counterexample |
| --- | --- | --- | --- |
| MUTANT_STALE_GENERATION | the generation comparison in `record_readiness` | `test_a_superseded_attempt_cannot_publish_ready` | `DID NOT RAISE ConflictError` |
| MUTANT_MISSING_CHECK | the gap a required check with no completed run raises | `test_one_missing_required_check_is_never_ready` | `assert 'READY' == 'BLOCKED'` |

**Both of these results were previously false, and the harness was reporting the
falsehood as a pass.** The recorded verdict was "2 of 2 caught". Neither test
had run. Each mutant named `test_formal_correspondence.py::…`, a node id that
does not resolve, and pytest answers an unresolvable node id with exit 4. The
harness read any nonzero exit as a catch, so a test that never executed was
recorded as a test that observed the mutant.

Two further faults sat behind that one. The temporary tree got a copy of `src`
and `tests` but not of `formal`, and the covering test imports `reference`,
which lives in `formal`, so even with a correct node id the test could not have
been collected. And the MUTANT_MISSING_CHECK mutation was wrong on its own
terms: it rewrote the readiness loop to iterate `required[:-1]`, and `required`
is sorted, so it drops whichever required check sorts first. With the required
set `("unit", "lint")` and the pass recorded for `unit`, the mutation dropped
`unit` and left the absent `lint` unexamined, which is a pass. Measured
directly: that mutation exits 0 against the core. The mutation now removes the
line that raises the gap for an absent check, which is the guard the rule
actually lives on, and it is caught.

The exit codes are now read individually, so only pytest's 1 counts as a catch
and 2, 4 and 5 are BLOCKED with the reason printed. A missing node id, a
collection error and a real assertion failure used to be the same signal.

## Deliverable B: theorem checking. OPTIONAL. Ran. VERIFIED.

The plan labels Deliverable B optional and requires that it be labelled so.
Lean 4.34.1 is present on this host, so the deliverable was built rather than
skipped. It blocks nothing: an absent Lean is a BLOCKED result for this category
and no effect at all on any other, and no ordinary verification path requires
it.

`formal/lean/Acceptance.lean`, checked by `formal/run_lean.py`.
`formal/results/Acceptance-lean-receipt.json` records the run; its module digest
reproduces against the committed file.

**The theorems.** `accept_correct : accept e o ctx = true ↔ AcceptsSpec e o ctx`,
in both directions, plus `allSatisfied_correct` and `satisfies_correct`. The
specification `AcceptsSpec` is stated by universal quantification over the
required checks and separately requires the set to be non-empty.

**Axiom audit, verbatim.**

```text
'Acceptance.satisfies_correct' depends on axioms: [propext, Quot.sound]
'Acceptance.allSatisfied_correct' depends on axioms: [propext, Quot.sound]
'Acceptance.accept_correct' depends on axioms: [propext, Quot.sound]
```

`Classical.choice` is not used, which is strictly better than the allowed set of
`propext`, `Classical.choice` and `Quot.sound`. No `sorry`, no `sorryAx`, no
custom axiom, no `opaque`, no `native_decide`.

**The nine `#eval` cases, verbatim, in file order.**

```text
true false false false false true true true false
```

| Case | Result |
| --- | --- |
| both required checks pass under the current identity | true |
| one required check has no record at all | false |
| one required check passed at a stale generation | false |
| one required check passed under a superseded revision | false |
| the required set is empty | false |
| a required check has a pass and a fail under one identity | true |
| the same evidence record is listed twice | true |
| a required check is named twice in the required set | true |
| a required check has a recorded fail and no pass | false |

**The `sorry` check was verified by injection, not assumed.** Replacing the
proof of `accept_correct` with `sorry` makes the harness fail twice over: the
source scan names it, and the axiom audit reports `sorryAx`. Restoring the
proof returns it to PASS.

### What this does not establish

Nothing about the Python core. The theorems are about a Lean model written
from the same documented rules, and a misunderstanding shared between that
model and the specification would be invisible to them, exactly as it is
invisible to the reference model in the correspondence tests. The `#eval` cases
use the same frozen records the TLA+ model and `formal/reference.py` use, and
agreeing on nine cases is a countercheck of nine decisions, not a proof that
the two agree everywhere.

The duplicate-evidence policy is stated explicitly in the module, and it is
existential: one matching pass satisfies a required check and a conflicting
`fail` under the same identity does not veto it. That policy has no recency
rule, and the Python core does have one: `compute_readiness` takes the most
recent terminal run per check. On a check re-run under one identity the two
answer different questions, and that divergence is recorded in the module
rather than left for the counterchecks to paper over.

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
> operation sequences, satisfied five safety properties in every one of the
> 207,360 reachable states of a finite model of them at a recorded
> configuration of two owners, two resources, two checks, two revisions and
> two generations, and were shown equivalent to a universally quantified
> specification in a separate Lean model whose theorems depend on no axiom
> outside Lean's standard three.

"Formally verified" is not available and is not claimed. Each sentence above
names its category: scenario, property, finite model checking, and theorem
checking, in the four categories `src/vkit/claimkind.py` defines. They do not
carry the same weight, and a generic PASS over all four is the thing this plan
exists to prevent.

## Ordinary verification does not need any of this

None of `formal/run_tlc.py`, `formal/run_mutants.py`, `formal/run_lean.py` or
`formal/run_python_mutants.py` downloads anything. They look for a JRE and a
`tla2tools.jar` the operator has already placed under `tmp/formal-tools/`, or
for a Lean already on the host, and report BLOCKED when they are absent.
`tmp/` is gitignored, so no toolchain is committed. No hook and no ordinary MCP
check call reaches this code, which is what CONTRACT.md requires.

An absent toolchain is a BLOCKED result for its own category and no effect at
all on any other. `python -m pytest` does not import `formal/`, and the
correspondence tests skip cleanly without Hypothesis, which
`tests/test_optional_toolchain.py` checks by actually hiding the package and
asserting the skip rather than trusting it.

`review/probe_formal_state.py` is the fifth script, and the one that reads the
other four's output. It recomputes the digest in every receipt, prints each
artifact as VERIFIED or BLOCKED, and exits 1 if a receipt claims a result its own
digest cannot support. It needs no toolchain at all, so it runs on a host with
neither a JRE nor Lean, which is exactly the host where the receipts go stale
unnoticed.

## A pre-existing flaky test, unrelated to this plan

`tests/test_recover.py::test_an_absent_pid_is_still_dead_with_a_recorded_identity`
and its sibling `test_liveness_distinguishes_a_real_process_from_an_absent_one`
fail intermittently in a full-suite run, and pass when the file is run alone.

The cause is in the `dead_pid` fixture. It spawns a process, waits for it to
be reaped, waits for its pid to stop answering a liveness probe, and returns
that pid. Between the fixture returning and the test asserting DEAD, Windows
can hand the same pid to a process another test started, and the assertion
then reads LIVE. Both tests passed five runs in isolation and failed on
different runs of the full suite, once each, on a tree with none of this plan's
files in it. That is a timing race in the fixture, not a defect this plan
introduced and not something this plan is scoped to fix.

It is recorded here because a reader comparing a full-suite result against this
file will otherwise see a number that varies and no explanation for it. The
formal work is unaffected: none of the four harnesses spawns a process or reads
a pid.

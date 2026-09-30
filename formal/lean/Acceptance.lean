/-
An OPTIONAL Lean policy example. Read this header before treating it as
evidence about anything.

## What this file is

A finite, self-contained model of vkit's acceptance decision, with a proved
equivalence between an executable decision function and a logical
specification stated by universal quantification. It is the plan's optional
Deliverable B, which the plan itself labels optional.

## What this file is not

It is NOT a statement about the Python core. It models the same rules in
Lean's own type system and proves things about THAT model. `accept_correct` is
proved for `accept`. Nothing here has been connected to the Python
implementation, and the frozen examples shared between the two are
counterchecks of a handful of decisions, which is a check that they agree on
those cases, not a proof that they agree everywhere. The plan says this in as
many words, and repeating it here is the point.

No `sorry`, no `sorryAx`, no custom axioms. `#print axioms` is run over all
three theorems and its output is recorded. A build that reports an axiom other
than Lean's three standard quotient and propositional-extensionality axioms is a
failure, not a pass.

No liveness, no termination. The theorems are about a pure function over a
finite structure.
-/
import Std

namespace Acceptance

/-- A required check ID, a source revision, a policy revision. -/
abbrev CheckId := String
abbrev Revision := String
abbrev Owner := String

/-- The outcome of one recorded run. `absent` is not a failure, it is the
    absence of a run, and the decision treats it differently from `fail`. -/
inductive Outcome where
  | pass
  | fail
  | blocked
  | absent
  deriving DecidableEq, Repr

/-- One recorded run, bound to the identity it was produced under. -/
structure Evidence where
  checkId : CheckId
  owner : Owner
  generation : Nat
  revision : Revision
  outcome : Outcome
  deriving DecidableEq, Repr

/-- A set of required checks, a current generation, and a current revision.
    The revision is the combined source+policy identity, which is what the
    Python core treats as one thing and what the TLA+ model does too. -/
structure Context where
  required : List CheckId
  generation : Nat
  revision : Revision
  deriving Repr

/--
The specification, stated independently of any function.

A task is acceptable exactly when the required set is not empty, and for EVERY
required check there EXISTS evidence that passes, belongs to this owner, sits
at this generation, and was produced under this revision.

The two guards are not decoration. `required ≠ []` is the plan's nonempty
requirement, and the universal quantifier over `c ∈ required` is what makes a
missing check a failure rather than a vacuous success. Writing it existentially
over the evidence list instead would accept on any single passing run.
-/
def AcceptsSpec (e : List Evidence) (o : Owner) (ctx : Context) : Prop :=
  ctx.required ≠ [] ∧
    ∀ c ∈ ctx.required,
      ∃ ev ∈ e, ev.checkId = c ∧ ev.owner = o ∧
        ev.generation = ctx.generation ∧ ev.revision = ctx.revision ∧
        ev.outcome = .pass

/-- Does one required check have passing evidence under the current identities? -/
def satisfies (e : List Evidence) (o : Owner) (c : CheckId) (ctx : Context) : Bool :=
  e.any fun ev =>
    ev.checkId = c && ev.owner = o &&
    ev.generation == ctx.generation && ev.revision = ctx.revision &&
    ev.outcome == .pass

/-- Does the evidence satisfy every check in this list? Empty is vacuously
    true, which is why the caller separately requires the required set to be
    non-empty. -/
def allSatisfied (e : List Evidence) (o : Owner) (ctx : Context) : List CheckId → Bool
  | [] => true
  | c :: cs => satisfies e o c ctx && allSatisfied e o ctx cs

/-- The executable decision. Same condition, evaluated by recursion.
    The required set must be non-empty AND every required check must be
    satisfied. -/
def accept (e : List Evidence) (o : Owner) (ctx : Context) : Bool :=
  decide (ctx.required ≠ []) && allSatisfied e o ctx ctx.required

/-!
## Duplicate and conflicting evidence policy

The plan requires this to be stated rather than left to a reader, so it is
stated here and then decided by the frozen examples at the bottom of the file.

`AcceptsSpec` quantifies EXISTENTIALLY over the evidence list. For each required
check it asks whether SOME record in `e` matches the current identity and
passes. It never asks whether EVERY matching record passes. Three consequences
follow, and all three are deliberate:

1. **A conflicting duplicate does not veto.** If `e` holds both a `pass` and a
   `fail` for the same check under the same owner, generation, and revision,
   that check is satisfied. One passing record is enough. The alternative —
   let any failing record for a required check block acceptance — would make
   the decision sensitive to how many times a check was re-run, and a re-run
   is not by itself new information about the current identity.

2. **Off-identity records are ignored entirely.** A record with a stale
   generation, a different revision, or a different owner neither satisfies a
   required check nor blocks one. It is not evidence about the identity being
   decided, so letting it vote either way would be a category error.

3. **Duplicates are inert.** Listing the same record twice changes nothing, and
   naming a check twice in `ctx.required` demands nothing extra: `required` is a
   `List`, so a repeated entry is a repeated conjunct over the same check, not a
   stronger demand.

What this policy does NOT do is order anything. There is no recency, no "latest
run wins", and no tie-breaking between conflicting records. `e` is an unordered
bag here. The Python core does take the most recent terminal run per check and
ignores the rest, so on a check that was re-run under one identity the core and
this model answer different questions. That divergence is real, it is not
resolved by this file, and it is one of the things a reader must not mistake the
frozen counterchecks below for having settled.
-/

/-- One required check has passing evidence under the current identities. -/
theorem satisfies_correct (e : List Evidence) (o : Owner) (c : CheckId) (ctx : Context) :
    satisfies e o c ctx = true ↔
      ∃ ev ∈ e, ev.checkId = c ∧ ev.owner = o ∧
        ev.generation = ctx.generation ∧ ev.revision = ctx.revision ∧
        ev.outcome = .pass := by
  simp [satisfies, and_assoc]

/-- Every required check has passing evidence under the current identities. -/
theorem allSatisfied_correct (e : List Evidence) (o : Owner) (ctx : Context) :
    allSatisfied e o ctx ctx.required = true ↔
      ∀ c ∈ ctx.required, ∃ ev ∈ e,
        ev.checkId = c ∧ ev.owner = o ∧
          ev.generation = ctx.generation ∧ ev.revision = ctx.revision ∧
          ev.outcome = .pass := by
  generalize ctx.required = req
  induction req with
  | nil => simp [allSatisfied]
  | cons c cs ih =>
    simp only [allSatisfied, Bool.and_eq_true]
    constructor
    · intro h c' hc'
      rcases List.mem_cons.mp hc' with hEq | hc'
      -- `hEq : c' = c`. Rewrite the GOAL with it; do not `subst`. The goal
      -- still names `c` in `h.1`'s index, and `subst` would eliminate `c`
      -- from scope and take the specification's index with it.
      · rw [hEq]
        exact (satisfies_correct e o c ctx).mp h.1
      · exact (ih.mp h.2) c' hc'
    · intro h
      refine ⟨(satisfies_correct e o c ctx).mpr (h c List.mem_cons_self), ?_⟩
      exact ih.mpr (fun c' hc' => h c' (List.mem_cons_of_mem c hc'))

/-- The executable decision and the specification agree, in both directions. -/
theorem accept_correct (e : List Evidence) (o : Owner) (ctx : Context) :
    accept e o ctx = true ↔ AcceptsSpec e o ctx := by
  simp only [accept, AcceptsSpec, Bool.and_eq_true, decide_eq_true_eq, ne_eq,
    allSatisfied_correct]

/-
Axiom audit. The only acceptable output is a line naming some subset of Lean's
three standard axioms — `propext`, `Classical.choice`, `Quot.sound` — or
`'... does not depend on any axioms', which is strictly better. Anything else
means an unproved goal or an unapproved axiom reached the theorem, and the
build has failed even though it type-checked.

Observed on Lean 4.32.2 with `import Std` and no other dependency:

    'Acceptance.satisfies_correct' depends on axioms: [propext, Quot.sound]
    'Acceptance.allSatisfied_correct' depends on axioms: [propext, Quot.sound]
    'Acceptance.accept_correct' depends on axioms: [propext, Quot.sound]
-/
#print axioms satisfies_correct
#print axioms allSatisfied_correct
#print axioms accept_correct

/-!
## Frozen examples

The plan asks for the same example records to be driven through the Python
acceptance evaluator and through this executable, as a countercheck of a
handful of decisions. These are that set, on the finite domain the TLA+ model
and `formal/reference.py` use: owner `w1`, required checks `unit` and `lint`,
generation 1, revision `rev1`.

Each `#eval` below is one decision on one record set. Agreeing on these is a
check that two implementations read the same handful of records the same way.
It is not a proof that they agree on records nobody wrote down.
-/

/-- The frozen owner. Every record below belongs to it. -/
def owner1 : Owner := "w1"

/-- One frozen record, bound to the identity it was produced under. -/
def ev (id : CheckId) (gen : Nat) (rev : Revision) (out : Outcome) : Evidence :=
  { checkId := id, owner := owner1, generation := gen, revision := rev, outcome := out }

/-- The frozen required set (`unit`, `lint`) and the frozen current identity
    (generation 1, revision `rev1`). -/
def current : Context :=
  { required := ["unit", "lint"], generation := 1, revision := "rev1" }

/-- The frozen decision: `accept` for the frozen owner. -/
def frozen (e : List Evidence) (ctx : Context) : Bool := accept e owner1 ctx

/- Accepts. Both required checks pass under the current owner, generation, and
   revision. The only true decision below that turns on ordinary evidence. -/
#eval frozen [ev "unit" 1 "rev1" .pass, ev "lint" 1 "rev1" .pass] current

/- Refuses. `lint` has no record at all. Absence of a run is not a pass. -/
#eval frozen [ev "unit" 1 "rev1" .pass] current

/- Refuses. `lint` passed, but at generation 0 while the current generation
   is 1. Stale evidence satisfies nothing. -/
#eval frozen [ev "unit" 1 "rev1" .pass, ev "lint" 0 "rev1" .pass] current

/- Refuses. `lint` passed, but under revision `rev0` while the current revision
   is `rev1`. -/
#eval frozen [ev "unit" 1 "rev1" .pass, ev "lint" 1 "rev0" .pass] current

/- Refuses. Nothing is required. Every record vacuously satisfies an empty
   required set, so without the nonempty guard this would accept a task no
   check ever ran. -/
#eval frozen [] { current with required := [] }

/- Accepts. `unit` appears twice under ONE identity with conflicting outcomes.
   This is the duplicate/conflicting policy above, decided: the passing record
   is enough and the `fail` does not veto. -/
#eval frozen [ev "unit" 1 "rev1" .pass, ev "unit" 1 "rev1" .fail, ev "lint" 1 "rev1" .pass] current

/- Accepts, identically. The same record listed twice changes nothing;
   duplicates in the evidence list are inert. -/
#eval frozen [ev "unit" 1 "rev1" .pass, ev "unit" 1 "rev1" .pass, ev "lint" 1 "rev1" .pass] current

/- Accepts, identically. `required` is a list, so naming `lint` twice is
   checking it twice and demands nothing extra. -/
#eval frozen [ev "unit" 1 "rev1" .pass, ev "lint" 1 "rev1" .pass] { current with required := ["unit", "lint", "lint"] }

/- Refuses. `lint` has a recorded `fail` and no recorded `pass`. The presence of
   an answer does not repair the absence of a pass. -/
#eval frozen [ev "unit" 1 "rev1" .pass, ev "lint" 1 "rev1" .fail] current

end Acceptance

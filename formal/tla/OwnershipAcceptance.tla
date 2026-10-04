-------------------------- MODULE OwnershipAcceptance --------------------------
(***************************************************************************)
(* A finite model of vkit's ownership and acceptance decisions.             *)
(*                                                                         *)
(* WHAT A SUCCESSFUL RUN ESTABLISHES, AND WHAT IT DOES NOT.               *)
(*                                                                         *)
(* TLC exploring this module to completion shows that the five safety       *)
(* properties below hold in EVERY state reachable in THIS model at THIS    *)
(* configuration. It is NOT a claim about three resources, a hundred        *)
(* workers, a third revision, or any Python program. The Python core is     *)
(* not the subject of this model. The correspondence tests under tests/     *)
(* carry the (much weaker) claim actually made about it, and they are      *)
(* described as correspondence, never as equivalence.                     *)
(*                                                                         *)
(* ONLY SAFETY IS CLAIMED. No fairness and no termination assumption is    *)
(* modelled, so no liveness property is stated or checked.                 *)
(*                                                                         *)
(* STATE SHAPE, AND WHY IT IS SHAPED THIS WAY.                            *)
(*                                                                         *)
(* Three choices here were forced by the state count, and each is recorded *)
(* rather than hidden, because a bound that cannot be seen cannot be       *)
(* checked.                                                                 *)
(*                                                                         *)
(* 1. `Revisions` is a set of two COMBINED source-and-policy identities,    *)
(*    not two source axes times two policy axes. CONTRACT.md treats the    *)
(*    source and policy revisions of one contract as one identity that     *)
(*    readiness is decided under, and the acceptance condition never       *)
(*    separates them. A run either matches the pair the task was opened     *)
(*    with or it does not. This is the plan's "two source/policy revision *)
(*    identities" read as a pair, which is the reading the core supports.  *)
(*                                                                         *)
(* 2. `evidence` is a FUNCTION from identity to a two-valued result, not a *)
(*    set of records. A set of records makes every subset a distinct       *)
(*    state; the first draft did that and reached 198,072 states with a    *)
(*    54,269-state queue before the fifth property was even reachable.     *)
(*    Indexing by identity, and recording "did not pass" as one value,     *)
(*    keeps the acceptance condition bit for bit identical while making    *)
(*    the state space countable. The distinction between a FAIL and an     *)
(*    absent result is NOT modelled, because the acceptance condition      *)
(*    draws no distinction between them: both are "not a pass". Anything   *)
(*    downstream of acceptance that cares about that difference belongs in *)
(*    the Python tests, where the reasons are actually recorded.          *)
(*                                                                         *)
(* 3. `accepted` records the MOST RECENT accepted decision, not the whole  *)
(*    history. Properties 2, 3 and 4 are all about the moment of           *)
(*    acceptance, and the witnesses fire there. Keeping an append-only     *)
(*    set of every decision on record multiplied the state count by up to  *)
(*    2^16 for no property that is stated here.                            *)
(*                                                                         *)
(* HOW THE PROPERTIES THAT ARE ABOUT EVENTS ARE ENCODED.                  *)
(*                                                                         *)
(* Properties 2, 3 and 4 say an execution cannot HAPPEN. A predicate over  *)
(* one state cannot say that, and the obvious restatements are not merely  *)
(* clumsy, they are WRONG. Two were tried and TLC rejected both:           *)
(*                                                                         *)
(*   "Every decision names its owner's current generation" is false on a   *)
(*   correct model, because a decision legitimately made at generation 1   *)
(*   is still on record after that owner reconciles to generation 2.       *)
(*                                                                         *)
(*   "Every recorded decision is still backed by evidence" is false        *)
(*   because recording a second result under the same identity replaces    *)
(*   the first. TLC produced a 5-state counterexample against a model      *)
(*   whose Accept action was guarded correctly.                            *)
(*                                                                         *)
(* So those three are checked through a `violations` fault-detection set.  *)
(* Each action appends a tag when it performs the thing the property      *)
(* forbids, and the property reads the tag's absence.                      *)
(*                                                                         *)
(* THE TAG IS WRITTEN IN ITS OWN CONJUNCT, NEVER INSIDE THE GUARD IT      *)
(* WATCHES. That is what makes the mutation meaningful. Removing the      *)
(* guard from Accept does not remove the tag, so the tag still fires and   *)
(* the property still fails. A witness welded to the guard would be        *)
(* removed along with it and the mutant would pass, which is exactly the   *)
(* failure mode this encoding exists to avoid. formal/mutants/ tests that  *)
(* rather than asserting it.                                                *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Owners,        \* finite set of owner identities, e.g. {"w1", "w2"}
    Resources,     \* finite set of exclusive resource keys
    Checks,        \* finite set of required check IDs
    Revisions,     \* finite set of COMBINED source+policy identities
    Generations    \* the set of attempt generations, e.g. {1, 2}

NoOwner     == "none"
Zero        == 0
NoDecision  == [verdict |-> "NONE", owner |-> NoOwner, generation |-> Zero,
                 revision |-> "NONE"]
NotAPass    == "notAPass"

FirstGeneration == CHOOSE g \in Generations : \A h \in Generations : g <= h

(***************************************************************************)
(* Violation tags. Each names one property, so a failing run says WHICH     *)
(* rule broke rather than only that something did.                         *)
(***************************************************************************)
StaleGenerationAccepted == "staleGenerationAccepted"
StaleIdentityAccepted   == "staleIdentityAccepted"
MissingCheckAccepted    == "missingCheckAccepted"
OwnershipMovedOnCrash   == "ownershipMovedOnCrash"

AllViolations ==
    {StaleGenerationAccepted, StaleIdentityAccepted, MissingCheckAccepted,
     OwnershipMovedOnCrash}

(***************************************************************************)
(* Variants. The state is a record of these rather than loose variables,   *)
(* so a half-updated ownership or acceptance is not representable.         *)
(***************************************************************************)

\* A resource is free, or held by one owner at one generation. The "at most
\* one" is not assumed: `owner` is a function, so a state naming two owners
\* for one resource cannot be built, and Property1 states the observable
\* consequence instead of restating the type.
ResourceState == [owner : Owners \cup {NoOwner}, generation : Generations \cup {Zero}]

\* Known run evidence, indexed by the identity it was produced under.
Evidence == [Checks -> [Owners -> [Generations -> [Revisions -> {"PASS", NotAPass}]]]]

Decision == [verdict    : {"READY", "NONE"},
             owner      : Owners \cup {NoOwner},
             generation : Generations \cup {Zero},
             revision   : Revisions \cup {"NONE"}]

VARIABLES
    genOf,          \* owner -> its current attempt generation
    alive,          \* owner -> TRUE while its process is running
    owner,          \* resource -> owner or NoOwner
    owned,          \* resource -> the generation holding it, or Zero
    evidence,       \* identity -> "PASS" or NotAPass
    curRevision,    \* the source+policy identity the project is at
    accepted,       \* the most recent accepted decision
    violations      \* SUBSET AllViolations, the fault detector

vars == << genOf, alive, owner, owned, evidence, curRevision, accepted, violations >>

TypeOK ==
    /\ genOf  \in [Owners -> Generations]
    /\ alive  \in [Owners -> BOOLEAN]
    /\ owner  \in [Resources -> (Owners \cup {NoOwner})]
    /\ owned  \in [Resources -> (Generations \cup {Zero})]
    /\ evidence   \in Evidence
    /\ curRevision \in Revisions
    /\ accepted   \in Decision
    /\ violations \subseteq AllViolations

(***************************************************************************)
(* The implementation's documented acceptance condition, transcribed from    *)
(* src/vkit/tasks.py::compute_readiness. This is the SPECIFICATION, the thing *)
(* the action is supposed to satisfy.                                       *)
(*                                                                         *)
(* `CoversEveryCheck` is the specification. `Accept` does NOT call it, and  *)
(* that is deliberate. An earlier draft guarded Accept with this operator    *)
(* and put the property's witness in the same conjunct, which was wrong in a *)
(* way the mutation harness found: weakening the guard widened acceptance,   *)
(* the specification then agreed with the weakened guard, and the witness     *)
(* stayed silent. A guard and the property that checks it must be           *)
(* independently stated, or they can be wrong together.                     *)
(*                                                                         *)
(* Every required check needs a passing result from this owner, at this      *)
(* generation, under this source and policy identity.                       *)
(***************************************************************************)
CoversEveryCheck(o, g, r) ==
    \A c \in Checks : evidence[c][o][g][r] = "PASS"

Acceptable(o, g, r) ==
    /\ Checks # {}
    /\ CoversEveryCheck(o, g, r)

(***************************************************************************)
(* Init.                                                                   *)
(***************************************************************************)
Init ==
    /\ genOf = [o \in Owners |-> FirstGeneration]
    /\ alive = [o \in Owners |-> TRUE]
    /\ owner = [r \in Resources |-> NoOwner]
    /\ owned = [r \in Resources |-> Zero]
    /\ evidence = [c \in Checks |-> [o \in Owners |->
                     [g \in Generations |-> [r \in Revisions |-> NotAPass]]]]
    /\ curRevision \in Revisions
    /\ accepted = NoDecision
    /\ violations = {}

(***************************************************************************)
(* Actions.                                                                *)
(***************************************************************************)

\* Acquire a free resource for the current attempt.
\*
\* The two conjuncts that are not "make it mine" are the guards. `alive[o]`
\* says only a running process contends. `owner[r] = NoOwner` is mutual
\* exclusion. The resource is taken at `genOf[o]`, the owner's CURRENT
\* generation, which is the same identity the real claim row stores.
\*
\* Acquiring can never transfer ownership. The guard requires the resource to
\* be free beforehand, and it answers to this owner afterwards. There is no
\* transition here that a state predicate could catch, so there is no tag.
Acquire(o, r) ==
    /\ o \in Owners
    /\ r \in Resources
    /\ alive[o]
    /\ owner[r] = NoOwner
    /\ owner' = [owner EXCEPT ![r] = o]
    /\ owned' = [owned EXCEPT ![r] = genOf[o]]
    /\ UNCHANGED << genOf, alive, evidence, curRevision, accepted, violations >>

\* Record a check result for an attempt and source/policy identity. Only a
\* live attempt records, and it records under the generation it currently
\* holds, so evidence never names a generation its owner has already moved
\* past. That is what makes a stale accept reachable in a mutant and
\* unreachable here.
RecordCheck(o, c, r, outcome) ==
    /\ o \in Owners
    /\ c \in Checks
    /\ r \in Revisions
    /\ outcome \in {"PASS", NotAPass}
    /\ alive[o]
    /\ evidence' = [evidence EXCEPT ![c][o][genOf[o]][r] = outcome]
    /\ UNCHANGED << genOf, alive, owner, owned, curRevision, accepted, violations >>

\* Release an owned resource after confirmed cleanup. `owned[r] = genOf[o]`
\* is the generation guard: a superseded attempt may not free what its
\* successor holds, matching claims.release refusing a superseded generation.
\* The resource becomes free, never another owner's, so this cannot transfer.
Release(o, r) ==
    /\ o \in Owners
    /\ r \in Resources
    /\ owner[r] = o
    /\ owned[r] = genOf[o]
    /\ owner' = [owner EXCEPT ![r] = NoOwner]
    /\ owned' = [owned EXCEPT ![r] = Zero]
    /\ UNCHANGED << genOf, alive, evidence, curRevision, accepted, violations >>

\* Crash an owner without silently releasing its resource.
\*
\* `owner'` and `owned'` are stated as equal to themselves rather than listed
\* in an UNCHANGED, and the reason is that the UNCHANGED form cannot be
\* compared against anything. Property 1, an iff between the owner and the
\* owning generation, is satisfied by ANY well-typed transfer: hand a held
\* resource to a different live owner and the resource still has an owner and
\* a nonzero generation, so Property 1 does not notice. An earlier draft of
\* this action used UNCHANGED and left Property 5 with nothing to check; the
\* mutation harness is what found that, by producing a well-typed transfer and
\* watching every invariant pass.
\*
\* Stating the two variables as definitions makes the comparison possible, and
\* the comparison IS SafetyProperty5: no crash step may change any ownership
\* fact. A correct model never fires the tag. A model whose crash hands the
\* resource to a competing live attempt fires it on the first reachable trace,
\* which is what MUTANT_CRASH_RELEASES demonstrates.
Crash(o) ==
    /\ o \in Owners
    /\ alive[o]
    /\ alive' = [alive EXCEPT ![o] = FALSE]
    /\ owner' = owner
    /\ owned' = owned
    /\ violations' =
         IF owner' # owner \/ owned' # owned
         THEN violations \cup {OwnershipMovedOnCrash}
         ELSE violations
    /\ UNCHANGED << genOf, evidence, curRevision, accepted >>

\* Reconcile a stopped owner and advance its attempt generation.
\*
\* This is the only action that advances a generation, and it advances the
\* generation WITHOUT freeing the resource. The crashed attempt's claim stays
\* held until a later explicit Release, which is what a real recovery does
\* after presenting evidence, and it is why a superseded process can be
\* refused while its own process may still be writing.
Reconcile(o) ==
    /\ o \in Owners
    /\ ~ alive[o]
    /\ genOf[o] + 1 \in Generations
    /\ genOf' = [genOf EXCEPT ![o] = genOf[o] + 1]
    /\ UNCHANGED << alive, owner, owned, evidence, curRevision, accepted, violations >>

\* Change the candidate or approved source/policy identity. Nothing is
\* deleted. The point is that nothing recorded under the old identity can
\* satisfy the new one, because a result is recorded per identity and the
\* new identity starts with no results at all.
ChangeRevision(r) ==
    /\ r \in Revisions
    /\ r # curRevision
    /\ curRevision' = r
    /\ UNCHANGED << genOf, alive, owner, owned, evidence, accepted, violations >>

(***************************************************************************)
(* Submit an acceptance decision.                                           *)
(*                                                                         *)
(* Three guards, and they are three different guards.                      *)
(*                                                                         *)
(* `g = genOf[o]` is the STALE-GENERATION GUARD. It is the model's version *)
(* of tasks.record_readiness comparing the generation that computed the     *)
(* verdict against the current one and refusing a mismatch. Removing it is *)
(* mutation MUTANT_STALE_GENERATION.                                       *)
(*                                                                         *)
(* `r = curRevision` is the IDENTITY GUARD. It is the model's version of a *)
(* task pinned to the contract and policy it was opened with. Removing it  *)
(* is mutation MUTANT_STALE_IDENTITY.                                      *)
(*                                                                         *)
(* `Acceptable` is the CHECK-COVERAGE GUARD, the transcribed form of the   *)
(* implementation's documented condition. Dropping one check from it is     *)
(* mutation MUTANT_MISSING_CHECK.                                          *)
(*                                                                         *)
(* The three tags sit in their own conjuncts, below the guards and outside  *)
(* them, so deleting a guard does not delete the witness.                  *)
(***************************************************************************)
Accept(o, g, r) ==
    /\ o \in Owners
    /\ g \in Generations
    /\ r \in Revisions
    /\ g = genOf[o]
    /\ r = curRevision
    /\ \A c \in Checks : evidence[c][o][g][r] = "PASS"
    /\ accepted' = [verdict |-> "READY", owner |-> o, generation |-> g, revision |-> r]
    /\ violations' =
         violations
           \cup (IF g # genOf[o] THEN {StaleGenerationAccepted} ELSE {})
           \cup (IF r # curRevision THEN {StaleIdentityAccepted} ELSE {})
           \cup (IF ~CoversEveryCheck(o, g, r) THEN {MissingCheckAccepted} ELSE {})
    /\ UNCHANGED << genOf, alive, owner, owned, evidence, curRevision >>

Next ==
    \/ \E o \in Owners, r \in Resources : Acquire(o, r)
    \/ \E o \in Owners, c \in Checks, r \in Revisions, outcome \in {"PASS", NotAPass} :
          RecordCheck(o, c, r, outcome)
    \/ \E o \in Owners, r \in Resources : Release(o, r)
    \/ \E o \in Owners : Crash(o)
    \/ \E o \in Owners : Reconcile(o)
    \/ \E r \in Revisions : ChangeRevision(r)
    \/ \E o \in Owners, g \in Generations, r \in Revisions : Accept(o, g, r)

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* SAFETY PROPERTY 1. State invariant.                                    *)
(*                                                                         *)
(* An exclusive resource is free or held at exactly one generation, never   *)
(* both and never neither. This is the observable form of mutual           *)
(* exclusion, and it is what `claims.holder` reports to a caller.          *)
(***************************************************************************)
Property1_ExclusiveResourceHasOneOwner ==
    \A r \in Resources : (owner[r] = NoOwner) <=> (owned[r] = Zero)

(***************************************************************************)
(* SAFETY PROPERTY 2. No accepted decision is ever appended on behalf of  *)
(* a generation that is not its owner's current one.                       *)
(*                                                                         *)
(* A superseded attempt that still holds its process and its evidence must  *)
(* not be able to publish an accepted verdict after reassignment.          *)
(***************************************************************************)
Property2_SupersededGenerationCannotAccept ==
    StaleGenerationAccepted \notin violations

(***************************************************************************)
(* SAFETY PROPERTY 3. No decision is ever appended unless every required  *)
(* check has a passing result under the exact attempt and revision         *)
(* identity that decision names.                                           *)
(*                                                                         *)
(* The mutant MUTANT_MISSING_CHECK drops one check from the guard, and the *)
(* witness then fires on a real trace.                                     *)
(***************************************************************************)
Property3_AcceptanceRequiresEveryCheck ==
    MissingCheckAccepted \notin violations

(***************************************************************************)
(* SAFETY PROPERTY 4. No accepted decision names a source or policy       *)
(* revision other than the current one.                                    *)
(*                                                                         *)
(* So a candidate or policy change cannot be satisfied by readiness       *)
(* decided under the identity it replaced. A decision already recorded    *)
(* stays on record as history; it is not deletable and that is correct,    *)
(* because the evidence behind it is not deletable either.                 *)
(***************************************************************************)
Property4_IdentityChangeInvalidatesPriorReadiness ==
    StaleIdentityAccepted \notin violations

(***************************************************************************)
(* SAFETY PROPERTY 5. No crash step changes any ownership fact.            *)
(*                                                                         *)
(* A crashed owner's resource must stay held by that owner, however long   *)
(* the crashed process goes on running, and must never be handed to a       *)
(* competing live attempt. Property1 cannot see this: it is an iff between  *)
(* the owner and the owning generation, and a clean transfer satisfies it.  *)
(* That is why the detector compares the ownership variables across the     *)
(* Crash step itself, rather than reading them as a state. The mutant      *)
(* MUTANT_CRASH_RELEASES produces a well-typed transfer, leaves Property1   *)
(* and TypeOK satisfied, and fails here.                                   *)
(***************************************************************************)
Property5_CrashAloneDoesNotTransferOwnership ==
    OwnershipMovedOnCrash \notin violations

=============================================================================

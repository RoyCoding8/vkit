"""A small reference model of the ownership and acceptance decision.

This is a second, deliberately naive implementation of the rules the TLA+ model
and the Python core both claim to follow. It holds everything in dictionaries,
performs no validation, and has no notion of a database, a transaction, or a
process. That is the point. It is written from the documented rules so that
when it and the core disagree, one of them has misunderstood a rule, and the
disagreement is a fact about the rules rather than a shared bug.

## What the correspondence tests establish, and what they do not

They establish CORRESPONDENCE: over the operation sequences Hypothesis
generates, the core and this model produce the same observable answers. They do
NOT establish equivalence of implementation. The two differ in almost every
mechanical respect, this model is finite where the core is not, and Hypothesis
samples rather than exhausts. A passing run means "no disagreement was found in
the sequences tried", which is a weaker sentence than "they agree".

The distinction matters because the TLA+ model and this file agree about the
rules, and neither is the core. A bug shared between the TLA+ model and this
reference model would be invisible to every test in the repository. Only the
core is under test here; the reference is the thing being compared against.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

Verdict = Literal["READY", "REJECTED", "BLOCKED"]
Outcome = Literal["PASS", "FAIL", "BLOCKED"]


@dataclass
class Evidence:
    """One recorded check result, bound to the identity it was produced under."""

    check: str
    owner: str
    generation: int
    revision: str
    outcome: Outcome

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.owner, self.check, self.generation, self.revision)


@dataclass
class Model:
    """The whole world, as a value. Every operation returns a new model.

    Nothing here mutates. That is what makes it safe to compare against a real
    database, and it is also why it is small enough to read in one sitting: a
    transition is visible as a single return.
    """

    # owner -> the generation its attempt currently has
    generations: dict[str, int] = field(default_factory=dict)
    # owner -> True while its process is running
    alive: dict[str, bool] = field(default_factory=dict)
    # resource -> (owner, generation) or None when free
    claims: dict[str, tuple[str, int] | None] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    # the source+policy identity the project is currently at
    revision: str = "rev1"
    # the most recent accepted decision, or None
    accepted: tuple[str, int, str] | None = None

    # --- setup ------------------------------------------------------------

    def add_owner(self, owner: str) -> "Model":
        return replace(
            self,
            generations={**self.generations, owner: 1},
            alive={**self.alive, owner: True},
        )

    def add_resource(self, key: str) -> "Model":
        # A free resource is simply absent from the mapping, which is what a
        # released or never-claimed resource looks like in the core's table.
        return replace(self, claims={k: v for k, v in self.claims.items() if k != key})

    def set_revision(self, revision: str) -> "Model":
        return replace(self, revision=revision)

    # --- operations -------------------------------------------------------

    def acquire(self, owner: str, resource: str) -> "Model":
        """Take a free resource for the owner's current generation.

        Refused when the resource is already held. A superseded process still
        running presents its OLD generation, but the claim table stores only
        the current generation for an owner, so a re-acquire after supersession
        simply presents the new one and is allowed. That matches the core,
        which has no separate 'is this process still the attempt' flag: the
        generation is the only identity, and a reassigned task legitimately
        takes its resources again.
        """
        if self.claims.get(resource) is not None:
            return self
        generation = self.generations[owner]
        return replace(self, claims={**self.claims, resource: (owner, generation)})

    def record(self, owner: str, check: str, revision: str, outcome: Outcome) -> "Model":
        """Record a result under the generation the owner currently has.

        The core records a run whenever a check is started, with no liveness
        flag on the attempt, so this does not gate on `alive` either. It records
        at whatever generation the owner currently has, which is how evidence
        never ends up naming a generation the owner has already moved past.
        """
        record = Evidence(check, owner, self.generations[owner], revision, outcome)
        return replace(self, evidence=[*self.evidence, record])

    def release(self, owner: str, resource: str) -> "Model":
        """Give up this generation's claims, keeping the claims held at any
        OTHER generation.

        The core's `claims.release` refuses the whole call when this owner
        holds anything at a generation other than the one it is releasing as.
        A superseded attempt that still holds its old claim cannot release
        anything at all, even a claim it took at the new generation, because
        letting it report success is how an attempt that owns nothing gets to
        act as though it does. The named resource is a filter applied after
        that refusal, not a substitute for it.

        Releasing DELETES the entry rather than setting it to None, because
        that is what the core does: a released claim leaves no row, and a
        resource that was never claimed also has no row. `holder` returns None
        for both, which is the observable the comparison uses.

        Hypothesis found the old-generation refusal: acquire a resource, crash,
        acquire a second one, then release the second. The core keeps the
        second one held and the first version of this model freed it.
        """
        current = self.generations[owner]
        if any(
            held is not None and held[0] == owner and held[1] != current
            for held in self.claims.values()
        ):
            return self
        held = self.claims.get(resource)
        if held is not None and held == (owner, current):
            return replace(
                self,
                claims={k: v for k, v in self.claims.items() if k != resource},
            )
        return self

    def crash(self, owner: str) -> "Model":
        """Stop an owner. Its claims are deliberately left held.

        A crashed process may still be writing, so freeing its resource is how a
        stale worker clobbers a live one. Only an explicit release, after
        evidence, gives the resource back.
        """
        if not self.alive.get(owner):
            return self
        return replace(self, alive={**self.alive, owner: False})

    def reconcile(self, owner: str) -> "Model":
        """Advance the generation, keeping the claims held.

        The core's `supersede_task` advances the generation whatever the
        attempt's liveness, and it only refuses a CLOSED task. Liveness is not
        a gate here either, so a reconcile on a still-running attempt advances
        it, which is the same thing superseding a live attempt means. The
        claims are untouched, which is the point: a superseded attempt whose
        resources vanished under it is how a stale worker clobbers a live one.
        """
        return replace(
            self,
            generations={**self.generations, owner: self.generations[owner] + 1},
        )

    # --- the decision -----------------------------------------------------

    def _latest_by_check(self, owner: str, generation: int | None = None) -> dict[str, Outcome]:
        """The most recent terminal result per check for this owner.

        This mirrors the core's `compute_readiness`, which reads every run
        attached to the task, keeps the ones recorded at the task's CURRENT
        attempt generation, takes them newest-first, and uses the first result it
        finds for each required check.

        It does NOT filter by revision: the core keys evidence on the task, not on
        the source and policy pair. The reference deliberately does the same, so
        a divergence about revision filtering shows up as a disagreement rather
        than being hidden by a stricter model.

        That is a statement about the TLA+ model, not about the core, and the
        difference is a real one rather than a disagreement about wording. "The
        project moves to a new revision" names at least two different movements,
        and they are not equally visible. Measured by
        `review/probe_revision_movement.py` over the real `Store` on a real
        checkout, each case opening from a READY the same evidence produced:

          empty commit, HEAD moves, no file changes
              finalize READY. `SourceIdentity.head` moved and
              `inventory_digest` did not, because `COMPARED_IDENTITIES` compares
              the inventory digest and `source_unchanged` says the digest decides
              and HEAD deliberately does not.
          edit a declared input, then commit
              finalize BLOCKED, gap names source.
          rewrite verification/manifest.json, evidence not re-run
              finalize BLOCKED, gap names SOURCE. The manifest is a tracked file,
              so rewriting it moves the inventory digest, and the recorded run
              genuinely still answers the contract this attempt was admitted
              under.
          rewrite verification/manifest.json, evidence re-run under it
              finalize BLOCKED, gap names POLICY. The digest compared against is
              the one pinned at ADMISSION, which does not follow the manifest.

        The TLA+ model forbids acceptance after ANY `ChangeRevision`, because
        `Accept` requires `evidence[c][o][g][r]` at `r = curRevision` and a result
        is recorded per identity, so the new identity starts with no results at
        all. Its `Revisions` are COMBINED source-and-policy identities, so a
        movement that changes no file content is not a state that model can even
        name. The core and this model agree with each other and both disagree
        with it, on exactly one case: a movement that rewrites no file content.
        Property4 is therefore genuinely violated rather than merely unmet, and
        the violation is invisible to the correspondence tests because they call
        `compute_readiness` with no `AcceptanceContext`, so no identity is
        compared on either side. That blindness is a separate, larger gap, named
        in the `acceptable` docstring below and measured in
        `tests/test_identity_invalidation.py`, and it is not something this file
        can close by filtering.

        `generation=None` deliberately ignores the generation, and exists for
        callers that want the recorded history rather than this attempt's
        evidence. `decide` passes the generation, because the core filters by it
        and a model that did not would answer about runs the attempt never made.
        """
        latest: dict[str, Outcome] = {}
        for record in self.evidence:
            if record.owner != owner:
                continue
            if generation is not None and record.generation != generation:
                continue
            latest[record.check] = record.outcome  # later run wins
        return latest

    def acceptable(self, owner: str, generation: int, revision: str) -> bool:
        """The documented condition, as the core now implements it.

        Every required check needs a passing most-recent result recorded for this
        owner AT THIS GENERATION. A superseded generation's evidence does not
        count: `supersede_task` advances the generation precisely to invalidate
        the previous attempt's authority, and CONTRACT.md says a retry is a new
        run linked to its predecessor rather than a continuation of it.

        An earlier version of this method accepted the generation argument and
        ignored it, with a comment saying the core ignored it too. That was true
        until the readiness fix, and then it was not, and the model was encoding
        the bug as the specification. The property test found it on its first
        execution -- `record FAIL` then `crash` gives core=BLOCKED and
        reference=REJECTED -- because it had never run before. Hypothesis being
        absent from the environment is what kept the disagreement invisible.

        The revision is still not consulted here, and that no longer looks like
        agreement. An earlier version of this comment said the core "does not
        invalidate a task's readiness when the project moves to a new revision
        either, so the two agree about that and the known divergence is narrower
        than it was", while `_latest_by_check` said the opposite about the same
        code. The sentence was true of a content change and false of an empty
        commit, and the measured table in `_latest_by_check` is what separates
        them. Summarised so the two docstrings cannot drift again: this method and
        the core agree that a movement which rewrites file content invalidates
        readiness, and both disagree with the TLA+ model about a movement which
        does not.

        The blindness that used to hide this is what the paragraph above is about.
        Deciding without an `AcceptanceContext` compares no identity, so a source
        or policy change is invisible to this method AND to the core. The
        reference is not a weaker model of a stronger core here; it is a model of
        the context-free decision, which is the decision
        `tests/test_formal_correspondence.py` actually compares. Closing the gap
        means changing which core call the comparison makes, not changing what
        this method does.
        """
        latest = self._latest_by_check(owner, generation=generation)
        return all(latest.get(check) == "PASS" for check in REQUIRED_CHECKS)

    def accept(self, owner: str, generation: int, revision: str) -> "Model":
        """Submit an acceptance decision.

        Three guards. The first two are the stale-generation and identity
        guards; the third is full check coverage. A decision that clears all
        three is recorded, and a decision that does not is refused with the
        model unchanged.
        """
        if generation != self.generations[owner]:
            return self
        if revision != self.revision:
            return self
        if not self.acceptable(owner, generation, revision):
            return self
        return replace(self, accepted=(owner, generation, revision))

    def decide(self, owner: str, generation: int, revision: str) -> Verdict:
        """What a caller would be told, mirroring compute_readiness' order.

        A recorded FAIL is an answer, not an absence, so it decides REJECTED
        immediately and outranks a missing check. This ordering is the core's
        and is deliberate: a failing check is not "incomplete".

        Evidence from another generation is not eligible here, and that includes
        a failure. This is F09's repair and the model is the side that had it
        wrong: it used to carry a stale FAIL across a generation change, on the
        reading that a retry that re-ran one check was not a repair of a failure
        in another. That reading made a repaired attempt permanently unaccepted,
        which is the defect the finding names. The failure is kept — as history,
        which the core reports separately — and the current attempt is decided
        only on runs it produced itself. So the generation is passed through to
        `_latest_by_check` rather than read across every attempt's evidence.

        A stale BLOCKED never carried across in the first place: an interrupted
        run that was never re-run is a gap in the current attempt, not a verdict
        about it, and a retry that re-runs it is exactly the repair that case
        calls for.
        """
        latest = self._latest_by_check(owner, generation)
        if any(outcome == "FAIL" for outcome in latest.values()):
            return "REJECTED"
        if not self.acceptable(owner, generation, revision):
            return "BLOCKED"
        return "READY"

    # --- observation ------------------------------------------------------

    def holder(self, resource: str) -> tuple[str, int] | None:
        return self.claims.get(resource)


# The finite domain the comparison runs over. It matches the TLA+ model's
# configuration so a reader can line the two up, and it is small enough that
# Hypothesis explores it densely.
MAX_GENERATION = 2
REQUIRED_CHECKS = ("unit", "lint")
REVISIONS = ("rev1", "rev2")
OWNERS = ("w1", "w2")
RESOURCES = ("checkout-A", "checkout-B")

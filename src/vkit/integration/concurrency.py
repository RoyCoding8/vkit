"""Two independent bounds, and the ownership that makes them hold.

The plan asks for bounded active writers and bounded running verification
processes, with the bounds independent of each other. That is two capacity
resources, not one, and the reason is not tidiness: a writer holds its slot for
the whole unit of work while a verification holds its slot only for the process,
so a bound of one writer and four verifications is a real configuration and
collapsing them into a single number would make it unreachable.

**The keys are fixed strings, not derived values.** A resource key is compared
byte for byte in a SQL primary key, so anything computed from a task id, a path
or a timestamp would let two callers name the same pool differently and each
believe it had a slot. `WRITER_POOL` and `VERIFICATION_POOL` are therefore
constants, and a campaign that wants different pools wants different keys,
which is a decision rather than an accident.

**A declared scope is exclusive, because "one assigned owner" is the
requirement.** `scope:<name>` is an exclusive claim, so two tasks that both need
a shared lockfile serialize on it and the second is refused with a name in the
error rather than deadlocking behind a queue. The refusal is the useful
outcome: the caller can be told the scope is owned, and a task that needs a
scope nobody holds takes it immediately.

**What this module does not do.** It does not schedule. Admission is a call to
`acquire` that either takes the whole batch or takes nothing, and the caller
does the work. There is no queue, no priority, no fairness, and no retry loop,
because a coordinator that needs those is a second scheduler and docs/verification.md
forbids one.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..claims import Claim, ResourceSpec, acquire, release

WRITER_POOL = "capacity:writers"
VERIFICATION_POOL = "capacity:verification"

EXCLUSIVE = "exclusive"
CAPACITY = "capacity"


class CapacityError(Exception):
    """The campaign's declared bounds are not usable."""


def writer_spec(bound: int) -> ResourceSpec:
    return ResourceSpec(WRITER_POOL, CAPACITY, capacity=_positive(bound, "writer bound"))


def verification_spec(bound: int) -> ResourceSpec:
    return ResourceSpec(VERIFICATION_POOL, CAPACITY, capacity=_positive(bound, "verification bound"))


def scope_spec(scope: str) -> ResourceSpec:
    """The exclusive claim behind a shared write scope.

    A scope name is the identity of the thing being shared -- `lockfile`,
    `schema`, a migration directory -- and it is compared exactly. A name that
    could be spelled two ways would let two tasks hold what they both call the
    same scope, so the name is required to be nonempty and is used verbatim.
    """
    if not scope or not scope.strip():
        raise ValueError("a write scope needs a name")
    return ResourceSpec(f"scope:{scope.strip()}", EXCLUSIVE)


def _positive(bound: int, what: str) -> int:
    """A bound of zero would mean the campaign can never start.

    That is a configuration mistake rather than a legitimate pause, so it is
    refused where it is written instead of showing up as a campaign that admits
    nothing and gives no reason.
    """
    if not isinstance(bound, int) or isinstance(bound, bool) or bound < 1:
        raise CapacityError(f"{what} must be a positive whole number, got {bound!r}")
    return bound


@dataclass(frozen=True)
class Admission:
    """What a caller holds after a successful `admit`, and what it must return.

    `keys` is what `release_admission` needs and nothing more. Recomputing it
    from a task's contract at release time would be a second source of truth
    about what was taken, and a task that added a check between admission and
    release would then leak its scope.
    """

    task_id: str
    generation: int
    keys: tuple[str, ...]

    def release(self, store) -> None:
        release(store, self.task_id, self.generation, self.keys)


def admit(
    store,
    task_id: str,
    generation: int,
    *,
    writers: int | None = None,
    verifications: int | None = None,
    scopes: tuple[str, ...] = (),
) -> Admission:
    """Take the writer slot, the verification slot and every named scope, or none.

    `acquire` is all-or-nothing across the batch, so a caller never holds a
    writer slot without the scopes that make writing safe. Asking for no
    capacity resource at all is legal: a read-only reviewer needs no writer slot
    and no private checkout, and a campaign that only reviews should not have to
    configure a bound it never uses.
    """
    specs: list[ResourceSpec] = []
    if writers is not None:
        specs.append(writer_spec(writers))
    if verifications is not None:
        specs.append(verification_spec(verifications))
    specs.extend(scope_spec(scope) for scope in scopes)
    if not specs:
        return Admission(task_id, generation, ())
    acquire(store, task_id, generation, specs)
    return Admission(task_id, generation, tuple(s.key for s in specs))


def observation(store) -> dict[str, int | None]:
    """The current occupancy of both pools.

    Returned for a caller that has to show a user why work is waiting. Reading
    `claim_holders` here rather than trusting a counter kept anywhere else is
    the point: there is one writable authority for who holds what.
    """
    from ..claims import holder

    out: dict[str, int | None] = {}
    for key in (WRITER_POOL, VERIFICATION_POOL):
        claim: Claim | None = holder(store, key)
        out[f"{key}:capacity"] = claim.capacity if claim else None
        out[f"{key}:held"] = claim.held if claim else 0
    return out

"""One validated contract, one admission decision, one readiness decision.

`admit` and `finalize` are the authority. Every adapter — CLI, MCP, hooks,
console enrollment — calls them and translates the answer into its own response
shape. None of them holds a rule of its own, because the moment two of them hold
one, a caller can pick the laxer adapter and get the laxer answer.

**Admission derives; it does not accept.** A caller names what it wants and
this module derives what is actually true: which repository and checkout this
is, which policy is approved, what the mandatory floor is, whether the claims
are free. A caller-supplied `policy_digest` and a caller-supplied `checkout_ref`
were both accepted at face value, which is how a task bound to
`a-nonexistent-ref` executed a real check and then compared its evidence
against the string it had supplied itself.

**The floor is frozen at admission and can only grow.** `required_checks` is
the approved mandatory set at the moment the task opened. Because it is derived
from the policy that is actually in force rather than from a list the caller
carried, deleting the policy file cannot shrink it, and a caller that selects
fewer checks cannot subtract from it. A task with no mandatory checks is
refused: there would be nothing for it to prove.

**A claim conflict is a refusal, not a note.** The required resources are taken
inside the transaction that writes the task row, so there is no state in which
a task exists as admitted and does not hold what it declared. `admitted: true`
printed beside a `claim_conflict` was that state, and it is what let two
workers write one checkout.

**Acceptance compares identity, and history is not a verdict.** A run recorded
which source, policy and fixtures it actually tested. `finalize` compares those
against the identities in force now; a mismatch is a gap for THIS attempt and
no other. Earlier attempts are returned as `history`, never erased and never
treated as the current attempt's answer, so a repaired attempt can be READY
while its predecessor's failure is still on the record and still visible.

An identity a run did not record is reported in `unverified_identities` rather
than compared, because "recorded as nothing" is not the same claim as
"recorded as something different" and only the second one is a gap.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from .claims import ResourceSpec, acquire_in
from .claimkind import ClaimCategory
from .identity import compute_source_identity
from .paths import Project
from .storage import ConflictError, Store, open_task as insert_task_row
from .verifiers.obligation import (
    Obligation,
    describe as describe_obligation,
    obligation_from_json,
    obligation_to_json,
)

TaskStatus = Literal["active", "paused", "closed"]
Readiness = Literal["READY", "REJECTED", "BLOCKED"]

#: The first generation an admitted task opens at.
FIRST_GENERATION = 1


class TaskError(Exception):
    """The requested task operation is not valid for the recorded state."""


class AdmissionRefused(TaskError):
    """The task cannot be admitted, and the reason names what would fix it.

    Separate from `TaskError` so an adapter can answer INVALID for a malformed
    request while still reserving BLOCKED for a recorded contract this build
    cannot validate, without either of them deciding the question itself. Only
    `task finalize` uses that split today; `task begin` reports both as INVALID,
    because every reason admission gives there is a reason the request is
    malformed rather than a conflict.

    A required check that has never run is not this. That is a readiness
    decision, not an exception: `finalize` returns BLOCKED from its gap list.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- the contract


@dataclass(frozen=True)
class Requirement:
    """One obligation a task owes, and the kind of evidence that could pay it.

    Three facts, not one. `check_id` says which run answers it, `evidence_kind`
    says what kind of answer is acceptable, and `obligations` says what that
    answer has to have discharged. Collapsing them into a bare check id is what
    let a scenario run of three named cases satisfy a floor that named two
    theorems, because a green tick carries none of the two facts that would have
    refused it.

    `evidence_kind` is nullable and that is a real answer, not a missing one. A
    requirement that pins no category is a requirement about verdicts rather than
    about evidence: it accepts whatever the run recorded, and the obligation set
    is still compared. Treating null as a wildcard was a deliberate reading
    rather than a convenient default, and it is the reading a policy that omits
    `evidence_kind` gets.

    `obligations` may also be empty, and that is a different thing from null. An
    empty set means the check is required but owes nothing by name, which is the
    whole of a v1 scenario check whose required cases are enforced by its own
    driver.
    """
    check_id: str
    obligations: tuple[Obligation, ...] = ()
    evidence_kind: ClaimCategory | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "evidence_kind": self.evidence_kind.value if self.evidence_kind else None,
            "obligations": [obligation_to_json(o) for o in self.obligations],
        }

    @classmethod
    def from_json(cls, document: Any) -> "Requirement":
        """Read a pinned requirement back, refusing one this build cannot hold.

        Parse, not cast, for the same reason `PolicyCheck` parses its own: a
        theorem silently read as a case is the confusion the typed obligations
        exist to prevent, and `obligation_from_json` refuses an unknown shape
        rather than inventing one.
        """
        if not isinstance(document, dict):
            raise AdmissionRefused("a requirement must be a JSON object")
        check_id = document.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            raise AdmissionRefused("a requirement needs a nonempty check_id")
        raw_kind = document.get("evidence_kind")
        category = None
        if raw_kind is not None:
            try:
                category = ClaimCategory(raw_kind)
            except ValueError:
                known = ", ".join(c.value for c in ClaimCategory)
                raise AdmissionRefused(
                    f"required check {check_id!r} was pinned evidence_kind "
                    f"{raw_kind!r}, which is not one of {known}. The category is "
                    f"derived from the check's variant, so a requirement may only "
                    f"pin one that already holds."
                ) from None
        raw_obligations = document.get("obligations", [])
        if not isinstance(raw_obligations, list) or any(
            not isinstance(o, dict) for o in raw_obligations
        ):
            raise AdmissionRefused(
                f"required check {check_id!r}: obligations must be a list of "
                f"obligation objects"
            )
        try:
            obligations = tuple(obligation_from_json(o) for o in raw_obligations)
        except (KeyError, TypeError, ValueError) as exc:
            raise AdmissionRefused(
                f"required check {check_id!r}: cannot read obligations: {exc}"
            ) from exc
        return cls(check_id=check_id, obligations=obligations, evidence_kind=category)


@dataclass(frozen=True)
class TaskContract:
    """What a task is, in the form every guarantee is derived from.

    Each field here is a fact the rest of the system compares against. The
    caller supplied none of them: `repository`, `policy_digest` and
    `required_checks` are measured at admission, and a task able to carry a
    caller's string instead would let acceptance compare the task against
    itself and find nothing wrong.
    """

    #: The repository root and shared Git directory this attempt is bound to.
    repository: dict[str, str]
    #: The manifest digest approved for this attempt, measured at admission.
    policy_digest: str
    #: The frozen mandatory floor. Never empty, and never reduced afterwards.
    required_checks: tuple[str, ...]
    #: What the task is for. Declarative: it records intent, it sandboxes
    #: nothing, and `required_checks` is what the decision is made against.
    scope: str
    #: The resources this attempt requires. Taken atomically with the row.
    resources: tuple[dict[str, Any], ...]
    #: What the caller supplied that is not one of the above, preserved so an
    #: adapter can round-trip its own request without the core endorsing it.
    declared: dict[str, Any]
    #: What each required check must have established, and in what kind of
    #: evidence. Derived from the floor at admission when a caller pins none, so
    #: a contract written before this field existed reads back with the
    #: requirements the manifest's own checks declare rather than with an empty
    #: set that would owe nothing.
    requirements: tuple[Requirement, ...] = ()

    def resource_specs(self) -> tuple[ResourceSpec, ...]:
        """The declared resources as `claims` takes them.

        Constructed from the stored declarations rather than kept alongside
        them, so there is one representation in the contract and a caller
        cannot record the same resource twice under two spellings.
        """
        specs = []
        for entry in self.resources:
            key, kind = entry["key"], entry["kind"]
            if kind == "exclusive":
                specs.append(ResourceSpec(key=key, kind="exclusive"))
            else:
                capacity = entry.get("capacity")
                if not isinstance(capacity, int) or capacity < 1:
                    raise AdmissionRefused(
                        f"resource {key!r} is capacity-kind and needs a positive capacity"
                    )
                specs.append(ResourceSpec(key=key, kind="capacity", capacity=capacity))
        return tuple(specs)

    def to_json(self) -> dict[str, Any]:
        return {
            "repository": dict(self.repository),
            "policy_digest": self.policy_digest,
            "required_checks": list(self.required_checks),
            "scope": self.scope,
            "resources": [dict(entry) for entry in self.resources],
            "declared": dict(self.declared),
            "requirements": [r.to_json() for r in self.requirements],
        }

    @classmethod
    def from_json(cls, document: Any) -> "TaskContract":
        """Read a contract back, refusing one that cannot be the contract pinned.

        A row written by an older build, or edited by hand, can hold a document
        this shape does not describe. Recovering with a best-effort default is
        how an empty floor reaches acceptance, so a malformed contract raises
        and the caller reports BLOCKED rather than deciding.
        """
        if not isinstance(document, dict):
            raise AdmissionRefused("a task contract must be a JSON object")
        required = document.get("required_checks")
        if not isinstance(required, list) or not required:
            raise AdmissionRefused(
                "this task was admitted without a mandatory check floor, so there is "
                "nothing to accept it against. Reopen it with an explicit contract"
            )
        if any(not isinstance(check, str) or not check for check in required):
            raise AdmissionRefused(
                "this task recorded a required_checks floor containing something "
                "other than a check id"
            )
        raw_requirements = document.get("requirements")
        if raw_requirements is None:
            # A contract written before the requirement set existed. It pins a
            # floor of check ids and nothing else, so what each one owes is read
            # off the checks the manifest declares rather than invented. An empty
            # set here would owe nothing and would accept any PASS.
            requirements = None
        elif not isinstance(raw_requirements, list):
            raise AdmissionRefused("this task recorded a malformed requirements list")
        else:
            requirements = tuple(Requirement.from_json(r) for r in raw_requirements)
            pinned = {r.check_id for r in requirements}
            unknown = sorted(pinned - set(required))
            if unknown:
                raise AdmissionRefused(
                    f"this task pinned requirements for check(s) "
                    f"{', '.join(unknown)}, which are not in its floor. A "
                    f"requirement for a check the task does not require is not "
                    f"narrower, it is a different contract"
                )
        for key in ("policy_digest", "scope"):
            if not isinstance(document.get(key), str):
                raise AdmissionRefused(f"this task recorded a malformed {key}")
        repository = document.get("repository")
        if not isinstance(repository, dict) or not repository.get("root"):
            raise AdmissionRefused(
                "this task recorded no repository binding, so its checkout cannot "
                "be verified. Reopen it against this repository"
            )
        resources = document.get("resources")
        if not isinstance(resources, list):
            raise AdmissionRefused("this task recorded a malformed required-resources list")
        declared = document.get("declared") or {}
        if not isinstance(declared, dict):
            raise AdmissionRefused("this task recorded malformed declared metadata")
        if "host" in declared:
            validate_host_binding(declared["host"])
        return cls(
            repository=dict(repository),
            policy_digest=document["policy_digest"],
            required_checks=tuple(required),
            scope=document["scope"],
            resources=tuple(dict(entry) for entry in resources),
            declared=dict(declared),
            requirements=() if requirements is None else requirements,
        )


def validate_host_binding(value: Any) -> dict[str, str]:
    """Validate the host identity used by completion hooks."""
    if not isinstance(value, dict) or set(value) - {"session_id", "agent_id"}:
        raise AdmissionRefused("host must contain only session_id and optional agent_id")
    session_id = value.get("session_id")
    agent_id = value.get("agent_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise AdmissionRefused("host.session_id must be a non-empty string")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id.strip()):
        raise AdmissionRefused("host.agent_id must be a non-empty string when given")
    return {"session_id": session_id, **({"agent_id": agent_id} if agent_id else {})}


@dataclass(frozen=True)
class TaskRecord:
    """A task as stored. Reading it proves nothing about whether it is valid."""

    task_id: str
    status: TaskStatus
    generation: int
    contract: dict[str, Any]
    policy_digest: str
    readiness: Readiness | None

    def pinned(self) -> TaskContract:
        """The validated contract this attempt was admitted against."""
        return TaskContract.from_json(self.contract)


# --------------------------------------------------------------- persistence

_TASK_COLUMNS = "task_id, status, generation, contract_json, policy_digest, readiness"


def _row_to_task(conn: sqlite3.Connection, task_id: str) -> TaskRecord:
    """Read a task on the caller's connection.

    Every read inside a transaction must use that connection. Opening a second
    one returns the pre-commit state, which is how a successful `set_status`
    reported the old status: the write was right and the answer was a lie.
    """
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise TaskError(f"no such task: {task_id}")
    return TaskRecord(
        task_id=row[0],
        status=row[1],
        generation=row[2],
        contract=json.loads(row[3]),
        policy_digest=row[4],
        readiness=row[5],
    )


def get_task(store: Store, task_id: str) -> TaskRecord:
    with store._connect() as conn:
        return _row_to_task(conn, task_id)


def current_generation(store: Store, task_id: str) -> int:
    return get_task(store, task_id).generation


def open_task(store: Store, *, task_id: str, contract: dict, policy_digest: str) -> TaskRecord:
    """Write a task row, validating the contract first.

    The storage primitive, not the admission decision: it derives nothing and
    takes no claim, so a caller that reached it directly would hold a task with
    no resources. `admit` is the decision, and every adapter calls that.

    It still validates. Its callers — the formal model's harness and the audit
    probes — build a store without a repository, and the contract they write has
    to be one `pinned()` can read back, or acceptance would have no floor and no
    policy to compare a pass against. Storing an unreadable contract and letting
    the failure surface at acceptance is the defect this refuses.

    Kept because those callers need a task row and no repository. A second
    writer of task rows would be worse than one narrow primitive that admits
    what it is given.
    """
    TaskContract.from_json(contract)
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
            raise TaskError(f"task {task_id} already exists")
        insert_task_row(conn, task_id, contract, policy_digest)
        return _row_to_task(conn, task_id)


# ------------------------------------------------------------------- lifecycle


def supersede_task(store: Store, task_id: str) -> TaskRecord:
    """Advance the generation, invalidating the previous attempt's authority.

    The generation advances only here, and only while the task is not closed.
    Claims held at the old generation are deliberately NOT released: the old
    process may still be alive and writing, and a superseded attempt whose
    resources vanished under it is how a stale worker clobbers a live one.
    Reconciliation is an explicit recovery action with evidence behind it.
    """
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT status, generation FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskError(f"no such task: {task_id}")
        status, generation = row
        if status == "closed":
            raise TaskError(f"task {task_id} is closed and cannot be reassigned")
        conn.execute(
            "UPDATE tasks SET generation = ?, status = 'active' WHERE task_id = ?",
            (generation + 1, task_id),
        )
        return _row_to_task(conn, task_id)


def set_status(store: Store, task_id: str, status: TaskStatus) -> TaskRecord:
    """Pause or close a task.

    Paused does not mean the resources are safe to release. A paused attempt's
    process may still be running, so the claims stay held and the state records
    that the owner is idle rather than gone.

    A closed task retains its reports and its result is final. Reopening one
    let a caller resurrect a finished task and then record a verdict on it, and
    left the row simultaneously active and stamped closed.
    """
    if status not in ("active", "paused", "closed"):
        raise TaskError(f"unknown task status {status!r}")
    with store.transaction() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskError(f"no such task: {task_id}")
        if row[0] == "closed":
            raise TaskError(f"task {task_id} is closed and cannot be reopened")
        if status == "closed":
            conn.execute(
                "UPDATE tasks SET status = 'closed', closed_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
        else:
            conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))
        return _row_to_task(conn, task_id)


# ------------------------------------------------------------------- admission


@dataclass(frozen=True)
class AcceptanceContext:
    """The repository and policy an admission and an acceptance are decided against.

    This is what "the current acceptance context" means: not the identities a run
    recorded, and not what a caller asserts, but what is actually true of this
    checkout and this policy right now.

    A policy that is missing, malformed, registers nothing, or declares an input
    that cannot be read lands in `refusal` and blocks. It never yields an empty
    floor that nothing can ever satisfy and that would read as success.
    """

    project: Project
    policy_digest: str
    source_inventory_digest: str
    fixture_digest: str | None
    #: Why the context is unusable, when it is.
    refusal: str | None = None

    @property
    def usable(self) -> bool:
        return self.refusal is None

    def to_json(self) -> dict[str, Any]:
        return {
            "repository": str(self.project.root),
            "policy_digest": self.policy_digest,
            "source_inventory_digest": self.source_inventory_digest,
            "fixture_digest": self.fixture_digest,
            "refusal": self.refusal,
        }


def acceptance_context(project: Project, manifest_loader) -> AcceptanceContext:
    """Read the policy now and measure the identities acceptance compares against.

    `manifest_loader()` parses the registered policy and raises when it cannot.
    It takes no argument because every caller that has one has already resolved
    the project — a bound `Server._registered_policy`, a lambda over `parse_manifest`
    — and asking each of them to accept a project they already hold is the kind of
    small friction that produces two loader shapes. It must not swallow the parse
    failure: "the policy is unreadable" and "there is no policy" are the same
    refusal here, and a loader that returns None for both would leave this unable
    to say which happened.
    """
    blocked = lambda policy_digest, source, refusal: AcceptanceContext(
        project, policy_digest, source, None, refusal
    )

    try:
        manifest = manifest_loader()
    except Exception as exc:  # noqa: BLE001 - every parse failure is one refusal
        return blocked("", "", f"there is no usable policy at {project.manifest_path}: {exc}")
    policy_digest = manifest.digest()
    if not manifest.checks:
        return blocked(
            policy_digest, "",
            f"the policy at {project.manifest_path} registers no checks, so there is "
            "nothing to admit against",
        )
    try:
        source = compute_source_identity(project)
    except Exception as exc:  # noqa: BLE001
        return blocked(
            policy_digest, "",
            f"the source identity of {project.root} could not be computed: {exc}",
        )
    fixture = manifest.fixture_identity()
    if fixture is None:
        # An unresolved measurement is an unresolved measurement. Recording null
        # and continuing would imply the fixtures had been measured when nothing
        # was read at all.
        return blocked(
            policy_digest, source.inventory_digest,
            f"the policy at {project.manifest_path} declares an input that cannot be "
            "read, so the fixture identity it would test against is unresolved",
        )
    return AcceptanceContext(
        project, policy_digest, source.inventory_digest, fixture.digest
    )


@dataclass(frozen=True)
class AdmittedTask:
    """A task that exists, holds what it declared, and knows what it must prove."""

    task: TaskRecord
    contract: TaskContract

    @property
    def task_id(self) -> str:
        return self.task.task_id

    @property
    def generation(self) -> int:
        return self.task.generation


def _resources(raw: Any) -> tuple[dict[str, Any], ...]:
    """The required-resource declarations, validated into one shape.

    A list of objects is the wire form an agent can write; a mapping is accepted
    because an MCP client naturally has one. Both normalise here so the contract
    stores one representation.
    """
    if raw is None:
        return ()
    if isinstance(raw, dict):
        # Each value has to be an object before it is unpacked. `{"key": k, **v}`
        # raises a TypeError on a bare string, which would leave the caller with
        # a traceback instead of the refusal naming what to send.
        for key, value in raw.items():
            if not isinstance(value, dict):
                raise AdmissionRefused(
                    f"required resource {key!r} must name an object, not "
                    f"{type(value).__name__}"
                )
            if "key" in value:
                raise AdmissionRefused(
                    f"required resource {key!r} is named twice: by the mapping key "
                    "and by a key field; give it one name"
                )
        raw = [{"key": key, **value} for key, value in raw.items()]
    if not isinstance(raw, list):
        raise AdmissionRefused("required_resources must be a list of resource objects")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise AdmissionRefused("each required resource must be an object")
        unknown = sorted(set(entry) - {"key", "kind", "capacity"})
        if unknown:
            raise AdmissionRefused(
                f"a required resource has unsupported key(s) {', '.join(unknown)}; "
                "a resource declares key, kind and capacity"
            )
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            raise AdmissionRefused("a required resource needs a nonempty key")
        if key in seen:
            raise AdmissionRefused(f"resource {key!r} is required twice in one contract")
        seen.add(key)
        kind = entry.get("kind", "exclusive")
        capacity = entry.get("capacity")
        if kind not in ("exclusive", "capacity"):
            raise AdmissionRefused(
                f"resource {key!r} has unknown kind {kind!r}; use exclusive or capacity"
            )
        if kind == "exclusive" and capacity is not None:
            raise AdmissionRefused(
                f"resource {key!r} is exclusive and cannot declare a capacity"
            )
        parsed.append({"key": key, "kind": kind, "capacity": capacity})
    return tuple(parsed)


def admit(
    store: Store,
    task_id: str,
    *,
    context: AcceptanceContext,
    required_checks: Iterable[str] | None = None,
    scope: str = "",
    resources: Any = None,
    declared: dict[str, Any] | None = None,
) -> AdmittedTask:
    """Validate a contract, take what it requires, and open the task at generation 1.

    The row and the claims are written in one transaction. That is the whole of
    the ownership guarantee: there is no window in which a task exists as
    admitted but does not hold the resource it declared, and a refused claim
    leaves no task behind at all.

    `required_checks` is the caller's selection. The frozen floor is the union
    of it with every check the approved policy registers, so a caller may add to
    the floor and cannot subtract from it — and because the floor is derived
    here, deleting the policy file cannot shrink it either.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise AdmissionRefused("a task id is required")
    if not context.usable:
        raise AdmissionRefused(context.refusal or "the acceptance context is not usable")

    resources = _resources(resources)
    # The floor is derived before the contract is built rather than patched into
    # it afterwards, so there is never a TaskContract in hand that has not been
    # validated. A malformed declaration raises here, before any transaction
    # opens, and so cannot reach SQL and leave a rolled-back row behind.
    contract = TaskContract(
        repository={"root": str(context.project.root),
                    "git_common_dir": str(context.project.git_common_dir)},
        policy_digest=context.policy_digest,
        required_checks=_floor(context, required_checks),
        scope=scope if isinstance(scope, str) else "",
        resources=resources,
        declared=dict(declared or {}),
        requirements=_declared_requirements(context),
    )
    specs = contract.resource_specs()

    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
            raise TaskError(f"task {task_id} already exists")
        # Claims first, inside this transaction, so a conflict rolls the whole
        # admission back rather than leaving a task that believes it owns a
        # resource another task holds.
        acquire_in(conn, task_id, FIRST_GENERATION, specs)
        insert_task_row(conn, task_id, contract.to_json(), contract.policy_digest)
        record = _row_to_task(conn, task_id)
    return AdmittedTask(task=record, contract=contract)


def _declared_requirements(context: AcceptanceContext) -> tuple[Requirement, ...]:
    """What each check in the floor owes, read from the approved manifest.

    Derived at admission and then frozen, rather than re-read at decision time.
    That is the whole of requirement 5: a manifest edited after the attempt
    opened still describes what the attempt owes, because what it owes was
    decided when it was admitted and a contract is the record of that decision.

    Read from `context.project`'s manifest through the same loader the policy
    digest came from, so a requirement and the digest it sits beside are two
    readings of one document rather than two documents.

    A check the manifest no longer declares contributes nothing here. It stays
    in `required_checks`, so the existing "no completed run" gap still fires for
    it, and dropping it from the obligation set is what stops the decision
    reporting a category nobody can supply.
    """
    from .manifest import parse_manifest

    try:
        manifest = parse_manifest(context.project, context.project.runs_root)
    except Exception:  # noqa: BLE001 - an unreadable manifest owes nothing
        return ()
    return tuple(
        Requirement(
            check_id=check.id,
            obligations=check.obligations(),
            evidence_kind=check.evidence_kind(),
        )
        for check in sorted(manifest.checks.values(), key=lambda c: c.id)
    )


def _floor(context: AcceptanceContext, selected: Iterable[str] | None) -> tuple[str, ...]:
    """The mandatory checks this attempt is frozen against.

    Read from the policy in force, so the floor survives the policy file
    disappearing and grows when the policy gains a check. A selection naming a
    check the policy does not register is refused rather than dropped: silently
    ignoring it would admit a task that looks like it was asked for something it
    is not.
    """
    from .manifest import parse_manifest

    manifest = parse_manifest(context.project, context.project.runs_root)
    chosen = tuple(selected or ())
    for check_id in chosen:
        if not isinstance(check_id, str) or not check_id.strip():
            raise AdmissionRefused("a required check id must be a nonempty string")
        if check_id not in manifest.checks:
            known = ", ".join(sorted(manifest.checks)) or "<none>"
            raise AdmissionRefused(
                f"unknown check {check_id!r}; the approved policy defines: {known}"
            )
    floor = tuple(sorted(set(manifest.checks) | set(chosen)))
    if not floor:
        raise AdmissionRefused(
            "a task with no mandatory checks cannot be admitted: there would be "
            "nothing for it to prove"
        )
    return floor


def verify_ownership_in(
    conn: sqlite3.Connection,
    task_id: str,
    generation: int,
    *,
    project: Project,
) -> None:
    """Confirm this generation still holds every resource its contract requires.

    The caller supplies its transaction so a launch fence can coordinate this
    decision with recovery. Admission records these facts atomically; launch and
    acceptance recheck them here.
    """
    record = _row_to_task(conn, task_id)
    if record.generation != generation:
        raise ConflictError(
            f"task {task_id} was reassigned from generation {generation} to "
            f"{record.generation}; this attempt no longer owns anything"
        )
    specs = record.pinned().resource_specs()
    repository = record.pinned().repository
    expected_root = Path(str(repository["root"])).resolve()
    if expected_root != project.root.resolve():
        raise ConflictError(
            f"task {task_id} is pinned to checkout {expected_root}, not "
            f"the current checkout {project.root.resolve()}"
        )
    expected_common = repository.get("git_common_dir")
    if (
        expected_common is not None
        and Path(str(expected_common)).resolve() != project.git_common_dir.resolve()
    ):
        raise ConflictError(
            f"task {task_id} is pinned to Git common directory {expected_common}, not "
            f"the current directory {project.git_common_dir}"
        )
    held = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT resource_key, generation FROM claim_members WHERE task_id = ?",
            (task_id,),
        )
    }
    for spec in specs:
        if (spec.key, generation) not in held:
            raise ConflictError(
                f"resource {spec.key!r} required by task {task_id} is no longer held at "
                f"generation {generation}; the attempt cannot proceed until "
                "reconciliation establishes who owns it"
            )


def verify_ownership(
    store: Store,
    task_id: str,
    generation: int,
    *,
    project: Project,
) -> None:
    """Run the shared checkout, generation and claim check in one transaction."""
    with store.transaction() as conn:
        verify_ownership_in(conn, task_id, generation, project=project)


# ------------------------------------------------------------------ readiness


@dataclass(frozen=True)
class ReadinessResult:
    """The verdict, and everything a reader needs to act on it.

    `gaps` blocks THIS attempt. `history` does not: it is what earlier attempts
    reached, kept because erasing a failure in order to reach READY would make
    the record a lie, and kept separate because a superseded attempt's failure
    is not this attempt's verdict.
    """

    readiness: Readiness
    gaps: tuple[str, ...]
    context: dict[str, Any]
    history: tuple[str, ...] = ()
    #: The identities the decision was made against, so a reader can see what was
    #: compared rather than being told only that it matched.
    tested: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "readiness": self.readiness,
            "gaps": list(self.gaps),
            "context": dict(self.context),
            "history": list(self.history),
            "tested": dict(self.tested),
        }


def finalize(
    store: Store,
    task_id: str,
    *,
    context: AcceptanceContext,
    additional_checks: Iterable[str] = (),
) -> ReadinessResult:
    """Decide READY, REJECTED or BLOCKED, and record it. The only such decision.

    The order encodes the domain:

    1. A recorded FAIL at this attempt is an answer, and REJECTED outranks
       BLOCKED. A failing check is not "incomplete".
    2. A missing required check, a blocked one, or an identity that no longer
       matches is BLOCKED. Absent or stale evidence is never success.
    3. Only evidence eligible for this attempt, under this contract and these
       identities, reaches READY.

    A run recorded with no attempt number stays readable as standalone evidence
    and cannot satisfy acceptance, because nothing establishes which contract it
    was produced under.
    """
    if not context.usable:
        record = get_task(store, task_id)
        if record.status == "closed":
            raise TaskError(f"task {task_id} is closed; its result is final")
        # Refused at the boundary the caller controls. A context with no policy
        # has no identity to require a pass to have been produced under, so
        # there is nothing here that could be compared and no verdict to reach.
        return _blocked_result(record, context.refusal or "the acceptance context is unusable")

    with store.transaction() as conn:
        record = _row_to_task(conn, task_id)
        if record.status == "closed":
            raise TaskError(f"task {task_id} is closed; its result is final")
        contract = record.pinned()
        required = tuple(sorted(set(contract.required_checks) | set(additional_checks)))
        try:
            verify_ownership_in(conn, task_id, record.generation, project=context.project)
        except ConflictError as exc:
            verdict = _blocked_result(record, str(exc))
        else:
            verdict = _decide(
                store, record, required, expected_identities(record, context), conn=conn
            )
        record_readiness_in(conn, task_id, verdict)
        return verdict


def expected_identities(
    record: TaskRecord, context: AcceptanceContext
) -> dict[str, Any]:
    """The identities a passing run must have been produced under.

    The policy is compared against the digest pinned at admission, not against
    the policy in force: the question is whether the evidence answers the
    contract this attempt was admitted under, and re-reading the current policy
    here would make the answer move underneath the run it is judging. Source and
    fixtures are compared against the live measurement, because those are what
    "the code that ran is the code that is here now" means.
    """
    return {
        "source_inventory_digest": context.source_inventory_digest,
        "policy_digest": record.pinned().policy_digest,
        "fixture_digest": context.fixture_digest,
    }


def _blocked_result(record: TaskRecord, gap: str) -> ReadinessResult:
    """BLOCKED, with the recorded generation so the verdict can still be stored."""
    return ReadinessResult(
        "BLOCKED",
        (gap,),
        {"generation": record.generation, "policy_digest": record.policy_digest},
    )


def compute_readiness(
    store: Store,
    task_id: str,
    *,
    required_check_ids: Iterable[str],
    context: AcceptanceContext | None = None,
) -> ReadinessResult:
    """Readiness decided from recorded evidence and the task's pinned contract.

    `finalize` is the authority and is what every adapter that can reach the
    repository calls. This is the same decision over one task, without recording
    it, and it takes an `AcceptanceContext` when the caller has one — pass it and
    the identity comparison runs; omit it and only the frozen floor and the
    evidence decide, which is all a caller with no repository can honestly know.

    A context that arrives unusable is BLOCKED, not a context to fall back from.
    "There is no policy here" and "there is no repository to compare against" are
    different questions, and only the second one answers READY: the first leaves
    the identity a pass would be measured against unmeasured, which is the same
    condition `finalize` already refuses at. Dropping it here is what let an
    adapter that had built a context still decide as though it had not.

    A caller may add to `required_check_ids` and never subtract: the pinned floor
    is unioned in unconditionally.
    """
    if context is None:
        record = get_task(store, task_id)
        required = tuple(sorted(set(record.pinned().required_checks) | set(required_check_ids)))
        return _decide(store, record, required)
    if not context.usable:
        record = get_task(store, task_id)
        return _blocked_result(
            record, context.refusal or "the acceptance context is unusable"
        )
    with store.transaction() as conn:
        record = _row_to_task(conn, task_id)
        required = tuple(sorted(set(record.pinned().required_checks) | set(required_check_ids)))
        try:
            verify_ownership_in(conn, task_id, record.generation, project=context.project)
        except ConflictError as exc:
            return _blocked_result(record, str(exc))
        return _decide(
            store, record, required, expected_identities(record, context), conn=conn
        )


def _decide(
    store: Store,
    record: TaskRecord,
    required: tuple[str, ...],
    expected: dict[str, Any] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> ReadinessResult:
    """The decision rule, over a required set already resolved.

    One function, used by both entry points, because two copies of a verdict rule
    is how the laxer adapter comes to disagree with the strict one.

    A requirement the contract did not pin falls back to requiring the verdict
    alone. That is the reading a contract written before the requirement set
    existed gets, and it is what `compute_readiness` gives a caller that adds a
    check id of its own.
    """
    pinned = {r.check_id: r for r in record.pinned().requirements}
    requirements = tuple(
        pinned.get(check_id) or Requirement(check_id) for check_id in required
    )

    eligible: dict[str, dict] = {}
    history: list[str] = []
    runs = store.list_runs(task_id=record.task_id, limit=1000) if conn is None else _runs_in(
        conn, record.task_id, 1000
    )
    for run in runs:
        if run["lifecycle"] != "terminal" or not run["result"]:
            continue
        if run["attempt"] != record.generation:
            history.append(
                f"check {run['check_id']!r} was recorded at attempt "
                f"{run['attempt'] if run['attempt'] is not None else 'none'}, not this "
                f"attempt {record.generation}"
            )
            continue
        # `list_runs` is newest-first, so the first row seen for a check is the
        # latest one and a re-run supersedes the pass before it.
        eligible.setdefault(run["check_id"], run)

    gaps: list[str] = []
    rejected = False
    tested: dict[str, Any] = {}
    met: dict[str, list[str]] = {}
    unmet: dict[str, list[str]] = {}
    for requirement in requirements:
        check_id = requirement.check_id
        run = eligible.get(check_id)
        if run is None:
            gaps.append(f"no completed run for required check {check_id!r}")
            continue
        result = run["result"]
        if result == "FAIL":
            rejected = True
        elif result == "BLOCKED":
            gaps.append(
                f"required check {check_id!r} is BLOCKED: "
                f"{run['reason'] or 'no reason recorded'}"
            )
        elif result != "PASS":
            gaps.append(f"required check {check_id!r} has no usable result")
            continue
        else:
            # Identity and category are compared only for a pass. A FAIL and a
            # BLOCKED are already answers about this attempt, and re-deciding
            # them against an identity would turn a decided result into a
            # different one.
            recorded = {key: run.get(_RUN_COLUMN[label]) for label, key in COMPARED_IDENTITIES}
            gaps.extend(_category_gaps(requirement, run))
            discharged, undisbursed = _obligation_gaps(requirement, run)
            met[check_id] = discharged
            unmet[check_id] = undisbursed
            if undisbursed:
                gaps.append(
                    f"required check {check_id!r} passed without discharging "
                    f"{len(undisbursed)} required obligation(s): "
                    f"{', '.join(undisbursed)}. A run that establishes some of "
                    f"what was required has not discharged the requirement"
                )
                continue
            tested[check_id] = {
                **recorded,
                "evidence_kind": run.get("evidence_kind"),
                "obligations": list(discharged),
            }
            if expected is not None:
                gaps.extend(_identity_gaps(check_id, recorded, expected))

    readiness: Readiness = "REJECTED" if rejected else "BLOCKED" if gaps else "READY"
    return ReadinessResult(
        readiness,
        tuple(dict.fromkeys(gaps)),
        {
            "generation": record.generation,
            "required_checks": list(required),
            # The requirements as pinned, so a reader can see what each one owed
            # rather than being told only which ones it failed.
            "requirements": [r.to_json() for r in requirements],
            "met_obligations": met,
            "unmet_obligations": unmet,
            "identities": expected,
            # Named rather than left silent. An identity a passing run never
            # recorded cannot be shown to describe what is in force now, and a
            # reader who is not told that is being told the pass covers it.
            "unverified_identities": sorted(
                label
                for label in _UNRECORDED_ON_RUN
                for run in eligible.values()
                if run["result"] == "PASS" and run.get(_RUN_COLUMN[label]) is None
            ),
        },
        tuple(dict.fromkeys(history)),
        tested,
    )


def _category_gaps(requirement: Requirement, run: dict[str, Any]) -> list[str]:
    """Why a passing run does not carry the evidence kind the contract pinned.

    The whole point of this comparison. A requirement that pins a category is
    refused when the run recorded a different one, and a category a run never
    recorded is a gap rather than an absence of disagreement: the run published
    no receipt, and a run that recorded nothing about its own evidence has not
    established what kind of evidence it is.

    A requirement pinning no category asks nothing here, which is a real reading
    rather than a lenient default: a v1 policy names check ids alone, and a
    check id is a statement about which run answers the obligation.
    """
    if requirement.evidence_kind is None:
        return []
    recorded = run.get("evidence_kind")
    wanted = requirement.evidence_kind.value
    if recorded == wanted:
        return []
    if recorded is None:
        return [
            f"required check {requirement.check_id!r} is required to produce "
            f"{wanted!r} evidence and this run recorded no evidence_kind at all. "
            f"A run that published no receipt has not established what kind of "
            f"evidence it is, so it cannot discharge a requirement that names one"
        ]
    return [
        f"required check {requirement.check_id!r} is required to produce "
        f"{wanted!r} evidence but this run produced {recorded!r}. A PASS in one "
        f"category does not discharge an obligation in another: {wanted} "
        f"establishes something this run's category does not"
    ]


def _obligation_gaps(
    requirement: Requirement, run: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Which of a requirement's obligations this run discharged, and which it did not.

    Returns the two lists rather than only the gap text because a reader needs
    both: what the evidence did establish is as much a part of the answer as
    what it failed to.

    **The empty-recording case, and why it is not a hole.** A receipt whose
    adapter reports no obligation results records `satisfied: []`, and a v1
    scenario driver is exactly that: `dispatch._obligation_results` returns two
    empty lists for a reading with no `obligation_results`, because a scenario
    artifact reports scenario ids and inventing obligations from them would be a
    claim the artifact never made. Such an adapter has already enforced its own
    required set at the verdict level -- `execution._scenarios_from_artifact`
    refuses a run whose artifact never reported a required scenario -- so a
    receipt that enumerates nothing is one that accounts for its obligations in
    its verdict rather than in its `satisfied` array. Requiring entries there
    would refuse every v1 scenario task in the product.

    The obligation comparison therefore applies where the receipt carries
    obligation results, which is every adapter that reports them. That is a
    narrower claim than "every obligation is compared", and it is stated here
    rather than left for a reader to discover.
    """
    if not requirement.obligations:
        return [], []
    satisfied = {_satisfied_obligation(entry) for entry in run.get("satisfied") or []}
    if not satisfied:
        return [], []
    discharged = [
        describe_obligation(o) for o in requirement.obligations if o in satisfied
    ]
    missing = [o for o in requirement.obligations if o not in satisfied]
    if not missing:
        return discharged, []
    named = ", ".join(describe_obligation(o) for o in missing)
    return discharged, [named]


def _satisfied_obligation(entry: dict[str, Any]) -> Obligation:
    """One `satisfied` entry read as the obligation it discharges.

    Parsed through the same reader the receipt's writer used, so the two cannot
    disagree about what shape an obligation takes. An entry the reader refuses
    is a receipt this build cannot hold evidence about, and it discharges
    nothing rather than raising here: one unreadable entry must not turn the
    whole decision into a traceback.
    """
    try:
        return obligation_from_json(entry.get("obligation"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return obligation_from_json({"kind": "case", "obligation": ""})


def _runs_in(conn: sqlite3.Connection, task_id: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT run_id, check_id, task_id, attempt, lifecycle, result, reason, "
        "configuration_digest, fixture_digest, source_json, evidence_kind, "
        "satisfied_json FROM runs "
        "WHERE task_id = ? ORDER BY rowid DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    runs = []
    for row in rows:
        source = json.loads(row[9] or "{}")
        runs.append({
            "run_id": row[0], "check_id": row[1], "task_id": row[2],
            "attempt": row[3], "lifecycle": row[4], "result": row[5],
            "reason": row[6], "configuration_digest": row[7],
            "fixture_digest": row[8],
            "source_inventory_digest": source.get("inventory_digest"),
            "evidence_kind": row[10],
            "satisfied": json.loads(row[11] or "[]"),
        })
    return runs


#: The identities acceptance compares, and what it calls each one.
#:
#: The pair is (label, name-in-the-recorded-map), and `_RUN_COLUMN` is the only
#: place the run row's own column names are spelled: the recorded map calls the
#: policy digest `policy_digest` because that is what a contract means by it,
#: while the run row calls it `configuration_digest` because that is what the
#: column is. Two spellings of one fact is exactly the drift a single map
#: removes, and reading the wrong one silently reports a compared identity as
#: unverified.
COMPARED_IDENTITIES = (
    ("source", "source_inventory_digest"),
    ("policy", "policy_digest"),
    ("fixtures", "fixture_digest"),
)

#: Where each compared identity lives on a run row.
_RUN_COLUMN = {
    "source": "source_inventory_digest",
    "policy": "configuration_digest",
    "fixtures": "fixture_digest",
}

#: Older runs may predate fixture recording; they remain unverified and blocked.
_UNRECORDED_ON_RUN = ("fixtures",)

def _identity_gaps(
    check_id: str, recorded: dict[str, Any], expected: dict[str, Any]
) -> list[str]:
    """Why a passing run no longer describes what is here now.

    An absent identity is a gap too: the pass cannot establish which fixtures it
    tested, so it cannot describe the fixtures in force now.
    """
    gaps: list[str] = []
    for label, key in COMPARED_IDENTITIES:
        actual, wanted = recorded.get(key), expected.get(key)
        if wanted is None or actual == wanted:
            continue
        if actual is None:
            gaps.append(
                f"required check {check_id!r} has no recorded {label} identity; "
                "re-run it to establish what it tested"
            )
            continue
        gaps.append(
            f"required check {check_id!r} passed against a different {label} "
            f"({actual}) than the one this attempt is bound to ({wanted}); the {label} "
            "changed since that run, so re-run it"
        )
    return gaps


def record_readiness(store: Store, task_id: str, result: ReadinessResult) -> TaskRecord:
    """Store a readiness verdict, refusing if the attempt was superseded.

    The generation is re-read inside the transaction. A client that began
    finalizing at generation 1 and was reassigned mid-computation must not be
    able to record READY at generation 2.
    """
    with store.transaction() as conn:
        return record_readiness_in(conn, task_id, result)


def record_readiness_in(
    conn: sqlite3.Connection, task_id: str, result: ReadinessResult
) -> TaskRecord:
    """Publish a verdict on a caller's connection and transaction."""
    row = conn.execute(
        "SELECT generation, status FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise TaskError(f"no such task: {task_id}")
    generation, status = row
    if status == "closed":
        raise TaskError(f"task {task_id} is closed; its result is final")
    decided_at = result.context.get("generation")
    if decided_at != generation:
        raise ConflictError(
            f"task {task_id} was reassigned from generation {decided_at} to "
            f"{generation}; refusing to record readiness for a superseded attempt"
        )
    conn.execute(
        "UPDATE tasks SET readiness = ? WHERE task_id = ?", (result.readiness, task_id)
    )
    return _row_to_task(conn, task_id)

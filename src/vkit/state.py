"""What is known about each check right now, and whether the tree is ready."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .formal.policy import FormalComponent, VerificationRequest, WorkingTree
from .inputs import InputSet, Snapshot, capture_working_tree
from .job import FormalGoal, RegisteredCheck, seal_job
from .manifest import Manifest
from .paths import ProjectError
from .store import Store
from .verifiers.spec import CheckSpec


class Freshness(str, enum.Enum):
    FRESH_PASS = "fresh_pass"
    FRESH_FAIL = "fresh_fail"
    FRESH_BLOCKED = "fresh_blocked"
    STALE = "stale"
    MISSING = "missing"
    NOT_APPROVED = "not_approved"
    BLOCKED = "blocked"


class Verdict(str, enum.Enum):
    READY = "READY"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


_FRESH = {
    "PASS": Freshness.FRESH_PASS,
    "FAIL": Freshness.FRESH_FAIL,
    "BLOCKED": Freshness.FRESH_BLOCKED,
}


def check_digests(manifest: Manifest) -> dict[str, str]:
    return {check_id: manifest.digest(check_id) for check_id in manifest.checks}


@dataclass(frozen=True)
class CheckState:
    check: CheckSpec
    digest: str
    key: str
    inputs: InputSet
    freshness: Freshness
    current: dict[str, Any] | None
    last: dict[str, Any] | None
    running: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        from .evidence import assurance

        category = self.check.evidence_kind()
        return {
            "id": self.check.id,
            "description": self.check.description,
            "kind": self.check.kind.value,
            "category": category.value,
            "assurance": assurance(self.check),
            "model_scope": getattr(self.check, "model_scope", category.establishes),
            "assumptions": list(
                getattr(
                    self.check,
                    "assumptions",
                    getattr(self.check, "permitted_axioms", ()),
                )
            ),
            "establishes": category.establishes,
            "does_not_establish": category.does_not_establish,
            "state": self.freshness.value,
            "key": self.key,
            "inputs": {
                "scope": self.inputs.scope,
                "declared": list(self.inputs.declared),
                "unmatched": list(self.inputs.unmatched),
            },
            "current": self.current,
            "last": self.last,
            "running": list(self.running),
        }


@dataclass(frozen=True)
class FormalState:
    component: FormalComponent
    key: str | None
    freshness: Freshness
    current: dict[str, Any] | None
    last: dict[str, Any] | None
    running: tuple[str, ...]
    detail: str | None = None

    @property
    def required(self) -> bool:
        return self.component.required

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.component.id,
            "kind": "formal_goal",
            "description": self.component.contract,
            "category": "source_verification",
            "assurance": "trusted_engine",
            "scope": "required_api",
            "required": self.required,
            "state": self.freshness.value,
            "key": self.key,
            "policy_sha256": self.component.digest,
            "package_sha256": self.component.package.sha256,
            "current": self.current,
            "last": self.last,
            "running": list(self.running),
            "detail": self.detail,
        }


def _resolve_execution(component: FormalComponent) -> dict[str, Any]:
    from .formal.verus import resolve_execution

    return resolve_execution(component)


def _formal_approval_matches(
    approved: dict[str, Any], component: FormalComponent, checker_sha256: str | None = None
) -> bool:
    record = approved.get(component.digest, {})
    return (
        record.get("kind") == "formal_policy"
        and record.get("policy_sha256") == component.digest
        and record.get("package_sha256") == component.package.sha256
        and (checker_sha256 is None or record.get("checker_sha256") == checker_sha256)
    )


def check_states(
    manifest: Manifest, store: Store, snapshot: Snapshot
) -> list[CheckState]:
    digests = check_digests(manifest)
    approved = store.approved()
    baselines = store.baselines()
    running: dict[str, list[str]] = {}
    for record in store.runs(limit=200):
        if record["state"] in ("queued", "running"):
            running.setdefault(record.get("job_id", record["key"]), []).append(
                record["run_id"]
            )
    states = []
    for check_id in sorted(manifest.checks):
        check = manifest.checks[check_id]
        inputs = snapshot.inputs(check.inputs)
        source = capture_working_tree(
            snapshot.project, [name for name, _ in inputs.files]
        )
        from .evidence import backend_identity

        try:
            backend = backend_identity(check)
        except (OSError, ValueError) as exc:
            backend = {"unavailable": str(exc)}
        key = seal_job(
            RegisteredCheck.resolve(
                manifest,
                check_id,
                baselines.get(check_id),
                backend,
                inputs.to_json(),
                source,
            ).payload()
        )["job_id"]
        current = store.evidence(check_id, key)
        last = store.latest(check_id)
        if (
            approved.get(digests[check_id], {}).get("kind") != "check_definition"
            or approved[digests[check_id]].get("backend") != backend
        ):
            freshness = Freshness.NOT_APPROVED
        elif current is not None:
            freshness = _FRESH[current["result"]]
        elif last is not None:
            freshness = Freshness.STALE
        else:
            freshness = Freshness.MISSING
        states.append(
            CheckState(
                check,
                digests[check_id],
                key,
                inputs,
                freshness,
                current,
                last,
                tuple(running.get(key, ())),
            )
        )
    return states


def formal_states(manifest: Manifest, store: Store, snapshot: Snapshot) -> list[FormalState]:
    approved = store.approved()
    running: dict[str, list[str]] = {}
    for record in store.runs(limit=200):
        if (record["state"] in ("queued", "running")
                and record.get("subject", {}).get("kind") == "formal_goal"):
            running.setdefault(record["job_id"], []).append(record["run_id"])
    states = []
    source = None
    for component in sorted(manifest.components.values(), key=lambda item: item.id):
        last = store.latest_for("formal_goal", component.id)
        key, current, detail = None, None, "formal policy and package are not accepted"
        freshness = Freshness.NOT_APPROVED
        if _formal_approval_matches(approved, component):
            try:
                identity = _resolve_execution(component)
                from .inputs import canonical_digest

                if _formal_approval_matches(approved, component, canonical_digest(identity["semantic"])):
                    if source is None:
                        source = capture_working_tree(snapshot.project)
                    request = VerificationRequest(component.id, WorkingTree())
                    key = seal_job(FormalGoal(request, component, identity, source).payload())["job_id"]
                    current = store.evidence_for("formal_goal", component.id, key)
                    freshness = (_FRESH[current["result"]] if current is not None else
                                 Freshness.STALE if last is not None else Freshness.MISSING)
                    detail = None
                else:
                    detail = "formal checker identity is not accepted"
            except (OSError, ValueError, RuntimeError, ProjectError) as exc:
                freshness, detail = Freshness.BLOCKED, str(exc)
        states.append(FormalState(component, key, freshness, current, last,
                                  tuple(running.get(key, ())), detail))
    return states


def needs_run(state: CheckState | FormalState) -> bool:
    if state.freshness not in (Freshness.STALE, Freshness.MISSING) or state.running:
        return False
    return not isinstance(state, FormalState) or state.key is not None


def gate(
    states: Iterable[CheckState], formal: Iterable[FormalState] = ()
) -> dict[str, Any]:
    states = list(states)
    formal = list(formal)
    required = [state for state in formal if state.required]
    if not states and not required:
        verdict, reason = Verdict.BLOCKED, "no checks are registered"
    elif any(
        state.freshness is Freshness.FRESH_FAIL
        for state in (*states, *required)
    ):
        verdict, reason = Verdict.REJECTED, "a required check failed against the current inputs"
    elif all(state.freshness is Freshness.FRESH_PASS for state in states) and all(
        state.freshness is Freshness.FRESH_PASS for state in required
    ):
        verdict, reason = Verdict.READY, "every required check passed against the current inputs"
    else:
        verdict, reason = (
            Verdict.BLOCKED,
            "some required checks have no passing evidence for the current inputs",
        )
    return {
        "verdict": verdict.value,
        "reason": reason,
        "checks": [
            {
                "id": state.check.id,
                "state": state.freshness.value,
                "run_id": (state.current or {}).get("run_id"),
            }
            for state in states
        ],
        "formal": [
            {
                "id": state.component.id,
                "state": state.freshness.value,
                "required": state.required,
                "run_id": (state.current or {}).get("run_id"),
            }
            for state in formal
        ],
    }

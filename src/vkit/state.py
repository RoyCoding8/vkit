"""What is known about each check right now, and whether the tree is ready."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Iterable

from . import __version__
from .inputs import InputSet, Snapshot, canonical_digest
from .manifest import Manifest
from .store import Store
from .verifiers.spec import CheckSpec


class Freshness(str, enum.Enum):
    FRESH_PASS = "fresh_pass"
    FRESH_FAIL = "fresh_fail"
    FRESH_BLOCKED = "fresh_blocked"
    STALE = "stale"
    MISSING = "missing"
    NOT_APPROVED = "not_approved"


class Verdict(str, enum.Enum):
    READY = "READY"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


_FRESH = {"PASS": Freshness.FRESH_PASS, "FAIL": Freshness.FRESH_FAIL, "BLOCKED": Freshness.FRESH_BLOCKED}


def check_digests(manifest: Manifest) -> dict[str, str]:
    return {check_id: manifest.digest(check_id) for check_id in manifest.checks}


def evidence_key(check_digest: str, inputs: InputSet, baseline: dict[str, Any] | None = None) -> str:
    return canonical_digest({"vkit": __version__, "check": check_digest, "inputs": inputs.digest,
                             "baseline": None if baseline is None else baseline["measurements"]})


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
        category = self.check.evidence_kind()
        return {
            "id": self.check.id,
            "description": self.check.description,
            "kind": self.check.kind.value,
            "category": category.value,
            "establishes": category.establishes,
            "does_not_establish": category.does_not_establish,
            "state": self.freshness.value,
            "key": self.key,
            "inputs": {"scope": self.inputs.scope, "declared": list(self.inputs.declared),
                       "unmatched": list(self.inputs.unmatched)},
            "current": self.current,
            "last": self.last,
            "running": list(self.running),
        }


def check_states(manifest: Manifest, store: Store, snapshot: Snapshot) -> list[CheckState]:
    digests = check_digests(manifest)
    approved = store.approved()
    baselines = store.baselines()
    running: dict[str, list[str]] = {}
    for record in store.runs(limit=200):
        if record["state"] == "running":
            running.setdefault(record["key"], []).append(record["run_id"])
    states = []
    for check_id in sorted(manifest.checks):
        check = manifest.checks[check_id]
        inputs = snapshot.inputs(check.inputs)
        key = evidence_key(digests[check_id], inputs, baselines.get(check_id))
        current = store.evidence(check_id, key)
        last = store.latest(check_id)
        if digests[check_id] not in approved:
            freshness = Freshness.NOT_APPROVED
        elif current is not None:
            freshness = _FRESH[current["result"]]
        elif last is not None:
            freshness = Freshness.STALE
        else:
            freshness = Freshness.MISSING
        states.append(CheckState(check, digests[check_id], key, inputs, freshness, current, last,
                                 tuple(running.get(key, ()))))
    return states


def needs_run(state: CheckState) -> bool:
    return state.freshness in (Freshness.STALE, Freshness.MISSING) and not state.running


def gate(states: Iterable[CheckState]) -> dict[str, Any]:
    states = list(states)
    if not states:
        verdict, reason = Verdict.BLOCKED, "no checks are registered"
    elif any(s.freshness is Freshness.FRESH_FAIL for s in states):
        verdict, reason = Verdict.REJECTED, "a check failed against the current inputs"
    elif all(s.freshness is Freshness.FRESH_PASS for s in states):
        verdict, reason = Verdict.READY, "every check passed against the current inputs"
    else:
        verdict, reason = Verdict.BLOCKED, "some checks have no passing evidence for the current inputs"
    return {
        "verdict": verdict.value,
        "reason": reason,
        "checks": [{"id": s.check.id, "state": s.freshness.value,
                    "run_id": (s.current or {}).get("run_id")} for s in states],
    }

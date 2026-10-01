"""The three things a check run can conclude, and why it concluded them.

Outcome is a sum type on purpose. A `result` string plus an optional `reason`
would admit PASS-with-a-reason and BLOCKED-with-no-reason, and CONTRACT.md
requires that a terminal run has exactly one outcome and that every BLOCKED
names its reason. Constructing the variants makes those states unrepresentable
rather than merely discouraged.

The non-empty scenario list is a different call. A check artifact that lists
zero scenarios cannot pass, and this module does not enforce that: `parse_artifact`
only decodes bytes, and the schema allows an empty `scenarios` array on purpose so
an empty report reads as ARTIFACT_EMPTY rather than as a malformed artifact.
`execution._scenarios_from_artifact` turns the empty list into that reason. The
rule lives there because only it knows the check's required scenarios.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class BlockedReason(str, enum.Enum):
    """Why execution or evidence was insufficient to decide.

    Every value here is a reason a run could not answer the question. Adding a
    value is the normal way to extend this; the derivation code is where each
    one is produced.
    """

    PREREQUISITE_MISSING = "prerequisite_missing"
    TOOL_MISSING = "tool_missing"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_MALFORMED = "artifact_malformed"
    ARTIFACT_EMPTY = "artifact_empty"
    SCENARIO_UNKNOWN = "scenario_unknown"
    SOURCE_CHANGED = "source_changed"
    OWNERSHIP_LOST = "ownership_lost"
    LAUNCH_FAILED = "launch_failed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ScenarioResult:
    """One observed behavior. `observation` is what a human would need to believe
    it, and is the difference between a FAIL and a shrug."""

    scenario_id: str
    passed: bool
    observation: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.scenario_id,
            "result": "PASS" if self.passed else "FAIL",
            "observation": self.observation,
        }


@dataclass(frozen=True)
class Passed:
    """Required observations succeeded under the recorded contract."""

    scenarios: tuple[ScenarioResult, ...]

    def to_json(self) -> dict[str, Any]:
        return {"result": "PASS", "scenarios": [s.to_json() for s in self.scenarios]}


@dataclass(frozen=True)
class Failed:
    """A valid check observed a product or required-policy failure."""

    scenarios: tuple[ScenarioResult, ...]

    def to_json(self) -> dict[str, Any]:
        return {"result": "FAIL", "scenarios": [s.to_json() for s in self.scenarios]}


@dataclass(frozen=True)
class Blocked:
    """Execution or evidence was insufficient to decide.

    `reason` is required, not optional. That is the whole point of this variant.
    `detail` is optional and human-facing; it never carries machine meaning.
    """

    reason: BlockedReason
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"result": "BLOCKED", "reason": self.reason.value}
        if self.detail:
            out["detail"] = self.detail
        return out


Outcome = Passed | Failed | Blocked

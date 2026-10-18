"""A run concludes Passed, Failed, or Blocked; Blocked always carries a reason, so PASS-with-reason
and BLOCKED-without-one cannot be built.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class BlockedReason(str, enum.Enum):
    """Why a run could not decide."""

    PREREQUISITE_MISSING = "prerequisite_missing"
    TOOL_MISSING = "tool_missing"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_MALFORMED = "artifact_malformed"
    ARTIFACT_EMPTY = "artifact_empty"
    SCENARIO_UNKNOWN = "scenario_unknown"
    SOURCE_CHANGED = "source_changed"
    NOT_APPROVED = "not_approved"
    INPUT_MISSING = "input_missing"
    LAUNCH_FAILED = "launch_failed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ScenarioResult:
    """One observed behavior and the evidence a reader needs to believe it."""

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
    """Every required observation succeeded."""

    scenarios: tuple[ScenarioResult, ...]

    def to_json(self) -> dict[str, Any]:
        return {"result": "PASS", "scenarios": [s.to_json() for s in self.scenarios]}


@dataclass(frozen=True)
class Failed:
    """A valid check observed a failure."""

    scenarios: tuple[ScenarioResult, ...]

    def to_json(self) -> dict[str, Any]:
        return {"result": "FAIL", "scenarios": [s.to_json() for s in self.scenarios]}


@dataclass(frozen=True)
class Blocked:
    """Execution or evidence was insufficient. `detail` is for humans only."""

    reason: BlockedReason
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"result": "BLOCKED", "reason": self.reason.value}
        if self.detail:
            out["detail"] = self.detail
        return out


Outcome = Passed | Failed | Blocked

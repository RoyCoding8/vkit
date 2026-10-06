"""The six check kinds. A parsed manifest entry is exactly one of these."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

from ..claimkind import ClaimCategory
from .obligation import CaseObligation, Obligation, PropertyObligation, TheoremObligation


class CheckKind(enum.StrEnum):
    SCENARIO = "scenario"
    PYTEST = "pytest"
    NODE_TEST = "node_test"
    PROPERTY = "property"
    LEAN = "lean"
    TLC = "tlc"


class LeanProfile(enum.StrEnum):
    UNREVIEWED = "unreviewed_agent"
    REVIEWED = "reviewed_proof_sources"


@dataclass(frozen=True)
class Prerequisite:
    name: str
    executable: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class PinnedRunner:
    executable: str
    base_argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class HypothesisSettings:
    max_examples: int
    stateful_step_count: int = 0
    deadline: float | None = None
    suppress_health_check: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplaySettings:
    database: str
    seed: int | None = None


@dataclass(frozen=True)
class ModuleRef:
    module: str
    path: str


@dataclass(frozen=True)
class ToolchainRef:
    tool: str
    version: str | None = None
    comparator: str | None = None
    jar_sha256: str | None = None


@dataclass(frozen=True)
class DomainBounds:
    bounds: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bounds", tuple(sorted(self.bounds)))

    @property
    def as_pairs(self) -> tuple[tuple[str, int], ...]:
        return self.bounds


@dataclass(frozen=True)
class FingerprintSpec:
    constants_from_config: bool
    checksum_states: bool
    workers: int


@dataclass(frozen=True)
class SubjectRef:
    paths: tuple[str, ...] = ()
    digest: str | None = None


@dataclass(frozen=True, kw_only=True)
class NativeCheckSpec:
    """Fields every kind has. `inputs` are the paths whose content the evidence is keyed on."""

    id: str
    cwd: Path
    timeout_seconds: float
    artifact_name: str
    subject: SubjectRef = SubjectRef()
    claim_id: str = ""
    description: str = ""
    prerequisites: tuple[Prerequisite, ...] = ()
    inputs: tuple[str, ...] = ()
    expectations: tuple[str, ...] | None = None

    @property
    def argv(self) -> tuple[str, ...]:
        return ()

    def evidence_kind(self) -> ClaimCategory:
        return evidence_kind(self)


@dataclass(frozen=True, kw_only=True)
class ScenarioCheck(NativeCheckSpec):
    """A driver the owner wrote, whose artifact names each scenario and what it observed."""

    command: tuple[str, ...]
    required_scenarios: tuple[CaseObligation, ...]
    kind: CheckKind = field(default=CheckKind.SCENARIO, init=False)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return self.required_scenarios


@dataclass(frozen=True, kw_only=True)
class _RunnerCheck(NativeCheckSpec):
    required_tests: tuple[str, ...]
    runner: PinnedRunner
    report_format: str
    expect_report_version: int

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(CaseObligation(test) for test in self.required_tests)


@dataclass(frozen=True, kw_only=True)
class PytestCheck(_RunnerCheck):
    kind: CheckKind = field(default=CheckKind.PYTEST, init=False)


@dataclass(frozen=True, kw_only=True)
class NodeTestCheck(_RunnerCheck):
    kind: CheckKind = field(default=CheckKind.NODE_TEST, init=False)


@dataclass(frozen=True, kw_only=True)
class PropertyCheck(_RunnerCheck):
    """A pytest run of Hypothesis tests under generator settings the owner pinned."""

    generator: HypothesisSettings
    replay: ReplaySettings | None = None
    kind: CheckKind = field(default=CheckKind.PROPERTY, init=False)


@dataclass(frozen=True, kw_only=True)
class LeanCheck(NativeCheckSpec):
    challenge: ModuleRef
    theorems: tuple[TheoremObligation, ...]
    profile: LeanProfile
    permitted_axioms: tuple[str, ...]
    toolchain: ToolchainRef
    kind: CheckKind = field(default=CheckKind.LEAN, init=False)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return self.theorems


@dataclass(frozen=True, kw_only=True)
class TlcCheck(NativeCheckSpec):
    model: ModuleRef
    config: str
    properties: tuple[PropertyObligation, ...]
    bounds: DomainBounds
    fingerprint: FingerprintSpec
    toolchain: ToolchainRef
    kind: CheckKind = field(default=CheckKind.TLC, init=False)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return self.properties


CheckSpec = ScenarioCheck | PytestCheck | NodeTestCheck | PropertyCheck | LeanCheck | TlcCheck

VARIANTS: dict[CheckKind, type] = {
    CheckKind.SCENARIO: ScenarioCheck, CheckKind.PYTEST: PytestCheck, CheckKind.NODE_TEST: NodeTestCheck,
    CheckKind.PROPERTY: PropertyCheck, CheckKind.LEAN: LeanCheck, CheckKind.TLC: TlcCheck,
}


def evidence_kind(check: NativeCheckSpec) -> ClaimCategory:
    """The category a PASS licenses, derived from the kind and never declared."""
    if isinstance(check, PropertyCheck):
        return ClaimCategory.PROPERTY
    if isinstance(check, LeanCheck):
        return ClaimCategory.THEOREM
    if isinstance(check, TlcCheck):
        return ClaimCategory.FINITE_MODEL
    if isinstance(check, (ScenarioCheck, PytestCheck, NodeTestCheck)):
        return ClaimCategory.SCENARIO
    raise TypeError(f"{type(check).__name__} is not a check variant")

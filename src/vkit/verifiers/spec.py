"""The six ways a check can be executed, as six types rather than one dict.

Every native check carries a `kind` and exactly the fields that kind has. A
`lean` variant has no `required_scenarios` field to be empty, because there is
no such field on it. A `scenario` variant has no `theorems` field, so it cannot
name a theorem. This is the same move `outcome.py` justifies in prose for the
verdict, applied to the declaration instead.

The eight fields that do not vary by kind (id, description, cwd, timeout,
artifact, prerequisites, inputs, expectations) live once on the base, not six
times on the variants. Six copies of the timeout rule is six places to forget
one.

`evidence_kind` is a function of the variant, never a field anybody sets. A
declared category is a label the candidate controls, and Plan 10 forbids a
candidate-controlled JSON document promoting evidence to a stronger category.
Making it a field would build the thing the plan refuses.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

from ..claimkind import ClaimCategory
from .obligation import (
    CaseObligation,
    Obligation,
    PropertyObligation,
    TheoremObligation,
)


class CheckKind(enum.StrEnum):
    """Who interprets the output. A different question from what a green result
    licenses, which is `evidence_kind`."""

    SCENARIO = "scenario"
    PYTEST = "pytest"
    NODE_TEST = "node_test"
    PROPERTY = "property"
    LEAN = "lean"
    TLC = "tlc"


@dataclass(frozen=True)
class PinnedRunner:
    """How vkit launches a test runner. An argument array, never a shell string."""

    executable: str
    base_argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class HypothesisSettings:
    """The generator settings vkit pinned. Required for a property result."""

    max_examples: int
    stateful_step_count: int = 0
    deadline: float | None = None
    suppress_health_check: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplaySettings:
    """Where a failing sequence is retained."""

    database: str
    seed: int | None = None


@dataclass(frozen=True)
class ModuleRef:
    """A named module and the file it lives in. The name is never derived from
    the path, because an adapter that guessed it could report the wrong one."""

    module: str
    path: str


@dataclass(frozen=True)
class LeanProfile:
    """How much of the proof environment vkit trusts.

    `REVIEWED` trusts reviewed source files and rechecks them with the kernel.
    `UNREVIEWED` requires an isolated challenge/solution comparator; it is
    BLOCKED until that capability is implemented and measured.
    """

    UNREVIEWED = "unreviewed_agent"
    REVIEWED = "reviewed_proof_sources"


@dataclass(frozen=True)
class ToolchainRef:
    """The tool a formal check is pinned to. A null here is 'not yet
    determined', which is honest; no argv is derived from it until the
    comparator contract is confirmed."""

    tool: str
    version: str | None = None
    comparator: str | None = None
    jar_sha256: str | None = None


@dataclass(frozen=True)
class DomainBounds:
    """Named model bounds, as the sorted pairs an obligation carries."""

    bounds: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bounds", tuple(sorted(self.bounds)))

    @property
    def as_pairs(self) -> tuple[tuple[str, int], ...]:
        return self.bounds


@dataclass(frozen=True)
class FingerprintSpec:
    """How the state fingerprint is computed. `workers` is required: with
    `-workers auto` each worker prints its own summary and they interleave, so
    the first match is whichever finished first rather than the run total."""

    constants_from_config: bool
    checksum_states: bool
    workers: int


@dataclass(frozen=True)
class SubjectRef:
    """What the check is about. `digest` is null when the owner has declared no
    expected digest, which is the honest direction; vkit measures it and the
    receipt carries the measurement."""

    paths: tuple[str, ...] = ()
    digest: str | None = None




@dataclass(frozen=True)
class NativeCheckSpec:
    """What every check has, regardless of which interpreter runs it.

    The eight shared fields and nothing else. `id` and `claim_id` are separate
    because renaming a check must not silently rename the claim it supports.

    Every field is keyword-only. The variants redeclare `kind` with a default,
    and a positional dataclass would then put a defaulted field ahead of a
    non-default one from the base, which is a construction error rather than a
    default anyone can use positionally. Nothing here is constructed
    positionally anyway.
    """

    id: str = field(kw_only=True)
    kind: CheckKind = field(kw_only=True)
    subject: SubjectRef = field(kw_only=True)
    claim_id: str = field(kw_only=True)
    cwd: Path = field(kw_only=True)
    timeout_seconds: float = field(kw_only=True)
    artifact_name: str = field(kw_only=True)
    description: str = field(default="", kw_only=True)
    prerequisites: tuple = field(default=(), kw_only=True)
    inputs: tuple[str, ...] = field(default=(), kw_only=True)
    expectations: tuple[str, ...] | None = field(default=None, kw_only=True)

    @property
    def argv(self) -> tuple[str, ...]:
        """The raw, unexpanded command. Only a `scenario` check has one; the
        others are launched by vkit from their own fields. An empty tuple is the
        honest answer for a variant that declares no command."""
        return ()


@dataclass(frozen=True)
class ScenarioCheck(NativeCheckSpec):
    """An approved driver process wrote `check-artifact.v1.json` bytes and vkit's
    parser accepted them.

    The only variant with a `command`, and the only one that can name a scenario
    id. It has no theorem field and no evidence_kind field, which is what makes
    a legacy driver claiming `theorem_checking` structurally impossible rather
    than merely checked.
    """

    kind: CheckKind = field(default=CheckKind.SCENARIO, kw_only=True)
    command: tuple[str, ...] = field(default=(), kw_only=True)
    required_scenarios: tuple[CaseObligation, ...] = field(default=(), kw_only=True)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return self.required_scenarios


@dataclass(frozen=True)
class PytestCheck(NativeCheckSpec):
    """vkit built the argv, ran the pinned runner, and parsed a structured
    report it alone produced.

    No `generator` field. Without one a pytest check cannot be read as property
    evidence no matter what the runner printed, which is the whole difference
    `claimkind.py` draws.
    """

    kind: CheckKind = CheckKind.PYTEST
    required_tests: tuple[str, ...] = field(default=(), kw_only=True)
    runner: PinnedRunner = field(default_factory=lambda: PinnedRunner(""), kw_only=True)
    report_format: str = field(default="pytest_json_report", kw_only=True)
    expect_report_version: int = field(default=1, kw_only=True)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(CaseObligation(test) for test in self.required_tests)


@dataclass(frozen=True)
class NodeTestCheck(NativeCheckSpec):
    """The same, for the Node built-in runner."""

    kind: CheckKind = CheckKind.NODE_TEST
    required_tests: tuple[str, ...] = field(default=(), kw_only=True)
    runner: PinnedRunner = field(default_factory=lambda: PinnedRunner(""), kw_only=True)
    report_format: str = field(default="node_tap", kw_only=True)
    expect_report_version: int = field(default=1, kw_only=True)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(CaseObligation(test) for test in self.required_tests)


@dataclass(frozen=True)
class PropertyCheck(NativeCheckSpec):
    """A pytest run whose required tests are Hypothesis tests, with the
    generator settings vkit pinned.

    `generator` is required rather than optional. A property result whose
    settings were not pinned says nothing about what was sampled, which is the
    whole difference between PROPERTY and SCENARIO. The settings come from the
    manifest, reviewed by a person, and never from a package being importable in
    the child.
    """

    kind: CheckKind = CheckKind.PROPERTY
    required_tests: tuple[str, ...] = field(default=(), kw_only=True)
    runner: PinnedRunner = field(default_factory=lambda: PinnedRunner(""), kw_only=True)
    report_format: str = field(default="pytest_json_report", kw_only=True)
    expect_report_version: int = field(default=1, kw_only=True)
    generator: HypothesisSettings | None = field(default=None, kw_only=True)
    replay: ReplaySettings | None = field(default=None, kw_only=True)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(CaseObligation(test) for test in self.required_tests)


@dataclass(frozen=True)
class LeanCheck(NativeCheckSpec):
    """A kernel or comparator run over a pinned challenge, with named theorems
    and an audited axiom set.

    No `required_scenarios`, and `theorems` and `profile` are both required, so
    a lean check cannot be constructed that asserts nothing or that declined to
    say which proof environment it ran in.
    """

    kind: CheckKind = CheckKind.LEAN
    challenge: ModuleRef = field(default_factory=lambda: ModuleRef("", ""), kw_only=True)
    theorems: tuple[TheoremObligation, ...] = field(default=(), kw_only=True)
    profile: str = field(default=LeanProfile.UNREVIEWED, kw_only=True)
    permitted_axioms: tuple[str, ...] = field(default=(), kw_only=True)
    toolchain: ToolchainRef = field(default_factory=lambda: ToolchainRef("lean"), kw_only=True)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return self.theorems


@dataclass(frozen=True)
class TlcCheck(NativeCheckSpec):
    """Exhaustive exploration of one finite model at one recorded
    configuration.

    `bounds` and `fingerprint` are both required: a finite model result at an
    unrecorded configuration is the specific overstatement `claimkind.py`
    refuses.
    """

    kind: CheckKind = CheckKind.TLC
    model: ModuleRef = field(default_factory=lambda: ModuleRef("", ""), kw_only=True)
    config: str = field(default="", kw_only=True)
    properties: tuple[PropertyObligation, ...] = field(default=(), kw_only=True)
    bounds: DomainBounds = field(default_factory=DomainBounds, kw_only=True)
    fingerprint: FingerprintSpec = field(default_factory=lambda: FingerprintSpec(True, False, 1), kw_only=True)
    toolchain: ToolchainRef = field(default_factory=lambda: ToolchainRef("tlc"), kw_only=True)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return self.properties


CheckSpec = (
    ScenarioCheck | PytestCheck | NodeTestCheck | PropertyCheck | LeanCheck | TlcCheck
)


VARIANTS: dict[CheckKind, type] = {
    CheckKind.SCENARIO: ScenarioCheck,
    CheckKind.PYTEST: PytestCheck,
    CheckKind.NODE_TEST: NodeTestCheck,
    CheckKind.PROPERTY: PropertyCheck,
    CheckKind.LEAN: LeanCheck,
    CheckKind.TLC: TlcCheck,
}


def evidence_kind(check: NativeCheckSpec) -> ClaimCategory:
    """The category a PASS from this check licenses. Derived, never declared.

    Every branch maps one variant to exactly one category, so a check cannot
    claim a category its interpreter cannot produce. A `pytest` variant with no
    generator block is SCENARIO no matter what the runner printed, which is the
    whole difference `claimkind.py` draws at lines 17-22.

    This function is total over the union and has no fallback branch. A caller
    holding something that is not a `NativeCheckSpec` has a bug upstream, and
    raising here is how it surfaces rather than being papered over with a
    default category.
    """
    if isinstance(check, PropertyCheck):
        return ClaimCategory.PROPERTY
    if isinstance(check, LeanCheck):
        return ClaimCategory.THEOREM
    if isinstance(check, TlcCheck):
        return ClaimCategory.FINITE_MODEL
    if isinstance(check, (ScenarioCheck, PytestCheck, NodeTestCheck)):
        return ClaimCategory.SCENARIO
    raise TypeError(
        f"{type(check).__name__} is not a check variant, so it licenses no "
        "evidence category. Every check is one of the six in VARIANTS."
    )

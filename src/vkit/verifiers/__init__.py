"""What kind of check ran, and what that kind is required to establish.

`spec` owns the six check variants and `evidence_kind`, the total function that
maps one to a category. `obligation` owns the union of things a check can be
required to prove, which is separate because a scenario id, a theorem and a
model property are three different names a receipt has to match.

Both are imported as `from vkit.verifiers import ...`; nothing reaches into the
submodules directly, so the split between them stays a fact about this package
rather than a path a caller has to know.
"""

from .obligation import (
    CaseObligation,
    Obligation,
    PropertyObligation,
    TheoremObligation,
    describe as describe_obligation,
    obligation_from_json,
    obligation_to_json,
    obligations_from_scenarios,
)
from .spec import (
    VARIANTS,
    CheckKind,
    CheckSpec,
    DomainBounds,
    FingerprintSpec,
    HypothesisSettings,
    LeanCheck,
    LeanProfile,
    ModuleRef,
    NativeCheckSpec,
    NodeTestCheck,
    PinnedRunner,
    PropertyCheck,
    PytestCheck,
    ReplaySettings,
    ScenarioCheck,
    SubjectRef,
    TlcCheck,
    ToolchainRef,
    evidence_kind,
)

__all__ = [
    "VARIANTS",
    "CaseObligation",
    "CheckKind",
    "CheckSpec",
    "DomainBounds",
    "FingerprintSpec",
    "HypothesisSettings",
    "LeanCheck",
    "LeanProfile",
    "ModuleRef",
    "NativeCheckSpec",
    "NodeTestCheck",
    "Obligation",
    "PinnedRunner",
    "PropertyCheck",
    "PropertyObligation",
    "PytestCheck",
    "ReplaySettings",
    "ScenarioCheck",
    "SubjectRef",
    "TheoremObligation",
    "TlcCheck",
    "ToolchainRef",
    "describe_obligation",
    "evidence_kind",
    "obligation_from_json",
    "obligation_to_json",
    "obligations_from_scenarios",
]

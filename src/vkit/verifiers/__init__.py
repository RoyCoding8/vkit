"""What kind of check ran, what it must establish, and who read its output.

`spec` owns the six check variants and `evidence_kind`, the total function that
maps one to a category. `obligation` owns the union of things a check can be
required to prove, which is separate because a scenario id, a theorem and a
model property are three different names a receipt has to match.

`dispatch` is the one place a kind is turned into an argv and read back as
evidence, and `pytest_adapter` is the interpreter for the pytest kind. They are
listed last because `dispatch` reads `spec` and `obligation`, and `execution`
reads `dispatch`; the scenario branch reaches back into `execution` through a
deferred import rather than a module-level one, because a package-level cycle
resolved by import order is invisible until the order changes.

All three are imported as `from vkit.verifiers import ...`; nothing reaches into
the submodules directly, so the split between them stays a fact about this
package rather than a path a caller has to know.
"""

from .dispatch import (
    ADAPTERS,
    HASH_LIMIT,
    NORMALIZATION,
    ORACLE_REVIEW,
    VERIFIER_MODULE,
    Adapter,
    ReceiptInputs,
    ScenarioReading,
    argv_for,
    build_receipt,
    for_kind,
    interpret,
    outcome_from_reading,
    persist,
    validate_receipt,
)
from .lean_adapter import AdapterResult as LeanAdapterResult
from .node_adapter import AdapterResult as NodeAdapterResult
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
from .property_adapter import PropertyAdapterResult, TestedScope
from .pytest_adapter import AdapterResult
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
from .tlc_adapter import AdapterResult as TlcAdapterResult

__all__ = [
    "ADAPTERS",
    "HASH_LIMIT",
    "NORMALIZATION",
    "ORACLE_REVIEW",
    "VARIANTS",
    "VERIFIER_MODULE",
    "Adapter",
    "AdapterResult",
    "CaseObligation",
    "CheckKind",
    "CheckSpec",
    "DomainBounds",
    "FingerprintSpec",
    "HypothesisSettings",
    "LeanAdapterResult",
    "LeanCheck",
    "LeanProfile",
    "ModuleRef",
    "NativeCheckSpec",
    "NodeAdapterResult",
    "NodeTestCheck",
    "Obligation",
    "PinnedRunner",
    "PropertyAdapterResult",
    "PropertyCheck",
    "PropertyObligation",
    "PytestCheck",
    "ReceiptInputs",
    "ReplaySettings",
    "ScenarioCheck",
    "ScenarioReading",
    "SubjectRef",
    "TestedScope",
    "TheoremObligation",
    "TlcAdapterResult",
    "TlcCheck",
    "ToolchainRef",
    "argv_for",
    "build_receipt",
    "describe_obligation",
    "evidence_kind",
    "for_kind",
    "interpret",
    "obligation_from_json",
    "obligation_to_json",
    "obligations_from_scenarios",
    "outcome_from_reading",
    "persist",
    "validate_receipt",
]

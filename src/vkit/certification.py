from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from .inputs import canonical_digest
from .schemas import validate

_SCHEMA = "change-certification.v1.json"
_INTENT_REVISION_SCHEMA = "intent-revision.v1.json"


class ComparisonKind(StrEnum):
    EQUIVALENT = "equivalent"
    STRENGTHENS = "strengthens"
    WEAKENS = "weakens"
    INCOMPARABLE = "incomparable"
    VACUOUS = "vacuous"
    UNKNOWN = "unknown"


class EntailmentKind(StrEnum):
    ENTAILED = "entailed"
    NOT_ENTAILED = "not_entailed"
    VACUOUS = "vacuous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FiniteDomain:
    binding: str
    inputs: tuple[str, ...]
    observations: tuple[str, ...]
    codec_sha256: str
    observation_sha256: str
    environment_sha256: str

    def to_json(self) -> dict[str, Any]:
        return {"binding": self.binding, "inputs": sorted(self.inputs),
                "observations": sorted(self.observations), "codec_sha256": self.codec_sha256,
                "observation_sha256": self.observation_sha256,
                "environment_sha256": self.environment_sha256}

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())


@dataclass(frozen=True)
class OutputRow:
    input: str
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class FiniteContract:
    domain: FiniteDomain
    admissible_inputs: tuple[str, ...]
    rows: tuple[OutputRow, ...]

    def to_json(self) -> dict[str, Any]:
        return {"domain": self.domain.to_json(),
                "admissible_inputs": sorted(self.admissible_inputs),
                "allowed_outputs": [
                    {"input": row.input, "outputs": sorted(row.outputs)}
                    for row in sorted(self.rows, key=lambda row: row.input)]}

    def outputs_for(self, input_token: str) -> frozenset[str]:
        for row in self.rows:
            if row.input == input_token:
                return frozenset(row.outputs)
        return frozenset()


@dataclass(frozen=True)
class SemanticArtifact:
    role: Literal["binding", "codec", "observation", "environment"]
    path: str
    sha256: str

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class IntentRevision:
    requirement_source: str
    requirement_statement: str
    contract: FiniteContract
    artifacts: tuple[SemanticArtifact, ...]

    @classmethod
    def from_json(cls, raw: Any) -> IntentRevision:
        validate("intent revision", _INTENT_REVISION_SCHEMA, raw)
        contract = _finite_contract_from_validated_json(raw["contract"])
        artifacts = tuple(
            SemanticArtifact(role, raw["artifacts"][role]["path"],
                             raw["artifacts"][role]["sha256"])
            for role in ("binding", "codec", "observation", "environment")
        )
        by_role = {artifact.role: artifact for artifact in artifacts}
        domain = contract.domain
        if (by_role["codec"].sha256 != domain.codec_sha256
                or by_role["observation"].sha256 != domain.observation_sha256
                or by_role["environment"].sha256 != domain.environment_sha256):
            raise ValueError("semantic artifact hashes must match the finite domain")
        return cls(raw["requirement"]["source"], raw["requirement"]["statement"],
                   contract, artifacts)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "requirement": {"source": self.requirement_source,
                            "statement": self.requirement_statement},
            "contract": self.contract.to_json(),
            "artifacts": {artifact.role: artifact.to_json()
                          for artifact in self.artifacts},
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())


@dataclass(frozen=True)
class ChangeCertificationRequest:
    base_oid: str
    candidate_oid: str
    declared_intent_sha256: str
    claim: FiniteContract

    @classmethod
    def from_json(cls, raw: Any) -> ChangeCertificationRequest:
        validate("change certification request", _SCHEMA, raw)
        if len(raw["base_oid"]) != len(raw["candidate_oid"]):
            raise ValueError("base and candidate Git object IDs must use the same object format")
        claim = raw["claim"]
        return cls(raw["base_oid"], raw["candidate_oid"], raw["intent_sha256"],
                   _finite_contract_from_validated_json(claim["contract"]))

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": 1, "base_oid": self.base_oid,
                "candidate_oid": self.candidate_oid,
                "intent_sha256": self.declared_intent_sha256,
                "claim": {"kind": "finite_contract", "contract": self.claim.to_json()}}


@dataclass(frozen=True)
class RefinementCheck:
    precondition_included: bool
    postcondition_included: bool
    input_witness: str | None
    output_witness: tuple[str, str] | None

    @property
    def holds(self) -> bool:
        return self.precondition_included and self.postcondition_included


@dataclass(frozen=True)
class ContractComparison:
    kind: ComparisonKind
    old_to_new: RefinementCheck | None
    new_to_old: RefinementCheck | None


@dataclass(frozen=True)
class IntentEntailment:
    kind: EntailmentKind
    inputs_included: bool | None
    outputs_included: bool | None
    input_witness: str | None
    output_witness: tuple[str, str] | None


def _validate_contract(domain: FiniteDomain, inputs: tuple[str, ...],
                       rows: tuple[OutputRow, ...]) -> None:
    if len(set(inputs)) != len(inputs):
        raise ValueError("admissible_inputs contains duplicate tokens")
    if not set(inputs) <= set(domain.inputs):
        raise ValueError("admissible_inputs contains a token outside the domain")
    row_inputs = tuple(row.input for row in rows)
    if len(set(row_inputs)) != len(row_inputs):
        raise ValueError("allowed_outputs contains duplicate input rows")
    if set(row_inputs) != set(inputs):
        raise ValueError("allowed_outputs must have exactly one row for every admissible input")
    for row in rows:
        if not row.outputs:
            raise ValueError(f"allowed_outputs for {row.input!r} must be nonempty")
        if len(set(row.outputs)) != len(row.outputs):
            raise ValueError(f"allowed_outputs for {row.input!r} contains duplicate tokens")
        if not set(row.outputs) <= set(domain.observations):
            raise ValueError(f"allowed_outputs for {row.input!r} contains a token outside the domain")


def _finite_domain_from_validated_json(raw: dict[str, Any]) -> FiniteDomain:
    return FiniteDomain(raw["binding"], tuple(raw["inputs"]), tuple(raw["observations"]),
                        raw["codec_sha256"], raw["observation_sha256"],
                        raw["environment_sha256"])


def _finite_contract_from_validated_json(raw: dict[str, Any]) -> FiniteContract:
    domain = _finite_domain_from_validated_json(raw["domain"])
    inputs = tuple(raw["admissible_inputs"])
    rows = tuple(OutputRow(row["input"], tuple(row["outputs"]))
                 for row in raw["allowed_outputs"])
    _validate_contract(domain, inputs, rows)
    return FiniteContract(domain, inputs, rows)


def _refines(old: FiniteContract, new: FiniteContract) -> RefinementCheck:
    input_witness = next((token for token in sorted(old.admissible_inputs)
                          if token not in new.admissible_inputs), None)
    post_witness = None
    for token in sorted(old.admissible_inputs):
        for output in sorted(new.outputs_for(token) - old.outputs_for(token)):
            post_witness = (token, output)
            break
        if post_witness is not None:
            break
    return RefinementCheck(input_witness is None, post_witness is None,
                           input_witness, post_witness)


def compare_contracts(old: FiniteContract, new: FiniteContract) -> ContractComparison:
    """An empty legal set makes comparison inconclusive.

    Directional checks remain available for inspection.
    """
    if old.domain.digest != new.domain.digest:
        return ContractComparison(ComparisonKind.UNKNOWN, None, None)
    old_to_new = _refines(old, new)
    new_to_old = _refines(new, old)
    if not old.admissible_inputs or not new.admissible_inputs:
        kind = ComparisonKind.VACUOUS
    elif old_to_new.holds and new_to_old.holds:
        kind = ComparisonKind.EQUIVALENT
    elif old_to_new.holds:
        kind = ComparisonKind.STRENGTHENS
    elif new_to_old.holds:
        kind = ComparisonKind.WEAKENS
    else:
        kind = ComparisonKind.INCOMPARABLE
    return ContractComparison(kind, old_to_new, new_to_old)


def intent_entails_claim(intent: FiniteContract, claim: FiniteContract) -> IntentEntailment:
    if intent.domain.digest != claim.domain.digest:
        return IntentEntailment(EntailmentKind.UNKNOWN, None, None, None, None)
    input_witness = next((token for token in sorted(intent.admissible_inputs)
                          if token not in claim.admissible_inputs), None)
    output_witness = None
    for token in sorted(intent.admissible_inputs):
        if token not in claim.admissible_inputs:
            continue
        for output in sorted(intent.outputs_for(token) - claim.outputs_for(token)):
            output_witness = (token, output)
            break
        if output_witness is not None:
            break
    inputs_included = input_witness is None
    outputs_included = output_witness is None
    if not intent.admissible_inputs:
        kind = EntailmentKind.VACUOUS
    elif inputs_included and outputs_included:
        kind = EntailmentKind.ENTAILED
    else:
        kind = EntailmentKind.NOT_ENTAILED
    return IntentEntailment(kind, inputs_included, outputs_included,
                            input_witness, output_witness)

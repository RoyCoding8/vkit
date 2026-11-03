from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from .inputs import canonical_digest
from .schemas import validate

_SCHEMA = "change-certification.v1.json"
_INTENT_REVISION_SCHEMA = "intent-revision.v1.json"
_DIFFERENCE_POLICY_SCHEMA = "finite-differences.v1.json"


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
class DifferenceGuard:
    id: str
    requirements: tuple[OutputRow, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirements": [
                {"input": row.input, "outputs": sorted(row.outputs)}
                for row in sorted(self.requirements, key=lambda item: item.input)
            ],
        }


@dataclass(frozen=True)
class DifferencePolicy:
    intent_sha256: str
    guards: tuple[DifferenceGuard, ...]

    @classmethod
    def from_json(cls, raw: Any, *, intent: IntentRevision) -> DifferencePolicy:
        validate("finite difference policy", _DIFFERENCE_POLICY_SCHEMA, raw)
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ValueError("finite difference policy schema_version must be integer 1")
        if raw["intent_sha256"] != intent.digest:
            raise ValueError("finite difference policy names a different intent revision")

        contract = intent.contract
        admissible = set(contract.admissible_inputs)
        guards = []
        guard_ids = set()
        intersections: dict[str, set[str]] = {}
        for raw_guard in raw["guards"]:
            guard_id = raw_guard["id"]
            if guard_id in guard_ids:
                raise ValueError(f"duplicate difference guard id: {guard_id}")
            guard_ids.add(guard_id)
            rows = tuple(
                OutputRow(item["input"], tuple(item["outputs"]))
                for item in raw_guard["requirements"]
            )
            row_inputs = [row.input for row in rows]
            if len(set(row_inputs)) != len(row_inputs):
                raise ValueError(f"difference guard {guard_id!r} contains duplicate input rows")
            for row in rows:
                if row.input not in admissible:
                    raise ValueError(
                        f"difference guard {guard_id!r} expands the accepted input domain"
                    )
                standing_outputs = contract.outputs_for(row.input)
                if not set(row.outputs) <= standing_outputs:
                    raise ValueError(
                        f"difference guard {guard_id!r} weakens standing target intent for {row.input!r}"
                    )
                guard_outputs = set(row.outputs)
                previous = intersections.get(row.input)
                intersection = guard_outputs if previous is None else previous & guard_outputs
                if not intersection:
                    raise ValueError(
                        f"overlapping difference guards conflict for {row.input!r}"
                    )
                intersections[row.input] = intersection
            guards.append(DifferenceGuard(guard_id, rows))
        return cls(raw["intent_sha256"], tuple(sorted(guards, key=lambda guard: guard.id)))

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "finite_differences",
            "intent_sha256": self.intent_sha256,
            "guards": [guard.to_json() for guard in sorted(self.guards, key=lambda item: item.id)],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())


@dataclass(frozen=True)
class PairedObservation:
    input: str
    base_values: tuple[str, ...]
    candidate_values: tuple[str, ...]
    failure: str | None = None
    selected: bool = True


DifferenceKind = Literal[
    "preserved",
    "guarded_requirement_met",
    "standing_intent_repair",
    "violation",
    "unknown",
]
DifferenceStatus = Literal["observed_conformance", "observed_violation", "unknown"]


@dataclass(frozen=True)
class InputDifference:
    input: str
    kind: DifferenceKind
    guard_ids: tuple[str, ...]
    base_output: str | None
    candidate_output: str | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "kind": self.kind,
            "guard_ids": list(self.guard_ids),
            "base_output": self.base_output,
            "candidate_output": self.candidate_output,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DifferenceAssessment:
    status: DifferenceStatus
    coverage_complete: bool
    findings: tuple[InputDifference, ...]
    missing_inputs: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "scope": "finite_paired_observations",
            "source_proof": False,
            "approval_authority": False,
            "status": self.status,
            "coverage_complete": self.coverage_complete,
            "findings": [finding.to_json() for finding in self.findings],
            "missing_inputs": list(self.missing_inputs),
        }


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


def assess_differences(
    intent: IntentRevision,
    policy: DifferencePolicy,
    observations: tuple[PairedObservation, ...],
    *,
    repetitions: int,
) -> DifferenceAssessment:
    """Check finite paired observations against target requirements and guards."""
    if policy.intent_sha256 != intent.digest:
        raise ValueError("difference policy does not match the target intent revision")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")

    contract = intent.contract
    admissible = tuple(sorted(contract.admissible_inputs))
    admitted = set(admissible)
    by_input: dict[str, list[PairedObservation]] = {}
    for observation in observations:
        if observation.input in admitted:
            by_input.setdefault(observation.input, []).append(observation)

    guards_by_input: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for guard in policy.guards:
        for row in guard.requirements:
            guards_by_input.setdefault(row.input, []).append(
                (guard.id, frozenset(row.outputs))
            )

    findings = []
    missing = []
    observation_model = set(contract.domain.observations)
    for token in admissible:
        guard_requirements = guards_by_input.get(token, [])
        guard_ids = tuple(sorted(guard_id for guard_id, _ in guard_requirements))
        standing_outputs = contract.outputs_for(token)
        rows = by_input.get(token, [])
        selected_rows = [row for row in rows if row.selected]
        nonselected_rows = [row for row in rows if not row.selected]

        if len(selected_rows) != 1:
            reason = "missing_observation" if not selected_rows else "duplicate_observations"
            missing.append(token)
            findings.append(InputDifference(token, "unknown", guard_ids, None, None, reason))

        observations_to_check = [*selected_rows, *nonselected_rows]
        for observation in observations_to_check:
            base_output = None
            candidate_output = None
            reason = None
            if observation.failure is not None:
                reason = observation.failure or "paired_execution_failed"
            elif (len(observation.base_values) != repetitions
                  or len(observation.candidate_values) != repetitions):
                reason = "wrong_repetition_count"
            elif len(set(observation.base_values)) != 1:
                reason = "unstable_base_observations"
            elif len(set(observation.candidate_values)) != 1:
                reason = "unstable_candidate_observations"
            else:
                base_output = observation.base_values[0]
                candidate_output = observation.candidate_values[0]
                if (base_output not in observation_model
                        or candidate_output not in observation_model):
                    reason = "out_of_model_observation"

            if reason is not None:
                if observation.selected and len(selected_rows) == 1:
                    missing.append(token)
                    findings.append(InputDifference(
                        token, "unknown", guard_ids, base_output, candidate_output, reason
                    ))
                elif not observation.selected:
                    findings.append(InputDifference(
                        token, "unknown", guard_ids, base_output, candidate_output, reason
                    ))
                continue

            finding = _classify_difference(
                token,
                base_output,
                candidate_output,
                standing_outputs,
                guard_requirements,
                guard_ids,
            )
            if observation.selected and len(selected_rows) == 1:
                findings.append(finding)
            elif finding.kind == "violation":
                findings.append(finding)

    coverage_complete = bool(admissible) and not missing
    if any(finding.kind == "violation" for finding in findings):
        status: DifferenceStatus = "observed_violation"
    elif coverage_complete and not any(finding.kind == "unknown" for finding in findings):
        status = "observed_conformance"
    else:
        status = "unknown"
    return DifferenceAssessment(status, coverage_complete, tuple(findings), tuple(missing))


def _classify_difference(
    token: str,
    base_output: str,
    candidate_output: str,
    standing_outputs: frozenset[str],
    guard_requirements: list[tuple[str, frozenset[str]]],
    guard_ids: tuple[str, ...],
) -> InputDifference:
    if candidate_output not in standing_outputs:
        return InputDifference(
            token, "violation", guard_ids, base_output, candidate_output,
            "candidate violates standing target intent",
        )

    if guard_requirements:
        permitted = set(guard_requirements[0][1])
        for _, outputs in guard_requirements[1:]:
            permitted.intersection_update(outputs)
        if candidate_output not in permitted:
            return InputDifference(
                token, "violation", guard_ids, base_output, candidate_output,
                "candidate violates an overlapping guard requirement",
            )
        return InputDifference(
            token, "guarded_requirement_met", guard_ids, base_output, candidate_output,
            "candidate meets the intersected guard requirements",
        )

    if base_output == candidate_output:
        return InputDifference(
            token, "preserved", (), base_output, candidate_output,
            "base and candidate observations are equal",
        )
    if base_output not in standing_outputs:
        return InputDifference(
            token, "standing_intent_repair", (), base_output, candidate_output,
            "candidate repairs a base result that violates standing target intent",
        )
    return InputDifference(
        token, "violation", (), base_output, candidate_output,
        "unguarded difference is not a standing-intent repair",
    )

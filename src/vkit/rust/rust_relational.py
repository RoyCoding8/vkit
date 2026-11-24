from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from vkit.change.certification import DelegationDerivation, DifferencePolicy
from vkit.change.analysis import _policy_for_captures
from vkit.core.inputs import CapturedSource, canonical_digest
from vkit.change.intent import BoundChangeRequest, _prepare_change_request
from vkit.change.paired import _json_value, _semantic_files, _u32_token, _validate_domain
from vkit.change.paired_profiles import U32Binding, parse_binding, parse_environment
from vkit.runtime.paths import Project
from vkit.rust.rust_source import RustSourceProfile, UnsupportedRustSource, admit_rust_source

_MAX_HARNESS_BYTES = 256 * 1024
_MAX_GUARD_REQUIREMENTS = 4096
_OBLIGATIONS = (
    "base_observation_model",
    "candidate_observation_model",
    "candidate_standing_intent",
    "relational_policy",
)
_CLAIM_SATISFACTION = "candidate_claim_satisfaction"
_CLAIM_NEAR_MISS = "near_miss_control"
_KANI_MODEL_SCOPE = "restricted_captured_rust_kani_model"


@dataclass(frozen=True)
class RustRelationalProgram:
    bound: BoundChangeRequest
    base: CapturedSource
    candidate: CapturedSource
    policy: DifferencePolicy
    policy_identity: tuple[tuple[str, str | bool | None], ...]
    policy_bytes: bytes
    binding: U32Binding
    base_profile: RustSourceProfile
    candidate_profile: RustSourceProfile
    harness: bytes

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scope": "rust_relational_obligation",
            "executed": False,
            "engine_toolchain_bound": False,
            "native_environment_validated": False,
            "source_proof": False,
            "approval_authority": False,
            "bound_request_sha256": self.bound.digest,
            "base_capture_sha256": self.base.digest,
            "candidate_capture_sha256": self.candidate.digest,
            "intent_sha256": self.bound.intent.digest,
            "policy": dict(self.policy_identity),
            "binding": {"language": self.binding.language, "source": self.binding.source,
                        "symbol": self.binding.symbol},
            "source_profiles": {"base": self.base_profile.to_json(),
                                "candidate": self.candidate_profile.to_json()},
            "harness_sha256": hashlib.sha256(self.harness).hexdigest(),
            "harness_name": "vkit_relational",
            "required_assertions": list(_OBLIGATIONS),
            "admissible_inputs": sorted(self.bound.intent.contract.admissible_inputs, key=int),
            "assumptions": [
                "Restricted captured Rust source under the selected Kani compiler model.",
                "Every safety and overflow check must pass in addition to the named assertions.",
                "Native compiler correspondence and approval authority are not established.",
                "Preparation parses the target environment declaration without using or validating its local tools.",
            ],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())


@dataclass(frozen=True)
class RustClaimProgram(RustRelationalProgram):
    derivation: DelegationDerivation

    @property
    def purpose(self) -> Literal["candidate_claim_satisfaction"]:
        return _CLAIM_SATISFACTION

    def to_json(self) -> dict[str, Any]:
        return _claim_program_json(self, self.purpose)


@dataclass(frozen=True)
class RustClaimControlProgram(RustRelationalProgram):
    derivation: DelegationDerivation

    @property
    def purpose(self) -> Literal["near_miss_control"]:
        return _CLAIM_NEAR_MISS

    def to_json(self) -> dict[str, Any]:
        result = _claim_program_json(self, self.purpose)
        result["near_miss"] = self.derivation.near_miss.to_json()
        return result


RustClaimProofProgram = RustClaimProgram | RustClaimControlProgram


def _claim_program_json(
    program: RustClaimProgram | RustClaimControlProgram,
    purpose: Literal["candidate_claim_satisfaction", "near_miss_control"],
) -> dict[str, Any]:
    result = RustRelationalProgram.to_json(program)
    derivation = program.derivation
    model = {
        "scope": _KANI_MODEL_SCOPE,
        "domain": derivation.claim.domain.to_json(),
        "admissible_inputs": sorted(derivation.claim.admissible_inputs, key=int),
        "binding": result["binding"],
        "source_profiles": result["source_profiles"],
        "base_capture_sha256": program.base.digest,
        "candidate_capture_sha256": program.candidate.digest,
    }
    result.update({
        "scope": "rust_claim_proof_program",
        "purpose": purpose,
        "proof_scope": _KANI_MODEL_SCOPE,
        "basis_sha256": derivation.basis.digest,
        "claim_sha256": derivation.claim.digest,
        "domain_sha256": derivation.claim.domain.digest,
        "model_sha256": canonical_digest(model),
        "source_proof": False,
        "approval_authority": False,
        "native_compiler_correspondence": False,
    })
    return result


def _rust_literal(value: str) -> str:
    escaped = []
    for char in value:
        if char in {'"', "\\"}:
            escaped.append("\\" + char)
        elif ord(char) < 32 or ord(char) == 127:
            escaped.append(f"\\u{{{ord(char):x}}}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _membership(values: frozenset[str] | tuple[str, ...], expression: str) -> str:
    numbers = sorted(_u32_token(token) for token in values)
    if not numbers:
        return "false"
    return f"matches!({expression}, {' | '.join(f'{number}u32' for number in numbers)})"


class _HarnessBuilder:
    def __init__(self, label: str, binding: U32Binding) -> None:
        self._label = label
        self._lines: list[str] = []
        self._byte_count = 0
        self.append("#![forbid(unsafe_code)]")
        for side in ("base", "candidate"):
            self.append(f"#[path = {_rust_literal(side + '/' + binding.source)}] mod vkit_{side};")

    def append(self, line: str) -> None:
        self._byte_count += len(line.encode("utf-8")) + 1
        if self._byte_count > _MAX_HARNESS_BYTES:
            raise UnsupportedRustSource(f"generated {self._label} harness exceeds the 256 KiB profile limit")
        self._lines.append(line)

    def append_contract(self, function: str, contract: Any) -> None:
        self.append(f"fn {function}(x: u32, y: u32) -> bool {{ match x {{")
        for token in sorted(contract.admissible_inputs, key=int):
            self.append(f"{_u32_token(token)}u32 => {_membership(contract.outputs_for(token), 'y')},")
        self.append("_ => false, } }")

    def append_proof_preamble(self, admissible_inputs: Any, symbol: str) -> None:
        self.append("#[kani::proof]")
        self.append("fn vkit_relational() {")
        self.append("let x: u32 = kani::any();")
        self.append(f"kani::assume({_membership(admissible_inputs, 'x')});")
        self.append(f"let before: u32 = vkit_base::{symbol}(x);")

    def finish(self) -> bytes:
        self.append("}")
        return ("\n".join(self._lines) + "\n").encode("utf-8")


def _harness(bound: BoundChangeRequest, policy: DifferencePolicy, binding: U32Binding) -> bytes:
    contract = bound.intent.contract
    builder = _HarnessBuilder("relational", binding)
    builder.append_contract("vkit_standing", contract)
    builder.append_proof_preamble(contract.admissible_inputs, binding.symbol)
    builder.append(f"let after: u32 = vkit_candidate::{binding.symbol}(x);")
    builder.append(f'assert!({_membership(contract.domain.observations, "before")}, "base_observation_model");')
    builder.append(f'assert!({_membership(contract.domain.observations, "after")}, "candidate_observation_model");')
    builder.append('assert!(vkit_standing(x, after), "candidate_standing_intent");')
    builder.append("let relation: bool = match x {")
    for token in sorted(contract.admissible_inputs, key=int):
        applicable = [frozenset(row.outputs) for guard in policy.guards
                      for row in guard.requirements if row.input == token]
        if applicable:
            allowed = frozenset.intersection(*applicable)
            builder.append(f"{_u32_token(token)}u32 => {_membership(allowed, 'after')},")
    builder.append("_ => before == after || (!vkit_standing(x, before) && vkit_standing(x, after)),")
    builder.append("};")
    builder.append('assert!(relation, "relational_policy");')
    return builder.finish()


def _claim_harness(
    bound: BoundChangeRequest,
    binding: U32Binding,
    derivation: DelegationDerivation,
    purpose: Literal["candidate_claim_satisfaction", "near_miss_control"],
) -> bytes:
    intent = bound.intent.contract
    claim = derivation.claim
    if intent.domain.digest != claim.domain.digest or intent.admissible_inputs != claim.admissible_inputs:
        raise ValueError("claim proof requires the exact accepted intent domain")
    builder = _HarnessBuilder("claim", binding)
    builder.append_contract("vkit_standing", intent)
    builder.append_contract("vkit_claim", claim)
    builder.append_proof_preamble(intent.admissible_inputs, binding.symbol)
    builder.append(f"let actual_after: u32 = vkit_candidate::{binding.symbol}(x);")
    if purpose == _CLAIM_NEAR_MISS:
        near_miss = derivation.near_miss
        builder.append(
            f"let after: u32 = if x == {_u32_token(near_miss.input)}u32 "
            f"{{ {_u32_token(near_miss.output)}u32 }} else {{ actual_after }};"
        )
    else:
        builder.append("let after: u32 = actual_after;")
    builder.append(f'assert!({_membership(intent.domain.observations, "before")}, "base_observation_model");')
    builder.append(f'assert!({_membership(intent.domain.observations, "after")}, "candidate_observation_model");')
    builder.append('assert!(vkit_standing(x, actual_after), "candidate_standing_intent");')
    builder.append('assert!(vkit_claim(x, after), "relational_policy");')
    return builder.finish()


def prepare_rust_relational(
    project: Project, raw_request: Any, *, trusted_target_oid: str,
) -> RustRelationalProgram:
    bound, base, candidate = _prepare_change_request(
        project, raw_request, trusted_target_oid=trusted_target_oid,
    )
    return _prepare_captured_rust_relational(bound, base, candidate)


def _admit_captured_rust(
    bound: BoundChangeRequest,
    base: CapturedSource,
    candidate: CapturedSource,
) -> tuple[
    DifferencePolicy,
    tuple[tuple[str, str | bool | None], ...],
    bytes,
    U32Binding,
    RustSourceProfile,
    RustSourceProfile,
]:
    semantic = _semantic_files(bound, base, candidate)
    binding = parse_binding(_json_value(semantic["binding"], "binding semantic artifact"))
    if binding.language != "rust" or not binding.source.endswith(".rs"):
        raise UnsupportedRustSource("relational source preparation requires a Rust .rs binding")
    for role, kind in (("codec", "u32_le"), ("observation", "u32_return")):
        declaration = _json_value(semantic[role], f"{role} semantic artifact")
        if (not isinstance(declaration, dict) or type(declaration.get("schema_version")) is not int
                or declaration != {"schema_version": 1, "kind": kind}):
            raise UnsupportedRustSource(f"unsupported {role} declaration")
    environment = parse_environment(_json_value(semantic["environment"], "environment semantic artifact"), "rust")
    if environment.dependencies:
        raise UnsupportedRustSource("relational source profile does not admit external dependencies")
    _validate_domain(bound)
    if not bound.intent.contract.admissible_inputs:
        raise UnsupportedRustSource("relational source preparation requires nonempty accepted inputs")
    policy, identity, policy_bytes = _policy_for_captures(bound, base, candidate)
    if (len(policy.guards) > _MAX_GUARD_REQUIREMENTS
            or sum(len(guard.requirements) for guard in policy.guards) > _MAX_GUARD_REQUIREMENTS):
        raise UnsupportedRustSource("relational source profile accepts at most 4096 guard requirements")
    profiles = []
    for side, capture in (("base", base), ("candidate", candidate)):
        source = next((file for file in capture.files if file.path == binding.source), None)
        if source is None:
            raise UnsupportedRustSource(f"{side} captured Rust binding source is missing")
        profiles.append(admit_rust_source(source.content, binding))
    return policy, tuple(sorted(identity.items())), policy_bytes, binding, profiles[0], profiles[1]


def _prepare_captured_rust_relational(
    bound: BoundChangeRequest,
    base: CapturedSource,
    candidate: CapturedSource,
) -> RustRelationalProgram:
    policy, policy_identity, policy_bytes, binding, base_profile, candidate_profile = (
        _admit_captured_rust(bound, base, candidate)
    )
    return RustRelationalProgram(
        bound, base, candidate, policy, policy_identity, policy_bytes,
        binding, base_profile, candidate_profile, _harness(bound, policy, binding),
    )


def prepare_rust_claim_program(
    bound: BoundChangeRequest,
    base: CapturedSource,
    candidate: CapturedSource,
    *,
    purpose: Literal["candidate_claim_satisfaction", "near_miss_control"],
) -> RustClaimProofProgram:
    if purpose not in {_CLAIM_SATISFACTION, _CLAIM_NEAR_MISS}:
        raise ValueError("unsupported claim proof purpose")
    derivation = bound.delegation
    if derivation is None:
        raise ValueError("claim proof requires a target-bound delegation derivation")
    if derivation.basis.intent_sha256 != bound.intent.digest:
        raise ValueError("claim proof basis does not match the bound accepted intent")
    if derivation.claim.digest != bound.request.claim.digest:
        raise ValueError("claim proof derivation does not match the request claim")
    policy, policy_identity, policy_bytes, binding, base_profile, candidate_profile = (
        _admit_captured_rust(bound, base, candidate)
    )
    harness = _claim_harness(bound, binding, derivation, purpose)
    program_type = RustClaimProgram if purpose == _CLAIM_SATISFACTION else RustClaimControlProgram
    return program_type(
        bound=bound,
        base=base,
        candidate=candidate,
        policy=policy,
        policy_identity=policy_identity,
        policy_bytes=policy_bytes,
        binding=binding,
        base_profile=base_profile,
        candidate_profile=candidate_profile,
        harness=harness,
        derivation=derivation,
    )

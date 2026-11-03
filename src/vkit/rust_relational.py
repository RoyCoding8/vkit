from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from .certification import DelegationDerivation, DifferencePolicy
from .change_analysis import _policy_for_captures
from .inputs import CapturedSource, canonical_digest
from .intent import BoundChangeRequest, _prepare_change_request
from .paired import _json_value, _semantic_files, _u32_token, _validate_domain
from .paired_profiles import U32Binding, parse_binding, parse_environment
from .paths import Project
from .rust_source import RustSourceProfile, UnsupportedRustSource, admit_rust_source

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


def _harness(bound: BoundChangeRequest, policy: DifferencePolicy, binding: U32Binding) -> bytes:
    contract = bound.intent.contract
    lines: list[str] = []
    byte_count = 0

    def append(line: str) -> None:
        nonlocal byte_count
        byte_count += len(line.encode("utf-8")) + 1
        if byte_count > _MAX_HARNESS_BYTES:
            raise UnsupportedRustSource("generated relational harness exceeds the 256 KiB profile limit")
        lines.append(line)

    append("#![forbid(unsafe_code)]")
    for side in ("base", "candidate"):
        append(f"#[path = {_rust_literal(side + '/' + binding.source)}] mod vkit_{side};")
    append("fn vkit_standing(x: u32, y: u32) -> bool { match x {")
    for token in sorted(contract.admissible_inputs, key=int):
        append(f"{_u32_token(token)}u32 => {_membership(contract.outputs_for(token), 'y')},")
    append("_ => false, } }")
    append("#[kani::proof]")
    append("fn vkit_relational() {")
    append("let x: u32 = kani::any();")
    append(f"kani::assume({_membership(contract.admissible_inputs, 'x')});")
    append(f"let before: u32 = vkit_base::{binding.symbol}(x);")
    append(f"let after: u32 = vkit_candidate::{binding.symbol}(x);")
    append(f'assert!({_membership(contract.domain.observations, "before")}, "base_observation_model");')
    append(f'assert!({_membership(contract.domain.observations, "after")}, "candidate_observation_model");')
    append('assert!(vkit_standing(x, after), "candidate_standing_intent");')
    append("let relation: bool = match x {")
    for token in sorted(contract.admissible_inputs, key=int):
        applicable = [frozenset(row.outputs) for guard in policy.guards
                      for row in guard.requirements if row.input == token]
        if applicable:
            allowed = frozenset.intersection(*applicable)
            append(f"{_u32_token(token)}u32 => {_membership(allowed, 'after')},")
    append("_ => before == after || (!vkit_standing(x, before) && vkit_standing(x, after)),")
    append("};")
    append('assert!(relation, "relational_policy");')
    append("}")
    return ("\n".join(lines) + "\n").encode("utf-8")


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
    if purpose not in {_CLAIM_SATISFACTION, _CLAIM_NEAR_MISS}:
        raise ValueError("unsupported claim proof purpose")
    lines: list[str] = []
    byte_count = 0

    def append(line: str) -> None:
        nonlocal byte_count
        byte_count += len(line.encode("utf-8")) + 1
        if byte_count > _MAX_HARNESS_BYTES:
            raise UnsupportedRustSource("generated claim harness exceeds the 256 KiB profile limit")
        lines.append(line)

    append("#![forbid(unsafe_code)]")
    for side in ("base", "candidate"):
        append(f"#[path = {_rust_literal(side + '/' + binding.source)}] mod vkit_{side};")
    for function, contract in (("vkit_standing", intent), ("vkit_claim", claim)):
        append(f"fn {function}(x: u32, y: u32) -> bool {{ match x {{")
        for token in sorted(contract.admissible_inputs, key=int):
            append(f"{_u32_token(token)}u32 => {_membership(contract.outputs_for(token), 'y')},")
        append("_ => false, } }")
    append("#[kani::proof]")
    append("fn vkit_relational() {")
    append("let x: u32 = kani::any();")
    append(f"kani::assume({_membership(intent.admissible_inputs, 'x')});")
    append(f"let before: u32 = vkit_base::{binding.symbol}(x);")
    append(f"let actual_after: u32 = vkit_candidate::{binding.symbol}(x);")
    if purpose == _CLAIM_NEAR_MISS:
        near_miss = derivation.near_miss
        append(
            f"let after: u32 = if x == {_u32_token(near_miss.input)}u32 "
            f"{{ {_u32_token(near_miss.output)}u32 }} else {{ actual_after }};"
        )
    else:
        append("let after: u32 = actual_after;")
    append(f'assert!({_membership(intent.domain.observations, "before")}, "base_observation_model");')
    append(f'assert!({_membership(intent.domain.observations, "after")}, "candidate_observation_model");')
    append('assert!(vkit_standing(x, actual_after), "candidate_standing_intent");')
    append('assert!(vkit_claim(x, after), "relational_policy");')
    append("}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def prepare_rust_relational(
    project: Project, raw_request: Any, *, trusted_target_oid: str,
) -> RustRelationalProgram:
    bound, base, candidate = _prepare_change_request(
        project, raw_request, trusted_target_oid=trusted_target_oid,
    )
    return _prepare_captured_rust_relational(bound, base, candidate)


def _prepare_captured_rust_relational(
    bound: BoundChangeRequest,
    base: CapturedSource,
    candidate: CapturedSource,
) -> RustRelationalProgram:
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
    return RustRelationalProgram(
        bound, base, candidate, policy, tuple(sorted(identity.items())), policy_bytes,
        binding, profiles[0], profiles[1], _harness(bound, policy, binding),
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
    admitted = _prepare_captured_rust_relational(bound, base, candidate)
    harness = _claim_harness(bound, admitted.binding, derivation, purpose)
    arguments = {
        "bound": admitted.bound,
        "base": admitted.base,
        "candidate": admitted.candidate,
        "policy": admitted.policy,
        "policy_identity": admitted.policy_identity,
        "policy_bytes": admitted.policy_bytes,
        "binding": admitted.binding,
        "base_profile": admitted.base_profile,
        "candidate_profile": admitted.candidate_profile,
        "harness": harness,
        "derivation": derivation,
    }
    if purpose == _CLAIM_SATISFACTION:
        return RustClaimProgram(**arguments)
    return RustClaimControlProgram(**arguments)

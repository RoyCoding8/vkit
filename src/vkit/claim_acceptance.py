from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .change_methods import ChangeMethodPlan, KaniMethod
from .evidence import _retained_path
from .inputs import CapturedSource, canonical_digest
from .intent import BoundChangeRequest
from .kani_result import classify_kani_result
from .paired import _artifact, _artifact_mismatches, _json_value
from .rust_relational import RustClaimProofProgram, prepare_rust_claim_program
from .schemas import validate
from .witness import ArtifactRef

ACCEPTANCE_PATH = "claim-acceptance.v1.json"
PROOF_DIRECTORY = "delegated-claim"
PURPOSES = ("candidate_claim_satisfaction", "near_miss_control")


@dataclass(frozen=True)
class ClaimAcceptance:
    bound: BoundChangeRequest
    satisfaction: ArtifactRef
    control: ArtifactRef
    implementation: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        derivation = self.bound.delegation
        if derivation is None:
            raise ValueError("claim acceptance requires a checked delegation")
        return {
            "schema_version": 1,
            "accepted_by": "agent_strengthening",
            "scope": "restricted_captured_rust_kani_model",
            "source_proof": False,
            "approval_authority": False,
            "native_compiler_correspondence": False,
            "bound_request_sha256": self.bound.digest,
            "intent_sha256": self.bound.intent.digest,
            "basis_sha256": derivation.basis.digest,
            "claim_sha256": derivation.claim.digest,
            "claim": derivation.claim.to_json(),
            "derivation": derivation.to_json(),
            "target_oid": self.bound.request.base_oid,
            "candidate_oid": self.bound.request.candidate_oid,
            "base_capture_sha256": self.bound.base_capture_sha256,
            "candidate_capture_sha256": self.bound.candidate_capture_sha256,
            "checker_sha256": canonical_digest(self.implementation),
            "satisfaction": self.satisfaction.to_json(),
            "near_miss_control": self.control.to_json(),
        }


def _read(root: Path, relative: str) -> Any:
    return _json_value(_retained_path(root, relative).read_bytes(), relative)


def _check_execution(
    root: Path, program: RustClaimProofProgram, method: KaniMethod, implementation: dict[str, str],
    expected: tuple[str, str],
) -> None:
    report = _read(root, "report.json")
    if (report["scope"] != "captured_rust_kani_execution"
            or report["program_scope"] != program.to_json()["scope"]
            or report["program_purpose"] != program.purpose
            or report["program_sha256"] != program.digest
            or report["toolchain"] != method.toolchain.to_json()
            or report["outcome"] != {"kind": expected[0], "reason": expected[1]}
            or report["platform"]["system"] != "Linux" or report["platform"]["machine"] != "x86_64"
            or any(report[name] is not True for name in (
                "executed", "toolchain_validated_before", "toolchain_validated_after"))
            or any(report[name] is not False for name in (
                "source_proof", "approval_authority", "native_compiler_correspondence"))
            or _artifact_mismatches(root, report)):
        raise ValueError("delegated proof execution is incomplete or differs from its binding")
    if (_read(root, "program.json") != program.to_json()
            or _read(root, "implementation.json") != implementation
            or _retained_path(root, "engine/harness.rs").read_bytes() != program.harness):
        raise ValueError("delegated proof program or checker identity differs")
    for side, capture in (("base", program.base), ("candidate", program.candidate)):
        if _read(root, f"captures/{side}.json") != capture.to_json():
            raise ValueError("delegated proof capture differs")
        for file in capture.files:
            if _retained_path(root, f"engine/{side}/{file.path}").read_bytes() != file.content:
                raise ValueError("delegated proof source bytes differ")
    pin = method.toolchain
    for name, digest in (("kani", pin.kani_manifest_sha256), ("rust", pin.rust_manifest_sha256)):
        before = _retained_path(root, f"toolchain/before/{name}.jsonl").read_bytes()
        after = _retained_path(root, f"toolchain/after/{name}.jsonl").read_bytes()
        if before != after or hashlib.sha256(before).hexdigest() != digest:
            raise ValueError("delegated proof toolchain manifest differs")
    for name in ("cargo-bin", "host-libraries"):
        if (_retained_path(root, f"toolchain/before/{name}.jsonl").read_bytes()
                != _retained_path(root, f"toolchain/after/{name}.jsonl").read_bytes()):
            raise ValueError("delegated proof host manifest changed")
    invocation = _read(root, "invocation.json")
    if invocation != {
        "argv": [str(pin.driver), "harness.rs", "--harness", "vkit_relational",
                 "-Z", "unstable-options", "--export-json", "kani-results.json"],
        "cwd": "engine", "environment": pin.environment(),
    }:
        raise ValueError("delegated proof invocation differs")
    process = _read(root, "process.json")
    if (process["code"] != (0 if expected[0] == "verified" else 1)
            or process["launch_error"] is not None
            or any(process[name] is not False for name in ("cancelled", "timed_out", "output_limited"))):
        raise ValueError("delegated proof process did not complete")
    outcome = classify_kani_result(
        _retained_path(root, "engine/kani-results.json").read_bytes(),
        exit_code=process["code"], harness=program.harness,
    )
    if ((outcome.kind, outcome.reason) != expected
            or _read(root, "classification.json") != outcome.to_json()):
        raise ValueError("delegated proof export does not establish the required result")


def checked_acceptance(
    bound: BoundChangeRequest, base: CapturedSource, candidate: CapturedSource,
    methods: ChangeMethodPlan, root: Path, implementation: dict[str, str],
) -> ClaimAcceptance:
    if bound.delegation is None or len(methods.steps) != 1 or not isinstance(methods.steps[0], KaniMethod):
        raise ValueError("claim acceptance requires only the qualified Kani method")
    method = methods.steps[0]
    refs = []
    for purpose, expected in zip(PURPOSES, (
        ("verified", "all_qualified_properties_succeeded"),
        ("proof_rejected", "named_relational_policy_assertion_failed"),
    )):
        program = prepare_rust_claim_program(bound, base, candidate, purpose=purpose)
        directory = root / PROOF_DIRECTORY / purpose
        _check_execution(directory, program, method, implementation, expected)
        refs.append(_artifact(root, directory / "report.json", "application/json"))
    acceptance = ClaimAcceptance(bound, refs[0], refs[1], implementation)
    validate("accepted claim", "claim-acceptance.v1.json", acceptance.to_json())
    return acceptance


def validate_claim_acceptance(record: dict[str, Any], directory: Path, base: CapturedSource,
                              candidate: CapturedSource) -> None:
    from .certification import ChangeCertificationRequest
    from .intent import _bind_captured_policy

    report = (record.get("verification") or {}).get("change_certification", {})
    delegated = report.get("delegated_claim")
    root = directory / "change-output"
    if not isinstance(delegated, dict) or delegated.get("status") != "accepted":
        if (root / ACCEPTANCE_PATH).exists():
            raise ValueError("unpublished claim acceptance artifact")
        return
    if (record["state"] != "done" or (directory / "cancel").exists()
            or report["stopped"] is not None or report["status"] != "model_verified"
            or _read(root, "change-certification.json") != report):
        raise ValueError("claim acceptance is cancelled, incomplete or stale")
    request = ChangeCertificationRequest.from_json(record["change_request"])
    bound = _bind_captured_policy(request, base, candidate)
    if (bound.digest != record["bound_request_sha256"]
            or request.base_oid != record["trusted_target_oid"]
            or base.commit != request.base_oid or candidate.commit != request.candidate_oid):
        raise ValueError("accepted claim target binding differs")
    expected = checked_acceptance(
        bound, base, candidate, ChangeMethodPlan.from_json(record["method_plan"]),
        root, record["implementation"],
    ).to_json()
    actual = _read(root, ACCEPTANCE_PATH)
    validate("accepted claim", "claim-acceptance.v1.json", actual)
    if (actual != expected
            or delegated != {"status": "accepted", "acceptance":
                             _artifact(root, root / ACCEPTANCE_PATH, "application/json").to_json()}):
        raise ValueError("accepted claim differs from its checked evidence")

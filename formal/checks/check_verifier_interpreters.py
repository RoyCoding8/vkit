"""Exhaustive real-code formal checker for Lean 4 and TLC report interpreters.

Enumerates finite abstract report states and runs the real
`vkit.verifiers.lean_adapter.interpret` (plus `logical_trust_from_report`) and
`vkit.verifiers.tlc_adapter.interpret` against an independent specification
oracle and explicit safety invariants, then proves the checker catches
deliberate negative-control mutants.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from vkit.core.outcome import Blocked, BlockedReason
from vkit.verifiers import lean_adapter, tlc_adapter
from vkit.verifiers.obligation import PropertyObligation, TheoremObligation
from vkit.verifiers.spec import (
    DomainBounds,
    FingerprintSpec,
    LeanCheck,
    LeanProfile,
    ModuleRef,
    TlcCheck,
    ToolchainRef,
)

LEAN_VERSION = "4.16.0"
LEAN_MODULE = "ProofChallenge"
LEAN_THEOREM = "main_theorem"
LEAN_PERMITTED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")
LEAN_OLEAN_DIGEST = "1" * 64
LEAN_OLEAN_DIGEST_ALT = "2" * 64
LEAN_WRONG_DIGEST = "0" * 64

TLC_VERSION = "2.19"
TLC_JAR_SHA256 = "a" * 64
TLC_MODEL_MODULE = "SpecModel"
TLC_MODEL_PATH = "SpecModel.tla"
TLC_CONFIG_PATH = "SpecModel.cfg"
TLC_PROPERTY = "TypeOK"
TLC_BOUND_NAME = "N"
TLC_BOUND_VALUE = 2


class OutcomeKind(StrEnum):
    PASS = "PASS"
    FAIL_WITNESS = "FAIL_WITNESS"
    BLOCKED = "BLOCKED"


class DigestState(StrEnum):
    MATCH = "match"
    EMPTY = "empty"
    MISMATCH = "mismatch"


class RecheckState(StrEnum):
    MATCH = "match"
    MISSING = "missing"
    EXIT_NONZERO = "exit_nonzero"
    DIGEST_MISMATCH = "digest_mismatch"


class TheoremResolution(StrEnum):
    RESOLVED = "resolved"
    MISSING_ERROR = "missing_error"
    UNREPORTED = "unreported"


class AxiomProfile(StrEnum):
    NONE = "none"
    PERMITTED = "permitted"
    SORRY = "sorry"
    UNAPPROVED = "unapproved"
    CUSTOM = "custom"
    SORRY_AND_CUSTOM = "sorry_and_custom"


class ComparatorStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CHALLENGE_CHANGED = "challenge_changed"
    UNKNOWN = "unknown"


class TlcSummaryState(StrEnum):
    ABSENT = "absent"
    QUEUE_ZERO = "queue_zero"
    QUEUE_NONZERO = "queue_nonzero"


class TlcPropertyState(StrEnum):
    ALL_SATISFIED = "all_satisfied"
    MISSING_IN_CONFIG = "missing_in_config"
    VIOLATED_VALIDATED = "violated_validated"
    VIOLATED_UNVALIDATED = "violated_unvalidated"


class TlcBoundsState(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class ExpectedOutcome:
    kind: OutcomeKind
    reason: BlockedReason | None = None
    token: str | None = None


@dataclass(frozen=True)
class LeanAbstractState:
    digest_state: DigestState
    build1_exit: int
    build1_sorry_msg: bool
    recheck_state: RecheckState
    theorem_resolution: TheoremResolution
    axiom_profile: AxiomProfile


@dataclass(frozen=True)
class TlcAbstractState:
    exit_code: int
    simulation_mode: bool
    summary_state: TlcSummaryState
    completed_banner: bool
    property_state: TlcPropertyState
    bounds_state: TlcBoundsState


def _blocked_token(result: Blocked) -> str:
    return result.detail.partition(":")[0]


def _classify_lean_result(result: lean_adapter.AdapterResult | Blocked) -> ExpectedOutcome:
    if isinstance(result, Blocked):
        return ExpectedOutcome(OutcomeKind.BLOCKED, result.reason, _blocked_token(result))
    if result.observations and not result.counterexamples:
        return ExpectedOutcome(OutcomeKind.PASS)
    return ExpectedOutcome(OutcomeKind.FAIL_WITNESS)


def _classify_tlc_result(result: tlc_adapter.AdapterResult | Blocked) -> ExpectedOutcome:
    if isinstance(result, Blocked):
        return ExpectedOutcome(OutcomeKind.BLOCKED, result.reason, _blocked_token(result))
    if result.observations and not result.counterexamples:
        return ExpectedOutcome(OutcomeKind.PASS)
    return ExpectedOutcome(OutcomeKind.FAIL_WITNESS)


def _lean_expected_axioms(profile: AxiomProfile) -> tuple[str, ...]:
    mapping: dict[AxiomProfile, tuple[str, ...]] = {
        AxiomProfile.NONE: (),
        AxiomProfile.PERMITTED: tuple(sorted(LEAN_PERMITTED_AXIOMS)),
        AxiomProfile.SORRY: tuple(sorted(("propext", lean_adapter.SORRY_AXIOM))),
        AxiomProfile.UNAPPROVED: tuple(sorted(("propext", "Lean.ofReduceBool"))),
        AxiomProfile.CUSTOM: tuple(sorted(("propext", "CustomAx"))),
        AxiomProfile.SORRY_AND_CUSTOM: tuple(sorted((lean_adapter.SORRY_AXIOM, "CustomAx"))),
    }
    return mapping[profile]


def _lean_oracle(state: LeanAbstractState) -> ExpectedOutcome:
    """Independent specification oracle for reviewed-profile Lean interpretation."""
    if state.digest_state is DigestState.EMPTY:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.ARTIFACT_MALFORMED, "challenge_unknown")
    if state.digest_state is DigestState.MISMATCH:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.SOURCE_CHANGED, "challenge_altered")
    if state.build1_exit != 0 or state.recheck_state is RecheckState.EXIT_NONZERO:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.FORMAL_OBLIGATION, "incomplete_proof")
    if state.build1_sorry_msg:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.FORMAL_OBLIGATION, "incomplete_proof")
    if state.recheck_state in {RecheckState.MISSING, RecheckState.DIGEST_MISMATCH}:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.INTERNAL_ERROR, "recheck_diverged")
    if state.theorem_resolution is TheoremResolution.MISSING_ERROR:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.SCENARIO_UNKNOWN, "theorem_absent")
    if state.theorem_resolution is TheoremResolution.UNREPORTED:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.FORMAL_OBLIGATION, "axioms_unreported")
    if state.axiom_profile in {AxiomProfile.SORRY, AxiomProfile.SORRY_AND_CUSTOM}:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.FORMAL_OBLIGATION, "proof_incomplete")
    if state.axiom_profile in {AxiomProfile.UNAPPROVED, AxiomProfile.CUSTOM}:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.FORMAL_OBLIGATION, "axioms_unapproved")
    return ExpectedOutcome(OutcomeKind.PASS)


def _lean_safety_violations(
    state: LeanAbstractState,
    actual: ExpectedOutcome,
    result: lean_adapter.AdapterResult | Blocked,
) -> list[str]:
    """Safety invariants that must hold for every Lean abstract state."""
    violations: list[str] = []
    if actual.kind is OutcomeKind.PASS:
        if state.axiom_profile in {AxiomProfile.SORRY, AxiomProfile.SORRY_AND_CUSTOM}:
            violations.append("lean_passed_with_sorryAx")
        if state.build1_sorry_msg:
            violations.append("lean_passed_with_sorry_message")
        if state.axiom_profile in {AxiomProfile.UNAPPROVED, AxiomProfile.CUSTOM}:
            violations.append("lean_passed_with_unapproved_axiom")
        if state.digest_state is not DigestState.MATCH:
            violations.append("lean_passed_with_digest_mismatch_or_empty")
        if state.recheck_state is not RecheckState.MATCH:
            violations.append("lean_passed_without_matching_recheck2")
        if state.build1_exit != 0:
            violations.append("lean_passed_with_nonzero_build_exit")
        if state.theorem_resolution is not TheoremResolution.RESOLVED:
            violations.append("lean_passed_without_resolved_theorem")
        assert isinstance(result, lean_adapter.AdapterResult)
        if result.logical_trust is None:
            violations.append("lean_pass_missing_logical_trust")
        elif result.logical_trust["axioms"]["status"] != "reported":
            violations.append("lean_pass_axioms_not_reported_in_trust")
    return violations


def _build_lean_report(
    state: LeanAbstractState,
    challenge_path: Path,
    measured_sha256: str,
) -> bytes:
    if state.digest_state is DigestState.MATCH:
        source_sha = measured_sha256
    elif state.digest_state is DigestState.EMPTY:
        source_sha = ""
    else:
        source_sha = LEAN_WRONG_DIGEST

    build1_messages: list[dict[str, Any]] = []
    if state.build1_exit != 0:
        build1_messages.append({
            "severity": "error",
            "kind": "error",
            "data": "type mismatch",
        })
    if state.build1_sorry_msg:
        build1_messages.append({
            "severity": "warning",
            "kind": "hasSorry",
            "data": "declaration uses 'sorry'",
        })

    steps: list[dict[str, Any]] = [
        {
            "name": "build:1",
            "exit_code": state.build1_exit,
            "olean_digest": LEAN_OLEAN_DIGEST,
            "messages": build1_messages,
        }
    ]

    if state.recheck_state is RecheckState.MATCH:
        steps.append({
            "name": "recheck:2",
            "exit_code": 0,
            "olean_digest": LEAN_OLEAN_DIGEST,
            "messages": [],
        })
    elif state.recheck_state is RecheckState.EXIT_NONZERO:
        steps.append({
            "name": "recheck:2",
            "exit_code": 1,
            "olean_digest": LEAN_OLEAN_DIGEST,
            "messages": [{"severity": "error", "kind": "error", "data": "recheck failed"}],
        })
    elif state.recheck_state is RecheckState.DIGEST_MISMATCH:
        steps.append({
            "name": "recheck:2",
            "exit_code": 0,
            "olean_digest": LEAN_OLEAN_DIGEST_ALT,
            "messages": [],
        })

    audit_messages: list[dict[str, Any]] = []
    if state.theorem_resolution is TheoremResolution.MISSING_ERROR:
        audit_messages.append({
            "severity": "error",
            "kind": "error",
            "data": f"unknown identifier '{LEAN_MODULE}.{LEAN_THEOREM}'",
        })
    elif state.theorem_resolution is TheoremResolution.RESOLVED:
        axioms = _lean_expected_axioms(state.axiom_profile)
        if not axioms:
            line = f"'{LEAN_MODULE}.{LEAN_THEOREM}' does not depend on any axioms"
        else:
            line = f"'{LEAN_MODULE}.{LEAN_THEOREM}' depends on axioms: [{', '.join(axioms)}]"
        audit_messages.append({
            "severity": "information",
            "kind": "info",
            "data": line,
        })

    steps.append({
        "name": "audit",
        "exit_code": 1 if state.theorem_resolution is TheoremResolution.MISSING_ERROR else 0,
        "messages": audit_messages,
    })

    document = {
        "tool": "lean",
        "version": lean_adapter.REPORT_VERSION,
        "complete": True,
        "module": LEAN_MODULE,
        "source": str(challenge_path),
        "profile": LeanProfile.REVIEWED.value,
        "tool_version": f"Lean (version {LEAN_VERSION}, x86_64-unknown-linux-gnu)",
        "source_sha256": source_sha,
        "theorems": [LEAN_THEOREM],
        "steps": steps,
    }
    return json.dumps(document).encode("utf-8")


def _tlc_oracle(state: TlcAbstractState) -> ExpectedOutcome:
    """Independent specification oracle for TLC report interpretation."""
    if state.exit_code in tlc_adapter.INFRASTRUCTURE_STATUSES:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.INTERNAL_ERROR, "incomplete_exploration")
    if state.exit_code not in (tlc_adapter.SUCCESS, *tlc_adapter.VIOLATION_STATUSES):
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.INTERNAL_ERROR, "incomplete_exploration")
    if state.simulation_mode:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.ARTIFACT_EMPTY, "simulation_only")
    if state.summary_state is TlcSummaryState.ABSENT:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.ARTIFACT_EMPTY, "results_absent")

    stdout_violated = state.property_state in {
        TlcPropertyState.VIOLATED_VALIDATED,
        TlcPropertyState.VIOLATED_UNVALIDATED,
    }
    violated = stdout_violated or (state.exit_code in tlc_adapter.VIOLATION_STATUSES)

    if not violated and state.exit_code == tlc_adapter.SUCCESS:
        if state.summary_state is TlcSummaryState.QUEUE_NONZERO:
            return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.INTERNAL_ERROR, "incomplete_exploration")
        if not state.completed_banner:
            return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.INTERNAL_ERROR, "incomplete_exploration")

    if state.property_state is TlcPropertyState.MISSING_IN_CONFIG:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.SCENARIO_UNKNOWN, "properties_absent")
    if state.bounds_state is TlcBoundsState.MISMATCH:
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.ARTIFACT_MALFORMED, "bounds_unresolved")

    if violated:
        if state.property_state is TlcPropertyState.VIOLATED_VALIDATED:
            return ExpectedOutcome(OutcomeKind.FAIL_WITNESS)
        if state.property_state is TlcPropertyState.VIOLATED_UNVALIDATED:
            return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.SCENARIO_UNKNOWN, "witness_validation_failed")
        return ExpectedOutcome(OutcomeKind.BLOCKED, BlockedReason.SCENARIO_UNKNOWN, "counterexample_unattributed")

    return ExpectedOutcome(OutcomeKind.PASS)


def _tlc_safety_violations(
    state: TlcAbstractState,
    actual: ExpectedOutcome,
    result: tlc_adapter.AdapterResult | Blocked,
) -> list[str]:
    """Safety invariants that must hold for every TLC abstract state."""
    violations: list[str] = []
    if actual.kind is OutcomeKind.PASS:
        if state.exit_code != tlc_adapter.SUCCESS:
            violations.append("tlc_passed_with_nonzero_exit")
        if state.simulation_mode:
            violations.append("tlc_passed_in_simulation_mode")
        if state.summary_state is not TlcSummaryState.QUEUE_ZERO:
            violations.append("tlc_passed_with_queue_nonempty_or_absent")
        if not state.completed_banner:
            violations.append("tlc_passed_without_completion_banner")
        if state.property_state is not TlcPropertyState.ALL_SATISFIED:
            violations.append("tlc_passed_with_missing_or_violated_property")
        if state.bounds_state is not TlcBoundsState.MATCH:
            violations.append("tlc_passed_with_mismatched_bounds")
        assert isinstance(result, tlc_adapter.AdapterResult)
        if not result.exploration.exhaustive:
            violations.append("tlc_pass_exploration_not_exhaustive")
    return violations


def _build_tlc_report_and_config(state: TlcAbstractState) -> tuple[bytes, str]:
    stdout_lines: list[str] = [f"TLC2 Version {TLC_VERSION} of 08 August 2024"]
    if state.simulation_mode:
        stdout_lines.append("The number of states generated: 100")

    if state.property_state in {
        TlcPropertyState.VIOLATED_VALIDATED,
        TlcPropertyState.VIOLATED_UNVALIDATED,
    }:
        stdout_lines.append(f"Error: Invariant {TLC_PROPERTY} is violated.")
        stdout_lines.append("Error: The behavior up to this state is:")
        stdout_lines.append("State 1: <Initial predicate>")
        stdout_lines.append("x = 0")

    if state.completed_banner:
        stdout_lines.append("Model checking completed. No error has been found.")

    if state.summary_state is TlcSummaryState.QUEUE_ZERO:
        stdout_lines.append("12 states generated, 6 distinct states found, 0 states left on queue.")
        stdout_lines.append("The depth of the complete state graph search is 3.")
    elif state.summary_state is TlcSummaryState.QUEUE_NONZERO:
        stdout_lines.append("12 states generated, 6 distinct states found, 4 states left on queue.")
        stdout_lines.append("The depth of the complete state graph search is 3.")

    witness_validation: dict[str, Any] | None = None
    trace_artifact: str | None = None
    validation_artifact: str | None = None
    if state.property_state is TlcPropertyState.VIOLATED_VALIDATED:
        witness_validation = {
            "status": "validated",
            "method": "tlc_json_replay",
            "property": TLC_PROPERTY,
            "state_sequence_sha256": "b" * 64,
            "minimization": {"applied": False},
            "source_locations": [],
            "source_locations_truncated": False,
        }
        trace_artifact = "trace.json"
        validation_artifact = "validation.json"

    document = {
        "tool": "tlc",
        "version": tlc_adapter.REPORT_VERSION,
        "complete": True,
        "model": TLC_MODEL_MODULE,
        "model_path": TLC_MODEL_PATH,
        "config": TLC_CONFIG_PATH,
        "tool_version": f"TLC2 Version {TLC_VERSION}",
        "jar_sha256": TLC_JAR_SHA256,
        "fingerprint": {
            "constants_from_config": True,
            "checksum_states": False,
            "workers": 1,
        },
        "exit_code": state.exit_code,
        "stdout": "\n".join(stdout_lines),
        "stderr": "",
        "trace_json_supported": True,
        "witness_validation": witness_validation,
        "trace_artifact": trace_artifact,
        "validation_artifact": validation_artifact,
    }

    bound_rhs = f"1..{TLC_BOUND_VALUE}" if state.bounds_state is TlcBoundsState.MATCH else "1..9"
    config_lines = [
        "SPECIFICATION Spec",
        f"CONSTANTS {TLC_BOUND_NAME} = {bound_rhs}",
    ]
    if state.property_state is not TlcPropertyState.MISSING_IN_CONFIG:
        config_lines.append(f"INVARIANT {TLC_PROPERTY}")

    return json.dumps(document).encode("utf-8"), "\n".join(config_lines) + "\n"


def _lean_states() -> list[LeanAbstractState]:
    return [
        LeanAbstractState(
            digest_state=digest_state,
            build1_exit=build1_exit,
            build1_sorry_msg=build1_sorry_msg,
            recheck_state=recheck_state,
            theorem_resolution=theorem_resolution,
            axiom_profile=axiom_profile,
        )
        for (
            digest_state,
            build1_exit,
            build1_sorry_msg,
            recheck_state,
            theorem_resolution,
            axiom_profile,
        ) in itertools.product(
            DigestState,
            (0, 1),
            (False, True),
            RecheckState,
            TheoremResolution,
            AxiomProfile,
        )
    ]


def _tlc_states() -> list[TlcAbstractState]:
    exit_codes = (
        tlc_adapter.SUCCESS,
        tlc_adapter.VIOLATION_SAFETY,
        tlc_adapter.FAILURE_SPEC_EVAL,
        42,
    )
    return [
        TlcAbstractState(
            exit_code=exit_code,
            simulation_mode=simulation_mode,
            summary_state=summary_state,
            completed_banner=completed_banner,
            property_state=property_state,
            bounds_state=bounds_state,
        )
        for (
            exit_code,
            simulation_mode,
            summary_state,
            completed_banner,
            property_state,
            bounds_state,
        ) in itertools.product(
            exit_codes,
            (False, True),
            TlcSummaryState,
            (False, True),
            TlcPropertyState,
            TlcBoundsState,
        )
    ]


def _check_lean_space(root: Path) -> dict[str, Any]:
    challenge_rel = f"{LEAN_MODULE}.lean"
    challenge_path = root / challenge_rel
    challenge_bytes = b"theorem main_theorem : True := trivial\n"
    challenge_path.write_bytes(challenge_bytes)
    measured_sha256 = hashlib.sha256(challenge_bytes).hexdigest()

    check = LeanCheck(
        id="formal-lean-check",
        cwd=root,
        timeout_seconds=30.0,
        artifact_name="lean-report.json",
        challenge=ModuleRef(module=LEAN_MODULE, path=challenge_rel),
        theorems=(TheoremObligation(theorem=LEAN_THEOREM, module=LEAN_MODULE),),
        profile=LeanProfile.REVIEWED,
        permitted_axioms=LEAN_PERMITTED_AXIOMS,
        toolchain=ToolchainRef(tool="lean", version=LEAN_VERSION),
    )

    states = _lean_states()
    oracle_mismatches: list[dict[str, Any]] = []
    safety_violations: list[dict[str, Any]] = []
    pass_count = 0
    trust_checks = 0

    for state in states:
        raw = _build_lean_report(state, challenge_path, measured_sha256)
        result = lean_adapter.interpret(raw, check)
        actual = _classify_lean_result(result)
        expected = _lean_oracle(state)
        if actual != expected:
            oracle_mismatches.append({
                "state": repr(state),
                "expected": repr(expected),
                "actual": repr(actual),
            })
        violations = _lean_safety_violations(state, actual, result)
        if violations:
            safety_violations.append({
                "state": repr(state),
                "violations": violations,
            })
        if actual.kind is OutcomeKind.PASS:
            pass_count += 1
            parsed = lean_adapter._read(raw)
            assert isinstance(parsed, lean_adapter.LeanRun)
            expected_trust = lean_adapter.logical_trust_from_report(parsed, check)
            assert isinstance(result, lean_adapter.AdapterResult)
            if result.logical_trust != expected_trust:
                oracle_mismatches.append({
                    "state": repr(state),
                    "expected": "logical_trust == logical_trust_from_report(parsed, check)",
                    "actual": "mismatch",
                })
            trust_checks += 1

    # Also verify logical_trust_from_report directly on an unreported theorem report.
    unreported_state = LeanAbstractState(
        digest_state=DigestState.MATCH,
        build1_exit=0,
        build1_sorry_msg=False,
        recheck_state=RecheckState.MATCH,
        theorem_resolution=TheoremResolution.UNREPORTED,
        axiom_profile=AxiomProfile.NONE,
    )
    unreported_parsed = lean_adapter._read(
        _build_lean_report(unreported_state, challenge_path, measured_sha256)
    )
    assert isinstance(unreported_parsed, lean_adapter.LeanRun)
    unreported_trust = lean_adapter.logical_trust_from_report(unreported_parsed, check)
    if unreported_trust["axioms"]["status"] != "not_reported":
        oracle_mismatches.append({
            "state": "logical_trust_unreported",
            "expected": "not_reported",
            "actual": unreported_trust["axioms"]["status"],
        })
    trust_checks += 1

    # Also verify unreviewed comparator profile statuses.
    comparator_check = LeanCheck(
        id="formal-lean-comparator",
        cwd=root,
        timeout_seconds=30.0,
        artifact_name="comparator-report.json",
        challenge=ModuleRef(module=LEAN_MODULE, path=challenge_rel),
        solution=ModuleRef(module="Solution", path="Solution.lean"),
        challenge_sha256=measured_sha256,
        theorems=(TheoremObligation(theorem=LEAN_THEOREM, module=LEAN_MODULE),),
        profile=LeanProfile.UNREVIEWED,
        permitted_axioms=LEAN_PERMITTED_AXIOMS,
        toolchain=ToolchainRef(tool="lean", version=LEAN_VERSION, comparator="comparator"),
    )
    comparator_expected: dict[ComparatorStatus, ExpectedOutcome] = {
        ComparatorStatus.ACCEPTED: ExpectedOutcome(OutcomeKind.PASS),
        ComparatorStatus.REJECTED: ExpectedOutcome(
            OutcomeKind.BLOCKED, BlockedReason.FORMAL_OBLIGATION, "proof_rejected"
        ),
        ComparatorStatus.CHALLENGE_CHANGED: ExpectedOutcome(
            OutcomeKind.BLOCKED, BlockedReason.NOT_APPROVED, "challenge_changed"
        ),
        ComparatorStatus.UNKNOWN: ExpectedOutcome(
            OutcomeKind.BLOCKED, BlockedReason.ARTIFACT_MALFORMED, "report_malformed"
        ),
    }
    for status in ComparatorStatus:
        payload = json.dumps({
            "status": status.value,
            "challenge_sha256": LEAN_WRONG_DIGEST,
            "exit_code": 1,
            "output": "proof rejected",
            "sandbox": "bubblewrap",
            "comparator_version": "0.1.0",
        }).encode("utf-8")
        actual = _classify_lean_result(lean_adapter.interpret(payload, comparator_check))
        expected = comparator_expected[status]
        if actual != expected:
            oracle_mismatches.append({
                "state": f"comparator:{status.value}",
                "expected": repr(expected),
                "actual": repr(actual),
            })

    return {
        "reviewed_tuples": len(states),
        "comparator_tuples": len(ComparatorStatus),
        "total_tuples": len(states) + len(ComparatorStatus),
        "pass_tuples": pass_count,
        "logical_trust_checks": trust_checks,
        "oracle_mismatches": len(oracle_mismatches),
        "safety_violations": len(safety_violations),
        "sample_mismatch": oracle_mismatches[0] if oracle_mismatches else None,
        "sample_violation": safety_violations[0] if safety_violations else None,
    }


def _check_tlc_space(root: Path) -> dict[str, Any]:
    (root / TLC_MODEL_PATH).write_text("---- MODULE SpecModel ----\n====\n", encoding="utf-8")
    config_file = root / TLC_CONFIG_PATH

    check = TlcCheck(
        id="formal-tlc-check",
        cwd=root,
        timeout_seconds=30.0,
        artifact_name="tlc-report.json",
        model=ModuleRef(module=TLC_MODEL_MODULE, path=TLC_MODEL_PATH),
        config=TLC_CONFIG_PATH,
        properties=(PropertyObligation(property_name=TLC_PROPERTY, bounds=((TLC_BOUND_NAME, TLC_BOUND_VALUE),)),),
        bounds=DomainBounds(((TLC_BOUND_NAME, TLC_BOUND_VALUE),)),
        fingerprint=FingerprintSpec(constants_from_config=True, checksum_states=False, workers=1),
        toolchain=ToolchainRef(tool="tlc", version=TLC_VERSION, jar_sha256=TLC_JAR_SHA256),
    )

    states = _tlc_states()
    oracle_mismatches: list[dict[str, Any]] = []
    safety_violations: list[dict[str, Any]] = []
    pass_count = 0
    witness_fail_count = 0

    for state in states:
        raw, config_text = _build_tlc_report_and_config(state)
        config_file.write_text(config_text, encoding="utf-8")
        result = tlc_adapter.interpret(raw, check)
        actual = _classify_tlc_result(result)
        expected = _tlc_oracle(state)
        if actual != expected:
            oracle_mismatches.append({
                "state": repr(state),
                "expected": repr(expected),
                "actual": repr(actual),
            })
        violations = _tlc_safety_violations(state, actual, result)
        if violations:
            safety_violations.append({
                "state": repr(state),
                "violations": violations,
            })
        if actual.kind is OutcomeKind.PASS:
            pass_count += 1
        elif actual.kind is OutcomeKind.FAIL_WITNESS:
            witness_fail_count += 1

    return {
        "total_tuples": len(states),
        "pass_tuples": pass_count,
        "witness_fail_tuples": witness_fail_count,
        "oracle_mismatches": len(oracle_mismatches),
        "safety_violations": len(safety_violations),
        "sample_mismatch": oracle_mismatches[0] if oracle_mismatches else None,
        "sample_violation": safety_violations[0] if safety_violations else None,
    }


def _run_mutant(
    name: str,
    target_module: Any,
    attr: str,
    make_replacement: Callable[[Any], Any],
    check_fn: Callable[[Path], dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    original = getattr(target_module, attr)
    setattr(target_module, attr, make_replacement(original))
    try:
        report = check_fn(root)
    finally:
        setattr(target_module, attr, original)
    caught = report["oracle_mismatches"] > 0 and report["safety_violations"] > 0
    return {
        "name": name,
        "caught": caught,
        "oracle_mismatches": report["oracle_mismatches"],
        "safety_violations": report["safety_violations"],
        "sample_violation": report["sample_violation"],
    }


def _run_negative_controls(root: Path) -> list[dict[str, Any]]:
    def lean_drop_sorry_axiom(original: Callable[..., dict[str, frozenset[str]]]) -> Callable[..., dict[str, frozenset[str]]]:
        def mutated(audit: dict, module: str) -> dict[str, frozenset[str]]:
            records = original(audit, module)
            return {
                name: frozenset(ax for ax in axioms if ax != lean_adapter.SORRY_AXIOM)
                for name, axioms in records.items()
            }
        return mutated

    def lean_skip_recheck2(original: Callable[[bytes], lean_adapter.LeanRun | Blocked]) -> Callable[[bytes], lean_adapter.LeanRun | Blocked]:
        def mutated(raw: bytes) -> lean_adapter.LeanRun | Blocked:
            parsed = original(raw)
            if isinstance(parsed, Blocked):
                return parsed
            has_recheck = any(step.get("name") == "recheck:2" for step in parsed.steps)
            if has_recheck:
                return parsed
            build1 = next(step for step in parsed.steps if step.get("name") == "build:1")
            synthesized = {
                "name": "recheck:2",
                "exit_code": 0,
                "olean_digest": build1.get("olean_digest", LEAN_OLEAN_DIGEST),
                "messages": [],
            }
            return lean_adapter.LeanRun(
                parsed.module,
                parsed.source,
                parsed.profile,
                parsed.tool_version,
                parsed.challenge_sha256,
                parsed.theorems,
                (*parsed.steps, synthesized),
            )
        return mutated

    def tlc_ignore_left_on_queue(original: Callable[[tlc_adapter.TlcRun], tlc_adapter.Exploration | None]) -> Callable[[tlc_adapter.TlcRun], tlc_adapter.Exploration | None]:
        def mutated(report: tlc_adapter.TlcRun) -> tlc_adapter.Exploration | None:
            exp = original(report)
            if exp is None:
                return None
            return tlc_adapter.Exploration(
                generated=exp.generated,
                distinct=exp.distinct,
                left_on_queue=0,
                depth=exp.depth,
                completed_banner=exp.completed_banner,
                violation_reported=exp.violation_reported,
            )
        return mutated

    def tlc_ignore_completion_banner(original: Callable[[tlc_adapter.TlcRun], tlc_adapter.Exploration | None]) -> Callable[[tlc_adapter.TlcRun], tlc_adapter.Exploration | None]:
        def mutated(report: tlc_adapter.TlcRun) -> tlc_adapter.Exploration | None:
            exp = original(report)
            if exp is None:
                return None
            return tlc_adapter.Exploration(
                generated=exp.generated,
                distinct=exp.distinct,
                left_on_queue=exp.left_on_queue,
                depth=exp.depth,
                completed_banner=True,
                violation_reported=exp.violation_reported,
            )
        return mutated

    return [
        _run_mutant(
            "lean_drop_sorry_axiom",
            lean_adapter,
            "_audit_records",
            lean_drop_sorry_axiom,
            _check_lean_space,
            root,
        ),
        _run_mutant(
            "lean_skip_recheck2",
            lean_adapter,
            "_read",
            lean_skip_recheck2,
            _check_lean_space,
            root,
        ),
        _run_mutant(
            "tlc_ignore_left_on_queue",
            tlc_adapter,
            "_exploration",
            tlc_ignore_left_on_queue,
            _check_tlc_space,
            root,
        ),
        _run_mutant(
            "tlc_ignore_completion_banner",
            tlc_adapter,
            "_exploration",
            tlc_ignore_completion_banner,
            _check_tlc_space,
            root,
        ),
    ]


def run() -> dict[str, Any]:
    """Run exhaustive Lean and TLC interpreter checks plus negative-control mutants."""
    with tempfile.TemporaryDirectory(prefix="vkit_formal_interpreters_") as tmp:
        root = Path(tmp)
        lean_summary = _check_lean_space(root)
        tlc_summary = _check_tlc_space(root)
        mutants = _run_negative_controls(root)

    all_mutants_caught = all(item["caught"] for item in mutants)
    ok = (
        lean_summary["oracle_mismatches"] == 0
        and lean_summary["safety_violations"] == 0
        and tlc_summary["oracle_mismatches"] == 0
        and tlc_summary["safety_violations"] == 0
        and all_mutants_caught
    )
    return {
        "ok": ok,
        "lean": lean_summary,
        "tlc": tlc_summary,
        "negative_controls": mutants,
        "all_mutants_caught": all_mutants_caught,
    }


def main() -> int:
    result = run()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .certification import (
    DifferenceAssessment,
    DifferencePolicy,
    PairedObservation,
    assess_differences,
)
from .evidence import _retained_path, is_link
from .inputs import CapturedSource
from .intent import BoundChangeRequest, _prepare_change_request
from .paired import (
    PairedExecution,
    _artifact_mismatches,
    _json_bytes,
    _json_value,
    _parse_output,
    _run_prepared_paired_execution,
    _select_inputs,
)
from .paths import Project
from .schemas import validate
from .witness import WitnessBinding

_POLICY_DIRECTORY = "verification/change-intents/differences"
_REPORT_SCHEMA = "paired-execution-report.v1.json"
_ANALYSIS_SCHEMA = "change-analysis.v1.json"
_SEMANTIC_ROLES = ("binding", "codec", "observation", "environment")


@dataclass(frozen=True)
class ChangeAnalysis:
    bound: BoundChangeRequest
    policy: DifferencePolicy
    policy_identity: dict[str, Any]
    paired_execution: PairedExecution
    assessment: DifferenceAssessment
    report: dict[str, Any]


def _policy_for_captures(
    bound: BoundChangeRequest,
    target: CapturedSource,
    candidate: CapturedSource,
) -> tuple[DifferencePolicy, dict[str, Any], bytes]:
    path = f"{_POLICY_DIRECTORY}/{bound.intent.digest}.v1.json"
    target_file = {item.path: item for item in target.files}.get(path)
    candidate_file = {item.path: item for item in candidate.files}.get(path)
    if target_file is None:
        if candidate_file is not None:
            raise ValueError("candidate adds a difference policy absent from the trusted target")
        raw_policy = {
            "schema_version": 1,
            "kind": "finite_differences",
            "intent_sha256": bound.intent.digest,
            "guards": [],
        }
        policy = DifferencePolicy.from_json(raw_policy, intent=bound.intent)
        raw = _json_bytes(policy.to_json())
        return policy, {
            "path": path,
            "target_tree_oid": bound.target_tree_oid,
            "blob_oid": None,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "policy_sha256": policy.digest,
            "default": True,
        }, raw
    if candidate_file is None or candidate_file.content != target_file.content:
        raise ValueError("candidate difference policy differs from the trusted target")
    policy = DifferencePolicy.from_json(
        _json_value(target_file.content, "target difference policy"),
        intent=bound.intent,
    )
    return policy, {
        "path": path,
        "target_tree_oid": bound.target_tree_oid,
        "blob_oid": target_file.blob_oid,
        "raw_sha256": target_file.raw_sha256,
        "policy_sha256": policy.digest,
        "default": False,
    }, target_file.content


def _retained_bytes(root: Path, reference: dict[str, Any]) -> bytes:
    path = _retained_path(root, reference["path"])
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f"retained artifact is not a regular file: {reference['path']}")
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"retained artifact is unavailable: {reference['path']}") from exc
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError(f"retained artifact hash mismatch: {reference['path']}")
    return raw


def _semantic_bytes(bound: BoundChangeRequest, target: CapturedSource) -> dict[str, bytes]:
    files = {item.path: item for item in target.files}
    result = {}
    for artifact in bound.intent.artifacts:
        captured = files.get(artifact.path)
        if captured is None or captured.raw_sha256 != artifact.sha256:
            raise ValueError(f"target {artifact.role} capture differs from accepted intent")
        result[artifact.role] = captured.content
    if set(result) != set(_SEMANTIC_ROLES):
        raise ValueError("accepted intent does not contain exactly four semantic artifacts")
    return result


def _report_observations(
    output_root: Path,
    raw_report: bytes,
    *,
    bound: BoundChangeRequest,
    target: CapturedSource,
    expected_binding: WitnessBinding | None,
    expected_selected: tuple[str, ...],
    repetitions: int,
) -> tuple[PairedObservation, ...]:
    report = _json_value(raw_report, "paired execution report")
    validate("paired execution report", _REPORT_SCHEMA, report)
    if _json_bytes(report) != raw_report:
        raise ValueError("paired execution report is not in canonical retained form")
    if report["request_sha256"] != bound.digest:
        raise ValueError("paired execution report names a different bound request")
    if report["source"] != {
        "base_oid": bound.request.base_oid,
        "candidate_oid": bound.request.candidate_oid,
        "base_capture_sha256": bound.base_capture_sha256,
        "candidate_capture_sha256": bound.candidate_capture_sha256,
    }:
        raise ValueError("paired execution report source identities differ from the bound request")
    if report["binding"] != (None if expected_binding is None else expected_binding.to_json()):
        raise ValueError("paired execution report differs from the live execution binding")
    if expected_binding is None:
        if report["samples"]:
            raise ValueError("paired samples have no independently supplied execution binding")
    else:
        for role, digest in (
            ("runner", expected_binding.runner_identity_sha256),
            ("runtime", expected_binding.runtime_identity_sha256),
        ):
            reference = report["identities"][role]
            if (reference is None or reference["path"] != f"identities/{role}.json"
                    or reference["sha256"] != digest):
                raise ValueError(f"paired {role} identity differs from the live execution binding")
    if report["input_selection"]["selected"] != list(expected_selected):
        raise ValueError("paired execution report selected inputs differ from this run")
    if report["repetitions"] != repetitions:
        raise ValueError("paired execution report repetition count differs from this run")

    mismatches = _artifact_mismatches(output_root, report)
    if mismatches:
        raise ValueError("paired execution retained artifacts changed: " + ", ".join(mismatches))
    semantics = _semantic_bytes(bound, target)
    if set(report["semantic_artifacts"]) != set(_SEMANTIC_ROLES):
        raise ValueError("paired execution report does not bind all four semantic artifacts")
    for role, content in semantics.items():
        reference = report["semantic_artifacts"][role]
        expected_path = f"semantics/{role}.json"
        if reference["path"] != expected_path or _retained_bytes(output_root, reference) != content:
            raise ValueError(f"retained {role} semantic artifact differs from the target capture")

    admissible = set(bound.intent.contract.admissible_inputs)
    selected_seen = []
    seen = set()
    observations = []
    for sample in report["samples"]:
        token = sample["input"]
        purpose = sample["purpose"]
        if token in seen:
            raise ValueError(f"paired execution report repeats input {token!r}")
        seen.add(token)
        if token not in admissible:
            raise ValueError(f"paired execution report samples inadmissible input {token!r}")
        if purpose == "selected":
            selected_seen.append(token)
        elif purpose != "shrink":
            raise ValueError(f"paired execution report has unknown sample purpose {purpose!r}")
        if purpose == "shrink" and token in expected_selected:
            raise ValueError("paired execution report relabels a selected input as shrink evidence")

        input_raw = _retained_bytes(output_root, sample["input_artifact"])
        try:
            numeric_input = int(token)
        except ValueError as exc:
            raise ValueError(f"paired report input is not a decimal u32: {token!r}") from exc
        if (str(numeric_input) != token or not 0 <= numeric_input <= 2**32 - 1
                or input_raw != numeric_input.to_bytes(4, "little")):
            raise ValueError(f"retained input artifact does not encode {token!r}")

        base_values: list[str] = []
        candidate_values: list[str] = []
        failure = None
        runs = sample["runs"]
        if len(runs) > repetitions:
            raise ValueError(f"paired sample {token!r} has too many repetitions")
        for index, run in enumerate(runs, 1):
            if run["repetition"] != index:
                raise ValueError(f"paired sample {token!r} has invalid repetition order")
            for side, values in (("base", base_values), ("candidate", candidate_values)):
                output = run[side]
                if output is None:
                    continue
                reference = output["stdout"]
                if output["outcome"] == "incomplete":
                    if output["value"] is not None:
                        raise ValueError("paired report marks an incomplete output as validated")
                    if reference is not None:
                        _retained_bytes(output_root, reference)
                    failure = failure or "incomplete_output"
                    continue
                if reference is None:
                    raise ValueError("paired report claims a validated output without retained stdout")
                if output["value"] is None:
                    raise ValueError("paired report marks a validated output without a value")
                stdout = _retained_bytes(output_root, reference)
                try:
                    value = _parse_output(stdout)
                except ValueError:
                    raise ValueError("paired report validated output has malformed retained stdout")
                if output["value"] != value:
                    raise ValueError("paired report output value differs from retained stdout")
                values.append(str(value))
        observations.append(PairedObservation(
            token,
            tuple(base_values),
            tuple(candidate_values),
            failure,
            purpose == "selected",
        ))
    if selected_seen != list(expected_selected[:len(selected_seen)]):
        raise ValueError("paired execution report selected inputs are out of order")
    return tuple(observations)


def _analysis_report(
    bound: BoundChangeRequest,
    policy_identity: dict[str, Any],
    paired_report_sha256: str,
    assessment: DifferenceAssessment,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "change_analysis",
        "scope": "sampled_change_analysis",
        "status": assessment.status,
        "source_proof": False,
        "approval_authority": False,
        "request_sha256": bound.digest,
        "source": {
            "base_oid": bound.request.base_oid,
            "candidate_oid": bound.request.candidate_oid,
            "target_tree_oid": bound.target_tree_oid,
            "base_capture_sha256": bound.base_capture_sha256,
            "candidate_capture_sha256": bound.candidate_capture_sha256,
        },
        "policy": policy_identity,
        "paired_report": {
            "path": "paired-report.json",
            "sha256": paired_report_sha256,
        },
        "assessment": assessment.to_json(),
    }


def run_change_analysis(
    project: Project,
    raw_request: object,
    *,
    trusted_target_oid: str,
    output_dir: Path,
    input_tokens: tuple[str, ...] | None = None,
    repetitions: int = 3,
    timeout_seconds: float = 10,
    max_shrink_trials: int = 20,
    should_cancel: Callable[[], bool] = lambda: False,
) -> ChangeAnalysis:
    bound, target, candidate = _prepare_change_request(
        project, raw_request, trusted_target_oid=trusted_target_oid
    )
    return _run_prepared_change_analysis(
        bound,
        target,
        candidate,
        output_dir=output_dir,
        input_tokens=input_tokens,
        repetitions=repetitions,
        timeout_seconds=timeout_seconds,
        max_shrink_trials=max_shrink_trials,
        should_cancel=should_cancel,
    )


def _run_prepared_change_analysis(
    bound: BoundChangeRequest,
    target: CapturedSource,
    candidate: CapturedSource,
    *,
    output_dir: Path,
    input_tokens: tuple[str, ...] | None = None,
    repetitions: int = 3,
    timeout_seconds: float = 10,
    max_shrink_trials: int = 20,
    should_cancel: Callable[[], bool] = lambda: False,
) -> ChangeAnalysis:
    policy, policy_identity, policy_bytes = _policy_for_captures(bound, target, candidate)
    accepted_inputs = tuple(sorted(bound.intent.contract.admissible_inputs, key=int))
    selected_request = accepted_inputs if input_tokens is None else input_tokens
    if not isinstance(selected_request, tuple) or not set(selected_request) <= set(accepted_inputs):
        raise ValueError("selected inputs must be drawn from the accepted intent domain")
    selected = _select_inputs(bound, selected_request)
    paired_execution = _run_prepared_paired_execution(
        bound,
        target,
        candidate,
        output_dir=output_dir,
        input_tokens=selected,
        repetitions=repetitions,
        timeout_seconds=timeout_seconds,
        max_shrink_trials=max_shrink_trials,
        should_cancel=should_cancel,
    )
    output_root = Path(output_dir).absolute()
    paired_path = _retained_path(output_root, "paired-report.json")
    if is_link(paired_path) or not stat.S_ISREG(paired_path.lstat().st_mode):
        raise ValueError("paired execution report is not a retained regular file")
    paired_raw = paired_path.read_bytes()
    if paired_raw != _json_bytes(paired_execution.report):
        raise ValueError("retained paired execution report differs from its live result")
    policy_directory = output_root / "policy"
    if is_link(policy_directory):
        raise ValueError("policy artifact directory is a filesystem link")
    policy_directory.mkdir(exist_ok=True)
    if is_link(policy_directory) or not policy_directory.is_dir():
        raise ValueError("policy artifact directory is not a plain directory")
    policy_path = policy_directory / "difference-policy.json"
    with policy_path.open("xb") as stream:
        stream.write(policy_bytes)
    policy_reference = {
        "path": "policy/difference-policy.json",
        "sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "media_type": "application/json",
    }
    if _retained_bytes(output_root, policy_reference) != policy_bytes:
        raise ValueError("retained policy artifact differs from the trusted target policy")
    policy_identity = {**policy_identity, "artifact": policy_reference}
    observations = _report_observations(
        output_root,
        paired_raw,
        bound=bound,
        target=target,
        expected_binding=paired_execution.binding,
        expected_selected=selected,
        repetitions=repetitions,
    )
    assessment = assess_differences(
        bound.intent, policy, observations, repetitions=repetitions
    )
    if (paired_execution.status in {"unknown", "infrastructure_failure"}
            and assessment.status != "observed_violation"):
        assessment = DifferenceAssessment(
            "unknown",
            assessment.coverage_complete,
            assessment.findings,
            assessment.missing_inputs,
        )
    paired_digest = hashlib.sha256(paired_raw).hexdigest()
    analysis_report = _analysis_report(bound, policy_identity, paired_digest, assessment)
    validate("change analysis report", _ANALYSIS_SCHEMA, analysis_report)
    analysis_path = output_root / "change-analysis.json"
    with analysis_path.open("xb") as stream:
        stream.write(_json_bytes(analysis_report))
    return ChangeAnalysis(
        bound, policy, policy_identity, paired_execution, assessment, analysis_report
    )

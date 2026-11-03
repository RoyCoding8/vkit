from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .change_analysis import _run_prepared_change_analysis
from .change_methods import ChangeMethodPlan, FiniteMethod, KaniMethod
from .evidence import _retained_path
from .inputs import CapturedSource
from .intent import BoundChangeRequest
from .kani_execution import run_kani_relational
from .paired import _artifact, _artifact_mismatches, _fresh_directory, _json_bytes, _write_artifact
from .rust_relational import _prepare_captured_rust_relational


def _conclusion(results: list[dict[str, Any]]) -> str:
    statuses = {item["status"] for item in results}
    for failure in ("observed_violation", "proof_rejected"):
        if failure in statuses:
            return failure
    if not statuses:
        return "unsupported"
    if statuses - {"observed_conformance", "verified"}:
        return "unknown"
    return "model_verified" if "verified" in statuses else "observed_conformance"


def run_change_methods(
    bound: BoundChangeRequest, base: CapturedSource, candidate: CapturedSource,
    methods: ChangeMethodPlan, *, output_dir: Path,
    should_cancel: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    root = _fresh_directory(output_dir)
    results: list[dict[str, Any]] = []
    stopped = None
    for index, method in enumerate(methods.steps):
        if should_cancel():
            stopped = "cancelled"
            break
        name = "finite_paired" if isinstance(method, FiniteMethod) else "rust_kani"
        directory = root / f"{index + 1:02d}-{name}"
        try:
            if isinstance(method, FiniteMethod):
                analysis = _run_prepared_change_analysis(
                    bound, base, candidate, output_dir=directory,
                    input_tokens=method.input_tokens, repetitions=method.repetitions,
                    timeout_seconds=method.timeout_seconds, max_shrink_trials=method.max_shrink_trials,
                    should_cancel=should_cancel,
                )
                path = _retained_path(directory, "change-analysis.json")
                if path.read_bytes() != _json_bytes(analysis.report):
                    raise ValueError("retained finite analysis differs from its live result")
                row = {"method": name, "status": analysis.assessment.status,
                       "scope": "sampled_native_observations",
                       "coverage_complete": analysis.assessment.coverage_complete,
                       "report": _artifact(root, path, "application/json").to_json()}
            elif isinstance(method, KaniMethod):
                program = _prepare_captured_rust_relational(bound, base, candidate)
                execution = run_kani_relational(
                    program, toolchain=method.toolchain, output_dir=directory,
                    timeout_seconds=method.timeout_seconds, should_cancel=should_cancel,
                )
                ref = _artifact(root, execution.report_path, "application/json")
                if ref.sha256 != execution.report_sha256:
                    raise ValueError("retained Kani report differs from its live result")
                row = {"method": name, "status": execution.kind, "reason": execution.reason,
                       "scope": "restricted_captured_rust_kani_model", "report": ref.to_json()}
            else:
                raise TypeError("unsupported internal change method")
        except (OSError, ValueError) as exc:
            ref = _write_artifact(root, f"{index + 1:02d}-failure.json", _json_bytes({
                "method": name, "error": str(exc), "source_proof": False, "approval_authority": False,
            }), "application/json")
            row = {"method": name, "status": "unknown", "reason": "method_could_not_complete",
                   "scope": "incomplete_method", "report": ref.to_json()}
        results.append(row)
        if row["status"] in {"observed_violation", "proof_rejected"}:
            stopped = "checked_refutation"
            break
    report = {
        "schema_version": 1, "scope": "shadow_change_certification",
        "bound_request_sha256": bound.digest,
        "intent_sha256": bound.intent.digest,
        "requirement": {"source": bound.intent.requirement_source,
                        "statement": bound.intent.requirement_statement},
        "base_capture_sha256": base.digest, "candidate_capture_sha256": candidate.digest,
        "method_plan": methods.to_json(), "methods": results,
        "status": _conclusion(results), "stopped": stopped,
        "source_proof": False, "approval_authority": False,
        "native_compiler_correspondence": False, "aggregate_deadline": False,
    }
    if stopped == "cancelled" or should_cancel():
        if report["status"] not in {"observed_violation", "proof_rejected"}:
            report.update(status="unknown", stopped="cancelled")
    if _artifact_mismatches(root, report):
        report.update(status="unknown", stopped="retained_artifact_changed")
    _write_artifact(root, "change-certification.json", _json_bytes(report), "application/json")
    return report

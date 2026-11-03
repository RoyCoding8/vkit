from __future__ import annotations

import hashlib
import json
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from . import proc
from .evidence import _retained_path, is_link, policy_identity, sha256 as _file_sha256
from .inputs import CapturedSource
from .intent import BoundChangeRequest, _prepare_change_request, _unique_object
from .paired_profiles import (
    ExecutionEnvironment,
    ProgramPlan,
    U32Binding,
    identify_environment,
    parse_binding,
    parse_environment,
    prepare_program,
    validate_environment,
)
from .paths import Project
from .schemas import SchemaValidationError, validate
from .witness import ArtifactRef, WitnessBinding

PairedStatus = Literal[
    "samples_within_contract",
    "observed_violation",
    "unknown",
    "infrastructure_failure",
]

_U32 = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_U32_LINE = re.compile(rb"(?:0|[1-9][0-9]*)\n\Z")
_MAX_U32 = 2**32 - 1
_MAX_INPUTS = 4096
_MAX_REPETITIONS = 32
_MAX_TIMEOUT_SECONDS = 300
_MAX_SHRINK_TRIALS = 256
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_PROCESS_INVOCATIONS = 128
_MAX_RUN_SECONDS = 300
_SEMANTIC_ROLES = ("binding", "codec", "observation", "environment")
_RESOURCE_SCOPE = (
    "Process stdout/stderr and paired-run wall time only; Git capture, generated files, "
    "and retained binaries have no OS quota."
)


@dataclass(frozen=True)
class PairedExecution:
    status: PairedStatus
    binding: WitnessBinding | None
    report: dict[str, Any]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _json_value(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _artifact(root: Path, path: Path, media_type: str) -> ArtifactRef:
    try:
        relative = path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError as exc:
        raise ValueError(f"artifact is outside the paired execution directory: {path}") from exc
    retained = _retained_path(root, relative)
    try:
        mode = retained.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"artifact is unavailable: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"artifact is not a regular file: {path}")
    return ArtifactRef(relative, _file_sha256(retained), media_type)


def _write_artifact(root: Path, relative: str, content: bytes, media_type: str) -> ArtifactRef:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
    return _artifact(root, path, media_type)


def _fresh_directory(path: Path) -> Path:
    resolved = path.absolute()
    resolved.mkdir(parents=True, exist_ok=False)
    if is_link(resolved) or not resolved.is_dir():
        raise ValueError(f"paired execution directory is not a plain directory: {resolved}")
    return resolved


def _semantic_files(
    bound: BoundChangeRequest,
    base: CapturedSource,
    candidate: CapturedSource,
) -> dict[str, bytes]:
    if (base.kind != "git_commit" or candidate.kind != "git_commit"
            or base.commit != bound.request.base_oid
            or candidate.commit != bound.request.candidate_oid
            or base.digest != bound.base_capture_sha256
            or candidate.digest != bound.candidate_capture_sha256):
        raise ValueError("captured source identities differ from the preflight request")
    base_files = {item.path: item for item in base.files}
    candidate_files = {item.path: item for item in candidate.files}
    semantic = {}
    for artifact in bound.intent.artifacts:
        base_file = base_files.get(artifact.path)
        candidate_file = candidate_files.get(artifact.path)
        if (base_file is None or base_file.raw_sha256 != artifact.sha256
                or candidate_file is None or candidate_file.raw_sha256 != artifact.sha256):
            raise ValueError(f"captured {artifact.role} artifact differs from the accepted intent")
        semantic[artifact.role] = base_file.content
    if set(semantic) != set(_SEMANTIC_ROLES):
        raise ValueError("accepted intent does not contain exactly four semantic artifacts")
    return semantic


def _u32_token(value: object) -> int:
    if not isinstance(value, str) or _U32.fullmatch(value) is None:
        raise ValueError(f"input token is not a canonical decimal u32: {value!r}")
    number = int(value)
    if number > _MAX_U32:
        raise ValueError(f"input token is outside the u32 range: {value!r}")
    return number


def _select_inputs(bound: BoundChangeRequest, tokens: tuple[str, ...] | None) -> tuple[str, ...]:
    admitted = bound.request.claim.admissible_inputs
    if tokens is not None and not isinstance(tokens, tuple):
        raise ValueError("input_tokens must be a tuple of canonical input strings")
    selected = admitted if tokens is None else tokens
    if not selected:
        raise ValueError("paired execution requires at least one selected input")
    if len(selected) > _MAX_INPUTS:
        raise ValueError(f"paired execution accepts at most {_MAX_INPUTS} selected inputs")
    for token in selected:
        _u32_token(token)
    if len(set(selected)) != len(selected):
        raise ValueError("selected inputs must be unique")
    for token in selected:
        if token not in admitted:
            raise ValueError(f"selected input is not admissible in the trusted claim: {token!r}")
    return tuple(sorted(selected, key=int))


def _environment_json(environment: ExecutionEnvironment) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "local_process",
        "tools": {
            role: {"path": pin.path, "sha256": pin.sha256}
            for role, pin in environment.tools
        },
        "dependencies": [
            {"path": pin.path, "sha256": pin.sha256}
            for pin in environment.dependencies
        ],
        "variables": dict(environment.variables),
    }


def _runtime_arguments(
    binding: U32Binding, environment: ExecutionEnvironment
) -> tuple[Path | None, Path | None, tuple[Path, ...]]:
    tools = dict(environment.tools)
    if binding.language == "rust":
        compiler, runtime = tools["compiler"].path, None
    elif binding.language == "python":
        compiler, runtime = None, tools["runtime"].path
    else:
        compiler, runtime = tools["compiler"].path, tools["runtime"].path
    dependencies = tuple(Path(pin.path) for pin in environment.dependencies)
    return (
        Path(compiler) if compiler is not None else None,
        Path(runtime) if runtime is not None else None,
        dependencies,
    )


def _tree_files(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if is_link(path):
            raise ValueError(f"paired execution tree contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            files[relative] = _sha256(path.read_bytes())
    return files


def _capture_tree(root: Path, output_root: Path, report: dict[str, Any]) -> dict[str, str]:
    files = _tree_files(root)
    for relative, digest in files.items():
        report["artifacts"].append(
            _artifact(output_root, root / relative, "application/octet-stream").to_json()
        )
        if digest != report["artifacts"][-1]["sha256"]:
            raise ValueError(f"paired execution artifact changed while recording: {relative}")
    return files


def _trees_unchanged(
    plans: tuple[tuple[str, CapturedSource, Path, Path, ProgramPlan], ...],
    built: dict[str, dict[str, str]],
) -> bool:
    try:
        return all(
            _source_matches(source, source_root)
            and _tree_files(build_root) == built[side]
            for side, source, source_root, build_root, _ in plans
        )
    except (OSError, ValueError):
        return False


def _source_matches(source: CapturedSource, root: Path) -> bool:
    expected = {item.path: item.raw_sha256 for item in source.files}
    try:
        actual = _tree_files(root)
        modes_match = all(
            (root.joinpath(*item.path.split("/")).stat().st_mode & 0o111 != 0)
            == (item.mode == "100755")
            for item in source.files
        )
    except (OSError, ValueError):
        return False
    return actual == expected and modes_match


def _validate_domain(bound: BoundChangeRequest) -> None:
    domain = bound.request.claim.domain
    if len(domain.inputs) > _MAX_INPUTS:
        raise ValueError(f"u32 domains accept at most {_MAX_INPUTS} input tokens")
    if len(domain.observations) > _MAX_INPUTS:
        raise ValueError(f"u32 domains accept at most {_MAX_INPUTS} observation tokens")
    for token in (*domain.inputs, *domain.observations, *bound.request.claim.admissible_inputs):
        _u32_token(token)
    for row in bound.request.claim.rows:
        _u32_token(row.input)
        for output in row.outputs:
            _u32_token(output)


def _record_failure(
    report: dict[str, Any], stage: str, classification: str, reason: str, message: str,
    artifacts: tuple[ArtifactRef, ...] = (),
) -> None:
    report["failures"].append({
        "stage": stage,
        "classification": classification,
        "reason": reason,
        "message": message,
        "artifacts": [item.to_json() for item in artifacts],
    })


def _final_status(report: dict[str, Any]) -> PairedStatus:
    classifications = {item["classification"] for item in report["failures"]}
    if "infrastructure_failure" in classifications:
        return "infrastructure_failure"
    if "unknown" in classifications:
        return "unknown"
    if any(item["candidate_within_contract"] is False for item in report["samples"]):
        return "observed_violation"
    return "samples_within_contract"


def _report_base(
    bound: BoundChangeRequest,
    selected: tuple[str, ...],
    repetitions: int,
    max_shrink_trials: int,
) -> dict[str, Any]:
    admissible = bound.request.claim.admissible_inputs
    return {
        "schema_version": 1,
        "kind": "paired_execution",
        "scope": "sampled_paired_execution",
        "status": "unknown",
        "source_proof": False,
        "dependency_closure_checked": False,
        "os_sandbox": False,
        "request_sha256": bound.digest,
        "binding": None,
        "source": {
            "base_oid": bound.request.base_oid,
            "candidate_oid": bound.request.candidate_oid,
            "base_capture_sha256": bound.base_capture_sha256,
            "candidate_capture_sha256": bound.candidate_capture_sha256,
        },
        "input_selection": {
            "admissible_count": len(admissible),
            "selected": list(selected),
            "omitted_count": len(admissible) - len(selected),
        },
        "repetitions": repetitions,
        "resource_limits": {
            "max_process_invocations": _MAX_PROCESS_INVOCATIONS,
            "max_output_bytes_per_process": _MAX_OUTPUT_BYTES,
            "max_total_process_output_bytes": _MAX_PROCESS_INVOCATIONS * _MAX_OUTPUT_BYTES,
            "wall_deadline_seconds": _MAX_RUN_SECONDS,
        },
        "resource_usage": {
            "process_invocations": 0,
            "process_output_bytes": 0,
            "elapsed_seconds": 0.0,
        },
        "resource_scope": _RESOURCE_SCOPE,
        "samples": [],
        "shrinking": {
            "limit": max_shrink_trials,
            "seed_input": None,
            "reduced_input": None,
            "attempted_inputs": [],
            "complete": True,
        },
        "identities": {"runner": None, "runtime": None},
        "semantic_artifacts": {},
        "builds": [],
        "failures": [],
        "artifacts": [],
    }


def _artifact_refs(value: object):
    if isinstance(value, dict):
        if set(value) == {"path", "sha256", "media_type"}:
            yield value
        else:
            for nested in value.values():
                yield from _artifact_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _artifact_refs(nested)


def _artifact_mismatches(root: Path, report: dict[str, Any]) -> tuple[str, ...]:
    mismatched = set()
    seen = set()
    for ref in _artifact_refs(report):
        key = (ref["path"], ref["sha256"])
        if key in seen:
            continue
        seen.add(key)
        try:
            path = _retained_path(root, ref["path"])
            if not stat.S_ISREG(path.lstat().st_mode) or _file_sha256(path) != ref["sha256"]:
                mismatched.add(ref["path"])
        except (OSError, ValueError):
            mismatched.add(ref["path"])
    return tuple(sorted(mismatched))


def _save_report(
    output_dir: Path,
    report: dict[str, Any],
    *,
    started: float,
    deadline: float,
    live_binding: WitnessBinding | None,
) -> PairedExecution:
    mismatches = _artifact_mismatches(output_dir, report)
    if mismatches:
        _record_failure(
            report,
            "retained_artifacts",
            "infrastructure_failure",
            "retained_artifact_mismatch",
            "retained artifact bytes differ from recorded hashes: " + ", ".join(mismatches),
        )
    elapsed = time.monotonic() - started
    report["resource_usage"]["elapsed_seconds"] = round(elapsed, 3)
    if (elapsed >= deadline - started
            and not any(item["reason"] == "wall_deadline" for item in report["failures"])):
        _record_failure(
            report, "budget", "unknown", "wall_deadline",
            "paired execution wall deadline expired",
        )
    report["status"] = _final_status(report)
    validate("paired execution report", "paired-execution-report.v1.json", report)
    with (output_dir / "paired-report.json").open("xb") as stream:
        stream.write(_json_bytes(report))
    return PairedExecution(report["status"], live_binding, report)


def _store_failure(
    output_dir: Path,
    report: dict[str, Any],
    stage: str,
    classification: str,
    reason: str,
    message: str,
    detail: bytes = b"",
) -> None:
    artifacts = ()
    if detail:
        name = f"diagnostics/{len(report['failures']):04d}.txt"
        artifacts = (_write_artifact(output_dir, name, detail, "text/plain"),)
        report["artifacts"].append(artifacts[0].to_json())
    _record_failure(report, stage, classification, reason, message, artifacts)


def _validate_limits(
    repetitions: int,
    timeout_seconds: float,
    max_shrink_trials: int,
) -> None:
    if type(repetitions) is not int or not 1 <= repetitions <= _MAX_REPETITIONS:
        raise ValueError(f"repetitions must be an integer from 1 to {_MAX_REPETITIONS}")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS):
        raise ValueError(f"timeout_seconds must be greater than 0 and at most {_MAX_TIMEOUT_SECONDS}")
    if (type(max_shrink_trials) is not int
            or not 0 <= max_shrink_trials <= _MAX_SHRINK_TRIALS):
        raise ValueError(f"max_shrink_trials must be an integer from 0 to {_MAX_SHRINK_TRIALS}")


def run_paired_execution(
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
) -> PairedExecution:
    _validate_limits(repetitions, timeout_seconds, max_shrink_trials)
    bound, base_capture, candidate_capture = _prepare_change_request(
        project, raw_request, trusted_target_oid=trusted_target_oid
    )
    return _run_prepared_paired_execution(
        bound,
        base_capture,
        candidate_capture,
        output_dir=output_dir,
        input_tokens=input_tokens,
        repetitions=repetitions,
        timeout_seconds=timeout_seconds,
        max_shrink_trials=max_shrink_trials,
        should_cancel=should_cancel,
    )


def _run_prepared_paired_execution(
    bound: BoundChangeRequest,
    base_capture: CapturedSource,
    candidate_capture: CapturedSource,
    *,
    output_dir: Path,
    input_tokens: tuple[str, ...] | None = None,
    repetitions: int = 3,
    timeout_seconds: float = 10,
    max_shrink_trials: int = 20,
    should_cancel: Callable[[], bool] = lambda: False,
) -> PairedExecution:
    _validate_limits(repetitions, timeout_seconds, max_shrink_trials)
    started = time.monotonic()
    deadline = started + _MAX_RUN_SECONDS
    selected = _select_inputs(bound, input_tokens)
    output_root = _fresh_directory(Path(output_dir))
    report = _report_base(bound, selected, repetitions, max_shrink_trials)
    live_binding: WitnessBinding | None = None

    def save_report() -> PairedExecution:
        return _save_report(
            output_root, report, started=started, deadline=deadline,
            live_binding=live_binding,
        )

    try:
        semantic = _semantic_files(bound, base_capture, candidate_capture)
        semantic_artifacts = {}
        semantic_json = {}
        for role in _SEMANTIC_ROLES:
            raw = semantic[role]
            ref = _write_artifact(output_root, f"semantics/{role}.json", raw, "application/json")
            semantic_artifacts[role] = ref.to_json()
            semantic_json[role] = _json_value(raw, f"{role} semantic artifact")
            report["artifacts"].append(ref.to_json())
        report["semantic_artifacts"] = semantic_artifacts
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _store_failure(output_root, report, "binding", "unknown", "semantic_artifact", str(exc))
        return save_report()

    try:
        binding_spec = parse_binding(semantic_json["binding"])
        codec = semantic_json["codec"]
        observation = semantic_json["observation"]
        if (not isinstance(codec, dict) or type(codec.get("schema_version")) is not int
                or codec != {"schema_version": 1, "kind": "u32_le"}):
            raise ValueError("unsupported codec declaration")
        if (not isinstance(observation, dict)
                or type(observation.get("schema_version")) is not int
                or observation != {"schema_version": 1, "kind": "u32_return"}):
            raise ValueError("unsupported observation declaration")
        _validate_domain(bound)
        environment = parse_environment(semantic_json["environment"], binding_spec.language)
    except (ValueError, SchemaValidationError, KeyError, TypeError) as exc:
        _store_failure(output_root, report, "binding", "unknown", "unsupported_declaration", str(exc))
        return save_report()

    base_root = output_root / "base"
    candidate_root = output_root / "candidate"
    base_source_root = base_root / "source"
    candidate_source_root = candidate_root / "source"
    base_build_root = base_root / "build"
    candidate_build_root = candidate_root / "build"
    for path in (base_root, candidate_root, base_source_root, candidate_source_root,
                 base_build_root, candidate_build_root):
        path.mkdir(parents=True, exist_ok=False)
    try:
        base_capture.materialize(base_source_root)
        candidate_capture.materialize(candidate_source_root)
        base_plan = prepare_program(binding_spec, base_source_root, base_build_root, environment)
        candidate_plan = prepare_program(
            binding_spec, candidate_source_root, candidate_build_root, environment
        )
        runner_identity = _runner_identity(
            (("base", base_plan, base_build_root),
             ("candidate", candidate_plan, candidate_build_root))
        )
        runner_ref = _write_artifact(
            output_root, "identities/runner.json", runner_identity, "application/json"
        )
        report["identities"]["runner"] = runner_ref.to_json()
        report["artifacts"].append(runner_ref.to_json())
    except (OSError, ValueError, SchemaValidationError, KeyError, TypeError) as exc:
        _store_failure(output_root, report, "prepare", "unknown", "unsupported_program", str(exc))
        return save_report()

    try:
        compiler, runtime, dependencies = _runtime_arguments(binding_spec, environment)
        measured_environment = identify_environment(
            binding_spec.language,
            compiler=compiler,
            runtime=runtime,
            dependencies=dependencies,
        )
        measured_environment.pop("variables", None)
        runtime_identity = _json_bytes({
            "schema_version": 1,
            "kind": "paired_runtime_identity",
            "environment": _environment_json(environment),
            "measured": measured_environment,
        })
        runtime_ref = _write_artifact(
            output_root, "identities/runtime.json", runtime_identity, "application/json"
        )
        report["identities"]["runtime"] = runtime_ref.to_json()
        report["artifacts"].append(runtime_ref.to_json())
        binding = WitnessBinding.from_bound_request(
            bound,
            runner_identity_sha256=runner_ref.sha256,
            runtime_identity_sha256=runtime_ref.sha256,
        )
        live_binding = binding
        report["binding"] = binding.to_json()
        validate_environment(environment)
    except (OSError, ValueError, SchemaValidationError, KeyError, TypeError) as exc:
        _store_failure(
            output_root, report, "environment", "infrastructure_failure", "tool_pin_mismatch", str(exc)
        )
        return save_report()

    plans = (
        ("base", base_capture, base_source_root, base_build_root, base_plan),
        ("candidate", candidate_capture, candidate_source_root, candidate_build_root, candidate_plan),
    )
    built: dict[str, dict[str, str]] = {}
    for side, source, source_root, build_root, plan in plans:
        build = {"side": side, "state": "not_required", "argv": [], "stdout": None, "stderr": None}
        if plan.build_argv is not None:
            build["argv"] = list(plan.build_argv)
            if not _source_matches(source, source_root):
                build["state"] = "failed"
                report["builds"].append(build)
                _store_failure(
                    output_root, report, "compile", "infrastructure_failure",
                    "source_changed", "materialized source changed before paired build",
                )
                return save_report()
            stdout_path = output_root / "commands" / f"{side}-build.stdout"
            stderr_path = output_root / "commands" / f"{side}-build.stderr"
            result, stdout_ref, stderr_ref, guard_failure = _run_process(
                environment, plan.build_argv, plan.cwd, output_root,
                stdout_path, stderr_path, timeout_seconds, should_cancel,
                report=report, deadline=deadline,
            )
            build["stdout"] = stdout_ref.to_json() if stdout_ref else None
            build["stderr"] = stderr_ref.to_json() if stderr_ref else None
            if stdout_ref:
                report["artifacts"].append(stdout_ref.to_json())
            if stderr_ref:
                report["artifacts"].append(stderr_ref.to_json())
            if guard_failure is not None:
                report["failures"].append(guard_failure)
                build["state"] = (
                    "not_run" if result is None else "failed"
                )
                report["builds"].append(build)
                return save_report()
            failure = _exit_failure(result, "compile")
            if failure is not None:
                build["state"] = "failed"
                report["builds"].append(build)
                _store_failure(
                    output_root, report, failure["stage"], failure["classification"],
                    failure["reason"], failure["message"],
                )
                return save_report()
            build["state"] = "built"
        report["builds"].append(build)
        try:
            if not _source_matches(source, source_root):
                raise ValueError("materialized source changed before paired playback")
            built[side] = _capture_tree(build_root, output_root, report)
        except (OSError, ValueError) as exc:
            _store_failure(output_root, report, "compile", "infrastructure_failure", "build_artifact", str(exc))
            return save_report()

    samples_by_input: dict[str, dict[str, Any]] = {}
    input_artifacts: dict[str, ArtifactRef] = {}

    def observe(token: str, purpose: str) -> dict[str, Any]:
        value = _u32_token(token)
        input_ref = input_artifacts.get(token)
        if input_ref is None:
            input_ref = _write_artifact(
                output_root, f"inputs/{token}.u32le", value.to_bytes(4, "little"),
                "application/octet-stream",
            )
            input_artifacts[token] = input_ref
            report["artifacts"].append(input_ref.to_json())
        sample: dict[str, Any] = {
            "input": token,
            "purpose": purpose,
            "input_artifact": input_ref.to_json(),
            "stable": None,
            "different": None,
            "in_observation_domain": None,
            "base_within_contract": None,
            "candidate_within_contract": None,
            "runs": [],
        }
        base_values = []
        candidate_values = []
        for repetition in range(1, repetitions + 1):
            row: dict[str, Any] = {"repetition": repetition, "base": None, "candidate": None}
            sample["runs"].append(row)
            for side, capture, source_root, build_root, plan in plans:
                if not _trees_unchanged(plans, built):
                    _record_failure(
                        report, "native_playback", "infrastructure_failure", "source_changed",
                        "source or build product changed during paired playback",
                    )
                    return sample
                stdout_path = output_root / "commands" / f"{side}-{token}-{repetition}.stdout"
                stderr_path = output_root / "commands" / f"{side}-{token}-{repetition}.stderr"
                result, stdout_ref, stderr_ref, guard_failure = _run_process(
                    environment, plan.run_argv, plan.cwd, output_root,
                    stdout_path, stderr_path, timeout_seconds, should_cancel, input_ref,
                    report=report, deadline=deadline,
                )
                output = {"outcome": "incomplete", "value": None,
                          "stdout": stdout_ref.to_json() if stdout_ref else None,
                          "stderr": stderr_ref.to_json() if stderr_ref else None}
                row[side] = output
                if stdout_ref:
                    report["artifacts"].append(stdout_ref.to_json())
                if stderr_ref:
                    report["artifacts"].append(stderr_ref.to_json())
                if guard_failure is not None:
                    report["failures"].append(guard_failure)
                    return sample
                failure = _exit_failure(result, "native_playback")
                if failure is not None:
                    _record_failure(
                        report, failure["stage"], failure["classification"],
                        failure["reason"], failure["message"],
                    )
                    return sample
                if not _trees_unchanged(plans, built):
                    _record_failure(
                        report, "native_playback", "infrastructure_failure", "source_changed",
                        "captured source or build product changed during paired playback",
                    )
                    return sample
                try:
                    raw_output = stdout_path.read_bytes()
                    output_value = _parse_output(raw_output)
                except (OSError, ValueError) as exc:
                    _record_failure(
                        report, "protocol", "infrastructure_failure", "malformed_output", str(exc),
                        (stdout_ref,) if stdout_ref else (),
                    )
                    return sample
                output["value"] = output_value
                output["outcome"] = "validated_output"
                if side == "base":
                    base_values.append(output_value)
                else:
                    candidate_values.append(output_value)
            if should_cancel():
                _record_failure(report, "native_playback", "unknown", "cancelled", "paired execution was cancelled")
                return sample
        base_stable = len(set(base_values)) == 1 and len(base_values) == repetitions
        candidate_stable = len(set(candidate_values)) == 1 and len(candidate_values) == repetitions
        sample["stable"] = base_stable and candidate_stable
        if not sample["stable"]:
            _record_failure(report, "native_playback", "unknown", "unstable", "repeated observations disagree")
            return sample
        base_value, candidate_value = base_values[0], candidate_values[0]
        sample["different"] = base_value != candidate_value
        output_tokens = {str(base_value), str(candidate_value)}
        domain_observations = set(bound.request.claim.domain.observations)
        sample["in_observation_domain"] = output_tokens <= domain_observations
        if not sample["in_observation_domain"]:
            _record_failure(
                report, "protocol", "unknown", "out_of_model",
                "an observed u32 is outside the declared observation domain",
            )
            return sample
        sample["base_within_contract"] = str(base_value) in bound.request.claim.outputs_for(token)
        sample["candidate_within_contract"] = (
            str(candidate_value) in bound.request.claim.outputs_for(token)
        )
        return sample

    for token in selected:
        sample = observe(token, "selected")
        samples_by_input[token] = sample
        report["samples"].append(sample)
        if sample["candidate_within_contract"] is None:
            break

    violation = next((token for token in selected
                      if samples_by_input.get(token, {}).get("candidate_within_contract") is False), None)
    if violation is not None and max_shrink_trials:
        shrink = report["shrinking"]
        shrink["complete"] = False
        shrink["seed_input"] = violation
        candidates = sorted(
            (token for token in bound.request.claim.admissible_inputs
             if _u32_token(token) < _u32_token(violation)),
            key=int,
        )
        trials = 0
        for token in candidates:
            if token in samples_by_input:
                shrink["attempted_inputs"].append({"input": token, "reused": True})
                sample = samples_by_input[token]
            else:
                if trials == max_shrink_trials:
                    shrink["complete"] = False
                    break
                trials += 1
                sample = observe(token, "shrink")
                samples_by_input[token] = sample
                report["samples"].append(sample)
                shrink["attempted_inputs"].append({"input": token, "reused": False})
            if sample["candidate_within_contract"] is None:
                shrink["complete"] = False
                break
            if sample["candidate_within_contract"] is False:
                if (shrink["reduced_input"] is None
                        or int(token) < int(shrink["reduced_input"])):
                    shrink["reduced_input"] = token
        else:
            shrink["complete"] = True
    elif violation is not None:
        report["shrinking"]["seed_input"] = violation
        report["shrinking"]["complete"] = False

    return save_report()


def _runner_identity(
    plans: tuple[tuple[str, ProgramPlan, Path], ...],
) -> bytes:
    generated = []
    for side, plan, build_root in plans:
        for path in plan.generated_files:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(build_root.resolve()).as_posix()
            generated.append({
                "side": side,
                "path": relative,
                "sha256": _sha256(resolved.read_bytes()),
            })
    return _json_bytes({
        "schema_version": 1,
        "kind": "paired_runner_identity",
        "implementation": policy_identity(),
        "generated_files": sorted(generated, key=lambda row: (row["side"], row["path"])),
    })


def _run_process(
    environment: ExecutionEnvironment,
    argv: tuple[str, ...],
    cwd: Path,
    output_root: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    should_cancel: Callable[[], bool],
    stdin_ref: ArtifactRef | None = None,
    *,
    report: dict[str, Any],
    deadline: float,
) -> tuple[proc.Exit | None, ArtifactRef | None, ArtifactRef | None, dict[str, str] | None]:
    try:
        validate_environment(environment)
    except (OSError, ValueError) as exc:
        return None, None, None, {
            "stage": "environment", "classification": "infrastructure_failure",
            "reason": "tool_pin_mismatch", "message": str(exc), "artifacts": [],
        }
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    usage = report["resource_usage"]
    limits = report["resource_limits"]
    if usage["process_invocations"] >= limits["max_process_invocations"]:
        return None, None, None, {
            "stage": "budget", "classification": "unknown",
            "reason": "process_budget_exhausted",
            "message": "paired execution process invocation limit reached", "artifacts": [],
        }
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, None, None, {
            "stage": "budget", "classification": "unknown", "reason": "wall_deadline",
            "message": "paired execution wall deadline expired", "artifacts": [],
        }
    stdin_path = None
    if stdin_ref is not None:
        try:
            stdin_path = _retained_path(output_root, stdin_ref.path)
            if not stat.S_ISREG(stdin_path.lstat().st_mode):
                raise ValueError("paired input artifact is not a regular file")
            expected_token = Path(stdin_ref.path).name.removesuffix(".u32le")
            expected_bytes = _u32_token(expected_token).to_bytes(4, "little")
            input_bytes = stdin_path.read_bytes()
            if input_bytes != expected_bytes or _sha256(input_bytes) != stdin_ref.sha256:
                raise ValueError("paired input artifact differs from its recorded four-byte value")
        except (OSError, ValueError) as exc:
            return None, None, None, {
                "stage": "retained_artifacts", "classification": "infrastructure_failure",
                "reason": "retained_artifact_mismatch", "message": str(exc), "artifacts": [],
            }
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, None, None, {
            "stage": "budget", "classification": "unknown", "reason": "wall_deadline",
            "message": "paired execution wall deadline expired", "artifacts": [],
        }
    wall_limited = remaining <= timeout_seconds
    process_timeout = min(float(timeout_seconds), remaining)
    usage["process_invocations"] += 1
    try:
        result = proc.run(
            argv,
            cwd=cwd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=process_timeout,
            env=dict(environment.variables),
            should_cancel=should_cancel,
            max_output_bytes=_MAX_OUTPUT_BYTES,
            stdin_path=stdin_path,
        )
    except (OSError, ValueError) as exc:
        result = proc.Exit(None, launch_error=str(exc))
    try:
        stdout_ref = _artifact(output_root, stdout_path, "text/plain")
        stderr_ref = _artifact(output_root, stderr_path, "text/plain")
    except (OSError, ValueError) as exc:
        return result, None, None, {
            "stage": "retained_artifacts", "classification": "infrastructure_failure",
            "reason": "retained_artifact_mismatch", "message": str(exc), "artifacts": [],
        }
    usage["process_output_bytes"] += stdout_path.stat().st_size + stderr_path.stat().st_size
    if time.monotonic() >= deadline:
        return result, stdout_ref, stderr_ref, {
            "stage": "budget", "classification": "unknown", "reason": "wall_deadline",
            "message": "paired execution wall deadline expired", "artifacts": [],
        }
    if wall_limited and result.timed_out:
        return result, stdout_ref, stderr_ref, {
            "stage": "budget", "classification": "unknown", "reason": "wall_deadline",
            "message": "paired execution wall deadline expired", "artifacts": [],
        }
    return result, stdout_ref, stderr_ref, None


def _exit_failure(result: proc.Exit | None, stage: str) -> dict[str, str] | None:
    if result is None:
        return {"stage": stage, "classification": "infrastructure_failure",
                "reason": "launch_error", "message": "process runner returned no result"}
    if result.cancelled:
        return {"stage": stage, "classification": "unknown", "reason": "cancelled",
                "message": "paired execution was cancelled"}
    if result.timed_out:
        return {"stage": stage, "classification": "unknown", "reason": "timeout",
                "message": "paired execution timed out"}
    if result.launch_error is not None:
        return {"stage": stage, "classification": "infrastructure_failure",
                "reason": "launch_error", "message": result.launch_error}
    if result.output_limited:
        return {"stage": stage, "classification": "infrastructure_failure",
                "reason": "output_limit", "message": "paired process exceeded the output limit"}
    if result.code != 0:
        return {"stage": stage, "classification": "infrastructure_failure",
                "reason": "exit_code", "message": f"paired process exited with code {result.code}"}
    return None


def _parse_output(raw: bytes) -> int:
    if _U32_LINE.fullmatch(raw) is None:
        raise ValueError("program output is not exactly one canonical decimal u32 line")
    value = int(raw[:-1])
    if value > _MAX_U32:
        raise ValueError("program output is outside the u32 range")
    return value

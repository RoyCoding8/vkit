"""Pinned external evidence production with a separately pinned replay checker."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .. import proc
from ..evidence import is_link
from ..inputs import canonical_digest, file_digest
from ..outcome import Blocked, BlockedReason, Outcome, Passed
from .spec import ExternalCheck, PinnedCommand, PinnedFile

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDERS = frozenset({"python", "snapshot", "run_dir", "request", "evidence", "receipt"})


class ExternalProtocolError(ValueError):
    """The pinned backend or checker does not satisfy the external evidence protocol."""


def identity(check: ExternalCheck) -> dict[str, Any]:
    """Resolve and verify every pinned executable and declared dependency."""
    from ..evidence import policy_identity

    return {
        "protocol": 1,
        "implementation": policy_identity(),
        "backend": _command_identity(check.backend, "backend"),
        "checker": _command_identity(check.checker, "checker"),
        "trusted_tool_dependencies": [
            _file_identity(item, "trusted tool dependency")
            for item in sorted(check.trusted_tool_dependencies, key=lambda pin: pin.path)
        ],
        "host": {
            "platform": platform.platform(),
            "system_root": os.environ.get("SYSTEMROOT", ""),
            "windir": os.environ.get("WINDIR", ""),
            "execution_environment": "vkit-external-v1",
        },
    }


def execute(
    check: ExternalCheck,
    run_dir: Path,
    job: dict[str, Any],
    should_cancel: Callable[[], bool],
) -> tuple[Outcome, dict[str, Any]]:
    """Produce evidence, then accept it only through the independently pinned checker."""
    try:
        current_identity = identity(check)
        snapshot, cwd, specification, job_files = _validate_job(check, run_dir, job, current_identity)
        _verify_snapshot(snapshot, job_files)
        evidence_dir = run_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for artifact in check.evidence_artifacts:
            _safe_relative(artifact, "evidence artifact")
        producer_request = run_dir / "producer-request.json"
        _write_json(producer_request, _request(check, job, specification, evidence_dir, current_identity))
        _ensure_identity(check, current_identity)
        started = time.monotonic()
        producer_exit = _run_command(
            check.backend,
            check,
            run_dir,
            snapshot,
            cwd,
            producer_request,
            evidence_dir,
            run_dir / "producer-receipt.json",
            "producer",
            should_cancel,
            check.timeout_seconds,
        )
        problem = _exit_problem(producer_exit, "backend")
        if problem is not None:
            return problem, _failure_metadata(check, job, current_identity, "backend", problem)
        _ensure_identity(check, current_identity)
        _verify_snapshot(snapshot, job_files)
        files = _evidence_files(evidence_dir, check.evidence_artifacts)
        evidence_digest = _evidence_digest(files)
        remaining = check.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return Blocked(BlockedReason.TIMEOUT, "producer exhausted the job deadline"), {}
        return _check_evidence(
            check,
            run_dir,
            job,
            current_identity,
            snapshot,
            cwd,
            specification,
            job_files,
            evidence_dir,
            files,
            evidence_digest,
            should_cancel,
            remaining,
        )
    except (ExternalProtocolError, OSError, ValueError) as exc:
        outcome = Blocked(BlockedReason.ARTIFACT_MALFORMED, str(exc))
        return outcome, _failure_metadata(check, job, None, "validation", outcome)


def replay(
    check: ExternalCheck,
    source_run_dir: Path,
    run_dir: Path,
    job: dict[str, Any],
    should_cancel: Callable[[], bool],
) -> tuple[Outcome, dict[str, Any]]:
    """Re-run the current checker against retained artifacts; stored status is ignored."""
    try:
        current_identity = identity(check)
        snapshot, cwd, specification, job_files = _validate_job(check, run_dir, job, current_identity)
        _verify_snapshot(snapshot, job_files)
        source_evidence = source_run_dir / "evidence"
        evidence_dir = run_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        files = _copy_evidence(source_evidence, evidence_dir, check.evidence_artifacts)
        evidence_digest = _evidence_digest(files)
        return _check_evidence(
            check,
            run_dir,
            job,
            current_identity,
            snapshot,
            cwd,
            specification,
            job_files,
            evidence_dir,
            files,
            evidence_digest,
            should_cancel,
            check.timeout_seconds,
        )
    except (ExternalProtocolError, OSError, ValueError) as exc:
        outcome = Blocked(BlockedReason.ARTIFACT_MALFORMED, str(exc))
        return outcome, _failure_metadata(check, job, None, "replay_validation", outcome)


def _check_evidence(
    check: ExternalCheck,
    run_dir: Path,
    job: dict[str, Any],
    current_identity: dict[str, Any],
    snapshot: Path,
    cwd: Path,
    specification: Path,
    job_files: dict[str, str],
    evidence_dir: Path,
    files: list[dict[str, str]],
    evidence_digest: str,
    should_cancel: Callable[[], bool],
    timeout_seconds: float,
) -> tuple[Outcome, dict[str, Any]]:
    spec_digest = job_files[check.specification]
    checker_request = run_dir / "checker-request.json"
    receipt_path = run_dir / "checker-receipt.json"
    _unlink_output(receipt_path)
    _write_json(checker_request, _request(check, job, specification, evidence_dir, current_identity) | {
        "evidence_bundle": {"files": files, "digest": evidence_digest},
    })
    _ensure_identity(check, current_identity)
    _verify_snapshot(snapshot, job_files)
    checker_exit = _run_command(
        check.checker,
        check,
        run_dir,
        snapshot,
        cwd,
        checker_request,
        evidence_dir,
        receipt_path,
        "checker",
        should_cancel,
        timeout_seconds,
    )
    problem = _exit_problem(checker_exit, "checker")
    if problem is not None:
        return problem, _failure_metadata(check, job, current_identity, "checker", problem,
                                          evidence_files=files, evidence_digest=evidence_digest)
    _ensure_identity(check, current_identity)
    _verify_snapshot(snapshot, job_files)
    after_files = _evidence_files(evidence_dir, check.evidence_artifacts)
    if after_files != files:
        raise ExternalProtocolError("checker modified the evidence bundle while replaying it")
    if not receipt_path.is_file() or is_link(receipt_path):
        raise ExternalProtocolError("checker receipt is missing or is not a regular file")
    receipt_bytes = receipt_path.read_bytes()
    receipt = _parse_receipt(receipt_bytes, check, job, current_identity, spec_digest, files, evidence_digest)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    accepted = receipt["verdict"] == "accepted"
    if not accepted:
        outcome = Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            f"checker returned {receipt['verdict']}; external evidence is inconclusive",
        )
    else:
        outcome = Passed(())
    metadata = {
        "schema_version": 1,
        "job_id": job["job_id"],
        "run_id": job["run_id"],
        "check_id": check.id,
        "check_digest": job["check_digest"],
        "identity": current_identity,
        "assurance": check.assurance.value,
        "model_scope": check.model_scope,
        "assumptions": list(check.assumptions),
        "specification": {"path": check.specification, "sha256": spec_digest},
        "evidence_bundle": {"files": files, "digest": evidence_digest},
        "checker_receipt": {
            "path": "checker-receipt.json",
            "sha256": receipt_digest,
            "document": receipt,
        },
        "obligations": receipt["obligations"],
    }
    _write_json(run_dir / check.artifact_name, metadata)
    return outcome, metadata


def _parse_receipt(
    raw: bytes,
    check: ExternalCheck,
    job: dict[str, Any],
    current_identity: dict[str, Any],
    specification_digest: str,
    files: list[dict[str, str]],
    evidence_digest: str,
) -> dict[str, Any]:
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalProtocolError(f"checker receipt is not valid UTF-8 JSON: {exc}") from exc
    required = {
        "schema_version", "job_id", "run_id", "check_id", "check_digest", "specification_digest",
        "identity", "evidence_digest", "verdict", "obligations",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ExternalProtocolError("checker receipt has an unexpected shape")
    expected = {
        "schema_version": 1,
        "job_id": job["job_id"],
        "run_id": job["run_id"],
        "check_id": check.id,
        "check_digest": job["check_digest"],
        "specification_digest": specification_digest,
        "identity": current_identity,
        "evidence_digest": evidence_digest,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ExternalProtocolError("checker receipt is bound to a different job, claim, specification, evidence, or tool identity")
    if not isinstance(receipt["verdict"], str) or receipt["verdict"] not in {"accepted", "rejected", "unknown"}:
        raise ExternalProtocolError("checker receipt has an unknown verdict")
    obligations = receipt["obligations"]
    if not isinstance(obligations, list) or len(obligations) != len(check.obligations):
        raise ExternalProtocolError("checker receipt does not contain every required obligation")
    artifacts = {item["path"] for item in files}
    names: list[str] = []
    results: list[str] = []
    for item in obligations:
        if not isinstance(item, dict) or set(item) != {"name", "result", "evidence"}:
            raise ExternalProtocolError("checker obligation receipt has an unexpected shape")
        if (not isinstance(item["name"], str) or not isinstance(item["result"], str)
                or item["result"] not in {"accepted", "rejected", "unknown"}):
            raise ExternalProtocolError("checker obligation receipt has an invalid name or result")
        evidence_refs = item["evidence"]
        if (not isinstance(evidence_refs, list) or not evidence_refs
                or any(not isinstance(path, str) or path not in artifacts for path in evidence_refs)):
            raise ExternalProtocolError(f"obligation {item['name']!r} does not reference a captured evidence artifact")
        names.append(item["name"])
        results.append(item["result"])
    if names != list(check.obligations):
        raise ExternalProtocolError("checker receipt obligation names or order do not match the accepted manifest")
    expected_verdict = "accepted" if all(result == "accepted" for result in results) else (
        "rejected" if any(result == "rejected" for result in results) else "unknown"
    )
    if receipt["verdict"] != expected_verdict:
        raise ExternalProtocolError("checker verdict disagrees with its per-obligation results")
    return receipt


def _validate_job(
    check: ExternalCheck,
    run_dir: Path,
    job: dict[str, Any],
    current_identity: dict[str, Any],
) -> tuple[Path, Path, Path, dict[str, str]]:
    if not isinstance(job, dict) or job.get("schema_version") != 1:
        raise ExternalProtocolError("job must be a version 1 object")
    if job.get("check_id") != check.id:
        raise ExternalProtocolError("job names a different check")
    if not isinstance(job.get("job_id"), str) or not _SHA256.fullmatch(job["job_id"]):
        raise ExternalProtocolError("job_id must be a SHA-256 key")
    if not isinstance(job.get("run_id"), str) or not job["run_id"]:
        raise ExternalProtocolError("job run_id is missing")
    entry = job.get("entry")
    if not isinstance(entry, dict) or canonical_digest(entry) != job.get("check_digest"):
        raise ExternalProtocolError("job entry does not match its accepted check digest")
    if entry.get("id") != check.id or entry.get("kind") != "external":
        raise ExternalProtocolError("job entry does not identify the parsed external check")
    if job.get("backend") != current_identity:
        raise ExternalProtocolError("job tool identity differs from the currently pinned commands")
    raw_inputs = job.get("inputs")
    if not isinstance(raw_inputs, dict) or not isinstance(raw_inputs.get("files"), list):
        raise ExternalProtocolError("job inputs are missing their file closure")
    job_files: dict[str, str] = {}
    for item in raw_inputs["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ExternalProtocolError("job input entry has an unexpected shape")
        path, digest = item["path"], item["sha256"]
        if not isinstance(path, str) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ExternalProtocolError("job input entry has an invalid path or digest")
        _safe_relative(path, "job input")
        if path in job_files:
            raise ExternalProtocolError(f"job input {path!r} is duplicated")
        job_files[path] = digest
    if list(job_files) != sorted(job_files):
        raise ExternalProtocolError("job inputs are not in canonical path order")
    if raw_inputs.get("digest") != canonical_digest([[path, job_files[path]] for path in job_files]):
        raise ExternalProtocolError("job input digest does not match its file closure")
    if check.specification not in job_files:
        raise ExternalProtocolError("frozen specification is missing from the job input closure")
    if entry.get("specification") != check.specification:
        raise ExternalProtocolError("job specification differs from the parsed check")
    snapshot = run_dir / "snapshot"
    specification = _safe_snapshot_path(snapshot, check.specification)
    cwd_raw = entry.get("cwd") or "."
    _safe_relative(cwd_raw, "check cwd", allow_dot=True)
    cwd = snapshot if cwd_raw == "." else _safe_snapshot_path(snapshot, cwd_raw)
    if not cwd.is_dir() or is_link(cwd) or not _within(cwd, snapshot):
        raise ExternalProtocolError("snapshot check working directory is missing or unsafe")
    return snapshot, cwd, specification, job_files


def _request(
    check: ExternalCheck,
    job: dict[str, Any],
    specification: Path,
    evidence_dir: Path,
    current_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job": job,
        "identity": current_identity,
        "specification": {
            "path": check.specification,
            "snapshot_path": str(specification),
            "sha256": next(
                item["sha256"] for item in job["inputs"]["files"] if item["path"] == check.specification
            ),
        },
        "obligations": list(check.obligations),
        "assumptions": list(check.assumptions),
        "model_scope": check.model_scope,
        "assurance": check.assurance.value,
        "evidence_artifacts": list(check.evidence_artifacts),
        "evidence_directory": str(evidence_dir),
    }


def _command_identity(command: PinnedCommand, what: str) -> dict[str, Any]:
    pinned = {str(Path(pin.path).resolve()) for pin in (command.executable, *command.dependencies)}
    for argument in command.argv:
        path = Path(argument)
        if path.is_absolute() and path.is_file() and str(path.resolve()) not in pinned:
            raise ExternalProtocolError(f"{what} argument file must be pinned as a dependency: {argument}")
    return {
        "executable": _file_identity(command.executable, f"{what} executable"),
        "argv": list(command.argv),
        "dependencies": [
            _file_identity(item, f"{what} dependency")
            for item in sorted(command.dependencies, key=lambda pin: pin.path)
        ],
    }


def _file_identity(pin: PinnedFile, what: str) -> dict[str, str]:
    path = Path(pin.path)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ExternalProtocolError(f"{what} is not a regular file: {pin.path}")
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise ExternalProtocolError(f"cannot resolve {what} {pin.path!r}: {exc}") from exc
    if not _SHA256.fullmatch(pin.sha256) or actual != pin.sha256:
        raise ExternalProtocolError(f"{what} SHA-256 does not match its pinned identity: {pin.path}")
    return {"path": str(resolved), "sha256": actual}


def _ensure_identity(check: ExternalCheck, expected: dict[str, Any]) -> None:
    if identity(check) != expected:
        raise ExternalProtocolError("a pinned verifier tool changed during execution")


def _run_command(
    command: PinnedCommand,
    check: ExternalCheck,
    run_dir: Path,
    snapshot: Path,
    cwd: Path,
    request: Path,
    evidence_dir: Path,
    receipt: Path,
    stage: str,
    should_cancel: Callable[[], bool],
    timeout_seconds: float,
) -> proc.Exit:
    if timeout_seconds <= 0:
        return proc.Exit(None, timed_out=True)
    paths = {
        "python": str(Path(command.executable.path).resolve()),
        "snapshot": str(snapshot),
        "run_dir": str(run_dir),
        "request": str(request),
        "evidence": str(evidence_dir),
        "receipt": str(receipt),
    }
    argv = [str(Path(command.executable.path).resolve())]
    for raw in command.argv:
        arg = raw
        for key, value in paths.items():
            arg = arg.replace("{{" + key + "}}", value)
        if re.search(r"\{\{[^{}]*\}\}", arg):
            raise ExternalProtocolError(f"{stage} command contains an unknown placeholder")
        argv.append(arg)
    run_dir.joinpath("tmp").mkdir(parents=True, exist_ok=True)
    env = {"PATH": "", "PYTHONNOUSERSITE": "1", "LANG": "C", "LC_ALL": "C"}
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            if value := os.environ.get(name):
                env[name] = value
        env["TEMP"] = env["TMP"] = str(run_dir / "tmp")
    else:
        env["TMPDIR"] = str(run_dir / "tmp")
    return proc.run(
        argv,
        cwd=cwd,
        stdout_path=run_dir / f"{stage}.stdout.log",
        stderr_path=run_dir / f"{stage}.stderr.log",
        timeout_seconds=timeout_seconds,
        env=env,
        should_cancel=should_cancel,
    )


def _exit_problem(exit: proc.Exit, stage: str) -> Blocked | None:
    if exit.launch_error is not None:
        return Blocked(BlockedReason.LAUNCH_FAILED, exit.launch_error)
    if exit.cancelled:
        return Blocked(BlockedReason.CANCELLED, f"{stage} stopped on request; descendants were stopped")
    if exit.timed_out:
        return Blocked(BlockedReason.TIMEOUT, f"{stage} exceeded its time budget; descendants were stopped")
    if exit.output_limited:
        return Blocked(BlockedReason.ARTIFACT_MALFORMED, f"{stage} output exceeded the process limit")
    if exit.code != 0:
        return Blocked(BlockedReason.ARTIFACT_MALFORMED, f"{stage} exited {exit.code}; its result is not accepted")
    return None


def _evidence_files(evidence_dir: Path, artifacts: tuple[str, ...]) -> list[dict[str, str]]:
    files = []
    for relative in sorted(artifacts):
        path = _safe_artifact_path(evidence_dir, relative)
        if not path.is_file() or is_link(path):
            raise ExternalProtocolError(f"required evidence artifact is missing or unsafe: {relative}")
        files.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return files


def _copy_evidence(source: Path, destination: Path, artifacts: tuple[str, ...]) -> list[dict[str, str]]:
    if not source.is_dir() or is_link(source):
        raise ExternalProtocolError("retained evidence directory is missing or unsafe")
    files = []
    for relative in sorted(artifacts):
        src = _safe_artifact_path(source, relative)
        if not src.is_file() or is_link(src):
            raise ExternalProtocolError(f"retained evidence artifact is missing or unsafe: {relative}")
        dest = _safe_artifact_path(destination, relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        files.append({"path": relative, "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})
    return files


def _evidence_digest(files: list[dict[str, str]]) -> str:
    return canonical_digest(files)


def _verify_snapshot(snapshot: Path, files: dict[str, str]) -> None:
    for relative, expected in files.items():
        path = _safe_snapshot_path(snapshot, relative)
        if not path.is_file() or file_digest(path) != expected:
            raise ExternalProtocolError(f"snapshot input changed or is missing: {relative}")


def _safe_snapshot_path(snapshot: Path, relative: str) -> Path:
    rel = _safe_relative(relative, "snapshot path")
    candidate = snapshot.joinpath(*rel.parts)
    current = snapshot
    if is_link(current):
        raise ExternalProtocolError("snapshot root is a symlink")
    for part in rel.parts:
        current = current / part
        if is_link(current):
            raise ExternalProtocolError(f"snapshot path contains a symlink: {relative}")
    if not _within(candidate, snapshot):
        raise ExternalProtocolError(f"snapshot path is unsafe: {relative}")
    return candidate


def _safe_artifact_path(root: Path, relative: str) -> Path:
    if is_link(root):
        raise ExternalProtocolError("evidence root is a symlink")
    rel = _safe_relative(relative, "evidence artifact")
    candidate = root.joinpath(*rel.parts)
    if not _within(candidate, root):
        raise ExternalProtocolError(f"evidence artifact escapes its directory: {relative}")
    current = root
    for part in rel.parts:
        current = current / part
        if is_link(current):
            raise ExternalProtocolError(f"evidence artifact path contains a symlink: {relative}")
    return candidate


def _safe_relative(raw: str, what: str, *, allow_dot: bool = False) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ExternalProtocolError(f"{what} must be a nonempty POSIX-relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or ("." in path.parts and not allow_dot):
        raise ExternalProtocolError(f"{what} must stay within its declared directory")
    if raw == "." and allow_dot:
        return path
    if path.as_posix() != raw:
        raise ExternalProtocolError(f"{what} must be normalized")
    if not path.parts or any(part in {"", "."} for part in path.parts):
        raise ExternalProtocolError(f"{what} must be a normalized relative path")
    return path


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _unlink_output(path: Path) -> None:
    if is_link(path) or path.is_file():
        path.unlink()
    elif path.exists():
        raise ExternalProtocolError(f"checker receipt output is not a file: {path.name}")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _failure_metadata(
    check: ExternalCheck,
    job: dict[str, Any],
    current_identity: dict[str, Any] | None,
    stage: str,
    outcome: Blocked,
    *,
    evidence_files: list[dict[str, str]] | None = None,
    evidence_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job.get("job_id") if isinstance(job, dict) else None,
        "run_id": job.get("run_id") if isinstance(job, dict) else None,
        "check_id": check.id,
        "identity": current_identity,
        "assurance": check.assurance.value,
        "model_scope": check.model_scope,
        "assumptions": list(check.assumptions),
        "evidence_bundle": {"files": evidence_files or [], "digest": evidence_digest},
        "stage": stage,
        "outcome": outcome.to_json(),
    }

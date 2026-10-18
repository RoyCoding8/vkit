"""Replay an approved scenario check with its harness retained from a Git commit."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..inputs import is_secret, matches
from ..manifest import ManifestError, parse_manifest, parse_manifest_data
from ..paths import Project, open_project
from ..proc import run as run_process
from ..runner import missing_prerequisite
from ..store import Store
from ..verifiers import dispatch
from ..verifiers.spec import ScenarioCheck
from .workloads import (MAX_INPUT_BYTES, MAX_INPUT_FILES, MAX_REPORT_BYTES,
                        _InputFile, _Unsupported, _capture_inputs, _commit_files, _environment,
                        _git, _write_workspace)

MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 600
_RUN_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class _Request:
    project: Project
    store: Store
    check: ScenarioCheck
    check_digest: str
    run_id: str
    record: dict[str, Any]
    current_files: tuple[_InputFile, ...]
    retained_files: tuple[_InputFile, ...]
    subjects: tuple[str, ...]
    before_ref: str
    environment: dict[str, str]
    environment_metadata: dict[str, str]


def _safe_git_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (bool(path) and not PurePosixPath(path).is_absolute()
            and not PureWindowsPath(path).drive and "\\" not in path
            and all(part not in {"", ".", ".."} for part in parts))


def _prepare(root: Path, run_id: Any, before_ref: Any) -> _Request:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise _Unsupported("run_id has an invalid format")
    project = open_project(root)
    store = Store.of(project)
    record = store.record(run_id)
    if not isinstance(record, dict) or record.get("state") != "done":
        raise _Unsupported("run is missing or was not completed")
    if record.get("worktree") and Path(record["worktree"]).resolve() != project.root.resolve():
        raise _Unsupported("run belongs to another checkout")
    check_id = record.get("check_id")
    manifest = parse_manifest(project)
    if not isinstance(check_id, str) or check_id not in manifest.checks:
        raise _Unsupported("run's check is no longer registered")
    check = manifest.require(check_id)
    if not isinstance(check, ScenarioCheck):
        raise _Unsupported("run must use an approved scenario check")
    prerequisite = missing_prerequisite(check)
    if prerequisite is not None:
        raise _Unsupported(prerequisite.detail)
    check_digest = manifest.digest(check_id)
    if record.get("check_digest") != check_digest or check_digest not in store.approved():
        raise _Unsupported("check definition changed or is no longer approved")
    if not isinstance(record.get("outcome"), dict) or record["outcome"].get("result") not in {"PASS", "FAIL"}:
        raise _Unsupported("run does not contain a completed scenario outcome")
    if not check.subject.paths:
        raise _Unsupported("approved scenario check must declare subject paths")

    current_files = _capture_inputs(project, check, record)
    names = {item.path for item in current_files}
    subjects = tuple(sorted(path for path in names
                            if any(matches(path, pattern) for pattern in check.subject.paths)))
    if not subjects:
        raise _Unsupported("approved subject paths match no captured check inputs")
    command_inputs = {
        PurePosixPath(argument.replace("\\", "/")).as_posix()
        for argument in check.command if "{{" not in argument and not argument.startswith("-")
    }
    overlap = sorted(set(subjects) & command_inputs)
    if overlap:
        raise _Unsupported("candidate subject overlaps an approved command input: " + ", ".join(overlap))
    if not (command_inputs & (names - set(subjects))):
        raise _Unsupported("approved command must invoke a retained non-subject check input")

    if not isinstance(before_ref, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", before_ref):
        raise _Unsupported("before_ref must be a full immutable Git commit SHA")
    resolved = _git(project, "rev-parse", "--verify", f"{before_ref}^{{commit}}").decode("ascii", "replace").strip()
    if resolved != before_ref:
        raise _Unsupported("before_ref does not resolve to the supplied full commit SHA")

    tree = _git(project, "ls-tree", "-r", "-z", "--full-tree", before_ref)
    old_paths: set[str] = set()
    for row in tree.split(b"\0"):
        if not row:
            continue
        if b"\t" not in row:
            raise _Unsupported("Git returned a malformed retained input tree")
        metadata, raw_path = row.split(b"\t", 1)
        fields = metadata.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            continue
        path = raw_path.decode("utf-8", "surrogateescape")
        if not _safe_git_path(path):
            raise _Unsupported("retained input path cannot be materialized safely")
        if (not is_secret(path) and (not check.inputs or any(matches(path, pattern) for pattern in check.inputs))):
            old_paths.add(path)
    current_paths = {item.path for item in current_files}
    if old_paths != current_paths:
        raise _Unsupported("before_ref and the captured run do not have the same exact check input paths")
    if len(old_paths) > MAX_INPUT_FILES:
        raise _Unsupported("retained check inputs exceed the file-count limit")

    old_files = _commit_files(project, before_ref, tuple(sorted(old_paths)))
    if sum(len(item.content) for item in old_files) > MAX_INPUT_BYTES:
        raise _Unsupported("retained check inputs exceed the total byte limit")
    old_subjects = {item.path for item in old_files
                    if any(matches(item.path, pattern) for pattern in check.subject.paths)}
    if old_subjects != set(subjects):
        raise _Unsupported("retained baseline and current candidate subject paths do not match")

    manifest_path = project.manifest_path.relative_to(project.root).as_posix()
    old_manifest_file = _commit_files(project, before_ref, (manifest_path,))[0]
    try:
        old_manifest_data = json.loads(old_manifest_file.content.decode("utf-8"))
        old_manifest = parse_manifest_data(old_manifest_data, project=project, origin=f"{before_ref}:{manifest_path}")
        old_check_digest = old_manifest.digest(check_id)
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestError, KeyError, TypeError):
        raise _Unsupported("before_ref does not contain the retained approved check definition") from None
    if old_check_digest != check_digest or not isinstance(old_manifest.require(check_id), ScenarioCheck):
        raise _Unsupported("before_ref check definition differs from the currently approved check")

    environment, environment_metadata = _environment()
    return _Request(project, store, check, check_digest, run_id, record, current_files,
                    old_files, subjects, before_ref, environment, environment_metadata)


def _assert_fresh(request: _Request) -> None:
    manifest = parse_manifest(request.project)
    if manifest.digest(request.check.id) != request.check_digest \
            or request.check_digest not in request.store.approved():
        raise _Unsupported("approved check definition changed or approval was revoked during replay")
    current = _capture_inputs(request.project, request.check, request.record)
    expected_hashes = {item.path: item.sha256 for item in request.current_files}
    if {item.path: item.sha256 for item in current} != expected_hashes:
        raise _Unsupported("captured candidate bytes changed during replay")


def _read_report(artifact: Path) -> bytes:
    try:
        info = artifact.lstat()
    except OSError:
        raise _Unsupported("scenario command did not write its report") from None
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise _Unsupported("scenario report is not a regular file")
    if info.st_size > MAX_REPORT_BYTES:
        raise _Unsupported("scenario report exceeds the report byte limit")
    try:
        descriptor = os.open(artifact, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise _Unsupported("scenario report changed while captured") from None
    with os.fdopen(descriptor, "rb") as source:
        raw = source.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES or source.read(1):
            raise _Unsupported("scenario report exceeds the report byte limit")
    return raw


def _run_once(request: _Request, side: str, deadline: float) -> dict[str, Any]:
    if side not in {"baseline", "candidate"}:
        raise ValueError("invalid replay side")
    replacements = ({item.path: item for item in request.current_files if item.path in request.subjects}
                    if side == "candidate" else {})
    with tempfile.TemporaryDirectory(prefix="vkit-retained-case-") as raw_workspace:
        workspace = Path(raw_workspace)
        _write_workspace(request.retained_files, workspace, replacements)
        relative_cwd = request.check.cwd.relative_to(request.project.root)
        cwd = workspace / relative_cwd
        cwd.mkdir(parents=True, exist_ok=True)
        run_dir = workspace / ".vkit-retained-run"
        run_dir.mkdir()
        artifact = run_dir / request.check.artifact_name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        check = replace(request.check, cwd=cwd)
        argv = dispatch.argv_for(check, run_dir, None)
        timeout = min(request.check.timeout_seconds, deadline - time.monotonic())
        if timeout <= 0:
            return {"status": "INCONCLUSIVE", "detail": "retained replay exceeded its wall deadline"}
        execution = run_process(argv, cwd=cwd, env=request.environment,
                                 stdout_path=run_dir / "stdout.log", stderr_path=run_dir / "stderr.log",
                                 timeout_seconds=timeout)
        if execution.launch_error or execution.cancelled or execution.timed_out:
            reason = "scenario command could not start" if execution.launch_error else (
                "scenario command was cancelled" if execution.cancelled else "scenario command timed out")
            return {"status": "INCONCLUSIVE", "detail": reason}
        if execution.code not in {0, 1}:
            return {"status": "INCONCLUSIVE", "detail": "scenario command exited with an untrusted status"}
        raw_report = _read_report(artifact)
        reading = dispatch.interpret(check, raw_report)
        if reading.problem is not None:
            return {"status": "INCONCLUSIVE", "detail": reading.problem.detail}
        expected = {item.test_id for item in request.check.required_scenarios}
        actual = [item.scenario_id for item in reading.scenarios]
        if set(actual) != expected or len(actual) != len(expected):
            return {"status": "INCONCLUSIVE", "detail": "scenario identities did not exactly match the approved check"}
        cases = [{"id": item.scenario_id, "result": "PASS" if item.passed else "FAIL",
                  "observation": item.observation} for item in reading.scenarios]
        return {"status": "PASS" if all(item.passed for item in reading.scenarios) else "FAIL",
                "exit_code": execution.code, "report_sha256": hashlib.sha256(raw_report).hexdigest(),
                "cases": cases}


def _paired_cases(request: _Request, baseline: dict[str, Any],
                  candidate: dict[str, Any] | None) -> list[dict[str, Any]]:
    baseline_by_id = {item["id"]: item for item in baseline["cases"]}
    candidate_by_id = {item["id"]: item for item in candidate["cases"]} if candidate is not None else {}
    return [{"id": obligation.test_id, "baseline": baseline_by_id[obligation.test_id],
             "candidate": candidate_by_id.get(obligation.test_id)}
            for obligation in request.check.required_scenarios]


def replay_retained_cases(root: Path, run_id: str, before_ref: str, *,
                          timeout_seconds: int = 120) -> dict[str, Any]:
    """Replay old approved scenario assertions against a captured current subject."""
    if type(timeout_seconds) is not int or not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [{MIN_TIMEOUT_SECONDS}, {MAX_TIMEOUT_SECONDS}]")
    result: dict[str, Any] = {"operation": "replay_retained_cases", "status": "UNSUPPORTED",
                              "claim_scope": "retained required scenario results; budget obligations are not evaluated",
                              "run_id": run_id if isinstance(run_id, str) else "",
                              "before_ref": before_ref if isinstance(before_ref, str) else "",
                              "baseline": None, "candidate": None, "case_results": [], "detail": None}
    try:
        request = _prepare(root, run_id, before_ref)
    except (OSError, ValueError, TypeError, KeyError, ManifestError) as exc:
        result["detail"] = str(exc)
        return result
    result.update({"check_id": request.check.id, "check_digest": request.check_digest,
                   "required_cases": [item.test_id for item in request.check.required_scenarios],
                   "retained_inputs": [{"path": item.path, "sha256": item.sha256, "mode": item.mode}
                                       for item in request.retained_files],
                   "candidate_subjects": [{"path": item.path, "sha256": item.sha256, "mode": item.mode}
                                          for item in request.current_files if item.path in request.subjects],
                   "environment": request.environment_metadata})
    deadline = time.monotonic() + timeout_seconds
    try:
        _assert_fresh(request)
        baseline = _run_once(request, "baseline", deadline)
        result["baseline"] = baseline
        _assert_fresh(request)
        if baseline["status"] == "FAIL":
            result.update({"status": "BASELINE_FAILED",
                           "case_results": _paired_cases(request, baseline, None),
                           "detail": "retained baseline did not pass every approved scenario; candidate was not classified"})
            return result
        if baseline["status"] != "PASS":
            result.update({"status": "INCONCLUSIVE", "detail": baseline.get("detail")})
            return result
        _assert_fresh(request)
        candidate = _run_once(request, "candidate", deadline)
        result["candidate"] = candidate
        _assert_fresh(request)
        if candidate["status"] != "PASS":
            if candidate["status"] == "FAIL":
                result["status"] = "REGRESSION"
                result["case_results"] = _paired_cases(request, baseline, candidate)
            else:
                result["status"] = "INCONCLUSIVE"
                result["detail"] = candidate.get("detail")
            return result
        result["status"] = "REPLAYED_PASS"
        result["case_results"] = _paired_cases(request, baseline, candidate)
        return result
    except (OSError, ValueError, TypeError, KeyError, _Unsupported) as exc:
        result.update({"status": "INCONCLUSIVE", "detail": str(exc)})
        return result


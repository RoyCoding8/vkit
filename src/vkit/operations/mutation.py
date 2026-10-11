"""Run bounded Stryker-generated literal edits against one approved Node test check."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .. import proc
from ..inputs import InputSet, is_secret, matches, source_files
from ..manifest import parse_manifest
from ..nowindow import hidden_window
from ..paths import Project, open_project
from ..store import Store
from ..verifiers import dispatch
from ..verifiers.node_adapter import AdapterResult
from ..outcome import Blocked
from ..verifiers.spec import NodeTestCheck

ENGINE_VERSION = "10.0.0"
NODE_VERSION = "v24.16.0"
PROTOCOL_VERSION = 1
MAX_MUTANTS = 100
MAX_TIMEOUT_SECONDS = 300
MAX_FILE_BYTES = 1_048_576
MAX_SNAPSHOT_FILES = 1_000
MAX_SNAPSHOT_BYTES = 16_777_216
MAX_ENGINE_OUTPUT = 1_048_576
MAX_REPORT_BYTES = 8_388_608
MAX_GIT_OUTPUT_BYTES = 8_388_608
MAX_HELPER_BYTES = 1_048_576
MAX_LOCK_BYTES = 4_194_304
_ENGINE_HELPER = "VKIT_MUTATION_HELPER"
_ENGINE_SHA = "VKIT_MUTATION_SHA256"
_ENGINE_LOCK = "VKIT_MUTATION_LOCKFILE"
_ENGINE_LOCK_SHA = "VKIT_MUTATION_LOCK_SHA256"
_NODE = "VKIT_NODE_BIN"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SOURCE_SUFFIXES = frozenset({".js", ".mjs", ".cjs"})

_MODEL_SCOPE = (
    "StrykerJS 10.0.0 literal source replacements for one recorded JavaScript file. "
    "Each trial runs the approved Node test check from captured inputs in a fresh temporary directory. "
    "The temporary directory is not an operating-system sandbox; test code runs with the service process permissions. "
    "No package installation, candidate configuration, mutation switching, coverage measurement, or defect claim is included."
)


class _Unsupported(ValueError):
    pass


class _CampaignTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class _SnapshotFile:
    path: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class _Request:
    project: Project
    run_id: str
    check: NodeTestCheck
    check_digest: str
    record: dict[str, Any]
    source_path: str
    source_text: str
    source_sha256: str
    files: tuple[_SnapshotFile, ...]
    cwd: str


def _relative_file(project: Project, raw: str) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise _Unsupported("path must be a repository-relative POSIX file path")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise _Unsupported("path must stay inside the repository")
    if relative.as_posix() != raw:
        raise _Unsupported("path must use its canonical repository-relative spelling")
    root = project.root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink() or current.lstat().st_file_attributes & _REPARSE_POINT:
                raise _Unsupported("symbolic links and reparse points are not accepted")
        except AttributeError:
            if current.is_symlink():
                raise _Unsupported("symbolic links are not accepted") from None
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise _Unsupported("path is missing or escapes the repository") from None
    if not resolved.is_file():
        raise _Unsupported("path must name a regular file")
    return raw, resolved


def _required_paths(check: NodeTestCheck) -> tuple[str, ...]:
    paths: list[str] = []
    for test_id in check.required_tests:
        file, separator, name = test_id.partition("::")
        if not separator or not file or not name:
            raise _Unsupported("required tests must use <repository-relative-file>::<case-name>")
        _, parsed = _relative_operand(file, "required test")
        if parsed.as_posix() not in paths:
            paths.append(parsed.as_posix())
    if not paths:
        raise _Unsupported("the approved Node check has no required test cases")
    return tuple(paths)


def _relative_operand(raw: str, label: str) -> tuple[str, PurePosixPath]:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise _Unsupported(f"{label} must be a repository-relative POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != raw:
        raise _Unsupported(f"{label} must stay inside the repository")
    return raw, path


def _capture(project: Project, record_inputs: Any, check: NodeTestCheck, source_path: str) -> tuple[_SnapshotFile, ...]:
    entries = record_inputs.get("files") if isinstance(record_inputs, dict) else None
    if not isinstance(entries, list) or len(entries) > MAX_SNAPSHOT_FILES:
        raise _Unsupported("recorded inputs are missing or exceed the snapshot file limit")
    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                or not isinstance(entry.get("sha256"), str) or not _SHA256.fullmatch(entry["sha256"]):
            raise _Unsupported("recorded input snapshot has an invalid file entry")
        path = entry["path"]
        _relative_operand(path, "recorded input")
        if path in expected:
            raise _Unsupported("recorded input snapshot has duplicate paths")
        expected[path] = entry["sha256"]
    if source_path not in expected:
        raise _Unsupported("source file is outside the recorded check inputs")
    for path in _required_paths(check):
        if path not in expected:
            raise _Unsupported(f"required test file {path!r} is outside the recorded check inputs")

    available = source_files(project)
    selected = tuple(name for name in available if not check.inputs or any(matches(name, pattern)
                                                                            for pattern in check.inputs))
    if len(selected) > MAX_SNAPSHOT_FILES:
        raise _Unsupported("recorded inputs exceed the snapshot file limit")
    unmatched = tuple(pattern for pattern in check.inputs
                      if not any(matches(name, pattern) for name in available))
    sizes: dict[str, int] = {}
    total_size = 0
    for path in selected:
        _, source = _relative_file(project, path)
        size = source.stat().st_size
        if size > MAX_FILE_BYTES:
            raise _Unsupported("recorded inputs exceed the per-file snapshot byte limit")
        total_size += size
        if total_size > MAX_SNAPSHOT_BYTES:
            raise _Unsupported("recorded inputs exceed the snapshot byte limit")
        sizes[path] = size

    total = 0
    files: list[_SnapshotFile] = []
    current_files: list[tuple[str, str]] = []
    for path in selected:
        _, source = _relative_file(project, path)
        with source.open("rb") as handle:
            content = handle.read(MAX_FILE_BYTES + 1)
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_SNAPSHOT_BYTES:
            raise _Unsupported("recorded inputs exceed the snapshot byte limit")
        if len(content) != sizes[path]:
            raise _Unsupported("check input changed size while capturing the immutable snapshot")
        normalized_sha = hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()
        if expected.get(path) != normalized_sha:
            raise _Unsupported("check inputs changed after the recorded run")
        current_files.append((path, normalized_sha))
        files.append(_SnapshotFile(path, content, hashlib.sha256(content).hexdigest()))
    if InputSet(tuple(check.inputs), tuple(current_files), unmatched).to_json() != record_inputs:
        raise _Unsupported("check inputs changed after the recorded run")
    return tuple(files)


def _package_configs(request_paths: tuple[str, ...], cwd: Path, project: Project) -> set[str]:
    root = project.root.resolve()
    directories = {cwd.resolve()}
    directories.update((root.joinpath(*PurePosixPath(path).parts).parent.resolve() for path in request_paths))
    configs: set[str] = set()
    for directory in directories:
        cursor = directory
        while cursor == root or root in cursor.parents:
            config = cursor / "package.json"
            if config.exists():
                try:
                    relative = config.relative_to(root).as_posix()
                    _relative_file(project, relative)
                    configs.add(relative)
                except (OSError, ValueError):
                    raise _Unsupported("Node package configuration escapes the repository") from None
            if cursor == root:
                break
            cursor = cursor.parent
    return configs


def _prepare(root: Path, run_id: str, path: str) -> _Request:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise _Unsupported("run ID has an invalid format")
    project = open_project(root)
    store = Store.of(project)
    record = store.record(run_id)
    if not isinstance(record, dict) or record.get("state") != "done":
        raise _Unsupported("run is missing or was not completed")
    if record.get("worktree") and Path(record["worktree"]).resolve() != project.root.resolve():
        raise _Unsupported("run belongs to another checkout")
    manifest = parse_manifest(project)
    check_id = record.get("check_id")
    if not isinstance(check_id, str) or check_id not in manifest.checks:
        raise _Unsupported("run's check is no longer registered")
    check_digest = manifest.digest(check_id)
    if record.get("check_digest") != check_digest:
        raise _Unsupported("check definition changed after the recorded run")
    if check_digest not in store.approved():
        raise _Unsupported("check definition is no longer approved")
    check = manifest.require(check_id)
    if not isinstance(check, NodeTestCheck):
        raise _Unsupported("run must use an approved NodeTestCheck")
    if check.runner.base_argv != ("--test",):
        raise _Unsupported("Node checks must use exactly the fixed --test runner arguments")
    if manifest.entries[check_id].get("cwd") not in (None, "", "."):
        raise _Unsupported("Node mutation checks must run from the project root")
    if check.cwd.resolve() != project.root.resolve():
        raise _Unsupported("Node mutation checks must run from the project root")
    _, source = _relative_file(project, path)
    if not any(matches(path, subject) for subject in check.subject.paths):
        raise _Unsupported("source file is outside the approved check subject")
    if source.suffix.lower() not in _SOURCE_SUFFIXES:
        raise _Unsupported("only .js, .mjs, and .cjs source files are supported")
    files = _capture(project, record.get("inputs"), check, path)
    file_map = {item.path: item.content for item in files}
    source_bytes = file_map[path]
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unsupported("source file must contain UTF-8 text") from None
    required_paths = _required_paths(check)
    if path in required_paths:
        raise _Unsupported("a required test file cannot be the mutation subject")
    request_paths = tuple(item.path for item in files if Path(item.path).suffix.lower() in _SOURCE_SUFFIXES)
    configs = _package_configs(request_paths, check.cwd, project)
    missing_configs = sorted(configs - set(file_map))
    if missing_configs:
        raise _Unsupported(f"Node package configuration is outside recorded inputs: {', '.join(missing_configs)}")
    manifest_relative = project.manifest_path.relative_to(project.root).as_posix()
    if path == manifest_relative or path in configs:
        raise _Unsupported("Node package configuration and the verification manifest cannot be mutation subjects")

    recorded_outcome = record.get("outcome")
    if not isinstance(recorded_outcome, dict) or recorded_outcome.get("result") != "PASS":
        raise _Unsupported("recorded run must have a completed PASS outcome")
    artifact = store.run_dir(run_id) / check.artifact_name
    if not artifact.is_file():
        raise _Unsupported("recorded Node report is missing")
    with artifact.open("rb") as report_file:
        report_bytes = report_file.read(MAX_REPORT_BYTES + 1)
    if len(report_bytes) > MAX_REPORT_BYTES:
        raise _Unsupported("recorded Node report exceeds the report limit")
    reading = dispatch.interpret(check, report_bytes)
    outcome = dispatch.outcome_from_reading(reading)
    if isinstance(outcome, Blocked) or outcome.to_json().get("result") != "PASS":
        raise _Unsupported("recorded report does not prove all required cases passed")
    if not isinstance(reading, AdapterResult):
        raise _Unsupported("recorded report does not contain the exact required case identities")
    identities = [item.scenario_id for item in reading.scenarios()]
    if len(identities) != len(check.required_tests) or set(identities) != set(check.required_tests):
        raise _Unsupported("recorded report does not contain the exact required case identities")
    return _Request(project, run_id, check, check_digest, record, path, source_text,
                    hashlib.sha256(source_bytes).hexdigest(), files, ".")


def _runtime() -> tuple[Path, str, Path, str, Path] | None:
    raw_helper = os.environ.get(_ENGINE_HELPER)
    expected_helper = os.environ.get(_ENGINE_SHA, "").lower()
    raw_lock = os.environ.get(_ENGINE_LOCK)
    expected_lock = os.environ.get(_ENGINE_LOCK_SHA, "").lower()
    raw_node = os.environ.get(_NODE)
    if not raw_helper or not raw_lock or not raw_node or not _SHA256.fullmatch(expected_helper) \
            or not _SHA256.fullmatch(expected_lock):
        return None
    helper, lockfile, node = (Path(item).expanduser().resolve() for item in (raw_helper, raw_lock, raw_node))
    try:
        if not helper.is_file() or not lockfile.is_file() or not node.is_file():
            return None
        with helper.open("rb") as source:
            helper_bytes = source.read(MAX_HELPER_BYTES + 1)
        with lockfile.open("rb") as source:
            lock_bytes = source.read(MAX_LOCK_BYTES + 1)
    except OSError:
        return None
    if len(helper_bytes) > MAX_HELPER_BYTES or len(lock_bytes) > MAX_LOCK_BYTES:
        return None
    helper_sha = hashlib.sha256(helper_bytes).hexdigest()
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()
    if helper_sha != expected_helper or lock_sha != expected_lock:
        return None
    return helper, helper_sha, lockfile, lock_sha, node


def _service_env() -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _assert_fresh(request: _Request, deadline: float) -> None:
    timeout = min(30, _remaining(deadline))
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=request.project.root, capture_output=True, timeout=timeout, check=False,
            **hidden_window(),
        )
    except subprocess.TimeoutExpired as exc:
        raise _CampaignTimeout("mutation campaign exceeded its total time budget while checking freshness") from exc
    except OSError as exc:
        raise _Unsupported(f"cannot list current check inputs: {exc}") from exc
    if len(listing.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise _Unsupported("current repository input list exceeds the freshness limit")
    if listing.returncode != 0:
        detail = listing.stderr.decode("utf-8", "replace").strip()
        raise _Unsupported(detail or "git could not list current check inputs")
    names = {raw.decode("utf-8", "surrogateescape") for raw in listing.stdout.split(b"\0") if raw}
    selected = tuple(sorted(name for name in names if not is_secret(name)
                            and (request.project.root / name).is_file()
                            and (not request.check.inputs or any(matches(name, pattern)
                                                                 for pattern in request.check.inputs))))
    if selected != tuple(item.path for item in request.files):
        raise _Unsupported("check input set changed during mutation analysis")
    for item in request.files:
        _remaining(deadline)
        _, current = _relative_file(request.project, item.path)
        if current.stat().st_size != len(item.content):
            raise _Unsupported("check inputs changed during mutation analysis")
        with current.open("rb") as handle:
            content = handle.read(MAX_FILE_BYTES + 1)
        if len(content) != len(item.content):
            raise _Unsupported("check inputs changed during mutation analysis")
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise _Unsupported("check inputs changed during mutation analysis")
    if parse_manifest(request.project).digest(request.check.id) != request.check_digest:
        raise _Unsupported("check definition changed during mutation analysis")


def _engine_mutants(request: _Request, helper: Path, node: Path, env: dict[str, str], limit: int,
                    remaining: float) -> tuple[int, list[dict[str, Any]]]:
    payload = json.dumps({"source": request.source_text, "file": request.source_path, "limit": limit},
                         separators=(",", ":")).encode("utf-8")
    try:
        result = subprocess.run([str(node), str(helper)], input=payload, capture_output=True, cwd=helper.parent,
                                env=env, timeout=max(0.05, remaining), check=False, **hidden_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _CampaignTimeout("mutation instrumenter did not complete within the campaign budget") from exc
    if len(result.stdout) > MAX_ENGINE_OUTPUT:
        raise ValueError("mutation instrumenter exceeded the output limit")
    if result.returncode != 0:
        raise ValueError("mutation instrumenter rejected the source or failed")
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("mutation instrumenter returned malformed data") from None
    if not isinstance(response, dict) or response.get("protocol_version") != PROTOCOL_VERSION \
            or response.get("engine_version") != ENGINE_VERSION \
            or type(response.get("generated")) is not int \
            or not isinstance(response.get("mutants"), list):
        raise ValueError("mutation instrumenter returned malformed data or the wrong engine version")
    return response["generated"], response["mutants"]


def _copy_workspace(files: tuple[_SnapshotFile, ...], destination: Path, source_path: str, source: bytes) -> None:
    for item in files:
        target = destination.joinpath(*PurePosixPath(item.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source if item.path == source_path else item.content)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _CampaignTimeout("mutation campaign exceeded its total time budget")
    return remaining


def _case_results(reading: AdapterResult) -> list[dict[str, str]]:
    return [{"id": item.scenario_id, "result": "PASS" if item.passed else "FAIL",
             "observation": item.observation} for item in reading.scenarios()]


def _execute(request: _Request, node: Path, env: dict[str, str], source: bytes, deadline: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="vkit-mutation-workspace-") as raw_workspace:
        workspace = Path(raw_workspace)
        _copy_workspace(request.files, workspace, request.source_path, source)
        check_cwd = workspace.joinpath(*PurePosixPath(request.cwd).parts)
        check_cwd.mkdir(parents=True, exist_ok=True)
        target = workspace.joinpath(*PurePosixPath(request.source_path).parts)
        run_dir = workspace / ".vkit-mutation-run"
        run_dir.mkdir()
        artifact = run_dir / request.check.artifact_name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        syntax = proc.run([str(node), "--check", str(target)], cwd=workspace, env=env,
                          stdout_path=run_dir / "syntax.stdout", stderr_path=run_dir / "syntax.stderr",
                          timeout_seconds=min(10.0, _remaining(deadline)))
        if syntax.timed_out and time.monotonic() >= deadline:
            raise _CampaignTimeout("mutation campaign exceeded its total time budget")
        if syntax.launch_error or syntax.timed_out or syntax.code != 0:
            return {"status": "UNKNOWN", "detail": "candidate source failed the Node syntax check",
                    "cases": [], "syntax_exit_code": syntax.code}
        check = replace(request.check, cwd=check_cwd,
                        runner=replace(request.check.runner, executable=str(node)))
        argv = dispatch.argv_for(check, run_dir, None)
        if not argv or argv[0] != str(node) or argv[1:2] != ("--test",):
            return {"status": "UNKNOWN", "detail": "approved Node argv did not match the fixed runner policy",
                    "cases": []}
        execution = proc.run(argv, cwd=check.cwd, env=env, stdout_path=run_dir / "stdout.log",
                             stderr_path=run_dir / "stderr.log",
                             timeout_seconds=min(request.check.timeout_seconds, _remaining(deadline)))
        if execution.timed_out and time.monotonic() >= deadline:
            raise _CampaignTimeout("mutation campaign exceeded its total time budget")
        if execution.launch_error or execution.cancelled or execution.timed_out:
            return {"status": "UNKNOWN", "detail": "approved Node test execution did not complete",
                    "cases": [], "exit_code": execution.code, "check_started": True}
        if execution.code not in (0, 1) or not artifact.is_file():
            return {"status": "UNKNOWN", "detail": "approved Node test execution produced no trusted report",
                    "cases": [], "exit_code": execution.code, "check_started": True}
        with artifact.open("rb") as report_file:
            report = report_file.read(MAX_REPORT_BYTES + 1)
        if len(report) > MAX_REPORT_BYTES:
            return {"status": "UNKNOWN", "detail": "approved Node test report exceeded the report limit",
                    "cases": [], "exit_code": execution.code, "check_started": True}
        reading = dispatch.interpret(check, report)
        if isinstance(reading, Blocked):
            return {"status": "UNKNOWN", "detail": reading.detail, "cases": [], "check_started": True}
        if not isinstance(reading, AdapterResult):
            return {"status": "UNKNOWN", "detail": "Node adapter returned an unsupported reading", "cases": [],
                    "check_started": True}
        cases = _case_results(reading)
        identities = [item["id"] for item in cases]
        if len(identities) != len(request.check.required_tests) or set(identities) != set(request.check.required_tests):
            return {"status": "UNKNOWN", "detail": "Node report case identities changed", "cases": cases,
                    "check_started": True}
        by_id = {item["id"]: item for item in cases}
        cases = [by_id[test_id] for test_id in request.check.required_tests]
        failed = any(item["result"] == "FAIL" for item in cases)
        return {"status": "FAIL" if failed else "PASS", "cases": cases, "exit_code": execution.code,
                "check_started": True}


def _offset(source: str, line: int, column: int) -> int:
    if type(line) is not int or type(column) is not int or line < 0 or column < 0:
        raise ValueError("instrumenter returned an invalid source location")
    current_line = 0
    current_column = 0
    index = 0
    while index < len(source):
        if current_line == line and current_column == column:
            return index
        char = source[index]
        if char == "\r":
            index += 1
            if index < len(source) and source[index] == "\n":
                index += 1
            current_line += 1
            current_column = 0
            continue
        if char in {"\n", "\u2028", "\u2029"}:
            index += 1
            current_line += 1
            current_column = 0
            continue
        units = len(char.encode("utf-16-le")) // 2
        if current_line == line and current_column < column < current_column + units:
            raise ValueError("instrumenter location splits a UTF-16 surrogate pair")
        current_column += units
        index += 1
    if current_line == line and current_column == column:
        return len(source)
    raise ValueError("instrumenter returned a source column outside the line")


def _literal_edit(request: _Request, mutant: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(mutant, dict) or not isinstance(mutant.get("location"), dict):
        raise ValueError("instrumenter returned a malformed mutant")
    location = mutant["location"]
    start = location.get("start")
    end = location.get("end")
    replacement = mutant.get("replacement")
    if not isinstance(start, dict) or not isinstance(end, dict) or not isinstance(replacement, str):
        raise ValueError("instrumenter returned a malformed source edit")
    left = _offset(request.source_text, start.get("line"), start.get("column"))
    right = _offset(request.source_text, end.get("line"), end.get("column"))
    if right < left or right > len(request.source_text):
        raise ValueError("instrumenter returned an inverted or out-of-range source edit")
    before = request.source_text[left:right]
    candidate = request.source_text[:left] + replacement + request.source_text[right:]
    content = candidate.encode("utf-8")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("mutated source exceeds the source byte limit")
    return content, {"mutator": mutant.get("mutatorName"),
                     "location": location, "before": before, "replacement": replacement,
                     "candidate_sha256": hashlib.sha256(content).hexdigest()}


def _base(run_id: str, path: str) -> dict[str, Any]:
    return {"operation": "mutation_survivors", "status": "UNKNOWN", "run_id": run_id,
            "path": path if isinstance(path, str) else "", "model_scope": _MODEL_SCOPE,
            "counts": {"generated": 0, "attempted": 0, "tested": 0, "unexamined": 0},
            "baseline": None, "mutants": [], "engine_identity": None,
            "source_identity": None, "snapshot_identity": None, "detail": None}


def mutation_survivors(root: Path, run_id: str, path: str, max_mutants: int = 20,
                       timeout_seconds: int = 60) -> dict[str, Any]:
    """Measure bounded Stryker literal edits against one fresh approved Node check."""
    if type(max_mutants) is not int or not 1 <= max_mutants <= MAX_MUTANTS:
        raise ValueError(f"max_mutants must be an integer in [1, {MAX_MUTANTS}]")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    request: _Request | None = None
    result = _base(run_id, path)
    try:
        request = _prepare(root, run_id, path)
        result.update({"check_id": request.check.id, "check_digest": request.check_digest,
                       "source_identity": {"path": request.source_path, "sha256": request.source_sha256},
                       "snapshot_identity": {"files": [{"path": item.path, "sha256": item.sha256}
                                                          for item in request.files]}})
    except (OSError, ValueError, TypeError) as exc:
        result.update({"status": "UNSUPPORTED", "detail": str(exc)})
        return result

    runtime = _runtime()
    if runtime is None:
        result.update({"status": "UNAVAILABLE", "detail": "the pinned mutation helper, lockfile, SHA-256 values, or Node path is unavailable"})
        return result
    helper, helper_sha, lockfile, lock_sha, node = runtime
    node_name = request.check.runner.executable
    resolved_runner = shutil.which(node_name) if not Path(node_name).is_absolute() else str(Path(node_name).resolve())
    if resolved_runner is None or Path(resolved_runner).resolve() != node:
        result.update({"status": "UNSUPPORTED", "detail": "approved Node runner does not resolve to VKIT_NODE_BIN"})
        return result
    env = _service_env()
    deadline = time.monotonic() + timeout_seconds
    try:
        version = subprocess.run([str(node), "--version"], capture_output=True, env=env, timeout=min(10, _remaining(deadline)),
                                 check=False, **hidden_window())
    except (OSError, subprocess.TimeoutExpired):
        result.update({"status": "UNAVAILABLE", "detail": "configured Node executable could not be started"})
        return result
    if version.returncode != 0 or version.stdout.decode("utf-8", "replace").strip() != NODE_VERSION:
        result.update({"status": "UNAVAILABLE", "detail": f"configured Node executable is not {NODE_VERSION}"})
        return result
    result["engine_identity"] = {"protocol_version": PROTOCOL_VERSION,
                                 "stryker_instrumenter": ENGINE_VERSION, "node": NODE_VERSION,
                                 "helper_sha256": helper_sha, "package_lock_sha256": lock_sha}

    try:
        _assert_fresh(request, deadline)
        baseline = _execute(request, node, env, request.source_text.encode("utf-8"), deadline)
        result["baseline"] = baseline
        _assert_fresh(request, deadline)
        if baseline["status"] != "PASS":
            result.update({"status": "UNKNOWN", "detail": "fresh baseline did not pass every required case"})
            return result
        if not request.check.required_tests:
            result.update({"status": "UNKNOWN", "detail": "approved Node check has no required cases"})
            return result
        generated, mutants = _engine_mutants(request, helper, node, env, max_mutants, _remaining(deadline))
        if generated < len(mutants) or len(mutants) > max_mutants:
            raise ValueError("mutation instrumenter returned inconsistent generation counts")
        result["counts"].update({"generated": generated, "unexamined": generated})
        outcome_rows: list[dict[str, Any]] = []
        stop_campaign = False
        for mutant in mutants:
            _assert_fresh(request, deadline)
            row: dict[str, Any] = {"id": mutant.get("id"), "status": "UNKNOWN", "edit": None,
                                   "trial": None, "replay": {"status": "NOT_RUN", "detail": None}}
            try:
                content, edit = _literal_edit(request, mutant)
                row["edit"] = edit
                result["counts"]["attempted"] += 1
                trial = _execute(request, node, env, content, deadline)
                row["trial"] = trial
                result["counts"]["tested"] += 1 if trial.get("check_started") else 0
                if trial["status"] == "FAIL":
                    row["status"] = "DETECTED"
                    row["replay"] = {"status": "NOT_REQUIRED", "detail": "the same required cases reported assertion failures"}
                elif trial["status"] == "PASS":
                    replay = _execute(request, node, env, content, deadline)
                    row["replay"] = replay
                    row["status"] = "SURVIVED" if replay["status"] == "PASS" else "UNKNOWN"
                else:
                    row["status"] = "UNKNOWN"
                    row["replay"] = {"status": "NOT_RUN", "detail": "first trial was inconclusive"}
            except TimeoutError as exc:
                row["status"] = "UNKNOWN"
                row["detail"] = str(exc)
                stop_campaign = isinstance(exc, _CampaignTimeout)
            except (ValueError, UnicodeError) as exc:
                row["status"] = "UNKNOWN"
                row["detail"] = str(exc)
            outcome_rows.append(row)
            result["mutants"] = outcome_rows
            result["counts"]["unexamined"] -= 1
            if stop_campaign:
                break
            _assert_fresh(request, deadline)
        if result["counts"]["unexamined"]:
            result.update({"status": "UNKNOWN", "detail": "campaign stopped at the mutant or time bound; unexamined edits remain"})
        elif any(row["status"] == "UNKNOWN" for row in outcome_rows):
            result.update({"status": "UNKNOWN", "detail": "one or more mutation trials or replay were inconclusive"})
        else:
            result["status"] = "ANALYZED"
        _assert_fresh(request, deadline)
        return result
    except TimeoutError as exc:
        result.update({"status": "UNKNOWN", "detail": str(exc)})
        return result
    except _Unsupported as exc:
        result.update({"status": "UNSUPPORTED", "detail": str(exc)})
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update({"status": "UNKNOWN", "detail": str(exc)})
        return result

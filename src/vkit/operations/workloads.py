"""Compare one approved scenario check across a committed and current subject."""
from __future__ import annotations

import hashlib
import math
import os
import platform
import random
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from statistics import median
from typing import Any

from ..inputs import canonical_digest, is_secret, matches, source_files
from ..manifest import Manifest, parse_manifest
from ..nowindow import hidden_window
from ..paths import Project, open_project
from ..proc import run as run_process
from ..runner import _child_env, missing_prerequisite
from ..store import Store
from ..verifiers import dispatch
from ..verifiers.spec import NativeCheckSpec, ScenarioCheck

MIN_PAIRS = 10
MAX_PAIRS = 20
MAX_CAMPAIGN_SECONDS = 600
MAX_INPUT_FILES = 1_000
MAX_FILE_BYTES = 1_048_576
MAX_INPUT_BYTES = 16_777_216
MAX_REPORT_BYTES = 8_388_608
BOOTSTRAP_SAMPLES = 10_000
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class _Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class _InputFile:
    path: str
    content: bytes
    sha256: str
    mode: int


@dataclass(frozen=True)
class _Request:
    project: Project
    store: Store
    manifest: Manifest
    check: ScenarioCheck
    check_digest: str
    run_id: str
    record: dict[str, Any]
    files: tuple[_InputFile, ...]
    subjects: tuple[str, ...]
    before_ref: str
    before_files: tuple[_InputFile, ...]
    environment: dict[str, str]
    environment_metadata: dict[str, str]
    recorded_report_sha256: str


def _inside(root: Path, relative: str, label: str) -> Path:
    target = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        raise _Unsupported(f"{label} {relative!r} is missing") from None
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise _Unsupported(f"{label} {relative!r} escapes the repository")
    try:
        info = target.lstat()
    except OSError:
        raise _Unsupported(f"{label} {relative!r} is missing") from None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise _Unsupported(f"{label} {relative!r} is a symlink or reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise _Unsupported(f"{label} {relative!r} is not a regular file")
    return target


def _read_input(project: Project, relative: str, *, max_file_bytes: int = MAX_FILE_BYTES) -> tuple[_InputFile, str]:
    path = _inside(project.root, relative, "check input")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _Unsupported(f"check input {relative!r} changed while it was captured") from None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_file_bytes:
            raise _Unsupported(f"check input {relative!r} exceeds the per-file byte limit")
        content = source.read(max_file_bytes + 1)
        if len(content) > max_file_bytes or source.read(1):
            raise _Unsupported(f"check input {relative!r} exceeds the per-file byte limit")
    normalized = hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()
    return _InputFile(relative, content, hashlib.sha256(content).hexdigest(), stat.S_IMODE(info.st_mode)), normalized


def _capture_inputs(project: Project, check: NativeCheckSpec, record: dict[str, Any], *,
                    max_file_bytes: int = MAX_FILE_BYTES,
                    max_total_bytes: int = MAX_INPUT_BYTES) -> tuple[_InputFile, ...]:
    available = source_files(project)
    selected = tuple(name for name in available if not check.inputs or any(matches(name, pattern)
                                                                           for pattern in check.inputs))
    unmatched = tuple(pattern for pattern in check.inputs
                      if not any(matches(name, pattern) for name in available))
    if unmatched:
        raise _Unsupported("approved check inputs no longer match files: " + ", ".join(unmatched))
    if len(selected) > MAX_INPUT_FILES:
        raise _Unsupported("check inputs exceed the file-count limit")
    recorded = record.get("inputs")
    if not isinstance(recorded, dict):
        raise _Unsupported("recorded run has no usable input snapshot")
    recorded_files = recorded.get("files")
    if not isinstance(recorded_files, list):
        raise _Unsupported("recorded run has no usable input snapshot")
    expected = {}
    for row in recorded_files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) \
                or not isinstance(row.get("sha256"), str) or row["path"] in expected:
            raise _Unsupported("recorded input snapshot is malformed")
        expected[row["path"]] = row["sha256"]
    if set(selected) != set(expected):
        raise _Unsupported("current files do not exactly match the recorded input snapshot")
    actual: dict[str, str] = {}
    files: list[_InputFile] = []
    total = 0
    for relative in selected:
        if is_secret(relative):
            raise _Unsupported(f"secret-like input {relative!r} cannot be captured")
        item, normalized = _read_input(project, relative, max_file_bytes=max_file_bytes)
        total += len(item.content)
        if total > max_total_bytes:
            raise _Unsupported("check inputs exceed the total byte limit")
        actual[relative] = normalized
        files.append(item)
    if actual != expected or canonical_digest([list(item) for item in sorted(actual.items())]) != recorded.get("digest"):
        raise _Unsupported("current files do not exactly match the recorded input snapshot")
    if not actual:
        raise _Unsupported("approved check has no captured input files")
    return tuple(files)


def _git(project: Project, *argv: str, timeout: float = 10) -> bytes:
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    try:
        result = subprocess.run(["git", "--literal-pathspecs", *argv], cwd=project.root, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout, check=False, env=env, **hidden_window())
    except (OSError, subprocess.TimeoutExpired):
        raise _Unsupported("the local Git object could not be read within the limit") from None
    if result.returncode != 0:
        raise _Unsupported("the requested Git object or subject file is unavailable")
    return result.stdout


def _commit_files(project: Project, before_ref: Any, subjects: tuple[str, ...]) -> tuple[_InputFile, ...]:
    if not isinstance(before_ref, str) or not _COMMIT.fullmatch(before_ref):
        raise _Unsupported("before_ref must be a full immutable Git commit SHA")
    resolved = _git(project, "rev-parse", "--verify", f"{before_ref}^{{commit}}").decode("ascii", "replace").strip()
    if resolved != before_ref:
        raise _Unsupported("before_ref does not resolve to the supplied full commit SHA")
    files = []
    total = 0
    for relative in subjects:
        entry = _git(project, "ls-tree", "-z", before_ref, "--", relative)
        rows = [row for row in entry.split(b"\0") if row]
        if len(rows) != 1 or b"\t" not in rows[0]:
            raise _Unsupported(f"subject {relative!r} is missing or ambiguous in before_ref")
        metadata, path_bytes = rows[0].split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ", 2)
        if path_bytes.decode("utf-8", "surrogateescape") != relative or mode not in {"100644", "100755"} \
                or kind != "blob":
            raise _Unsupported(f"subject {relative!r} is a symlink, submodule, or unsupported Git entry")
        size_raw = _git(project, "cat-file", "-s", object_id)
        try:
            size = int(size_raw.strip())
        except ValueError:
            raise _Unsupported(f"Git returned an invalid size for {relative!r}") from None
        if size < 0 or size > MAX_FILE_BYTES:
            raise _Unsupported(f"old subject {relative!r} exceeds the per-file byte limit")
        content = _git(project, "cat-file", "blob", object_id)
        if len(content) != size:
            raise _Unsupported(f"old subject {relative!r} changed while it was captured")
        total += size
        if total > MAX_INPUT_BYTES:
            raise _Unsupported("old subject files exceed the total byte limit")
        files.append(_InputFile(relative, content, hashlib.sha256(content).hexdigest(), int(mode, 8) & 0o777))
    return tuple(files)


def _environment() -> tuple[dict[str, str], dict[str, str]]:
    env = _child_env()
    fingerprint = canonical_digest(sorted(env.items()))
    metadata = {"python": sys.version.split()[0], "platform": platform.platform(),
                "environment_sha256": fingerprint}
    return env, metadata


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
    digest = manifest.digest(check_id)
    if record.get("check_digest") != digest or digest not in store.approved():
        raise _Unsupported("check definition changed or is no longer approved")
    outcome = record.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("result") != "PASS":
        raise _Unsupported("the captured scenario check must have a completed PASS run")
    if not check.budgets:
        raise _Unsupported("scenario check must declare at least one measured budget")
    if not check.subject.paths:
        raise _Unsupported("scenario check must declare subject paths")
    files = _capture_inputs(project, check, record)
    names = {item.path for item in files}
    subjects = tuple(sorted(path for path in names if any(matches(path, pattern) for pattern in check.subject.paths)))
    if not subjects:
        raise _Unsupported("approved subject paths match no captured check inputs")
    command_inputs = {
        PurePosixPath(argument.replace("\\", "/")).as_posix()
        for argument in check.command if "{{" not in argument and not argument.startswith("-")
    }
    overlapping_harness = sorted(set(subjects) & command_inputs)
    if overlapping_harness:
        raise _Unsupported("measured subject includes a check command input: " + ", ".join(overlapping_harness))
    budget_names = [budget.name for budget in check.budgets]
    if len(set(budget_names)) != len(budget_names):
        raise _Unsupported("check contains duplicate budget names")

    store_run_dir = store.run_dir(run_id)
    artifact_path = store_run_dir / check.artifact_name
    try:
        info = artifact_path.lstat()
    except OSError:
        raise _Unsupported("recorded passing scenario report is missing") from None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise _Unsupported("recorded scenario report is not a regular file")
    if info.st_size > MAX_REPORT_BYTES:
        raise _Unsupported("recorded scenario report exceeds the report byte limit")
    try:
        descriptor = os.open(artifact_path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise _Unsupported("recorded scenario report changed while captured") from None
    with os.fdopen(descriptor, "rb") as source:
        report = source.read(MAX_REPORT_BYTES + 1)
        if len(report) > MAX_REPORT_BYTES or source.read(1):
            raise _Unsupported("recorded scenario report exceeds the report byte limit")
    reading = dispatch.interpret(check, report)
    report_ids = [row.scenario_id for row in reading.scenarios]
    if reading.problem is not None or set(report_ids) != {
            obligation.test_id for obligation in check.required_scenarios} \
            or len(report_ids) != len(set(report_ids)) \
            or any(not row.passed for row in reading.scenarios):
        raise _Unsupported("recorded report does not prove every required scenario passed")
    measurement_names = {measurement.name for measurement in reading.measurements}
    if measurement_names != {budget.name for budget in check.budgets}:
        raise _Unsupported("recorded report measurements do not exactly match the check budgets")
    if len(measurement_names) != len(reading.measurements) or any(
            not math.isfinite(measurement.value) for measurement in reading.measurements):
        raise _Unsupported("recorded report measurements are duplicated or non-finite")

    before_files = _commit_files(project, before_ref, subjects)
    before_map = {item.path: item for item in before_files}
    if set(before_map) != set(subjects):
        raise _Unsupported("before_ref does not contain every measured subject")
    env, environment = _environment()
    report_sha256 = hashlib.sha256(report).hexdigest()
    return _Request(project, store, manifest, check, digest, run_id, record, files, subjects,
                    before_ref, before_files, env, environment, report_sha256)


def _assert_fresh(request: _Request) -> None:
    manifest = parse_manifest(request.project)
    if manifest.digest(request.check.id) != request.check_digest \
            or request.check_digest not in request.store.approved():
        raise _Unsupported("approved check definition changed during the campaign")
    _capture_inputs(request.project, request.check, request.record)


def _write_workspace(files: tuple[_InputFile, ...], destination: Path, replacement: dict[str, _InputFile]) -> Path:
    for item in files:
        raw = replacement.get(item.path, item).content
        path = destination.joinpath(*PurePosixPath(item.path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if os.name != "nt":
            path.chmod(replacement.get(item.path, item).mode)
    cwd = destination
    return cwd


def _read_sample(request: _Request, side: str, deadline: float) -> tuple[dict[str, Any] | None, str | None]:
    if time.monotonic() >= deadline:
        return None, "campaign exceeded its total time budget"
    replacements = {item.path: item for item in request.before_files} if side == "before" else {}
    with tempfile.TemporaryDirectory(prefix="vkit-workload-") as raw_workspace:
        workspace = Path(raw_workspace)
        _write_workspace(request.files, workspace, replacements)
        cwd = workspace.joinpath(*request.check.cwd.relative_to(request.project.root).parts)
        cwd.mkdir(parents=True, exist_ok=True)
        run_dir = workspace / ".vkit-workload-run"
        run_dir.mkdir()
        artifact = run_dir / request.check.artifact_name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        check = replace(request.check, cwd=cwd)
        argv = dispatch.argv_for(check, run_dir, None)
        timeout = min(request.check.timeout_seconds, deadline - time.monotonic())
        if timeout <= 0:
            return None, "campaign exceeded its total time budget"
        result = run_process(argv, cwd=cwd, env=request.environment,
                             stdout_path=run_dir / "stdout.log", stderr_path=run_dir / "stderr.log",
                             timeout_seconds=timeout)
        if result.launch_error or result.cancelled or result.timed_out:
            reason = "scenario command could not start" if result.launch_error else (
                "scenario command was cancelled" if result.cancelled else "scenario command timed out")
            return None, reason
        try:
            artifact_info = artifact.lstat()
        except OSError:
            return None, "scenario command did not write its report"
        if not stat.S_ISREG(artifact_info.st_mode) or getattr(artifact_info, "st_file_attributes", 0) & _REPARSE_POINT:
            return None, "scenario report is not a regular file"
        if artifact_info.st_size > MAX_REPORT_BYTES:
            return None, "scenario report exceeded the byte limit"
        if result.code not in {0, 1}:
            return None, "scenario command exited with an untrusted status"
        try:
            descriptor = os.open(artifact, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                                 | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None, "scenario report changed while captured"
        with os.fdopen(descriptor, "rb") as source:
            raw_report = source.read(MAX_REPORT_BYTES + 1)
            if len(raw_report) > MAX_REPORT_BYTES or source.read(1):
                return None, "scenario report exceeded the byte limit"
        reading = dispatch.interpret(check, raw_report)
        if reading.problem is not None:
            return None, reading.problem.detail
        expected_scenarios = {item.test_id for item in request.check.required_scenarios}
        actual_scenarios = [item.scenario_id for item in reading.scenarios]
        if set(actual_scenarios) != expected_scenarios or len(actual_scenarios) != len(expected_scenarios):
            return None, "scenario identities did not exactly match the approved check"
        if any(not item.passed for item in reading.scenarios):
            return None, "a required scenario failed, so its measurement is not comparable"
        actual_measurements = {item.name: item for item in reading.measurements}
        expected_measurements = {item.name for item in request.check.budgets}
        if len(actual_measurements) != len(reading.measurements) or set(actual_measurements) != expected_measurements:
            return None, "measurement names did not exactly match the approved budgets"
        return ({**{name: measurement.to_json() for name, measurement in actual_measurements.items()},
                 "_report_sha256": hashlib.sha256(raw_report).hexdigest()}, None)


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def _interval(values: list[float], seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    estimates = [median([values[rng.randrange(len(values))] for _ in values])
                 for _ in range(BOOTSTRAP_SAMPLES)]
    return _quantile(estimates, 0.025), _quantile(estimates, 0.975)


def _result_for_measurement(name: str, pairs: list[dict[str, float]], budget: Any,
                            identity: dict[str, Any]) -> dict[str, Any]:
    regressions = [row["regression_pct"] for row in pairs]
    seed_bytes = canonical_digest({**identity, "measurement": name, "pairs": pairs}).encode("ascii")
    low, high = _interval(regressions, int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "big"))
    threshold = budget.max_regression_pct
    if threshold is None:
        status = "NO_REGRESSION_BUDGET"
    elif low > threshold:
        status = "OVER_BUDGET"
    elif high <= threshold:
        status = "WITHIN_BUDGET"
    else:
        status = "INCONCLUSIVE"
    return {"name": name, "unit": pairs[0]["unit"], "better": pairs[0]["better"],
            "samples": pairs, "median_regression_pct": median(regressions),
            "confidence_interval_95_pct": [low, high],
            "max_regression_pct": threshold, "status": status}


def compare_workload_cost(root: Path, run_id: str, before_ref: str, *, pairs: int = 10,
                          timeout_seconds: int = MAX_CAMPAIGN_SECONDS) -> dict[str, Any]:
    """Run one approved scenario check in paired fresh workspaces and compare measurements."""
    if type(pairs) is not int or not MIN_PAIRS <= pairs <= MAX_PAIRS:
        raise ValueError(f"pairs must be an integer in [{MIN_PAIRS}, {MAX_PAIRS}]")
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= MAX_CAMPAIGN_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [30, {MAX_CAMPAIGN_SECONDS}]")
    result: dict[str, Any] = {"operation": "compare_workload_cost", "status": "UNSUPPORTED",
                              "run_id": run_id if isinstance(run_id, str) else "",
                              "before_ref": before_ref if isinstance(before_ref, str) else "",
                              "requested_pairs": pairs, "completed_pairs": 0,
                              "samples": [], "measurements": [], "detail": None}
    try:
        request = _prepare(root, run_id, before_ref)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["detail"] = str(exc)
        return result
    result.update({"check_id": request.check.id, "check_digest": request.check_digest,
                   "input_snapshot_digest": request.record["inputs"]["digest"],
                   "captured_inputs": [{"path": item.path, "sha256": item.sha256}
                                       for item in request.files],
                   "recorded_report_sha256": request.recorded_report_sha256,
                   "candidate": {"run_id": run_id, "subjects": [
                       {"path": item.path, "sha256": item.sha256, "mode": item.mode}
                       for item in request.files if item.path in request.subjects]},
                   "before": {"commit": request.before_ref, "subjects": [
                       {"path": item.path, "sha256": item.sha256, "mode": item.mode}
                       for item in request.before_files]},
                   "environment": request.environment_metadata})
    deadline = time.monotonic() + timeout_seconds
    budget_by_name = {budget.name: budget for budget in request.check.budgets}
    collected: dict[str, list[dict[str, float]]] = {name: [] for name in budget_by_name}
    identity = {"check_digest": request.check_digest, "before_ref": request.before_ref,
                "candidate": result["candidate"]}
    measurement_meta: dict[str, tuple[str, str]] = {}
    failure = None
    for pair_index in range(pairs):
        pair_samples = []
        order = ("before", "candidate") if pair_index % 2 == 0 else ("candidate", "before")
        for side in order:
            try:
                _assert_fresh(request)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                failure = str(exc)
                break
            try:
                readings, detail = _read_sample(request, side, deadline)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                failure = f"scenario sample could not be interpreted: {exc}"
                break
            if detail is not None or readings is None:
                failure = detail or "scenario run returned no measurements"
                break
            pair_samples.append((side, readings))
        if failure is not None:
            break
        values = {side: readings for side, readings in pair_samples}
        pair_result = {"pair": pair_index + 1, "order": list(order)}
        pair_result["report_sha256"] = {side: readings["_report_sha256"]
                                        for side, readings in pair_samples}
        for name in budget_by_name:
            before = values["before"][name]
            candidate = values["candidate"][name]
            meta = (before["unit"], before["better"])
            if meta != (candidate["unit"], candidate["better"]) or \
                    (name in measurement_meta and measurement_meta[name] != meta) or \
                    not math.isfinite(before["value"]) or not math.isfinite(candidate["value"]):
                failure = f"measurement {name!r} changed units, direction, or returned a non-finite value"
                break
            measurement_meta[name] = meta
            base_value, candidate_value = float(before["value"]), float(candidate["value"])
            if base_value <= 0:
                failure = f"measurement {name!r} has a nonpositive baseline, so relative regression is undefined"
                break
            delta = candidate_value - base_value
            change_pct = delta / abs(base_value) * 100
            regression_pct = change_pct if before["better"] == "lower" else -change_pct
            if not all(math.isfinite(value) for value in (delta, change_pct, regression_pct)):
                failure = f"measurement {name!r} produced a non-finite paired regression"
                break
            row = {"pair": pair_index + 1, "before": base_value, "candidate": candidate_value,
                   "delta": delta, "change_pct": change_pct,
                   "regression_pct": regression_pct, "unit": before["unit"], "better": before["better"]}
            collected[name].append(row)
            pair_result.setdefault("measurements", {})[name] = row
        if failure is not None:
            break
        result["samples"].append(pair_result)
        result["completed_pairs"] += 1
    try:
        _assert_fresh(request)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        failure = failure or str(exc)
    if failure is not None or result["completed_pairs"] != pairs:
        result.update({"status": "INCONCLUSIVE", "detail": failure or "campaign ended before all requested pairs completed"})
        return result
    result["measurements"] = [_result_for_measurement(name, collected[name], budget_by_name[name], identity)
                               for name in budget_by_name]
    result["status"] = "MEASURED"
    return result

"""Run one explicitly approved offline build, reproducibility, install, and import check."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..inputs import Snapshot, is_secret, matches
from ..manifest import ManifestError, parse_manifest
from ..nowindow import hidden_window
from ..outcome import BlockedReason
from ..paths import Project, open_project
from ..proc import run as run_process
from ..schemas import BUILD_ARTIFACT, SchemaValidationError, validate
from ..store import Store
from ..verifiers.dispatch import _build_profile
from ..verifiers.spec import BuildCheck
from ._data import encode_json
from .workloads import (
    MAX_INPUT_FILES,
    _capture_inputs,
    _commit_files,
    _git,
    _Unsupported,
    _write_workspace,
)

PROFILE = "offline-wheel-build-install-v1"
MAX_TOOL_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_INPUT_FILE_BYTES = 64 * 1024 * 1024
MAX_INPUT_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 256 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_LOG_FILES = 64
MAX_BASELINE_TREE_BYTES = 16 * 1024 * 1024
MAX_INCREMENTAL_PATH_BYTES = 512 * 1024
_IMPORT_PROBE = (
    "import importlib,json,sys; "
    "rows=[]; "
    "[(rows.append({'module':name,'origin':getattr(module,'__file__',None)}), None) "
    "for name in sys.argv[1:] for module in [importlib.import_module(name)]]; "
    "print(json.dumps(rows,separators=(',',':')))"
)


class _Blocked(Exception):
    def __init__(self, reason: BlockedReason, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


def _sha256(path: Path, *, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            total += len(block)
            if limit is not None and total > limit:
                raise _Blocked(
                    BlockedReason.INPUT_MISSING,
                    f"{path.name} exceeds the {limit}-byte limit",
                )
            digest.update(block)
    return digest.hexdigest()


def _read_bounded(path: Path, limit: int = MAX_PROCESS_OUTPUT_BYTES) -> bytes:
    if not path.is_file() or path.stat().st_size > limit:
        raise _Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            f"{path.name} exceeds the {limit}-byte process-output limit",
        )
    raw = path.read_bytes()
    if len(raw) > limit:
        raise _Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            f"{path.name} changed beyond its output limit",
        )
    return raw


def _environment(
    *, temp: Path, cache: Path, scripts: Path | None = None
) -> dict[str, str]:
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    path_parts = [str(scripts)] if scripts is not None else []
    path_parts.extend(part for part in env.get("PATH", "").split(os.pathsep) if part)
    env["PATH"] = os.pathsep.join(path_parts)
    env["TEMP"] = str(temp)
    env["TMP"] = str(temp)
    env["UV_CACHE_DIR"] = str(cache)
    env["UV_NO_PROGRESS"] = "1"
    env["UV_OFFLINE"] = "1"
    env["UV_PYTHON_DOWNLOADS"] = "never"
    return env


def _record_command(
    label: str,
    argv: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    run_dir: Path,
    timeout: float,
) -> tuple[Any, bytes, bytes]:
    if len(list(run_dir.glob("*.stdout.log"))) >= MAX_LOG_FILES:
        raise _Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            "the build check exceeded its process-log count limit",
        )
    stdout_path = run_dir / f"{label}.stdout.log"
    stderr_path = run_dir / f"{label}.stderr.log"
    result = run_process(
        argv,
        cwd=cwd,
        env=env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=timeout,
        max_output_bytes=MAX_PROCESS_OUTPUT_BYTES,
    )
    if result.output_limited:
        raise _Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            f"{label} exceeded the {MAX_PROCESS_OUTPUT_BYTES}-byte process-output limit",
        )
    stdout = _read_bounded(stdout_path)
    stderr = _read_bounded(stderr_path)
    return result, stdout, stderr


def _log_evidence(raw: bytes) -> dict[str, Any]:
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _assertion(
    identifier: str, expected: Any, observed: Any, passed: bool
) -> dict[str, Any]:
    return {
        "id": identifier,
        "result": "PASS" if passed else "FAIL",
        "expected": expected,
        "observed": observed,
    }


def _base(check: BuildCheck) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "BLOCKED",
        "profile": PROFILE,
        "toolchain": {
            "uv_version": check.uv_version,
            "uv_sha256": check.uv_sha256,
            "python_version": check.python_version,
            "python_sha256": check.python_sha256,
        },
        "builds": [],
        "assertions": [],
    }


def _report(report_path: Path, value: dict[str, Any]) -> None:
    validate("build artifact", BUILD_ARTIFACT, value)
    encoded = encode_json(value, max_bytes=MAX_REPORT_BYTES).encode("utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(encoded)


def _blocked(
    report_path: Path,
    check: BuildCheck,
    reason: BlockedReason,
    detail: str,
    report: dict[str, Any] | None = None,
) -> int:
    result = _base(check) if report is None else report
    result["status"] = "BLOCKED"
    result["problem"] = {"reason": reason.value, "detail": detail[:2048]}
    _report(report_path, result)
    return 0


def _version(path: Path, expected: str) -> None:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
            **hidden_window(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _Blocked(
            BlockedReason.PREREQUISITE_MISSING,
            "the pinned uv executable could not report its version",
        ) from None
    output = result.stdout[:4096].decode("utf-8", "replace").strip()
    if (
        len(result.stdout) > 4096
        or len(result.stderr) > 4096
        or result.returncode != 0
        or not re.search(rf"(?:^|\s)uv {re.escape(expected)}(?:\s|$)", output)
    ):
        raise _Blocked(
            BlockedReason.PREREQUISITE_MISSING,
            "the pinned uv executable version does not match",
        )


def _project_and_check(profile: dict[str, Any]) -> tuple[Project, BuildCheck, str]:
    project = open_project(profile["cwd"])
    manifest = parse_manifest(project)
    check = manifest.require(profile["id"])
    profile_check = {
        key: value for key, value in profile.items() if key != "input_digest"
    }
    if not isinstance(check, BuildCheck) or _build_profile(check) != profile_check:
        raise _Blocked(
            BlockedReason.NOT_APPROVED,
            "the build profile no longer matches its registered check",
        )
    if manifest.digest(check.id) not in Store.of(project).approved():
        raise _Blocked(
            BlockedReason.NOT_APPROVED, "the build profile is no longer approved"
        )
    return project, check, manifest.digest(check.id)


def _capture(project: Project, check: BuildCheck, expected_digest: str):
    snapshot = Snapshot(project).inputs(check.inputs)
    if snapshot.digest != expected_digest:
        raise _Blocked(
            BlockedReason.INPUT_MISSING,
            "captured project inputs differ from the approved run snapshot",
        )
    if snapshot.unmatched:
        raise _Blocked(
            BlockedReason.INPUT_MISSING,
            "approved build inputs no longer match files: "
            + ", ".join(snapshot.unmatched),
        )
    try:
        files = _capture_inputs(
            project,
            check,
            {"inputs": snapshot.to_json()},
            max_file_bytes=MAX_INPUT_FILE_BYTES,
            max_total_bytes=MAX_INPUT_TOTAL_BYTES,
        )
    except _Unsupported as exc:
        raise _Blocked(BlockedReason.INPUT_MISSING, str(exc)) from None
    selected = {item.path for item in files}
    wheelhouse_files = [
        path for path in selected if path.startswith(check.wheelhouse.rstrip("/") + "/")
    ]
    if not wheelhouse_files:
        raise _Blocked(
            BlockedReason.INPUT_MISSING, "the captured wheelhouse contains no files"
        )
    return files, snapshot


def _baseline_inputs(project: Project, check: BuildCheck, current_files):
    baseline_sha = check.incremental_baseline_sha
    if baseline_sha is None:
        return None
    try:
        resolved = _git(project, "rev-parse", "--verify", f"{baseline_sha}^{{commit}}").decode("ascii").strip()
        if resolved != baseline_sha:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "incremental baseline is not the supplied full commit SHA")
        tree = _git(project, "ls-tree", "-r", "-l", "-z", "--full-tree", baseline_sha)
    except _Unsupported as exc:
        raise _Blocked(BlockedReason.INPUT_MISSING, str(exc)) from None
    if len(tree) > MAX_BASELINE_TREE_BYTES:
        raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "incremental baseline tree exceeds its byte limit")

    paths: list[str] = []
    total_bytes = 0
    for row in tree.split(b"\0"):
        if not row:
            continue
        if b"\t" not in row:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "Git returned a malformed incremental baseline tree")
        metadata, raw_path = row.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) < 3:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "Git returned malformed incremental baseline metadata")
        try:
            path = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "incremental baseline path is not UTF-8") from None
        if is_secret(path) or (check.inputs and not any(matches(path, pattern) for pattern in check.inputs)):
            continue
        relative = PurePosixPath(path)
        if (
            not path
            or relative.is_absolute()
            or PureWindowsPath(path).drive
            or "\\" in path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "incremental baseline path cannot be materialized safely")
        mode, kind, _object_id = fields[:3]
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, f"incremental baseline path {path!r} is a symlink, submodule, or unsupported Git entry")
        if len(fields) != 4:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "Git returned malformed incremental baseline size metadata")
        raw_size = fields[3]
        try:
            size = int(raw_size)
        except ValueError:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, f"incremental baseline size for {path!r} is invalid") from None
        if size < 0 or size > MAX_INPUT_FILE_BYTES:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, f"incremental baseline input {path!r} exceeds the per-file byte limit")
        total_bytes += size
        if total_bytes > MAX_INPUT_TOTAL_BYTES:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "incremental baseline exceeds the captured total byte limit")
        paths.append(path)
        if len(paths) > MAX_INPUT_FILES:
            raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "incremental baseline exceeds the captured file-count limit")
    if not paths:
        raise _Blocked(BlockedReason.INPUT_MISSING, "incremental baseline has no files matching the approved input closure")
    try:
        baseline = _commit_files(
            project,
            baseline_sha,
            tuple(sorted(paths)),
            max_file_bytes=MAX_INPUT_FILE_BYTES,
            max_total_bytes=MAX_INPUT_TOTAL_BYTES,
        )
    except _Unsupported as exc:
        raise _Blocked(BlockedReason.INPUT_MISSING, str(exc)) from None

    old = {item.path: item for item in baseline}
    current = {item.path: item for item in current_files}
    path_bytes = sum(len(path.encode("utf-8")) for path in old) + sum(
        len(path.encode("utf-8")) for path in current
    )
    if path_bytes > MAX_INCREMENTAL_PATH_BYTES:
        raise _Blocked(BlockedReason.ARTIFACT_MALFORMED, "incremental path evidence exceeds its byte limit")
    support_paths = {check.build_constraints, check.runtime_requirements}
    old_wheelhouse = {path for path in old if path.startswith(check.wheelhouse.rstrip("/") + "/")}
    current_wheelhouse = {path for path in current if path.startswith(check.wheelhouse.rstrip("/") + "/")}
    if (
        not support_paths <= old.keys()
        or not support_paths <= current.keys()
        or not old_wheelhouse
        or old_wheelhouse != current_wheelhouse
        or any(old[path].sha256 != current[path].sha256 for path in support_paths | old_wheelhouse)
    ):
        raise _Blocked(
            BlockedReason.INPUT_MISSING,
            "incremental baseline must retain the approved build locks and exact wheelhouse bytes",
        )
    return baseline


def _wheel(path: Path, artifact_dir: Path, index: int) -> tuple[dict[str, Any], bytes]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > MAX_ARCHIVE_BYTES
    ):
        raise _Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            "uv produced a missing, linked, or oversized wheel",
        )
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise _Blocked(
            BlockedReason.ARTIFACT_MALFORMED,
            "uv wheel output exceeds the archive byte limit",
        )
    destination_dir = artifact_dir / f"build-{index}"
    destination_dir.mkdir(exist_ok=True)
    destination = destination_dir / path.name
    destination.write_bytes(raw)
    return {
        "filename": path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "path": destination.name,
    }, raw


def _build_once(
    project: Project,
    check: BuildCheck,
    source: Path,
    output: Path,
    cache: Path,
    run_dir: Path,
    uv: Path,
    label: str,
    index: int,
    artifact_index: int | str,
    attempts: list[dict[str, Any]],
) -> bytes:
    output.mkdir()
    cwd = source.joinpath(*check.cwd.relative_to(project.root).parts)
    cwd.mkdir(parents=True, exist_ok=True)
    env = _environment(temp=cache.parent, cache=cache)
    argv = (
        str(uv),
        "build",
        "--wheel",
        "--out-dir",
        str(output),
        "--build-constraints",
        str(source / check.build_constraints),
        "--require-hashes",
        "--no-index",
        "--offline",
        "--no-config",
        "--no-python-downloads",
        "--find-links",
        str(source / check.wheelhouse),
        "--python",
        sys.executable,
        str(cwd),
    )
    result, stdout, stderr = _record_command(
        label, argv, cwd, env, run_dir, check.timeout_seconds
    )
    row: dict[str, Any] = {
        "index": index,
        "exit_code": result.code if result.code is not None else -1,
        "stdout_bytes": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_bytes": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "wheel": None,
    }
    attempts.append(row)
    if result.timed_out:
        raise _Blocked(BlockedReason.TIMEOUT, f"{label} exceeded the check timeout")
    if result.cancelled:
        raise _Blocked(BlockedReason.CANCELLED, f"{label} was cancelled")
    if result.launch_error:
        raise _Blocked(BlockedReason.LAUNCH_FAILED, f"{label} could not start: {result.launch_error}")
    if result.code != 0:
        raise _Blocked(
            BlockedReason.INTERNAL_ERROR,
            f"approved build backend exited {result.code} during {label}; this is an execution observation, not a package-defect finding",
        )
    output_wheels = tuple(output.glob("*.whl"))
    if len(output_wheels) != 1:
        raise _Blocked(
            BlockedReason.ARTIFACT_MISSING,
            f"{label} produced {len(output_wheels)} wheels; this profile requires exactly one",
        )
    wheel, raw = _wheel(output_wheels[0], run_dir / "artifacts", artifact_index)
    row["wheel"] = {key: wheel[key] for key in ("filename", "bytes", "sha256")}
    return raw


def _run_builds(
    project: Project,
    check: BuildCheck,
    files,
    run_dir: Path,
    uv: Path,
    builds: list[dict[str, Any]],
) -> list[bytes]:
    wheels: list[bytes] = []
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    for index in (1, 2):
        with tempfile.TemporaryDirectory(prefix=f"vkit-build-{index}-") as raw_temp:
            temp = Path(raw_temp)
            source = temp / "source"
            source.mkdir()
            _write_workspace(files, source, {})
            output = temp / "dist"
            cache = temp / "uv-cache"
            wheels.append(
                _build_once(
                    project,
                    check,
                    source,
                    output,
                    cache,
                    run_dir,
                    uv,
                    f"build-{index}",
                    index,
                    index,
                    builds,
                )
            )
    return wheels


def _apply_overlay(source: Path, baseline_files, current_files) -> list[str]:
    current_paths = {item.path for item in current_files}
    deleted = sorted(item.path for item in baseline_files if item.path not in current_paths)
    for relative in deleted:
        path = source.joinpath(*PurePosixPath(relative).parts)
        path.unlink()
        parent = path.parent
        while parent != source and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    _write_workspace(current_files, source, {})
    return deleted


def _verify_workspace_files(source: Path, files) -> None:
    for item in files:
        path = source.joinpath(*PurePosixPath(item.path).parts)
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or info.st_size > MAX_INPUT_FILE_BYTES
            ):
                raise OSError
            raw = path.read_bytes()
        except OSError:
            raise _Blocked(BlockedReason.SOURCE_CHANGED, f"build backend changed captured input {item.path!r}") from None
        if hashlib.sha256(raw).hexdigest() != item.sha256:
            raise _Blocked(BlockedReason.SOURCE_CHANGED, f"build backend changed captured input {item.path!r}")


def _file_rows(files) -> list[dict[str, Any]]:
    return [
        {"path": item.path, "sha256": item.sha256, "mode": 493 if item.mode & 0o111 else 420}
        for item in sorted(files, key=lambda item: item.path)
    ]


def _run_incremental(
    project: Project,
    check: BuildCheck,
    baseline_files,
    current_files,
    clean_wheel: dict[str, Any],
    run_dir: Path,
    uv: Path,
) -> dict[str, Any]:
    baseline_attempts: list[dict[str, Any]] = []
    overlay_attempts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="vkit-build-incremental-") as raw_temp:
        temp = Path(raw_temp)
        source = temp / "source"
        source.mkdir()
        _write_workspace(baseline_files, source, {})
        output_baseline = temp / "baseline-dist"
        cache = temp / "uv-cache"
        baseline_wheel = _build_once(
            project,
            check,
            source,
            output_baseline,
            cache,
            run_dir,
            uv,
            "incremental-baseline",
            0,
            "incremental-baseline",
            baseline_attempts,
        )
        _verify_workspace_files(source, baseline_files)
        deleted = _apply_overlay(source, baseline_files, current_files)
        output_overlay = temp / "overlay-dist"
        overlay_wheel = _build_once(
            project,
            check,
            source,
            output_overlay,
            cache,
            run_dir,
            uv,
            "incremental-overlay",
            3,
            3,
            overlay_attempts,
        )
        _verify_workspace_files(source, current_files)
    del baseline_wheel
    observed = overlay_attempts[0]["wheel"]
    passed = observed == clean_wheel
    return {
        "baseline_sha": check.incremental_baseline_sha,
        "baseline_inputs": _file_rows(baseline_files),
        "overlay_inputs": _file_rows(current_files),
        "deleted_inputs": deleted,
        "baseline_build": baseline_attempts[0],
        "overlay_build": overlay_attempts[0],
        "wheel_matches_clean": passed,
        "assertion": _assertion("incremental:wheel-matches-clean", clean_wheel, observed, passed),
    }


def _python_path(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _require_command(result: Any, step: str) -> None:
    if result.timed_out:
        raise _Blocked(BlockedReason.TIMEOUT, f"{step} exceeded the check timeout")
    if result.cancelled:
        raise _Blocked(BlockedReason.CANCELLED, f"{step} was cancelled")
    if result.launch_error:
        raise _Blocked(
            BlockedReason.LAUNCH_FAILED,
            f"{step} could not start: {result.launch_error}",
        )


def _smoke(check: BuildCheck, files, run_dir: Path, uv: Path) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    artifact_dir = run_dir / "artifacts"
    wheel_paths = [artifact_dir / f"build-{index}" / "*.whl" for index in (1, 2)]
    for index, matches in enumerate((tuple(path.parent.glob(path.name)) for path in wheel_paths), 1):
        if len(matches) != 1:
            raise _Blocked(BlockedReason.ARTIFACT_MISSING, f"clean build {index} did not retain exactly one wheel")
        wheel_path = matches[0]
        with tempfile.TemporaryDirectory(prefix=f"vkit-install-{index}-") as raw_temp:
            temp = Path(raw_temp)
            source = temp / "source"
            source.mkdir()
            _write_workspace(files, source, {})
            env_dir = temp / "venv"
            venv_env = _environment(temp=temp, cache=temp / "uv-cache")
            created, out, err = _record_command(
                f"venv-{index}",
                (
                    str(uv),
                    "venv",
                    "--python",
                    sys.executable,
                    "--offline",
                    "--no-config",
                    "--no-python-downloads",
                    str(env_dir),
                ),
                temp,
                venv_env,
                run_dir,
                check.timeout_seconds,
            )
            _require_command(created, f"uv venv {index}")
            if created.code != 0:
                raise _Blocked(
                    BlockedReason.PREREQUISITE_MISSING,
                    f"uv could not create the clean install environment {index}",
                )
            interpreter = _python_path(env_dir)
            if not interpreter.is_file():
                raise _Blocked(
                    BlockedReason.ARTIFACT_MISSING,
                    "uv venv omitted its Python interpreter",
                )
            wheelhouse = source / check.wheelhouse
            requirements = source / check.runtime_requirements
            deps, dep_out, dep_err = _record_command(
                f"runtime-deps-{index}",
                (
                    str(uv),
                    "pip",
                    "install",
                    "--python",
                    str(interpreter),
                    "--offline",
                    "--no-index",
                    "--find-links",
                    str(wheelhouse),
                    "--require-hashes",
                    "-r",
                    str(requirements),
                ),
                temp,
                venv_env,
                run_dir,
                check.timeout_seconds,
            )
            _require_command(deps, f"runtime dependency installation {index}")
            if deps.code != 0:
                raise _Blocked(
                    BlockedReason.INPUT_MISSING,
                    f"runtime lock or wheelhouse could not provision installation {index}; this is incomplete check setup",
                )
            installed, inst_out, inst_err = _record_command(
                f"install-wheel-{index}",
                (
                    str(uv),
                    "pip",
                    "install",
                    "--python",
                    str(interpreter),
                    "--offline",
                    "--no-index",
                    "--no-deps",
                    str(wheel_path),
                ),
                temp,
                venv_env,
                run_dir,
                check.timeout_seconds,
            )
            _require_command(installed, f"wheel installation {index}")
            if installed.code != 0:
                raise _Blocked(
                    BlockedReason.ARTIFACT_MALFORMED,
                    f"the built wheel could not be installed in clean environment {index}",
                )
            checked, check_out, check_err = _record_command(
                f"pip-check-{index}",
                (str(uv), "pip", "check", "--python", str(interpreter)),
                temp,
                venv_env,
                run_dir,
                check.timeout_seconds,
            )
            _require_command(checked, f"runtime dependency check {index}")
            if checked.code != 0:
                raise _Blocked(
                    BlockedReason.INPUT_MISSING,
                    f"the runtime lock does not close the built wheel dependencies in environment {index}",
                )
            imports, import_out, import_err = _record_command(
                f"imports-{index}",
                (str(interpreter), "-I", "-c", _IMPORT_PROBE, *check.imports),
                temp,
                _environment(
                    temp=temp, cache=temp / "uv-cache", scripts=interpreter.parent
                ),
                run_dir,
                check.timeout_seconds,
            )
            _require_command(imports, f"import smoke {index}")
            if imports.code != 0:
                raise _Blocked(
                    BlockedReason.ARTIFACT_MALFORMED,
                    f"an import probe failed in clean environment {index}",
                )
            try:
                observed_imports = json.loads(import_out.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise _Blocked(
                    BlockedReason.ARTIFACT_MALFORMED,
                    "import probe returned malformed evidence",
                ) from None
            correct_imports = (
                isinstance(observed_imports, list)
                and [item.get("module") for item in observed_imports]
                == list(check.imports)
                and all(
                    isinstance(item.get("origin"), str)
                    and Path(item["origin"]).resolve().is_relative_to(env_dir.resolve())
                    for item in observed_imports
                )
            )
            assertions.append(
                _assertion(
                    f"install-{index}:imports",
                    list(check.imports),
                    observed_imports,
                    correct_imports,
                )
            )
            for entry in check.entry_points:
                script = (
                    env_dir
                    / ("Scripts" if os.name == "nt" else "bin")
                    / (entry.name + (".exe" if os.name == "nt" else ""))
                )
                if not script.is_file():
                    assertions.append(
                        _assertion(
                            f"install-{index}:entrypoint:{entry.name}",
                            {"installed": True},
                            {"installed": False},
                            False,
                        )
                    )
                    continue
                endpoint_env = _environment(
                    temp=temp, cache=temp / "uv-cache", scripts=script.parent
                )
                result, stdout, stderr = _record_command(
                    f"entrypoint-{index}-{entry.name}",
                    (str(script), *entry.args),
                    temp,
                    endpoint_env,
                    run_dir,
                    check.timeout_seconds,
                )
                _require_command(
                    result, f"entry point {entry.name} in environment {index}"
                )
                observed = {
                    "exit_code": result.code,
                    "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                    "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                    "stderr_base64": base64.b64encode(stderr).decode("ascii"),
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                }
                expected = {
                    "exit_code": entry.expected_exit_code,
                    "stdout": entry.expected_stdout,
                    "stderr": entry.expected_stderr,
                }
                passed = (
                    result.code == entry.expected_exit_code
                    and stdout == entry.expected_stdout.encode("utf-8")
                    and stderr == entry.expected_stderr.encode("utf-8")
                )
                assertions.append(
                    _assertion(
                        f"install-{index}:entrypoint:{entry.name}",
                        expected,
                        observed,
                        passed,
                    )
                )
    return assertions


def _run(profile_path: Path, report_path: Path) -> int:
    try:
        profile_raw = profile_path.read_bytes()
        if len(profile_raw) > 256 * 1024:
            raise _Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build profile exceeds the input byte limit",
            )
        profile = json.loads(profile_raw.decode("utf-8"))
        project, check, check_digest = _project_and_check(profile)
        report = _base(check)
        uv = Path(check.uv_executable).resolve(strict=True)
        if (
            uv.is_dir()
            or uv.stat().st_size > MAX_TOOL_BYTES
            or _sha256(uv, limit=MAX_TOOL_BYTES) != check.uv_sha256
        ):
            raise _Blocked(
                BlockedReason.PREREQUISITE_MISSING,
                "the pinned uv executable is missing or has a different digest",
            )
        _version(uv, check.uv_version)
        python = Path(sys.executable).resolve(strict=True)
        if (
            platform.python_version() != check.python_version
            or _sha256(python, limit=MAX_TOOL_BYTES) != check.python_sha256
        ):
            raise _Blocked(
                BlockedReason.PREREQUISITE_MISSING,
                "the active Python runtime does not match the pinned interpreter",
            )
        input_digest = profile.get("input_digest")
        if not isinstance(input_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", input_digest
        ):
            raise _Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build profile is missing its captured input digest",
            )
        files, snapshot = _capture(project, check, input_digest)
        baseline_files = _baseline_inputs(project, check, files)
        current = Snapshot(project).inputs(check.inputs)
        if current.digest != snapshot.digest:
            raise _Blocked(
                BlockedReason.INPUT_MISSING,
                "captured project inputs changed during capture",
            )
        report["toolchain"] = {
            "uv_version": check.uv_version,
            "uv_sha256": check.uv_sha256,
            "python_version": platform.python_version(),
            "python_sha256": _sha256(python, limit=MAX_TOOL_BYTES),
        }
        report["builds"] = []
        wheels = _run_builds(
            project, check, files, report_path.parent, uv, report["builds"]
        )
        builds = report["builds"]
        same = len(wheels) == 2 and wheels[0] == wheels[1]
        report["same_wheel_bytes"] = same
        filenames_same = (
            builds[0]["wheel"]["filename"] == builds[1]["wheel"]["filename"]
        )
        assertions = [
            _assertion("builds:wheel-bytes-identical", True, same, same),
            _assertion(
                "builds:wheel-filename-identical",
                builds[0]["wheel"]["filename"],
                builds[1]["wheel"]["filename"],
                filenames_same,
            ),
        ]
        if baseline_files is not None:
            incremental = _run_incremental(
                project,
                check,
                baseline_files,
                files,
                builds[0]["wheel"],
                report_path.parent,
                uv,
            )
            assertions.append(incremental.pop("assertion"))
            report["incremental"] = incremental
        assertions.extend(_smoke(check, files, report_path.parent, uv))
        report["assertions"] = assertions
        try:
            current_manifest = parse_manifest(open_project(project.root))
        except ManifestError:
            raise _Blocked(
                BlockedReason.NOT_APPROVED,
                "the approved build check changed while it was running",
            ) from None
        if (
            check.id not in current_manifest.checks
            or current_manifest.digest(check.id) != check_digest
            or check_digest not in Store.of(project).approved()
        ):
            raise _Blocked(
                BlockedReason.NOT_APPROVED,
                "the approved build check changed while it was running",
            )
        if Snapshot(project).inputs(check.inputs).digest != input_digest:
            raise _Blocked(
                BlockedReason.SOURCE_CHANGED,
                "captured build inputs changed while the check was running",
            )
        report["status"] = (
            "PASS"
            if same and all(item["result"] == "PASS" for item in assertions)
            else "FAIL"
        )
        _report(report_path, report)
        return 0
    except _Blocked as exc:
        if "check" not in locals():
            return 1
        return _blocked(
            report_path,
            check,
            exc.reason,
            exc.detail,
            report if "report" in locals() else None,
        )
    except (
        OSError,
        ValueError,
        ManifestError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        if "check" not in locals():
            return 1
        return _blocked(
            report_path,
            check,
            BlockedReason.ARTIFACT_MALFORMED,
            f"build worker could not complete: {exc}",
            report if "report" in locals() else None,
        )


def worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vkit-build-worker")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)
    return _run(arguments.profile, arguments.report)


def check_release_artifact(root: Path, check_id: str) -> dict[str, Any]:
    """Invoke the registered BuildCheck through the ordinary approval and run-record path."""
    from ..runner import run_check

    project = open_project(root)
    manifest = parse_manifest(project)
    check = manifest.require(check_id)
    if not isinstance(check, BuildCheck):
        return {
            "operation": "check_release_artifact",
            "status": "UNSUPPORTED",
            "detail": f"{check_id!r} is not an explicitly declared build check",
        }
    record = run_check(project, manifest, check_id)
    outcome = record.get("outcome") or {}
    status = outcome.get("result", "BLOCKED")
    report_path = Store.of(project).run_dir(record["run_id"]) / check.artifact_name
    try:
        raw = report_path.read_bytes()
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("build report exceeds its output limit")
        artifact = json.loads(raw.decode("utf-8"))
        validate("build artifact", BUILD_ARTIFACT, artifact)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        SchemaValidationError,
    ) as exc:
        artifact = {"status": "BLOCKED", "detail": str(exc)}
        if status == "PASS":
            status = "BLOCKED"
    return {
        "operation": "check_release_artifact",
        "status": status,
        "profile": PROFILE,
        "scope": "two clean offline wheel builds, exact wheel-byte comparison, and the declared clean-install imports and entry points on this Python/uv/host profile; not OS isolation or cross-host reproducibility",
        "check_id": check_id,
        "run": record,
        "artifact": artifact,
    }


if __name__ == "__main__":
    raise SystemExit(worker_main())

"""Reduce one source file while preserving one recorded check failure."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .. import proc
from ..inputs import Snapshot, matches
from ..manifest import Manifest, parse_manifest
from ..paths import Project
from ..runner import _child_env
from ..store import Store
from ..verifiers import dispatch
from ..verifiers.spec import PytestCheck, ScenarioCheck

_SUPPORTED = (ScenarioCheck, PytestCheck)
_PERSES_ENV = "VKIT_PERSES_JAR"
_PERSES_SHA_ENV = "VKIT_PERSES_SHA256"
_PERSES_SOURCE = str(Path(__file__).resolve().parents[2])
_SUPPORTED_SUFFIXES = frozenset({".py", ".py3"})


class ReductionRefused(ValueError):
    """The recorded run or requested path cannot safely anchor a reduction."""


@dataclass(frozen=True)
class ReductionResult:
    status: Literal["REDUCED", "UNRESOLVED", "UNAVAILABLE"]
    run_id: str
    check_id: str
    source_path: str
    source_sha256: str
    failure_id: str
    failure_observation: str
    reduced_source: str | None = None
    reduced_sha256: str | None = None
    perses_version: str | None = None
    perses_sha256: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        reduced = self.status == "REDUCED"
        complete = (self.reduced_source is not None and self.reduced_sha256 is not None
                    and self.perses_version is not None and self.perses_sha256 is not None)
        if reduced != complete or (not reduced and (self.reduced_source is not None or self.reduced_sha256 is not None)):
            raise ValueError("only a verified reduction carries source and engine identity")

    def to_json(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "check_id": self.check_id,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "failure_id": self.failure_id,
            "failure_observation": self.failure_observation,
            "reduced_source": self.reduced_source,
            "reduced_sha256": self.reduced_sha256,
            "perses_version": self.perses_version,
            "perses_sha256": self.perses_sha256,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class _Target:
    scenario_id: str
    observation: str


@dataclass(frozen=True)
class _Request:
    project: Project
    manifest: Manifest
    check: ScenarioCheck | PytestCheck
    record: dict
    target: _Target
    source_path: str
    source_bytes: bytes
    source_sha256: str
    snapshot_files: tuple[tuple[str, str], ...]


def _relative_file(project: Project, raw: str) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ReductionRefused("path must be a repository-relative POSIX file path")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReductionRefused("path must stay inside the repository")
    if relative.as_posix() != raw:
        raise ReductionRefused("path must use its canonical repository-relative spelling")
    candidate = project.root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project.root.resolve())
    except (OSError, ValueError):
        raise ReductionRefused("path is missing or escapes the repository") from None
    current = project.root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ReductionRefused("symbolic links are not reduced")
    if not resolved.is_file():
        raise ReductionRefused("path must name a regular source file")
    return raw, resolved


def _failed_scenarios(outcome: object) -> list[dict]:
    if not isinstance(outcome, dict) or outcome.get("result") != "FAIL":
        raise ReductionRefused("run must contain a completed failed outcome")
    scenarios = outcome.get("scenarios")
    if not isinstance(scenarios, list):
        raise ReductionRefused("run has no replayable failed scenario")
    failed = [entry for entry in scenarios if isinstance(entry, dict) and entry.get("result") == "FAIL"
              and isinstance(entry.get("id"), str) and isinstance(entry.get("observation"), str)]
    if not failed:
        raise ReductionRefused("run has no replayable failed scenario")
    return failed


def _validate_dependencies(project: Project, check: ScenarioCheck | PytestCheck,
                           copied: set[str], target_path: str) -> None:
    root = project.root.resolve()
    cwd = check.cwd.resolve()
    try:
        cwd.relative_to(root)
    except ValueError:
        raise ReductionRefused("check working directory escapes the repository") from None
    if isinstance(check, ScenarioCheck):
        command = check.command
    else:
        command = (check.runner.executable, *check.runner.base_argv,
                   *(test.split("::", 1)[0] for test in check.required_tests))
    for token in command:
        if not isinstance(token, str) or "{{run_dir}}" in token or "{{python}}" in token:
            continue
        candidate = Path(token)
        if candidate.is_absolute():
            try:
                candidate.resolve(strict=False).relative_to(root)
            except ValueError:
                continue
            raise ReductionRefused("check command names the original checkout by absolute path")
        else:
            try:
                relative = (cwd / candidate).resolve(strict=False).relative_to(root).as_posix()
            except ValueError:
                continue
        source = root.joinpath(*PurePosixPath(relative).parts)
        if relative == target_path:
            raise ReductionRefused("the check driver or required test file cannot be reduced")
        if source.is_file() and relative not in copied:
            raise ReductionRefused("check driver or dependency is outside the recorded inputs")
    required = ()
    if isinstance(check, PytestCheck):
        required = tuple(test.split("::", 1)[0] for test in check.required_tests)
    for raw in required:
        path = Path(raw)
        candidate = path if path.is_absolute() else cwd / path
        try:
            relative = candidate.resolve(strict=True).relative_to(root).as_posix()
        except (OSError, ValueError):
            raise ReductionRefused("required test file is missing or escapes the repository") from None
        if relative not in copied:
            raise ReductionRefused("required test file is outside the recorded inputs")
        if relative == target_path:
            raise ReductionRefused("the check driver or required test file cannot be reduced")


def _prepare(project: Project, run_id: str, raw_path: str, store: Store) -> _Request:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ReductionRefused("run ID has an invalid format")
    record = store.record(run_id)
    if record is None or record.get("state") != "done":
        raise ReductionRefused("run is missing or was not completed")
    if record.get("worktree") and Path(record["worktree"]).resolve() != project.root.resolve():
        raise ReductionRefused("run belongs to another checkout")
    manifest = parse_manifest(project)
    check_id = record.get("check_id")
    if not isinstance(check_id, str) or check_id not in manifest.checks:
        raise ReductionRefused("run's check is no longer registered")
    check_digest = manifest.digest(check_id)
    if record.get("check_digest") != check_digest:
        raise ReductionRefused("check definition changed after the recorded run")
    if check_digest not in store.approved():
        raise ReductionRefused("check definition is no longer approved")
    check = manifest.require(check_id)
    if not isinstance(check, _SUPPORTED):
        raise ReductionRefused("this check kind cannot be replayed safely for reduction")
    relative, source = _relative_file(project, raw_path)
    recorded_inputs = record.get("inputs")
    if not isinstance(recorded_inputs, dict):
        raise ReductionRefused("run has no input snapshot")
    current = Snapshot(project).inputs(check.inputs)
    if current.to_json() != recorded_inputs:
        raise ReductionRefused("check inputs changed after the recorded run")
    input_files = tuple((entry["path"], entry["sha256"]) for entry in recorded_inputs.get("files", []))
    if not any(name == relative for name, _ in input_files):
        raise ReductionRefused("source file is outside the recorded check inputs")
    _validate_dependencies(project, check, {name for name, _ in input_files}, relative)
    if isinstance(check, PytestCheck) and PurePosixPath(relative).name == "conftest.py":
        raise ReductionRefused("pytest configuration and conftest files cannot be reduced")
    if not any(matches(relative, subject) for subject in check.subject.paths):
        raise ReductionRefused("source file is outside the approved check subject")
    if source.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ReductionRefused("Perses does not support this source file type")
    failed = _failed_scenarios(record.get("outcome"))
    selected = failed[0]
    artifact = store.run_dir(run_id) / check.artifact_name
    if not artifact.is_file():
        raise ReductionRefused("recorded run artifact is missing")
    reading = dispatch.interpret(check, artifact.read_bytes())
    replay = dispatch.outcome_from_reading(reading)
    if not any(s.scenario_id == selected["id"] and s.observation == selected["observation"] and not s.passed
               for s in getattr(replay, "scenarios", ())):
        raise ReductionRefused("recorded failure is budget-only or cannot be replayed from its artifact")
    source_bytes = source.read_bytes()
    try:
        source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ReductionRefused("source file must contain UTF-8 text") from None
    expected_source = dict(input_files)[relative]
    if hashlib.sha256(source_bytes.replace(b"\r\n", b"\n")).hexdigest() != expected_source:
        raise ReductionRefused("source file changed while preparing the reduction")
    return _Request(project, manifest, check, record, _Target(selected["id"], selected["observation"]),
                    relative, source_bytes, hashlib.sha256(source_bytes).hexdigest(), input_files)


def _copy_inputs(request: _Request, destination: Path) -> None:
    copied: set[str] = set()
    root = request.project.root
    for relative, expected in request.snapshot_files:
        _, source = _relative_file(request.project, relative)
        content = source.read_bytes()
        if hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest() != expected:
            raise ReductionRefused("check inputs changed while preparing the temporary workspace")
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        copied.add(relative)
    manifest_relative = request.project.manifest_path.relative_to(root).as_posix()
    if manifest_relative not in copied:
        target = destination / manifest_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(request.project.manifest_path, target)
    cwd_relative = request.check.cwd.relative_to(root).as_posix()
    (destination / cwd_relative).mkdir(parents=True, exist_ok=True)


def _runtime() -> tuple[Path, str, str] | None:
    if os.name == "nt":
        return None
    raw_jar = os.environ.get(_PERSES_ENV)
    expected = os.environ.get(_PERSES_SHA_ENV, "").lower()
    if not raw_jar or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return None
    jar = Path(raw_jar).expanduser().resolve()
    if not jar.is_file():
        return None
    actual = hashlib.sha256(jar.read_bytes()).hexdigest()
    if actual != expected:
        return None
    java_home = os.environ.get("JAVA_HOME")
    java = (Path(java_home) / "bin" / "java") if java_home else None
    java_path = str(java) if java is not None and java.is_file() else shutil.which("java")
    if java_path is None:
        return None
    return jar, actual, java_path


def _engine_version(java: str, jar: Path, scratch: Path) -> str | None:
    stdout = scratch / "version.out"
    stderr = scratch / "version.err"
    result = proc.run([java, "-jar", str(jar), "--version"], cwd=scratch, stdout_path=stdout, stderr_path=stderr,
                      timeout_seconds=10)
    if result.code != 0:
        return None
    version = stdout.read_text(encoding="utf-8", errors="replace").strip()
    if not version:
        version = stderr.read_text(encoding="utf-8", errors="replace").strip()
    first_line = version.splitlines()[0] if version else ""
    return first_line if re.search(r"(?<!\d)2\.7(?!\d)", first_line) else None


def _assert_fresh(request: _Request) -> None:
    if Snapshot(request.project).inputs(request.check.inputs).to_json() != request.record["inputs"]:
        raise ReductionRefused("check inputs changed during reduction")
    if parse_manifest(request.project).digest(request.check.id) != request.record["check_digest"]:
        raise ReductionRefused("check definition changed during reduction")


def _candidate_matches(config_path: Path, candidate_root: Path) -> bool:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        source_path = config["source_path"]
        snapshot_root = Path(config["snapshot_root"])
        source = candidate_root.resolve() / config["candidate_name"]
        if source.is_symlink() or not source.is_file():
            return False
        with tempfile.TemporaryDirectory(prefix="vkit-replay-workspace-") as raw_workspace:
            workspace = Path(raw_workspace)
            for relative in config["snapshot_files"]:
                original = snapshot_root.joinpath(*PurePosixPath(relative).parts)
                target = workspace.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original.read_bytes())
            workspace.joinpath(*PurePosixPath(source_path).parts).write_bytes(source.read_bytes())
            project = Project(workspace, workspace)
            manifest = parse_manifest(project)
            check = manifest.require(config["check_id"])
            if manifest.digest(check.id) != config["check_digest"] or not isinstance(check, _SUPPORTED):
                return False
            check.cwd.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="vkit-replay-") as raw_run_dir:
                run_dir = Path(raw_run_dir)
                artifact = run_dir / check.artifact_name
                result = proc.run(
                    dispatch.argv_for(check, run_dir, None),
                    cwd=check.cwd,
                    env=_child_env(),
                    stdout_path=run_dir / "stdout.log",
                    stderr_path=run_dir / "stderr.log",
                    timeout_seconds=float(config["check_timeout_seconds"]),
                )
                if result.launch_error or result.cancelled or result.timed_out or result.code not in (0, 1):
                    return False
                if not artifact.is_file():
                    return False
                reading = dispatch.interpret(check, artifact.read_bytes())
                outcome = dispatch.outcome_from_reading(reading)
                return any(
                    not scenario.passed and scenario.scenario_id == config["failure_id"]
                    and scenario.observation == config["failure_observation"]
                    for scenario in getattr(outcome, "scenarios", ())
                )
    except Exception:
        return False


def _test_script(python: str, config: Path) -> str:
    return "#!/bin/sh\nexec " + shlex.quote(python) + " -P -m vkit.operations.reduction --perses-candidate " \
        + shlex.quote(str(config)) + " \"$PWD\"\n"


def reduce_failure(project: Project, run_id: str, path: str, *, timeout_seconds: float = 60) -> ReductionResult:
    """Reduce a source file from one fresh failed run without writing into the checkout."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    request = _prepare(project, run_id, path, Store.of(project))
    runtime = _runtime()
    base = dict(
        run_id=run_id, check_id=request.check.id, source_path=request.source_path,
        source_sha256=request.source_sha256, failure_id=request.target.scenario_id,
        failure_observation=request.target.observation,
    )
    if runtime is None:
        return ReductionResult("UNAVAILABLE", **base, detail="Perses 2.7 and its pinned JAR are unavailable")
    jar, jar_sha, java = runtime
    with tempfile.TemporaryDirectory(prefix="vkit-reduction-") as raw_temp:
        temp = Path(raw_temp)
        workspace = temp / "workspace"
        workspace.mkdir()
        _copy_inputs(request, workspace)
        engine_input = temp / "input"
        engine_input.mkdir()
        source_name = PurePosixPath(request.source_path).name
        target = engine_input / source_name
        target.write_bytes(request.source_bytes)
        config = temp / "predicate.json"
        manifest_relative = request.project.manifest_path.relative_to(request.project.root).as_posix()
        snapshot_files = [name for name, _ in request.snapshot_files]
        if manifest_relative not in snapshot_files:
            snapshot_files.append(manifest_relative)
        config.write_text(json.dumps({
            "check_id": request.check.id,
            "check_digest": request.manifest.digest(request.check.id),
            "check_timeout_seconds": min(request.check.timeout_seconds, timeout_seconds),
            "failure_id": request.target.scenario_id,
            "failure_observation": request.target.observation,
            "snapshot_root": str(workspace),
            "snapshot_files": snapshot_files,
            "source_path": request.source_path,
            "candidate_name": f"input/{source_name}",
        }), encoding="utf-8")
        script = temp / "test.sh"
        script.write_text(_test_script(sys.executable, config), encoding="utf-8")
        script.chmod(0o700)
        output = temp / "out"
        scratch = temp / "logs"
        scratch.mkdir()
        version = _engine_version(java, jar, scratch)
        _assert_fresh(request)
        if version is None:
            return ReductionResult("UNAVAILABLE", **base, perses_sha256=jar_sha,
                                   detail="configured JAR did not identify itself as Perses 2.7")
        argv = [java, "-jar", str(jar), "--test-script", str(script), "--input-file", source_name,
                "--output-dir", str(output), "--parser-facade-class-name",
                "org.perses.grammar.python3.Python3ParserFacade", "--script-execution-timeout-in-seconds",
                str(max(1, math.ceil(min(timeout_seconds, request.check.timeout_seconds)))),
                "--fully-deterministic-mode", "true", "--threads", "1"]
        result = proc.run(argv, cwd=engine_input, env={**os.environ, "PYTHONPATH": _PERSES_SOURCE},
                          stdout_path=scratch / "perses.out", stderr_path=scratch / "perses.err",
                          timeout_seconds=timeout_seconds)
        _assert_fresh(request)
        if result.code != 0:
            return ReductionResult("UNRESOLVED", **base, perses_version=version, perses_sha256=jar_sha,
                                   detail="Perses did not complete a reduction")
        reduced_path = output / "input" / source_name
        if not reduced_path.is_file() or reduced_path.is_symlink():
            return ReductionResult("UNRESOLVED", **base, perses_version=version, perses_sha256=jar_sha,
                                   detail="Perses did not produce the expected reduced source file")
        reduced = reduced_path.read_bytes()
        if reduced == request.source_bytes:
            return ReductionResult("UNRESOLVED", **base, perses_version=version, perses_sha256=jar_sha,
                                   detail="Perses completed without changing the source")
        if not _candidate_matches(config, output):
            _assert_fresh(request)
            return ReductionResult("UNRESOLVED", **base, perses_version=version, perses_sha256=jar_sha,
                                   detail="independent replay did not preserve the selected failure")
        _assert_fresh(request)
        try:
            reduced_text = reduced.decode("utf-8")
        except UnicodeDecodeError:
            return ReductionResult("UNRESOLVED", **base, perses_version=version, perses_sha256=jar_sha,
                                   detail="Perses output is not UTF-8 source text")
        return ReductionResult(
            "REDUCED", **base, reduced_source=reduced_text,
            reduced_sha256=hashlib.sha256(reduced).hexdigest(), perses_version=version,
            perses_sha256=jar_sha,
        )


def _candidate_main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "--perses-candidate":
        return 2
    return 0 if _candidate_matches(Path(argv[1]), Path(argv[2])) else 1


if __name__ == "__main__":
    raise SystemExit(_candidate_main(sys.argv[1:]))

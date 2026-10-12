"""One adapter per check kind: the argv that runs it, and the reading of the report it wrote."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..budgets import Measurement
from ..outcome import Blocked, BlockedReason, Failed, Outcome, Passed, ScenarioResult
from ..schemas import BUILD_ARTIFACT, CHECK_ARTIFACT, SchemaValidationError, parse_artifact, validate
from . import lean_adapter, node_adapter, property_adapter, pytest_adapter, static_adapter, tlc_adapter
from .obligation import CaseObligation, obligation_to_json
from .spec import BuildCheck, CheckKind

PROPERTY_PLUGIN = "vkit.verifiers.property_adapter"


@dataclass(frozen=True)
class Adapter:
    kind: CheckKind
    argv_for: Callable[[Any, Path, "str | None"], tuple[str, ...]]
    read: Callable[[Any, bytes], Any]


@dataclass(frozen=True)
class ScenarioReading:
    scenarios: tuple[ScenarioResult, ...]
    problem: Blocked | None = None
    measurements: tuple[Measurement, ...] = ()

    @property
    def failing(self) -> tuple[ScenarioResult, ...]:
        return tuple(s for s in self.scenarios if not s.passed)

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        if self.problem is not None:
            return [], []
        satisfied = [{"kind": "case_satisfied", "obligation": obligation_to_json(CaseObligation(s.scenario_id)),
                      "observation": s.observation} for s in self.scenarios if s.passed]
        counterexamples = [{"obligation": obligation_to_json(CaseObligation(s.scenario_id)), "trace": s.observation}
                           for s in self.scenarios if not s.passed]
        return satisfied, counterexamples



@dataclass(frozen=True)
class BuildReading:
    assertions: tuple[dict[str, Any], ...]
    problem: Blocked | None = None

    def scenarios(self) -> tuple[ScenarioResult, ...]:
        if self.problem is not None:
            return ()
        return tuple(
            ScenarioResult(
                item["id"],
                item["result"] == "PASS",
                f"expected={item['expected']!r}; observed={item['observed']!r}",
            )
            for item in self.assertions
        )

    @property
    def counterexamples(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in self.assertions if item["result"] == "FAIL")

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        return [], list(self.counterexamples)


def read_build(check: BuildCheck, raw: bytes) -> BuildReading:
    try:
        document = parse_artifact(raw)
        validate("build artifact", BUILD_ARTIFACT, document)
    except SchemaValidationError as exc:
        return BuildReading((), Blocked(BlockedReason.ARTIFACT_MALFORMED, exc.reason))
    if document["status"] == "BLOCKED":
        try:
            reason = BlockedReason(document["problem"]["reason"])
        except ValueError:
            return BuildReading(
                (),
                Blocked(
                    BlockedReason.ARTIFACT_MALFORMED,
                    "build report contains an unknown blocked reason",
                ),
            )
        return BuildReading((), Blocked(reason, document["problem"]["detail"]))
    expected_toolchain = {
        "uv_version": check.uv_version,
        "uv_sha256": check.uv_sha256,
        "python_version": check.python_version,
        "python_sha256": check.python_sha256,
    }
    if document["toolchain"] != expected_toolchain:
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report toolchain does not match its approved profile",
            ),
        )
    assertions = document["assertions"]
    names = [item["id"] for item in assertions]
    if len(names) != len(set(names)) or not assertions:
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report has duplicate or missing assertion ids",
            ),
        )
    expected_names = {
        "builds:wheel-bytes-identical",
        "builds:wheel-filename-identical",
        *(f"install-{index}:imports" for index in (1, 2)),
        *(
            f"install-{index}:entrypoint:{entry.name}"
            for index in (1, 2)
            for entry in check.entry_points
        ),
    }
    if set(names) != expected_names:
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report assertions do not match the approved install smoke profile",
            ),
        )
    if len(document["builds"]) != 2 or any(
        item["exit_code"] != 0 or item["wheel"] is None for item in document["builds"]
    ):
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "completed build report does not contain two successful wheel builds",
            ),
        )
    if [item["index"] for item in document["builds"]] != [1, 2]:
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report does not identify both independent builds",
            ),
        )
    if document["same_wheel_bytes"] != (
        document["builds"][0]["wheel"]["sha256"]
        == document["builds"][1]["wheel"]["sha256"]
        and document["builds"][0]["wheel"]["bytes"]
        == document["builds"][1]["wheel"]["bytes"]
    ):
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report byte-equality assertion disagrees with wheel digests",
            ),
        )
    by_id = {item["id"]: item for item in assertions}
    identical = by_id["builds:wheel-bytes-identical"]
    if (
        identical["expected"] is not True
        or identical["observed"] is not document["same_wheel_bytes"]
        or (identical["result"] == "PASS") != document["same_wheel_bytes"]
    ):
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report byte-equality assertion is inconsistent",
            ),
        )
    filenames_match = (
        document["builds"][0]["wheel"]["filename"]
        == document["builds"][1]["wheel"]["filename"]
    )
    filename_assertion = by_id["builds:wheel-filename-identical"]
    if (
        filename_assertion["expected"] != document["builds"][0]["wheel"]["filename"]
        or filename_assertion["observed"] != document["builds"][1]["wheel"]["filename"]
        or (filename_assertion["result"] == "PASS") != filenames_match
    ):
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report filename assertion is inconsistent",
            ),
        )
    passed = all(item["result"] == "PASS" for item in assertions)
    if (document["status"] == "PASS") != (passed and document["same_wheel_bytes"]):
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "build report status disagrees with its assertions",
            ),
        )
    if document["status"] == "FAIL" and passed and document["same_wheel_bytes"]:
        return BuildReading(
            (),
            Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "failed build report contains no failed assertion",
            ),
        )
    return BuildReading(tuple(assertions))



def read_scenarios(raw: bytes, required: tuple[str, ...]) -> ScenarioReading:
    try:
        document = parse_artifact(raw)
        validate("check artifact", CHECK_ARTIFACT, document)
    except SchemaValidationError as exc:
        return ScenarioReading((), Blocked(BlockedReason.ARTIFACT_MALFORMED, exc.reason))
    if not document["scenarios"]:
        return ScenarioReading((), Blocked(BlockedReason.ARTIFACT_EMPTY, "the artifact listed no scenarios"))
    scenarios = tuple(ScenarioResult(item["id"], item["result"] == "PASS", item["observation"])
                      for item in document["scenarios"])
    known = {s.scenario_id for s in scenarios}
    missing = [s for s in required if s not in known]
    if missing:
        return ScenarioReading(scenarios, Blocked(
            BlockedReason.SCENARIO_UNKNOWN, f"the artifact never reported required scenario(s): {', '.join(missing)}"))
    measurements = tuple(Measurement(m["name"], float(m["value"]), m["unit"], m["better"])
                         for m in document.get("measurements", ()))
    return ScenarioReading(scenarios, measurements=measurements)


def _variant(check: Any) -> Any:
    return getattr(check, "variant", None) or check


def _substituted(parts: tuple, run_dir: Path, interpreter: str) -> tuple[str, ...]:
    return tuple(str(p).replace("{{run_dir}}", str(run_dir)).replace("{{python}}", interpreter) for p in parts)


def _runner(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    variant = _variant(check)
    interpreter = python or sys.executable
    return (*_substituted((variant.runner.executable,), run_dir, interpreter),
            *_substituted(variant.runner.base_argv, run_dir, interpreter))


def _scenario_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    return _substituted(check.argv, run_dir, python or sys.executable)


def _pytest_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    return (*_runner(check, run_dir, python), "-p", pytest_adapter.PLUGIN_NAME,
            pytest_adapter.REPORT_FLAG, str(run_dir / check.artifact_name), *_variant(check).required_tests)


def _property_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    variant = _variant(check)
    return (*_runner(check, run_dir, python), "-p", pytest_adapter.PLUGIN_NAME, "-p", PROPERTY_PLUGIN,
            pytest_adapter.REPORT_FLAG, str(run_dir / check.artifact_name),
            property_adapter.SETTINGS_FLAG, property_adapter.settings_token(variant), *variant.required_tests)


def _node_argv(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    variant = _variant(check)
    files, pattern = node_adapter.argv_selectors(variant.required_tests)
    executable, *base = _runner(check, run_dir, python)
    return (executable, *base, node_adapter.isolation_flag(executable),
            "--test-reporter", node_adapter.reporter_argv_token(),
            "--test-reporter-destination", str(run_dir / check.artifact_name),
            *(("--test-name-pattern", pattern) if pattern else ()), *files)


def _build_profile(check: BuildCheck) -> dict[str, Any]:
    return {
        "id": check.id,
        "cwd": str(check.cwd),
        "timeout_seconds": check.timeout_seconds,
        "artifact": check.artifact_name,
        "inputs": list(check.inputs),
        "build": {
            "uv_executable": check.uv_executable,
            "uv_version": check.uv_version,
            "uv_sha256": check.uv_sha256,
            "python_version": check.python_version,
            "python_sha256": check.python_sha256,
            "build_constraints": check.build_constraints,
            "wheelhouse": check.wheelhouse,
            "runtime_requirements": check.runtime_requirements,
            "imports": list(check.imports),
            "entry_points": [
                {
                    "name": item.name,
                    "args": list(item.args),
                    "expected_exit_code": item.expected_exit_code,
                    "expected_stdout": item.expected_stdout,
                    "expected_stderr": item.expected_stderr,
                }
                for item in check.entry_points
            ],
        },
    }


def _build_argv(
    check: BuildCheck, run_dir: Path, python: str | None, input_digest: str | None
) -> tuple[str, ...]:
    profile_path = run_dir / "build-profile.json"
    profile = {**_build_profile(check), "input_digest": input_digest}
    profile_path.write_text(
        json.dumps(profile, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    return (
        python or sys.executable,
        "-m",
        "vkit.operations.wheel_build",
        "--profile",
        str(profile_path),
        "--report",
        str(run_dir / check.artifact_name),
    )


ADAPTERS: dict[CheckKind, Adapter] = {
    CheckKind.SCENARIO: Adapter(CheckKind.SCENARIO, _scenario_argv,
                                lambda check, raw: read_scenarios(raw, tuple(o.test_id for o in check.required_scenarios))),
    CheckKind.PYTEST: Adapter(CheckKind.PYTEST, _pytest_argv,
                              lambda check, raw: pytest_adapter.interpret(raw, _variant(check))),
    CheckKind.NODE_TEST: Adapter(CheckKind.NODE_TEST, _node_argv,
                                 lambda check, raw: node_adapter.interpret(raw, _variant(check))),
    CheckKind.PROPERTY: Adapter(CheckKind.PROPERTY, _property_argv,
                                lambda check, raw: property_adapter.interpret(raw, _variant(check))),
    CheckKind.LEAN: Adapter(CheckKind.LEAN, lean_adapter.argv_for,
                            lambda check, raw: lean_adapter.interpret(raw, _variant(check))),
    CheckKind.TLC: Adapter(CheckKind.TLC, tlc_adapter.argv_for,
                           lambda check, raw: tlc_adapter.interpret(raw, _variant(check))),
    CheckKind.STATIC: Adapter(CheckKind.STATIC, _scenario_argv,
                              lambda check, raw: static_adapter.read(raw, check.cwd.as_posix().rstrip("/") + "/")),
    CheckKind.BUILD: Adapter(CheckKind.BUILD, lambda check, run_dir, python: _build_argv(check, run_dir, python, None), read_build),
}


def argv_for(check: Any, run_dir: Path, python: str | None, *, input_digest: str | None = None) -> tuple[str, ...]:
    if isinstance(check, BuildCheck):
        return _build_argv(check, run_dir, python, input_digest)
    return ADAPTERS[check.kind].argv_for(check, run_dir, python)


def interpret(check: Any, raw: bytes) -> Any:
    return ADAPTERS[check.kind].read(check, raw)


def outcome_from_reading(reading: Any, baseline: dict[str, Any] | None = None) -> Outcome:
    if isinstance(reading, Blocked):
        return reading
    if isinstance(reading, static_adapter.StaticReading):
        return static_adapter.judge(reading, (baseline or {}).get("findings"))
    if isinstance(reading, ScenarioReading):
        if reading.problem is not None:
            return reading.problem
        return Failed(reading.scenarios) if reading.failing else Passed(reading.scenarios)
    if isinstance(reading, BuildReading):
        if reading.problem is not None:
            return reading.problem
        scenarios = reading.scenarios()
        return Failed(scenarios) if reading.counterexamples else Passed(scenarios)
    scenarios = reading.scenarios()
    return Failed(scenarios) if reading.counterexamples else Passed(scenarios)

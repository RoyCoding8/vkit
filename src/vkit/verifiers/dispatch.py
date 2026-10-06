"""One adapter per check kind: the argv that runs it, and the reading of the report it wrote."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..budgets import Measurement
from ..outcome import Blocked, BlockedReason, Failed, Outcome, Passed, ScenarioResult
from ..schemas import CHECK_ARTIFACT, SchemaValidationError, parse_artifact, validate
from . import lean_adapter, node_adapter, property_adapter, pytest_adapter, tlc_adapter
from .obligation import CaseObligation, obligation_to_json
from .spec import CheckKind

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
}


def argv_for(check: Any, run_dir: Path, python: str | None) -> tuple[str, ...]:
    return ADAPTERS[check.kind].argv_for(check, run_dir, python)


def interpret(check: Any, raw: bytes) -> Any:
    return ADAPTERS[check.kind].read(check, raw)


def outcome_from_reading(reading: Any) -> Outcome:
    if isinstance(reading, Blocked):
        return reading
    if isinstance(reading, ScenarioReading):
        if reading.problem is not None:
            return reading.problem
        return Failed(reading.scenarios) if reading.failing else Passed(reading.scenarios)
    scenarios = reading.scenarios()
    return Failed(scenarios) if reading.counterexamples else Passed(scenarios)

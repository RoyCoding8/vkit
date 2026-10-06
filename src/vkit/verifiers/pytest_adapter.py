"""A pytest plugin that writes a versioned report of every test the runner executed, and the reader
that decides what the report establishes. The report is the only evidence: stdout and stderr are
never read.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..outcome import Blocked, BlockedReason
from .obligation import CaseObligation, case_results, scenario_results
from .spec import PytestCheck

REPORT_VERSION = 1

PLUGIN_NAME = "vkit.verifiers.pytest_adapter"

REPORT_FLAG = "--vkit-json-report"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
ERRORED = "error"
OUTCOMES = frozenset({PASSED, FAILED, SKIPPED, ERRORED})

MESSAGE_LIMIT = 2000


@dataclass(frozen=True)
class TestOutcome:
    """One test id with the runner's own outcome word; `generator` holds Hypothesis settings, or
    None for other tests.
    """

    nodeid: str
    outcome: str
    duration: float
    message: str
    generator: dict | None = None


@dataclass(frozen=True)
class RunnerReport:
    """A report that passed every shape check in `_read`."""

    tests: tuple[TestOutcome, ...]

    def by_id(self) -> dict[str, TestOutcome]:
        """Results keyed by test id; `_read` has already refused contradictory duplicates."""
        return {test.nodeid: test for test in self.tests}


@dataclass(frozen=True)
class AdapterResult:
    """What one pytest run established. Passing and failing cases are kept apart so a FAIL still
    names what passed.
    """

    observations: tuple[tuple[CaseObligation, str], ...]
    counterexamples: tuple[tuple[CaseObligation, str], ...]
    generator_settings: dict[str, dict] | None = None

    @property
    def is_pass(self) -> bool:
        return not self.counterexamples

    def scenarios(self) -> tuple:
        """The reading as outcome `ScenarioResult`s."""
        return scenario_results(self.observations, self.counterexamples)

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """Satisfied cases and counterexamples as run-record dicts."""
        return case_results(self.observations, self.counterexamples)


def pytest_addoption(parser: Any) -> None:
    """Register the report-path flag."""
    group = parser.getgroup("vkit", "vkit evidence contract")
    group.addoption(
        REPORT_FLAG, action="store", default=None, metavar="PATH",
        help="write a vkit evidence report to PATH",
    )


def pytest_configure(config: Any) -> None:
    """Install the collector on the config, so a second run in one interpreter cannot inherit the
    first's tests.
    """
    config._vkit_reported = []


def pytest_runtest_makereport(item: Any, call: Any) -> None:
    """Record one test's `call` phase, or any phase that raised.

    Uses this hook because `pytest_runtest_logreport` has no config on its report.
    """
    if not (call.when == "call" or call.excinfo is not None):
        return
    config = item.config
    reported = getattr(config, "_vkit_reported", None)
    if reported is None:
        return
    excinfo = call.excinfo
    skipped = _skip(call)
    reported.append({
        "nodeid": item.nodeid,
        "outcome": (
            SKIPPED if skipped is not None
            else PASSED if excinfo is None
            else FAILED if call.when == "call" and _is_assertion(excinfo)
            else ERRORED
        ),
        "duration": float(getattr(call, "duration", 0.0)),
        "message": skipped if skipped is not None else _message(excinfo),
        "generator": _generator_settings(item) if call.when == "call" else None,
    })


def _generator_settings(item: Any) -> dict | None:
    """The Hypothesis settings this test ran under, or None. Read with `getattr` so a runner that
    drops the attribute yields null, not an error.
    """
    settings = getattr(
        getattr(item, "function", None), "_hypothesis_internal_use_settings", None,
    )
    if settings is None:
        return None
    deadline = settings.deadline
    return {
        "max_examples": int(settings.max_examples),
        "stateful_step_count": int(settings.stateful_step_count),
        "deadline": None if deadline is None else deadline.total_seconds(),
        "suppress_health_check": [
            getattr(check, "__name__", str(check))
            for check in settings.suppress_health_check
        ],
        "database": type(settings.database).__name__ if settings.database else None,
    }


def _is_assertion(excinfo: Any) -> bool:
    """Whether the exception is a failed assertion rather than a crash.

    `pytest.fail` raises `Failed`, which is not an `AssertionError`; a bare `assert` raises
    `AssertionError`.
    """
    import pytest as _pytest

    return isinstance(excinfo.value, (AssertionError, _pytest.fail.Exception))


def _skip(call: Any) -> str | None:
    """The skip reason for this phase, or None."""
    import pytest as _pytest

    excinfo = getattr(call, "excinfo", None)
    if excinfo is None:
        return None
    if isinstance(excinfo.value, _pytest.skip.Exception):
        return str(excinfo.value) or "skipped"
    return None


def _message(excinfo: Any) -> str:
    """The failure's own message, bounded; the traceback is already in the run's stderr log."""
    if excinfo is None:
        return "the runner reported a failure with no message"
    return str(excinfo.value)[:MESSAGE_LIMIT] or type(excinfo.value).__name__


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Write the report, even when the session failed. A failing writer is left to raise so its
    traceback reaches the run's stderr log.
    """
    path = session.config.getoption(REPORT_FLAG, None)
    if not path:
        return
    reported = getattr(session.config, "_vkit_reported", [])
    document = {
        "version": REPORT_VERSION,
        "complete": exitstatus not in _INTERRUPTED_EXITSTATUSES,
        "exit_code": int(exitstatus),
        "tests": reported,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(target.suffix + ".partial")
    staged.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    staged.replace(target)


_INTERRUPTED_EXITSTATUSES = frozenset({2, 3, 4})


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable token that callers and tests match on."""
    return Blocked(reason, f"{token}: {detail}")


def _read(raw: bytes) -> RunnerReport | Blocked:
    """Decode and shape-check a report, refusing each unusable form with its own token."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is not UTF-8 JSON: {exc}",
        )
    if not isinstance(document, dict):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is a {type(document).__name__}, not an object",
        )

    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return _refuse(
            "report_version", BlockedReason.ARTIFACT_MALFORMED,
            f"the report declares version {version!r}, which is not a version number",
        )
    if version != REPORT_VERSION:
        return _refuse(
            "report_version", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is version {version}; this vkit reads version "
            f"{REPORT_VERSION} only",
        )

    if document.get("complete") is not True:
        return _refuse(
            "report_truncated", BlockedReason.ARTIFACT_MALFORMED,
            "the runner reported that it did not finish the session, so the "
            "tests it did list are not the tests it would have run",
        )

    raw_tests = document.get("tests")
    if not isinstance(raw_tests, list):
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report's tests member is a {type(raw_tests).__name__}, not a list",
        )
    if not raw_tests:
        return _refuse(
            "report_empty", BlockedReason.ARTIFACT_EMPTY,
            "the runner collected no tests, which is not evidence about anything",
        )

    parsed: list[TestOutcome] = []
    for index, entry in enumerate(raw_tests):
        if not isinstance(entry, dict):
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"test entry {index} is a {type(entry).__name__}, not an object",
            )
        nodeid = entry.get("nodeid")
        if not isinstance(nodeid, str) or not nodeid:
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"test entry {index} names no test",
            )
        outcome = entry.get("outcome")
        if outcome not in OUTCOMES:
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"test {nodeid!r} reports outcome {outcome!r}, which is not one of "
                f"{', '.join(sorted(OUTCOMES))}",
            )
        duration = entry.get("duration", 0.0)
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"test {nodeid!r} reports a non-numeric duration {duration!r}",
            )
        message = entry.get("message", "")
        if not isinstance(message, str):
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"test {nodeid!r} reports a non-textual message",
            )
        generator = entry.get("generator")
        if generator is not None and not _is_generator(generator):
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"test {nodeid!r} reports generator settings that are not the "
                "documented shape, so what it sampled cannot be read",
            )
        parsed.append(TestOutcome(nodeid, outcome, float(duration), message, generator))

    contradiction = _contradiction(parsed)
    if contradiction is not None:
        return _refuse(
            "report_contradiction", BlockedReason.ARTIFACT_MALFORMED,
            f"test {contradiction[0]!r} is reported twice with different results "
            f"({contradiction[1]} and {contradiction[2]}), so the report does not "
            "describe one execution",
        )
    return RunnerReport(tuple(parsed))


_GENERATOR_KEYS = frozenset({
    "max_examples", "stateful_step_count", "deadline",
    "suppress_health_check", "database",
})


def _is_generator(document: Any) -> bool:
    """Whether a `generator` block has the shape this code reads."""
    if not isinstance(document, dict) or not _GENERATOR_KEYS <= set(document):
        return False
    if not isinstance(document["max_examples"], int):
        return False
    if not isinstance(document["stateful_step_count"], int):
        return False
    deadline = document["deadline"]
    if deadline is not None and not isinstance(deadline, (int, float)):
        return False
    return isinstance(document["suppress_health_check"], list)


def _contradiction(tests: list[TestOutcome]) -> tuple[str, str, str] | None:
    """One id reported twice with different outcomes, or None. Identical repeats are one result."""
    seen: dict[str, str] = {}
    for test in tests:
        first = seen.setdefault(test.nodeid, test.outcome)
        if first != test.outcome:
            return (test.nodeid, first, test.outcome)
    return None


def interpret(raw: bytes, check: PytestCheck) -> AdapterResult | Blocked:
    """What the run established, or why it could not. Required test ids match exactly, since node
    ids carry the parameterisation.
    """
    report = _read(raw)
    if isinstance(report, Blocked):
        return report
    reported = report.by_id()

    missing = [t for t in check.required_tests if t not in reported]
    if missing:
        return _refuse(
            "required_test_absent", BlockedReason.SCENARIO_UNKNOWN,
            f"the report never ran required test(s): {', '.join(missing)}",
        )

    skipped = [reported[t].nodeid for t in check.required_tests if reported[t].outcome == SKIPPED]
    if skipped:
        return _refuse(
            "required_test_skipped", BlockedReason.SCENARIO_UNKNOWN,
            f"required test(s) were skipped rather than run: {', '.join(skipped)}. "
            "A check that declares a test must watch it run",
        )

    errored = [reported[t].nodeid for t in check.required_tests if reported[t].outcome == ERRORED]
    if errored:
        return _refuse(
            "infrastructure_error", BlockedReason.INTERNAL_ERROR,
            f"required test(s) errored before asserting anything: {', '.join(errored)}. "
            "That is the runner failing to deliver an answer, not the code "
            "disagreeing with its expectation",
        )

    counterexamples: list[tuple[CaseObligation, str]] = []
    observations: list[tuple[CaseObligation, str]] = []
    generator_settings: dict[str, dict] = {}
    for test_id in check.required_tests:
        result = reported[test_id]
        obligation = CaseObligation(test_id)
        if result.generator is not None:
            generator_settings[test_id] = result.generator
        if result.outcome == FAILED:
            counterexamples.append((obligation, result.message or "the assertion failed"))
            continue
        observations.append((
            obligation,
            f"the runner reported {result.outcome} in {result.duration:g}s",
        ))
    return AdapterResult(
        tuple(observations), tuple(counterexamples), generator_settings or None,
    )
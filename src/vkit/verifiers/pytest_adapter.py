"""Interpret one pytest run from a structured report, and refuse everything else.

This module is two halves of one contract. The first is a pytest plugin: it is
loaded into the pinned runner by the argv `dispatch` builds, and it writes a
versioned document naming every test the runner actually executed. The second
reads that document back and decides what the run established.

**Why vkit produces the report rather than importing a plugin that does.** A
third-party report plugin is the obvious source of a structured pytest report,
and this build has no way to depend on one: `pyproject.toml` is not this
package's to change, and a report format named by a version this package did not
choose is a format whose meaning is a fact about somebody else's release. The
contract says a verdict must come from actual checker output the adapter alone
interprets, and the strongest reading of that is that vkit owns both ends of the
document it reads. It also removes a failure mode with no equivalent in the rest
of the system: a report plugin that changes its schema between two runs of the
same check, which would make a stored receipt describe a document that is no
longer the one this code parses.

**Why the report is the only evidence.** `plans/CONTRACT.md:58` requires the core
to interpret structured output rather than scrape success prose, and prose is
what a runner writes to a terminal. Nothing here reads stdout or stderr. A
runner that exits zero having printed the word "passed" and executed nothing has
written no report, and `report_absent` is the answer.

**What each test's outcome means.** A failed assertion is FAIL and an
infrastructure error is BLOCKED, and they are different answers to different
questions: one says the code under test disagreed with its expectation, the other
says the runner could not deliver an answer at all. The runner's own vocabulary
distinguishes them, and collapsing the two is what turns a broken environment
into a red test that a reader is invited to go and fix.

**The refusals, each with one reason.** Seven ways a report can fail to answer
the question are refused separately rather than as one "malformed", because each
has a different repair and a reader who is told only "malformed" has to
rediscover which. `BlockedReason` is frozen in `vkit.outcome` and enumerated
again by `schemas/run-report.v1.json`, so the distinction is carried in the
detail's leading token. Each token below is the whole contract of that refusal;
a caller may match on it and the string is covered by a test.

  report_absent          the runner wrote no report at all
  report_truncated       the document's own completeness marker says it stopped early
  report_version         the document is not a version this code reads
  report_malformed       the document is not the shape this code reads
  report_empty           the runner collected no tests at all
  report_contradiction   one test id carries two different results
  required_test_absent   a required test id never appears in the report
  required_test_skipped  a required test id appears, but was not run
  infrastructure_error   a test errored before it could assert anything
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..outcome import Blocked, BlockedReason
from .obligation import CaseObligation, obligation_to_json
from .spec import PytestCheck

#: The one report version this code reads. A check declares the version it
#: expects, and a document carrying any other value is refused rather than
#: parsed on a best effort: the fields below are read by name, so an unknown
#: version is a document whose meaning is unknown, and guessing at it is how a
#: receipt ends up describing a report nobody produced.
REPORT_VERSION = 1

#: The plugin module name, as `pytest -p` takes it. Loading it by module name
#: rather than by path is what lets the runner resolve it through the same
#: `sys.path` every other import in the child resolves through.
PLUGIN_NAME = "vkit.verifiers.pytest_adapter"

#: The flag carrying where the report goes. It is a flag rather than a
#: substitution placeholder because the two documented placeholders are
#: `{{run_dir}}` and `{{python}}`, and adding a third to the manifest parser
#: would widen a frozen contract for the sake of one flag.
REPORT_FLAG = "--vkit-json-report"

#: The outcomes this code distinguishes, taken from the runner's own vocabulary.
#: A name outside this set is `report_malformed`: a future runner could add one,
#: and reading it with today's meanings would be a guess.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
ERRORED = "error"
OUTCOMES = frozenset({PASSED, FAILED, SKIPPED, ERRORED})

#: How much of a failing or erroring test's message reaches a receipt. A
#: counterexample has to be readable by the engineer who is asked to fix it, and
#: unbounded it would carry a whole traceback into durable evidence for every
#: failure. Bounded is a stated limit rather than a hidden one.
MESSAGE_LIMIT = 2000


# --------------------------------------------------------------- the report


@dataclass(frozen=True)
class TestOutcome:
    """One test id, as the runner reported it.

    `outcome` is the runner's own word and is not translated here. Translating
    it at this layer would put a second vocabulary beside the first, and the two
    would drift the first time the runner added a word.
    """

    nodeid: str
    outcome: str
    duration: float
    message: str

    @property
    def passed(self) -> bool:
        return self.outcome == PASSED


@dataclass(frozen=True)
class RunnerReport:
    """A report whose shape this code has already accepted.

    Constructing one is the only way to hold a report, so a caller cannot hold a
    document that failed the refusals above: the shape is the constructor's
    argument and the refusals are its callers.
    """

    version: int
    complete: bool
    tests: tuple[TestOutcome, ...]

    def by_id(self) -> dict[str, TestOutcome]:
        """Every result keyed by test id. A duplicate cannot appear: `_read`
        refuses a contradictory one, and two identical entries are one result."""
        return {test.nodeid: test for test in self.tests}


@dataclass(frozen=True)
class AdapterResult:
    """What one pytest run's structured report established.

    `observations` and `counterexamples` are separate rather than one list with a
    flag, because a run with both is a FAIL whose receipt must still name what
    did pass. A single list would force the receipt to either drop the passing
    cases or present the failing ones as successes.
    """

    passed: tuple[CaseObligation, ...]
    observations: tuple[tuple[CaseObligation, str], ...]
    counterexamples: tuple[tuple[CaseObligation, str], ...]

    @property
    def is_pass(self) -> bool:
        return not self.counterexamples

    def scenarios(self) -> tuple:
        """The reading in the shape every existing outcome consumer already reads.

        Built here rather than by the caller, so a report, a receipt and a
        console view all describe a pytest run through one projection rather than
        each translating the runner's vocabulary for itself.
        """
        from ..outcome import ScenarioResult

        return tuple(
            ScenarioResult(o.test_id, True, observation)
            for o, observation in self.observations
        ) + tuple(
            ScenarioResult(o.test_id, False, trace) for o, trace in self.counterexamples
        )

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """The satisfied obligations and counterexamples, in the receipt's shapes.

        A satisfied case carries the observation the runner produced and a
        counterexample carries the failure message. Nothing else is emitted: a
        theorem satisfied record needs an axiom audit and a property one needs
        state counts, and neither is derivable from a pytest report, so a pytest
        receipt has no way to claim either.
        """
        satisfied = [
            {
                "kind": "case_satisfied",
                "obligation": obligation_to_json(obligation),
                "observation": observation,
            }
            for obligation, observation in self.observations
        ]
        counterexamples = [
            {
                "obligation": obligation_to_json(obligation),
                "trace": trace,
            }
            for obligation, trace in self.counterexamples
        ]
        return satisfied, counterexamples


# -------------------------------------------------------- the pytest plugin
#
# Everything below this line runs inside the pinned runner's process, and is
# imported by pytest rather than by this package's callers. It writes exactly one
# file and decides nothing.


def pytest_addoption(parser: Any) -> None:
    """Teach the runner the one flag vkit needs it to accept."""
    group = parser.getgroup("vkit", "vkit evidence contract")
    group.addoption(
        REPORT_FLAG, action="store", default=None, metavar="PATH",
        help="write a vkit evidence report to PATH",
    )


def pytest_configure(config: Any) -> None:
    """Install the collector before any test runs.

    The report is accumulated on the config object rather than in a module-level
    list, because a module-level list survives across runs in the same
    interpreter. A process that ran pytest twice would otherwise report the first
    run's tests as the second's, which is precisely the duplicate a run of the
    same test id must be refused for.
    """
    config._vkit_reported = []


def pytest_runtest_makereport(item: Any, call: Any) -> None:
    """Record one phase of one test, from the hook that receives the config.

    `pytest_runtest_logreport` is the more obvious hook and cannot be used: the
    `TestReport` it is handed carries no config, and reaching for one raises
    `AttributeError` on every test and turns the whole run into pytest's
    INTERNAL_ERROR. Measured, and the failure is a true BLOCKED whose detail then
    names the wrong cause. This hook receives the `item`, and the item carries
    both the config and the phase.

    Only the phases that carry a decision are recorded, and a test's verdict is
    its `call` phase. A test that fails and then passes in its teardown has two
    phases saying different things, and the call phase is the one that says
    whether the code under test met its expectation.
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
        # A failure outside the call phase is the test not running rather than
        # the code disagreeing with its expectation, and the two are different
        # answers. Distinguishing them is why the phases are not collapsed.
        "outcome": (
            SKIPPED if skipped is not None
            else PASSED if excinfo is None
            else FAILED if call.when == "call" and _is_assertion(excinfo)
            else ERRORED
        ),
        "duration": float(getattr(call, "duration", 0.0)),
        "message": skipped if skipped is not None else _message(excinfo),
    })


def _is_assertion(excinfo: Any) -> bool:
    """Whether this exception is a failed assertion rather than a crash.

    Two types, because the runner raises two. `pytest.fail(msg)` raises
    `Failed`, which is not a subclass of `AssertionError` -- it descends from
    `OutcomeException` instead -- while a bare `assert` statement raises
    `AssertionError` directly and never goes through `Failed` at all. Measured on
    this host, and testing for `Failed` alone recorded every passing test as an
    error: a passing test raises nothing, so the check ran against a `None` path
    and reported the phase it was in.

    Read off the exception's own type rather than its text, because the runner's
    own judgement is what this adapter is recording. A `RuntimeError` from a
    fixture is neither of these, and treating it alike is how a broken
    environment becomes a red test that a reader is invited to go and fix.
    """
    import pytest as _pytest

    return isinstance(excinfo.value, (AssertionError, _pytest.fail.Exception))


def _skip(call: Any) -> str | None:
    """The runner's skip reason for this phase, or None when it was not skipped."""
    import pytest as _pytest

    excinfo = getattr(call, "excinfo", None)
    if excinfo is None:
        return None
    if isinstance(excinfo.value, _pytest.skip.Exception):
        return str(excinfo.value) or "skipped"
    return None


def _message(excinfo: Any) -> str:
    """Whatever the runner recorded about a failure, bounded.

    `str(excinfo.value)` is the assertion's own message and is what a reader
    needs; the traceback is what pytest already wrote to the run's stderr log.
    Duplicating the whole traceback into durable evidence would grow a receipt
    without adding anything a reader could act on. Measured: a bare `assert`
    raises `AssertionError` whose `str` carries both the author's message and
    the comparison, so one string is enough to act on.
    """
    if excinfo is None:
        return "the runner reported a failure with no message"
    return str(excinfo.value)[:MESSAGE_LIMIT] or type(excinfo.value).__name__


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Write the report, or say that it could not be written.

    A failure to write is recorded as an absent report rather than swallowed:
    `report_absent` names a runner that produced nothing, and a report that
    could not be written is that, not a pass.
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
    # Written to a temporary name and moved, so a reader never sees half a
    # document. The reader is a different process on the other side of this one
    # exiting, and a partial read would be refused as malformed with a reason
    # that named the wrong thing.
    staged = target.with_suffix(target.suffix + ".partial")
    staged.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    staged.replace(target)


#: Exit statuses that mean the runner stopped before finishing what it set out to
#: do. Each is an interrupted run, not a completed one whose tests failed, and a
#: document written by one of them says so through `complete`.
_INTERRUPTED_EXITSTATUSES = frozenset({2, 3, 4})


# ------------------------------------------------------------ the refusals


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable, matchable token."""
    return Blocked(reason, f"{token}: {detail}")


def _read(raw: bytes) -> RunnerReport | Blocked:
    """Decode and shape-check a report, refusing each way it can be unusable."""
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
        parsed.append(TestOutcome(nodeid, outcome, float(duration), message))

    contradiction = _contradiction(parsed)
    if contradiction is not None:
        return _refuse(
            "report_contradiction", BlockedReason.ARTIFACT_MALFORMED,
            f"test {contradiction[0]!r} is reported twice with different results "
            f"({contradiction[1]} and {contradiction[2]}), so the report does not "
            "describe one execution",
        )
    return RunnerReport(version, True, tuple(parsed))


def _contradiction(tests: list[TestOutcome]) -> tuple[str, str, str] | None:
    """One id reported twice with different outcomes, or None.

    A report carrying the same id twice with the same result is one result: the
    runner is allowed to report a test's setup and its call and the plugin
    collapses those already, so two identical entries mean a runner that
    repeated itself, which is not evidence against the check.
    """
    seen: dict[str, str] = {}
    for test in tests:
        first = seen.setdefault(test.nodeid, test.outcome)
        if first != test.outcome:
            return (test.nodeid, first, test.outcome)
    return None


# -------------------------------------------------------------- the adapter


def interpret(raw: bytes, check: PytestCheck) -> AdapterResult | Blocked:
    """What the run established, or why it could not be established.

    The required test ids are compared by exact match against what the report
    names. Exact is the only defensible match: pytest node ids carry the
    parameterisation, and two ids differing only in a parameter are two tests,
    so a prefix or substring comparison would let a required parameterisation be
    discharged by a different one.
    """
    report = _read(raw)
    if isinstance(report, Blocked):
        return report
    reported = report.by_id()

    missing = [t for t in check.required_tests if t not in reported]
    if missing:
        # An exit code of zero does not reach this branch's absence; the report
        # is what does, which is why a runner that passed everything else and
        # skipped the required test cannot pass.
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

    passed: list[CaseObligation] = []
    counterexamples: list[tuple[CaseObligation, str]] = []
    observations: list[tuple[CaseObligation, str]] = []
    for test_id in check.required_tests:
        result = reported[test_id]
        obligation = CaseObligation(test_id)
        if result.outcome == FAILED:
            counterexamples.append((obligation, result.message or "the assertion failed"))
            continue
        passed.append(obligation)
        observations.append((
            obligation,
            f"the runner reported {result.outcome} in {result.duration:g}s",
        ))
    return AdapterResult(
        tuple(passed), tuple(observations), tuple(counterexamples)
    )
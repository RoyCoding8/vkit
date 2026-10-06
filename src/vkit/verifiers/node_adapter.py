"""Reads a `node --test` run from a TAP 13 report written by a reporter this module ships, and
decides what the run established. Nothing is read from the console, and exit code zero is never
a pass.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from ..outcome import Blocked, BlockedReason
from ..nowindow import hidden_window
from .obligation import CaseObligation, case_results, scenario_results
from .spec import NodeTestCheck

REPORT_VERSION = 13

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
TODOED = "todoed"
ERRORED = "error"
OUTCOMES = frozenset({PASSED, FAILED, SKIPPED, TODOED, ERRORED})

MESSAGE_LIMIT = 2000

ASSERTION_CODE = "ERR_ASSERTION"

# Stock Node TAP gives only failing entries a `location`, so this reporter emits TAP 13 with
# a `location` on every entry. It is passed as a `data:` URL because a bare absolute path
# fails on Windows (the drive letter reads as a URL scheme) and no packaged file is needed.
REPORTER_SOURCE = '''\
import { Transform } from "node:stream";

const cwd = process.cwd();

function relative(file) {
  if (!file) return "";
  const path = String(file).replace(/\\\\/g, "/");
  const base = cwd.replace(/\\\\/g, "/").replace(/\\/+$/, "") + "/";
  return path.startsWith(base) ? path.slice(base.length) : path;
}

/* Node reports a wrapper test named after each file; it is not a declared case. */
function isFileWrapper(data) {
  if (!data.file || !data.name) return false;
  return String(data.file).replace(/\\\\/g, "/").split("/").pop() === data.name;
}

/* The event stream wraps every failure and loses `error.code`; `error.cause.name`
   is `AssertionError` for `assert.*` and the thrown class otherwise. */
function failureCode(error) {
  if (!error) return null;
  if (error.code === "ERR_ASSERTION") return "ERR_ASSERTION";
  const cause = error.cause;
  if (cause && cause.name === "AssertionError") return "ERR_ASSERTION";
  return "ERR_TEST_FAILURE";
}

export default class VkitNodeReporter extends Transform {
  constructor() {
    super({ objectMode: true });
    this.events = [];
  }

  _transform(event, encoding, done) {
    this.events.push(event);
    done();
  }

  _flush(done) {
    const cases = [];
    let counts = null;
    for (const event of this.events) {
      const data = event.data || {};
      if (event.type === "test:summary" && data.counts && !data.file) counts = data.counts;
      if (event.type !== "test:pass" && event.type !== "test:fail") continue;
      const details = data.details || {};
      if (details.type !== "test") continue;
      if (isFileWrapper(data)) continue;
      const error = details.error;
      cases.push({
        name: data.name,
        location: relative(data.file),
        skip: data.skip == null ? null : String(data.skip),
        code: failureCode(error),
        message: error ? String(error.message || error) : null,
        duration: typeof details.duration_ms === "number" ? details.duration_ms : 0,
      });
    }

    let out = "TAP version 13\\n# vkit node evidence\\n";
    cases.forEach((entry, index) => {
      const failed = entry.code !== null;
      const verdict = failed ? "not ok" : "ok";
      out += verdict + " " + (index + 1) + " - " + entry.name;
      if (entry.skip !== null) out += " # SKIP " + entry.skip;
      out += "\\n  ---\\n";
      out += "  duration_ms: " + entry.duration + "\\n";
      out += "  type: 'test'\\n";
      out += "  location: '" + entry.location + "'\\n";
      if (failed) out += "  code: " + entry.code + "\\n";
      if (failed) out += "  message: " + JSON.stringify(entry.message || "") + "\\n";
      out += "  ...\\n";
    });
    out += "1.." + cases.length + "\\n";
    out += "# tests " + (counts ? counts.tests : cases.length) + "\\n";
    out += "# pass " + (counts ? counts.passed : 0) + "\\n";
    out += "# fail " + (counts ? counts.failed : 0) + "\\n";
    out += "# skipped " + (counts ? counts.skipped : 0) + "\\n";
    done(null, out);
  }
}
'''

REPORTER_SCHEME = "data:text/javascript;base64,"

PLAN_MARKER = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)


def reporter_argv_token() -> str:
    """The `--test-reporter` value naming the inlined reporter."""
    return REPORTER_SCHEME + base64.b64encode(REPORTER_SOURCE.encode("utf-8")).decode("ascii")


# Node 24 stabilised the spelling; older runners exit 9 on it, writing no report.
ISOLATION_FLAG_STABLE = "--test-isolation=none"
ISOLATION_FLAG_EXPERIMENTAL = "--experimental-test-isolation=none"

STABLE_ISOLATION_FROM = 24


@lru_cache(maxsize=None)
def isolation_flag(executable: str) -> str:
    """The spelling of the no-isolation flag this `node` understands.

    `--test-isolation=none` keeps the runner from reporting each file as a test of its own; Node
    24 stabilised the spelling, and an unreadable version gets the experimental one.
    """
    major = _major_version(executable)
    return ISOLATION_FLAG_STABLE if major >= STABLE_ISOLATION_FROM else ISOLATION_FLAG_EXPERIMENTAL


def _major_version(executable: str) -> int:
    """The binary's major release, or 0 when it will not say."""
    try:
        done = subprocess.run(
            [executable, "--version"], capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=VERSION_PROBE_SECONDS, **hidden_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.search(r"v(\d+)", done.stdout or "")
    return int(match.group(1)) if match else 0


VERSION_PROBE_SECONDS = 20


CASE_SEPARATOR = "::"


def case_parts(test_id: str) -> tuple[str, str] | Blocked:
    """The `(file, name)` a required case id names, or a Blocked when the id is not
    `<file>::<name>`.
    """
    file, separator, name = test_id.partition(CASE_SEPARATOR)
    if not separator or not file or not name:
        return Blocked(
            BlockedReason.SCENARIO_UNKNOWN,
            f"required_test_absent: {test_id!r} is not a Node case id. One is "
            f"'<file>{CASE_SEPARATOR}<name>', because a test name on its own does "
            "not identify a case: two files may declare the same name",
        )
    return file, name


def argv_selectors(required: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    """The files to run and the `--test-name-pattern` selecting the required names, or `((),
    refusal text)` for a malformed id.

    The pattern is an exact alternation, so a required name is never discharged by a longer one.
    """
    files: list[str] = []
    names: list[str] = []
    for test_id in required:
        parts = case_parts(test_id)
        if isinstance(parts, Blocked):
            return (), parts.detail
        file, name = parts
        if file not in files:
            files.append(file)
        if name not in names:
            names.append(name)
    return tuple(files), "|".join(re.escape(name) for name in names)


@dataclass(frozen=True)
class CaseOutcome:
    """One test case as the runner reported it; a required id is the pair `(location, name)`
    because two files may share a name.
    """

    name: str
    location: str
    outcome: str
    duration: float
    message: str

    @property
    def identity(self) -> str:
        return f"{self.location}::{self.name}"


@dataclass(frozen=True)
class RunnerReport:
    """A report that passed every shape check in `_read`."""

    tests: tuple[CaseOutcome, ...]

    def by_id(self) -> dict[str, CaseOutcome]:
        """Results keyed by case id; `_read` has already refused contradictory duplicates."""
        return {test.identity: test for test in self.tests}


@dataclass(frozen=True)
class AdapterResult:
    """What one Node run established, in the same shape as the pytest adapter's result."""

    observations: tuple[tuple[CaseObligation, str], ...]
    counterexamples: tuple[tuple[CaseObligation, str], ...]

    def scenarios(self) -> tuple:
        """The reading as outcome `ScenarioResult`s."""
        return scenario_results(self.observations, self.counterexamples)

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """Satisfied cases and counterexamples as run-record dicts."""
        return case_results(self.observations, self.counterexamples)


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable token that callers and tests match on."""
    return Blocked(reason, f"{token}: {detail}")


_ENTRY = re.compile(
    r"^(?P<verdict>not ok|ok)\s+(?P<number>\d+)\s+-\s+(?P<name>.+?)"
    r"(?:\s+#\s+(?P<directive>SKIP|TODO)\b(?P<skip>.*))?\s*$",
    re.MULTILINE,
)

_FIELDS = {
    "location": re.compile(r"^\s+location:\s+'(?P<value>.*)'\s*$", re.MULTILINE),
    "code": re.compile(r"^\s+code:\s+(?P<value>\S+)\s*$", re.MULTILINE),
    "duration_ms": re.compile(r"^\s+duration_ms:\s+(?P<value>[\d.eE+-]+)\s*$", re.MULTILINE),
    "message": re.compile(r"^\s+message:\s+(?P<value>.*)$", re.MULTILINE),
}


def _read(raw: bytes) -> RunnerReport | Blocked:
    """Decode and shape-check a report, refusing each unusable form with its own token."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is not UTF-8: {exc}",
        )

    header = text.splitlines()[0].strip() if text.strip() else ""
    match = re.fullmatch(r"TAP version (\d+)", header)
    if match is None:
        return _refuse(
            "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
            f"the report opens with {header!r}, which is not a TAP version line. "
            "Console output is not evidence, so a runner that wrote prose instead "
            "of a report is refused rather than searched for the word passed",
        )
    version = int(match.group(1))
    if version != REPORT_VERSION:
        return _refuse(
            "report_version", BlockedReason.ARTIFACT_MALFORMED,
            f"the report is TAP version {version}; this vkit reads version "
            f"{REPORT_VERSION} only",
        )

    plan = PLAN_MARKER.search(text)
    if plan is None:
        return _refuse(
            "report_truncated", BlockedReason.ARTIFACT_MALFORMED,
            "the report carries no plan marker, so the runner stopped before it "
            "finished writing and the cases it did list are not the cases it would "
            "have listed",
        )

    tests = _entries(text)
    if isinstance(tests, Blocked):
        return tests
    if not tests:
        return _refuse(
            "report_empty", BlockedReason.ARTIFACT_EMPTY,
            "the runner collected no test cases, which is not evidence about anything",
        )

    contradiction = _contradiction(tests)
    if contradiction is not None:
        return _refuse(
            "report_contradiction", BlockedReason.ARTIFACT_MALFORMED,
            f"case {contradiction[0]!r} is reported twice with different results "
            f"({contradiction[1]} and {contradiction[2]}), so the report does not "
            "describe one execution",
        )

    announced = int(plan.group(1))
    if announced != len(tests):
        return _refuse(
            "report_truncated", BlockedReason.ARTIFACT_MALFORMED,
            f"the report's plan announces {announced} case(s) and it carries "
            f"{len(tests)}, so it does not describe the run it claims to",
        )
    return RunnerReport(tuple(tests))


def _entries(text: str) -> list[CaseOutcome] | Blocked:
    """Every case in the report, in document order."""
    parsed: list[CaseOutcome] = []
    for match in _ENTRY.finditer(text):
        name = match.group("name").strip()
        if not name:
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                "the report has an entry naming no test",
            )
        block = _block_after(text, match.end())
        if block is None:
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"case {name!r} has no field block, so its result is not stated",
            )
        location = _field(block, "location")
        if not location:
            return _refuse(
                "report_malformed", BlockedReason.ARTIFACT_MALFORMED,
                f"case {name!r} reports no location, so it cannot be matched against "
                "a required case. Stock Node TAP omits this on a passing entry, "
                "which is why vkit supplies its own reporter",
            )
        parsed.append(CaseOutcome(
            name=name,
            location=location,
            outcome=_outcome_of(match, block),
            duration=_duration(block),
            message=_message(block),
        ))
    return parsed


def _block_after(text: str, start: int) -> str | None:
    """The `---` to `...` block following one entry, or None."""
    window = text[start:start + 8192]
    opening = window.find("---")
    if opening == -1:
        return None
    closing = window.find("...", opening)
    if closing == -1:
        return None
    return window[opening:closing]


def _field(block: str, name: str) -> str:
    """One field of a block, or "" when absent."""
    match = _FIELDS[name].search(block)
    if match is None:
        return ""
    value = match.group("value")
    if name == "message":
        try:
            return str(json.loads(value))
        except json.JSONDecodeError:
            return value.strip("'\"")
    return value


def _outcome_of(entry: "re.Match[str]", block: str) -> str:
    """The outcome for one entry. SKIP and TODO directives are read before the verdict because a
    skipped case is reported as `ok`.
    """
    if entry.group("directive") == "SKIP":
        return SKIPPED
    if entry.group("directive") == "TODO":
        return TODOED
    if entry.group("verdict") == "ok":
        return PASSED
    return ERRORED if _field(block, "code") != ASSERTION_CODE else FAILED


def _duration(block: str) -> float:
    raw = _field(block, "duration_ms")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _message(block: str) -> str:
    message = _field(block, "message")
    return message[:MESSAGE_LIMIT] if message else ""


def _contradiction(tests: list[CaseOutcome]) -> tuple[str, str, str] | None:
    """One case reported twice with different outcomes, or None. Identical repeats are one result.
    """
    seen: dict[str, str] = {}
    for test in tests:
        first = seen.setdefault(test.identity, test.outcome)
        if first != test.outcome:
            return (test.identity, first, test.outcome)
    return None


def interpret(raw: bytes, check: NodeTestCheck) -> AdapterResult | Blocked:
    """What the run established, or why it could not. Required ids match exactly on `(file, name)`.
    """
    report = _read(raw)
    if isinstance(report, Blocked):
        return report
    reported = report.by_id()

    malformed = [
        test_id for test_id in check.required_tests
        if isinstance(case_parts(test_id), Blocked)
    ]
    if malformed:
        return _refuse(
            "required_test_absent", BlockedReason.SCENARIO_UNKNOWN,
            f"required case(s) are not Node case ids: {', '.join(malformed)}. One is "
            f"'<file>{CASE_SEPARATOR}<name>', because a test name on its own does not "
            "identify a case",
        )

    missing = [t for t in check.required_tests if t not in reported]
    if missing:
        return _refuse(
            "required_test_absent", BlockedReason.SCENARIO_UNKNOWN,
            f"the report never ran required case(s): {', '.join(missing)}",
        )

    skipped = [
        reported[t].identity for t in check.required_tests
        if reported[t].outcome in (SKIPPED, TODOED)
    ]
    if skipped:
        return _refuse(
            "required_test_skipped", BlockedReason.SCENARIO_UNKNOWN,
            f"required case(s) were skipped or deferred rather than run: "
            f"{', '.join(skipped)}. A check that declares a test must watch it run",
        )

    errored = [
        reported[t].identity for t in check.required_tests
        if reported[t].outcome == ERRORED
    ]
    if errored:
        return _refuse(
            "infrastructure_error", BlockedReason.INTERNAL_ERROR,
            f"required case(s) errored before asserting anything: "
            f"{', '.join(errored)}. That is the test failing to run, not the code "
            "disagreeing with its expectation",
        )

    counterexamples: list[tuple[CaseObligation, str]] = []
    observations: list[tuple[CaseObligation, str]] = []
    for test_id in check.required_tests:
        result = reported[test_id]
        obligation = CaseObligation(test_id)
        if result.outcome == FAILED:
            counterexamples.append((
                obligation, result.message or "the assertion failed",
            ))
            continue
        observations.append((
            obligation,
            f"the runner reported {result.outcome} for {result.location} in "
            f"{result.duration:g}ms",
        ))
    return AdapterResult(tuple(observations), tuple(counterexamples))
"""Interpret one `node --test` run from a vkit-owned TAP report, and refuse everything else.

The twin of `pytest_adapter`, and the same contract in a different runner's
vocabulary. Both halves hold: a reporter vkit ships writes the document, and the
reader below is the only thing that decides what it means. Nothing here reads
the console, and an exit code of zero is never on its own a pass.

**Why a vkit reporter rather than the runner's own TAP.** `node --test` writes
TAP 13, and it is a fine format, but a passing TAP entry carries no file
identity: only a failing entry carries `location:`. Measured on node 24.16.0.
Two files that each declare `test('shared')` therefore produce two TAP entries
both named "shared", and the only thing that tells them apart is the location
on the one that failed. A check that declares required tests by file cannot be
satisfied from that, and an adapter that matched on the bare name would let one
file's pass stand in for another's.

The runner also emits an event stream carrying `file` on every case, and it
accepts `--test-reporter=<module>`. `REPORTER_SOURCE` below is that reporter.
It emits TAP 13, so `report_format` stays `node_tap` as the frozen manifest
schema requires and `EXPECTED_REPORT_VERSION` stays 1, and it adds the one field
TAP lacks: a `location` on every entry, so a required case is matched on the
pair (file, name) rather than on a name that two files may share.

**Why the reporter is a `data:` URL.** Node resolves `--test-reporter` through
the ESM loader, and on Windows a bare absolute path is refused with
`ERR_UNSUPPORTED_ESM_URL_SCHEME` (measured) because a drive-letter path reads as
a URL scheme. A `file:` URL would need a real `.mjs` file inside the installed wheel, and
`pyproject.toml` and `schemas/` belong to other owners at this checkpoint.
Inlining the reporter as a base64 `data:` URL needs no new packaged file, works
on every platform Node runs on, and keeps the whole evidence path in bytes this
module owns. Measured working on node 24.16.0.

**The refusals.** The same eight tokens `pytest_adapter` uses, deliberately, so
there is one vocabulary for "this report cannot answer the question" across
every test-kind adapter. A reader who learns one adapter has learned the
vocabulary rather than having to learn a second dialect per runner:

  report_version         the document is not a TAP version this code reads
  report_truncated       the document has no plan marker, so it stopped early
  report_malformed       the document is not the shape this code reads
  report_empty           the runner collected no tests at all
  report_contradiction   one case carries two different results
  required_test_absent   a required case never appears in the report
  required_test_skipped  a required case appears, but was not run
  infrastructure_error   a required case errored before it could assert anything

A report that was never written is not in that list, for the reason
`pytest_adapter` gives: `execution._derive` catches the absence one layer up and
returns `ARTIFACT_MISSING` naming the file. Measured: pointing `node --test` at
a path that does not exist exits 1 having written no report, which is exactly
that case.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from functools import lru_cache

from ..outcome import Blocked, BlockedReason
from .obligation import CaseObligation, obligation_to_json
from .spec import NodeTestCheck

#: The TAP version this code reads. A document whose first line says anything
#: else is refused rather than parsed on a best effort: the fields below are read
#: by name, so an unknown version is a document whose meaning is unknown.
REPORT_VERSION = 13

#: The version the manifest's `expect_report_version` counts in. The frozen
#: schema pins it to a minimum of 1 and the report announces `TAP version 13`,
#: so the number a check declares is this one and the number on the wire is
#: REPORT_VERSION. Both are recorded rather than conflated, because a check that
#: declares 1 and a document that says 13 are not a contradiction: the first
#: counts vkit's report format, the second counts the wire format it wraps.
EXPECTED_REPORT_VERSION = 1

#: The outcomes this code distinguishes, taken from the runner's own vocabulary.
#: `skipped` and `todo` are separate words in TAP and stay separate here: a test
#: the author deferred and a test a condition suppressed are different facts, and
#: collapsing them is how a deferred case becomes an unexplained gap.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
TODOED = "todoed"
ERRORED = "error"
OUTCOMES = frozenset({PASSED, FAILED, SKIPPED, TODOED, ERRORED})

#: How much of a failing case's message reaches a receipt. The same bound, and
#: for the same reason, as `pytest_adapter.MESSAGE_LIMIT`: a counterexample has
#: to be readable by the engineer asked to fix it, and unbounded it carries a
#: whole stack into durable evidence on every failure.
MESSAGE_LIMIT = 2000

#: Node's own code for an assertion that failed. Measured on 24.16.0: stock TAP
#: reports `ERR_ASSERTION` for a disagreeing assertion and `ERR_TEST_FAILURE`
#: for a `throw`, a TypeError and an unhandled rejection alike. The event stream
#: this adapter reads wraps every failure and loses that code, so the inlined
#: reporter recovers the distinction from `error.cause.name`, which is
#: `AssertionError` for `assert.*` and the thrown error's own class otherwise.
#: That split is the difference between the code under test disagreeing with its
#: expectation and the test failing to run, which the receipt has to tell apart:
#: one is a FAIL and one is BLOCKED.
ASSERTION_CODE = "ERR_ASSERTION"

#: The reporter, inlined. Read `REPORTER_SOURCE` for why it is a data URL.
REPORTER_SOURCE = '''\
import { Transform } from "node:stream";

/* vkit's Node evidence reporter.

   Emits TAP 13 so the format a check declares is the format the wire carries,
   and adds the one field stock Node TAP lacks: a `location` on every entry, so
   a required case is matched on the file it ran in as well as the name it
   declared. Decides nothing. It observes what the runner reports and writes it
   down; `vkit.verifiers.node_adapter` is the only thing that reads it. */

const cwd = process.cwd();

function relative(file) {
  if (!file) return "";
  const path = String(file).replace(/\\\\/g, "/");
  const base = cwd.replace(/\\\\/g, "/").replace(/\\/+$/, "") + "/";
  return path.startsWith(base) ? path.slice(base.length) : path;
}

/* A test the runner reports under the name of its own file is the wrapper node
   adds per file, not a case anybody declared. It is dropped here rather than
   counted, so `# tests` and the entry list describe the same set. */
function isFileWrapper(data) {
  if (!data.file || !data.name) return false;
  return String(data.file).replace(/\\\\/g, "/").split("/").pop() === data.name;
}

/* An assertion that failed, told from a test that failed some other way.

   The runner's own `error.code` is `ERR_TEST_FAILURE` for both, so it cannot
   separate them. Measured on 24.16.0: stock TAP reports `ERR_ASSERTION` for a
   disagreeing assertion and `ERR_TEST_FAILURE` for a throw, a TypeError and an
   unhandled rejection, but the event stream wraps every one of them and loses
   that code. What survives the wrapping is `error.cause.name`, which is
   `AssertionError` for `assert.*` and the thrown error's own class otherwise.
   That is the signal the reader below uses, and it is the runner's own
   judgement rather than a guess at the message text. */
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

#: How the inline reporter is named on the command line. Node loads it through
#: the ESM loader, so it is a URL and not a path.
REPORTER_SCHEME = "data:text/javascript;base64,"

#: The marker the runner writes after the last entry. Its absence is how a
#: report that stopped early is recognised, which is the same device
#: `pytest_adapter` uses with its `complete` flag: a completeness fact the
#: document carries about itself rather than one the reader infers.
PLAN_MARKER = re.compile(r"^1\.\.(\d+)\s*$", re.MULTILINE)


def reporter_argv_token() -> str:
    """The `--test-reporter` value naming the inlined reporter."""
    return REPORTER_SCHEME + base64.b64encode(REPORTER_SOURCE.encode("utf-8")).decode("ascii")


#: The flag that runs every file in one process, in each spelling the runner has
#: used. The first is the current one; the second is how Node 22 and 23 spell the
#: same behaviour, and the reason is that on those lines the flag was not stable
#: enough to carry without the `experimental_` prefix.
ISOLATION_FLAG_STABLE = "--test-isolation=none"
ISOLATION_FLAG_EXPERIMENTAL = "--experimental-test-isolation=none"

#: The first Node major that accepted the unprefixed spelling. Measured: 24.16.0
#: accepts `--test-isolation=none`; 22.23.3 answers it with `bad option`, exits 9
#: and writes no report, while accepting `--experimental-test-isolation=none`.
STABLE_ISOLATION_FROM = 24


@lru_cache(maxsize=None)
def isolation_flag(executable: str) -> str:
    """The spelling of the no-isolation flag this `node` binary understands.

    **Why the flag is version-dependent at all.** `--test-isolation=none` is what
    makes required-case accounting mean something: with the default per-file
    isolation the runner reports each FILE as a test of its own, so a required
    case would be satisfied by a file that ran no case at all. That reasoning is
    right on every version. Only the spelling moved.

    A binary that does not recognize the flag exits 9 having written nothing, and
    `execution._derive` reads that as `artifact_missing` -- "node-report.tap was
    not written, so the check reported nothing". That refusal is CORRECT and
    useless: it names the missing file, not the runner's refusal to start. Seven
    tests in `tests/test_node_verifier.py` failed on ubuntu-latest and
    windows-latest for this reason and passed on macos-latest, because the macOS
    image ships Node 24.20.0 and the other two ship Node 22.

    The version is read from the binary rather than guessed from a table of
    known releases, and an unreadable or unparseable version yields the
    experimental spelling, which is the one the older and therefore wider range
    of runners accepts. Node 22 is not going away in a way that makes that the
    wrong default, and a Node that cannot be asked is a Node whose runner output
    will not be trusted anyway.
    """
    major = _major_version(executable)
    return ISOLATION_FLAG_STABLE if major >= STABLE_ISOLATION_FROM else ISOLATION_FLAG_EXPERIMENTAL


def _major_version(executable: str) -> int:
    """The binary's major release, or 0 when it will not say.

    0 rather than a raise, because the caller has a usable answer either way and
    a version probe must not become the reason a check cannot run. `subprocess`
    is imported here so this module stays importable in a process that has no
    business spawning anything.
    """
    import subprocess

    from ..nowindow import hidden_window

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


#: How long a version probe waits. A `--version` that has not answered in this
#: time is a binary that is not going to answer, and the probe's caller has a
#: default to fall back on.
VERSION_PROBE_SECONDS = 20


#: What separates a required case id into its file and its name.
CASE_SEPARATOR = "::"


def case_parts(test_id: str) -> tuple[str, str] | None:
    """The `(file, name)` a required case id names, or None when it names neither.

    An id is the file the case ran in and the name it declared, joined by `::`,
    because a bare name is not an identity: two files may declare `test('shared')`
    and a check that required one of them would be satisfied by the other. The
    pair is what makes the requirement mean one case.

    A `Blocked` rather than a raise, because an id that is not a pair is a
    manifest error and the refusal belongs to the interpreter rather than to
    whatever happened to try the split first.
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
    """The files to run and the name pattern that selects the required cases.

    Two arguments derived from one declaration, because the runner takes them
    separately: `node --test` is given the files, and `--test-name-pattern` is
    given the names. Passing the ids themselves to the runner does not work, and
    the failure is quiet enough to be worth naming. Measured: `node --test` read
    a `file::name` argument as a filename and wrote
    `Could not find 'test/a.test.mjs::alpha'` to stderr, exiting 1 having run
    nothing and written no report.

    The pattern is an alternation of the exact names, not a prefix. A regex that
    matched a prefix would let `an even two-way` be discharged by a case named
    `an even two-way split when there are three people`, which is the
    substring problem `pytest_adapter` refuses at the other end.

    A malformed id yields no selectors and the refusal text rather than a raise,
    so `dispatch.argv_for` can hand the adapter nothing to launch and the core's
    own refusal for an empty command is what a reader sees. `interpret` applies
    the same refusal again on the report bytes, so the reason is stated whichever
    of the two reads it first.
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


# --------------------------------------------------------------- the report


@dataclass(frozen=True)
class CaseOutcome:
    """One test case, as the runner reported it.

    `location` is part of the identity rather than a detail. Node reports a file
    on every case, and two files may declare the same test name, so a required
    id is the pair. `outcome` is the runner's own word and is not translated
    here, for the reason `pytest_adapter.TestOutcome` gives.
    """

    name: str
    location: str
    outcome: str
    duration: float
    message: str

    @property
    def identity(self) -> str:
        return f"{self.location}::{self.name}"

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
    tests: tuple[CaseOutcome, ...]

    def by_id(self) -> dict[str, CaseOutcome]:
        """Every result keyed by case id. A duplicate cannot appear: `_read`
        refuses a contradictory one, and two identical entries are one result."""
        return {test.identity: test for test in self.tests}


@dataclass(frozen=True)
class AdapterResult:
    """What one Node run's report established.

    The same shape `pytest_adapter.AdapterResult` carries, so one receipt builder
    and one outcome bridge read both. A reader that handled a pytest run
    handles this one without knowing which runner produced it.
    """

    passed: tuple[CaseObligation, ...]
    observations: tuple[tuple[CaseObligation, str], ...]
    counterexamples: tuple[tuple[CaseObligation, str], ...]

    @property
    def is_pass(self) -> bool:
        return not self.counterexamples

    def scenarios(self) -> tuple:
        """The reading in the shape every existing outcome consumer already reads.

        Built here rather than by the caller, for the reason
        `pytest_adapter.AdapterResult.scenarios` gives: a report, a receipt and a
        console view all describe a Node run through one projection rather than
        each translating the runner's vocabulary for itself.
        """
        from ..outcome import ScenarioResult

        return tuple(
            ScenarioResult(obligation.test_id, True, observation)
            for obligation, observation in self.observations
        ) + tuple(
            ScenarioResult(obligation.test_id, False, trace)
            for obligation, trace in self.counterexamples
        )

    def obligation_results(self) -> tuple[list[dict], list[dict]]:
        """The satisfied obligations and counterexamples, in the receipt's shapes.

        Same two lists, same reason for two: a run with both is a FAIL whose
        receipt must still name what did pass.
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


# ------------------------------------------------------------ the refusals


def _refuse(token: str, reason: BlockedReason, detail: str) -> Blocked:
    """A BLOCKED whose detail leads with a stable, matchable token."""
    return Blocked(reason, f"{token}: {detail}")


#: One entry's YAML block. Node writes the fields of a case as a `---` block
#: between the entry line and a `...` terminator, one `  key: value` per line.
#: Only the four keys this reader needs are looked for, and each is read by name
#: so an unknown key in the block is ignored rather than guessed at.
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
    """Decode and shape-check a report, refusing each way it can be unusable."""
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

    # The plan marker is what a runner writes after the last entry. A document
    # without one stopped before it got there, and the cases it does list are not
    # the cases it would have listed. Measured: a report cut mid-stream has the
    # entries and no `1..N`.
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
    return RunnerReport(version, tuple(tests))


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
    """The `---` to `...` block following one entry, or None when it has none."""
    window = text[start:start + 8192]
    opening = window.find("---")
    if opening == -1:
        return None
    closing = window.find("...", opening)
    if closing == -1:
        return None
    return window[opening:closing]


def _field(block: str, name: str) -> str:
    """One field of a block, or an empty string when the block omits it."""
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
    """The outcome this reader gives one entry, from the runner's own words.

    The order matters and is the runner's: a `# SKIP` or `# TODO` directive is
    read before the verdict, because the runner reports a skipped case as `ok`
    and a verdict-first reading would call it a pass.
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
    """One case reported twice with different outcomes, or None.

    A report carrying the same case twice with the same result is one result,
    for the reason `pytest_adapter._contradiction` gives.
    """
    seen: dict[str, str] = {}
    for test in tests:
        first = seen.setdefault(test.identity, test.outcome)
        if first != test.outcome:
            return (test.identity, first, test.outcome)
    return None


# -------------------------------------------------------------- the adapter


def interpret(raw: bytes, check: NodeTestCheck) -> AdapterResult | Blocked:
    """What the run established, or why it could not be established.

    A required id is matched on the pair `(file, name)`, compared exactly. The
    pair rather than the bare name because two files may declare the same test
    name, and exact because a prefix or substring comparison would let a
    required case be discharged by a differently named one.
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
        # An exit code of zero does not reach this branch's absence; the report
        # is what does, which is why a runner that passed everything else and
        # never ran the required case cannot pass.
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

    passed: list[CaseObligation] = []
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
        passed.append(obligation)
        observations.append((
            obligation,
            f"the runner reported {result.outcome} for {result.location} in "
            f"{result.duration:g}ms",
        ))
    return AdapterResult(
        tuple(passed), tuple(observations), tuple(counterexamples)
    )
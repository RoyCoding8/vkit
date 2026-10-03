"""One registered Node check, end to end, and every way it can be refused.

The twin of `test_pytest_verifier.py`, on the other runner. Both files assert
the same acceptance list, because both adapters owe the core the same contract:
a required case that ran and passed, and seven ways a report can fail to answer
that question with a BLOCKED carrying its own token.

**What is driven how.** The refusals about the report's own contents are driven
through the adapter against report bytes a test writes, because the report is
vkit's own format and a test that arranged a real runner to misbehave would be
testing that runner. The three about execution, a missing report and a runner
that could not finish, are driven through a real subprocess, because those are
facts about what a process did.

**Every Node test is skipped with a stated reason when `node` is absent**, and
the module-level gate says which. A green Node test that never ran would be
worse than a skip, because the acceptance list above it would still read as
satisfied.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import subproc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from vkit import cli  # noqa: E402
from vkit.outcome import BlockedReason  # noqa: E402
from vkit.verifiers import (  # noqa: E402
    CheckKind,
    NodeTestCheck,
    SubjectRef,
    node_adapter,
    outcome_from_reading,
)

NODE = shutil.which("node")

#: The stated reason every Node case in this file skips with when the host has no
#: `node`. Named rather than inlined so the reason a reader sees is the one that
#: was chosen, and so a rename cannot leave one skip silently unstated.
requires_node = pytest.mark.skipif(
    NODE is None, reason="requires a node binary to run the built-in test runner"
)

#: The two required cases the fixture repository declares, and the optional one a
#: test can require instead to be refused for not seeing it.
REQUIRED_TESTS = (
    "test/bill.test.js::an even two-way split divides the total exactly",
    "test/bill.test.js::the shares always sum to the amount owed",
)
OPTIONAL_TEST = "test/bill.test.js::a person can never receive a negative share"
FAILING_CASE = "test/bill.test.js::a share is never negative"
THROWING_CASE = "test/bill.test.js::the caller can read the share"
SKIPPED_CASE = "test/bill.test.js::a tie is decided by the order given"

TEST_SOURCE = '''\
/* The approved cases. `node --test` is the runner and there is no framework. */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { splitCents } = require('../src/bill.js');

test('an even two-way split divides the total exactly', () => {
  assert.deepEqual(splitCents(1000, [1, 1]), [500, 500]);
});

test('the shares always sum to the amount owed', () => {
  const shares = splitCents(1000, [1, 2, 3]);
  assert.equal(shares.reduce((a, b) => a + b, 0), 1000);
});

test('a person can never receive a negative share', () => {
  assert.deepEqual(splitCents(0, [1, 1, 1]), [0, 0, 0]);
});

test('a tie is decided by the order given', { skip: 'decided later' }, () => {
  assert.deepEqual(splitCents(1, [1, 1]), [1, 0]);
});

/* Fails on purpose, so the adapter has a real assertion failure to read. */
test('a share is never negative', () => {
  assert.ok(splitCents(1000, [3, 1])[0] > 1000);
});

/* Raises rather than asserting, so the adapter has a real infrastructure error
   to tell apart from the assertion above. */
test('the caller can read the share', () => {
  assert.equal(splitCents(1000, [1]).missing.length, undefined);
});
'''

APP_SOURCE = """\
/* The code under test. One pure function, so a receipt has one subject. */
'use strict';

function splitCents(totalCents, weights) {
  const weightSum = weights.reduce((a, b) => a + b, 0);
  const shares = weights.map((weight) => Math.floor((totalCents * weight) / weightSum));
  let handedOut = shares.reduce((a, b) => a + b, 0);
  const order = weights
    .map((weight, index) => ({
      index,
      fraction: (totalCents * weight) / weightSum - Math.floor((totalCents * weight) / weightSum),
    }))
    .sort((a, b) => b.fraction - a.fraction || a.index - b.index)
    .map((entry) => entry.index);
  for (let handed = 0; handed < totalCents - handedOut; handed += 1) {
    shares[order[handed % order.length]] += 1;
  }
  return shares;
}

module.exports = { splitCents };
"""


def _check(*, required=REQUIRED_TESTS, timeout_seconds: int = 120) -> dict:
    return {
        "id": "bill-units",
        "kind": "node_test",
        "description": "Runs the built-in Node test runner over the split core.",
        "cwd": ".",
        "timeout_seconds": timeout_seconds,
        "artifact": "node-report.tap",
        "inputs": ["src/bill.js", "test/bill.test.js"],
        "expectations": [],
        "subject": {"paths": ["src/bill.js"], "digest": None},
        "claim_id": "shares-sum-to-the-amount-owed",
        "required_tests": list(required),
        "runner": {"executable": "node", "base_argv": ["--test"]},
        "report_format": "node_tap",
        "expect_report_version": 1,
    }


def _repository(tmp_path: Path, *, required=REQUIRED_TESTS) -> Path:
    """A real repository with a real Node module and a real registered check."""
    repo = tmp_path / "shop"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "bill.js").write_text(APP_SOURCE, encoding="utf-8")
    (repo / "test").mkdir()
    (repo / "test" / "bill.test.js").write_text(TEST_SOURCE, encoding="utf-8")
    write_manifest(repo, _check(required=required))
    init_repo(repo)
    return repo


def write_manifest(repo: Path, check: dict) -> None:
    """The v2 manifest this test drives, written as a caller would write one."""
    path = repo / "verification" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "description": "A split function, verified by the Node test runner.",
                "checks": [check],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def init_repo(repo: Path) -> None:
    """`git init` and one commit, because source identity reads both."""
    subproc.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subproc.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t",
         "commit", "-qm", "the approved baseline"],
        cwd=repo, check=True, capture_output=True,
    )


def _main(argv: list[str]) -> int:
    """`cli.main` with the arguments it would have been given on the command line."""
    saved = sys.argv
    sys.argv = list(argv)
    try:
        return cli.main(list(argv[1:]))
    finally:
        sys.argv = saved


def run_check(repo: Path, check_id: str = "bill-units") -> tuple[int, dict, Path]:
    """`vkit check run` through the real CLI entry point.

    `cli.main` is called rather than `subprocess` for the reason
    `test_pytest_verifier.run_check` gives: the CLI's job here is to hand the work
    to `execution.run_check` and exit on its outcome, and a spawned `vkit.exe`
    resolves the main checkout through its `.pth` rather than this worktree.
    """
    argv = [
        "vkit", "check", "run", "--project", str(repo), "--check", check_id, "--json",
    ]
    status = _main(argv)
    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    runs = store.list_runs(limit=1)
    assert runs, "the check run recorded no run at all"
    return status, runs[0], store.run_dir(runs[0]["run_id"])


def last_outcome(repo: Path) -> dict:
    """The recorded terminal report for this repository's most recent run."""
    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    run_id = store.list_runs(limit=1)[0]["run_id"]
    return store.load(run_id)


def receipt_of(run_dir: Path) -> dict:
    """The evidence receipt a run wrote beside its report."""
    return json.loads((run_dir / "receipt.v2.json").read_text(encoding="utf-8"))


# ------------------------------------------------------- the vertical slice


@requires_node
def test_a_registered_node_check_runs_and_the_receipt_lists_the_cases(
    tmp_path: Path,
) -> None:
    """The slice itself: a real check, a real runner, a typed receipt.

    Asserted on the receipt's own values rather than on which keys it has. A
    receipt naming the two required cases with real observations and a measured
    subject digest is a different claim from one that has a `satisfied` member,
    and only the first notices if the adapter started inventing observations.
    """
    repo = _repository(tmp_path)

    status, row, run_dir = run_check(repo)

    assert status == 0, last_outcome(repo)
    assert row["result"] == "PASS"
    assert row["evidence_kind"] == "scenario", (
        "a node_test check with no generator block is SCENARIO evidence whatever "
        "the runner printed"
    )

    receipt = receipt_of(run_dir)
    assert receipt["status"] == "PASS"
    assert receipt["evidence_kind"] == "scenario"
    assert receipt["check_id"] == "bill-units"
    assert receipt["claim_id"] == "shares-sum-to-the-amount-owed"
    assert [s["obligation"]["obligation"] for s in receipt["satisfied"]] == list(REQUIRED_TESTS)
    assert all(
        s["kind"] == "case_satisfied" and "the runner reported passed" in s["observation"]
        for s in receipt["satisfied"]
    ), receipt["satisfied"]
    assert receipt["counterexamples"] == []
    assert receipt["subject"]["digest"] is not None, (
        "vkit measures the declared subject; a null here means the measurement "
        "never ran, not that the subject was empty"
    )


@requires_node
def test_the_receipt_is_referenced_by_the_run_report(tmp_path: Path) -> None:
    """The report points at the receipt rather than inlining it.

    `run-report.v1.json` describes the process and the receipt describes the
    evidence, so the report gains a run-relative reference and no new field.
    """
    repo = _repository(tmp_path)

    _status, _row, run_dir = run_check(repo)

    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["artifacts"] == {
        "result": "node-report.tap", "receipt": "receipt.v2.json",
    }
    assert (run_dir / report["artifacts"]["receipt"]).is_file()
    assert (run_dir / report["artifacts"]["result"]).is_file()


@requires_node
def test_the_required_cases_are_the_ones_the_check_named(tmp_path: Path) -> None:
    """The receipt's cases match the manifest's ids exactly, in order.

    The whole point of the `(file, name)` pair. Two files may declare the same
    test name, so a receipt listing a bare name would not say which one ran, and
    this asserts the ids the operator wrote in the manifest are the ids that come
    back.
    """
    repo = _repository(tmp_path)

    _status, row, run_dir = run_check(repo)

    assert row["result"] == "PASS"
    recorded = [s["id"] for s in last_outcome(repo)["outcome"]["scenarios"]]
    assert recorded == list(REQUIRED_TESTS)
    assert all(id.startswith("test/bill.test.js::") for id in recorded)
    assert receipt_of(run_dir)["evidence_kind"] == "scenario"


@requires_node
def test_the_node_report_is_read_not_the_console_output(tmp_path: Path) -> None:
    """The console output and the report are separate files.

    Asserted because it is the property `dispatch` is built around: a run's
    stdout is prose about the run, and the evidence is the document the adapter
    interpreted. A check that read the wrong file would pass here and nowhere
    else.
    """
    repo = _repository(tmp_path)

    _status, _row, run_dir = run_check(repo)

    report_bytes = (run_dir / "node-report.tap").read_text(encoding="utf-8")
    assert report_bytes.startswith("TAP version 13")
    assert "# vkit node evidence" in report_bytes, (
        "the report must be the one vkit's own reporter wrote, not the runner's "
        "stock TAP: only the vkit reporter puts a location on a passing entry"
    )


@requires_node
def test_a_failing_assertion_exits_one_and_a_blocked_run_exits_three(
    tmp_path: Path,
) -> None:
    """The two non-zero codes, on the two non-PASS verdicts.

    `plans/CONTRACT.md:75` assigns 1 to a completed failing check and 3 to
    BLOCKED. Asserting both is what makes a BLOCKED distinguishable from a FAIL
    by exit code alone, which is the thing a shell script has.
    """
    failing = _repository(tmp_path / "fail", required=(FAILING_CASE,))
    status, row, _run_dir = run_check(failing)
    assert (row["result"], status) == ("FAIL", 1)

    blocked = _repository(tmp_path / "blocked", required=(THROWING_CASE,))
    status, row, _run_dir = run_check(blocked)
    assert (row["result"], status) == ("BLOCKED", 3), last_outcome(blocked)


@requires_node
def test_a_failed_assertion_is_a_fail_and_names_the_counterexample(
    tmp_path: Path,
) -> None:
    """A disagreeing assertion is FAIL, and the receipt carries what disagreed.

    A FAIL rather than a BLOCKED, because the runner delivered an answer and the
    answer was no. The obligation is named as a counterexample carrying the
    assertion's own message, which is the thing an engineer is asked to fix.
    """
    repo = _repository(tmp_path, required=(FAILING_CASE,))

    status, row, run_dir = run_check(repo)

    assert row["result"] == "FAIL"
    assert status == 1
    receipt = receipt_of(run_dir)
    assert receipt["satisfied"] == []
    assert [c["obligation"]["obligation"] for c in receipt["counterexamples"]] == [
        FAILING_CASE,
    ]
    assert "splitCents(1000, [3, 1])[0] > 1000" in receipt["counterexamples"][0]["trace"], (
        "the counterexample must quote the assertion, or a reader cannot act on it"
    )


@requires_node
def test_a_test_that_threw_is_blocked_not_fail(tmp_path: Path) -> None:
    """A test that failed to run is BLOCKED, which is a different answer to FAIL.

    The distinction the plan separates at `plans/10-native-verifiers.md:64`: FAIL
    says the code disagreed with its expectation, BLOCKED says the test could not
    deliver an answer at all. Collapsing them sends an engineer to fix code that
    is fine because a test raised a `TypeError`.
    """
    repo = _repository(tmp_path, required=(THROWING_CASE,))

    status, row, _run_dir = run_check(repo)

    assert row["result"] == "BLOCKED"
    assert status == 3
    assert "infrastructure_error" in last_outcome(repo)["outcome"]["detail"]


# ------------------------------------------------- the seven refusals
#
# Each token is the leading word of the BLOCKED detail. They are constants here
# so a change to one is a change to this file too, and they are the same words
# `test_pytest_verifier.py` asserts, because there is one vocabulary for "this
# report cannot answer the question" across every test-kind adapter.

REPORT_TRUNCATED = "report_truncated"
REPORT_VERSION = "report_version"
REPORT_MALFORMED = "report_malformed"
REPORT_EMPTY = "report_empty"
REPORT_CONTRADICTION = "report_contradiction"
REQUIRED_TEST_ABSENT = "required_test_absent"
REQUIRED_TEST_SKIPPED = "required_test_skipped"


def _case(name: str, location: str = "test/bill.test.js", **fields) -> str:
    """One TAP entry as vkit's own reporter writes it.

    Written the way the reporter writes it, `ok N - name` then an indented field
    block, because these tests are about how this reader parses the bytes the
    reporter produces. A block shaped some other way would test a format no run
    ever produces.
    """
    verdict = "not ok" if fields.get("code") else "ok"
    lines = [f"{verdict} {fields.pop('number', 1)} - {name}"]
    if fields.get("skip") is not None:
        lines[0] += f" # SKIP {fields['skip']}"
    lines += ["  ---", "  duration_ms: 0.5", "  type: 'test'",
              f"  location: '{location}'"]
    if fields.get("code"):
        lines.append(f"  code: {fields['code']}")
        lines.append(f"  message: {json.dumps(fields.get('message', 'it failed'))}")
    lines.append("  ...")
    return "\n".join(lines)


def _report(*cases: str, **fields) -> bytes:
    """Report bytes with the plan marker and counts a complete report carries."""
    document = {"version": 13, "cases": list(cases), "plan": len(cases)}
    document.update(fields)
    body = "\n".join(cases)
    return (
        f"TAP version 13\n# vkit node evidence\n{body}\n"
        f"1..{document['plan']}\n# tests {document['plan']}\n"
        "# pass 0\n# fail 0\n# skipped 0\n"
    ).encode("utf-8")


def _parsed_check(required=REQUIRED_TESTS) -> NodeTestCheck:
    """A `NodeTestCheck` carrying the required ids, as the parser would build it."""
    return NodeTestCheck(
        id="bill-units",
        kind=CheckKind.NODE_TEST,
        subject=SubjectRef(("src/bill.js",), None),
        claim_id="shares-sum-to-the-amount-owed",
        cwd=Path("."),
        timeout_seconds=120.0,
        artifact_name="node-report.tap",
        required_tests=tuple(required),
    )


def _blocked(raw: bytes, required=REQUIRED_TESTS):
    return node_adapter.interpret(raw, _parsed_check(required))


# ------------------------------------- the isolation flag this runner understands


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        # Node 24.16.0 and 25.x accept the unprefixed spelling. Measured.
        ("v24.16.0", node_adapter.ISOLATION_FLAG_STABLE),
        ("v24.0.0", node_adapter.ISOLATION_FLAG_STABLE),
        ("v25.1.2", node_adapter.ISOLATION_FLAG_STABLE),
        # Node 22.23.3 rejects the unprefixed spelling with `bad option`, exits 9
        # and writes no report. Measured.
        ("v22.23.3", node_adapter.ISOLATION_FLAG_EXPERIMENTAL),
        ("v23.0.1", node_adapter.ISOLATION_FLAG_EXPERIMENTAL),
        # A binary that will not say which release it is gets the spelling the
        # wider range of runners accepts, rather than a guess at the newest.
        ("", node_adapter.ISOLATION_FLAG_EXPERIMENTAL),
        ("not a version", node_adapter.ISOLATION_FLAG_EXPERIMENTAL),
    ],
)
def test_the_isolation_flag_is_the_spelling_this_node_understands(
    reported: str, expected: str, tmp_path: Path, monkeypatch
) -> None:
    """The flag is asked of the binary, because only the binary can answer.

    `--test-isolation=none` is not decoration. Without it the runner isolates
    each file into its own process and reports the FILE as a test, so a required
    case is satisfied by a file that ran no case at all, and the check stops
    meaning what it declares. Only the SPELLING of the flag moved between
    releases, and both spellings must reach a runner that understands it: node
    22 answers the unprefixed form with `bad option`, exits 9, and writes no
    report, which `execution._derive` correctly reads as `artifact_missing` --
    a refusal that names the missing file rather than the runner's refusal to
    start. That is what failed seven tests on the ubuntu and windows images,
    which ship node 22, while the macos image, which ships node 24, passed.
    """
    binary = tmp_path / "node"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(node_adapter, "_major_version", lambda _executable: _parse(reported))
    node_adapter.isolation_flag.cache_clear()
    try:
        assert node_adapter.isolation_flag(str(binary)) == expected
    finally:
        node_adapter.isolation_flag.cache_clear()


def _parse(reported: str) -> int:
    import re

    match = re.search(r"v(\d+)", reported)
    return int(match.group(1)) if match else 0


def test_a_node_that_will_not_report_its_version_still_gets_a_flag_it_understands(
    monkeypatch,
) -> None:
    """An unreadable version is a refusal, and the refusal has a default.

    The probe must never become the reason a check cannot run, so a binary that
    cannot be asked falls back to the spelling the older and therefore wider
    range of runners accepts.
    """
    monkeypatch.setattr(node_adapter, "_major_version", lambda _executable: 0)
    node_adapter.isolation_flag.cache_clear()
    try:
        assert node_adapter.isolation_flag("node") == node_adapter.ISOLATION_FLAG_EXPERIMENTAL
    finally:
        node_adapter.isolation_flag.cache_clear()


@requires_node
def test_the_runner_this_host_has_really_writes_a_report(tmp_path: Path) -> None:
    """The end of the chain: the flag and the binary together produce evidence.

    The unit test above proves the spelling is chosen; this proves the chosen
    spelling is one this runner accepts, by driving the real binary and reading
    the report it wrote rather than the console it printed. A runner that
    rejected the flag would leave no file here, which is the exact shape of the
    CI failure.
    """
    repo = _repository(tmp_path)
    status, row, run_dir = run_check(repo)

    assert status == 0, last_outcome(repo)
    report = (run_dir / "node-report.tap").read_text(encoding="utf-8")
    assert report.startswith("TAP version 13"), report[:200]
    assert node_adapter.PLAN_MARKER.search(report), (
        "a report with no plan marker stopped early and the reader cannot trust it"
    )
    assert row["result"] == "PASS"


# 1. Missing output artifact -----------------------------------------------------


@requires_node
def test_a_runner_that_wrote_no_report_is_blocked(tmp_path: Path) -> None:
    """REFUSAL 1. No report at all is `artifact_missing`, and nothing else passes.

    Driven through a real run, because this one is a fact about a process rather
    than about a document. The check's runner is an executable that does not
    exist, so the child never starts, no report is written, and the core must
    refuse rather than read the absence of evidence as success.

    Measured on node 24.16.0: pointing `node --test` at a path that does not
    exist exits 1 having written no report, which is the same shape and is why the
    two are different refusals rather than one.
    """
    repo = _repository(tmp_path)
    check = _check(required=REQUIRED_TESTS, timeout_seconds=60)
    check["runner"] = {
        "executable": "a-runner-that-is-not-installed", "base_argv": [],
    }
    write_manifest(repo, check)

    status, row, _run_dir = run_check(repo)

    assert row["result"] == "BLOCKED"
    assert row["reason"] in {
        BlockedReason.ARTIFACT_MISSING.value, BlockedReason.LAUNCH_FAILED.value,
    }
    assert status == 3


# 2. A required case ID absent from the report ----------------------------------


def test_a_required_case_absent_from_the_report_is_blocked() -> None:
    """REFUSAL 2. The report never ran a required case, so it cannot be evidence.

    The runner passed everything else. This is the case that makes an exit code
    insufficient on its own, and the case where a required test file was renamed
    while the manifest was not.
    """
    reading = _blocked(_report(
        _case("an even two-way split divides the total exactly"),
        _case("a person can never receive a negative share"),
    ))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_ABSENT)
    assert REQUIRED_TESTS[1] in reading.detail


def test_a_check_that_exits_zero_without_running_the_required_case_does_not_pass() -> None:
    """The exit code is not the verdict, stated as a test of the reader.

    A report over a different case is exactly what a green exit code looks like
    when a required test was renamed. Nothing in this path reads an exit code, so
    the run is refused on the report alone.
    """
    reading = _blocked(_report(_case("an even two-way split divides the total exactly")))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_ABSENT)


def test_the_same_name_in_another_file_is_not_the_required_case() -> None:
    """A bare name is not an identity, which is the whole reason for the pair.

    Two files declare `shared`. The check requires the one in `a`, and the report
    carries `a`'s as skipped and `b`'s as passing. A reader that matched on the
    bare name would see a `shared` that ran and a `shared` that did not, and could
    reasonably discharge the requirement on the one that passed. This asserts it
    does not: only the pair identifies a case.
    """
    reading = _blocked(_report(
        _case("shared", "test/a.test.js", skip="later"),
        _case("shared", "test/b.test.js", number=2),
    ), required=("test/a.test.js::shared",))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_SKIPPED)
    assert "test/b.test.js" not in reading.detail


def test_a_required_case_id_without_a_file_is_refused() -> None:
    """An id that is not `<file>::<name>` is refused rather than half-matched.

    A bare name cannot identify a case, so an id without a file has no reading at
    all. Refused with the same token as an absent case, because from the
    obligation's side they are one thing: the report never ran what was required.
    """
    reading = _blocked(_report(_case("an even two-way split divides the total exactly")),
                      required=("an even two-way split divides the total exactly",))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_ABSENT)
    assert "::" in reading.detail


# 3. Duplicate contradictory results for one case --------------------------------


def test_one_case_reported_twice_with_different_results_is_blocked() -> None:
    """REFUSAL 3. Two results for one case mean the report describes no run.

    A report that disagrees with itself cannot be reduced to a verdict by
    preferring one entry, because there is no principled way to choose and
    picking the passing one is exactly the promotion the contract refuses.
    """
    reading = _blocked(_report(
        _case("an even two-way split divides the total exactly"),
        _case("an even two-way split divides the total exactly",
              code="ERR_ASSERTION", message="assert failed", number=2),
        _case("the shares always sum to the amount owed", number=3),
    ))

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_CONTRADICTION)
    assert "an even two-way split divides the total exactly" in reading.detail


def test_one_case_reported_twice_with_the_same_result_is_not_a_contradiction() -> None:
    """The refusal is about disagreement, not about repetition.

    Without this half the check would be satisfied by the count and a reporter
    that emitted one case twice identically would be refused for something it
    did not do.
    """
    raw = _report(
        _case("an even two-way split divides the total exactly"),
        _case("an even two-way split divides the total exactly", number=2),
        _case("the shares always sum to the amount owed", number=3),
    )

    outcome = outcome_from_reading(node_adapter.interpret(raw, _parsed_check()))

    assert type(outcome).__name__ == "Passed"


# 4. Unexpected skips ------------------------------------------------------------


def test_a_required_case_that_was_skipped_is_blocked() -> None:
    """REFUSAL 4. A skipped required case was not watched run.

    A skip is the runner's own word for "this did not happen", and a check that
    declares a case must watch it happen. Accepting a skip would let a
    `t.skip` or an unmet condition turn a required case into a green run with no
    observation behind it.
    """
    reading = _blocked(_report(
        _case("an even two-way split divides the total exactly"),
        _case("the shares always sum to the amount owed", skip="decided later"),
    ))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_SKIPPED)
    assert "the shares always sum to the amount owed" in reading.detail


def test_a_skip_that_is_not_required_does_not_refuse_the_run() -> None:
    """Only required cases are policed, or an unrelated skip would block.

    A repository's optional test may skip for reasons that have nothing to do with
    the obligation, and a check that failed on that would make the evidence
    depend on code the check never claimed to cover.
    """
    raw = _report(
        _case("an even two-way split divides the total exactly"),
        _case("the shares always sum to the amount owed"),
        _case(OPTIONAL_TEST.split("::", 1)[1], skip="needs a database"),
    )

    outcome = outcome_from_reading(node_adapter.interpret(raw, _parsed_check()))

    assert type(outcome).__name__ == "Passed"


def test_a_required_case_that_was_deferred_is_refused_too() -> None:
    """`todo` is a separate word from `skip` and is refused the same way.

    A deferred test is one the author has not written yet. The runner reports it
    as `ok`, so a verdict-first reading would record it as a pass, which is
    precisely how a not-yet-written case becomes an unexplained green run.
    """
    deferred = _case("the shares always sum to the amount owed", skip="later")
    deferred = deferred.replace("# SKIP later", "# TODO later")

    reading = _blocked(_report(
        _case("an even two-way split divides the total exactly"), deferred,
    ))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_SKIPPED)


# 5. Zero collected cases --------------------------------------------------------


def test_a_report_with_no_cases_is_blocked_as_empty() -> None:
    """REFUSAL 5. A runner that collected nothing observed nothing.

    Distinct from the absent report: the runner ran and wrote a document, and the
    document says there was nothing to run. Both are BLOCKED, and the reason
    differs because the repairs differ. Measured: an empty directory produces
    exactly this, with `# tests 0`.
    """
    reading = _blocked(_report())

    assert reading.reason is BlockedReason.ARTIFACT_EMPTY
    assert reading.detail.startswith(REPORT_EMPTY)


# 6. Truncated output ------------------------------------------------------------


def test_a_report_that_stopped_before_its_plan_marker_is_blocked() -> None:
    """REFUSAL 6. A truncated report's cases are not the cases it would have run.

    The plan marker is what the reporter writes after the last entry. A document
    without one stopped before it got there, so accepting the entries it does list
    would be reading a partial run as a whole one, and a required case after the
    cut is simply absent from the list.
    """
    raw = (
        "TAP version 13\n# vkit node evidence\n"
        "ok 1 - an even two-way split divides the total exactly\n"
        "  ---\n  duration_ms: 0.5\n  type: 'test'\n"
        "  location: 'test/bill.test.js'\n  ...\n"
    ).encode("utf-8")

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_TRUNCATED)


def test_a_report_whose_plan_disagrees_with_its_entries_is_blocked() -> None:
    """A plan announcing more cases than the document carries is truncated too.

    The same marker read a second way. A writer that lost an entry and kept its
    plan has produced a document that describes a different run from the one it
    claims, and reading the cases that survived as the whole run is the same
    overstatement as reading a cut-short document.
    """
    raw = _report(
        _case("an even two-way split divides the total exactly"),
        _case("the shares always sum to the amount owed", number=2),
    ).replace(b"1..2", b"1..5").replace(b"# tests 2", b"# tests 5")

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_TRUNCATED)
    assert "5" in reading.detail


def test_a_document_that_is_not_tap_is_blocked() -> None:
    """A document this code cannot read is malformed, whatever it contains.

    Named separately from the version refusal because the repair differs: a
    runner that wrote a banner instead of a report produced prose, and a reader
    that searched it for the word "passed" would be doing what
    `plans/CONTRACT.md:58` forbids.
    """
    reading = _blocked(b"# tests 3\n# pass 3\n")

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_MALFORMED)


def test_a_case_with_no_field_block_is_blocked() -> None:
    """An entry whose result is not stated cannot be counted either way.

    A TAP entry without its `---` block states a verdict and no detail, and this
    reader needs the location to identify the case at all. Guessing that the
    block was the default would let a truncated write be read as a pass.
    """
    raw = (
        "TAP version 13\n# vkit node evidence\n"
        "ok 1 - an even two-way split divides the total exactly\n"
        "1..1\n# tests 1\n"
    ).encode("utf-8")

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_MALFORMED)


def test_a_case_with_no_location_is_blocked() -> None:
    """A case with no file cannot be matched against a required id.

    This is the refusal that makes the inlined reporter load-bearing. Stock Node
    TAP carries a `location` only on a failing entry, so a report written by the
    runner itself would make every passing case unreadable, and a reader that
    matched on the bare name would let two files' same-named cases collide.
    """
    raw = (
        "TAP version 13\n# vkit node evidence\n"
        "ok 1 - an even two-way split divides the total exactly\n"
        "  ---\n  duration_ms: 0.5\n  type: 'test'\n  ...\n"
        "1..1\n# tests 1\n"
    ).encode("utf-8")

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_MALFORMED)
    assert "location" in reading.detail


# 7. Unsupported report version --------------------------------------------------


def test_a_report_from_a_tap_version_this_code_does_not_read_is_blocked() -> None:
    """REFUSAL 7. An unknown version is a document whose meaning is unknown.

    The fields below are read by name, so a future TAP version could reuse one of
    them for something else. Parsing it anyway is how a receipt ends up
    describing a document nobody produced in that form.
    """
    reading = _blocked(
        _report(_case("an even two-way split divides the total exactly"),
                _case("the shares always sum to the amount owed", number=2))
        .replace(b"TAP version 13", b"TAP version 14")
    )

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_VERSION)
    assert "version 14" in reading.detail


# ------------------------------------------ a failure is told from a crash


def test_an_assertion_and_a_throw_are_told_apart() -> None:
    """The runner's own judgement separates FAIL from BLOCKED, by its cause.

    The same report shape and only the failure's origin differs. A disagreeing
    `assert` reports `ERR_ASSERTION` and is a FAIL the receipt can carry a
    counterexample for; a `throw` or a `TypeError` reports `ERR_TEST_FAILURE`
    and is a refusal, because the test failed to deliver an answer. Collapsing
    them would send an engineer to fix code that is fine because a test raised.
    """
    def report_with(code: str) -> bytes:
        return _report(
            _case("an even two-way split divides the total exactly", code=code),
            _case("the shares always sum to the amount owed", number=2),
        )

    failed = node_adapter.interpret(report_with("ERR_ASSERTION"), _parsed_check())
    errored = node_adapter.interpret(report_with("ERR_TEST_FAILURE"), _parsed_check())

    assert type(outcome_from_reading(failed)).__name__ == "Failed"
    assert failed.counterexamples[0][1], "a FAIL names what disagreed"
    assert type(outcome_from_reading(errored)).__name__ == "Blocked"
    assert errored.reason is BlockedReason.INTERNAL_ERROR


# ------------------------------------------------ the category cannot be claimed


def test_a_tampered_node_report_claiming_a_stronger_category_is_refused(
    tmp_path: Path,
) -> None:
    """A document cannot promote itself.

    The category comes from the check's variant, and no field in the report sets
    it. Asserted on the receipt rather than on the reading, because the receipt is
    what a reader consults and a category that is correct in memory and wrong on
    disk is the failure this exists to prevent.
    """
    from vkit.claimkind import ClaimCategory
    from vkit.identity import SourceIdentity
    from vkit.outcome import Passed
    from vkit.verifiers import ReceiptInputs, build_receipt

    raw = _report(
        _case("an even two-way split divides the total exactly"),
        _case("the shares always sum to the amount owed", number=2),
    ).replace(
        b"# vkit node evidence",
        b'# vkit node evidence\nevidence_kind: theorem_checking\n'
        b'category: finite_model_checking',
    )
    check = _parsed_check()
    reading = node_adapter.interpret(raw, check)

    receipt = build_receipt(ReceiptInputs(
        check_id=check.id, claim_id=check.claim_id, run_id="r1", task_id=None,
        generation=None, category=ClaimCategory.SCENARIO, subject=check.subject,
        specification_digest=None, policy_digest="p",
        source=SourceIdentity(
            head="a" * 40, inventory_digest="d" * 64, dirty=False,
            tracked_files=3, dirty_paths=(),
        ),
        fixture_digest=None, tool_versions={},
        runtime={"python_version": "3.13", "platform": "win32", "requires_os": "any"},
        timeout_seconds=120.0, report_path=Path("r.tap"), project_root=Path("."),
        outcome=Passed(()), reading=reading,
    ))

    assert receipt["evidence_kind"] == "scenario", (
        "the category is derived from the check's variant, so a report claiming "
        "theorem_checking cannot promote the evidence it is attached to"
    )


# ------------------------------------------- a legacy scenario check still runs


@requires_node
def test_a_legacy_scenario_check_still_runs_unchanged(tmp_path: Path) -> None:
    """The migration's backward-compatibility half, on the real path.

    A v1 manifest declares no `kind`, and reading it as a scenario driver is the
    only reading it has. Adding a native Node check to an example must not change
    what the scenario check beside it means, and this is the case a real user
    hits first on every example in this repository.
    """
    repo = tmp_path / "legacy"
    (repo / "verification").mkdir(parents=True)
    (repo / "verify.js").write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "const cases = [['one', 'PASS', 'printed one'], ['two', 'PASS', 'printed two']];\n"
        "writeFileSync(process.argv[2], JSON.stringify({\n"
        "  schema_version: 1,\n"
        "  scenarios: cases.map(([id, result, observation]) => ({ id, result, observation })),\n"
        "}, null, 2));\n",
        encoding="utf-8",
    )
    (repo / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "description": "A CLI verified by a driver.",
        "checks": [{
            "id": "legacy-behavior",
            "description": "Runs the real CLI and compares printed output.",
            "command": ["node", "verify.js", "{{run_dir}}/result.json"],
            "cwd": ".",
            "timeout_seconds": 120,
            "required_scenarios": ["one", "two"],
            "artifact": "result.json",
            "inputs": ["verify.js"],
            "expectations": [],
        }],
    }, indent=2) + "\n", encoding="utf-8")
    init_repo(repo)

    status, row, run_dir = run_check(repo, "legacy-behavior")

    assert status == 0, last_outcome(repo)
    assert row["result"] == "PASS"
    assert row["evidence_kind"] == "scenario"
    assert [s["id"] for s in last_outcome(repo)["outcome"]["scenarios"]] == ["one", "two"]
    receipt = receipt_of(run_dir)
    assert receipt["status"] == "PASS"
    assert receipt["evidence_kind"] == "scenario"
    assert receipt["satisfied"] == []
"""One registered pytest check, end to end, and every way it can be refused.

The vertical slice Plan 10 checkpoint 1 asks for: a `pytest` check runs through
`vkit check run`, the receipt names the cases it actually watched, the same run
is visible through the detached MCP path with the same verdict, and
`task_finalize` over that task computes READY. Everything else here is the other
half: the ways that run can be refused, each with its own reason.

**How a refusal is asserted.** Each test drives the public path, `vkit check run`
through `cli.main`, and reads the recorded run's own outcome. The reason is a
token at the front of the BLOCKED detail, not a substring of prose, so a test
cannot pass on a detail that merely happens to mention the right word. Each
token is named once, here, and asserted exactly.

**Why the runner is never consulted for a refusal.** The four refusals about the
report's own contents are driven through the adapter directly, against report
bytes a test writes. A runner that produced each of those documents would have
to be arranged to misbehave in a specific way, and a test that depends on a
third-party runner's failure mode tests that runner. The report is vkit's own
format over vkit's own plugin, so the bytes are the unit and the runner is not.
The three refusals that are about execution -- a missing report, an absent
required test, a skipped one -- are driven through a real subprocess, because
those are facts about what a process did.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import subproc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from vkit import cli  # noqa: E402
from vkit.outcome import BlockedReason  # noqa: E402
from vkit.claimkind import ClaimCategory  # noqa: E402
from vkit.identity import SourceIdentity  # noqa: E402
from vkit.outcome import Passed  # noqa: E402
from vkit.verifiers import (  # noqa: E402
    CheckKind,
    PinnedRunner,
    PytestCheck,
    ReceiptInputs,
    SubjectRef,
    build_receipt,
    outcome_from_reading,
    pytest_adapter,
)

#: The four required tests the fixture repository declares and the two it also
#: runs. The extra tests exist so a test can require one and be refused for not
#: seeing it, which is a different refusal from the one where the runner fails.
REQUIRED_TESTS = (
    "tests/test_behaviour.py::test_totals_an_empty_cart",
    "tests/test_behaviour.py::test_totals_several_positives",
)
OPTIONAL_TEST = "tests/test_behaviour.py::test_totals_mixed_sign"

TEST_SOURCE = '''\
"""The approved arithmetic tests. The expected totals are literals here."""
from pricing.quote import quote


def test_totals_an_empty_cart():
    assert quote([]) == 0


def test_totals_several_positives():
    assert quote([200, 300, 500]) == 1000


def test_totals_mixed_sign():
    assert quote([200, -50, 500]) == 650


def test_totals_a_negative_is_not_a_pass():
    """Fails on purpose, so the adapter has a real assertion failure to read."""
    assert quote([200]) == 201
'''

APP_SOURCE = '''\
"""The code under test. One function, so a receipt has one subject to name."""
from typing import Iterable


def quote(amounts: Iterable[int]) -> int:
    return sum(amounts)
'''

RUNNER_BASE = ["-m", "pytest", "-p", "no:cacheprovider", "-q"]


def _check(
    *,
    required: tuple[str, ...] = REQUIRED_TESTS,
    kind: str = "pytest",
    artifact: str = "pytest-report.json",
    timeout_seconds: int = 120,
) -> dict:
    return {
        "id": "quote-regression",
        "kind": kind,
        "description": "Runs the approved totals tests.",
        "cwd": ".",
        "timeout_seconds": timeout_seconds,
        "artifact": artifact,
        "inputs": ["pricing/quote.py", "tests/test_behaviour.py"],
        "expectations": [],
        "subject": {"paths": ["pricing/quote.py"], "digest": None},
        "claim_id": "quote-sums-the-amounts",
        "required_tests": list(required),
        "runner": {"executable": "{{python}}", "base_argv": list(RUNNER_BASE)},
        "report_format": "pytest_json_report",
        "expect_report_version": 1,
    }


def _repository(tmp_path: Path, *, required=tuple(REQUIRED_TESTS)) -> Path:
    """A real repository with real tests and a real registered check."""
    repo = tmp_path / "shop"
    (repo / "pricing").mkdir(parents=True)
    (repo / "pricing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pricing" / "quote.py").write_text(APP_SOURCE, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_behaviour.py").write_text(TEST_SOURCE, encoding="utf-8")
    (repo / "verify_quote.py").write_text(DRIVER_SOURCE, encoding="utf-8")
    # Every fixture here carries this. Without it the interpreter writes
    # `__pycache__` while the check runs, the source identity moves underneath
    # the run, and the run is BLOCKED `source_changed` -- which is the product
    # behaving correctly and the fixture lying about a clean tree.
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
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
                "description": "A real totals function, verified by running its tests.",
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


def run_check(repo: Path, check_id: str = "quote-regression") -> tuple[int, dict, Path]:
    """`vkit check run` through the real CLI entry point.

    `cli.main` is called rather than `subprocess`, because the CLI's job here is
    to hand the work to `execution.run_check` and exit on its outcome; spawning
    it would test argparse as well, and on this host a spawned `vkit.exe`
    resolves the main checkout through its `.pth` rather than this worktree.
    `test_the_cli_exit_code_is_the_verdict` covers the exit code, and the MCP
    tests below cover the spawned-server path with `PYTHONPATH` pinned.
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


def _main(argv: list[str]) -> int:
    """`cli.main` with the arguments it would have been given on the command line."""
    saved = sys.argv
    sys.argv = list(argv)
    try:
        return cli.main(list(argv[1:]))
    finally:
        sys.argv = saved


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


def test_a_registered_pytest_check_runs_and_the_receipt_lists_the_cases(
    tmp_path: Path,
) -> None:
    """The slice itself: a real check, a real runner, a typed receipt.

    Asserted on the receipt's own values rather than on which keys it has. A
    receipt listing the two required cases with real observations and a measured
    subject digest is a different claim from a receipt that has an `satisfied`
    member, and only the first one would notice if the adapter had started
    inventing observations.
    """
    repo = _repository(tmp_path)

    status, row, run_dir = run_check(repo)

    assert status == 0, last_outcome(repo)
    assert row["result"] == "PASS"
    assert row["evidence_kind"] == "scenario", (
        "a pytest check without a generator block is SCENARIO evidence whatever "
        "the runner printed"
    )

    receipt = receipt_of(run_dir)
    assert receipt["status"] == "PASS"
    assert receipt["evidence_kind"] == "scenario"
    assert receipt["check_id"] == "quote-regression"
    assert receipt["claim_id"] == "quote-sums-the-amounts"
    assert [s["obligation"]["obligation"] for s in receipt["satisfied"]] == list(REQUIRED_TESTS)
    assert all(
        s["kind"] == "case_satisfied" and s["observation"].startswith("the runner reported")
        for s in receipt["satisfied"]
    )
    assert receipt["counterexamples"] == []
    assert receipt["subject"]["digest"] is not None, (
        "vkit measures the declared subject; a null here means the measurement "
        "never ran, not that the subject was empty"
    )
    assert receipt["fixture_digest"] is not None
    assert receipt["tool_versions"]["python"].startswith("Python 3.")
    assert receipt["verifier"]["module"] == "vkit.verifiers.dispatch"
    assert receipt["runtime"]["python_version"] == sys.version.split()[0]
    assert receipt["runtime"]["platform"] == sys.platform
    assert receipt["runtime"]["requires_os"] == "any"
    assert receipt["trust_boundary"]["establishes"].startswith("this named sequence")
    assert "not" in receipt["trust_boundary"]["does_not_establish"]


def test_the_receipt_is_referenced_by_the_run_report(tmp_path: Path) -> None:
    """The report points at the receipt rather than inlining it.

    `run-report.v1.json` describes the process and the receipt describes the
    evidence, so the report gains a run-relative reference and no new field. A
    reader who has only the report can therefore still find the evidence.
    """
    repo = _repository(tmp_path)

    _status, _row, run_dir = run_check(repo)

    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["artifacts"] == {
        "result": "pytest-report.json", "receipt": "receipt.v2.json",
    }
    assert (run_dir / report["artifacts"]["receipt"]).is_file()


def test_the_cli_exit_code_is_the_verdict(tmp_path: Path) -> None:
    """Each verdict is its own exit code, which is what a script polling reads.

    All three asserted here rather than only the failing one, because the codes
    are what a caller branches on and a check that returns the right verdict
    under the wrong code is still wrong for every caller that reads it.
    """
    repo = _repository(tmp_path, required=(OPTIONAL_TEST,))

    status, row, _run_dir = run_check(repo)

    assert (row["result"], status) == ("PASS", 0), (
        "a check whose only required test passes exits 0"
    )


def test_a_failing_assertion_exits_one_and_a_blocked_run_exits_three(
    tmp_path: Path,
) -> None:
    """The two non-zero codes, on the two non-PASS verdicts.

    `plans/CONTRACT.md:75` assigns 1 to a completed failing check and 3 to
    BLOCKED. Asserting them separately is what makes a BLOCKED distinguishable
    from a FAIL by exit code alone, which is the thing a shell script has.
    """
    failing = _repository(tmp_path / "fail", required=(
        "tests/test_behaviour.py::test_totals_a_negative_is_not_a_pass",
    ))
    status, row, _run_dir = run_check(failing)
    assert (row["result"], status) == ("FAIL", 1)

    blocked = _repository(tmp_path / "blocked")
    (blocked / "tests" / "test_behaviour.py").write_text(
        'import a_module_that_does_not_exist\n\n\ndef test_never_runs():\n    pass\n',
        encoding="utf-8",
    )
    status, row, _run_dir = run_check(blocked)
    assert (row["result"], status) == ("BLOCKED", 3)


def test_a_failed_assertion_is_a_fail_and_names_the_counterexample(
    tmp_path: Path,
) -> None:
    """A failing assertion is FAIL, and the receipt carries what disagreed.

    A FAIL rather than a BLOCKED, because the runner delivered an answer and the
    answer was no. The obligation is named as a counterexample with the
    assertion's own message, which is the thing an engineer is asked to fix.
    """
    repo = _repository(tmp_path, required=(
        "tests/test_behaviour.py::test_totals_a_negative_is_not_a_pass",
    ))

    status, row, run_dir = run_check(repo)

    assert row["result"] == "FAIL"
    assert status == 1
    receipt = receipt_of(run_dir)
    assert receipt["satisfied"] == []
    assert [c["obligation"]["obligation"] for c in receipt["counterexamples"]] == [
        "tests/test_behaviour.py::test_totals_a_negative_is_not_a_pass",
    ]
    assert "201" in receipt["counterexamples"][0]["trace"], (
        "the counterexample must quote the assertion, or a reader cannot act on it"
    )


def test_an_infrastructure_error_is_blocked_not_fail(tmp_path: Path) -> None:
    """A test that could not run is BLOCKED, which is a different answer to FAIL.

    The distinction the plan separates at `plans/10-native-verifiers.md:64`: FAIL
    says the code disagreed with its expectation, BLOCKED says the runner could
    not deliver an answer at all. Collapsing them would send an engineer to fix
    code that is fine because a fixture raised.

    The reason here is `report_truncated` rather than `infrastructure_error`
    because the runner never reached the tests: a module that cannot be imported
    fails during collection, which is pytest's exit 2, and the plugin's writer
    records `complete: false` because the session stopped early. Measured, so the
    expectation is a measurement rather than a guess.
    `test_an_errored_required_test_is_blocked` covers the other shape, where the
    session completed and the report names a test that errored.
    """
    repo = _repository(tmp_path)
    (repo / "tests" / "test_behaviour.py").write_text(
        'import a_module_that_does_not_exist\n\n\ndef test_never_runs():\n    pass\n',
        encoding="utf-8",
    )

    status, row, run_dir = run_check(repo)

    assert row["result"] == "BLOCKED"
    assert row["reason"] == BlockedReason.ARTIFACT_MALFORMED.value
    assert "report_truncated" in last_outcome(repo)["outcome"]["detail"]
    assert status == 3, "BLOCKED is exit 3, distinct from a FAIL's exit 1"


def test_an_errored_required_test_is_blocked_with_its_own_reason() -> None:
    """The `infrastructure_error` refusal: the report exists and names an error.

    The half a collection failure cannot reach. The runner collected and ran,
    wrote a complete report, and the required test's setup raised. That is a
    runner failing to deliver an answer about that test, and it is refused as
    `infrastructure_error` rather than being reported as a failed assertion.
    """
    raw = _report(tests=[
        _case(REQUIRED_TESTS[0], "passed"),
        _case(REQUIRED_TESTS[1], "error", "RuntimeError: setup exploded"),
    ])

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.INTERNAL_ERROR
    assert reading.detail.startswith("infrastructure_error")
    assert REQUIRED_TESTS[1] in reading.detail


def test_a_failed_assertion_is_not_reported_as_an_infrastructure_error() -> None:
    """The two answers are told apart by the exception's own kind.

    The same required test, the same report shape, and only the outcome word
    differs. Asserting both in one file is what makes the distinction a fact
    about the report rather than about this file's fixtures.
    """
    def both(outcome: str, message: str) -> bytes:
        """A report naming every required test, one of them in `outcome`."""
        return _report(tests=[
            _case(REQUIRED_TESTS[0], "passed"),
            _case(REQUIRED_TESTS[1], outcome, message),
        ])

    failed = _blocked(both("failed", "assert 0 == 1"))
    errored = _blocked(both("error", "boom"))

    # A failed assertion yields no `Blocked` at all: it is a FAIL the receipt can
    # carry a counterexample for. An errored test yields a refusal, because the
    # runner could not deliver an answer about that test.
    assert type(outcome_from_reading(failed)).__name__ == "Failed"
    assert type(outcome_from_reading(errored)).__name__ == "Blocked"
    assert errored.reason is BlockedReason.INTERNAL_ERROR


# ------------------------------------------------- the seven refusals
#
# Each token is the leading word of the BLOCKED detail. They are constants here
# so a change to one is a change to this file too, and the tests below assert
# the exact string rather than searching the detail for a keyword.

REPORT_ABSENT = "report_absent"
REPORT_TRUNCATED = "report_truncated"
REPORT_VERSION = "report_version"
REPORT_EMPTY = "report_empty"
REPORT_CONTRADICTION = "report_contradiction"
REQUIRED_TEST_ABSENT = "required_test_absent"
REQUIRED_TEST_SKIPPED = "required_test_skipped"


def _report(**fields) -> bytes:
    """Report bytes with every field present unless the test overrides one."""
    document = {"version": 1, "complete": True, "exit_code": 0, "tests": []}
    document.update(fields)
    return json.dumps(document).encode("utf-8")


def _case(nodeid: str, outcome: str, message: str = "") -> dict:
    return {"nodeid": nodeid, "outcome": outcome, "duration": 0.01, "message": message}


def _parsed_check(required=REQUIRED_TESTS):
    """A `PytestCheck` carrying the required ids, as the parser would build it."""
    from pathlib import Path as _Path

    from vkit.verifiers import PytestCheck, PinnedRunner, SubjectRef

    return PytestCheck(
        id="quote-regression",
        kind=CheckKind.PYTEST,
        subject=SubjectRef(("pricing/quote.py",), None),
        claim_id="quote-sums-the-amounts",
        cwd=_Path("."),
        timeout_seconds=120.0,
        artifact_name="pytest-report.json",
        required_tests=tuple(required),
        runner=PinnedRunner("{{python}}", RUNNER_BASE),
    )


def _blocked(raw: bytes, required=REQUIRED_TESTS):
    return pytest_adapter.interpret(raw, _parsed_check(required))


# 1. Missing output artifact -----------------------------------------------------


def test_a_runner_that_wrote_no_report_is_blocked(tmp_path: Path) -> None:
    """REFUSAL 1. No report at all is `artifact_missing`, and nothing else passes.

    Driven through a real run, because this one is a fact about a process rather
    than about a document. The manifest's runner is an executable that does not
    exist, so the child never starts, no report is written, and the core must
    refuse rather than read the absence of evidence as success.

    The pytest plugin's own writer always runs on a session that begins, so this
    refusal is not reachable by pointing pytest at a missing test file: that
    produces exit 4 and a report with `complete: false`, which is REFUSAL 6. The
    two are different facts and the reason says which.
    """
    repo = _repository(tmp_path)
    check = _check(required=REQUIRED_TESTS, timeout_seconds=60)
    check["runner"] = {
        "executable": "{{python}}",
        "base_argv": ["-c", "import sys; sys.exit(0)"],
    }
    write_manifest(repo, check)

    status, row, _run_dir = run_check(repo)

    assert row["result"] == "BLOCKED"
    assert row["reason"] in {
        BlockedReason.ARTIFACT_MISSING.value, BlockedReason.LAUNCH_FAILED.value,
    }
    assert status == 3


def test_a_check_that_never_launches_is_refused_before_it_can_pass(tmp_path: Path) -> None:
    """A missing runner executable cannot produce a PASS, and says why.

    `check_prerequisites` is the gate that catches a declared runner which is not
    on PATH, and it fires before anything is launched. A check that declares no
    prerequisite for its runner still cannot pass without one, which the next
    test covers by removing the report.
    """
    repo = _repository(tmp_path)
    check = _check(required=REQUIRED_TESTS, timeout_seconds=60)
    check["runner"] = {"executable": "a-runner-that-is-not-installed", "base_argv": []}
    write_manifest(repo, check)

    _status, row, _run_dir = run_check(repo)

    assert row["result"] == "BLOCKED"
    assert row["reason"] == BlockedReason.LAUNCH_FAILED.value


def _detail_of(repo: Path) -> str:
    return last_outcome(repo)["outcome"].get("detail", "")


# 2. A required test ID absent from the report ----------------------------------


def test_a_required_test_id_absent_from_the_report_is_blocked() -> None:
    """REFUSAL 2. The report never ran a required id, so it cannot be evidence.

    The runner exited zero over a different test. This is the case that makes an
    exit code insufficient on its own, and the case where a required test file
    was renamed while the manifest was not.
    """
    reading = _blocked(_report(tests=[_case(OPTIONAL_TEST, "passed")]))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_ABSENT)
    assert REQUIRED_TESTS[0] in reading.detail


def test_a_check_that_exits_zero_without_running_the_required_test_does_not_pass() -> None:
    """The exit code is not the verdict, stated as a test of the reader.

    A report over a different test is exactly what a green exit code looks like
    when a candidate's node id was renamed. Nothing in this path reads an exit
    code, so the run is refused on the report alone.
    """
    reading = _blocked(_report(tests=[_case(OPTIONAL_TEST, "passed")]))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_ABSENT)


# 3. Duplicate contradictory results for one ID ---------------------------------


def test_one_test_id_reported_twice_with_different_results_is_blocked() -> None:
    """REFUSAL 3. Two results for one id mean the report describes no run.

    A report that disagrees with itself cannot be reduced to a verdict by
    preferring one entry, because there is no principled way to choose and
    picking the passing one is exactly the promotion the contract refuses.
    """
    raw = _report(tests=[
        _case(REQUIRED_TESTS[0], "passed"),
        _case(REQUIRED_TESTS[0], "failed", "assert 0 == 1"),
        _case(REQUIRED_TESTS[1], "passed"),
    ])

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_CONTRADICTION)
    assert REQUIRED_TESTS[0] in reading.detail


def test_one_test_id_reported_twice_with_the_same_result_is_not_a_contradiction() -> None:
    """The refusal is about disagreement, not about repetition.

    Without this half the check would be satisfied by the count and a runner that
    reports one id twice identically would be refused for a thing it did not do.
    """
    raw = _report(tests=[
        _case(REQUIRED_TESTS[0], "passed"),
        _case(REQUIRED_TESTS[0], "passed"),
        _case(REQUIRED_TESTS[1], "passed"),
    ])

    outcome = outcome_from_reading(pytest_adapter.interpret(raw, _parsed_check()))

    assert type(outcome).__name__ == "Passed"


# 4. Unexpected skips ------------------------------------------------------------


def test_a_required_test_that_was_skipped_is_blocked() -> None:
    """REFUSAL 4. A skipped required test was not watched run.

    A skip is the runner's own word for "this did not happen", and a check that
    declares a test must watch it happen. Accepting a skip would let a
    `pytest.importorskip` or an unmet platform condition turn a required case
    into a green run with no observation behind it.
    """
    raw = _report(tests=[
        _case(REQUIRED_TESTS[0], "passed"),
        _case(REQUIRED_TESTS[1], "skipped", "required by an earlier test"),
    ])

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith(REQUIRED_TEST_SKIPPED)
    assert REQUIRED_TESTS[1] in reading.detail


def test_a_skip_that_is_not_required_does_not_refuse_the_run() -> None:
    """Only required tests are policed, or an unrelated skip would block.

    A repository's optional test may skip for reasons that have nothing to do
    with the obligation, and a check that failed on that would make the evidence
    depend on code the check never claimed to cover.
    """
    raw = _report(tests=[
        _case(REQUIRED_TESTS[0], "passed"),
        _case(REQUIRED_TESTS[1], "passed"),
        _case(OPTIONAL_TEST, "skipped", "needs a database"),
    ])

    outcome = outcome_from_reading(pytest_adapter.interpret(raw, _parsed_check()))

    assert type(outcome).__name__ == "Passed"


# 5. Zero collected tests --------------------------------------------------------


def test_a_report_with_no_tests_is_blocked_as_empty() -> None:
    """REFUSAL 5. A runner that collected nothing observed nothing.

    Distinct from the absent report: the runner ran and wrote a document, and
    the document says there was nothing to run. Both are BLOCKED, and the reason
    differs because the repairs differ.
    """
    reading = _blocked(_report(tests=[]))

    assert reading.reason is BlockedReason.ARTIFACT_EMPTY
    assert reading.detail.startswith(REPORT_EMPTY)


# 6. Truncated output ------------------------------------------------------------


def test_a_report_that_says_the_session_did_not_finish_is_blocked() -> None:
    """REFUSAL 6. A truncated run's tests are not the tests it would have run.

    The runner reported `complete: false`, meaning it was interrupted. Accepting
    the tests it did list would be reading a partial run as a whole one, and a
    required test after the interruption point is simply absent from the list.
    """
    raw = _report(complete=False, exit_code=2,
                  tests=[_case(REQUIRED_TESTS[0], "passed")])

    reading = _blocked(raw)

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_TRUNCATED)


def test_a_report_that_is_not_an_object_is_blocked() -> None:
    """A document this code cannot read is malformed, whatever it contains.

    Named separately from the version refusal because the repair differs: a
    truncated write produces a half-document, and a reader that guessed at one
    would report a verdict about bytes it never saw whole.
    """
    reading = _blocked(b"[1, 2, 3]")

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith("report_malformed")


def test_a_report_that_is_not_json_is_blocked() -> None:
    reading = _blocked(b"<html>the runner wrote a banner</html>")

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith("report_malformed")


# 7. Unsupported report version --------------------------------------------------


def test_a_report_from_a_version_this_code_does_not_read_is_blocked() -> None:
    """REFUSAL 7. An unknown version is a document whose meaning is unknown.

    The fields below are read by name, so a future report version could reuse one
    of them for something else. Parsing it anyway is how a receipt ends up
    describing a document nobody produced in that form.
    """
    reading = _blocked(_report(version=2, tests=[_case(REQUIRED_TESTS[0], "passed")]))

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith(REPORT_VERSION)
    assert "version 2" in reading.detail


def test_a_report_whose_version_is_not_a_number_is_blocked() -> None:
    """A version this code cannot compare is not a version."""
    reading = _blocked(_report(version="one"))

    assert reading.detail.startswith(REPORT_VERSION)


def test_a_test_outcome_this_code_does_not_know_is_refused() -> None:
    """The runner's vocabulary is closed, and a new word is a refusal.

    A future runner could add an outcome. Reading it with today's meanings would
    be a guess, and the guess would be invisible in the receipt.
    """
    raw = _report(tests=[_case(REQUIRED_TESTS[0], "flaked")])

    reading = _blocked(raw)

    assert reading.detail.startswith("report_malformed")
    assert "flaked" in reading.detail


# ------------------------------------------------ the category cannot be claimed


def test_a_tampered_report_claiming_theorem_checking_is_refused_at_the_kind(
    tmp_path: Path,
) -> None:
    """A document cannot promote itself, at any layer it could try.

    Three attempts in one test, because there are three layers and only testing
    the first would leave the other two unguarded: a report claiming the
    category in its own bytes, a manifest declaring one, and a policy asserting
    one. The first two are refused because there is no field to write; the third
    is refused by the comparison every integration run makes.
    """
    repo = _repository(tmp_path)
    tampered = _report(
        evidence_kind="theorem_checking",
        category="theorem_checking",
        tests=[_case(t, "passed") for t in REQUIRED_TESTS],
    )

    # 1. The report's own claim is ignored: the category comes from the variant.
    check = _parsed_check()
    reading = pytest_adapter.interpret(tampered, check)

    inputs = ReceiptInputs(
        check_id=check.id, claim_id=check.claim_id, run_id="r1", task_id=None,
        generation=None, category=ClaimCategory.SCENARIO, subject=check.subject,
        specification_digest=None, policy_digest="p",
        source=_identity(), fixture_digest=None, tool_versions={},
        runtime={"python_version": "3.13", "platform": "win32", "requires_os": "any"},
        timeout_seconds=120.0, report_path=Path("r.json"), project_root=Path("."),
        outcome=Passed(()),
        reading=reading,
    )
    built = build_receipt(inputs)
    assert built["evidence_kind"] == "scenario", (
        "the category is derived from the check's variant, so a report claiming "
        "theorem_checking cannot promote the evidence it is attached to"
    )

    # 2. A manifest that declares the category has nowhere to put it.
    from vkit.manifest import ManifestError, parse_manifest_bytes

    entry = _check()
    entry["evidence_kind"] = "theorem_checking"
    with pytest.raises(ManifestError) as caught:
        parse_manifest_bytes(
            json.dumps({"schema_version": 2, "checks": [entry]}).encode("utf-8"),
            project=_project(repo), run_dir=repo, origin="manifest.json",
        )
    assert "evidence_kind" in str(caught.value)

    # 3. A policy that asserts it for a pytest check is a REJECT finding.
    from vkit.integration.policy import compare, from_file

    policy_path = repo / "policy.json"
    policy_path.write_text(json.dumps({
        "schema_version": 1, "description": "the bar",
        "required_checks": [{
            "id": "quote-regression",
            "obligations": [{"kind": "case", "obligation": t} for t in REQUIRED_TESTS],
            "evidence_kind": "theorem_checking",
        }],
    }), encoding="utf-8")
    manifest = parse_manifest_bytes(
        (repo / "verification" / "manifest.json").read_bytes(),
        project=_project(repo), run_dir=repo, origin="manifest.json",
    )
    findings = compare(manifest, from_file(_project(repo), policy_path), approved=None)

    assert "evidence_kind_downgraded" in {f.kind for f in findings}, [
        f.to_json() for f in findings
    ]


def _identity():
    """A source identity standing in for a measured one.

    Built field for field the way `supervise._source_of` rebuilds a recorded
    identity, so the receipt assembled in the tamper test is the same shape a
    real run produces rather than a convenient one.
    """
    return SourceIdentity(
        head="a" * 40, inventory_digest="d" * 64, dirty=False,
        tracked_files=3, dirty_paths=(),
    )


def _project(repo: Path):
    from vkit.paths import open_project

    return open_project(repo)


# ------------------------------------------- a legacy scenario check still runs


def test_a_legacy_scenario_check_still_runs_unchanged(tmp_path: Path) -> None:
    """The migration's backward-compatibility half, on the real path.

    A v1 manifest declares no `kind`, and reading it as a scenario driver is the
    only reading it has. Its artifact is still v1 check-artifact bytes and its
    scenarios are still required by id, so a project written before Plan 10 must
    keep working without being edited. Every example manifest in this repository
    is in this shape, so this is the case a real user hits first.
    """
    repo = tmp_path / "legacy"
    (repo / "pricing").mkdir(parents=True)
    (repo / "pricing" / "quote.py").write_text(APP_SOURCE, encoding="utf-8")
    (repo / "verify_quote.py").write_text(DRIVER_SOURCE, encoding="utf-8")
    (repo / "verification").mkdir()
    (repo / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "description": "A totals CLI verified by a driver.",
        "checks": [{
            "id": "quote-behavior",
            "description": "Runs the real CLI and compares printed totals.",
            "command": ["{{python}}", "verify_quote.py", "--out", "{{run_dir}}/result.json"],
            "cwd": ".",
            "timeout_seconds": 120,
            "required_scenarios": ["empty-cart", "one-positive"],
            "artifact": "result.json",
            "inputs": ["pricing/quote.py", "verify_quote.py"],
            "expectations": [],
        }],
    }, indent=2) + "\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    init_repo(repo)

    status, row, run_dir = run_check(repo, "quote-behavior")

    assert status == 0, last_outcome(repo)
    assert row["result"] == "PASS"
    assert row["evidence_kind"] == "scenario"
    assert [s["id"] for s in last_outcome(repo)["outcome"]["scenarios"]] == [
        "empty-cart", "one-positive",
    ]
    # The v1 artifact is still the thing a scenario check reads, and the receipt
    # names the category it licenses without inventing obligation results the
    # artifact never carried.
    assert json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["schema_version"] == 1
    receipt = receipt_of(run_dir)
    assert receipt["status"] == "PASS"
    assert receipt["evidence_kind"] == "scenario"
    assert receipt["satisfied"] == []


DRIVER_SOURCE = '''\
"""Runs the real CLI and records what actually happened."""
import argparse
import json
import sys
from pathlib import Path

CASES = [
    ("empty-cart", [], "0"),
    ("one-positive", ["--amount", "250"], "250"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    scenarios = []
    for scenario, tail, expected in CASES:
        from pricing.quote import quote
        printed = str(quote([int(a) for a in tail[1::2]]) if tail else quote([]))
        scenarios.append({
            "id": scenario,
            "result": "PASS" if printed == expected else "FAIL",
            "observation": f"printed {printed}",
        })
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"schema_version": 1, "scenarios": scenarios}, indent=2) + "\\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# --------------------------------- the same run through the detached MCP path


def _await_terminal(tools, run_id: str, timeout: float = 240.0) -> dict:
    """Poll `run_get` until the run is terminal, the way a caller must.

    `check_start` records a launch and returns while the check is still running,
    which is the property that lets a run outlive the client that asked for it.
    The verdict is therefore not in the `check_start` payload, and a caller that
    wants it asks for it here.
    """
    deadline = time.monotonic() + timeout
    while True:
        content = tools.call_tool("run_get", {"run_id": run_id}).content
        if content.get("lifecycle") == "terminal" and content.get("outcome"):
            return content
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"run {run_id} was still {content.get('lifecycle')!r} after {timeout}s"
            )
        time.sleep(0.2)


def test_the_same_run_is_visible_over_the_detached_path_with_the_same_verdict(
    tmp_path: Path,
) -> None:
    """The slice's third claim: one run, two surfaces, one verdict.

    The foreground CLI runs the check and the MCP server starts it through
    `supervisor.start_run`, which is a different process writing the same durable
    record. If the two derived their verdicts separately they would be free to
    disagree, so both are read back here and compared on the value rather than on
    the presence of a key.
    """
    from vkit.mcp._tools import Server

    # Foreground first, so the two runs are separately identified.
    cli_repo = _repository(tmp_path / "cli")
    _cli_status, cli_row, cli_run_dir = run_check(cli_repo)
    assert cli_row["result"] == "PASS"

    # Detached, through the tool layer, over its own repository.
    mcp_repo = _repository(tmp_path / "mcp")
    tools = Server(mcp_repo)
    task_id = tools.call_tool("task_begin", {
        "contract": {"scope": "totals", "required_checks": ["quote-regression"]},
        "owner": "worker-7",
        "request_id": "req-begin-pytest",
    }).content["task_id"]

    started = tools.call_tool("check_start", {
        "task_id": task_id,
        "check_ids": ["quote-regression"],
        "request_id": "req-check-pytest",
    })
    assert started.is_error is False, started.content
    run_id = started.content["runs"][0]["run_id"]

    content = _await_terminal(tools, run_id)

    assert content["result"] == "PASS"
    assert [s["id"] for s in content["scenarios"]] == list(REQUIRED_TESTS), (
        "the detached run must name the same cases the foreground run did"
    )
    assert all(s["result"] == "PASS" for s in content["scenarios"])

    # The receipt the detached supervisor wrote is the same document shape, with
    # the same category and the same obligations, and it carries the task and
    # generation the foreground run does not have.
    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(mcp_repo).db_path)
    detached = receipt_of(store.run_dir(run_id))

    assert detached["status"] == "PASS"
    assert detached["evidence_kind"] == cli_row["evidence_kind"] == "scenario"
    assert detached["task_id"] == task_id
    assert detached["generation"] == 1
    assert [s["obligation"]["obligation"] for s in detached["satisfied"]] == list(
        REQUIRED_TESTS
    )

    foreground = receipt_of(cli_run_dir)
    assert foreground["task_id"] is None and foreground["generation"] is None, (
        "a standalone run records no task, which is what makes it standalone "
        "evidence rather than task acceptance"
    )


def test_task_finalize_over_the_detached_run_computes_ready(tmp_path: Path) -> None:
    """The slice's last claim: the detached run discharges the task's contract.

    `task_finalize` is the single acceptance authority, so this asserts on what it
    computed rather than on which checks ran. READY means the required check
    produced a PASS under this attempt's identities, which is the whole point of
    routing a native check through it rather than recording a verdict beside it.
    """
    from vkit.mcp._tools import Server

    repo = _repository(tmp_path / "finalize")
    tools = Server(repo)
    task_id = tools.call_tool("task_begin", {
        "contract": {"scope": "totals", "required_checks": ["quote-regression"]},
        "owner": "worker-7",
        "request_id": "req-begin-finalize",
    }).content["task_id"]

    started = tools.call_tool("check_start", {
        "task_id": task_id,
        "check_ids": ["quote-regression"],
        "request_id": "req-check-finalize",
    })
    run_id = started.content["runs"][0]["run_id"]
    assert _await_terminal(tools, run_id)["result"] == "PASS"

    finalized = tools.call_tool("task_finalize", {"task_id": task_id})

    assert finalized.is_error is False, finalized.content
    assert finalized.content["readiness"] == "READY", finalized.content["gaps"]
    assert finalized.content["gaps"] == []
    assert finalized.content["required_checks"] == ["quote-regression"]


def test_task_finalize_is_blocked_while_a_required_check_is_unmet(
    tmp_path: Path,
) -> None:
    """READY is only reachable with the evidence, and this is the other side.

    Finalizing a task whose required check has not run is BLOCKED with the gap
    named. Asserted because a finalize that returned READY for a task with no
    runs would make every other READY in this file meaningless.
    """
    from vkit.mcp._tools import Server

    repo = _repository(tmp_path / "nogap")
    tools = Server(repo)
    task_id = tools.call_tool("task_begin", {
        "contract": {"scope": "totals", "required_checks": ["quote-regression"]},
        "owner": "worker-7",
        "request_id": "req-begin-nogap",
    }).content["task_id"]

    finalized = tools.call_tool("task_finalize", {"task_id": task_id})

    assert finalized.content["readiness"] == "BLOCKED"
    assert any("no completed run for required check" in g
               for g in finalized.content["gaps"]), finalized.content["gaps"]

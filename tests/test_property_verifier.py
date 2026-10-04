"""One approved property check, end to end, and the proof that the category is not the package.

The half of checkpoint 10.2 that is about classification rather than about
running anything. Two claims, and the second is the one a reader would otherwise
have to take on trust.

**The category comes from the obligation.** `docs/verification.md`
says to classify approved Hypothesis tests as `property` when the registered
obligation says so, and explicitly not to infer it from a package being
installed. The proof is one test: the same file, the same test, the same runner,
declared twice, once as `pytest` and once as `property`. The pytest declaration
over the identical bytes is SCENARIO. That is the whole contract in one
comparison, and no assertion on the property path alone would establish it.

**The receipt says what was sampled, and does not say it was exhausted.**
Hypothesis draws a bounded family of inputs. `claimkind.py` draws the same
distinction in prose, and the two must agree: a receipt claiming more than the
generator tried is the overstatement this category exists to prevent.

Every case that runs Hypothesis is skipped with a stated reason when Hypothesis
is absent, for the reason the Node file gives: a green test that never ran reads
as satisfied.
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
    HypothesisSettings,
    PropertyCheck,
    ReplaySettings,
    SubjectRef,
    property_adapter,
    pytest_adapter,
)

def _hypothesis_available() -> bool:
    """Whether the interpreter that runs this suite can import Hypothesis.

    Probed by importing rather than by looking for a distribution on disk,
    because the thing a property check needs is an importable module in the child
    and a package that is installed but broken is not that.
    """
    return subproc.run(
        [sys.executable, "-c", "import hypothesis"],
        capture_output=True, check=False,
    ).returncode == 0


HAS_HYPOTHESIS = _hypothesis_available()

requires_hypothesis = pytest.mark.skipif(
    not HAS_HYPOTHESIS,
    reason="requires an importable hypothesis for the child pytest run",
)

#: The two required property tests the fixture declares. They disagree with a
#: reference model, which is what `claimkind.py` describes PROPERTY evidence as.
REQUIRED_TESTS = (
    "tests/test_property.py::test_quote_agrees_with_the_reference_model",
    "tests/test_property.py::test_quote_of_one_amount_is_that_amount",
)
OPTIONAL_TEST = "tests/test_property.py::test_quote_is_additive"
FAILING_TEST = "tests/test_property.py::test_quote_is_not_disagreeing"
CRASHING_TEST = "tests/test_property.py::test_quote_of_a_missing_amount"
SKIPPED_TEST = "tests/test_property.py::test_quote_over_large_amounts"

APP_SOURCE = '''\
"""The code under test, and a separate model of what it should return.

The two differ mechanically on purpose. A property check that compared the code
against itself would agree with every bug it contained, so the model here is a
separate loop over the same inputs rather than a second name for the first.
"""


def quote(amounts):
    return sum(amounts)


def model_quote(amounts):
    total = 0
    for amount in amounts:
        total += amount
    return total
'''

TEST_SOURCE = '''\
"""The approved property tests. Every input here is generated, not written down."""
from hypothesis import given, strategies as st

from shop.pricing import model_quote, quote


@given(st.lists(st.integers(min_value=-10_000, max_value=10_000), max_size=40))
def test_quote_agrees_with_the_reference_model(amounts):
    assert quote(amounts) == model_quote(amounts)


@given(st.integers(min_value=-10_000, max_value=10_000))
def test_quote_of_one_amount_is_that_amount(amount):
    assert quote([amount]) == amount


@given(st.lists(st.integers(min_value=0, max_value=1_000), min_size=2, max_size=8))
def test_quote_is_additive(amounts):
    assert quote(amounts) == sum(amounts)


def test_quote_is_not_disagreeing():
    """Fails on purpose, so the adapter has a real assertion to read."""
    assert quote([2, 3]) == 6


def test_quote_of_a_missing_amount():
    """Raises rather than asserting, so the adapter has an error to tell apart."""
    quote(None).bit_length()


@given(st.integers(min_value=10**9))
def test_quote_over_large_amounts(amount):
    assert quote([amount]) == amount
'''

#: What the fixture's manifest pins. 25 rather than Hypothesis's default of 100
#: precisely so a receipt that read the library default instead of the pinned
#: declaration would be visibly wrong.
PINNED_EXAMPLES = 25
PINNED_HEALTH_CHECKS = ("too_slow", "data_too_large")

RUNNER_BASE = ["-m", "pytest", "-p", "no:cacheprovider", "-q"]


def _check(*, kind: str = "property", required=REQUIRED_TESTS,
           max_examples: int = PINNED_EXAMPLES) -> dict:
    """One manifest entry, in either of the two kinds that can run these tests.

    `kind` is the only thing that differs between the two comparisons this file
    makes. Everything else, including the file the tests live in and the runner
    that executes them, is held constant on purpose.
    """
    entry = {
        "id": "quote-property",
        "kind": kind,
        "description": "Runs the approved tests and requires each to run and pass.",
        "cwd": ".",
        "timeout_seconds": 180,
        "artifact": "property-report.json",
        "inputs": ["shop/pricing.py", "tests/test_property.py"],
        "expectations": [],
        "subject": {"paths": ["shop/pricing.py"], "digest": None},
        "claim_id": "quote-agrees-with-its-model",
        "required_tests": list(required),
        "runner": {"executable": "{{python}}", "base_argv": list(RUNNER_BASE)},
        "report_format": "pytest_json_report",
        "expect_report_version": 1,
    }
    if kind == "property":
        entry["generator"] = {
            "max_examples": max_examples,
            "stateful_step_count": 0,
            "deadline": None,
            "suppress_health_check": list(PINNED_HEALTH_CHECKS),
        }
        entry["replay"] = {"database": ".hypothesis/examples", "seed": None}
    return entry


def _repository(tmp_path: Path, *, kind: str = "property",
                required=REQUIRED_TESTS) -> Path:
    """A real repository with real Hypothesis tests and a real registered check."""
    repo = tmp_path / "shop"
    (repo / "shop").mkdir(parents=True)
    (repo / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "shop" / "pricing.py").write_text(APP_SOURCE, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_property.py").write_text(TEST_SOURCE, encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".hypothesis/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    write_manifest(repo, _check(kind=kind, required=required))
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
                "description": "A quote function, verified over generated inputs.",
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


def run_check(repo: Path, check_id: str = "quote-property") -> tuple[int, dict, Path]:
    """`vkit check run` through the real CLI entry point.

    `cli.main` rather than `subprocess`, for the reason
    `test_pytest_verifier.run_check` gives: on this host a spawned `vkit.exe`
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




@requires_hypothesis
def test_an_approved_property_check_runs_and_is_categorised_property(
    tmp_path: Path,
) -> None:
    """The slice itself: a real check, a real generator, a typed receipt.

    Asserted on the receipt's own values. `evidence_kind` is the field the whole
    checkpoint turns on, and the satisfied obligations are the field that shows
    the run watched the two cases the manifest named.
    """
    repo = _repository(tmp_path)

    status, row, run_dir = run_check(repo)

    assert status == 0, last_outcome(repo)
    assert row["result"] == "PASS"
    assert row["evidence_kind"] == "property"

    receipt = receipt_of(run_dir)
    assert receipt["status"] == "PASS"
    assert receipt["evidence_kind"] == "property"
    assert receipt["check_id"] == "quote-property"
    assert [s["obligation"]["obligation"] for s in receipt["satisfied"]] == list(REQUIRED_TESTS)
    assert receipt["counterexamples"] == []
    assert receipt["subject"]["digest"] is not None


@requires_hypothesis
def test_the_receipt_records_the_generator_settings_that_actually_ran(
    tmp_path: Path,
) -> None:
    """The pinned family, measured off the run rather than copied from the manifest.

    `max_examples` is pinned to 25 in the manifest, which is not Hypothesis's
    default of 100 precisely so this assertion can tell the two apart. The number
    in the receipt is read back out of the test object during the run, so a
    receipt that quoted the declaration rather than the measurement would say 25
    here and a receipt that read the library default would say 100. Only a
    measurement says 25.

    The measured value is 25 either way when nothing moved the settings, so the
    assertion also pins the value itself: the sentence a reader reads must name
    the family the owner approved.
    """
    repo = _repository(tmp_path)

    _status, _row, run_dir = run_check(repo)

    assumptions = receipt_of(run_dir)["assumptions"]
    settings_line = next(
        line for line in assumptions if line.startswith("hypothesis generator settings")
    )
    assert f"max_examples={PINNED_EXAMPLES}" in settings_line, settings_line
    assert "deadline=none" in settings_line, settings_line
    for name in PINNED_HEALTH_CHECKS:
        assert name in settings_line, (
            f"{name} was pinned in the manifest and is absent from the receipt's "
            f"settings line: {settings_line}"
        )
    assert "max_examples=100" not in "\n".join(assumptions), (
        "100 is Hypothesis's own default. A receipt carrying it means the pinned "
        "profile never loaded and the run sampled a family nobody approved"
    )


@requires_hypothesis
def test_the_receipt_records_where_a_failing_sequence_is_retained(
    tmp_path: Path,
) -> None:
    """Replay information is recorded, including that no seed was pinned.

    A property result with no way to reproduce its counterexample is a weaker
    claim than one with a way, and the manifest's `replay.seed` is explicitly
    nullable. The honest reading of a null seed is that the generator chose and
    the choice was not pinned, which is a different sentence from omitting the
    field.
    """
    repo = _repository(tmp_path)

    _status, _row, run_dir = run_check(repo)

    replay_line = next(
        line for line in receipt_of(run_dir)["assumptions"] if line.startswith("replay:")
    )
    assert ".hypothesis/examples" in replay_line, replay_line
    assert "not pinned" in replay_line, replay_line


@requires_hypothesis
def test_a_property_result_does_not_claim_to_have_exhausted_its_inputs(
    tmp_path: Path,
) -> None:
    """The receipt states the limit, on every observation and not only once.

    Two places, because a reader reaches both. `assumptions` is where a person
    looks before trusting a verdict, and each observation is where a reader
    arrives having read one line. An observation reading only "the runner
    reported passed" is the sentence `claimkind.py:20-22` exists to prevent, so
    the scope rides on the observation itself.

    Asserted on the words, not on the presence of a field: a scope key with a
    confident sentence in it would satisfy a presence check and fail this.
    """
    repo = _repository(tmp_path)

    _status, _row, run_dir = run_check(repo)

    receipt = receipt_of(run_dir)
    scope_lines = [line for line in receipt["assumptions"] if "sampled" in line]
    assert scope_lines, receipt["assumptions"]
    assert "rather than exhausting it" in scope_lines[0], scope_lines[0]
    assert "not over every possible input" in scope_lines[0], scope_lines[0]
    for satisfied in receipt["satisfied"]:
        assert "Tested scope:" in satisfied["observation"], satisfied
        assert "rather than exhausting it" in satisfied["observation"], satisfied


@requires_hypothesis
def test_the_category_does_not_establish_implementation_equivalence(
    tmp_path: Path,
) -> None:
    """The trust boundary says what this category is not.

    Copied from `ClaimCategory`, so this is a test that the receipt carries the
    category's own limit rather than a paraphrase of it. The phrase that matters
    is "implementation equivalence", because that is the claim a reader is most
    likely to make from a green property run.
    """
    repo = _repository(tmp_path)

    _status, _row, run_dir = run_check(repo)

    boundary = receipt_of(run_dir)["trust_boundary"]
    assert "reference model" in boundary["establishes"], boundary
    assert "implementation equivalence" in boundary["does_not_establish"], boundary
    assert "sampled, not exhausted" in boundary["does_not_establish"], boundary




@requires_hypothesis
def test_the_same_file_declared_as_pytest_yields_scenario(tmp_path: Path) -> None:
    """The proof that the category is the obligation's, not the package's.

    One repository, one test file, one runner. The only difference between the two
    runs is the `kind` key in the manifest, and the category changes with it while
    every case passes in both. Hypothesis is installed and imported by the child
    in both runs, so nothing about the environment distinguishes them.

    This is the comparison `docs/verification.md` requires and the one
    no assertion on the property path alone could substitute for: without the
    pytest run, a reader could not tell whether `property` came from the declared
    generator block or from Hypothesis happening to be importable.
    """
    as_property = _repository(tmp_path / "property")
    status, property_row, property_receipt = run_check(as_property)

    as_pytest = _repository(tmp_path / "pytest", kind="pytest")
    pytest_status, pytest_row, pytest_receipt = run_check(as_pytest)

    assert (status, property_row["result"]) == (0, "PASS"), last_outcome(as_property)
    assert (pytest_status, pytest_row["result"]) == (0, "PASS"), last_outcome(as_pytest)
    assert receipt_of(property_receipt)["evidence_kind"] == "property"
    assert receipt_of(pytest_receipt)["evidence_kind"] == "scenario"
    assert property_row["evidence_kind"] == "property"
    assert pytest_row["evidence_kind"] == "scenario"

    property_cases = [s["obligation"]["obligation"] for s in receipt_of(property_receipt)["satisfied"]]
    pytest_cases = [s["obligation"]["obligation"] for s in receipt_of(pytest_receipt)["satisfied"]]
    assert property_cases == pytest_cases == list(REQUIRED_TESTS)


@requires_hypothesis
def test_the_two_kinds_record_different_things_about_the_same_run(
    tmp_path: Path,
) -> None:
    """The same run read two ways says two different things, which is the point.

    The pytest receipt records the case outcomes and nothing about sampling. The
    property receipt records the generator settings, the replay information and
    the scope. A reader holding both cannot confuse one for the other, which is
    what a category that never reaches the reader is worth.
    """
    as_property = _repository(tmp_path / "property")
    run_check(as_property)
    as_pytest = _repository(tmp_path / "pytest", kind="pytest")
    run_check(as_pytest)

    from vkit.paths import open_project
    from vkit.storage import Store

    def latest_assumptions(repo: Path) -> list[str]:
        store = Store(open_project(repo).db_path)
        run_id = store.list_runs(limit=1)[0]["run_id"]
        return receipt_of(store.run_dir(run_id))["assumptions"]

    property_assumptions = latest_assumptions(as_property)
    pytest_assumptions = latest_assumptions(as_pytest)

    assert any(line.startswith("hypothesis generator settings") for line in property_assumptions)
    assert not any(line.startswith("hypothesis generator settings") for line in pytest_assumptions)
    assert any("sampled" in line for line in property_assumptions)
    assert not any("sampled" in line for line in pytest_assumptions)


def test_evidence_kind_is_derived_from_the_variant_and_nothing_else() -> None:
    """The function that decides the category is total and takes one argument.

    Asserted without running anything, because this is the structural claim: the
    category comes from which variant a check is, and the parser is the only
    thing that picks the variant. A `PytestCheck` is SCENARIO whatever its
    fields say, and a `PropertyCheck` is PROPERTY whatever its `kind` field says,
    which is a stronger guarantee than reading the field: a caller holding a
    variant cannot construct one that disagrees with itself.

    A reader who trusted only this file's integration tests would be trusting an
    implementation detail without this.
    """
    from vkit.verifiers import evidence_kind
    from vkit.verifiers.spec import PropertyCheck as PC
    from vkit.verifiers.spec import PytestCheck as PY

    def common() -> dict:
        return {
            "id": "c", "subject": SubjectRef(("a.py",), None), "claim_id": "c",
            "cwd": Path("."), "timeout_seconds": 60.0, "artifact_name": "r.json",
            "required_tests": ("t.py::t",),
        }

    pinned = HypothesisSettings(
        max_examples=25, stateful_step_count=0, deadline=None,
        suppress_health_check=(),
    )

    assert evidence_kind(PC(**common(), kind=CheckKind.PROPERTY, generator=pinned)).value == "property"
    assert evidence_kind(PY(**common(), kind=CheckKind.PYTEST)).value == "scenario"
    assert evidence_kind(PC(**common(), kind=CheckKind.PYTEST, generator=pinned)).value == "property", (
        "the category follows which variant this is, so a kind field that "
        "disagrees with the variant cannot demote the evidence"
    )




def _report(**fields) -> bytes:
    """Report bytes with every field present unless the test overrides one."""
    document = {"version": 1, "complete": True, "exit_code": 0, "tests": []}
    document.update(fields)
    return json.dumps(document).encode("utf-8")


def _case(nodeid: str, outcome: str, generator=None, message: str = "") -> dict:
    return {
        "nodeid": nodeid, "outcome": outcome, "duration": 0.01,
        "message": message, "generator": generator,
    }


def _settings(max_examples: int = PINNED_EXAMPLES, **overrides) -> dict:
    """The generator block the plugin writes for a Hypothesis test."""
    document = {
        "max_examples": max_examples,
        "stateful_step_count": 0,
        "deadline": 0.2,
        "suppress_health_check": [],
        "database": "_StorageDirectoryDatabase",
    }
    document.update(overrides)
    return document


def _parsed_check(required=REQUIRED_TESTS, *, generator=Ellipsis) -> PropertyCheck:
    """A `PropertyCheck` carrying the required ids, as the parser would build it."""
    if generator is Ellipsis:
        generator = HypothesisSettings(
            max_examples=PINNED_EXAMPLES, stateful_step_count=0, deadline=None,
            suppress_health_check=PINNED_HEALTH_CHECKS,
        )
    return PropertyCheck(
        id="quote-property",
        kind=CheckKind.PROPERTY,
        subject=SubjectRef(("shop/pricing.py",), None),
        claim_id="quote-agrees-with-its-model",
        cwd=Path("."),
        timeout_seconds=180.0,
        artifact_name="property-report.json",
        required_tests=tuple(required),
        generator=generator,
        replay=ReplaySettings(".hypothesis/examples", None),
    )


def _blocked(raw: bytes, required=REQUIRED_TESTS, check=None, generator=Ellipsis):
    parsed = check or _parsed_check(required, generator=generator)
    return property_adapter.interpret(raw, parsed)




@requires_hypothesis
def test_a_property_run_that_wrote_no_report_is_blocked(tmp_path: Path) -> None:
    """REFUSAL 1. No report at all is `artifact_missing`.

    The runner is an executable that does not exist, so the child never starts
    and nothing is written. Driven through a real run because this one is a fact
    about a process rather than about a document.
    """
    repo = _repository(tmp_path)
    check = _check(timeout_seconds=180) if False else _check()
    check["runner"] = {"executable": "a-runner-that-is-not-installed", "base_argv": []}
    write_manifest(repo, check)

    status, row, _run_dir = run_check(repo)

    assert row["result"] == "BLOCKED"
    assert row["reason"] in {
        BlockedReason.ARTIFACT_MISSING.value, BlockedReason.LAUNCH_FAILED.value,
    }
    assert status == 3




def test_a_property_required_test_absent_from_the_report_is_blocked() -> None:
    """REFUSAL 2. The report never ran a required test.

    Same token as the pytest adapter's, because it is the same fact about the
    same kind of report. A second vocabulary for it would be a second dialect of
    one language.
    """
    reading = _blocked(_report(tests=[
        _case(REQUIRED_TESTS[0], "passed", _settings()),
    ]))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith("required_test_absent")
    assert REQUIRED_TESTS[1] in reading.detail




def test_one_property_test_reported_twice_with_different_results_is_blocked() -> None:
    """REFUSAL 3. Two results for one id mean the report describes no run."""
    reading = _blocked(_report(tests=[
        _case(REQUIRED_TESTS[0], "passed", _settings()),
        _case(REQUIRED_TESTS[0], "failed", _settings(), "assert 0 == 1"),
        _case(REQUIRED_TESTS[1], "passed", _settings()),
    ]))

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith("report_contradiction")
    assert REQUIRED_TESTS[0] in reading.detail




def test_a_required_property_test_that_was_skipped_is_blocked() -> None:
    """REFUSAL 4. A skipped required test was not watched run.

    A generator that never ran is not a sampled family, so accepting the skip
    would let a hypothesis test turned off by a marker report a sampled scope it
    never established.
    """
    reading = _blocked(_report(tests=[
        _case(REQUIRED_TESTS[0], "passed", _settings()),
        _case(REQUIRED_TESTS[1], "skipped", _settings(), "required by an earlier test"),
    ]))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith("required_test_skipped")
    assert REQUIRED_TESTS[1] in reading.detail




def test_a_property_report_with_no_tests_is_blocked_as_empty() -> None:
    """REFUSAL 5. A runner that collected nothing observed nothing."""
    reading = _blocked(_report(tests=[]))

    assert reading.reason is BlockedReason.ARTIFACT_EMPTY
    assert reading.detail.startswith("report_empty")




def test_a_property_report_that_did_not_finish_is_blocked() -> None:
    """REFUSAL 6. A truncated run's tests are not the tests it would have run.

    Reported as `complete: false`, meaning the session was interrupted. The
    generator settings of the tests it did list say nothing about the tests after
    the interruption, so the scope they imply would be a scope the run did not
    reach.
    """
    reading = _blocked(_report(complete=False, exit_code=2, tests=[
        _case(REQUIRED_TESTS[0], "passed", _settings()),
    ]))

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith("report_truncated")




def test_a_property_report_from_a_version_this_code_does_not_read_is_blocked() -> None:
    """REFUSAL 7. An unknown version is a document whose meaning is unknown."""
    reading = _blocked(_report(version=2, tests=[
        _case(REQUIRED_TESTS[0], "passed", _settings()),
    ]))

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith("report_version")
    assert "version 2" in reading.detail




def test_a_property_test_that_reported_no_generator_is_blocked() -> None:
    """The one refusal `pytest_adapter` does not make, and it is this category's.

    A `property` check whose required tests reported no generator settings ran no
    generator this build can read, so the family it sampled is unknown. Recording
    a scope for it would be inventing the one number that makes a property result
    honest, and a receipt doing so would be the overstatement `claimkind.py`
    exists to prevent. A `pytest` run over the same file has no such refusal,
    because it makes no claim about sampling.
    """
    reading = _blocked(_report(tests=[
        _case(REQUIRED_TESTS[0], "passed", generator=None),
        _case(REQUIRED_TESTS[1], "passed", generator=None),
    ]))

    assert reading.reason is BlockedReason.SCENARIO_UNKNOWN
    assert reading.detail.startswith("generator_absent")
    for test_id in REQUIRED_TESTS:
        assert test_id in reading.detail


def test_the_same_generator_less_run_is_accepted_as_a_pytest_check() -> None:
    """The counterpart, and it is what makes the refusal specific.

    The report is byte-identical to the one refused above. Declared as `pytest`,
    it is a PASS, because nothing in that category claims a family. One report,
    two declarations, two readings, and the difference is the obligation.
    """
    raw = _report(tests=[
        _case(REQUIRED_TESTS[0], "passed", generator=None),
        _case(REQUIRED_TESTS[1], "passed", generator=None),
    ])

    reading = pytest_adapter.interpret(raw, _parsed_check())

    assert type(reading).__name__ == "AdapterResult"
    assert reading.generator_settings is None


def test_a_property_check_with_no_pinned_generator_is_refused() -> None:
    """A property variant with no settings cannot be property evidence at all.

    Unreachable through the manifest, whose `property` branch requires the block,
    and guarded here because the dataclass permits it. Without the guard a caller
    holding one would get a receipt claiming a sampled family that was never
    declared.
    """
    reading = _blocked(
        _report(tests=[
            _case(REQUIRED_TESTS[0], "passed", _settings()),
            _case(REQUIRED_TESTS[1], "passed", _settings()),
        ]),
        generator=None,
    )

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith("generator_absent")


def test_the_tightest_pinned_family_is_the_one_recorded() -> None:
    """Where two required tests ran under different bounds, the smaller is recorded.

    The family is a ceiling on the evidence, so the receipt must quote the
    tightest one any case ran under. Recording the larger would describe the
    least-supported case in the run, which is the same overstatement as
    recording the library default.
    """
    reading = _blocked(_report(tests=[
        _case(REQUIRED_TESTS[0], "passed", _settings(max_examples=200)),
        _case(REQUIRED_TESTS[1], "passed", _settings(max_examples=25)),
    ]))

    assert reading.scope.family == 25
    assert "up to 25 generated case" in reading.scope.sentence()


def test_generator_settings_of_an_unreadable_shape_are_refused() -> None:
    """A settings block this code cannot read is a refusal, not a default.

    The block is written by a plugin inside the child process and read by the
    core, so it is as much an external document as the check's own artifact. A
    missing family field means the one number the receipt needs is absent, and
    substituting a default would be the overstatement.
    """
    broken = {"stateful_step_count": 0, "deadline": None,
              "suppress_health_check": []}
    reading = _blocked(_report(tests=[
        _case(REQUIRED_TESTS[0], "passed", broken),
        _case(REQUIRED_TESTS[1], "passed", broken),
    ]))

    assert reading.reason is BlockedReason.ARTIFACT_MALFORMED
    assert reading.detail.startswith("report_malformed")
    assert REQUIRED_TESTS[0] in reading.detail




@requires_hypothesis
def test_a_property_failure_is_a_fail_and_a_property_crash_is_blocked(
    tmp_path: Path,
) -> None:
    """FAIL and BLOCKED stay apart under the property category too.

    The category changes what a receipt may claim about sampling; it does not
    change what a disagreeing assertion means. A test that raises is still a test
    that failed to deliver an answer, and reporting it as a counterexample would
    put a shrunk falsifying input in the receipt for a run that never found one.
    """
    failing = _repository(tmp_path / "fail", required=(FAILING_TEST,))
    status, row, run_dir = run_check(failing)

    assert (row["result"], status) == ("FAIL", 1), last_outcome(failing)
    assert row["evidence_kind"] == "property", (
        "a FAIL still carries its category, so a reader can tell what the failure "
        "was about"
    )
    receipt = receipt_of(run_dir)
    assert receipt["counterexamples"][0]["obligation"]["obligation"] == FAILING_TEST
    assert receipt["counterexamples"][0]["trace"], "a counterexample names the disagreement"

    crashing = _repository(tmp_path / "crash", required=(CRASHING_TEST,))
    status, row, _run_dir = run_check(crashing)

    assert (row["result"], status) == ("BLOCKED", 3), last_outcome(crashing)
    assert "infrastructure_error" in last_outcome(crashing)["outcome"]["detail"]


@requires_hypothesis
def test_a_property_scope_is_never_claimed_for_a_blocked_run(tmp_path: Path) -> None:
    """A refused run reports no sampled scope, because it sampled nothing.

    The core's own rule is that a BLOCKED reading has no obligations to record,
    and the same has to hold for the scope. A receipt carrying a tested scope next
    to a BLOCKED status would be claiming a family for a run that established
    none, which is the specific overstatement this module is written against.
    """
    repo = _repository(tmp_path, required=(CRASHING_TEST,))

    status, row, run_dir = run_check(repo)

    assert (row["result"], status) == ("BLOCKED", 3)
    receipt = receipt_of(run_dir)
    assert receipt["status"] == "BLOCKED"
    assert receipt["satisfied"] == []
    assert not any("sampled" in line for line in receipt["assumptions"]), (
        receipt["assumptions"]
    )




@requires_hypothesis
def test_a_legacy_scenario_check_still_runs_unchanged(tmp_path: Path) -> None:
    """Adding a property kind does not change what a v1 scenario check means.

    A v1 manifest declares no `kind` and reading it as a scenario driver is the
    only reading it has. Every example manifest in this repository is in that
    shape, so this is the case a real user hits first.
    """
    repo = tmp_path / "legacy"
    (repo / "shop").mkdir(parents=True)
    (repo / "shop" / "pricing.py").write_text(APP_SOURCE, encoding="utf-8")
    (repo / "verify.py").write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from shop.pricing import quote\n"
        "cases = [('one', [1], 1), ('two', [2, 3], 5)]\n"
        "scenarios = [\n"
        "    {'id': name, 'result': 'PASS' if quote(values) == expected else 'FAIL',\n"
        "     'observation': f'printed {quote(values)}'}\n"
        "    for name, values, expected in cases\n"
        "]\n"
        "Path(sys.argv[1]).write_text(json.dumps(\n"
        "    {'schema_version': 1, 'scenarios': scenarios}, indent=2))\n",
        encoding="utf-8",
    )
    (repo / "verification").mkdir()
    (repo / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "description": "A quote function verified by a driver.",
        "checks": [{
            "id": "quote-behavior",
            "description": "Runs the real function and compares printed totals.",
            "command": ["{{python}}", "verify.py", "{{run_dir}}/result.json"],
            "cwd": ".",
            "timeout_seconds": 120,
            "required_scenarios": ["one", "two"],
            "artifact": "result.json",
            "inputs": ["shop/pricing.py", "verify.py"],
            "expectations": [],
        }],
    }, indent=2) + "\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".hypothesis/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    init_repo(repo)

    status, row, run_dir = run_check(repo, "quote-behavior")

    assert status == 0, last_outcome(repo)
    assert row["result"] == "PASS"
    assert row["evidence_kind"] == "scenario"
    assert [s["id"] for s in last_outcome(repo)["outcome"]["scenarios"]] == ["one", "two"]
    receipt = receipt_of(run_dir)
    assert receipt["status"] == "PASS"
    assert receipt["evidence_kind"] == "scenario"
    assert [item["obligation"]["obligation"] for item in receipt["satisfied"]] == [
        "one", "two",
    ]
    assert [item["kind"] for item in receipt["satisfied"]] == ["case_satisfied", "case_satisfied"]
    assert [item["observation"] for item in receipt["satisfied"]] == ["printed 1", "printed 5"]
    assert not any("sampled" in line for line in receipt["assumptions"])

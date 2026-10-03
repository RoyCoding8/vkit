"""Acceptance weighs the category and the obligations, not just the verdict.

`tasks._decide` read a PASS as a bare PASS. Any check under the right id, with
the right identities, discharged any requirement, so a scenario run of three
named cases could satisfy a floor that named two theorems. The receipt has
carried `evidence_kind` and `satisfied` since checkpoint 10.1b, and until now
the identity comparison was the only thing reading them.

**What each run here is.** A real `pytest` check, driven through
`execution.run_check` by a real child process, so the receipt under test is the
one the product wrote. What varies between tests is the requirement set the task
pinned at admission, which is the only lever the gap needed: the requirement is
what says "this run owes a theorem", and nothing else in the system says that.

**Why a pytest check and not a lean one.** `lean` and `tlc` now parse and have
real adapters, but running one means running a real Lean toolchain or a real JRE,
which this module deliberately does not do. What is exercised here is the
comparison, through the same public `finalize` every adapter calls, with the
required category supplied by a contract. The Lean and TLC adapters are exercised
on a real checker in `tests/test_lean_verifier.py` and `tests/test_tlc_verifier.py`,
including on Ubuntu CI, and this module stays about the acceptance comparison
alone.

**The obligation half is not a restatement of the identity guard.** A pytest
check's required test ids are not in the manifest digest at all, because
`manifest._scenario_names` leaves `required_scenarios` empty for every
non-scenario variant. So editing the required set moves no policy digest and the
identity comparison has nothing to notice. `test_the_required_obligation_set_is_
frozen_at_admission` asserts that measurement rather than asserting it in prose.

**The control comes first** because every refusal below is only meaningful
against it. A test that refused a generic PASS because the fixture was broken
would satisfy the letter of the checkpoint and none of its intent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import subproc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from vkit.identity import compute_source_identity  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.tasks import (  # noqa: E402
    AcceptanceContext,
    acceptance_context,
    admit,
    finalize,
    get_task,
    open_task,
)
from vkit.verifiers import (  # noqa: E402
    CaseObligation,
    TheoremObligation,
    obligation_to_json,
)

CHECK_ID = "quote-regression"
REQUIRED_TESTS = (
    "tests/test_behaviour.py::test_totals_an_empty_cart",
    "tests/test_behaviour.py::test_totals_several_positives",
)
UNWRITTEN_TEST = "tests/test_behaviour.py::test_never_written"
THEOREMS = (
    TheoremObligation("decision_implies_ownership", "Ownership.Decision"),
    TheoremObligation("ready_iff_all_required_pass", "Ownership.Ready"),
)

APP_SOURCE = '''\
"""The approved code under test."""


def quote(amounts):
    """The sum of the amounts, which is the whole behaviour under test."""
    return sum(amounts)
'''

TEST_SOURCE = '''\
"""The approved tests. Their totals are literals here."""
from pricing.quote import quote


def test_totals_an_empty_cart():
    assert quote([]) == 0


def test_totals_several_positives():
    assert quote([200, 300, 500]) == 1000
'''

RUNNER_BASE = ["-m", "pytest", "-p", "no:cacheprovider", "-q"]


def _check(*, required: tuple[str, ...] = REQUIRED_TESTS) -> dict:
    return {
        "id": CHECK_ID,
        "kind": "pytest",
        "description": "Runs the approved totals tests.",
        "cwd": ".",
        "timeout_seconds": 180,
        "artifact": "pytest-report.json",
        "inputs": ["pricing/quote.py", "tests/test_behaviour.py"],
        "expectations": [],
        "subject": {"paths": ["pricing/quote.py"], "digest": None},
        "claim_id": "quote-sums-the-amounts",
        "required_tests": list(required),
        "runner": {"executable": "{{python}}", "base_argv": list(RUNNER_BASE)},
        "report_format": "pytest_json_report",
        "expect_report_version": 1,
    }


def _repository(tmp_path: Path, name: str) -> Path:
    """A real repository with real tests and one real registered check."""
    repo = tmp_path / name
    (repo / "pricing").mkdir(parents=True)
    (repo / "pricing" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pricing" / "quote.py").write_text(APP_SOURCE, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_behaviour.py").write_text(TEST_SOURCE, encoding="utf-8")
    # Without this the interpreter writes `__pycache__` while the check runs, the
    # source identity moves underneath the run, and every verdict below would be a
    # source gap rather than the category gap under test.
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (repo / "verification").mkdir()
    (repo / "verification" / "manifest.json").write_text(
        json.dumps({
            "schema_version": 2,
            "description": "A real totals function, verified by running its tests.",
            "checks": [_check()],
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    subproc.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subproc.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t",
         "commit", "-qm", "the approved baseline"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


def _context(project) -> AcceptanceContext:
    """The context the product builds, read through its own constructor.

    Built afresh for every decision rather than reused, because
    `acceptance_context` measures the identities it will compare against at the
    moment it is called. A reused context would compare the run against the
    digest it already agreed with.
    """
    return acceptance_context(project, lambda: parse_manifest(project, project.runs_root))


def _pinned(tmp_path: Path, name: str, requirements: list[dict]):
    """A repository, a task that pins these requirements, and a real run under it.

    The task is opened before the run and the run is recorded under it, which is
    the only arrangement in which these tests measure anything. A task pinned
    after the run owns no evidence, so acceptance would answer "no completed
    run" for a reason that has nothing to do with the category, and every
    refusal below would pass for the wrong one.

    The row is written through `open_task` rather than `admit`, because `admit`
    derives its requirements from the manifest and these tests need to state a
    requirement the manifest does not declare. That is the state a policy pins
    and a candidate cannot.
    """
    from vkit.execution import run_check

    repo = _repository(tmp_path, name)
    task_id = f"t-{name}"
    project = open_project(repo)
    store = Store(project.db_path)
    digest = parse_manifest(project, project.runs_root).digest()
    open_task(
        store, task_id=task_id,
        contract={
            "repository": {
                "root": str(project.root),
                "git_common_dir": str(project.git_common_dir),
            },
            "policy_digest": digest,
            "required_checks": [CHECK_ID],
            "requirements": requirements,
            "scope": "ship",
            "resources": [],
            "declared": {},
        },
        policy_digest=digest,
    )
    outcome = run_check(
        parse_manifest(project, project.runs_root), CHECK_ID, store=store,
        source=compute_source_identity(project), task_id=task_id, attempt=1,
    )
    assert outcome.report["outcome"]["result"] == "PASS", (
        f"the control run was {outcome.report['outcome']}, so every refusal below "
        f"would be testing a broken fixture rather than the category comparison"
    )
    return repo, store, task_id


def _requirement(*, evidence_kind=None, obligations=()) -> dict:
    return {
        "check_id": CHECK_ID,
        "evidence_kind": evidence_kind,
        "obligations": [obligation_to_json(o) for o in obligations],
    }


def _decided(repo: Path, store: Store, task_id: str):
    return finalize(store, task_id, context=_context(open_project(repo)))


# ------------------------------------------------------------------ control


def test_admission_pins_what_each_check_owes_from_the_manifest(tmp_path: Path) -> None:
    """The production path. Every other test here writes the row by hand.

    `_pinned` opens a task with a contract it supplies, which is what lets a
    test state a requirement the manifest does not declare. It is therefore also
    the arrangement that could hide a mistake in the real path, so this test
    drives `admit` and asserts that a task admitted the ordinary way arrives
    already owing what the approved manifest says each check owes.

    Without this, an `admit` that pinned nothing would pass every other test in
    this file and leave the product accepting any verdict.
    """
    from vkit.tasks import admit

    repo = _repository(tmp_path, "admitted")
    project = open_project(repo)
    store = Store(project.db_path)
    context = _context(project)
    assert context.usable, context.refusal

    admitted = admit(store, "t-admitted", context=context, required_checks=[CHECK_ID])

    pinned = admitted.contract.requirements
    assert [r.check_id for r in pinned] == [CHECK_ID], [r.to_json() for r in pinned]
    assert pinned[0].evidence_kind.value == "scenario", (
        f"admission pinned category {pinned[0].evidence_kind!r} for a pytest check"
    )
    assert [o.test_id for o in pinned[0].obligations] == list(REQUIRED_TESTS), (
        f"admission pinned obligations {[o.test_id for o in pinned[0].obligations]}, "
        f"which are not the check's own required tests"
    )


def test_a_pass_of_the_required_category_over_every_obligation_is_ready(
    tmp_path: Path,
) -> None:
    """The control: nothing new is refused when every obligation is discharged."""
    repo, store, task_id = _pinned(
        tmp_path, "control",
        [_requirement(obligations=tuple(CaseObligation(t) for t in REQUIRED_TESTS))],
    )

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "READY", (
        f"a real PASS of a real pytest check under the policy that requires it was "
        f"{verdict.readiness}: {verdict.gaps}"
    )


# --------------------------------------------------------- category refusals


def test_a_generic_pass_does_not_discharge_a_theorem_obligation(tmp_path: Path) -> None:
    """The checkpoint's own claim.

    The recorded run is a real pytest check that genuinely passed both of its
    cases, and its receipt says `scenario`. The requirement names two theorems,
    which no scenario run discharges however green it is. Before this comparison
    existed the verdict was READY.
    """
    repo, store, task_id = _pinned(
        tmp_path, "theorem",
        [_requirement(evidence_kind="theorem_checking", obligations=THEOREMS)],
    )

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "BLOCKED", (
        f"a scenario PASS discharged a theorem_checking requirement: {verdict!r}"
    )
    assert verdict.tested == {}, (
        "a run refused for its category must not be recorded as the tested "
        f"evidence for the requirement it was refused against: {verdict.tested}"
    )


def test_the_category_refusal_names_both_categories(tmp_path: Path) -> None:
    """Both categories, because a reader has to act on the difference.

    A gap that said only "wrong kind of evidence" would send the reader to go and
    find which two, which is the work the gap exists to do.
    """
    repo, store, task_id = _pinned(
        tmp_path, "both-categories",
        [_requirement(evidence_kind="finite_model_checking")],
    )

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "BLOCKED", verdict
    assert any("finite_model_checking" in gap for gap in verdict.gaps), (
        f"no gap named the required category: {verdict.gaps}"
    )
    assert any("scenario" in gap for gap in verdict.gaps), (
        f"no gap named the recorded category: {verdict.gaps}"
    )


def test_a_run_recording_no_category_is_refused(tmp_path: Path) -> None:
    """An unrecorded category is a gap, not an absence of disagreement.

    `evidence_kind` is written from the receipt in the same transaction as the
    verdict, so a row carrying none is a run that recorded no receipt. Reading
    that as "no disagreement" would make a missing receipt the cheapest way to
    discharge an obligation, which is the opposite of what a missing receipt
    means.

    The requirement pins `scenario`, which is the category this check really does
    produce. So the two disagree in only one way: the run recorded nothing. A
    requirement that pinned no category would correctly be indifferent here,
    which is why this one pins the category the run would have had to record.
    """
    repo, store, task_id = _pinned(
        tmp_path, "no-category",
        [_requirement(
            evidence_kind="scenario",
            obligations=tuple(CaseObligation(t) for t in REQUIRED_TESTS),
        )],
    )
    run_id = store.list_runs(task_id=task_id, limit=1)[0]["run_id"]
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET evidence_kind = NULL WHERE run_id = ?", (run_id,))

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "BLOCKED", (
        f"a run that recorded no category was {verdict.readiness}: {verdict.gaps}"
    )
    assert any("evidence_kind" in gap for gap in verdict.gaps), (
        f"no gap named the category as the missing fact: {verdict.gaps}"
    )


def test_a_requirement_pinning_no_category_asks_nothing_of_it(tmp_path: Path) -> None:
    """The other side of the same decision, asserted because it is a decision.

    A requirement that names no category accepts whatever the run recorded. That
    is what a v1 policy naming check ids alone gets, and it would be a defect if
    it were silent, so it is stated here rather than left as a default.
    """
    repo, store, task_id = _pinned(
        tmp_path, "no-category-pinned",
        [_requirement(obligations=tuple(CaseObligation(t) for t in REQUIRED_TESTS))],
    )
    run_id = store.list_runs(task_id=task_id, limit=1)[0]["run_id"]
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET evidence_kind = NULL WHERE run_id = ?", (run_id,))

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "READY", (
        f"a requirement pinning no category was refused for the category the run "
        f"did not record: {verdict.gaps}"
    )


# ------------------------------------------------------ obligation refusals


def test_an_obligation_the_run_never_discharged_is_named(tmp_path: Path) -> None:
    """Discharging two of three obligations is not discharging the requirement.

    The gap names the one that is missing, because a verdict that said only
    "insufficient" leaves the engineer to diff two lists by hand to learn which
    case to write.
    """
    repo, store, task_id = _pinned(
        tmp_path, "partial",
        [_requirement(
            obligations=tuple(
                CaseObligation(name) for name in (*REQUIRED_TESTS, UNWRITTEN_TEST)
            ),
        )],
    )

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "BLOCKED", (
        f"a run that discharged two of three obligations was {verdict.readiness}: "
        f"{verdict.gaps}"
    )
    assert any(UNWRITTEN_TEST in gap for gap in verdict.gaps), (
        f"no gap named the obligation that went undischarged: {verdict.gaps}"
    )
    assert not any(REQUIRED_TESTS[0] in gap for gap in verdict.gaps), (
        f"an obligation the run did discharge was reported as missing: {verdict.gaps}"
    )


def test_a_requirement_removed_from_the_manifest_stays_required(tmp_path: Path) -> None:
    """The frozen half of requirement 5, stated as the truth rather than as a gap.

    The task pinned two obligations. The manifest is re-declared to require one
    case, which is the weakest form of the attack: the candidate edits its own
    declaration rather than its evidence. The pinned contract is unchanged, and
    the decision is still made against two obligations.

    **What this asserts, and why it is not the same claim as `test_an_obligation_
    the_run_never_discharged_is_named`.** Here the run discharged both of them,
    so there is no obligation gap and the verdict turns on the source identity,
    which the edit moved. That is the honest answer: the run is not wrong about
    anything the task asked, but it was produced against a source that is no
    longer the one in force. The obligation comparison's own refusal is exercised
    by the partial test, where the evidence genuinely falls short.

    **What it does claim.** The pinned contract survived the edit. A contract
    that re-derived its requirements from the manifest at decision time would
    owe one case here, and `met_obligations` below is where that would show.
    """
    repo, store, task_id = _pinned(
        tmp_path, "frozen",
        [_requirement(obligations=tuple(CaseObligation(t) for t in REQUIRED_TESTS))],
    )
    contract_before = _contract_of(store, task_id)
    project = open_project(repo)
    assert _context(project).source_inventory_digest == _recorded_source(store, task_id), (
        "the control run did not record the source identity in force when it ran, "
        "so the assertions below could not tell one kind of gap from another"
    )

    path = repo / "verification" / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["checks"][0]["required_tests"] = [REQUIRED_TESTS[0]]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    pinned = json.loads(contract_before)
    assert parse_manifest(project, project.runs_root).digest() == pinned["policy_digest"], (
        "the manifest digest moved when only a pytest check's required test ids "
        "changed. Measured here rather than assumed, because "
        "`manifest._scenario_names` leaves `required_scenarios` empty for every "
        "non-scenario variant."
    )
    assert _contract_of(store, task_id) == contract_before, (
        "the pinned contract changed without an admission. A manifest edit reached "
        "back into a contract that was frozen."
    )

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "BLOCKED", verdict.gaps
    assert verdict.context["met_obligations"][CHECK_ID] == [
        f"case {REQUIRED_TESTS[0]!r}", f"case {REQUIRED_TESTS[1]!r}",
    ], (
        "the manifest edit shrank what the task owes. The decision was made "
        f"against {verdict.context['met_obligations']}"
    )
    assert verdict.context["unmet_obligations"][CHECK_ID] == [], (
        "the run genuinely discharged both pinned obligations, so nothing is "
        f"unmet: {verdict.context['unmet_obligations']}"
    )


def test_an_obligation_replaced_during_the_attempt_stays_required(tmp_path: Path) -> None:
    """Requirement 5's other half: replacing an obligation does not retire it.

    The task pinned three obligations and the run discharged two of them. The
    manifest is then re-declared so the second pinned obligation is no longer
    among the check's required tests, which is what "replaced" looks like from
    the candidate's side. The pinned contract is untouched, so the third
    obligation is still owed and the gap still names it.

    This is the test that the manifest edit cannot launder an undischarged
    obligation, and it is separate from the partial test because that one holds
    the manifest still and this one moves it.
    """
    repo, store, task_id = _pinned(
        tmp_path, "replaced",
        [_requirement(obligations=tuple(
            CaseObligation(name) for name in (*REQUIRED_TESTS, UNWRITTEN_TEST)
        ))],
    )
    contract_before = _contract_of(store, task_id)

    path = repo / "verification" / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["checks"][0]["required_tests"] = [REQUIRED_TESTS[0]]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    assert _contract_of(store, task_id) == contract_before, (
        "the pinned contract changed without an admission, so there was nothing "
        "frozen to compare against"
    )

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "BLOCKED", verdict.gaps
    assert any(UNWRITTEN_TEST in gap for gap in verdict.gaps), (
        f"the pinned obligation stopped being required because the manifest was "
        f"edited: {verdict.gaps}"
    )
    assert verdict.context["unmet_obligations"][CHECK_ID] == [
        f"case {UNWRITTEN_TEST!r}"
    ], verdict.context["unmet_obligations"]


# ------------------------------------------------------------ what surfaces


def test_the_decision_publishes_the_categories_and_the_unmet_obligations(
    tmp_path: Path,
) -> None:
    """What a reader acts on travels with the verdict, not only inside the gap.

    `task_finalize` and `vkit task finalize --json` both publish
    `ReadinessResult.context` verbatim, so this asserts on that dict rather than
    on each adapter. The console and the MCP surface cannot drift from the core
    here without a second decision, and there is no second decision.
    """
    repo, store, task_id = _pinned(
        tmp_path, "surfaces",
        [_requirement(evidence_kind="theorem_checking", obligations=(THEOREMS[0],))],
    )

    published = json.loads(json.dumps(_decided(repo, store, task_id).context))

    requirements = published["requirements"]
    assert [r["check_id"] for r in requirements] == [CHECK_ID], published
    assert requirements[0]["evidence_kind"] == "theorem_checking", (
        f"the required category was not published: {requirements}"
    )
    assert requirements[0]["obligations"] == [
        obligation_to_json(THEOREMS[0])
    ], published
    assert published["met_obligations"][CHECK_ID] == [], (
        f"a refused requirement reported met obligations: {published}"
    )
    assert published["unmet_obligations"][CHECK_ID] == [
        "theorem 'decision_implies_ownership' in module 'Ownership.Decision'"
    ], f"the exact unmet obligation was not published: {published}"


def test_the_exact_unmet_obligation_reaches_the_mcp_surface(tmp_path: Path) -> None:
    """`task_finalize` over MCP, driven the way a client drives it.

    Driven through the real `Server` rather than through `finalize`, because a
    payload assembled by the same code the payload is being tested for proves
    only that the dict is serializable. The gap text and the published unmet
    obligation are the two things a client reads.
    """
    from vkit.mcp._tools import Server

    repo, _store, task_id = _pinned(
        tmp_path, "mcp",
        [_requirement(evidence_kind="theorem_checking", obligations=(THEOREMS[0],))],
    )

    result = Server(repo).call_tool("task_finalize", {"task_id": task_id})

    assert result.content["readiness"] == "BLOCKED", result.content
    unmet = result.content["context"]["unmet_obligations"][CHECK_ID]
    assert unmet == ["theorem 'decision_implies_ownership' in module 'Ownership.Decision'"], (
        f"the MCP surface published unmet obligations {unmet}"
    )
    assert any("theorem_checking" in gap for gap in result.content["gaps"]), (
        f"no MCP gap named the category: {result.content['gaps']}"
    )


# ----------------------------------------------- the guard that already held


def test_a_source_change_still_refuses_the_run_that_predates_it(tmp_path: Path) -> None:
    """The identity guard, asserted so a change to `_decide` cannot displace it.

    Not new coverage. `_identity_gaps` has held this since the identity
    comparison landed and this test claims no credit for it. It is here because
    the checkpoint asks whether a source change still makes earlier evidence
    unusable, and `_decide` is exactly where that could quietly stop being true.
    """
    repo, store, task_id = _pinned(
        tmp_path, "source-change",
        [_requirement(obligations=tuple(CaseObligation(t) for t in REQUIRED_TESTS))],
    )
    assert _decided(repo, store, task_id).readiness == "READY"

    (repo / "pricing" / "quote.py").write_text(
        APP_SOURCE + "\n# an edit made after the run\n", encoding="utf-8"
    )

    verdict = _decided(repo, store, task_id)

    assert verdict.readiness == "BLOCKED", (
        f"a run taken before the source moved was {verdict.readiness}: {verdict.gaps}"
    )
    assert any("source" in gap for gap in verdict.gaps), verdict.gaps


def _contract_of(store: Store, task_id: str) -> str:
    return json.dumps(get_task(store, task_id).contract, sort_keys=True)


def _recorded_source(store: Store, task_id: str) -> str | None:
    return store.list_runs(task_id=task_id, limit=1)[0]["source_inventory_digest"]
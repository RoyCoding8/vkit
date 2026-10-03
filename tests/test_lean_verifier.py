"""The Lean adapter, against a real Lean toolchain and against bytes a tool never wrote.

Two halves, and the split is the point. Everything that can be decided without a
checker is decided here on every host, from report bytes and a check declaration,
so a refusal this file asserts is a fact about the adapter rather than about
whether the operator has Lean installed. Everything that needs the kernel runs
only where a Lean toolchain exists, and is skipped with a stated reason where one
does not, because a green test that never ran reads as satisfied.

**The real cases run the real checker.** `lean_runner.py` is launched exactly as
`dispatch` launches it and Lean is the toolchain on `PATH`; the argv it uses was
measured against `lean --help` on 4.34.1 rather than read out of a manual. On this
host (elan, Lean 4.34.1, Windows) the positive case and all four negative cases
were executed and are reproduced in `formal/results/`. The reviewed profile's own
gate is Linux-only and is refused here by measurement, which is the point of the
gate: a Windows host with a real Lean in it still cannot discharge it, and a test
that passed here on the strength of the installed toolchain would be a false pass.

**Every negative case is proven red against correct code**, not merely asserted to
be refused. The construction for each is in the module that produces it: a `sorry`
makes Lean's own `#print axioms` name `sorryAx`; a custom axiom makes it name that
axiom; a missing theorem makes the audit fail to resolve the constant; an
elaborated-but-false proof makes the elaboration step exit nonzero. Each of those
is a property of the input bytes that does not depend on this adapter being
correct, so an adapter that accepted any of them would be a bug and the test
below is the assertion that catches it.
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

from vkit.outcome import Blocked, BlockedReason  # noqa: E402
from vkit.verifiers import lean_adapter  # noqa: E402
from vkit.verifiers.dispatch import outcome_from_reading  # noqa: E402
from vkit.verifiers.spec import (  # noqa: E402
    CheckKind,
    LeanCheck,
    LeanProfile,
    ModuleRef,
    SubjectRef,
    TheoremObligation,
    ToolchainRef,
)


def lean_available() -> bool:
    return shutil.which("lean") is not None


needs_lean = pytest.mark.skipif(
    not lean_available(), reason="no Lean toolchain on PATH; see the CI job"
)

#: The one good proof every real case builds from. `Nat.add_zero` is a theorem,
#: so nothing here leans on an axiom and the audit has an honest empty set to
#: report. A case built on `simp` would work too; the point is that the base case
#: is one a reviewer would accept without reading the proof.
GOOD = """\
namespace Case

theorem wanted (n : Nat) : n + 0 = n := by
  exact Nat.add_zero n

end Case
"""

#: The same statement, left unproved. Lean accepts this and reports `hasSorry`,
#: and `#print axioms` then names `sorryAx`, which is what the adapter refuses.
SORRY = """\
namespace Case

theorem wanted (n : Nat) : n + 0 = n := by
  sorry

end Case
"""

#: A goal discharged by an axiom the check never permitted. This is the one the
#: plan names: "hides sorry in a dependency, or adds an axiom". The module
#: elaborates cleanly, which is what makes it dangerous.
AXIOM = """\
namespace Case

axiom cheat : (n : Nat) → n + 0 = n

theorem wanted (n : Nat) : n + 0 = n := by
  exact cheat n

end Case
"""

#: A theorem that does not exist. The obligation names `wanted` and this module
#: only declares `other`, so the audit cannot resolve the constant and the
#: adapter refuses rather than reporting a theorem that was never checked.
MISSING_THEOREM = """\
namespace Case

theorem other (n : Nat) : n + 0 = n := by
  exact Nat.add_zero n

end Case
"""

#: A statement that is false. It elaborates as a declaration with a proof term
#: only if the proof is also broken, so the build step exits nonzero and the
#: adapter refuses on an elaboration that never completed.
UNPROVABLE = """\
namespace Case

theorem wanted (n : Nat) : n + 1 = n := by
  exact Nat.add_zero n

end Case
"""

#: The challenge altered after the run. Same declaration, different statement:
#: this is what a candidate would leave behind to make a run about the approved
#: obligation look like a run about a weaker one.
ALTERED = GOOD.replace("n + 0 = n", "n + 1 = n")


def a_check(project: Path, **overrides) -> LeanCheck:
    """A Lean check over `project/Case.lean`, with the given fields replaced."""
    base = dict(
        id="theorem-check",
        kind=CheckKind.LEAN,
        subject=SubjectRef(),
        claim_id="claim",
        cwd=project,
        timeout_seconds=600.0,
        artifact_name="lean-report.json",
        challenge=ModuleRef("Case", "Case.lean"),
        theorems=(TheoremObligation("wanted", "Case"),),
        profile=LeanProfile.UNREVIEWED,
        permitted_axioms=(),
        toolchain=ToolchainRef("lean"),
    )
    base.update(overrides)
    return LeanCheck(**base)


def run_real(tmp_path: Path, source: str) -> dict:
    """Write `source` and run the real Lean through the adapter's own runner.

    Launched through `argv_for` and `subproc`, exactly as `execution` would, so
    this exercises the shipped argv rather than a call written for the test. A
    runner that only worked when a test called it a different way would pass every
    other test in this file and fail a real run.
    """
    project = tmp_path / "proj"
    run_dir = tmp_path / "run"
    project.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    (project / "Case.lean").write_text(source, encoding="utf-8")
    check = a_check(project)
    done = subproc.run(
        lean_adapter.argv_for(check, run_dir, sys.executable),
        cwd=str(project), capture_output=True, encoding="utf-8", errors="replace",
        timeout=900, check=False,
    )
    report = run_dir / check.artifact_name
    assert report.is_file(), (
        f"the runner wrote no report. exit {done.returncode}\n"
        f"stdout: {done.stdout}\nstderr: {done.stderr}"
    )
    return {"report": report.read_bytes(), "check": check, "stdout": done.stdout}


# ------------------------------------------- refusals that need no toolchain


def _digest_of(source: str) -> str:
    """The digest the adapter will measure for this source.

    The same normalization `lean_adapter._digest` applies, with CRLF folded to
    LF. Written here rather than called from the adapter so a test whose source
    differs from the base one gets a matching digest by default, and a test about
    the comparison itself can still pass a mismatched one deliberately.
    """
    import hashlib

    return hashlib.sha256(source.encode("utf-8").replace(b"\r\n", b"\n")).hexdigest()


def _report(source: str = GOOD, **overrides) -> bytes:
    """A well-formed report for `source`, with the given fields replaced.

    Built from a shape that passes every refusal so a test can vary exactly one
    thing. A refusal asserted against a report that is malformed for some other
    reason would pass for the wrong reason.

    `source_sha256` defaults to the digest of the same `source` the test writes to
    disk, because the challenge comparison is mandatory and a base report with a
    digest of something else would fail every test here for one unrelated reason.
    """
    document = {
        "version": lean_adapter.REPORT_VERSION,
        "complete": True,
        "tool": "lean",
        "tool_version": "Lean (version 4.34.1)",
        "module": "Case",
        "source_sha256": _digest_of(source),
        "theorems": ["wanted"],
        "audit_text": "",
        "steps": [
            {"name": "build:1", "exit_code": 0, "messages": [],
             "olean_digest": "d1"},
            {"name": "recheck:2", "exit_code": 0, "messages": [],
             "olean_digest": "d1"},
            {"name": "audit", "exit_code": 0, "lean_path": ".", "messages": [
                {"severity": "information", "kind": "[anonymous]",
                 "data": "'Case.wanted' does not depend on any axioms",
                 "file": "audit.lean", "line": 2, "column": 0},
            ]},
        ],
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


def _project(tmp_path: Path, source: str = GOOD) -> Path:
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "Case.lean").write_text(source, encoding="utf-8")
    return project


def test_a_well_formed_report_satisfies_its_theorem(tmp_path: Path) -> None:
    """The positive case on bytes no checker produced, which is the unit level.

    The real kernel case is below. This one exists so the adapter's acceptance
    path is exercised on every host, including the ones with no Lean at all.
    """
    project = _project(tmp_path)
    result = lean_adapter.interpret(_report(), a_check(project))
    assert isinstance(result, lean_adapter.AdapterResult)
    assert result.is_pass
    satisfied, counterexamples = result.obligation_results()
    assert counterexamples == []
    assert satisfied == [{
        "kind": "theorem_satisfied",
        "obligation": {"kind": "theorem", "obligation": "wanted", "module": "Case"},
        "axioms": [],
    }]


@pytest.mark.parametrize("version,token", [
    (99, "report_version"),
    (None, "report_version"),
])
def test_a_report_of_another_version_is_refused(tmp_path: Path, version, token) -> None:
    """A document whose fields are read by name is one whose meaning is unknown."""
    project = _project(tmp_path)
    result = lean_adapter.interpret(_report(version=version), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith(token), result.detail


def test_a_truncated_report_is_refused(tmp_path: Path) -> None:
    """A runner that stopped cannot say which theorems it did and did not check."""
    project = _project(tmp_path)
    result = lean_adapter.interpret(
        _report(complete=False, failure="the Lean toolchain could not be launched"),
        a_check(project),
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("report_truncated"), result.detail


def test_bytes_that_are_not_json_are_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = lean_adapter.interpret(b"not json at all", a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("report_malformed"), result.detail


def test_a_report_with_no_elaboration_is_refused(tmp_path: Path) -> None:
    """An audit with no build in front of it certifies nothing."""
    project = _project(tmp_path)
    result = lean_adapter.interpret(
        _report(steps=[{"name": "audit", "exit_code": 0, "messages": []}]),
        a_check(project),
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("report_malformed"), result.detail


def test_a_report_with_no_axiom_audit_is_refused(tmp_path: Path) -> None:
    """A compiled module is not a theorem result without the audit that read it."""
    project = _project(tmp_path)
    result = lean_adapter.interpret(
        _report(steps=[{"name": "build:1", "exit_code": 0, "messages": [],
                        "olean_digest": "d1"}]),
        a_check(project),
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("report_malformed"), result.detail


def test_an_elaboration_error_is_an_incomplete_proof(tmp_path: Path) -> None:
    """A module that did not compile established nothing, which is not a FAIL."""
    project = _project(tmp_path)
    steps = [
        {"name": "build:1", "exit_code": 1, "olean_digest": "",
         "messages": [{"severity": "error", "kind": "[anonymous]",
                       "data": "Type mismatch\n  0\nhas type\n  Nat",
                       "file": "Case.lean", "line": 3, "column": 4}]},
        {"name": "recheck:2", "exit_code": 1, "olean_digest": "",
         "messages": []},
        {"name": "audit", "exit_code": 1, "messages": []},
    ]
    result = lean_adapter.interpret(_report(GOOD, steps=steps), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("incomplete_proof"), result.detail
    assert result.reason is BlockedReason.INTERNAL_ERROR


def test_a_sorry_declaration_is_an_incomplete_proof(tmp_path: Path) -> None:
    """`hasSorry` is Lean's own name for a declaration that compiled unproved."""
    project = _project(tmp_path, SORRY)
    steps = [
        {"name": "build:1", "exit_code": 0, "olean_digest": "d1",
         "messages": [{"severity": "warning", "kind": "hasSorry",
                       "data": "declaration uses `sorry`",
                       "file": "Case.lean", "line": 3, "column": 2}]},
        {"name": "recheck:2", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "audit", "exit_code": 0, "messages": [
            {"severity": "information", "kind": "[anonymous]",
             "data": "'Case.wanted' depends on axioms: [sorryAx]",
             "file": "audit.lean", "line": 2, "column": 0},
        ]},
    ]
    result = lean_adapter.interpret(_report(SORRY, steps=steps), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("incomplete_proof"), result.detail


def test_a_theorem_depending_on_sorry_ax_is_a_counterexample(tmp_path: Path) -> None:
    """The sorry hidden behind a declaration the build step did not flag.

    `sorryAx` reaching the audit rather than the build is the shape the plan
    names: a `sorry` in a dependency. The audit is the checker's own answer, so
    the refusal reads it rather than scanning source text for a spelling.
    """
    project = _project(tmp_path, SORRY)
    steps = [
        {"name": "build:1", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "recheck:2", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "audit", "exit_code": 0, "messages": [
            {"severity": "information", "kind": "[anonymous]",
             "data": "'Case.wanted' depends on axioms: [sorryAx]",
             "file": "audit.lean", "line": 2, "column": 0},
        ]},
    ]
    result = lean_adapter.interpret(_report(SORRY, steps=steps), a_check(project))
    assert not isinstance(result, Blocked)
    assert not result.is_pass
    _, counterexamples = result.obligation_results()
    assert len(counterexamples) == 1
    assert "sorryAx" in counterexamples[0]["trace"], counterexamples


def test_an_unapproved_axiom_is_a_counterexample(tmp_path: Path) -> None:
    """An assumption added to close a goal is not a proof of the statement."""
    project = _project(tmp_path, AXIOM)
    steps = [
        {"name": "build:1", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "recheck:2", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "audit", "exit_code": 0, "messages": [
            {"severity": "information", "kind": "[anonymous]",
             "data": "'Case.wanted' depends on axioms: [cheat]",
             "file": "audit.lean", "line": 2, "column": 0},
        ]},
    ]
    result = lean_adapter.interpret(_report(AXIOM, steps=steps), a_check(project))
    assert not isinstance(result, Blocked)
    assert not result.is_pass
    _, counterexamples = result.obligation_results()
    assert "cheat" in counterexamples[0]["trace"], counterexamples


def test_an_axiom_the_check_permits_is_accepted(tmp_path: Path) -> None:
    """The permitted set is the ceiling, and a check sets it.

    The same bytes that fail above pass here with `Case.cheat` permitted, which
    is what shows the refusal is about the declaration rather than about axioms
    existing. A check that could not permit anything could never accept a real
    proof either.
    """
    project = _project(tmp_path, AXIOM)
    steps = [
        {"name": "build:1", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "recheck:2", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "audit", "exit_code": 0, "messages": [
            {"severity": "information", "kind": "[anonymous]",
             "data": "'Case.wanted' depends on axioms: [cheat]",
             "file": "audit.lean", "line": 2, "column": 0},
        ]},
    ]
    result = lean_adapter.interpret(
        _report(AXIOM, steps=steps),
        a_check(project, permitted_axioms=("cheat",)),
    )
    assert isinstance(result, lean_adapter.AdapterResult)
    assert result.is_pass
    satisfied, _ = result.obligation_results()
    assert satisfied[0]["axioms"] == ["cheat"]


def test_a_theorem_the_audit_could_not_resolve_is_refused(tmp_path: Path) -> None:
    """A theorem the kernel could not name is not a theorem that holds."""
    project = _project(tmp_path, MISSING_THEOREM)
    steps = [
        {"name": "build:1", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "recheck:2", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "audit", "exit_code": 1, "messages": [
            {"severity": "error", "kind": "lean.unknownIdentifier._namedError",
             "data": "Unknown constant `Case.wanted`",
             "file": "audit.lean", "line": 2, "column": 14},
        ]},
    ]
    result = lean_adapter.interpret(_report(MISSING_THEOREM, steps=steps), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("theorem_absent"), result.detail


def test_a_theorem_with_no_audit_line_is_a_counterexample(tmp_path: Path) -> None:
    """A silent audit is not a clean one.

    A run whose third `#print axioms` never printed would otherwise read as a
    theorem that depends on nothing, which is the one conclusion a missing line
    must never support.
    """
    project = _project(tmp_path)
    steps = [
        {"name": "build:1", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "recheck:2", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "audit", "exit_code": 0, "messages": []},
    ]
    result = lean_adapter.interpret(_report(GOOD, steps=steps), a_check(project))
    assert not isinstance(result, Blocked)
    assert not result.is_pass
    _, counterexamples = result.obligation_results()
    assert "said nothing about" in counterexamples[0]["trace"], counterexamples


def test_an_altered_challenge_is_refused(tmp_path: Path) -> None:
    """The proof was checked against different bytes than the ones on disk.

    The run recorded the digest of GOOD, which is what it actually compiled, and
    the file on disk now holds ALTERED. The adapter measures the file and compares
    the two, so a challenge swapped after the run cannot have that result
    attributed to the approved statement.
    """
    project = _project(tmp_path, GOOD)
    (project / "Case.lean").write_text(ALTERED, encoding="utf-8")
    result = lean_adapter.interpret(_report(GOOD), a_check(project))
    assert isinstance(result, Blocked), (
        "the run's challenge digest was the digest of GOOD while the file on disk "
        "holds ALTERED, and the run was accepted anyway"
    )
    assert result.detail.startswith("challenge_altered"), result.detail
    assert result.reason is BlockedReason.SOURCE_CHANGED


def test_a_run_that_recorded_no_challenge_digest_is_refused(tmp_path: Path) -> None:
    """An unrecorded digest is the absence of the one measurement, not agreement.

    The first version of the adapter compared `if recorded and recorded !=
    measured`, which meant a report carrying no digest skipped the comparison
    entirely and the check passed. The refusal below is what closes that, and this
    test is the regression for it.
    """
    project = _project(tmp_path, GOOD)
    result = lean_adapter.interpret(_report(GOOD, source_sha256=""), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("challenge_unknown"), result.detail


def test_an_absent_challenge_is_refused(tmp_path: Path) -> None:
    """Nothing was compared against anything, so nothing can be concluded."""
    project = tmp_path / "proj"
    project.mkdir()
    result = lean_adapter.interpret(_report(), a_check(project))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("challenge_absent"), result.detail


# --------------------------------------------------------- the two profiles


def test_the_default_profile_is_the_unreviewed_one() -> None:
    """The plan's default, and the direction that cannot be reached by omission.

    `manifest` puts this on a check that declared no profile, so an agent cannot
    obtain the stronger profile's trust assumption by leaving a field out.
    """
    assert lean_adapter.DEFAULT_PROFILE == LeanProfile.UNREVIEWED
    assert lean_adapter.DEFAULT_PROFILE == "unreviewed_agent"


def test_the_reviewed_profile_requires_isolation_and_the_unreviewed_one_does_not() -> None:
    """A function of the profile alone, so the machine cannot answer it.

    Reading the host in here would make the answer change with the host, and the
    host is not evidence about a proof.
    """
    assert lean_adapter.isolation_required(LeanProfile.REVIEWED) is True
    assert lean_adapter.isolation_required(LeanProfile.UNREVIEWED) is False


def test_the_reviewed_profile_is_refused_without_isolation_and_is_not_downgraded(
    tmp_path: Path,
) -> None:
    """A missing capability is an environment blocker, named, never a downgrade.

    Skipped where the host does isolate a build, because there the run is
    legitimately accepted and this test would be asserting the wrong thing. The
    reviewed profile's real acceptance is covered by the Linux CI job.
    """
    if lean_adapter.requires_isolation():
        pytest.skip("this host isolates a proof build; see the CI job for the "
                    "reviewed profile's real run")
    project = _project(tmp_path)
    result = lean_adapter.interpret(
        _report(), a_check(project, profile=LeanProfile.REVIEWED)
    )
    assert isinstance(result, Blocked)
    assert result.detail.startswith("isolation_unavailable"), result.detail
    assert LeanProfile.UNREVIEWED in result.detail, (
        "the refusal must name the profile it declined to fall back to"
    )
    assert "proof-build isolation" in result.detail


def test_the_reviewed_profile_refuses_two_elaborations_that_differ(tmp_path: Path) -> None:
    """A recheck that cannot reproduce the first is not a recheck."""
    if not lean_adapter.requires_isolation():
        pytest.skip("the reviewed profile is refused before this point off Linux")
    project = _project(tmp_path)
    steps = [
        {"name": "build:1", "exit_code": 0, "olean_digest": "d1", "messages": []},
        {"name": "recheck:2", "exit_code": 0, "olean_digest": "DIFFERENT", "messages": []},
        {"name": "audit", "exit_code": 0, "messages": [
            {"severity": "information", "kind": "[anonymous]",
             "data": "'Case.wanted' does not depend on any axioms",
             "file": "audit.lean", "line": 2, "column": 0},
        ]},
    ]
    result = lean_adapter.interpret(_report(steps=steps), a_check(project, profile=LeanProfile.REVIEWED))
    assert isinstance(result, Blocked)
    assert result.detail.startswith("recheck_diverged"), result.detail


def test_the_profile_is_recorded_in_the_receipt_assumptions(tmp_path: Path) -> None:
    """A reader has to know which proof environment the claim was made in."""
    project = _project(tmp_path)
    result = lean_adapter.interpret(_report(), a_check(project))
    text = " ".join(result.assumptions())
    assert "unreviewed_agent" in text
    assert "does not review the proof source" in text


def test_the_receipt_says_a_theorem_says_nothing_about_the_python_core(
    tmp_path: Path,
) -> None:
    """`claimkind.py`'s prose, which the receipt must not overstate.

    Asserted as a literal substring rather than by importing the property, so a
    change to `claimkind.py` that loosened the sentence fails here rather than
    quietly changing what a receipt claims.
    """
    project = _project(tmp_path)
    result = lean_adapter.interpret(_report(), a_check(project))
    text = " ".join(result.assumptions())
    assert "anything about the Python core" in text
    assert "correspondence obligation" in text


# ------------------------------------------------------------------ the argv


def test_the_argv_names_this_adapter_runner_and_the_declared_theorems(
    tmp_path: Path,
) -> None:
    """A candidate cannot redirect the reader, and the audit is written here.

    The runner path and the theorem list are both load-bearing: the first stops
    a substituted reader, the second stops a project shipping its own audit file
    that audits nothing.
    """
    project = _project(tmp_path)
    run_dir = tmp_path / "run"
    argv = lean_adapter.argv_for(a_check(project), run_dir, sys.executable)
    assert argv[0] == sys.executable
    assert argv[1] == str(lean_adapter.RUNNER_PATH)
    assert lean_adapter.RUNNER_PATH.is_file()
    joined = " ".join(argv)
    assert "--theorems wanted" in joined
    assert "--module Case" in joined
    assert str((project / "Case.lean").resolve()) in argv


def test_dispatch_reaches_the_lean_adapter_through_its_one_table(
    repo, tmp_path: Path,
) -> None:
    """There is one dispatch, and this is the route a run takes for `lean`.

    Driven through `manifest.parse_manifest_bytes` rather than by naming an
    attribute, because the claim is about the public path a run takes and a reader
    cannot check a claim about a lambda. A kind given a fallback adapter would
    return a verdict here that no checker produced, which is what this asserts
    against.
    """
    from vkit.verifiers.dispatch import ADAPTERS, argv_for, interpret

    assert set(ADAPTERS) == set(CheckKind), (
        "every declared kind has an adapter and none was added without one: "
        f"{sorted(k.value for k in ADAPTERS)}"
    )
    assert ADAPTERS[CheckKind.LEAN].kind is CheckKind.LEAN

    project = repo({"Case.lean": GOOD})
    parsed = _parsed_lean_check(project).checks["theorem-check"]
    assert parsed.kind is CheckKind.LEAN
    assert parsed.evidence_kind().value == "theorem_checking"
    argv = argv_for(parsed, tmp_path / "run", sys.executable)
    assert argv[1] == str(lean_adapter.RUNNER_PATH)
    # The same refusal the direct call produces, reached through dispatch. A
    # second dispatch that disagreed with this one would be a second authority
    # about what a verdict means, and this is what would catch it.
    reading = interpret(parsed, b"not json")
    assert isinstance(reading, Blocked)
    assert reading.detail.startswith("report_malformed")


def _parsed_lean_check(project):
    """The manifest as the parser builds it, from the public v2 bytes.

    Going through `parse_manifest_bytes` rather than hand-building the wrapper is
    what makes this test the public path: the `CheckSpec` a run receives is the
    parser's product, not a shape a caller assembles.
    """
    from vkit import manifest as manifest_module

    definition = {
        "schema_version": 2,
        "checks": [{
            "id": "theorem-check",
            "kind": "lean",
            "cwd": ".",
            "timeout_seconds": 600,
            "artifact": "lean-report.json",
            "subject": {"paths": ["Case.lean"], "digest": None},
            "claim_id": "claim",
            "challenge": {"module": "Case", "path": "Case.lean"},
            "theorems": ["wanted"],
            "profile": LeanProfile.UNREVIEWED,
            "permitted_axioms": [],
            "toolchain": {"tool": "lean"},
        }],
    }
    return manifest_module.parse_manifest_bytes(
        json.dumps(definition).encode("utf-8"),
        project=project, run_dir=project.runs_root, origin="<test>",
    )


# ------------------------------------------- the real checker, where it exists


@needs_lean
def test_a_real_proof_of_a_real_theorem_passes(tmp_path: Path) -> None:
    """The positive case, run by Lean's kernel on this host.

    Not a proxy. `lean_runner` launches the toolchain on PATH, the toolchain
    elaborates the module twice and the adapter reads back the axioms the kernel
    reported. A test that asserted the adapter's shape without running Lean would
    pass on a host where the argv is wrong.
    """
    run = run_real(tmp_path, GOOD)
    result = lean_adapter.interpret(run["report"], run["check"])
    assert isinstance(result, lean_adapter.AdapterResult), result
    assert result.is_pass, result.obligation_results()
    satisfied, _ = result.obligation_results()
    assert satisfied[0]["obligation"]["obligation"] == "wanted"
    assert satisfied[0]["axioms"] == []
    assert "Lean" in result.tool_version
    outcome = outcome_from_reading(result)
    assert outcome.to_json()["result"] == "PASS"


@needs_lean
def test_a_real_sorry_is_refused(tmp_path: Path) -> None:
    """Negative case 1, and why it cannot pass: Lean's own `hasSorry`.

    The refusal is reachable because `lean` reports `hasSorry` for a `sorry` even
    though it exits zero. A runner that trusted the exit code would call this a
    pass.
    """
    run = run_real(tmp_path, SORRY)
    result = lean_adapter.interpret(run["report"], run["check"])
    assert isinstance(result, Blocked), f"a sorry was accepted: {result!r}"
    assert result.detail.startswith("incomplete_proof"), result.detail


@needs_lean
def test_a_real_added_axiom_is_a_counterexample(tmp_path: Path) -> None:
    """Negative case 2, and why it cannot pass: `#print axioms` names it.

    This module elaborates cleanly and exits zero, so the only thing standing
    between it and a PASS is the axiom audit. That is the case the plan names and
    it is the reason the audit is written by vkit rather than read from the
    project.
    """
    run = run_real(tmp_path, AXIOM)
    result = lean_adapter.interpret(run["report"], run["check"])
    assert isinstance(result, lean_adapter.AdapterResult), result
    assert not result.is_pass
    _, counterexamples = result.obligation_results()
    assert "cheat" in counterexamples[0]["trace"]


@needs_lean
def test_a_real_missing_theorem_is_refused(tmp_path: Path) -> None:
    """Negative case 3: the obligation names a declaration the module lacks."""
    run = run_real(tmp_path, MISSING_THEOREM)
    result = lean_adapter.interpret(run["report"], run["check"])
    assert isinstance(result, Blocked), f"a missing theorem was accepted: {result!r}"
    assert result.detail.startswith("theorem_absent"), result.detail


@needs_lean
def test_a_real_unprovable_statement_is_an_incomplete_proof(tmp_path: Path) -> None:
    """Negative case 4: the elaboration itself fails.

    `n + 1 = n` is false, so the build step exits nonzero and the adapter refuses
    on an incomplete proof rather than reporting a counterexample. A false
    theorem is not a theorem with a counterexample; there is no theorem to have
    one.
    """
    run = run_real(tmp_path, UNPROVABLE)
    result = lean_adapter.interpret(run["report"], run["check"])
    assert isinstance(result, Blocked), f"a false statement was accepted: {result!r}"
    assert result.detail.startswith("incomplete_proof"), result.detail


@needs_lean
def test_the_runner_produced_the_audit_from_the_checks_own_theorem_list(
    tmp_path: Path,
) -> None:
    """The audit text is vkit's, not the project's.

    Asserted on the runner's own report rather than on the adapter, because the
    property is about what was written before the adapter read anything. A
    project that shipped an audit file printing nothing would pass an adapter
    test that only checked the reading.
    """
    run = run_real(tmp_path, GOOD)
    report = json.loads(run["report"].decode("utf-8"))
    audit_text = report["audit_text"]
    assert "import Case" in audit_text
    assert "#print axioms wanted" in audit_text
    assert "namespace Case" in audit_text
    steps = {step["name"]: step for step in report["steps"]}
    assert {"build:1", "recheck:2", "audit"} <= set(steps)
    assert steps["build:1"]["olean_digest"] == steps["recheck:2"]["olean_digest"], (
        "the two elaborations of the same bytes produced different modules, so "
        "the fresh-rechecking half of the reviewed profile has nothing to rest on"
    )
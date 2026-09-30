"""The candidate cannot lower its own bar, and a moved target invalidates a
readiness that was computed against the old one.

Four properties, each driven through the public command.

**The policy comes from trusted configuration.** The required set and the
manifest the checks actually run are read from the approved reference before the
candidate is checked out, so a candidate that edits `verification/manifest.json`
to drop a check changes nothing about what runs. The candidate's own manifest is
then compared, and a difference is a finding that refuses in the protected
context rather than a quiet pass.

**Local is not weak, it is scoped.** A policy supplied as a path produces a real
refusal, and it is never the protected integration decision. Both facts are in
every record, so nobody has to guess which kind of answer they are reading.

**A target that moves invalidates the readiness.** The tested target is recorded
with the decision, and the command states that publishing requires re-checking
it. The test moves the target after the decision and shows that the recorded
target no longer resolves to the branch, so the recorded readiness is stale by
construction rather than by a flag somebody has to remember to set.

**A local report edited to PASS is not evidence.** The checks run again, in a
checkout of the candidate, in a process the candidate does not control, and the
captured artifact is compared with the one the check left behind.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures as fx  # noqa: E402
from test_integration import (  # noqa: E402
    EXIT_BLOCKED, EXIT_CHECK_FAILED, EXIT_INVALID, EXIT_OK,
    apply_edit, one_json_object, passing_manifest_only_candidate, verify, vkit,
)

RAISE_CEILING = (fx.RULES_NAME, fx.RAISE_THE_CEILING_OLD, fx.RAISE_THE_CEILING_NEW)
HARD_CODE_GUARD = (fx.QUOTE_NAME, fx.HARD_CODE_THE_GUARD_OLD, fx.HARD_CODE_THE_GUARD_NEW)


def broken_candidate(repo: Path, branch: str = "broken") -> str:
    """A candidate that fails on its own: both locally reasonable edits, together."""
    fx.checkout_branch(repo, branch, fx.baseline_revision(repo))
    fx.replace_in(repo, *RAISE_CEILING, "let a promotion exceed a hundred percent")
    return fx.replace_in(repo, *HARD_CODE_GUARD, "narrow the floor to ordinary discounts")


# --------------------------------------------------------- a candidate edits its manifest


def test_a_candidate_that_drops_a_required_check_is_refused(tmp_path: Path) -> None:
    """Removing the check from the candidate's manifest must not remove the bar.

    The candidate deletes `quote-behavior` from `verification/manifest.json` and
    keeps the code. The trusted policy still requires it, so the check still
    runs and the defect is still caught -- and the removal is reported as a
    finding rather than discovered by accident.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "manifest-edits", target)
    fx.replace_in(repo, *RAISE_CEILING, "let a promotion exceed a hundred percent")
    fx.replace_in(repo, *HARD_CODE_GUARD, "narrow the floor to ordinary discounts")
    manifest = json.loads((repo / fx.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    manifest["checks"] = []
    fx.write_json(repo / fx.MANIFEST_RELATIVE, manifest)
    candidate = fx.commit_all(repo, "drop the only check")

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED"

    kinds = {f["kind"]: f for f in record["findings"]}
    # An empty checks array is not a manifest that "happens to omit" the check:
    # it is not a manifest at all under the schema, and the finding says so
    # rather than reporting an absence. Either way the decision is REJECTED and
    # the bar is unchanged.
    assert "candidate_manifest_rejected" in kinds, record["findings"]
    assert kinds["candidate_manifest_rejected"]["severity"] == "REJECT"
    assert "non-empty" in kinds["candidate_manifest_rejected"]["detail"]
    assert record["required_checks"] == [fx.CHECK_ID]
    # The bar was not lowered: the checks did not run, and the reason says why.
    assert record["checks"] == []
    assert any("already lowered" in gap for gap in record["gaps"]), record["gaps"]


def test_a_candidate_that_drops_a_required_scenario_is_refused(tmp_path: Path) -> None:
    """Weakening the scenarios a check must report is a policy change too.

    The candidate keeps the check and lowers the number of observations it
    demands, which is the subtler version of the same attack: the check still
    runs, and it just stops looking at the case that catches the defect.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "scenario-edits", target)
    manifest = json.loads((repo / fx.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    kept = [s for s in fx.SCENARIOS if s != "discount-above-ceiling"]
    manifest["checks"][0]["required_scenarios"] = list(kept)
    fx.write_json(repo / fx.MANIFEST_RELATIVE, manifest)
    candidate = fx.commit_all(repo, "stop requiring the above-ceiling scenario")

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    kinds = {f["kind"] for f in record["findings"]}
    assert "required_scenario_removed" in kinds, record["findings"]


def test_a_candidate_that_raises_a_timeout_is_flagged_for_review(tmp_path: Path) -> None:
    """A raised ceiling is not a removal, so it refuses on a different ground.

    The trusted policy still runs the approved command, so the candidate's
    timeout has no effect at all. It is still reported, because a candidate that
    reached for a longer timeout is telling the reviewer something, and the
    protected context refuses on a REVIEW finding rather than absorbing it.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "timeout-edits", target)
    manifest = json.loads((repo / fx.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    manifest["checks"][0]["timeout_seconds"] = 3600
    fx.write_json(repo / fx.MANIFEST_RELATIVE, manifest)
    candidate = fx.commit_all(repo, "give the check an hour")

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    kinds = {f["kind"]: f for f in record["findings"]}
    assert "timeout_raised" in kinds, record["findings"]
    assert kinds["timeout_raised"]["severity"] == "REVIEW"
    assert "120" in kinds["timeout_raised"]["detail"]
    # And the review finding is why it refused, in a protected context.
    assert any("repository owner's review" in gap for gap in record["gaps"]), record["gaps"]


def test_the_approved_manifest_selects_the_command_not_the_candidate(
    tmp_path: Path,
) -> None:
    """The executed manifest is the approved one, so the candidate's *policy
    edits* do not decide what runs.

    The candidate rewrites `verification/manifest.json` to select a completely
    different command -- one that writes an all-PASS artifact and never runs the
    app. The trusted policy pins the approved manifest, so that command is
    never executed: the run executes the approved `verify_price.py`, in the
    candidate's checkout, over the candidate's code.

    What the candidate CAN do is ship a different driver, because the driver is
    repository code and the repository is the thing under test. That is the
    honest boundary. The check is protected from a lowered *policy*; the
    definition of what a passing observation means lives in the repo and moves
    with it. A candidate that rewrites the driver to fabricate a pass is making
    a semantic change the check cannot detect from outside -- and the response
    to that is reviewing the driver, which is a review of the candidate, not a
    weakness in the policy layer. The test asserts the policy layer's actual
    guarantee and stops there.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    approved = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "manifest-edits-command", target)
    manifest = json.loads((repo / fx.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    manifest["checks"][0]["command"] = ["python", "cheat.py", "--out", "{{run_dir}}/result.json"]
    fx.write_json(repo / fx.MANIFEST_RELATIVE, manifest)
    (repo / "cheat.py").write_text("# never run, because the policy pins the approved manifest\n",
                                  encoding="utf-8")
    candidate = fx.commit_all(repo, "point the check at a command that always passes")

    done, record = verify(repo, candidate, target, "@main")
    assert record["manifest"]["approved_revision"] == approved
    # The candidate's own manifest claims a different command; the executed
    # manifest is the approved one, whose command is the real driver.
    assert record["manifest"]["candidate"]["definition"][fx.CHECK_ID]["command"] == [
        "python", "cheat.py", "--out", "{{run_dir}}/result.json"
    ]
    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    for check in record["checks"]:
        report = store.load(check["run_id"])
        executed = report["command"]["argv"]
        assert "verify_price.py" in executed, executed
        assert "cheat.py" not in " ".join(executed), executed
        # The command that ran is the approved one; the candidate's manifest
        # could not move it.
        assert report["configuration_digest"] == record["manifest"]["executed_digest"]


# --------------------------------------------------------- local versus protected


def test_a_local_policy_is_local_evidence_and_says_so(tmp_path: Path) -> None:
    """A user-selected policy decides, refuses, and is never the protected answer."""
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    local_policy = tmp_path / "local-policy.json"
    fx.write_json(local_policy, fx.policy_document())

    done, record = verify(repo, candidate, target, str(local_policy))
    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED"
    assert record["context"] == "local"
    assert record["policy"]["origin"]["source"] == "local-file"
    assert "local evidence" in record["policy"]["origin"]["authority"]

    # The human form says it too, because a person reads the CI log.
    human = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", candidate, "--target", target, "--policy", str(local_policy),
    )
    assert human.returncode == EXIT_OK
    assert "local evidence" in human.stdout
    assert "not a protected integration decision" in human.stdout


def test_a_local_policy_that_claims_protected_is_refused_not_downgraded(
    tmp_path: Path,
) -> None:
    """A local file that asks for protected context is told it cannot have it.

    `--policy` is the authority, not the file's own `context` field. Accepting
    the file while quietly calling it local would leave a reader believing a
    claim that was never honoured, so the request is refused and the way to
    actually ask is named.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    lying = tmp_path / "lying-policy.json"
    fx.write_json(lying, fx.policy_document(context="protected"))

    done = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", candidate, "--target", target, "--policy", str(lying), "--json",
    )
    assert done.returncode == EXIT_INVALID
    error = one_json_object(done)["error"]
    assert "cannot confer protected integration context" in error
    assert "@<ref>" in error


def test_a_protected_policy_must_pin_the_approved_manifest_revision(
    tmp_path: Path,
) -> None:
    """A protected policy with nothing pinned would execute the candidate's own.

    That is the whole attack, so the refusal is at the policy boundary rather
    than at the decision: nothing runs, and the reason names what is missing.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "unpinned", target)
    fx.write_json(repo / fx.POLICY_NAME, fx.policy_document(context="protected"))
    unpinned = fx.commit_all(repo, "a protected policy that pins nothing")

    done = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", unpinned, "--target", target, "--policy", "@unpinned", "--json",
    )
    assert done.returncode == EXIT_INVALID
    assert "must pin manifest_revision" in one_json_object(done)["error"]


# --------------------------------------------------------- the target moves


def test_a_target_that_moves_after_the_decision_makes_the_readiness_stale(
    tmp_path: Path,
) -> None:
    """The plan's third row.

    A candidate is accepted against a target. The target then moves. The
    recorded readiness still names the target it was tested against, and that
    commit is no longer what the branch resolves to -- so the readiness is stale
    by construction, and a caller checking `readiness_at_publish` before
    publishing has to notice.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_OK, record
    tested = record["readiness_at_publish"]["target_tested"]
    assert tested == target
    # The target is named as a commit, not as a branch, so the tested target is
    # a fact the record carries rather than a name that can move under it.
    assert tested != fx.main_revision(repo), "the fixture's main must differ from the baseline"

    # The target moves, on a branch off the tested target.
    fx.git(repo, "checkout", "-q", "main")
    moved = fx.commit_all(repo, "someone else's change lands")
    assert fx.git(repo, "rev-parse", "main") != tested

    # The recorded readiness does not follow. It names the commit, and that
    # commit is not the branch any more.
    assert record["readiness_at_publish"]["target_tested"] == tested
    assert record["readiness_at_publish"]["recheck_before_publish"] == "target"
    assert fx.git(repo, "rev-parse", "main") != record["readiness_at_publish"]["target_tested"]

    # And a fresh candidate built on the moved target is a different decision
    # with a different acceptance id, because the target is inside the id.
    rebuilt = apply_edit(repo, RAISE_CEILING, "rebuilt", "a change on the new base")
    _, second = verify(repo, rebuilt, moved, "@main")
    assert second["readiness_at_publish"]["target_tested"] == moved
    assert second["acceptance_id"] != record["acceptance_id"]


def test_the_same_decision_asked_twice_records_one_acceptance(tmp_path: Path) -> None:
    """A retry recomputes the same id, so it cannot write a second decision.

    The id is a digest of the repository, the candidate, the target, the policy
    digest, the context and the required checks. Anything that would make a
    different decision is inside it.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    _, first = verify(repo, candidate, target, "@main")
    _, second = verify(repo, candidate, target, "@main")
    assert first["acceptance_id"] == second["acceptance_id"]
    assert first["decision"] == second["decision"] == "ACCEPTED"

    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    recorded = store.load_acceptance(first["acceptance_id"])
    assert recorded is not None
    assert recorded["candidate"] == candidate
    assert recorded["required_checks"] == [fx.CHECK_ID]
    assert len(store.list_acceptances()) == 1


# --------------------------------------------------------- the report is edited


def test_a_local_report_edited_to_pass_does_not_survive_the_protected_path(
    tmp_path: Path,
) -> None:
    """The plan's fifth row, run against a real failure.

    A failing candidate is verified once, so a report exists on disk that says
    FAIL. That report is then edited to say PASS -- the shape of a developer
    "fixing" the evidence rather than the code. The next verification does not
    read it: it runs the checks again, in a fresh checkout of the candidate,
    under a launcher the candidate's code cannot reach, and rejects the candidate
    again.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = broken_candidate(repo, "cheater")

    done, first = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, first
    run_id = first["checks"][0]["run_id"]

    # Edit the stored report to claim a pass, exactly as a well-meaning person
    # with a text editor would.
    report_path = repo / ".git" / "verification-kit" / "runs" / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["outcome"]["result"] == "FAIL"
    report["outcome"] = {
        "result": "PASS",
        "scenarios": [
            {"id": s, "result": "PASS", "observation": "looks fine to me"}
            for s in fx.SCENARIOS
        ],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # The edited report is on disk and claims a pass.
    assert json.loads(report_path.read_text(encoding="utf-8"))["outcome"]["result"] == "PASS"

    # The protected path does not believe it.
    done, second = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, second
    assert second["decision"] == "REJECTED"
    assert "expected 0, printed -1000" in second["checks"][0]["detail"]
    # And it is not the same run: the decision rests on a run whose recorded
    # source head is the candidate and whose provenance names the launcher.
    assert second["checks"][0]["run_id"] != run_id or second["checks"][0]["result"] == "REJECTED"

    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    fresh = store.load(second["checks"][0]["run_id"])
    assert fresh["outcome"]["result"] == "FAIL"
    assert fresh["source"]["head"] == candidate
    assert fresh["provenance"]["mode"] == "trusted_integration"
    assert fresh["provenance"]["verifier_revision"]


def test_an_artifact_claiming_pass_for_a_failing_check_is_rejected_by_the_code(
    tmp_path: Path,
) -> None:
    """A driver that hard-codes PASS does not make the check pass.

    The driver writes an all-PASS artifact without running the app at all, and
    exits zero. The schema accepts it -- it is a well-formed claim -- so the
    only thing that can catch it is the real process: the check's artifact
    claims a pass, but the driver's own exit and the absence of any execution
    are the evidence, and the required-scenario comparison against the approved
    manifest catches the one whose expectations the real code would have
    contradicted.

    What this row is really about is the honest one: the trusted path executes
    the approved command, so a candidate cannot swap in a driver that reports
    what it likes. The driver here IS the approved driver, edited to lie, and
    the run is still rejected because the ceiling it lies about is now 900.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    # A driver that fabricates a pass without running the app. It is
    # well-formed and exits zero, so nothing about the artifact itself is wrong.
    liar = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "\n"
        f"SCENARIOS = {list(fx.SCENARIOS)!r}\n"
        "\n"
        "def main():\n"
        "    out = Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "    out.write_text(json.dumps({'schema_version': 1, 'scenarios': [\n"
        "        {'id': s, 'result': 'PASS', 'observation': 'reported without running'}\n"
        "        for s in SCENARIOS]}), encoding='utf-8')\n"
        "    return 0\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )

    fx.checkout_branch(repo, "reports-without-running", target)
    (repo / fx.DRIVER_NAME).write_text(liar, encoding="utf-8")
    candidate = fx.commit_all(repo, "a driver that reports a pass without running the app")

    done, record = verify(repo, candidate, target, "@main")
    # The approved manifest's command is what runs, and the approved driver is
    # what it names. A candidate that rewrote the driver changed the file, not
    # the command, so the real driver still runs and still executes the app.
    # The fabricated artifact is therefore not even produced; the run is
    # decided on the approved driver's honest observation.
    assert record["manifest"]["approved_revision"] == target
    assert done.returncode in (EXIT_CHECK_FAILED, EXIT_BLOCKED, EXIT_OK), record
    # And whichever way it lands, the decision is not ACCEPTED on the strength
    # of a fabricated report: the executed command named the real driver.
    assert record["manifest"]["executed_digest"] is not None
    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    for check in record["checks"]:
        report = store.load(check["run_id"])
        executed = report["command"]["argv"]
        assert "verify_price.py" in executed[1], executed
        # The driver's own stdout, not its artifact, is what the run trusts.
        assert report["provenance"]["mode"] == "trusted_integration"


def test_the_candidate_cannot_import_the_verifier_from_inside_a_check(
    tmp_path: Path,
) -> None:
    """The driver's own process cannot reach the code that reads its verdict.

    A driver that tries to import vkit gets an ImportError, and the check is
    BLOCKED rather than passed. The refusal is the boundary: the verifier can
    import vkit, and the code under test cannot.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    original = fx.driver_source()
    reaching = original.replace(
        "CASES = ",
        "import vkit  # the code under test must not be able to read its own verdict\nCASES = ",
        1,
    )
    assert reaching != original

    fx.checkout_branch(repo, "reaches-for-vkit", target)
    (repo / fx.DRIVER_NAME).write_text(reaching, encoding="utf-8")
    candidate = fx.commit_all(repo, "a driver that imports the verifier")

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_BLOCKED, record
    assert record["decision"] == "BLOCKED"
    assert "artifact" in record["checks"][0]["detail"] or record["checks"][0]["result"] == "BLOCKED"

    # The refusal reached the log, which is where a developer would look for it.
    from vkit.paths import open_project

    logs = list(open_project(repo).state_root.rglob("stderr.log"))
    text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)
    assert "vkit is not importable inside a trusted integration check" in text

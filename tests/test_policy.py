"""The candidate cannot lower its own bar, cannot supply the bar, and cannot
re-decide what was already decided.

Six properties, each driven through the public command.

**The policy comes from trusted configuration.** The required set and the
manifest the checks actually run are read from the approved reference before the
candidate is checked out, so a candidate that edits `verification/manifest.json`
to drop a check changes nothing about what runs. The candidate's own manifest is
then compared, and a difference is a finding that refuses in the protected
context rather than a quiet pass.

**The verification code is the approved bytes, not the candidate's.** A manifest
that names a script by path is not thereby trusted, because the path resolves
inside the candidate. The script is measured at both the approved revision and
the candidate, a difference refuses, and the approved bytes are what execute
when they agree. The way out of that refusal is the policy pin itself: the same
commit is accepted under a policy that approves its driver, and a further edit
is refused again. Trust is exactly as wide as the pin.

**A receipt is decided once.** The acceptance id covers the candidate, the
target, the policy and the measured verification context, so a repeated request
recomputes the same id and returns the recorded decision -- the stored record,
not a fresh copy. An observation that reaches a different decision under that id
is a contradiction: it is recorded under its own id, answers BLOCKED, and leaves
the recorded acceptance untouched.

**Local is not weak, it is scoped.** A policy supplied as a path produces a real
refusal, and it is never the protected integration decision. Both facts are in
every record, so nobody has to guess which kind of answer they are reading.

**A target that moves invalidates the readiness.** The tested target is recorded
with the decision, and the command states that publishing requires re-checking
it. The test moves the target after the decision and shows that the recorded
target no longer resolves to the branch, so the recorded readiness is stale by
construction rather than by a flag somebody has to remember to set.

**A local report edited to PASS is not evidence.** The approved checks run again
in a checkout of the candidate, and the answer rests on that fresh run rather
than on the edited file. The second run is a different candidate on purpose: a
replay of the same request returns the recorded decision without running
anything, which is correct and would prove nothing about a second execution.
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
from vkit.integration.verify import _conflict_id, _settle, summarize  # noqa: E402

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
    never executed. The run executes the approved revision's copy of
    `verify_price.py`, materialized outside the candidate checkout, with the
    candidate's product code under it.

    The manifest the candidate ships is still read and recorded, so a reviewer
    can see the edit that was refused. Editing the driver itself is a different
    attack, refused outright; see `test_a_candidate_that_edits_the_driver_is_refused`.
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
    assert done.returncode == EXIT_OK, record
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
        # The driver that ran is the approved revision's copy, materialized in a
        # verifier-owned directory the candidate cannot write to.
        assert Path(executed[1]).name == fx.DRIVER_NAME, executed
        assert "approved" in Path(executed[1]).parts, executed
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
    readiness = record["manifest"]["readiness_at_publish"]
    tested = readiness["target_tested"]
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
    assert record["manifest"]["readiness_at_publish"]["target_tested"] == tested
    assert record["manifest"]["readiness_at_publish"]["recheck_before_publish"] == "target"
    assert fx.git(repo, "rev-parse", "main") != readiness["target_tested"]

    # And a fresh candidate built on the moved target is a different decision
    # with a different acceptance id, because the target is inside the id.
    rebuilt = apply_edit(repo, RAISE_CEILING, "rebuilt", "a change on the new base")
    _, second = verify(repo, rebuilt, moved, "@main")
    assert second["manifest"]["readiness_at_publish"]["target_tested"] == moved
    assert second["acceptance_id"] != record["acceptance_id"]


# --------------------------------------------------------- the report is edited


def test_a_local_report_edited_to_pass_does_not_survive_the_protected_path(
    tmp_path: Path,
) -> None:
    """The plan's fifth row, run against a real failure.

    A failing candidate is verified once, so a report exists on disk that says
    FAIL. That report is then edited to say PASS -- the shape of a developer
    "fixing" the evidence rather than the code. A second verification of a
    DIFFERENT candidate over the same defect does not read it: it runs the
    approved driver's checks again, in a fresh checkout, under a launcher the
    candidate's code cannot reach, and rejects again.

    The second candidate is a different commit on purpose. Re-verifying the same
    commit is a replay, and a replay returns the recorded decision without
    running anything -- which is the correct answer for a replay and would make
    this test assert nothing about a second run.
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

    # A second candidate carrying the same defect, so the answer must come from
    # a fresh execution rather than from the record.
    fx.git(repo, "commit", "-q", "--allow-empty", "-m", "an unrelated change on the defect")
    second_candidate = fx.git(repo, "rev-parse", "HEAD")
    assert second_candidate != candidate

    # The protected path does not believe the edited report.
    done, second = verify(repo, second_candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, second
    assert second["decision"] == "REJECTED"
    assert "expected 0, printed -1000" in second["checks"][0]["detail"]
    # And it is a different run from the edited one.
    assert second["checks"][0]["run_id"] != run_id

    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    fresh = store.load(second["checks"][0]["run_id"])
    assert fresh["outcome"]["result"] == "FAIL"
    assert fresh["source"]["head"] == second_candidate
    assert fresh["provenance"]["mode"] == "trusted_integration"
    assert fresh["provenance"]["verifier_revision"]


def test_a_policy_pin_approves_the_driver_bytes_and_nothing_earlier_does(
    tmp_path: Path,
) -> None:
    """The refusal names its way out, and the way out is the policy pin.

    A driver that reports a pass without running the app is the same attack as
    one that edits the expectations, and both are refused here for the same
    reason: the bytes differ from the approved revision. That is the right answer
    but it is not the only one, and a refusal nobody can act on is a dead end.

    So the same commit is verified again under a policy that pins the candidate's
    own driver as the approved one. The attack now succeeds -- which is correct,
    because the policy pin is the explicit review that establishes new trusted
    bytes, and refusing it would mean the bar could never be changed by anyone.
    What the second run shows is that the trust is exactly as wide as the pin:
    the falsified candidate is ACCEPTED, and a candidate that edits the now
    approved driver again is refused. There is no widening; each approval
    approves one revision's bytes.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

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

    # Under the approved policy: refused, no check runs, and the refusal says
    # which file and which revision.
    done, refused = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, refused
    assert refused["decision"] == "REJECTED"
    assert refused["checks"] == [], "a refused oracle must not run a check"
    changed = [f for f in refused["findings"] if f["kind"] == "checker_bytes_changed"]
    assert len(changed) == 1, refused["findings"]
    assert changed[0]["severity"] == "REJECT"
    assert changed[0]["check_id"] == fx.DRIVER_NAME
    assert target[:12] in changed[0]["detail"]
    # Both sides of the comparison are recorded, so the refusal is checkable
    # rather than an assertion.
    context = refused["verifier"]["verification_context"]
    assert context["approved_revision"] == target
    assert context["approved_digests"][fx.DRIVER_NAME] != context["candidate_digests"][fx.DRIVER_NAME]

    # The approval: a policy that pins that commit's driver as the trusted one.
    # That is the explicit review, and it is the same mechanism the refusal
    # named -- no second registry, no flag. It is named as a commit rather than
    # as a branch, because an approval is a fixed point: a branch that moved
    # would make the trusted bytes a function of when the run happened.
    fx.write_json(repo / fx.POLICY_NAME, fx.policy_document(manifest_revision=candidate))
    approving = fx.commit_all(repo, "approve the rewritten driver")
    # The candidate is rebuilt on the approval, because a candidate is always
    # the target plus changes and the approval is now the target.
    fx.git(repo, "commit", "-q", "--allow-empty", "-m", "a change on the approved base")
    rebuilt = fx.git(repo, "rev-parse", "HEAD")
    done, approved = verify(repo, rebuilt, approving, f"@{approving}")
    assert done.returncode == EXIT_OK, approved
    assert approved["decision"] == "ACCEPTED", approved
    context = approved["verifier"]["verification_context"]
    # The pin approves the driver's bytes at the commit that introduced them,
    # and the record says so rather than naming the policy that names it.
    assert context["approved_revision"] == candidate
    assert context["source"].startswith("approved-manifest")
    assert context["approved_digests"] == context["candidate_digests"]

    # The falsified check really did run, and the artifact it produced is the
    # evidence -- because under this pin the driver IS the approved code.
    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    report = store.load(approved["checks"][0]["run_id"])
    assert report["outcome"]["result"] == "PASS"
    assert all(s["observation"] == "reported without running" for s in report["outcome"]["scenarios"])

    # And the pin approves one revision's bytes and no more: an edit on top of
    # the now-approved driver is refused again, by the same rule.
    (repo / fx.DRIVER_NAME).write_text(liar + "# edited again\n", encoding="utf-8")
    later = fx.commit_all(repo, "edit the driver again")
    done, again = verify(repo, later, approving, f"@{approving}")
    assert done.returncode == EXIT_CHECK_FAILED, again
    assert again["decision"] == "REJECTED"
    assert again["checks"] == []
    assert [f["kind"] for f in again["findings"]] == ["checker_bytes_changed"], again["findings"]


def test_a_repeated_request_returns_the_recorded_decision_and_names_its_source(
    tmp_path: Path,
) -> None:
    """Re-verifying the same thing returns what was recorded, not a new opinion.

    The acceptance id is derived from the candidate, the target, the policy, the
    context AND the measured verification context -- the verifier's own bytes, the
    interpreter, the fixture digests. So an identical request recomputes the same
    id, and the answer is the one already in the table: a second CI run does not
    get to re-decide, and a decision cannot change because the run was repeated.

    What the replay returns is the STORED record, not the copy the second run
    built. That is the part worth asserting: the run ids and the decided_at
    timestamp come from the first execution, so nothing in the returned payload
    claims a check ran twice.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    done, first = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_OK, first
    first_id = first["acceptance_id"]

    done, second = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_OK, second
    assert second["acceptance_id"] == first_id
    assert second["decision"] == first["decision"] == "ACCEPTED"
    assert second["decided_at"] == first["decided_at"]
    assert [c["run_id"] for c in second["checks"]] == [c["run_id"] for c in first["checks"]]

    # The returned record is the durable one, not a fresh copy: it is what the
    # table holds, and nothing in it was written twice.
    from vkit.paths import open_project
    from vkit.storage import Store

    store = Store(open_project(repo).db_path)
    stored = store.load_acceptance(first_id)
    assert stored["decision"] == "ACCEPTED"
    assert stored["decided_at"] == first["decided_at"]
    assert stored["checks"][0]["run_id"] == first["checks"][0]["run_id"]
    assert len(store.list_acceptances()) == 1

    # The human form says which acceptance it is reporting, so a log reader can
    # match it to the table.
    human = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", candidate, "--target", target, "--policy", "@main",
    )
    assert human.returncode == EXIT_OK
    assert f"recorded as {first_id}" in human.stdout, human.stdout


def test_a_second_observation_that_contradicts_a_receipt_is_kept_beside_it(
    tmp_path: Path,
) -> None:
    """A receipt id never comes back with a different decision.

    The first run records ACCEPTED. The second run reaches REJECTED under the
    same id -- which happens for real whenever something outside the identity
    changes: the run directory is emptied, a stale artefact, an environment the
    id cannot see. Choosing either observation silently would be picking a
    winner, so the run records the contradiction as its OWN acceptance,
    answers BLOCKED, and retains the recorded one. Both are readable afterwards.

    Note what is NOT the trigger: the id covers the measured context, so a real
    change to the verifier, the interpreter or the fixture produces a different
    id and an ordinary separate decision. The conflict path is for the residual
    case, and it is worth being loud rather than tidy about it.
    """
    from vkit.paths import open_project
    from vkit.storage import Store

    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    done, accepted = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_OK, accepted
    recorded_id = accepted["acceptance_id"]
    store = Store(open_project(repo).db_path)
    assert store.load_acceptance(recorded_id)["decision"] == "ACCEPTED"

    # The same identity, a contradicting observation. This is the state a real
    # contradiction leaves behind: an id already holding a decision.
    contradictory = {
        **accepted,
        "integration_id": "a-different-integration",
        "decision": "REJECTED",
        "gaps": ["the required checks were not run: the trusted verification code and the "
                 "approved policy do not both hold for this candidate, so running them "
                 "would verify a bar the candidate had already lowered"],
        "findings": [{"kind": "checker_bytes_changed", "check_id": fx.DRIVER_NAME,
                      "severity": "REJECT",
                      "detail": "the candidate's verification code differs from the approved "
                                "revision the policy pins"}],
        "checks": [],
        "decided_at": "2026-01-01T00:00:00+00:00",
    }
    expected_id = _conflict_id(recorded_id, contradictory)
    assert expected_id != recorded_id, "a conflict must not reuse the id it contradicts"

    settled = _settle(Store(open_project(repo).db_path), recorded_id, contradictory)

    # BLOCKED, under its own id. Not the recorded id with a different decision:
    # the one outcome the invariant forbids.
    assert settled.decision == "BLOCKED"
    assert settled.acceptance_id == expected_id
    assert settled.acceptance_id != recorded_id

    # Both observations survive. The recorded one is untouched, which is the
    # property that matters: nothing overwrote the acceptance a reader already
    # has a reference to.
    assert store.load_acceptance(recorded_id)["decision"] == "ACCEPTED"
    assert store.load_acceptance(expected_id)["decision"] == "REJECTED"
    assert len(store.list_acceptances()) == 2

    # The contradiction names both decisions and the acceptance it disagrees
    # with, and it survives a round trip through the table -- so a reader who
    # finds the second row knows to distrust both, not just the second.
    retained = store.load_acceptance(expected_id)
    assert retained["gaps"][0].startswith("CONFLICT:")
    assert recorded_id in retained["gaps"][0]
    assert "ACCEPTED" in retained["gaps"][0]
    assert "REJECTED" in retained["gaps"][0]

    # And the human form says it, because "BLOCKED" alone would not tell a
    # reader why.
    text = summarize(settled)
    assert "CONFLICT" in text
    assert recorded_id in text


def test_candidate_code_cannot_import_the_verifier_from_inside_a_check(
    tmp_path: Path,
) -> None:
    """The code the check runs cannot reach the code that reads its verdict.

    A driver that tried this would be refused before it ran, by the oracle check
    -- a candidate cannot edit the verification code. So the reach comes from the
    product, which a candidate is allowed to change, and that is the reach that
    matters: the product is imported by the same process as the driver, in a
    subprocess the candidate fully controls, and it still cannot import vkit.

    The observation is FAIL rather than BLOCKED, because the driver is the
    approved one and it faithfully recorded a scenario that crashed. A driver
    reporting its own verdict is a separate attack, and the test above it is the
    one that answers it.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    # The reach is in the product module the approved driver imports, and the
    # product is what a candidate owns.
    fx.checkout_branch(repo, "reaches-for-vkit", target)
    (repo / fx.QUOTE_NAME).write_text(
        "import vkit  # the code under test must not reach the code that reads its result\n"
        + (repo / fx.QUOTE_NAME).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    candidate = fx.commit_all(repo, "a product module that imports the verifier")

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED"
    # The approved driver ran and every scenario failed on the import, so the
    # decision is the driver's honest observation and not a refused oracle.
    assert record["checks"][0]["result"] == "REJECTED"
    assert record["checks"][0]["detail"].startswith("no-discount: exited 1: Traceback"), record

    # The observation is the driver reporting a crashed scenario, and it
    # truncates the traceback that would say why. The reason is checked here
    # instead, in the process the launcher creates, with the same flag: the
    # refusal is the import guard and not a consequence of the candidate's edit.
    env = dict(os.environ, VKIT_TRUSTED_LAUNCHER="1")
    inside = subprocess.run(
        [sys.executable, "-c", "import vkit"], capture_output=True, text=True,
        env=env, cwd=str(repo),
    )
    assert inside.returncode != 0
    assert "vkit is not importable inside a trusted integration check" in inside.stderr

    # Without the launcher's flag the same import succeeds, so it is the flag and
    # not this repository that refuses.
    outside = subprocess.run(
        [sys.executable, "-c", "import vkit"], capture_output=True, text=True,
        env={k: v for k, v in env.items() if k != "VKIT_TRUSTED_LAUNCHER"},
        cwd=str(repo),
    )
    assert outside.returncode == 0, outside.stderr

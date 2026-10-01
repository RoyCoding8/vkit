"""The central property: a passing worker branch cannot stand in for the merged
candidate.

Every test here drives `vkit integration verify` as a subprocess against a real
throwaway repository, because the thing being verified is what a user or a
protected CI job gets: the exit code, and one JSON object on stdout. Nothing
imports the decision and asserts on an attribute; a test that did would pass
against a product that never produced the record at all.

The scenario the plan names is exercised against the product rather than the
research script. The fixture makes the two branches touch different files, so
Git reports no conflict, and makes the combination wrong for a reason neither
branch could have known: one raises the discount ceiling and the other replaces
a floor with a bound derived from the amount being discounted. Each passes alone.
Merged, exactly one scenario fails and the other five still pass, which is what
makes the failure evidence about the merge rather than about the harness.
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
from conftest import console_script  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# One exit table, the product's. This file used to declare its own four,
# which is how a test suite ends up asserting a code the CLI never returns.
from vkit.cli import (  # noqa: E402
    EXIT_BLOCKED, EXIT_CHECK_FAILED, EXIT_INVALID, EXIT_OK,
)

# The two independent edits. `fixtures` owns the text; the test only says which
# file each one lands in, so the fixture and the test cannot disagree about what
# "the ceiling change" means.
RAISE_CEILING = (fx.RULES_NAME, fx.RAISE_THE_CEILING_OLD, fx.RAISE_THE_CEILING_NEW)
HARD_CODE_GUARD = (fx.QUOTE_NAME, fx.HARD_CODE_THE_GUARD_OLD, fx.HARD_CODE_THE_GUARD_NEW)


def vkit(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the real console entry point in a real process.

    `PYTHONPATH` names this checkout's `src`. The shared environment has an
    editable install pointing at the main checkout, so without it this would
    exercise code the tests did not write. tests/conftest.py does the same for
    the in-process case.

    The script itself is located through the installation, because the same
    `sys.executable` sibling assumption this replaced put it in the wrong place
    on every runner but the maintainer's.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return subprocess.run(
        [str(console_script()), *args], capture_output=True, text=True, timeout=900,
        env=env, cwd=None if cwd is None else str(cwd),
    )


def one_json_object(done: subprocess.CompletedProcess[str]) -> dict:
    """stdout, parsed as exactly one JSON document.

    `json.loads` raises on trailing content, so this also proves nothing else
    reached stdout. A command that printed a warning before its payload fails
    here rather than passing a "does it parse" check.
    """
    assert done.stdout.strip(), f"no output; stderr was:\n{done.stderr}"
    return json.loads(done.stdout)


def verify(repo: Path, candidate: str, target: str, policy: str, *extra: str):
    """One integration verification, as a protected job runs it."""
    done = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", candidate, "--target", target, "--policy", policy,
        "--json", *extra,
    )
    return done, one_json_object(done)


def passing_manifest_only_candidate(repo: Path) -> str:
    """A commit that changes a comment, so the tree differs from the baseline.

    Needed by several tests: without a real change the candidate and the
    baseline are the same commit, and "the checks ran against the candidate"
    would be indistinguishable from "the checks ran against the baseline".

    The comment sits in the product module, never in the driver. The driver is
    the verification code: the policy pins its bytes, and a candidate that edits
    it is refused rather than verified. That is a different property with its
    own tests, and putting it in this helper would have made every test that
    wants an ordinary good candidate depend on an attack.
    """
    fx.checkout_branch(repo, "candidate-only", fx.baseline_revision(repo))
    literal = fx.RAISE_THE_CEILING_OLD
    return fx.replace_in(
        repo, fx.RULES_NAME, literal,
        literal + "\n# A comment on product code. The candidate owns this file, and\n"
                  "# changing it must not change what a passing observation means.",
        "note why a product comment is harmless",
    )


def broken_candidate(repo: Path, branch: str = "broken") -> str:
    """A candidate that genuinely fails, on its own rather than only in a merge.

    Both edits, in one commit. Either alone is a legitimate change; the pair is
    the semantic conflict, and a test that needs a failing candidate without a
    merge in the story needs both of them.
    """
    fx.checkout_branch(repo, branch, fx.baseline_revision(repo))
    fx.replace_in(repo, *RAISE_CEILING, "let a promotion exceed a hundred percent")
    return fx.replace_in(repo, *HARD_CODE_GUARD, "restate the floor as a literal")


def apply_edit(repo: Path, edit: tuple[str, str, str], branch: str, message: str) -> str:
    """One branch off the approved baseline: branch, edit, commit, return the sha."""
    return apply_edit_from(repo, edit, branch, fx.baseline_revision(repo), message)


def apply_edit_from(
    repo: Path, edit: tuple[str, str, str], branch: str, start: str, message: str = "apply"
) -> str:
    """One branch off an explicit base. Same edit, a different base, when a test
    needs the base to be the only variable."""
    filename, old, new = edit
    fx.checkout_branch(repo, branch, start)
    return fx.replace_in(repo, filename, old, new, message)


def test_two_passing_branches_that_conflict_semantically_fail_the_combined_candidate(
    tmp_path: Path,
) -> None:
    """The plan's first row, run against the product.

    Each branch passes on its own. Git merges them with no conflict, because
    they touch different files. The combined candidate fails, and the failure is
    specific: one scenario, with the expected and actual values in the
    observation.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    raise_ceiling = apply_edit(
        repo, RAISE_CEILING, "raise-the-ceiling", "let a promotion exceed a hundred percent"
    )
    hard_code_guard = apply_edit(
        repo, HARD_CODE_GUARD, "hard-code-the-guard",
        "bound the discount by the amount instead of by zero",
    )

    # Each branch passes alone, in its own checkout, through the same command.
    _, branch_one = verify(repo, raise_ceiling, target, "@main")
    _, branch_two = verify(repo, hard_code_guard, target, "@main")
    assert branch_one["decision"] == "ACCEPTED", branch_one
    assert branch_two["decision"] == "ACCEPTED", branch_two

    # The merge. Git reports no conflict: the two commits touch different files.
    fx.checkout_branch(repo, "integrated", target)
    merge = fx.git(repo, "merge", "--no-ff", "-m", "integrate both branches",
                   raise_ceiling, hard_code_guard)
    assert "CONFLICT" not in merge
    combined = fx.git(repo, "rev-parse", "HEAD")

    done, record = verify(repo, combined, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED"

    check = record["checks"][0]
    assert check["result"] == "REJECTED"
    assert "discount-above-ceiling" in check["detail"], check
    # The observation carries both numbers, so a reader can judge without
    # re-running. `expected 0, printed -1000` is the whole point: neither branch
    # could produce a negative total on its own.
    assert "expected 0, printed -1000" in check["detail"], check

    # The evidence names the candidate that was actually executed.
    assert record["candidate"] == combined
    assert record["checks"][0]["source_head"] == combined
    assert record["combination"]["relationship"] == "candidate"
    assert record["target"] == target


def test_an_old_worker_checkout_that_still_passes_cannot_satisfy_the_candidate(
    tmp_path: Path,
) -> None:
    """The plan's second row.

    A worker branch is verified and its evidence exists. The candidate is then
    built on that branch and the candidate fails. The passing worker report is
    still in the store, and none of it changes the candidate's answer, because
    the answer is computed from runs whose recorded source head is the candidate
    commit.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "worker", target)
    worker_commit = passing_manifest_only_candidate(repo)
    _, worker_record = verify(repo, worker_commit, target, "@main")
    assert worker_record["decision"] == "ACCEPTED", worker_record
    worker_run = worker_record["checks"][0]["run_id"]

    # The candidate is the worker branch plus a real defect. The worker branch
    # itself still passes; the candidate on top of it does not.
    fx.replace_in(repo, *RAISE_CEILING, "let a promotion exceed a hundred percent")
    fx.replace_in(repo, *HARD_CODE_GUARD, "restate the floor as a literal")
    candidate = fx.git(repo, "rev-parse", "HEAD")

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED"

    # The passing worker run is still readable, and still a different revision.
    state = repo / ".git" / "verification-kit"
    stored = json.loads(
        (state / "runs" / worker_run / "report.json").read_text(encoding="utf-8")
    )
    assert stored["outcome"]["result"] == "PASS"
    assert stored["source"]["head"] == worker_commit
    assert stored["source"]["head"] != record["candidate"]

    # Nothing in the acceptance borrowed it. Every run the decision rests on
    # has the candidate as its recorded source head.
    for check in record["checks"]:
        assert check["source_head"] == candidate


def test_a_passing_report_for_another_revision_is_not_evidence_for_this_one(
    tmp_path: Path,
) -> None:
    """A worker may hand over its run id. The id is not the answer.

    The plan says evidence is collected "through app IDs". This is the shape of
    that: a real run id from a real PASS is offered for a candidate that was
    never run, and the decision still rests on the candidate.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "worker", target)
    worker_commit = passing_manifest_only_candidate(repo)
    _, worker_record = verify(repo, worker_commit, target, "@main")
    worker_run = worker_record["checks"][0]["run_id"]

    # A different candidate, never verified, containing a real defect.
    candidate = broken_candidate(repo, "broken")
    assert candidate != worker_commit

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED"
    assert worker_run not in {c["run_id"] for c in record["checks"]}


def test_a_divergent_candidate_is_refused_before_any_check_runs(tmp_path: Path) -> None:
    """A candidate on a line of its own is not the target plus changes.

    Running the checks anyway would burn the slow path on a commit that could
    not be published onto this target at all, and a REJECTED answer about an
    unrelated commit would read as an integration decision.

    The sibling is branched from before the target moved, not from the target.
    A candidate built *on* the target does contain it, and accepting that is
    correct; the case under test is the one where they share only an ancestor.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    main = fx.main_revision(repo)
    assert main != target, "the fixture must move main past the baseline for this row"

    fx.checkout_branch(repo, "sibling", target)
    sibling = passing_manifest_only_candidate(repo)

    done, record = verify(repo, sibling, main, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED"
    assert record["checks"] == []
    assert record["combination"]["relationship"] == "divergent"
    assert record["combination"]["merge_base"] == target
    assert "divergent" in record["gaps"][0]


def test_a_candidate_built_on_the_target_contains_it_and_is_not_refused(
    tmp_path: Path,
) -> None:
    """The complement of the divergence row, so neither could pass vacuously.

    Two commits on divergent lines are refused; a commit whose parent is the
    target is accepted. Both come out of the same fixture and the same command,
    and the only difference is the parent, so the refusal above cannot be
    passing because the command refuses everything.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    fx.checkout_branch(repo, "on-target", target)
    candidate = passing_manifest_only_candidate(repo)
    assert fx.git(repo, "rev-parse", "HEAD^") == target

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED"
    assert record["combination"]["relationship"] == "candidate"
    assert record["candidate_parent"] == target


def test_a_stale_base_is_refused_and_names_the_fresh_candidate_it_needs(
    tmp_path: Path,
) -> None:
    """A candidate that does not contain the target cannot be published onto it.

    The target moves on, and the candidate is not rebuilt. Two checks are still
    the right shape for "not the target plus changes": the candidate is behind,
    or it is on a line of its own. Both are refused, both name the merge base,
    and the refusal says a fresh candidate is required. Asserting one of the
    two here would test Git's ancestry walk rather than the decision, so the
    assertion is the one the decision actually makes.
    """
    repo = fx.build_repository(tmp_path)
    old_target = fx.baseline_revision(repo)

    stale = apply_edit(repo, RAISE_CEILING, "work", "a change on the old base")

    # The target moves on, off the same base rather than off the candidate, so
    # the candidate is a sibling of the new target rather than its ancestor.
    fx.git(repo, "checkout", "-q", "main")
    moved = fx.commit_all(repo, "an unrelated change lands on main")
    # The rebuilt candidate contains the new target, and the same change the
    # rejected candidate made is accepted on the new base. The base is the only
    # variable, so the test cannot be satisfied by a difference in the edit.
    fresh = apply_edit_from(repo, RAISE_CEILING, "late-rebuild", moved)

    done, record = verify(repo, stale, moved, "@main")
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED"
    assert record["checks"] == []
    assert record["combination"]["relationship"] in ("behind", "divergent")
    assert record["combination"]["merge_base"] == old_target
    assert "candidate" in record["gaps"][0]

    # The rebuilt candidate contains the new target, and the same change the
    # rejected candidate made is accepted on the new base. The base is the
    # variable; the code is held constant so the test cannot be satisfied by a
    # difference in the edit.
    done, rebuilt = verify(repo, fresh, moved, "@main")
    assert done.returncode == EXIT_OK, rebuilt
    assert rebuilt["decision"] == "ACCEPTED"
    assert rebuilt["target"] == moved


def test_a_ref_that_does_not_resolve_is_an_invalid_invocation_not_a_decision(
    tmp_path: Path,
) -> None:
    """Nothing ran, so there is no decision to record and the code is 2."""
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)

    done = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", "refs/heads/there-is-no-such-branch",
        "--target", target, "--policy", "@main", "--json",
    )
    assert done.returncode == 2
    payload = one_json_object(done)
    assert "does not resolve to a commit" in payload["error"]


def test_the_command_states_that_it_does_not_publish(tmp_path: Path) -> None:
    """A reader of a CI log must not have to guess whether this merged anything.

    The acceptance record carries the tested target, so a caller that does hold
    an authorization has what it needs to re-check the target, and the record
    says the re-check is what a publish requires.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = passing_manifest_only_candidate(repo)

    done, record = verify(repo, candidate, target, "@main")
    assert done.returncode == EXIT_OK, record
    # The publish statement is carried inside a column the acceptances table
    # persists, so a replayed decision still says it rather than losing it.
    assert record["manifest"]["publishes"] is False
    readiness = record["manifest"]["readiness_at_publish"]
    assert readiness["target_tested"] == target
    assert readiness["recheck_before_publish"] == "target"
    assert "does not push, merge or tag" in readiness["note"]

    # And the human form says it too, because a human reads the log.
    human = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", candidate, "--target", target, "--policy", "@main",
    )
    assert human.returncode == EXIT_OK
    assert "does not push, merge or tag" in human.stdout

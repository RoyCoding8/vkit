"""The protected integration decision asks the candidate for the cleanup it owes.

One gate, driven through the real command. Plan 11 line 79 is the whole of it:
protected integration checks are non-mutating, they verify the exact candidate
revision, and a candidate whose required cleanup is missing is told to make a new
candidate commit rather than having its checkout cleaned first.

**Why this gate is scoped by the diff against the target.** Checkpoint 11.3 named
`cleanup.candidate_gaps(checkout_project)` at the `gaps.extend(...)` sites beside
`checkouts.assert_clean(...)`. That seam cannot fire, and the reason is a property
of the two functions rather than a bug in either. `candidate_gaps` is `freshness`,
and `freshness` reads `changed_files(checkout)` -- `git status --porcelain`. This
module calls `assert_clean` twice, once before the required checks and once after,
and `assert_clean` raises unless the checkout's status is empty. So at the instant
the gap would be read, the checkout is clean by construction and the function
returns (). Measured on a candidate commit that carries a pending ordinary
trailing comment, with an approved policy enabling every cleanup rule:

    `git status --porcelain`  : ''
    changed_files(checkout)   : ()
    candidate_gaps(checkout)  : ()

Wiring it in as described would leave every decision exactly as it is today.

What is wrong with a candidate is not that its checkout is dirty. It is that its
commit carries cleanup the owner approved and the commit has not applied. The
scope that admits that is the set of paths the candidate changed against the
target, which is what this gate reads, previewed over every line of each: the
whole candidate is under review, so the "only on a line the task changed" limit
that `candidate_gaps` applies for a worker's checkout is not the limit here.

**Nothing here writes.** The cleanup reader demotes the owner's policy to
`preview` itself, so the guarantee is a property of `cleanup/hooks.py` rather than
a promise by this caller, and `test_a_rejected_candidate_checkout_is_byte_identical`
is what holds this module to it.

Every test drives `vkit integration verify` as a real process against a real
throwaway repository, and asserts the JSON record a protected CI job reads. The
checkout assertions are the load-bearing ones.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures as fx  # noqa: E402
import subproc  # noqa: E402
from test_integration import one_json_object, vkit  # noqa: E402

from vkit.cli import EXIT_CHECK_FAILED, EXIT_OK  # noqa: E402

#: Every rule the cleanup engine can apply, which is what an owner enables when
#: they enable automatic cleanup.
CLEANUP_POLICY = {
    "mode": "apply_verified",
    "enabled_rules": [
        "LOGIC-EMPTY-ELSE-PASS", "LOGIC-REDUNDANT-PASS", "ORDINARY_TRAILING_COMMENT",
    ],
    "excluded_paths": [],
    "python_version": None,
    "optimize_levels": [0, 1, 2],
}

TRAILING_COMMENT_OLD = "MAX_DISCOUNT_PERCENT = 100"
TRAILING_COMMENT_NEW = "MAX_DISCOUNT_PERCENT = 100  # the most a promotion may take off"


def cleanup_enabled_repository(root: Path) -> tuple[Path, str]:
    """A repository whose approved configuration enables automatic cleanup.

    Returns `(repo, target)`: `target` is the commit a candidate is built on, and
    it carries both the approved manifest revision the policy pins and the cleanup
    policy. The policy is committed rather than written into the working tree,
    because the candidate checkout is a checkout of a commit and an uncommitted
    file would not exist there.
    """
    repo = fx.build_repository(root)
    target = fx.baseline_revision(repo)
    (repo / "verification").mkdir(parents=True, exist_ok=True)
    (repo / "verification" / "cleanup.json").write_text(
        json.dumps(CLEANUP_POLICY, indent=2) + "\n", encoding="utf-8"
    )
    return repo, fx.commit_all(repo, "enable automatic cleanup")


def commented_candidate(repo: Path, target: str, branch: str = "candidate") -> str:
    """A candidate commit whose product code still owes a trailing comment.

    On a product line, never on the driver: a candidate that edits the
    verification code is refused by a different gate, and a test that wants an
    ordinary pending cleanup must not depend on an attack.
    """
    fx.checkout_branch(repo, branch, target)
    return fx.replace_in(
        repo, fx.RULES_NAME, TRAILING_COMMENT_OLD, TRAILING_COMMENT_NEW,
        "note the discount ceiling on the line that sets it",
    )


def protected_verify(repo: Path, candidate: str, target: str):
    """One protected verification, as a protected job runs it.

    `--keep-checkout` is what makes the non-mutation assertion measurable: the
    checkout is retired on the ordinary path, and there would be nothing left to
    read.
    """
    done = vkit(
        "integration", "verify", "--project", str(repo),
        "--candidate", candidate, "--target", target, "--policy", "@main",
        "--json", "--keep-checkout",
    )
    return done, one_json_object(done)


# --------------------------------------------------------------------------- #
# The control. Every refusal below is only meaningful against an accepted
# candidate produced by the same fixture, through the same command.
# --------------------------------------------------------------------------- #


def test_a_candidate_owning_no_pending_cleanup_is_accepted(tmp_path: Path) -> None:
    """The control: cleanup enabled, nothing owed, the candidate is accepted.

    Without it, a gate that refused every candidate would satisfy every assertion
    below.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    fx.checkout_branch(repo, "ordinary", target)
    candidate = fx.replace_in(
        repo, fx.QUOTE_NAME,
        "        reason = \"allowed\"",
        "        reason = \"allowed\"  # inside the rule, so no clamp",
        "note why the discount is inside the rule",
    )

    done, record = protected_verify(repo, candidate, target)

    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED", record


# ------------------------------------------------------------ the cleanup gate


def test_a_candidate_with_pending_required_cleanup_is_rejected(tmp_path: Path) -> None:
    """Gap 2. The candidate's commit still owes cleanup, so it cannot pass.

    The gate reports the gap and requires a new candidate commit. It does not
    clean the checkout and then attest to the commit it was cleaning, which is
    what Plan 11 line 79 forbids by name.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    candidate = commented_candidate(repo, target)

    done, record = protected_verify(repo, candidate, target)

    assert record["decision"] == "REJECTED", record
    assert done.returncode == EXIT_CHECK_FAILED, record


def test_the_rejection_names_the_path_and_the_rule(tmp_path: Path) -> None:
    """A gap a reader cannot act on is a gap they have to go and reconstruct.

    Both halves are named: which path still owes cleanup, and which rule says so.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    candidate = commented_candidate(repo, target)

    _, record = protected_verify(repo, candidate, target)

    assert any(fx.RULES_NAME in gap for gap in record["gaps"]), (
        f"no gap named {fx.RULES_NAME}: {record['gaps']}"
    )
    assert any("ORDINARY_TRAILING_COMMENT" in gap for gap in record["gaps"]), (
        f"no gap named the rule that says so: {record['gaps']}"
    )


def test_the_rejection_demands_a_new_candidate_commit(tmp_path: Path) -> None:
    """The reader has to be told what to do, not only what is wrong.

    A gap saying "cleanup is missing" leaves the operator choosing between
    cleaning the tree and committing the cleanup. Only the second establishes
    anything about the candidate, so the gap says that.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    candidate = commented_candidate(repo, target)

    _, record = protected_verify(repo, candidate, target)

    assert any("new candidate commit" in gap for gap in record["gaps"]), (
        f"no gap told the reader that a new commit is the way out: {record['gaps']}"
    )


def test_a_rejected_candidate_checkout_is_byte_identical_and_head_unmoved(
    tmp_path: Path,
) -> None:
    """The load-bearing assertion, and the one easiest to fake.

    Every file in the checkout is read before the decision and again after, each
    hashed rather than compared through Git, and HEAD is read separately because
    a clean tree and the right revision are different facts. A gate that cleaned
    the checkout would leave the comment gone and the tree clean, so both of those
    would still look right -- the bytes are what catch it.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    candidate = commented_candidate(repo, target)

    done, record = protected_verify(repo, candidate, target)
    assert record["decision"] == "REJECTED", record

    checkout = Path(record["checkout"])
    assert checkout.is_dir(), f"{checkout} was not kept, so there is nothing to inspect"

    committed = _committed_bytes(repo, candidate)
    on_disk = _checkout_bytes(checkout)
    assert on_disk == committed, (
        "the candidate checkout does not hold the bytes of the commit it was "
        f"materialized from; only in the checkout: "
        f"{sorted(set(on_disk) - set(committed))}"
    )
    assert fx.git(repo, "-C", str(checkout), "rev-parse", "HEAD") == candidate
    assert fx.git(repo, "-C", str(checkout), "status", "--porcelain",
                  "--untracked-files=all") == ""


def test_a_candidate_that_committed_its_own_cleanup_is_accepted(tmp_path: Path) -> None:
    """The gate is a demand, not a wall.

    The same comment, this time removed and committed by the candidate. Nothing
    about the policy changed; the only difference is that the cleanup is in the
    commit the protected run verifies. This is what makes the rejection above
    actionable.
    """
    repo, target = cleanup_enabled_repository(tmp_path)

    fx.checkout_branch(repo, "self-cleaned", target)
    commented = fx.replace_in(
        repo, fx.RULES_NAME, TRAILING_COMMENT_OLD, TRAILING_COMMENT_NEW,
        "note the discount ceiling",
    )
    cleaned = apply_approved_cleanup(repo, commented)

    done, record = protected_verify(repo, cleaned, target)

    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED", record


def test_the_gate_reads_the_owners_policy_rather_than_assuming(tmp_path: Path) -> None:
    """Cleanup is off, so the same comment is not owed.

    No `verification/cleanup.json` means the approved policy approves no rules.
    The candidate carries the comment and the decision is the ordinary one: a
    gate that consulted nothing and refused anyway would be measuring the
    fixture, not the policy.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    candidate = commented_candidate(repo, target, branch="cleanup-off")

    done, record = protected_verify(repo, candidate, target)

    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED", record


def test_cleanup_in_a_file_the_candidate_never_touched_is_not_owed(tmp_path: Path) -> None:
    """Scope is the candidate's own change.

    An approved trailing comment on a file the candidate does not touch was
    already in the target, so the candidate did not create it. Plan 11's scope
    is a task-owned changed line, and applying it to a whole repository would
    make the gate unpassable for any project that predates the policy.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    fx.checkout_branch(repo, "inherited", target)

    rules = repo / fx.RULES_NAME
    rules.write_text(
        rules.read_text(encoding="utf-8").replace(
            TRAILING_COMMENT_OLD, f"{TRAILING_COMMENT_OLD}  # a pre-existing note"
        ),
        encoding="utf-8",
    )
    inherited = fx.commit_all(repo, "an approved comment predates the candidate")
    candidate = fx.replace_in(
        repo, fx.QUOTE_NAME,
        '        reason = "allowed"',
        '        reason = "allowed"  # inside the rule, so no clamp',
        "change a different file",
    )

    done, record = protected_verify(repo, candidate, inherited)

    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED", record


def test_the_rejection_is_recorded_as_a_policy_finding(tmp_path: Path) -> None:
    """A reader looking at findings must see why the candidate was refused.

    `gaps` is what the summary prints under the decision and what the acceptance
    table persists, but the findings are where the rest of this module's refusals
    live, so a gate that only wrote a gap would be a shape a reader has to learn.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    candidate = commented_candidate(repo, target)

    _, record = protected_verify(repo, candidate, target)

    kinds = {finding["kind"] for finding in record["findings"]}
    assert "required_cleanup_pending" in kinds, (
        f"the refusal is not among the findings a reader inspects: {sorted(kinds)}"
    )


# --------------------------------------------------------------------------- #
# The evidence-kind half of the decision (checkpoint 10.4).
# --------------------------------------------------------------------------- #


def test_a_policy_pinning_a_category_the_approved_driver_cannot_produce_is_rejected(
    tmp_path: Path,
) -> None:
    """The evidence kind is compared, and the comparison is load bearing.

    The approved revision declares a v1 scenario driver, which licenses
    `scenario`. The policy pins `theorem_checking` for that check. A scenario run
    however green establishes a named sequence of calls producing an observed
    result, which is not what a theorem obligation asks for, so the candidate is
    refused before the checks run.

    This is the property checkpoint 10.4 asked for. The control below is what
    makes it meaningful: the same repository, the same command and the same
    candidate, with the category the driver really produces.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    fx.write_json(
        repo / fx.POLICY_NAME,
        fx.policy_document(manifest_revision=target, evidence_kind="theorem_checking"),
    )
    target = fx.commit_all(repo, "pin theorem_checking as the required category")
    candidate = commented_candidate(repo, target, branch="wrong-category")

    done, record = protected_verify(repo, candidate, target)

    assert record["decision"] == "REJECTED", record
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert any(
        finding["kind"] == "evidence_kind_downgraded"
        for finding in record["findings"]
    ), f"the category disagreement was not reported: {record['findings']}"


def test_the_same_candidate_is_accepted_when_the_policy_pins_the_real_category(
    tmp_path: Path,
) -> None:
    """The control for the row above, so the refusal cannot be vacuous.

    One value changes between the two runs: the category the policy pins. A gate
    that refused both would pass the refusal and mean nothing by it.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    candidate = apply_approved_cleanup(
        repo, commented_candidate(repo, target, branch="right-category")
    )

    done, record = protected_verify(repo, candidate, target)

    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED", record


def test_the_reachable_evidence_kind_half_is_the_one_already_in_place(
    tmp_path: Path,
) -> None:
    """Where the checkpoint 10.4 seam actually lands, measured rather than assumed.

    The checkpoint asked for a route through `tasks._decide`. That route is not
    buildable in this tree, and the reason is a measurement rather than a
    preference: `oracle.repoint_approved` writes the approved script's absolute
    path under `<run_dir>/approved/`, `manifest.digest()` covers `argv`, and
    `tasks._identity_gaps` compares the resulting `configuration_digest` against
    the policy digest a task pinned at admission. Two executions of the same
    candidate under the same policy therefore record two different digests --
    measured `a8f305c6...` against `fbaf2e57...` -- so a routed call raises a
    policy-identity gap on every run and refuses every candidate, clean ones
    included. Separately, a scenario driver's receipt carries `satisfied: []`,
    which `_obligation_gaps` documents as a deliberate no-op, so the obligation
    half would compare nothing for the most common check kind.

    What remains is the half that IS reachable, and the two rows above measure it
    end to end: a policy pinning a category the approved variant cannot license is
    refused by `policy.compare`, before any process starts. `evidence_kind` is
    derived from the variant (`spec.evidence_kind`) and never declared, so a run
    cannot record a category that disagrees with the check it executed; the only
    disagreement a candidate can produce is the one a policy assertion makes
    against the candidate's own manifest, and that is already refused.

    So this row asserts the claim that closes the checkpoint: the reachable
    disagreement is refused, and the catalogued name is the one a reader greps
    for. It is the test a future change to `compare` has to break.
    """
    repo = fx.build_repository(tmp_path)
    target = fx.baseline_revision(repo)
    fx.write_json(
        repo / fx.POLICY_NAME,
        fx.policy_document(manifest_revision=target, evidence_kind="theorem_checking"),
    )
    target = fx.commit_all(repo, "pin a category the driver cannot produce")
    candidate = commented_candidate(repo, target, branch="named-refusal")

    _, record = protected_verify(repo, candidate, target)

    kinds = {finding["kind"] for finding in record["findings"]}
    assert "evidence_kind_downgraded" in kinds, (
        f"the reachable evidence-kind disagreement is not reported under its "
        f"catalogued name, so a reader grepping for it would find nothing: "
        f"{sorted(kinds)}"
    )


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #


def apply_approved_cleanup(repo: Path, at: str) -> str:
    """What the approved trailing-comment rule would remove, then committed.

    Driven through the product's own preview so the "already cleaned" candidate
    carries exactly the bytes the checker preserves, rather than a hand-written
    edit that might not be what the rule produces. A refusal here would mean the
    fixture does not owe what the tests below think it owes, so it is asserted
    rather than assumed.
    """
    from vkit.cleanup.comments import preview_comment_cleanup
    from vkit.cleanup.hooks import all_lines
    from vkit.paths import open_project

    path = repo / fx.RULES_NAME
    proposal = preview_comment_cleanup(
        open_project(repo), fx.RULES_NAME, changed_lines=all_lines(path.read_bytes())
    )
    removed = getattr(proposal, "removed", ())
    assert removed, f"the approved rule found nothing to remove in {fx.RULES_NAME}"
    path.write_bytes(proposal.after_bytes)
    return fx.commit_all(repo, "apply the approved trailing-comment cleanup")


def _db(repo: Path) -> Path:
    return repo / ".git" / "verification-kit" / "state.sqlite3"


def _checkout_bytes(root: Path) -> dict[str, str]:
    """Every file in the checkout, hashed by content, keyed by relative path.

    `.git` is excluded because a linked worktree's administrative file is not
    source and legitimately differs between checkouts.
    """
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        digests[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return digests


def _committed_bytes(repo: Path, commit: str) -> dict[str, str]:
    """The bytes that commit ships, keyed the same way, read from Git.

    From the commit rather than from the caller's working tree, because the
    comparison is against the commit the decision is about.
    """
    digests: dict[str, str] = {}
    for name in fx.git(repo, "ls-tree", "-r", "--name-only", commit).splitlines():
        if name and name != ".gitignore":
            digests[name] = hashlib.sha256(_blob(repo, commit, name)).hexdigest()
    return digests


def _blob(repo: Path, commit: str, path: str) -> bytes:
    """One file's bytes as that commit ships them.

    `git show` through `fx.git` would decode as text and re-encode, which loses
    a file that is not valid UTF-8 and normalizes nothing else. Git writes the
    blob itself, so this is the commit's bytes with no decoding in the way.
    """
    done = subproc.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{path}"],
        capture_output=True, check=True, **subproc.hidden_window(),
    )
    return done.stdout
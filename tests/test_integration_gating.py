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
    cleanup_revision = fx.commit_all(repo, "enable automatic cleanup")
    fx.write_json(repo / fx.POLICY_NAME, fx.policy_document(manifest_revision=cleanup_revision))
    return repo, fx.commit_all(repo, "approve the manifest and cleanup policy revision")


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




def test_a_candidate_owning_no_pending_cleanup_is_accepted(tmp_path: Path) -> None:
    """The control: cleanup enabled, nothing owed, the candidate is accepted.

    Without it, a gate that refused every candidate would satisfy every assertion
    below. The change is a real one that carries no removable comment, so the
    tree differs from the target and the candidate is not the target.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    fx.checkout_branch(repo, "ordinary", target)
    candidate = fx.replace_in(
        repo, fx.QUOTE_NAME,
        '    before = subtotal(prices_cents, quantities)',
        '    before = subtotal(prices_cents, quantities)  # the order total before any discount',
        "note what the total starts from",
    )
    _apply_approved_cleanup_to(repo, fx.QUOTE_NAME)
    candidate = fx.commit_all(repo, "apply the approved cleanup")

    done, record = protected_verify(repo, candidate, target)

    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED", record




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

    A gate that cleaned the checkout would leave the comment GONE and the tree
    still clean, so `git status` would report nothing and the test would pass.
    The comment's own bytes are therefore asserted directly: the file the
    candidate committed, with its trailing comment, must still be on disk exactly
    as the candidate wrote it.

    **Why the comparison normalizes line endings.** On this host
    `core.autocrlf=true`, so Git writes CRLF into a linked worktree and stores LF
    in the blob. Measured on the fixture: `pricing/quote.py` is 2353 bytes with
    zero CRLF in the blob and 2415 bytes with 62 CRLF in the worktree, and
    `git status` is empty while they differ. Git considers the worktree file
    unmodified, so the difference is in what Git chose to write, not in what the
    candidate shipped. Comparing raw bytes against the blob would therefore fail
    on every candidate on this host and prove nothing about the gate, so both
    sides are normalized to LF first. That is a stated limit rather than a
    convenience: a transient edit that is reverted with the line endings
    restored would not be visible here either, and docs/verification.md already says the
    before/after digests cannot detect a transient edit that is restored.
    """
    repo, target = cleanup_enabled_repository(tmp_path)
    candidate = commented_candidate(repo, target)

    done, record = protected_verify(repo, candidate, target)
    assert record["decision"] == "REJECTED", record

    checkout = Path(record["checkout"])
    assert checkout.is_dir(), f"{checkout} was not kept, so there is nothing to inspect"

    on_disk = (checkout / fx.RULES_NAME).read_text(encoding="utf-8")
    assert "# the most a promotion may take off" in on_disk, (
        "the trailing comment is gone from the candidate checkout: the gate "
        "cleaned the candidate instead of demanding a new commit"
    )
    assert on_disk == (repo / fx.RULES_NAME).read_text(encoding="utf-8"), (
        "the candidate checkout does not hold the bytes the candidate committed"
    )

    on_disk_digests = _checkout_bytes(checkout)
    committed_digests = _committed_bytes(repo, candidate)
    assert on_disk_digests == committed_digests, (
        "the checkout differs from the commit beyond line endings; "
        f"only in the checkout: {sorted(set(on_disk_digests) - set(committed_digests))}"
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
    """Scope is the candidate's own change against its own target.

    An approved trailing comment sits in `pricing/rules.py`, committed BEFORE the
    target the candidate is built on. The candidate changes `pricing/quote.py`
    only, so it never authored the comment and is not asked to remove it.

    This is the boundary that keeps the gate usable. Scope measured against the
    caller's checkout would ask about whatever happens to be dirty in a working
    tree; scope measured against the whole tree would make every repository that
    predates the policy unpassable, because a comment nobody touched is still in
    it forever.
    """
    repo = fx.build_repository(tmp_path)

    fx.checkout_branch(repo, "pre-existing", fx.baseline_revision(repo))
    rules = repo / fx.RULES_NAME
    rules.write_text(
        rules.read_text(encoding="utf-8").replace(
            TRAILING_COMMENT_OLD, f"{TRAILING_COMMENT_OLD}  # a note that predates the candidate"
        ),
        encoding="utf-8",
    )
    target = fx.commit_all(repo, "a comment lands before cleanup is enabled")

    (repo / "verification").mkdir(parents=True, exist_ok=True)
    (repo / "verification" / "cleanup.json").write_text(
        json.dumps(CLEANUP_POLICY, indent=2) + "\n", encoding="utf-8"
    )
    cleanup_revision = fx.commit_all(repo, "enable automatic cleanup")
    fx.write_json(repo / fx.POLICY_NAME, fx.policy_document(manifest_revision=cleanup_revision))
    target = fx.commit_all(repo, "approve cleanup with its pre-existing comment")
    fx.git(repo, "branch", "-f", "main", target)

    fx.checkout_branch(repo, "unrelated-change", target)
    candidate = fx.replace_in(
        repo, fx.QUOTE_NAME,
        '    before = subtotal(prices_cents, quantities)',
        '    before = subtotal(prices_cents, quantities)  # the order total before any discount',
        "note what the total starts from",
    )
    _apply_approved_cleanup_to(repo, fx.QUOTE_NAME)
    candidate = fx.commit_all(repo, "apply the approved cleanup to its own change")

    done, record = protected_verify(repo, candidate, target)

    assert done.returncode == EXIT_OK, record
    assert record["decision"] == "ACCEPTED", (
        f"the gate demanded cleanup for a file the candidate never changed: "
        f"{record['gaps']}"
    )


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




def apply_approved_cleanup(repo: Path, at: str) -> str:
    """The approved trailing-comment cleanup applied and committed, for `pricing/rules.py`."""
    _apply_approved_cleanup_to(repo, fx.RULES_NAME)
    return fx.commit_all(repo, "apply the approved trailing-comment cleanup")


def _apply_approved_cleanup_to(repo: Path, name: str) -> None:
    """What the approved rules would remove from one file, written in place.

    Driven through the product's own preview so a "already cleaned" candidate
    carries exactly the bytes the checker preserves, rather than a hand-written
    edit that might not be what the rule produces. A preview finding nothing is
    asserted rather than assumed: a silent no-op would let a fixture that does not
    owe what the test thinks it owes pass for the wrong reason.
    """
    from vkit.cleanup.comments import preview_comment_cleanup
    from vkit.cleanup.hooks import all_lines
    from vkit.paths import open_project

    path = repo / name
    proposal = preview_comment_cleanup(
        open_project(repo), name, changed_lines=all_lines(path.read_bytes())
    )
    removed = getattr(proposal, "removed", ())
    assert removed, f"the approved rule found nothing to remove in {name}"
    path.write_bytes(proposal.after_bytes)


def _db(repo: Path) -> Path:
    return repo / ".git" / "verification-kit" / "state.sqlite3"


#: CRLF, and LF. A host with `core.autocrlf=true` has Git write CRLF into a
#: linked worktree while the blob holds LF, so both sides of a byte comparison are
#: normalized before hashing. Measured on the fixture; see the byte-identity test.
CRLF = b"\r\n"
LF = b"\n"


def _checkout_bytes(root: Path) -> dict[str, str]:
    """Every file in the checkout, hashed by content, keyed by relative path.

    `.git` is excluded because a linked worktree's administrative file is not
    source and legitimately differs between checkouts. Line endings are
    normalized, because on this host they differ from the blob by construction.
    """
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        digests[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes().replace(CRLF, LF)
        ).hexdigest()
    return digests


def _committed_bytes(repo: Path, commit: str) -> dict[str, str]:
    """The bytes that commit ships, keyed the same way, read from Git.

    From the commit rather than from the caller's working tree, because the
    comparison is against the commit the decision is about.
    """
    digests: dict[str, str] = {}
    for name in fx.git(repo, "ls-tree", "-r", "--name-only", commit).splitlines():
        if name:
            digests[name] = hashlib.sha256(
                _blob(repo, commit, name).replace(CRLF, LF)
            ).hexdigest()
    return digests


def _blob(repo: Path, commit: str, path: str) -> bytes:
    """One file's bytes as that commit ships them.

    `git show` through `fx.git` would decode as text and re-encode, which loses
    a file that is not valid UTF-8 and normalizes nothing else. Git writes the
    blob itself, so this is the commit's bytes with no decoding in the way.
    """
    done = subproc.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{path}"],
        capture_output=True, check=True,
    )
    return done.stdout

def test_a_candidate_cannot_disable_the_approved_cleanup_policy(tmp_path: Path) -> None:
    repo, target = cleanup_enabled_repository(tmp_path)
    fx.checkout_branch(repo, "disable-cleanup", target)
    (repo / "verification/cleanup.json").write_text(json.dumps({"mode": "off"}), encoding="utf-8")
    candidate = fx.commit_all(repo, "disable the owner cleanup policy")
    done, record = protected_verify(repo, candidate, target)
    assert done.returncode == EXIT_CHECK_FAILED, record
    assert record["decision"] == "REJECTED", record
    assert any("cleanup_policy_changed" in gap for gap in record["gaps"]), record

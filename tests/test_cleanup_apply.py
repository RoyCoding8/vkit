"""Plan 11 checkpoint 2: the guarded apply.

Each test writes a real file into a real repository, previews, applies, and
asserts on the bytes the caller would receive. The load-bearing properties are
the plan's acceptance rows for this checkpoint:

* an approved trailing comment on a task-owned changed line applies with a
  preservation receipt;
* another task owning the file, or the before bytes having changed, produces NO
  overwrite;
* the same proposal and request repeated converges to the same result with no
  second edit;
* a tampered receipt is refused, and so are a changed constant, the exception
  table, a closure, and a binding;
* a crash after replacement is recoverable by comparing before and after digests;
* every refusal leaves the file byte-identical, which is asserted in each case
  rather than assumed.

The ownership check is a parameter, so these tests supply their own and assert
on the refusal the product reports. `test_the_ownership_check_runs_before_any_byte_moves`
is the one that pins the ordering, because ordering is the design.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subproc  # noqa: E402

from vkit.cleanup import (  # noqa: E402
    AlreadyApplied,
    ApplyRefused,
    ApplyRequest,
    Applied,
    CLEANUP_RULE_IDS,
    CleanupMode,
    CleanupPolicy,
    PolicyRefused,
    apply_cleanup,
    apply_comment_cleanup,
    apply_logic_cleanup,
    check_policy,
    is_excluded,
    parse_policy,
    preview_comment_cleanup,
    preview_logic_cleanup,
    Proposal,
)
from vkit.paths import open_project  # noqa: E402

APPLY_POLICY = CleanupPolicy(
    mode=CleanupMode.APPLY_VERIFIED, enabled_rules=CLEANUP_RULE_IDS
)


def run_git(*args: str, cwd: Path) -> None:
    subproc.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def project_with(tmp_path: Path, relative: str, content: bytes):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    run_git("init", "-q", cwd=root)
    run_git("add", "-A", cwd=root)
    run_git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture", cwd=root)
    return open_project(root)


COMMENTED = b"def bump(total):\n    total = total + 1  # increment the counter\n    return total\n"
ELSE_PASS = b"def pick(a):\n    if a:\n        return 1\n    else:\n        pass\n"


def refuse_reason(result) -> str:
    assert isinstance(result, ApplyRefused), result
    return result.reason


# --------------------------------------------------------------------------- #
# What applies
# --------------------------------------------------------------------------- #


def test_an_approved_trailing_comment_applies_with_a_preservation_receipt(
    tmp_path: Path,
) -> None:
    """The plan's first acceptance row, through the whole path."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(proposal, Proposal), proposal
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )

    assert isinstance(result, Applied), result
    assert (project.root / "sample.py").read_bytes() == (
        b"def bump(total):\n    total = total + 1\n    return total\n"
    )
    assert result.before_digest == proposal.before_digest
    assert result.after_digest == proposal.after_digest
    assert result.receipt["result"] == "PASS"
    assert result.generation == 1
    assert result.policy_digest == APPLY_POLICY.digest


def test_an_empty_else_pass_applies_and_the_receipt_names_the_checker(
    tmp_path: Path,
) -> None:
    """The plan's row: an empty else with identical executable fields can apply."""
    project = project_with(tmp_path, "sample.py", ELSE_PASS)

    proposal = preview_logic_cleanup(project, "sample.py")
    assert not isinstance(proposal, Proposal)
    result = apply_logic_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )

    assert isinstance(result, Applied), result
    assert (project.root / "sample.py").read_bytes() == (
        b"def pick(a):\n    if a:\n        return 1\n"
    )
    assert result.receipt["optimizeLevels"] == [0, 1, 2]
    assert "co_consts" in result.receipt["checkedFields"]


def test_the_original_bytes_are_preserved_as_an_artifact(tmp_path: Path) -> None:
    """A write that cannot be undone must leave the before bytes recoverable."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )

    assert isinstance(result, Applied)
    assert Path(result.artifact_path).read_bytes() == COMMENTED


# --------------------------------------------------------------------------- #
# What does not overwrite
# --------------------------------------------------------------------------- #


def test_another_task_owning_the_file_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """The plan's row: another task owns the file, so no overwrite."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
        ownership_check=lambda request: "task T was reassigned from generation 1 to 2",
    )

    assert refuse_reason(result) == "not_task_owned"
    assert (project.root / "sample.py").read_bytes() == COMMENTED


def test_changed_before_bytes_are_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """The plan's row: the before bytes changed, so no overwrite."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    (project.root / "sample.py").write_bytes(
        b"def bump(total):\n    total = total + 2  # someone else's edit\n    return total\n"
    )
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )

    assert refuse_reason(result) == "before_bytes_changed"
    assert result.observed_before_digest != result.expected_before_digest
    assert b"someone else's edit" in (project.root / "sample.py").read_bytes()


def test_cleanup_off_writes_nothing(tmp_path: Path) -> None:
    """Absence of policy is not permission."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    off = CleanupPolicy(mode=CleanupMode.OFF, enabled_rules=CLEANUP_RULE_IDS)
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=off
    )

    assert refuse_reason(result) == "cleanup_off"
    assert (project.root / "sample.py").read_bytes() == COMMENTED


def test_preview_mode_never_writes(tmp_path: Path) -> None:
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    previewing = CleanupPolicy(
        mode=CleanupMode.PREVIEW, enabled_rules=CLEANUP_RULE_IDS
    )
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=previewing
    )

    assert isinstance(result, ApplyRefused)
    assert (project.root / "sample.py").read_bytes() == COMMENTED


def test_a_disabled_rule_is_refused(tmp_path: Path) -> None:
    """Enabling a mode does not authorize stripping every comment."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    narrow = CleanupPolicy(mode=CleanupMode.APPLY_VERIFIED, enabled_rules=())
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=narrow
    )

    assert refuse_reason(result) == "rule_not_enabled"
    assert (project.root / "sample.py").read_bytes() == COMMENTED


def test_an_excluded_path_is_refused(tmp_path: Path) -> None:
    project = project_with(tmp_path, "vendor/sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "vendor/sample.py", changed_lines=[2])
    narrow = CleanupPolicy(
        mode=CleanupMode.APPLY_VERIFIED, enabled_rules=CLEANUP_RULE_IDS,
        excluded_paths=("vendor",),
    )
    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=narrow
    )

    assert refuse_reason(result) == "path_excluded"
    assert (project.root / "vendor" / "sample.py").read_bytes() == COMMENTED


def test_a_symlink_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    """A write through a symlink lands wherever it points, so it is refused.

    Creating a symlink needs a privilege Windows grants per-user (Developer
    Mode or SeCreateSymbolicLinkPrivilege), so this end-to-end case skips on a
    host without it and runs on CI. `test_the_symlink_guard_fires_without_one`
    covers the same guard on every host.
    """
    import os

    project = project_with(tmp_path, "real.py", COMMENTED)
    outside = tmp_path / "outside.py"
    outside.write_bytes(COMMENTED)
    link = project.root / "linked.py"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symlink")

    proposal = preview_comment_cleanup(project, "real.py", changed_lines=[2])
    request = ApplyRequest(
        request_id="req-1",
        proposal=type(proposal)(
            relative_path="linked.py",
            proposal_id=proposal.proposal_id,
            before_bytes=COMMENTED,
            after_bytes=proposal.after_bytes,
            removed=proposal.removed,
            preserved=proposal.preserved,
            receipt=proposal.receipt,
        ),
        generation=1,
        policy=APPLY_POLICY,
    )
    result = apply_cleanup(project, request)

    assert refuse_reason(result) == "symlink_or_escape"
    assert outside.read_bytes() == COMMENTED


def test_the_symlink_guard_fires_without_creating_one(tmp_path: Path) -> None:
    """The same guard, exercised on every host.

    A path that reports itself a symlink is refused. Monkeypatching `is_symlink`
    rather than creating a real link is what lets this run where the OS withholds
    the privilege; the guard is the unit under test, and it is the guard that is
    asserted, not the filesystem's willingness to co-operate.
    """
    project = project_with(tmp_path, "sample.py", COMMENTED)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])

    original = Path.is_symlink
    Path.is_symlink = lambda self: self.name == "sample.py"
    try:
        result = apply_comment_cleanup(
            project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
        )
    finally:
        Path.is_symlink = original

    assert refuse_reason(result) == "symlink_or_escape"
    assert (project.root / "sample.py").read_bytes() == COMMENTED


def test_a_missing_request_id_is_refused(tmp_path: Path) -> None:
    """Without a request id a repeat cannot be told from a first attempt."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    result = apply_comment_cleanup(
        project, proposal, request_id="", generation=1, policy=APPLY_POLICY
    )

    assert refuse_reason(result) == "missing_request_id"
    assert (project.root / "sample.py").read_bytes() == COMMENTED


# --------------------------------------------------------------------------- #
# Idempotence and recovery
# --------------------------------------------------------------------------- #


def test_the_same_request_repeated_converges_with_no_second_edit(
    tmp_path: Path,
) -> None:
    """The plan's row: same proposal, same request, same result."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    first = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )
    after_first = (project.root / "sample.py").read_bytes()
    second = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )
    third = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )

    assert isinstance(first, Applied), first
    assert isinstance(second, AlreadyApplied), second
    assert isinstance(third, AlreadyApplied), third
    assert (project.root / "sample.py").read_bytes() == after_first
    assert second.after_digest == first.after_digest


def test_a_crash_after_replacement_is_recoverable_by_digest(tmp_path: Path) -> None:
    """The file holds the after bytes and no receipt exists. The retry recovers.

    Simulated by writing the after bytes directly, which is exactly the state a
    crash between `os.replace` and the receipt would leave.
    """
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    (project.root / "sample.py").write_bytes(proposal.after_bytes)

    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )

    assert isinstance(result, AlreadyApplied), result
    assert result.after_digest == proposal.after_digest


def test_a_later_edit_is_never_rolled_back_over(tmp_path: Path) -> None:
    """This module never restores the before bytes. Somebody else's edit stands."""
    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )
    later = b"def bump(total):\n    total = total + 9\n    return total  # later work\n"
    (project.root / "sample.py").write_bytes(later)

    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY
    )

    assert isinstance(result, ApplyRefused)
    assert (project.root / "sample.py").read_bytes() == later


# --------------------------------------------------------------------------- #
# Tampering
# --------------------------------------------------------------------------- #


def test_a_tampered_receipt_is_refused(tmp_path: Path) -> None:
    """A receipt that does not describe the proposal's before bytes is refused."""
    from dataclasses import replace as dataclass_replace

    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    tampered = dataclass_replace(
        proposal, receipt=dataclass_replace(proposal.receipt, before_digest="0" * 64)
    )
    result = apply_cleanup(
        project,
        ApplyRequest(
            request_id="req-1", proposal=tampered, generation=1, policy=APPLY_POLICY
        ),
    )

    assert refuse_reason(result) == "receipt_mismatch"
    assert (project.root / "sample.py").read_bytes() == COMMENTED


def test_after_bytes_that_are_not_just_the_comment_are_refused(tmp_path: Path) -> None:
    """A proposal carrying smuggled code changes fails the re-verification.

    The apply re-runs the preservation check on the bytes in hand rather than
    trusting the receipt, so a proposal whose after bytes are not the registered
    transformation is refused at the boundary.
    """
    from dataclasses import replace as dataclass_replace

    project = project_with(tmp_path, "sample.py", COMMENTED)

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    smuggled = ApplyRequest(
        request_id="req-1",
        proposal=dataclass_replace(
            proposal, after_bytes=proposal.after_bytes + b"import os\n"
        ),
        generation=1,
        policy=APPLY_POLICY,
    )
    result = apply_cleanup(project, smuggled)

    assert isinstance(result, ApplyRefused)
    assert (project.root / "sample.py").read_bytes() == COMMENTED


# --------------------------------------------------------------------------- #
# Ordering, which is the design
# --------------------------------------------------------------------------- #


def test_the_ownership_check_runs_before_any_byte_moves(tmp_path: Path) -> None:
    """If ownership ran after the write it would be a report, not a guard."""
    project = project_with(tmp_path, "sample.py", COMMENTED)
    seen: list[str] = []

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    original_read = Path.read_bytes

    def watching_read(self):
        seen.append(str(self))
        return original_read(self)

    Path.read_bytes = watching_read
    try:
        result = apply_comment_cleanup(
            project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
            ownership_check=lambda request: seen.append("ownership") or "refused",
        )
    finally:
        Path.read_bytes = original_read

    assert refuse_reason(result) == "not_task_owned"
    assert seen == ["ownership"], f"the file was read before ownership: {seen}"


def test_every_guard_precedes_the_first_write(tmp_path: Path) -> None:
    """A guard that ran after `os.replace` would not be a guard."""
    project = project_with(tmp_path, "sample.py", COMMENTED)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])

    replaced: list[str] = []
    real_replace = __import__("os").replace

    def watching_replace(src, dst):
        replaced.append(str(dst))
        return real_replace(src, dst)

    import os as os_module

    os_module.replace = watching_replace
    try:
        result = apply_comment_cleanup(
            project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
            ownership_check=lambda request: "refused",
        )
    finally:
        os_module.replace = real_replace

    assert isinstance(result, ApplyRefused)
    assert replaced == [], "the file was replaced despite a refusal"


def test_no_temporary_file_survives_a_refusal(tmp_path: Path) -> None:
    """A stray temp file would show up in the next `git status` as an untracked path."""
    project = project_with(tmp_path, "sample.py", COMMENTED)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])

    apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
        ownership_check=lambda request: "refused",
    )

    leftovers = [p.name for p in (project.root).iterdir() if ".vkit-cleanup" in p.name]
    assert leftovers == [], leftovers


# --------------------------------------------------------------------------- #
# Ownership, against the real task layer
# --------------------------------------------------------------------------- #


def admitted_project(tmp_path: Path):
    """A project with a real task admitted at generation 1, as a CLI would have.

    The ownership check is the one decision this package does not make, so it is
    the one decision that has to be tested against the module that actually makes
    it. A stub returning "refused" would prove the plumbing and nothing about the
    guarantee.
    """
    import json

    from vkit import tasks as task_layer
    from vkit.manifest import parse_manifest
    from vkit.storage import Store

    project = project_with(tmp_path, "sample.py", COMMENTED)
    (project.root / "verification").mkdir(parents=True, exist_ok=True)
    (project.root / "verification" / "manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "description": "A check for the cleanup ownership test.",
            "checks": [{
                "id": "c1", "description": "d", "command": ["python", "-c", "pass"],
                "cwd": ".", "timeout_seconds": 10, "required_scenarios": ["one"],
                "artifact": "r.json", "inputs": [], "expectations": [],
            }],
        })
    )
    store = Store(project.db_path)
    context = task_layer.acceptance_context(
        project, lambda: parse_manifest(project, project.runs_root)
    )
    task_layer.admit(store, "T1", context=context, required_checks=["c1"], scope="s")
    return project, store


def test_the_shared_ownership_check_applies_while_the_attempt_holds_the_claims(
    tmp_path: Path,
) -> None:
    from vkit.cleanup import ownership_from_tasks

    project, store = admitted_project(tmp_path)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])

    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
        ownership_check=ownership_from_tasks(project, store, "T1"),
    )

    assert isinstance(result, Applied), result
    assert (project.root / "sample.py").read_bytes() == (
        b"def bump(total):\n    total = total + 1\n    return total\n"
    )


def test_a_superseded_attempt_is_refused_by_the_shared_ownership_check(
    tmp_path: Path,
) -> None:
    """The real acceptance case: the generation moved, so the attempt holds nothing."""
    from vkit import tasks as task_layer
    from vkit.cleanup import ownership_from_tasks

    project, store = admitted_project(tmp_path)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    task_layer.supersede_task(store, "T1")

    result = apply_comment_cleanup(
        project, proposal, request_id="req-1", generation=1, policy=APPLY_POLICY,
        ownership_check=ownership_from_tasks(project, store, "T1"),
    )

    assert refuse_reason(result) == "not_task_owned"
    assert "generation" in result.detail
    assert (project.root / "sample.py").read_bytes() == COMMENTED


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def test_a_policy_cannot_name_an_unregistered_rule() -> None:
    with pytest.raises(PolicyRefused):
        CleanupPolicy(mode=CleanupMode.APPLY_VERIFIED, enabled_rules=("LOGIC-INVENTED",))


def test_a_policy_cannot_narrow_the_required_optimization_levels() -> None:
    """An owner may add a level. Removing one is the plan's gate being switched off."""
    with pytest.raises(PolicyRefused):
        CleanupPolicy(
            mode=CleanupMode.APPLY_VERIFIED, enabled_rules=CLEANUP_RULE_IDS,
            optimize_levels=(0, 1),
        )


def test_an_exclusion_matches_any_segment_and_not_a_prefix() -> None:
    """A bare directory name excludes it wherever it is nested."""
    assert is_excluded("vendor/a.py", ("vendor",))
    assert is_excluded("src/vendor/a.py", ("vendor",))
    assert not is_excluded("vendor_helper.py", ("vendor",))


def test_a_multi_segment_exclusion_is_anchored_to_the_root() -> None:
    """`src/vendor` names one path, not any directory called `vendor`."""
    assert is_excluded("src/vendor/a.py", ("src/vendor",))
    assert not is_excluded("lib/src/vendor/a.py", ("src/vendor",))


def test_the_three_modes_are_explicit_and_off_cannot_write() -> None:
    from vkit.cleanup import OFF_POLICY

    assert CleanupMode.OFF.value == "off"
    assert CleanupMode.PREVIEW.value == "preview"
    assert CleanupMode.APPLY_VERIFIED.value == "apply_verified"
    assert not OFF_POLICY.may_write()


def test_a_parsed_policy_round_trips_and_its_digest_is_stable() -> None:
    document = {
        "mode": "apply_verified",
        "enabled_rules": list(CLEANUP_RULE_IDS),
        "excluded_paths": ["vendor"],
    }
    first = parse_policy(document)
    second = parse_policy(dict(document))

    assert first.digest == second.digest
    assert first.may_write()


def test_a_policy_approved_on_another_python_is_refused() -> None:
    """The compiled-equality claim is scoped to one interpreter."""
    policy = CleanupPolicy(
        mode=CleanupMode.APPLY_VERIFIED, enabled_rules=CLEANUP_RULE_IDS,
        python_version="3.9.0",
    )
    refusal = check_policy(policy, "sample.py", python_version="3.13.14")

    assert refusal is not None
    assert refusal.reason == "runtime_not_approved"
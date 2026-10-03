"""A changed policy or a changed input invalidates evidence already accepted.

The product's central promise is that a verification number means the tree and
the policy it was produced under. That promise has an implementation
(`tasks._identity_gaps`) and no test that reaches it: `test_formal_correspondence.py`
decides without an `AcceptanceContext`, so the identity comparison never runs
there, and `scripts/acceptance02.py`'s row 10 decides the same way. Everything
below is measured against the real core, and the values are literal.

## Why each test can fail

Each test opens by reaching READY on the real path and asserting it. A test that
only ever saw BLOCKED would pass against a store where nothing can ever pass, so
the READY is the control that gives the later BLOCKED its meaning. Each test then
changes exactly one thing and asserts the literal gap naming it.

The policy test ends BLOCKED and stays BLOCKED, because the comparison is
against the digest pinned at admission. An attempt admitted under one contract
cannot be satisfied by evidence answering another, and the repair is a new
attempt rather than a re-run.

The source test ends READY again, because evidence re-gathered from the changed
source does answer the current source. The BLOCKED in the middle is the window
in which the old evidence is still on record and no longer applies.

## What this does not cover

`fixtures` is in `_UNRECORDED_ON_RUN`: `execution` writes a null fixture digest
on every run in this build, so a fixture disagreement cannot yet arise and none
is asserted here. `finalize` reports it as `unverified_identities` instead, and
the guarantee is only as wide as that reporting.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import fixtures  # noqa: E402

from vkit import tasks  # noqa: E402
from vkit.claims import ResourceSpec, holder, release  # noqa: E402
from vkit.execution import run_check  # noqa: E402
from vkit.identity import compute_source_identity  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402

#: The repository the checks run against: a real git checkout of the shipped
#: example, so the checks are the product's own and the commands are the ones a
#: user would install.
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
MANIFEST_RELATIVE = "verification/manifest.json"
CHECK_ID = "totals-behavior"


def _example_checks() -> list[dict]:
    return json.loads((EXAMPLE / MANIFEST_RELATIVE).read_text(encoding="utf-8"))["checks"]


def _project_with(checks: list[dict], root: Path):
    """A committed checkout of the example carrying exactly these checks.

    `compute_source_identity` reads the index, HEAD and the worktree's file
    contents, so a fixture that is not a repository has no identity to compare
    and the test would be asserting against a digest of nothing.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(EXAMPLE, root, dirs_exist_ok=True)
    fixtures.write_json(root / MANIFEST_RELATIVE, {"schema_version": 2, "checks": checks})
    fixtures.git(root, "init", "-q", "-b", "main")
    fixtures.git(root, "config", "user.email", "t@example.invalid")
    fixtures.git(root, "config", "user.name", "Identity Test")
    fixtures.commit_all(root, "the policy under test")
    return open_project(root)


def _context(project):
    """The context a caller that can reach the repository builds.

    Read through `acceptance_context` rather than assembled by hand, so the
    identities under test are the ones the product measures and not a fixture's
    idea of them.
    """
    return tasks.acceptance_context(
        project, lambda: parse_manifest(project, project.runs_root)
    )


def _run(store, project, check_id: str, task_id: str, attempt: int = 1):
    """Execute one registered check against the real store, as any surface does."""
    manifest = parse_manifest(project, project.runs_root)
    return run_check(
        manifest, check_id, store=store,
        source=compute_source_identity(project), task_id=task_id, attempt=attempt,
    )


def _accept_and_run(tmp_path: Path, name: str, checks: list[dict], task_id: str):
    """Admit under this policy, run the check, and return READY.

    Returns the store and project so the caller can change one thing and ask
    again. The task holds an exclusive claim on the repository throughout, so
    the evidence below is produced under real ownership rather than by a task
    that was admitted and then walked away from.
    """
    project = _project_with(checks, tmp_path / name)
    store = Store(project.db_path)
    context = _context(project)
    assert context.usable, context.refusal
    tasks.admit(
        store, task_id, context=context, required_checks=[CHECK_ID],
        resources=[{"key": "repository", "kind": "exclusive"}],
    )
    _run(store, project, CHECK_ID, task_id)
    verdict = tasks.finalize(store, task_id, context=context)
    assert verdict.readiness == "READY", f"the control run was {verdict.readiness}: {verdict.gaps}"
    return store, project, context


def test_a_policy_change_refuses_evidence_answered_under_another_policy(tmp_path: Path) -> None:
    """An attempt admitted under one policy cannot be satisfied by another's evidence.

    The policy gains a check. The evidence is then re-gathered under the new
    policy, which is the strongest form of the claim: the failure is not stale
    evidence but fresh evidence answering a contract this attempt was never
    admitted under, and the only thing that can tell the two apart is the
    comparison.
    """
    store, project, admitted_under = _accept_and_run(
        tmp_path, "policy-change", _example_checks(), "t-policy",
    )

    changed = [
        *_example_checks(),
        {
            "id": "second-behavior",
            "command": list(_example_checks()[0]["command"]),
            "cwd": ".",
            "timeout_seconds": 120,
            "required_scenarios": list(_example_checks()[0]["required_scenarios"]),
            "artifact": "result.json",
            "kind": "scenario",
            "subject": {"paths": [], "digest": None},
            "claim_id": "second-behavior",
        },
    ]
    fixtures.write_json(project.root / MANIFEST_RELATIVE, {"schema_version": 2, "checks": changed})

    in_force = _context(project)
    assert in_force.policy_digest != admitted_under.policy_digest, (
        "the policy was rewritten, so the digest in force must differ from the one "
        "this attempt was admitted under"
    )
    assert tasks.get_task(store, "t-policy").pinned().policy_digest == admitted_under.policy_digest, (
        "the digest an attempt is bound to is the one pinned at admission, so it "
        "must not follow the manifest after the manifest is rewritten"
    )

    _run(store, project, CHECK_ID, "t-policy")

    verdict = tasks.finalize(store, "t-policy", context=in_force)
    assert verdict.readiness == "BLOCKED", (
        f"evidence gathered under the new policy satisfied an attempt admitted under "
        f"the old one: {verdict.readiness} with gaps {list(verdict.gaps)}"
    )
    assert len(verdict.gaps) == 1, (
        f"expected the policy to be the only thing disagreeing, got {list(verdict.gaps)}"
    )
    assert "passed against a different policy" in verdict.gaps[0], verdict.gaps[0]
    # The recorded verdict moved, not just the answer to a question.
    assert tasks.get_task(store, "t-policy").readiness == "BLOCKED"

    # And it stays refused. A re-run answers the same different contract, so
    # asking again cannot turn it into an acceptance.
    again = tasks.finalize(store, "t-policy", context=in_force)
    assert again.readiness == "BLOCKED", f"a second decision was {again.readiness}"
    assert "passed against a different policy" in again.gaps[0], again.gaps[0]


def test_a_new_attempt_under_the_new_policy_is_accepted(tmp_path: Path) -> None:
    """The refusal above is about the attempt, and a new attempt clears it.

    Without this, the previous test could be satisfied by a repository that
    refuses everything after a policy change. The repair is a new attempt under
    the new policy, which is what the pinned-digest comparison is for.
    """
    store, project, _ = _accept_and_run(
        tmp_path, "policy-recovery", _example_checks(), "t-old",
    )
    changed = [
        *_example_checks(),
        {
            "id": "second-behavior",
            "command": list(_example_checks()[0]["command"]),
            "cwd": ".",
            "timeout_seconds": 120,
            "required_scenarios": list(_example_checks()[0]["required_scenarios"]),
            "artifact": "result.json",
            "kind": "scenario",
            "subject": {"paths": [], "digest": None},
            "claim_id": "second-behavior",
        },
    ]
    fixtures.write_json(project.root / MANIFEST_RELATIVE, {"schema_version": 2, "checks": changed})
    in_force = _context(project)

    # The old attempt has had its decision and gives the repository up. A new
    # attempt cannot take it while the old one still holds it, and the refusal is
    # the claim table doing its job rather than a wrinkle in this test.
    release(store, "t-old", tasks.current_generation(store, "t-old"))
    assert holder(store, "repository") is None

    tasks.admit(store, "t-new", context=in_force, required_checks=[CHECK_ID],
                resources=[{"key": "repository", "kind": "exclusive"}])
    # The floor is the union of the caller's selection and everything the policy
    # now registers, so the new attempt owes evidence for the added check as
    # well. Asking for only the original one does not shrink the obligation.
    assert tasks.get_task(store, "t-new").pinned().required_checks == (
        "second-behavior", CHECK_ID
    ), tasks.get_task(store, "t-new").pinned().required_checks
    _run(store, project, CHECK_ID, "t-new")
    _run(store, project, "second-behavior", "t-new")
    verdict = tasks.finalize(store, "t-new", context=in_force)
    assert verdict.readiness == "READY", (
        f"an attempt admitted under the policy in force was {verdict.readiness}: "
        f"{list(verdict.gaps)}"
    )
    claim = holder(store, "repository")
    assert (claim.task_id, claim.generation) == ("t-new", 1)


def test_a_changed_input_invalidates_the_pass_recorded_against_the_old_one(
    tmp_path: Path,
) -> None:
    """Editing the code a check declares invalidates the pass it already produced.

    The BLOCKED sits between the edit and the re-run, which is the window the
    guarantee is about: the pass is still on record and no longer answers the
    code that is here now. The READY that follows the re-run is what stops this
    from being a test that only ever sees a refusal.
    """
    store, project, before_change = _accept_and_run(
        tmp_path, "input-change", _example_checks(), "t-input",
    )

    target = project.root / "src" / "totals.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# an edit the check's inputs declare\n",
        encoding="utf-8",
    )
    after_change = _context(project)
    assert after_change.source_inventory_digest != before_change.source_inventory_digest, (
        "a tracked file changed, so the source identity in force must differ from "
        "the one the recorded run was measured under"
    )

    stale = tasks.finalize(store, "t-input", context=after_change)
    assert stale.readiness == "BLOCKED", (
        f"a pass recorded before the edit satisfied the code after it: "
        f"{stale.readiness} with gaps {list(stale.gaps)}"
    )
    assert any("passed against a different source" in gap for gap in stale.gaps), list(stale.gaps)
    assert tasks.get_task(store, "t-input").readiness == "BLOCKED"

    _run(store, project, CHECK_ID, "t-input")
    repaired = tasks.finalize(store, "t-input", context=after_change)
    assert repaired.readiness == "READY", (
        f"evidence re-gathered from the changed source was {repaired.readiness}: "
        f"{list(repaired.gaps)}"
    )


def test_compute_readiness_compares_identities_when_given_a_context(
    tmp_path: Path,
) -> None:
    """`compute_readiness` answers the same way `finalize` does, with a context.

    It is the second public entry point to the same decision, and it takes the
    identities as an argument rather than deriving them, so a caller that has
    built a context can drop it without an error and get the weaker answer.
    This decides over the same real store rather than restating `finalize`'s
    verdict, so the two entry points are checked against one set of evidence
    instead of each against its own fixture.
    """
    store, project, _ = _accept_and_run(
        tmp_path, "two-entry-points", _example_checks(), "t-compute",
    )
    target = project.root / "src" / "totals.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# an edit after the recorded run\n",
        encoding="utf-8",
    )
    after_change = _context(project)

    with_context = tasks.compute_readiness(
        store, "t-compute", required_check_ids=[CHECK_ID], context=after_change
    )
    assert with_context.readiness == "BLOCKED", (
        f"a recorded pass satisfied the changed source when the caller supplied the "
        f"context: {with_context.readiness} with gaps {list(with_context.gaps)}"
    )
    assert any("passed against a different source" in gap for gap in with_context.gaps), list(
        with_context.gaps
    )
    # What was compared is reported rather than left silent.
    assert with_context.context["identities"]["source_inventory_digest"] == (
        after_change.source_inventory_digest
    )

    # A caller with no repository has only the frozen floor and the evidence to
    # decide from, and the disagreement above is invisible to it: no identity is
    # compared, so no identity gap exists. This is why the contexts matter, and it
    # is stated here rather than left as a reader's assumption.
    without = tasks.compute_readiness(store, "t-compute", required_check_ids=[CHECK_ID])
    assert without.context["identities"] is None
    assert without.gaps == (), (
        f"the context-free decision reported gaps {list(without.gaps)}, which would "
        f"mean it compared identities it was never given"
    )
    # `compute_readiness` decides without recording, so the stored verdict is
    # still the READY the control run left behind. Reading the store instead of
    # the result is what shows that: the answer changed, the record did not.
    assert tasks.get_task(store, "t-compute").readiness == "READY"
    assert without.readiness == "READY", (
        f"a context-free caller was told {without.readiness}, so the disagreement "
        f"above is one only a caller holding a context can see"
    )
    # `finalize` is the entry point that records, and it records the refusal.
    assert tasks.finalize(store, "t-compute", context=after_change).readiness == "BLOCKED"
    assert tasks.get_task(store, "t-compute").readiness == "BLOCKED"


def test_the_evidence_was_produced_under_a_claim_the_task_still_holds(
    tmp_path: Path,
) -> None:
    """The identity above was decided for a task that owns the repository.

    Ownership is what makes the identity meaningful: a pass recorded by a task
    that no longer holds the work is evidence about nothing. This reads the
    claim back the way an operator would, one query, not the row that wrote it.
    """
    store, project, _ = _accept_and_run(
        tmp_path, "ownership", _example_checks(), "t-owner",
    )
    claim = holder(store, "repository")
    assert claim is not None, "the task's exclusive claim on the repository is gone"
    assert (claim.task_id, claim.generation, claim.held) == ("t-owner", 1, 1)
    assert tasks.get_task(store, "t-owner").pinned().resource_specs() == (
        ResourceSpec(key="repository", kind="exclusive", capacity=None),
    )

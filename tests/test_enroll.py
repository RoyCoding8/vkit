"""Enrollment: proposing policy, and the refusal to run it before it is accepted.

The invariant under test is a negative one, and negative invariants are the
ones a passing suite proves least. So each test here asserts that something did
*not* happen, using the same surface a person would use.

The central claim is that execution is impossible before acceptance, and it is
asserted the only way that means anything: through `parse_manifest`, the one
function that decides what may run. Not through a flag on a record, and not
through the absence of a warning. A proposal written to disk, with a manifest
document that parses perfectly well, still leaves `parse_manifest` refusing,
because the proposal is at a path the core never reads.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from vkit import enroll as enrollment
from vkit.enroll import (
    PROPOSAL_RELATIVE,
    EnrollmentError,
    State,
    accept,
    build_proposal,
    decline,
    diff_summary,
    enroll,
    read_enrollment,
)
from vkit.manifest import ManifestError, parse_manifest
from vkit.paths import Project

ProjectBuilder = Callable[..., Project]

#: A complete, hand-maintained manifest: what a repository looks like after a
#: person has already enrolled it and tuned the policy to their own needs.
COMPLETE_MANIFEST = {
    "schema_version": 1,
    "description": "hand-maintained by a person who knows this application",
    "checks": [
        {
            "id": "totals",
            "command": ["python", "verify.py", "--out", "{{run_dir}}/result.json"],
            "timeout_seconds": 60,
            "required_scenarios": ["empty-cart"],
            "artifact": "result.json",
        }
    ],
}


def test_enroll_proposes_and_the_core_refuses_to_read_the_proposal(
    repo: ProjectBuilder,
) -> None:
    """The refusal that matters: a proposal on disk is not a manifest.

    Asserted through `parse_manifest`, because that is the function the
    execution path actually calls. If a future change made the core read the
    proposal path, this test fails, and that failure is the alarm the invariant
    deserves.
    """
    project = repo({"package.json": json.dumps({"scripts": {"test": "node --test test/"}})})

    proposal, path = enroll(project)
    assert path is not None and path.is_file()
    assert path.relative_to(project.root).as_posix() == PROPOSAL_RELATIVE.as_posix()

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["checks"][0]["id"] == "npm-test"

    with pytest.raises(ManifestError, match="no manifest"):
        parse_manifest(project, project.runs_root / "probe")

    assert read_enrollment(project).state is State.PROPOSED
    assert read_enrollment(project).state.execution_permitted is False


def test_a_proposal_with_no_scenarios_cannot_be_accepted(repo: ProjectBuilder) -> None:
    """vkit cannot invent an expected outcome, so an unfinished proposal is un-acceptable.

    The schema refuses `required_scenarios: []` for every check, so acceptance
    fails on the real parser rather than on a rule vkit invented for the purpose.
    A future change that let an empty scenario list through would reopen the
    hole this closes: a check that exercises nothing and passes.
    """
    project = repo({"package.json": json.dumps({"scripts": {"test": "node --test test/"}})})
    enroll(project)

    with pytest.raises(EnrollmentError) as caught:
        accept(project)

    message = str(caught.value)
    assert "not a valid manifest" in message
    assert "required_scenarios" in message
    assert not project.manifest_path.exists(), "a refused acceptance wrote the policy anyway"
    assert read_enrollment(project).state is State.PROPOSED


def test_accept_writes_policy_only_after_a_person_completes_the_proposal(
    repo: ProjectBuilder,
) -> None:
    """The whole path, taken the way a person takes it.

    A driver that runs a real program and reports a literal expected result is
    the thing a person writes. Here it is written by the test, and the test then
    asserts on the acceptance the same way a user would: through the record and
    through the core that will execute the policy.
    """
    project = repo({
        "package.json": json.dumps({"scripts": {"test": "node --test test/"}}),
        "verify.py": "print('a driver')\n",
    })
    enroll(project)

    proposal_path = project.root / PROPOSAL_RELATIVE
    document = json.loads(proposal_path.read_text(encoding="utf-8"))
    document["checks"][0]["command"] = ["{{python}}", "verify.py", "--out", "{{run_dir}}/result.json"]
    document["checks"][0]["required_scenarios"] = ["it-runs"]
    proposal_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    record = accept(project)

    assert record.state is State.ACCEPTED
    assert record.state.execution_permitted is True
    assert record.policy_digest, "an accepted policy recorded no digest"
    assert project.manifest_path.is_file()
    assert proposal_path.exists(), "accepting should not delete the proposal a person reviewed"

    manifest = parse_manifest(project, project.runs_root / "probe")
    assert list(manifest.checks) == ["npm-test"]
    spec = manifest.require("npm-test")
    assert spec.required_scenarios == ("it-runs",)
    assert spec.argv[1] == "verify.py", f"the accepted command was {spec.argv}"


def test_enroll_never_overwrites_a_hand_maintained_manifest(repo: ProjectBuilder) -> None:
    """A person's tuned policy survives a re-run of enroll, byte for byte.

    The assertion is on the exact bytes, not on a successful parse. A merge or
    a reformat would still parse, and would still be a person losing work they
    did in a diff they did not approve.
    """
    project = repo({
        "package.json": json.dumps({"scripts": {"test": "node --test test/"}}),
        "verification/manifest.json": json.dumps(COMPLETE_MANIFEST, indent=2),
    })
    before = project.manifest_path.read_bytes()

    _, proposal_path = enroll(project)

    assert project.manifest_path.read_bytes() == before, "enroll overwrote a hand-maintained policy"
    assert proposal_path.is_file(), "a refusal wrote no proposal, so no diff can be shown"
    assert proposal_path != project.manifest_path

    difference = "\n".join(diff_summary(project.manifest_path, proposal_path))
    assert "totals" in difference, "the diff does not show what the existing policy declares"
    assert "npm-test" in difference, "the diff does not show what enrolling would add"


def test_a_diff_is_shown_when_an_existing_policy_differs_from_the_proposal(
    repo: ProjectBuilder, tmp_path: Path
) -> None:
    """A person can see exactly what enrolling would change.

    Written as a direct `diff_summary` call because the refusal path does not
    write a proposal file; the diff is what a person gets in place of one.
    """
    project = repo({"verification/manifest.json": json.dumps(COMPLETE_MANIFEST, indent=2)})
    other = tmp_path / "proposed.json"
    other.write_text(json.dumps({"schema_version": 1, "checks": []}, indent=2), encoding="utf-8")

    difference = "\n".join(diff_summary(project.manifest_path, other))

    assert "-  " in difference or "-" in difference
    assert "totals" in difference, "the diff does not show what the existing policy declares"


def test_decline_leaves_every_file_untouched_and_returns_to_not_enrolled(
    repo: ProjectBuilder,
) -> None:
    """Declining is a clean answer, not a half-applied one.

    No policy, no leftover proposal, and a record that says the truth. A
    proposal left behind would be found by the next `enroll` as though it were
    current, which is the failure this prevents.
    """
    project = repo({"package.json": json.dumps({"scripts": {"test": "node --test test/"}})})
    enroll(project)
    assert (project.root / PROPOSAL_RELATIVE).is_file()

    record = decline(project)

    assert record.state is State.NOT_ENROLLED
    assert record.state.execution_permitted is False
    assert not (project.root / PROPOSAL_RELATIVE).exists(), "the proposal survived a decline"
    assert not project.manifest_path.exists()
    with pytest.raises(ManifestError):
        parse_manifest(project, project.runs_root / "probe")


def test_acceptance_is_refused_when_the_proposal_changed_after_review(
    repo: ProjectBuilder,
) -> None:
    """The digest is the contract between reviewing and running.

    A person approves one specific set of commands. If the file changed between
    that approval and acceptance, what would run is not what they read, and the
    only safe answer is to refuse and make them look again.
    """
    project = repo({
        "package.json": json.dumps({"scripts": {"test": "node --test test/"}}),
    })
    enroll(project)

    _complete(project, command="the-first-driver", scenarios=["a"])
    approved = _digest_of(project)
    assert approved, "a complete proposal produced no digest to accept"

    proposal_path = project.root / PROPOSAL_RELATIVE
    document = json.loads(proposal_path.read_text(encoding="utf-8"))
    document["checks"].append({
        "id": "npm-build", "command": ["npm", "run", "build"],
        "timeout_seconds": 60, "required_scenarios": ["b"], "artifact": "result.json",
    })
    proposal_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(EnrollmentError) as caught:
        accept(project, expected_digest=approved)

    assert "changed since it was reviewed" in str(caught.value)
    assert not project.manifest_path.exists(), "a stale-digest acceptance wrote the policy"


def test_install_hooks_are_never_proposed_as_checks(repo: ProjectBuilder) -> None:
    """A check that runs `npm install` is a check that runs whatever the author wrote.

    The script is still reported by discovery, so a reviewer can see it. It is
    never turned into policy, and never silently dropped either.
    """
    project = repo({"package.json": json.dumps({
        "scripts": {"preinstall": "node -e write-the-sentinel", "test": "node --test test/"},
    })})

    proposal = build_proposal(project)

    skipped = {i: reason for i, reason in proposal.skipped}
    assert "npm:preinstall" in skipped, f"the install hook was not reported: {proposal.skipped}"
    assert "install" in skipped["npm:preinstall"]
    assert "npm-preinstall" not in {e["id"] for e in proposal.entries}, (
        "an install hook became executable policy"
    )
    assert "write-the-sentinel" in json.dumps(proposal.to_json())


def test_the_state_enum_refuses_execution_in_every_state_but_one() -> None:
    """The property itself, tested without any filesystem in the way.

    If a fourth state is ever added, this fails and the new state has to say
    which side of the line it is on. That is the point of keeping execution
    permission as a property of the state rather than as a field somebody sets.
    """
    permitted = {s for s in State if s.execution_permitted}
    assert permitted == {State.ACCEPTED}
    assert State.NOT_ENROLLED.execution_permitted is False
    assert State.PROPOSED.execution_permitted is False


def _complete(project: Project, *, command: str, scenarios: list[str]) -> None:
    """Fill in the parts of a proposal only a person can supply.

    Scenarios and the driver are facts about the application, so the tests
    write them the way a person would: by editing the proposal file directly.
    """
    path = project.root / PROPOSAL_RELATIVE
    document = json.loads(path.read_text(encoding="utf-8"))
    for entry in document["checks"]:
        entry["command"] = ["{{python}}", command, "--out", "{{run_dir}}/result.json"]
        entry["required_scenarios"] = list(scenarios)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _digest_of(project: Project) -> str:
    """The policy digest of a proposal file, computed the way `accept` does."""
    manifest = parse_manifest(project, project.runs_root / "probe", path=project.root / PROPOSAL_RELATIVE)
    return manifest.digest()

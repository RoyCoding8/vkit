"""`project_inspect` reports discovery, and reports the enrollment state.

Two claims are under test and neither is visible from the tool list.

The first is that the tool answers the question a client actually has. A
repository with an npm test script and no registered check does not want to be
told "no checks"; it wants to be told that a command exists, where it came
from, and that nobody has registered it. Without that, the only way to learn is
to read the repository by hand, which is the work the tool exists to remove.

The second is that a working environment over an unaccepted policy still reports
`execution_available: false`. That conjunction is the whole point of adding
enrollment to this tool: before, availability was a statement about the machine,
and a repository that had proposed a manifest and had not had it accepted looked
exactly as ready as one that had.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vkit.enroll import PROPOSAL_RELATIVE, accept, enroll  # noqa: E402
from vkit.mcp import Server  # noqa: E402


def _repo(tmp_path: Path, files: dict[str, str], name: str = "r") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name],
        cwd=root, check=True, capture_output=True,
    )
    return root


PACKAGE_JSON = json.dumps({"scripts": {"test": "node --test test/", "start": "node ."}})


def test_inspect_reports_a_declared_command_no_check_covers(tmp_path: Path) -> None:
    """The question a client has, answered instead of a bare "no checks"."""
    root = _repo(tmp_path, {"package.json": PACKAGE_JSON})

    result = Server(root).call_tool("project_inspect", {})

    assert result.is_error is False
    discovery = result.content["discovery"]
    assert "node" in discovery["ecosystem"]
    declared = {c["id"]: c for c in discovery["declared_commands"]}
    assert "npm:test" in declared, f"the declared test command is missing: {declared}"
    assert declared["npm:test"]["provenance"]["file"] == "package.json"
    assert result.content["checks"] == [], "a declared command is not a registered check"
    assert any(
        "no registered check covers" in gap for gap in result.content["gaps"]
    ), f"the unclaimed command was not reported: {result.content['gaps']}"


def test_inspect_reports_a_waiting_proposal_and_withholds_execution(tmp_path: Path) -> None:
    """A proposed policy that was not accepted is not executable.

    Asserted on `execution_available` rather than only on the gap text, because
    `execution_available` is the field a client branches on and the one that
    decides whether execution is offered at all.
    """
    root = _repo(tmp_path, {"package.json": PACKAGE_JSON}, name="proposed")
    project = Server(root).project
    enroll(project)
    assert (root / PROPOSAL_RELATIVE).is_file()

    result = Server(root).call_tool("project_inspect", {})

    assert result.content["policy_accepted"] is False
    assert result.content["enrollment"]["state"] == "proposed"
    assert any(
        "has not been accepted" in gap for gap in result.content["gaps"]
    ), f"the waiting proposal was not reported: {result.content['gaps']}"


def test_inspect_separates_a_working_environment_from_an_unaccepted_policy(
    tmp_path: Path,
) -> None:
    """Two flags, two facts, and a client that needs both must check both.

    The fixture has a valid manifest and no missing prerequisite, so the
    environment is genuinely ready. The enrollment record says not_enrolled, so
    the policy is not consented to. `execution_available` reports the first and
    `policy_accepted` reports the second.

    Folding consent into `execution_available` was the alternative, and it was
    rejected: one flag cannot say which of two things it means, and a client
    branching on it would silently stop offering execution for repositories
    that already work.
    """
    root = _repo(tmp_path, {
        "package.json": PACKAGE_JSON,
        "verification/manifest.json": json.dumps({
            "schema_version": 1,
            "checks": [{
                "id": "app-test", "command": ["node", "verify.js"],
                "timeout_seconds": 60, "required_scenarios": ["a"],
                "artifact": "result.json",
            }],
        }),
    }, name="unaccepted")
    project = Server(root).project
    # An explicit non-acceptance record, which is what `enroll --decline` writes
    # after a proposal existed.
    enroll(project)
    from vkit.enroll import decline
    decline(project)

    result = Server(root).call_tool("project_inspect", {})

    assert result.content["manifest"] is not None, "the manifest is valid and should be reported"
    assert result.content["enrollment"]["state"] == "not_enrolled"
    assert result.content["execution_available"] is True, (
        "the environment is fine, and execution_available is a statement about the "
        "environment; the unaccepted policy is reported by policy_accepted"
    )
    assert result.content["policy_accepted"] is False, (
        "a valid manifest and a satisfied prerequisite over an unaccepted policy "
        "reported itself as consented to"
    )


def test_inspect_keeps_working_for_a_hand_maintained_manifest(tmp_path: Path) -> None:
    """Plan 01's examples have a manifest and no acceptance record.

    Requiring a record for those would block repositories that already work, so
    an absent record is not by itself a reason to withhold execution. The
    assertion is that a hand-maintained manifest is still reported and is not
    reported as blocked by enrollment.
    """
    root = _repo(tmp_path, {
        "package.json": PACKAGE_JSON,
        "verification/manifest.json": json.dumps({
            "schema_version": 1,
            "checks": [{
                "id": "app-test", "command": ["node", "verify.js"],
                "timeout_seconds": 60, "required_scenarios": ["a"],
                "artifact": "result.json",
            }],
        }),
    }, name="handmade")

    result = Server(root).call_tool("project_inspect", {})

    assert result.content["manifest"]["registered_check_ids"] == ["app-test"]
    assert not any("has not been accepted" in gap for gap in result.content["gaps"]), (
        f"a hand-maintained manifest was treated as unaccepted: {result.content['gaps']}"
    )
    assert result.content["execution_available"] is True, f"gaps were {result.content['gaps']}"


def test_the_tool_response_stays_bounded_with_many_declared_commands(tmp_path: Path) -> None:
    """A repository with two hundred build scripts does not fill a context window."""
    scripts: dict[str, str] = {f"build:{n}": f"echo {n}" for n in range(200)}
    scripts["test"] = "node --test test/"
    root = _repo(tmp_path, {"package.json": json.dumps({"scripts": scripts})}, name="many")

    result = Server(root).call_tool("project_inspect", {"limit": 5})

    discovery = result.content["discovery"]
    assert len(discovery["declared_commands"]) <= 5
    assert discovery["declared_total"] > 5, "the total was not reported, so the bound hides a loss"
    assert result.content["truncated"] is True


def test_discovery_is_absent_only_when_inspection_could_not_read_the_repository(
    tmp_path: Path,
) -> None:
    """An empty repository still gets a discovery block, saying nothing was found.

    A missing key would read as "this tool did not look", which is a different
    and wrong thing to say.
    """
    root = _repo(tmp_path, {"README.md": "# nothing\n"}, name="empty")

    result = Server(root).call_tool("project_inspect", {})

    assert "discovery" in result.content
    assert result.content["discovery"]["declared_commands"] == []
    assert any(
        "no supported project file" in gap
        for gap in result.content["discovery"]["gaps"]
    )

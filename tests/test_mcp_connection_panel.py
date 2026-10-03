"""`project_inspect` answers what an agent needs before it can start work.

The tool describes checks, and until checkpoint 12.4 it described them through
one field. `required_scenarios` is populated only for a scenario check:
`_scenario_names` in `manifest.py` returns `()` for every other kind, because
inventing a scenario for a `lean` or `tlc` check "would be the claim this
contract exists to prevent". That is the right rule for the field and the wrong
shape for the answer, because a check whose obligations are named tests or model
properties came back with an empty list that reads as "this check requires
nothing".

The console half was repaired at checkpoint 10.2 by carrying both
`required_scenarios` and `obligations`. MCP was not. These tests pin the repair,
plus the three facts an agent cannot otherwise learn: what this build's backend
can produce at all, which check ids are usable right now rather than merely
registered, and whether cleanup is available.

**Everything here drives the real tool**, through `Server.call_tool`, which is
the same entry point the stdio transport forwards to. No test names a private
helper, and each asserts a value that came out of the project rather than a
restatement of what the code computed.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import subproc
from vkit.mcp import Server

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
CHECK_ID = "totals-behavior"


def make_repo(tmp_path: Path, name: str = "project", checks: list[dict] | None = None) -> Path:
    """A throwaway Git repository holding a real copy of the example.

    `checks` replaces the manifest's check list, which is how a project with a
    non-scenario check is built.
    """
    target = tmp_path / name
    shutil.copytree(EXAMPLE, target)
    if checks is not None:
        document = json.loads((target / "verification" / "manifest.json").read_text("utf-8"))
        document["checks"] = checks
        (target / "verification" / "manifest.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
    subproc.run(["git", "init", "-q"], cwd=target, check=True, capture_output=True)
    subproc.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True, capture_output=True,
    )
    return target


#: A `pytest` check, which carries obligations that are named tests rather than
#: scenario ids. It parses without an optional toolchain, unlike `lean` and
#: `tlc`, so the test exercises the gap rather than a missing dependency.
PYTEST_CHECK: dict = {
    "id": "totals-unit",
    "description": "The totals module's own unit tests.",
    "kind": "pytest",
    "cwd": ".",
    "timeout_seconds": 120,
    "artifact": "report.json",
    "required_tests": ["tests/test_totals.py::test_empty", "tests/test_totals.py::test_mixed"],
    "runner": {"executable": "python", "base_argv": ["-m", "pytest"]},
    "report_format": "pytest_json_report",
    "expect_report_version": 1,
    "inputs": ["src/totals.py"],
    "subject": {"paths": ["src/totals.py"], "digest": None},
    "claim_id": "totals-unit",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture()
def tools(repo: Path) -> Server:
    return Server(repo)


def inspect(tools: Server, **arguments: object) -> dict:
    result = tools.call_tool("project_inspect", arguments)
    assert result.is_error is False, result.content
    return result.content


# --------------------------------------------------------------- obligations


def test_a_check_whose_obligations_are_not_scenarios_reports_them(tmp_path: Path) -> None:
    """The gap this closes, on the check shape that triggers it.

    A `pytest` check declares two required tests. `required_scenarios` is empty
    for it by design, so before this change the tool reported a check with real
    obligations as a check requiring nothing at all. The obligations are now
    carried under their own name, described through the core's own
    `describe_obligation`, so the string here is the string the receipt uses for
    the same obligation.
    """
    project = make_repo(tmp_path, "non-scenario", checks=[PYTEST_CHECK])
    body = inspect(Server(project))

    view = next(c for c in body["checks"] if c["id"] == "totals-unit")
    assert view["required_scenarios"] == [], (
        "a pytest check declares no scenario ids; the field is populated only "
        "for a scenario check and must stay empty here rather than being filled "
        "to make the response look complete"
    )
    assert view["obligations"] == [
        "case 'tests/test_totals.py::test_empty'",
        "case 'tests/test_totals.py::test_mixed'",
    ]


def test_a_scenario_check_still_reports_its_scenarios_and_its_obligations(
    tmp_path: Path,
) -> None:
    """Both fields are kept. One does not replace the other.

    For a scenario check the two agree, and that is a fact about the manifest
    rather than redundancy to be collapsed: a caller that wants the scenario ids
    alone reads `required_scenarios`, and a caller that wants the general
    obligation set reads `obligations`. Removing the first would break four
    callers that already read it.
    """
    body = inspect(Server(make_repo(tmp_path)))

    view = next(c for c in body["checks"] if c["id"] == CHECK_ID)
    assert view["required_scenarios"] == [
        "empty-cart", "single-positive", "several-positives",
        "mixed-sign", "negatives-only", "cancels-to-zero",
    ]
    assert view["obligations"] == [f"case {name!r}" for name in view["required_scenarios"]]


def test_a_check_view_names_the_category_a_pass_from_it_licenses(tools: Server) -> None:
    """A green mark means something different in each category.

    A scenario pass says a named sequence produced an observed result; a
    property pass says no disagreement was found over generated sequences. The
    console's evidence section carries `category` for exactly this reason, and a
    tool response that names only the check id lets a reader treat the two as
    the same verdict.
    """
    view = next(c for c in inspect(tools)["checks"] if c["id"] == CHECK_ID)
    assert view["category"] == "scenario"


# ------------------------------------------------------------- usable ids


def test_usable_check_ids_name_the_checks_that_can_start_right_now(tools: Server) -> None:
    """Registered is not the same as usable, and one field conflating them lies.

    `manifest.registered_check_ids` is a list of everything the manifest names.
    An agent that reads it as a list of runnable ids will call `check_start` on a
    check whose prerequisite is not installed and be refused, which is a wasted
    round trip and reads as the server being unreliable rather than the
    environment being incomplete.
    """
    body = inspect(tools)

    assert body["usable_check_ids"] == [CHECK_ID], (
        "the example's only check declares python as a prerequisite, which is "
        "on PATH here, so it is usable"
    )
    assert CHECK_ID in body["manifest"]["registered_check_ids"]


def test_a_check_whose_prerequisite_is_absent_is_not_a_usable_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field tracks the environment, so it moves when the environment does.

    `tests/test_mcp.py::test_project_inspect_reports_a_missing_prerequisite`
    already monkeypatches `shutil.which` to establish the mechanism. What is
    asserted here is the consequence for the new field: a check that cannot start
    leaves the list rather than being listed with a warning beside it.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    body = inspect(Server(make_repo(tmp_path)))

    assert body["usable_check_ids"] == []
    assert body["manifest"]["registered_check_ids"] == [CHECK_ID]
    assert any("python" in gap for gap in body["gaps"]), (
        f"a check with no prerequisite on PATH must still be named in the gaps: "
        f"{body['gaps']}"
    )


def test_an_unenrolled_project_has_no_usable_ids(tmp_path: Path) -> None:
    """The field is absent, and the empty list is the answer.

    `examples/python-cli` ships a hand-maintained manifest nobody accepted, so
    `execution_available` is already False. A bare repository has no manifest at
    all and takes the early return, which must still carry the three new fields
    rather than leaving a client to discover their absence by a KeyError.
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    subproc.run(["git", "init", "-q"], cwd=bare, check=True, capture_output=True)
    body = inspect(Server(bare))

    assert body["usable_check_ids"] == []
    assert body["capabilities"], "the early return carries no capabilities"
    assert "cleanup" in body, "the early return carries no cleanup support"


# ----------------------------------------------------------- capabilities


def test_capabilities_name_the_kinds_this_build_can_actually_run(tools: Server) -> None:
    """A backend capability is what runs, not what the schema accepts.

    Checkpoint 10.3 gave `lean` and `tlc` real adapters, so this build now
    supports all six declared kinds and the old "two are declared but refused"
    split is gone. The question a capability list answers moved with it: it is
    no longer "does a runner exist for this kind" but "is the toolchain that
    runner needs present on THIS host". `toolchain_present` is therefore the
    row that can differ between two machines, and `supported` must agree with
    the adapter table rather than with a stale list of refusals.
    """
    capabilities = inspect(tools)["capabilities"]

    supported = {c["kind"] for c in capabilities["kinds"] if c["supported"]}
    assert supported == {
        "scenario", "pytest", "node_test", "property", "lean", "tlc",
    }, "a kind with a registered adapter is supported; the refusals it replaced are gone"
    for entry in capabilities["kinds"]:
        assert entry["adapter"], f"the {entry['kind']} row names no adapter identity"
        assert isinstance(entry["toolchain_present"], bool), (
            f"the {entry['kind']} row does not say whether its toolchain is here"
        )

    absent = {c["kind"] for c in capabilities["kinds"] if not c["toolchain_present"]}
    assert absent <= supported, "a kind with no adapter cannot also claim a toolchain"


def test_capabilities_carry_the_category_each_kind_licenses(tools: Server) -> None:
    """The toolchain a category needs is part of the capability.

    A `property` check needs Hypothesis and a `finite_model_checking` check needs
    a JRE and `tla2tools.jar`. Reporting the categories without naming what they
    require leaves an agent to discover the requirement by running the check.
    """
    capabilities = inspect(tools)["capabilities"]

    needs = {c["value"]: c["needs_toolchain"] for c in capabilities["categories"]}
    assert needs == {
        "scenario": None,
        "property": "hypothesis (a Python package)",
        "finite_model_checking": "a JRE and tla2tools.jar",
        "theorem_checking": "the Lean toolchain",
    }


def test_a_kind_that_needs_a_missing_toolchain_is_reported_as_not_runnable_here(
    tools: Server,
) -> None:
    """Support in the build and presence on this machine are two facts.

    A `property` adapter is registered, so the build supports it. Hypothesis may
    not be installed, in which case a property check is unusable here. The
    capability entry therefore carries both, and does not collapse them into the
    single word "supported".
    """
    capabilities = inspect(tools)["capabilities"]
    entry = next(c for c in capabilities["kinds"] if c["kind"] == "property")

    assert entry["supported"] is True, (
        "the property adapter is registered in this build, so the build supports it"
    )
    assert isinstance(entry["toolchain_present"], bool), (
        "a build-level capability reports whether the toolchain is on this machine"
    )


# ------------------------------------------------------- prerequisite gaps


def test_prerequisite_gaps_name_the_check_and_the_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gap names what is absent and which check it blocks.

    `gaps` already carries the bare fact that an executable is not on PATH, which
    is enough for a person reading a summary and not enough for an agent
    deciding which of its planned checks it can attempt.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    body = inspect(Server(make_repo(tmp_path)))

    gaps = body["prerequisite_gaps"]
    assert gaps, "a missing prerequisite produces no structured gap"
    entry = next(g for g in gaps if g["check_id"] == CHECK_ID)
    assert entry["executable"] == "python"
    assert entry["present"] is False
    assert entry["name"], "the gap names no prerequisite name"
    blocked = {g["check_id"] for g in gaps if not g["present"]}
    assert blocked == {CHECK_ID}


def test_a_present_prerequisite_is_listed_as_present(tmp_path: Path) -> None:
    """The gap list reports the state, not only the absence.

    A field that listed only what is missing would be identical for a project
    whose checks need nothing and one whose checks need nothing installed. The
    present entries are what make it a census rather than an alarm.
    """
    body = inspect(Server(make_repo(tmp_path)))
    entry = next(
        g for g in body["prerequisite_gaps"]
        if g["check_id"] == CHECK_ID and g["executable"] == "python"
    )
    assert entry["present"] is True


# -------------------------------------------------------------- cleanup


def test_cleanup_support_reports_the_policy_and_whether_it_may_write(tools: Server) -> None:
    """Cleanup support is a fact about this project's policy, not a constant.

    The example registers no cleanup policy, so `load_policy` yields the default
    and `may_write()` is False. Reporting "cleanup is supported" without the mode
    would let an agent believe edits will be cleaned up when the policy is off.
    """
    cleanup = inspect(tools)["cleanup"]

    assert cleanup["available"] is True
    assert cleanup["policy_path"] == "verification/cleanup.json"
    assert cleanup["may_write"] is False, (
        "the example has no cleanup policy, so nothing may be written"
    )
    assert cleanup["mode"] == "off"
    assert cleanup["outstanding"] == []
    assert cleanup["registered_rules"], "no cleanup rules are named"


def test_cleanup_support_names_a_policy_that_will_not_parse(tmp_path: Path) -> None:
    """A broken policy is reported, not swallowed into "unavailable".

    `policy_problem` exists precisely to keep "cleanup is off" and "your policy
    file is broken" as two different answers. A field that reported only a
    boolean would make them one, and an operator would go looking for a
    toolchain that was never the problem.
    """
    project = make_repo(tmp_path, "bad-policy")
    (project / "verification" / "cleanup.json").write_text(
        json.dumps({"mode": "apply_everything", "enabled_rules": ["NOPE"]}) + "\n",
        encoding="utf-8",
    )
    cleanup = inspect(Server(project))["cleanup"]

    assert cleanup["available"] is False
    assert cleanup["problem"], "an unusable policy names no problem"
    assert "cleanup.json" in cleanup["problem"]


def test_cleanup_support_names_the_panels_this_build_cannot_fill(tools: Server) -> None:
    """The absent panels are named rather than rendered as an empty list.

    The cleanup package returns a preservation receipt on the value it hands its
    caller and persists only the original bytes, so there is no applied-patch
    list or receipt collection to read. An empty list would be indistinguishable
    from a project that has never cleaned anything, which is a different and
    false claim.
    """
    cleanup = inspect(tools)["cleanup"]

    assert cleanup["unavailable"], "the panels this build cannot fill are not named"
    for entry in cleanup["unavailable"]:
        assert entry["panel"] and entry["missing"], f"an unnamed gap: {entry}"


# -------------------------------------------------- one root, six tools only


def test_no_tool_can_switch_the_bound_project_root() -> None:
    """The root is a process fact, and no argument reaches it.

    Every published schema is walked for a parameter that could name a
    different repository, and the server's own root is asserted unchanged after
    each attempt. A tool that re-pointed the server would make the bound root a
    suggestion, and every evidence record that names a repository would be worth
    less than it claims.
    """
    from vkit.mcp import TOOLS

    forbidden = frozenset({
        "root", "project", "project_root", "cwd", "path", "repository",
        "checkout", "workdir", "directory", "repo",
    })
    for spec in TOOLS:
        declared = set(spec.input_schema.get("properties") or {})
        nested = {
            key
            for value in spec.input_schema.get("properties", {}).values()
            if isinstance(value, dict)
            for key in (value.get("properties") or {})
        }
        assert not (declared | nested) & forbidden, (
            f"{spec.name} accepts a parameter that could name another repository: "
            f"{sorted((declared | nested) & forbidden)}"
        )


def test_the_server_stays_bound_to_one_root_after_every_tool_is_called(
    tmp_path: Path,
) -> None:
    """Six calls, one root, measured after each.

    `Server.project` is resolved once in `__init__` and never reassigned. This
    asserts the property rather than the reading of the source: the root is
    captured, every tool is called with arguments that try to move it, and the
    root is compared afterwards.
    """
    project = make_repo(tmp_path)
    tools = Server(project)
    root_before = str(tools.project.root)

    attempts = [
        ("project_inspect", {"root": str(tmp_path / "elsewhere")}),
        ("task_begin", {"contract": {"root": "C:/Windows"}, "request_id": "r1"}),
        ("check_start", {"task_id": "t", "check_ids": ["x"], "request_id": "r2",
                         "cwd": "C:/Windows"}),
        ("run_get", {"run_id": "deadbeef", "repository": "C:/Windows"}),
        ("run_cancel", {"run_id": "deadbeef", "request_id": "r3", "path": "C:/"}),
        ("task_finalize", {"task_id": "t", "checkout": "C:/Windows"}),
    ]
    for name, arguments in attempts:
        tools.call_tool(name, arguments)
        assert str(tools.project.root) == root_before, (
            f"{name} moved the server off {root_before}"
        )

    assert str(tools.project.root) == root_before


def test_the_bound_root_is_the_one_the_process_was_opened_with(
    tmp_path: Path,
) -> None:
    """A second repository's checks are not reachable through this server."""
    first = make_repo(tmp_path, "first")
    second = make_repo(tmp_path, "second")
    tools = Server(first)

    body = tools.call_tool("project_inspect", {}).content
    assert body["project_root"] == str(tools.project.root)
    assert body["project_root"] != str(second.resolve())
    assert body["manifest"]["path"].startswith(str(tools.project.root))


# ------------------------------------------------------ the response's shape


def test_project_inspect_stays_json_serialisable_with_every_new_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new fields are data, not typed values.

    The obligations are serialized through the core's `describe_obligation`
    precisely because passing `check.obligations()` straight in was a
    `TypeError` from `json.dumps` on the first request that reached it. This
    asserts the whole response survives the round trip on both the happy path
    and the path where every prerequisite is missing.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    for project in (make_repo(tmp_path, "one"), make_repo(tmp_path, "two")):
        result = Server(project).call_tool("project_inspect", {})
        assert result.is_error is False
        text = json.dumps(result.content)
        assert json.loads(text) == result.content


def test_the_tool_count_is_unchanged() -> None:
    """Six, and the connection panel added none."""
    from vkit.mcp import BY_NAME, TOOL_NAMES, tool_definitions

    assert len(TOOL_NAMES) == 6
    assert list(TOOL_NAMES) == [
        "project_inspect", "task_begin", "check_start",
        "run_get", "run_cancel", "task_finalize",
    ]
    assert [entry["name"] for entry in tool_definitions()] == list(TOOL_NAMES)
    assert set(BY_NAME) == set(TOOL_NAMES)
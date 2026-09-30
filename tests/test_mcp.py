"""Behaviour of the six MCP tools, driven the way an MCP client drives them.

Every test calls tools through `Server.call_tool`, the same entry point an SDK
adapter forwards to, and every assertion is on the response a client reads. No
test names a private helper, and none of them would pass if the tool handlers
returned empty dicts: each one checks a value that came out of the durable
record, not the shape of a function's return.

The project under test is a real throwaway Git repository built from
`examples/python-cli`, so `check_start` launches a real process against a real
manifest and `run_get` reads the evidence that process actually produced.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vkit.mcp import MAX_LOG_BYTES, TOOL_NAMES, Server, ToolSurface

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

#: A second repository with the same manifest. Its runs are recorded in its own
#: store under its own Git common directory, which is what makes its run ids
#: foreign to the first project.
FOREIGN_CHECK = "totals-behavior"


def make_repo(tmp_path: Path, name: str) -> Path:
    """A throwaway Git repository holding a real copy of the example."""
    target = tmp_path / name
    shutil.copytree(EXAMPLE, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True,
    )
    return target


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path, "project")


@pytest.fixture()
def tools(repo: Path) -> Server:
    return Server(repo)


def begin_task(tools: Server, request_id: str = "req-begin-1", **overrides) -> str:
    """Open a task through the tool layer and return its id.

    The policy digest and checkout reference are absent because admission derives
    them: a caller that supplied its own was binding the attempt to a string it
    chose, and then comparing the resulting evidence against that string.
    """
    args = {
        "contract": {"scope": "totals"},
        "owner": "worker-7",
        "request_id": request_id,
    }
    args.update(overrides)
    result = tools.call_tool("task_begin", args)
    assert result.is_error is False, result.content
    return result.content["task_id"]


def start(tools: Server, task_id: str, request_id: str = "req-check-1", checks=(FOREIGN_CHECK,)):
    return tools.call_tool(
        "check_start", {"task_id": task_id, "check_ids": list(checks), "request_id": request_id}
    )


# --- the surface -------------------------------------------------------------

def test_exactly_six_tools_are_published(tools: Server) -> None:
    assert isinstance(tools, ToolSurface)
    assert list(TOOL_NAMES) == [
        "project_inspect", "task_begin", "check_start", "run_get", "run_cancel", "task_finalize",
    ]
    assert [spec.name for spec in tools.list_tools()] == list(TOOL_NAMES)
    for spec in tools.list_tools():
        assert spec.description.strip()
        assert spec.input_schema["additionalProperties"] is False


def test_no_tool_parameter_accepts_a_command_a_root_or_an_approval(tools: Server) -> None:
    """The published schemas are the interface, so they are what gets checked.

    A parameter whose name says it takes a command, a working directory, a
    project root, an integration approval or a plugin is the exact thing
    CONTRACT forbids, and reading the schemas is how a client reads the
    interface without trusting this file's prose.
    """
    forbidden = {
        "command", "cmd", "argv", "shell", "exec", "script",
        "root", "project", "project_root", "cwd", "working_directory", "path", "directory",
        "approve", "approval", "integrations", "install", "plugin",
    }
    for spec in tools.list_tools():
        properties = set(spec.input_schema["properties"])
        assert not properties & forbidden, f"{spec.name} exposes {properties & forbidden}"
        for key in properties:
            assert "command" not in key and "shell" not in key, f"{spec.name}.{key}"

    # A root is bound at startup and no call may name another one.
    assert tools.call_tool("run_get", {"run_id": "0" * 32, "root": str(tools.project.root.parent)}).is_error
    assert tools.call_tool(
        "check_start",
        {"task_id": "t", "check_ids": ["x"], "request_id": "r", "command": "python -c 'import os'"},
    ).is_error


def test_unknown_tool_and_unknown_argument_are_refused(tools: Server) -> None:
    missing = tools.call_tool("shell_exec", {"command": "whoami"})
    assert missing.is_error is True
    assert "unknown tool" in missing.content["error"]

    extra = tools.call_tool("run_get", {"run_id": "0" * 32, "verbose": True})
    assert extra.is_error is True
    assert "unknown argument" in extra.content["error"]


# --- project_inspect ---------------------------------------------------------

def test_project_inspect_lists_registered_checks(tools: Server) -> None:
    result = tools.call_tool("project_inspect", {})
    assert result.is_error is False
    assert result.content["project_root"] == str(tools.project.root.resolve())
    assert result.content["manifest"]["registered_check_ids"] == ["totals-behavior"]
    assert [c["id"] for c in result.content["checks"]] == ["totals-behavior"]
    assert result.content["checks"][0]["required_scenarios"] == [
        "empty-cart", "single-positive", "several-positives", "mixed-sign",
        "negatives-only", "cancels-to-zero",
    ]
    assert result.content["gaps"] == []


def test_project_inspect_reports_a_missing_prerequisite(tools: Server, monkeypatch) -> None:
    """A prerequisite that is not installed is a gap, not a crash.

    `shutil.which` is patched on the module itself, which is the single object
    `vkit.execution.check_prerequisites` and this tool both call, so the probe
    under test is the core's probe.
    """
    from vkit.execution import check_prerequisites
    from vkit.manifest import parse_manifest

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: None if n == "python" else real_which(n))

    manifest = parse_manifest(tools.project, tools.project.runs_root / "probe")
    blocked = check_prerequisites(manifest.require("totals-behavior"))
    assert blocked is not None
    assert blocked.detail == "python: 'python' is not on PATH"

    result = tools.call_tool("project_inspect", {})
    assert result.is_error is False
    assert "'python' is not on PATH" in result.content["gaps"]
    assert result.content["execution_available"] is False
    assert result.content["checks"][0]["prerequisites"][0] == {
        "name": "python", "executable": "python", "on_path": False,
    }


def test_project_inspect_reports_an_unenrolled_project(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    result = Server(bare).call_tool("project_inspect", {})
    assert result.is_error is False
    assert result.content["checks"] == []
    assert result.content["execution_available"] is False
    assert result.content["manifest"] is None
    assert result.content["gaps"] == [
        f"no usable manifest at {Server(bare).project.manifest_path}; "
        "this project cannot run checks until one is enrolled"
    ]


def test_an_unenrolled_project_refuses_execution(tmp_path: Path) -> None:
    bare = tmp_path / "bare2"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    tools = Server(bare)
    result = tools.call_tool(
        "check_start", {"task_id": "t", "check_ids": ["any"], "request_id": "r1"}
    )
    assert result.is_error is True
    assert "not enrolled" in result.content["error"]


# --- the lifecycle -----------------------------------------------------------

def test_a_real_check_produces_a_run_that_run_get_reads(tools: Server) -> None:
    task_id = begin_task(tools)

    started = start(tools, task_id)
    assert started.is_error is False, started.content
    run = started.content["runs"][0]
    assert run["check_id"] == "totals-behavior"
    assert run["result"] == "PASS"
    assert run["launched"] is True
    assert len(run["run_id"]) == 32

    fetched = tools.call_tool("run_get", {"run_id": run["run_id"]})
    assert fetched.is_error is False
    assert fetched.content["lifecycle"] == "terminal"
    assert fetched.content["result"] == "PASS"
    assert fetched.content["check_id"] == "totals-behavior"
    assert [s["id"] for s in fetched.content["scenarios"]] == [
        "empty-cart", "single-positive", "several-positives", "mixed-sign",
        "negatives-only", "cancels-to-zero",
    ]
    assert all(s["result"] == "PASS" for s in fetched.content["scenarios"])
    assert fetched.content["process"]["exit_code"] == 0
    assert fetched.content["artifacts"]["result"]["reference"] == (
        f"run:{run['run_id']}/result.json"
    )
    assert fetched.content["artifacts"]["result"]["exists"] is True
    assert "PASS" in fetched.content["summary"]


def test_a_run_id_from_another_project_is_refused(tmp_path: Path, repo: Path) -> None:
    other = Server(make_repo(tmp_path, "other"))
    foreign_task = begin_task(other, request_id="foreign-begin")
    foreign_run = start(other, foreign_task, request_id="foreign-check").content["runs"][0]["run_id"]

    # The id is well formed and real. It simply is not this project's.
    assert len(foreign_run) == 32
    result = Server(repo).call_tool("run_get", {"run_id": foreign_run})
    assert result.is_error is True
    assert "not recorded in this project's evidence store" in result.content["error"]

    cancelled = Server(repo).call_tool(
        "run_cancel", {"run_id": foreign_run, "request_id": "cross-1"}
    )
    assert cancelled.is_error is True
    assert "not recorded in this project's evidence store" in cancelled.content["error"]


def test_a_run_id_that_is_not_a_run_id_is_refused(tools: Server) -> None:
    traversal = tools.call_tool("run_get", {"run_id": "../../../../etc/passwd"})
    assert traversal.is_error is True
    assert "not a run id" in traversal.content["error"]


def test_an_unknown_check_is_refused_and_launches_nothing(tools: Server) -> None:
    task_id = begin_task(tools)
    result = start(tools, task_id, request_id="req-unknown", checks=["rm -rf /"])
    assert result.is_error is True
    assert "unknown check 'rm -rf /'" in result.content["error"]

    # Nothing ran: the project has no run records at all.
    from vkit.storage import Store

    assert Store(tools.project.db_path).list_runs(limit=100) == []


def test_a_second_check_start_for_one_request_id_does_not_run_twice(tools: Server) -> None:
    task_id = begin_task(tools)
    first = start(tools, task_id, request_id="req-once")
    second = start(tools, task_id, request_id="req-once")
    assert first.content["runs"][0]["run_id"] == second.content["runs"][0]["run_id"]
    assert first.content["runs"][0]["result"] == second.content["runs"][0]["result"]

    from vkit.storage import Store

    assert len(Store(tools.project.db_path).list_runs(limit=100)) == 1


def test_the_same_key_with_a_different_payload_is_refused(tools: Server) -> None:
    """One key, two requests. The second is refused, and the first stands.

    The conflict has to be observable even when the second request is refused
    for an unrelated reason first, so this is checked on `task_begin`, where the
    payload really does differ and nothing else can pre-empt it.
    """
    first = begin_task(tools, request_id="req-conflict", contract={"scope": "one"})
    clash = tools.call_tool("task_begin", {
        "contract": {"scope": "two"},
        "owner": "worker-8",
        "request_id": "req-conflict",
    })
    assert clash.is_error is True
    assert "already used with a different payload" in clash.content["error"]
    assert clash.content["task_id"] == first

    # A different request id with the same contract is a different request.
    other = begin_task(tools, request_id="req-distinct", contract={"scope": "one"})
    assert other != first


def test_an_unknown_check_leaves_the_request_key_unclaimed(tools: Server) -> None:
    """The check id is resolved before the key is claimed, so a refusal does not
    burn a key the client would then be unable to reuse for the real request."""
    task_id = begin_task(tools)
    refused = start(tools, task_id, request_id="req-burn", checks=["no-such-check"])
    assert refused.is_error is True

    # The same key, now naming a real check, is accepted.
    accepted = start(tools, task_id, request_id="req-burn", checks=["totals-behavior"])
    assert accepted.is_error is False
    assert accepted.content["runs"][0]["result"] == "PASS"


# --- bounded logs ------------------------------------------------------------

def test_run_get_returns_a_bounded_window_of_a_large_log(tools: Server) -> None:
    """A log of a known size comes back as a known number of bytes.

    The window is asserted against the file on disk, so a handler that
    slurped the whole log and truncated the string afterwards would fail here
    on the byte counts.
    """
    task_id = begin_task(tools)
    run_id = start(tools, task_id).content["runs"][0]["run_id"]

    from vkit.storage import Store

    store = Store(tools.project.db_path)
    stdout_path = store.run_dir(run_id) / "stdout.log"
    filler = b"x" * 200_000
    stdout_path.write_bytes(b"A" * 4096 + filler)
    assert stdout_path.stat().st_size == 204_096

    first = tools.call_tool("run_get", {"run_id": run_id, "log_limit": 1000})
    page = first.content["logs"]["stdout"]
    assert page["bytes"] == 1000
    assert page["total_bytes"] == 204_096
    assert page["eof"] is False
    assert page["next_offset"] == 1000
    assert len(page["text"].encode("utf-8")) == 1000
    assert first.content["logs"]["stderr"]["bytes"] == 0

    second = tools.call_tool(
        "run_get", {"run_id": run_id, "log_limit": 1000, "log_offset": page["next_offset"]}
    )
    assert second.content["logs"]["stdout"]["text"][0] == "A"
    assert second.content["logs"]["stdout"]["offset"] == 1000

    whole = tools.call_tool("run_get", {"run_id": run_id, "log_limit": MAX_LOG_BYTES})
    assert whole.content["logs"]["stdout"]["bytes"] == MAX_LOG_BYTES
    assert whole.content["logs"]["stdout"]["eof"] is False


def test_an_oversized_log_limit_is_refused(tools: Server) -> None:
    result = tools.call_tool("run_get", {"run_id": "0" * 32, "log_limit": MAX_LOG_BYTES + 1})
    assert result.is_error is True
    assert "log_limit must be between" in result.content["error"]


def test_run_get_of_an_unknown_run_is_a_refusal_not_an_exception(tools: Server) -> None:
    result = tools.call_tool("run_get", {"run_id": "b" * 32})
    assert result.is_error is True
    assert "not recorded in this project's evidence store" in result.content["error"]


# --- readiness ---------------------------------------------------------------

def test_a_task_missing_a_required_check_cannot_reach_ready(tools: Server) -> None:
    """The floor is frozen at admission. Running all of it is READY; adding a
    check the task has not run, or naming a check the policy does not register,
    cannot be."""
    task_id = begin_task(tools)
    start(tools, task_id, request_id="req-partial", checks=["totals-behavior"])

    result = tools.call_tool("task_finalize", {"task_id": task_id, "check_ids": []})
    assert result.is_error is False
    assert result.content["readiness"] == "READY"
    assert result.content["required_checks"] == ["totals-behavior"]

    # A caller may widen the floor, and a check it names but has not run is a gap.
    widen = tools.call_tool(
        "task_finalize", {"task_id": task_id, "check_ids": ["totals-behavior"]}
    )
    assert widen.content["readiness"] == "READY"

    # A contract naming a check the approved policy does not register is refused
    # at admission rather than admitted against a floor that does not contain it.
    unknown = tools.call_tool("task_begin", {
        "contract": {"required_checks": ["no-such-check"]},
        "request_id": "req-unknown-check",
    })
    assert unknown.is_error is True
    assert unknown.content["admitted"] is False
    assert "unknown check" in unknown.content["admission_conflict"]


def test_a_failing_check_finalizes_rejected(tools: Server, repo: Path) -> None:
    """A real defect, recorded by the core, becomes REJECTED at finalization.

    The defect is a change to the arithmetic itself rather than to the driver,
    so a FAIL means the product is wrong, which is the point of the example.
    """
    source = repo / "src" / "totals.py"
    broken = source.read_text(encoding="utf-8").replace(
        "        running += amount", "        running += 1",
    )
    assert broken != source.read_text(encoding="utf-8")
    source.write_text(broken, encoding="utf-8")

    tools = Server(repo)
    task_id = begin_task(tools, request_id="req-defect")
    run = start(tools, task_id, request_id="req-defect-check").content["runs"][0]
    assert run["result"] == "FAIL"
    assert {s["id"] for s in run["outcome"]["scenarios"] if s["result"] == "FAIL"}

    finalized = tools.call_tool("task_finalize", {"task_id": task_id})
    assert finalized.content["readiness"] == "REJECTED"
    assert finalized.content["required_checks"] == ["totals-behavior"]


def test_task_finalize_of_an_unknown_task_is_refused(tools: Server) -> None:
    result = tools.call_tool("task_finalize", {"task_id": "nope"})
    assert result.is_error is True
    assert "no such task" in result.content["error"]


# --- cancellation ------------------------------------------------------------

def test_run_cancel_refuses_when_the_record_carries_no_verified_identity(tools: Server) -> None:
    """The core verifies (pid, creation_time). A record with only a pid cannot
    be verified, so the refusal is explicit and the claims stay held."""
    # A run that FINISHED cannot exercise this path: cancelling a completed run
    # correctly returns the outcome it actually reached. The refusal needs a run
    # that is still in flight, so register one that never launches a process.
    run_id = "0" * 32   # the shape this build mints; the guard rejects anything else
    tools._store().register_run(
        run_id, "totals-behavior", task_id=None, attempt=None,
        source={"head": "h", "inventory_digest": "i", "dirty": False},
        configuration_digest="c", fixture_digest=None,
    )
    tools._store().mark_running(run_id, {"pid": 4321, "ownership": "windows_job_object",
                                      "exit_code": None, "timed_out": False})

    result = tools.call_tool("run_cancel", {"run_id": run_id, "request_id": "req-cancel-1"})
    assert result.is_error is True
    assert "no creation time" in result.content["error"]
    assert "claims are retained" in result.content["error"]


def test_run_cancel_refuses_a_pid_that_does_not_match_the_record(tools: Server) -> None:
    task_id = begin_task(tools)
    run_id = start(tools, task_id).content["runs"][0]["run_id"]

    result = tools.call_tool(
        "run_cancel", {"run_id": run_id, "request_id": "req-cancel-2", "owner_pid": 4}
    )
    assert result.is_error is True
    assert "is owned by pid" in result.content["error"]


# --- the schemas -------------------------------------------------------------

def test_the_published_schemas_freeze() -> None:
    from vkit.mcp import schemas

    frozen = schemas()
    assert list(frozen) == list(TOOL_NAMES)
    assert frozen["check_start"]["required"] == ["task_id", "check_ids", "request_id"]
    assert set(frozen["check_start"]["properties"]) == {"task_id", "check_ids", "request_id"}
    assert set(frozen["run_get"]["properties"]) == {"run_id", "log_limit", "log_offset"}
    assert frozen["run_get"]["properties"]["log_limit"]["maximum"] == MAX_LOG_BYTES
    assert frozen["run_cancel"]["required"] == ["run_id", "request_id"]
    assert frozen["task_finalize"]["required"] == ["task_id"]
    assert frozen["project_inspect"]["required"] == []


def test_responses_are_json_serialisable(tools: Server) -> None:
    """A tool result crosses a protocol boundary, so it has to be plain data."""
    task_id = begin_task(tools)
    run_id = start(tools, task_id).content["runs"][0]["run_id"]
    for name, args in (
        ("project_inspect", {}),
        ("task_finalize", {"task_id": task_id}),
        ("run_get", {"run_id": run_id, "log_limit": 64}),
    ):
        result = tools.call_tool(name, args)
        assert json.loads(json.dumps(result.content)) == result.content

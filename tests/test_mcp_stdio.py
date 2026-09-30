"""The MCP stdio transport, driven as a real client over real JSON-RPC frames.

`tests/test_mcp.py` covers the six tools through `Server.call_tool`, which is the
function an adapter forwards to. That proves the tools. It cannot prove the
transport, because calling a function in-process never asks the server to
parse a frame, negotiate a version, or answer with an `isError`. This file closes
that gap, and every test here spawns the shipped `vkit mcp serve` as a
subprocess and writes bytes to its stdin.

**No test in this file mocks the transport.** The client in `mcp_client.py`
deliberately does not import `mcp` or `vkit`; it assembles frames by hand, so it
would notice an SDK that changed the wire format rather than agreeing with the
server about it. A test that could only pass by patching the stream would not
close the gap, and none is written.

The project under test is a real throwaway Git repository built from
`examples/python-cli`, so `check_start` makes the server launch a real process
against a real manifest, and the verdicts asserted here are the ones that
process actually produced.

**What these tests do not cover.** They prove protocol compliance between a real
client and this server. They say nothing about whether Claude Code loads the
plugin, discovers the tools, or grants a subagent access to them, which is a
different question about a different program.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_client import CLIENT_PROTOCOL, StdioClient

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
TOOL_NAMES = [
    "project_inspect", "task_begin", "check_start", "run_get", "run_cancel", "task_finalize",
]

#: A whole lifecycle launches a process and waits on it, so the bound is generous
#: enough for a loaded Windows CI box and short enough that a hang fails rather
#: than sitting forever.
LIFECYCLE_TIMEOUT = 600.0


def make_repo(tmp_path: Path, name: str = "project") -> Path:
    target = tmp_path / name
    shutil.copytree(EXAMPLE, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True,
    )
    return target


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One repository for the read-only handshake and listing checks.

    Module scope because a subprocess handshake is a second of work each, and
    nothing in the protocol shape depends on the repository's contents.
    """
    return make_repo(tmp_path_factory.mktemp("protocol"), "project")


@pytest.fixture()
def connected(repo: Path):
    """A live, handshaken server, torn down at the end of the test.

    The exit code is asserted on the way out, so a server that only survives
    while a test is watching it cannot pass. A test that kills the process on
    purpose says so, and this does not hold the kill against it.
    """
    client = StdioClient(repo).start()
    try:
        yield client
    finally:
        code = client.close()
    if not client.killed:
        assert code == 0, (
            f"vkit mcp serve exited with {code}; stderr: {client.stderr_lines[-6:]}"
        )


def cli_json(argv: list[str]) -> dict:
    """One `vkit` command's parsed JSON, run in this interpreter.

    `sys.executable` rather than the bare name on PATH, so the CLI under test is
    this checkout and not whichever install happens to lead.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")])
    done = subprocess.run(
        [sys.executable, "-m", "vkit.cli", *argv, "--json"],
        capture_output=True, encoding="utf-8", timeout=300, env=env, cwd=str(REPO_ROOT),
    )
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    return json.loads(done.stdout)


def begin_task(client: StdioClient, request_id: str = "req-begin-1", **extra) -> str:
    result = client.call("task_begin", {
        "contract": {"scope": "totals"},
        "policy_digest": "policy-sha-0001",
        "checkout_ref": "refs/heads/main",
        "owner": "protocol-suite",
        "request_id": request_id,
        **extra,
    })
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])["task_id"]


# --- the handshake -----------------------------------------------------------

def test_the_server_completes_a_handshake_and_negotiates_a_version(connected) -> None:
    """A client that says which version it speaks gets that version back, plus
    a server name and only the capabilities that are wired.

    Capabilities are asserted for what is absent as much as present: a server
    that advertised prompts, resources or completions would be claiming a feature
    `serve_stdio` never registers, and a client would then call into a hole.
    """
    handshake = connected.handshake()
    assert handshake["protocolVersion"] == CLIENT_PROTOCOL
    assert handshake["serverInfo"]["name"] == "project"
    assert handshake["serverInfo"]["version"]
    assert set(handshake["capabilities"]) == {"experimental", "tools"}
    assert handshake["capabilities"]["tools"] == {"listChanged": False}


@pytest.mark.parametrize(
    ("asked", "answered"),
    [
        ("2024-11-05", "2024-11-05"),
        ("2025-06-18", "2025-06-18"),
        # A version this server cannot serve is not echoed back, because echoing
        # it would claim compatibility. The SDK names what it does serve.
        ("2026-07-28", "2025-11-25"),
        ("1999-01-01", "2025-11-25"),
        ("banana", "2025-11-25"),
    ],
    ids=["oldest-supported", "mid-range", "newer-than-server", "nonsense", "not-a-version"],
)
def test_a_client_gets_back_a_version_the_server_actually_serves(
    repo: Path, asked: str, answered: str
) -> None:
    """Negotiation is a real answer, not a rubber stamp.

    Every version in the handshake ladder comes back unchanged; anything outside
    it comes back as the newest version this server speaks. A server that echoed
    whatever it was asked for would be asserting compatibility it does not have,
    and two peers would then disagree about what a field means.

    The "banana" row is the honest one. A malformed version is treated as an
    unsupported one, not as a fault, and the session survives either way.
    """
    client = StdioClient(repo).start(handshake=False)
    try:
        reply = client.request("initialize", {
            "protocolVersion": asked,
            "capabilities": {},
            "clientInfo": {"name": "vkit-protocol-suite", "version": "1.0.0"},
        })
        assert reply["result"]["protocolVersion"] == answered
        assert [t["name"] for t in client.list_tools()] == TOOL_NAMES
    finally:
        client.kill()


# --- the tool list -----------------------------------------------------------

def test_tools_list_publishes_exactly_the_six_tools_with_their_schemas(connected) -> None:
    """The wire list is the same six names, the same schemas, the same hints.

    Every assertion reads the frame a client received, so a transport that
    dropped a tool or reworded a schema would fail here rather than at a host.
    """
    tools = connected.list_tools()
    assert [t["name"] for t in tools] == TOOL_NAMES

    from vkit.mcp import schemas

    frozen = schemas()
    for tool in tools:
        assert tool["inputSchema"] == frozen[tool["name"]], tool["name"]
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["description"].strip()
        annotations = tool["annotations"]
        assert annotations["title"] == tool["name"]
        assert isinstance(annotations["readOnlyHint"], bool)
        assert isinstance(annotations["idempotentHint"], bool)
        assert isinstance(annotations["destructiveHint"], bool)

    by_name = {t["name"]: t for t in tools}
    assert by_name["project_inspect"]["annotations"]["readOnlyHint"] is True
    assert by_name["run_cancel"]["annotations"]["destructiveHint"] is True
    assert by_name["run_get"]["annotations"]["readOnlyHint"] is True


# --- a whole lifecycle over the wire -----------------------------------------

def test_a_client_drives_the_python_example_to_ready_over_the_wire(tmp_path: Path) -> None:
    """The Plan 03 acceptance row, end to end, with nothing patched.

    Inspect, begin a task, start a real check, read the evidence it produced,
    and finalize. Every value asserted is one read off the wire; the run itself
    is a real `python verify_totals.py` launched by the server.
    """
    project = make_repo(tmp_path, "lifecycle")
    with StdioClient(project) as client:
        inspected = client.call_body("project_inspect", {})
        assert inspected["project_root"] == str(project.resolve())
        assert inspected["manifest"]["registered_check_ids"] == ["totals-behavior"]
        assert inspected["gaps"] == []
        assert inspected["execution_available"] is True

        task_id = begin_task(client)

        started = client.call("check_start", {
            "task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-check-1",
        }, timeout=LIFECYCLE_TIMEOUT)
        assert started["isError"] is False, started
        run = json.loads(started["content"][0]["text"])["runs"][0]
        assert run["result"] == "PASS"
        assert run["launched"] is True
        assert len(run["run_id"]) == 32

        fetched = client.call_body("run_get", {"run_id": run["run_id"], "log_limit": 4096})
        assert fetched["lifecycle"] == "terminal"
        assert fetched["result"] == "PASS"
        assert fetched["check_id"] == "totals-behavior"
        assert [s["id"] for s in fetched["scenarios"]] == [
            "empty-cart", "single-positive", "several-positives", "mixed-sign",
            "negatives-only", "cancels-to-zero",
        ]
        assert all(s["result"] == "PASS" for s in fetched["scenarios"])
        assert fetched["process"]["exit_code"] == 0
        assert fetched["artifacts"]["result"]["exists"] is True
        assert "PASS" in fetched["summary"]

        finalized = client.call_body("task_finalize", {"task_id": task_id})
        assert finalized["readiness"] == "READY"
        assert finalized["gaps"] == []
        assert finalized["required_checks"] == ["totals-behavior"]

        assert client.close() == 0


def test_the_cli_and_the_wire_report_the_same_run(tmp_path: Path) -> None:
    """The two surfaces cannot describe different evidence.

    The CLI reads the same durable store the server wrote, so this is the check
    that an MCP answer and a shell answer are the same fact rather than a
    parallel implementation of it.
    """
    project = make_repo(tmp_path, "same-run")
    with StdioClient(project) as client:
        task_id = begin_task(client)
        started = client.call_body(
            "check_start",
            {"task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-same"},
            timeout=LIFECYCLE_TIMEOUT,
        )
        run = started["runs"][0]
        client.close()

    report = cli_json(["run", "show", "--project", str(project), "--run", run["run_id"]])

    assert report["run_id"] == run["run_id"]
    assert report["check_id"] == "totals-behavior"
    # The whole outcome object, not just its result. A surface that agreed on
    # PASS but disagreed about the scenarios would still be a second, divergent
    # implementation of the same fact.
    assert report["outcome"] == run["outcome"]
    assert report["process"]["exit_code"] == 0
    assert isinstance(report["process"]["pid"], int)
    # The ownership value names the mechanism that contained this run, and the
    # schema admits exactly two. The row's subject is that the CLI and the wire
    # report the SAME run, so what matters is that both carry the value the run
    # reported, and that it is the one this host's mechanism produces. Pinning
    # the Windows value made the row fail wherever the other mechanism ran, which
    # says nothing about whether the two surfaces agree.
    expected = "windows_job_object" if sys.platform == "win32" else "posix_process_group"
    assert report["process"]["ownership"] == expected
    assert report["process"]["timed_out"] is False
    assert report["artifacts"] == {"result": "result.json"}


# --- a stored FAIL must not become a protocol error --------------------------

def test_a_real_defect_comes_back_as_a_verdict_not_a_failed_call(tmp_path: Path) -> None:
    """A FAIL is a recorded answer, so `isError` is false and the body says FAIL.

    This is the load-bearing honesty test. If a FAIL were reported as a protocol
    error a host would treat it as a broken call and might retry; if it were
    reported as a PASS the whole product would be worthless. The only correct
    encoding is a successful call whose body reads FAIL, and finalization then
    has to derive REJECTED from it.
    """
    project = make_repo(tmp_path, "defect")
    source = project / "src" / "totals.py"
    broken = source.read_text(encoding="utf-8").replace(
        "        running += amount", "        running += 1",
    )
    assert broken != source.read_text(encoding="utf-8")
    source.write_text(broken, encoding="utf-8")

    with StdioClient(project) as client:
        task_id = begin_task(client)
        result = client.call("check_start", {
            "task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-defect",
        }, timeout=LIFECYCLE_TIMEOUT)
        assert result["isError"] is False, result

        body = json.loads(result["content"][0]["text"])
        run = body["runs"][0]
        assert run["result"] == "FAIL"
        assert {s["id"] for s in run["outcome"]["scenarios"] if s["result"] == "FAIL"}

        refetched = client.call_body("run_get", {"run_id": run["run_id"]})
        assert refetched["result"] == "FAIL"
        assert "FAIL" in refetched["summary"]

        finalized = client.call_body("task_finalize", {"task_id": task_id})
        assert finalized["readiness"] == "REJECTED"
        assert finalized["gaps"] == []

        assert client.close() == 0


def test_a_stored_fail_still_reads_as_fail_on_a_second_connection(tmp_path: Path) -> None:
    """Reconnecting does not re-derive the verdict, and a retry runs nothing.

    A FAIL recorded in the first session is re-read verbatim in the second, and
    replaying the same request id returns the same run rather than executing the
    check again. This is what "never re-derive an outcome" means on the wire.
    """
    project = make_repo(tmp_path, "reconnect")
    source = project / "src" / "totals.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace("        running += amount", "        running += 1"),
        encoding="utf-8",
    )

    with StdioClient(project) as first:
        task_id = begin_task(first)
        started = first.call_body(
            "check_start",
            {"task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-once"},
            timeout=LIFECYCLE_TIMEOUT,
        )
        run_id = started["runs"][0]["run_id"]
        assert started["runs"][0]["result"] == "FAIL"
        first.close()

    with StdioClient(project) as second:
        again = second.call_body("run_get", {"run_id": run_id})
        assert again["result"] == "FAIL"
        assert [s["id"] for s in again["scenarios"]] == [
            s["id"] for s in started["runs"][0]["outcome"]["scenarios"]
        ]

        replay = second.call_body(
            "check_start",
            {"task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-once"},
            timeout=LIFECYCLE_TIMEOUT,
        )
        assert replay["runs"][0]["run_id"] == run_id
        assert replay["runs"][0]["replayed"] is True
        assert replay["runs"][0]["result"] == "FAIL"
        second.close()


# --- refusals are answers, not faults ----------------------------------------

def test_a_refused_request_comes_back_as_iserror_not_a_protocol_error(connected) -> None:
    """Every refusal shape a client can provoke is a well-formed result.

    A refusal is an answer, so it arrives as `isError: true` with the reason in
    the body. A JSON-RPC error would mean the transport failed, and a client
    cannot tell a tool that declined from a server that broke.
    """
    unknown_tool = connected.call("shell_exec", {"command": "whoami"})
    assert unknown_tool["isError"] is True
    assert "unknown tool" in json.loads(unknown_tool["content"][0]["text"])["error"]

    unknown_arg = connected.call("run_get", {"run_id": "0" * 32, "verbose": True})
    assert unknown_arg["isError"] is True
    assert "unknown argument" in json.loads(unknown_arg["content"][0]["text"])["error"]

    missing_required = connected.call("task_begin", {"request_id": "r1"})
    assert missing_required["isError"] is True

    bad_type = connected.call("run_get", {"run_id": "b" * 32, "log_limit": 999999})
    assert bad_type["isError"] is True
    assert "log_limit must be between" in json.loads(bad_type["content"][0]["text"])["error"]

    traversal = connected.call("run_get", {"run_id": "../../../../etc/passwd"})
    assert traversal["isError"] is True
    assert "not a run id" in json.loads(traversal["content"][0]["text"])["error"]

    unknown_check = connected.call(
        "check_start", {"task_id": "t", "check_ids": ["rm -rf /"], "request_id": "r2"}
    )
    assert unknown_check["isError"] is True
    assert "unknown check" in json.loads(unknown_check["content"][0]["text"])["error"]


def test_a_cross_project_run_id_is_refused_over_the_wire(tmp_path: Path) -> None:
    """An id from another repository is refused, not served.

    Two repositories with the same manifest, so the run is real and the id is
    well formed. Only its ownership is wrong, which is the case a shape check
    cannot catch.
    """
    here = make_repo(tmp_path, "here")
    other = make_repo(tmp_path, "other")

    with StdioClient(other) as elsewhere:
        foreign_task = begin_task(elsewhere, request_id="foreign-begin")
        foreign = elsewhere.call_body(
            "check_start",
            {"task_id": foreign_task, "check_ids": ["totals-behavior"], "request_id": "foreign-check"},
            timeout=LIFECYCLE_TIMEOUT,
        )["runs"][0]["run_id"]
        assert len(foreign) == 32
        elsewhere.close()

    with StdioClient(here) as client:
        fetched = client.call("run_get", {"run_id": foreign})
        assert fetched["isError"] is True
        assert "not recorded in this project's evidence store" in json.loads(
            fetched["content"][0]["text"]
        )["error"]

        cancelled = client.call("run_cancel", {"run_id": foreign, "request_id": "cross-1"})
        assert cancelled["isError"] is True
        assert "not recorded in this project's evidence store" in json.loads(
            cancelled["content"][0]["text"]
        )["error"]
        client.close()


def test_one_request_id_naming_two_contracts_is_refused_on_the_wire(tmp_path: Path) -> None:
    """Reusing a key for a different request is refused, and the first stands."""
    project = make_repo(tmp_path, "conflict")
    with StdioClient(project) as client:
        first = begin_task(client, request_id="req-conflict", contract={"scope": "one"})
        clash = client.call("task_begin", {
            "contract": {"scope": "two"},
            "policy_digest": "policy-sha-0001",
            "checkout_ref": "refs/heads/main",
            "request_id": "req-conflict",
        })
        assert clash["isError"] is True
        body = json.loads(clash["content"][0]["text"])
        assert "already used with a different payload" in body["error"]
        assert body["task_id"] == first
        client.close()


# --- the server must not die, and must not lie -------------------------------

def test_a_malformed_frame_is_ignored_and_the_session_continues(connected) -> None:
    """Garbage on stdin does not kill the server or corrupt its output.

    Four unparseable lines go in, then a real request. The request is answered
    with the right id, which is what a client needs: a transport that died would
    leave it waiting forever, and one that echoed the garbage back would corrupt
    the stream.
    """
    for junk in ("{this is not json", "[]", '{"jsonrpc":"2.0"}', "not a frame at all"):
        connected.send_raw(junk)

    tools = connected.request("tools/list", {})["result"]["tools"]
    assert [t["name"] for t in tools] == TOOL_NAMES
    connected.assert_alive()

    inspected = connected.call_body("project_inspect", {})
    assert inspected["manifest"]["registered_check_ids"] == ["totals-behavior"]


def test_a_structurally_invalid_frame_is_rejected_without_stopping_the_server(
    connected,
) -> None:
    """A JSON-RPC envelope the SDK cannot parse is answered or ignored, and the
    next real request still works. Either is acceptable; dying is not."""
    connected.send_raw(json.dumps({
        "jsonrpc": "2.0", "id": 700, "method": "tools/call", "params": {"name": 12345},
    }))
    connected.send_raw(json.dumps({"jsonrpc": "2.0", "id": 701, "method": 99}))

    # Drain whatever the server chose to answer, if anything, without asserting
    # a count: the contract is only that it stays alive and well-framed.
    connected.assert_alive()
    tools = connected.request("tools/list", {})["result"]["tools"]
    assert [t["name"] for t in tools] == TOOL_NAMES


def test_an_unknown_method_is_a_protocol_error_and_the_server_keeps_serving(
    connected,
) -> None:
    """A method the server does not implement gets a JSON-RPC error, not a result.

    This is the one case where a protocol error is the correct answer, and it
    has to be distinguishable from a tool refusal: the client asked for something
    outside the protocol rather than something the server declined.
    """
    error = connected.request_error("tools/explode", {})
    assert error["code"] == -32601, error
    assert "tools/explode" in error.get("message", "") or "not found" in error.get("message", "").lower()

    connected.assert_alive()
    assert [t["name"] for t in connected.list_tools()] == TOOL_NAMES


def test_an_internal_fault_never_comes_back_as_a_verdict(tmp_path: Path) -> None:
    """A fault inside the server is a protocol error, and the server survives it.

    The state root is replaced with a regular file, so opening the evidence store
    raises `FileExistsError` inside `Server.call_tool`. Nothing catches an OSError
    there, and that is the design: the SDK turns it into a JSON-RPC error, writes
    the traceback to stderr, and keeps serving.

    The test asserts the two things that matter. The client gets an error rather
    than a body, so no client can read a broken store as PASS. And the next
    request is answered, so one fault does not end the session.

    The code is 0, not `-32603`. That is the SDK's choice and not this server's:
    `mcp.shared.jsonrpc_dispatcher` writes `ErrorData(code=0, ...)` for an
    unrecognised handler exception, with a `TODO(L58)` saying it pins
    existing-server compatibility where JSON-RPC specifies INTERNAL_ERROR. The
    number is pinned here so a change is noticed; the guarantee under test is the
    absence of a verdict, which does not depend on it.
    """
    project = make_repo(tmp_path, "fault")
    state_root = project / ".git" / "verification-kit"
    state_root.parent.mkdir(parents=True, exist_ok=True)
    state_root.write_text("not a directory", encoding="utf-8")

    with StdioClient(project) as client:
        reply = client.request("tools/call", {
            "name": "task_begin",
            "arguments": {
                "contract": {"scope": "totals"},
                "policy_digest": "p",
                "checkout_ref": "refs/heads/main",
                "request_id": "req-fault",
            },
        })
        assert "error" in reply, f"an internal fault answered with a result: {reply}"
        # No verdict-shaped body anywhere in the reply. This is the guarantee.
        assert "PASS" not in json.dumps(reply)
        assert "FAIL" not in json.dumps(reply)
        assert "result" not in reply
        # Pinned so a change is noticed; see the docstring for why it is 0.
        assert reply["error"]["code"] == 0, reply
        assert "verification-kit" in reply["error"]["message"]

        # And the fault is visible server-side, which is where a maintainer looks.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any("FileExistsError" in line for line in client.stderr_lines):
                break
            time.sleep(0.2)
        assert any("FileExistsError" in line for line in client.stderr_lines), client.stderr_lines[-10:]

        client.assert_alive()
        inspected = client.call_body("project_inspect", {})
        # Inspection stays useful when the store is broken: it still describes
        # the registered check, and it says plainly that nothing can be run.
        # Losing the checklist as well as the evidence would be a worse failure.
        assert [c["id"] for c in inspected["checks"]] == ["totals-behavior"]
        assert inspected["manifest"]["registered_check_ids"] == ["totals-behavior"]
        assert inspected["execution_available"] is False
        assert any("not writable" in gap for gap in inspected["gaps"]), inspected["gaps"]
        client.close()


def test_a_client_that_disconnects_mid_lifecycle_loses_no_evidence(tmp_path: Path) -> None:
    """Killing the server mid-call does not lose the run, and reconnecting finds it.

    The first server is killed outright, so it never runs its shutdown path and
    never flushes anything. A second, entirely new server process is then asked
    for the same run, and the recorded outcome is there. A run that lived only in
    the first process's memory would be gone, and the product's whole claim is
    that it is not.
    """
    project = make_repo(tmp_path, "disconnect")

    client = StdioClient(project).start()
    task_id = begin_task(client)
    run_id = client.call_body(
        "check_start",
        {"task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-die"},
        timeout=LIFECYCLE_TIMEOUT,
    )["runs"][0]["run_id"]
    # A hard kill: no stdin close, no chance to flush on the way out.
    client.kill()

    with StdioClient(project) as reconnected:
        fetched = reconnected.call_body("run_get", {"run_id": run_id})
        assert fetched["run_id"] == run_id
        assert fetched["result"] == "PASS"
        assert fetched["lifecycle"] == "terminal"
        assert fetched["process"]["exit_code"] == 0

        finalized = reconnected.call_body("task_finalize", {"task_id": task_id})
        assert finalized["readiness"] == "READY"
        assert finalized["gaps"] == []
        reconnected.close()


def test_a_clean_disconnect_shuts_the_server_down(connected) -> None:
    """Closing stdin ends the process with 0, and it logged to stderr.

    The two halves are one requirement. stdout is the protocol channel, so the
    startup banner has to be on stderr; and a host that closes the stream must
    get a process that ends rather than one that lingers holding the root.
    """
    code = connected.close()
    assert code == 0, f"clean disconnect exited {code}; stderr: {connected.stderr_lines[-6:]}"
    assert any("vkit mcp serving" in line for line in connected.stderr_lines), connected.stderr_lines
    # Nothing but protocol frames ever reached stdout.
    for line in connected.stdout_lines:
        json.loads(line)  # raises if a banner or a log leaked into the stream


def test_a_check_that_times_out_is_recorded_as_blocked_not_passed(tmp_path: Path) -> None:
    """A run that exceeds its manifest timeout is BLOCKED, with a reason.

    The timeout is the whole point: a check that hangs must not be able to sit in
    `running` forever looking like work in progress, and it certainly must not
    read as a pass. The server answers with the recorded reason and keeps going.
    """
    project = make_repo(tmp_path, "timeout")
    manifest_path = project / "verification" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checks"][0]["command"] = ["python", "-c", "import time; time.sleep(30)"]
    manifest["checks"][0]["timeout_seconds"] = 3
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    with StdioClient(project) as client:
        task_id = begin_task(client)
        started = client.call_body(
            "check_start",
            {"task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-timeout"},
            timeout=LIFECYCLE_TIMEOUT,
        )
        run = started["runs"][0]
        assert run["result"] == "BLOCKED"
        assert run["outcome"]["reason"] == "timeout"

        refetched = client.call_body("run_get", {"run_id": run["run_id"]})
        assert refetched["result"] == "BLOCKED"
        assert "BLOCKED" in refetched["summary"]

        finalized = client.call_body("task_finalize", {"task_id": task_id})
        assert finalized["readiness"] == "BLOCKED"
        assert any("totals-behavior" in gap for gap in finalized["gaps"]), finalized["gaps"]
        client.close()


def test_large_logs_stay_bounded_across_the_wire(tmp_path: Path) -> None:
    """A 200 KB log comes back one bounded page at a time, byte counts exact.

    The page size is asserted against the file on disk, so a handler that read
    the whole log and trimmed the string would fail on the counts rather than
    pass on the text.
    """
    project = make_repo(tmp_path, "logs")
    with StdioClient(project) as client:
        task_id = begin_task(client)
        run_id = client.call_body(
            "check_start",
            {"task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-logs"},
            timeout=LIFECYCLE_TIMEOUT,
        )["runs"][0]["run_id"]

        # The log is grown on disk after the run, because the point is that a
        # 200 KB log comes back one bounded page at a time. Asserted against the
        # file, so a handler that read it all and trimmed the string would fail
        # on the byte counts instead of passing on the text.
        from vkit.paths import open_project

        runs_root = open_project(project).runs_root / run_id
        assert runs_root.is_dir(), runs_root
        stdout_path = runs_root / "stdout.log"
        stdout_path.write_bytes(b"A" * 4096 + b"x" * 200_000)
        total = stdout_path.stat().st_size
        assert total == 204_096

        first = client.call_body("run_get", {"run_id": run_id, "log_limit": 1000})
        page = first["logs"]["stdout"]
        assert page["bytes"] == 1000
        assert page["total_bytes"] == total
        assert page["eof"] is False
        assert page["next_offset"] == 1000
        assert len(page["text"].encode("utf-8")) == 1000

        second = client.call_body(
            "run_get", {"run_id": run_id, "log_limit": 1000, "log_offset": page["next_offset"]}
        )
        assert second["logs"]["stdout"]["offset"] == 1000
        assert second["logs"]["stdout"]["text"][0] == "A"
        client.close()


# --- a missing SDK is a refusal, not a hang ----------------------------------

def test_a_missing_sdk_exits_with_its_own_code_and_says_so(repo: Path, tmp_path: Path) -> None:
    """Without the SDK the command says what is missing and exits 5.

    Not a hang and not a traceback into the protocol stream. A host that starts
    this command and gets nothing back cannot tell a missing optional dependency
    from a broken server, so the absence has to be reported on stderr with a
    code that says it.
    """
    env = dict(os.environ)
    # A shim module that shadows the `mcp` package with something unimportable,
    # which is what an environment without the optional extra looks like.
    shim = tmp_path / "no-sdk"
    shim.mkdir()
    (shim / "mcp.py").write_text("raise ImportError('this build has no MCP SDK')\n", encoding="utf-8")
    env["PYTHONPATH"] = os.pathsep.join([str(shim), str(REPO_ROOT / "src")])

    done = subprocess.run(
        [sys.executable, "-m", "vkit.cli", "mcp", "serve", "--project", str(repo)],
        capture_output=True, encoding="utf-8", timeout=300, env=env, cwd=str(REPO_ROOT),
    )
    assert done.returncode == 5, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == "", f"the refusal went to the protocol channel: {done.stdout!r}"
    assert "vkit[mcp]" in done.stderr

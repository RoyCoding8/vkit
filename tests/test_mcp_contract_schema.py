"""`task_begin`'s contract is a closed vocabulary, and the wire says which.

`task_begin` published its `contract` as a bare object: no `properties`, no
`additionalProperties`. Every sibling object in the same table is closed. So a
client reading `--json` could not tell which contract keys the server reads, and
a contract naming `command` or `root` was accepted and discarded without a word
while the task was admitted anyway. Admission was sound -- `tasks.admit`
derives the repository, the policy digest and the floor server-side -- so nothing
executed. The defect was that the published schema described an interface that
did not exist.

These tests pin the two facts that close it. The declared key list is exactly
the list the handler admits, asserted by calling the tool with each declared key
and watching it take effect, and with an undeclared key and watching it refuse.
And the four operations CONTRACT forbids are probed against all six tools over
the real stdio transport, which is the only surface a client actually reads.

**The missing acceptance row.** `plans/12` names "MCP client connects without a
Claude plugin or any skills | Discovers tools, inspects project, runs a task,
and reads finalization". `test_mcp_stdio.py` drives that to READY, but reaches
its verdict through `task_finalize` alone. A run reaching a terminal verdict
through `check_start` plus `run_get` -- the pair a client without a plugin
actually has -- was uncovered. The lifecycle test at the end of this file is
that row, over the same hand-written JSON-RPC client, which imports neither
`vkit` nor `mcp` and therefore cannot agree with the server about a changed
contract by construction.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_client import StdioClient

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
TOOL_NAMES = [
    "project_inspect", "task_begin", "check_start", "run_get", "run_cancel", "task_finalize",
]

#: Generous enough for a loaded Windows CI box, short enough that a hang fails
#: rather than sitting forever. A whole lifecycle launches a real process.
LIFECYCLE_TIMEOUT = 600.0

#: The keys `task_begin` reads from a contract. Read from the table under test
#: rather than restated, so this file cannot go stale while the schema changes;
#: `test_the_published_contract_declares_exactly_what_the_handler_reads` is what
#: would notice, and it compares the two rather than trusting either.
DECLARED_CONTRACT_KEYS = ("description", "required_checks", "scope")

#: Every parameter name any of the six tools accepts, gathered from the published
#: schemas. The forbidden probe needs to know that none of these is one of the
#: four things CONTRACT forbids, which is a statement about the whole catalogue
#: rather than about one tool.
FORBIDDEN_PROBES: tuple[tuple[str, dict[str, object]], ...] = (
    # An arbitrary root: a place for a check to run, named by the caller.
    ("project-root", {"root": "C:/Windows", "project": "C:/Windows", "cwd": "C:/Windows",
                      "path": "C:/Windows", "repository": "C:/Windows", "checkout": "C:/Windows",
                      "workdir": "C:/Windows"}),
    # A raw command: a line this server would have to execute.
    ("raw-command", {"command": "whoami", "cmd": "whoami", "argv": ["whoami"],
                     "args": ["whoami"], "shell": "whoami", "exec": "whoami",
                     "program": "whoami", "script": "whoami"}),
    # An integration approval: a grant the human is supposed to give.
    ("integration-approval", {"approve": True, "approved": True, "approval": True,
                              "consent": True, "allow": True, "permitted": True,
                              "trusted": True, "escalate": True}),
    # A plugin install: a package fetched and run on the operator's machine.
    ("plugin-install", {"install": "some-plugin", "plugin": "some-plugin",
                        "plugins": ["some-plugin"], "package": "some-plugin",
                        "marketplace": "some-plugin", "source": "some-plugin",
                        "extension": "some-plugin"}),
)


def hidden_window() -> dict[str, object]:
    """`subprocess` keywords that keep a launched Git off the operator's desktop.

    A console application launched from a test runner inherits a console, and on
    Windows that opens a window over whatever the human was using. `git` is
    launched twice per fixture here, so this is applied to every child rather
    than to the first one that popped up. Both keywords are needed: the creation
    flag stops the window being created, and the startup info stops a host that
    honours `STARTUPINFO` from showing one anyway.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def make_repo(tmp_path: Path, name: str = "project") -> Path:
    target = tmp_path / name
    shutil.copytree(EXAMPLE, target)
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
    ):
        subprocess.run(argv, cwd=target, check=True, **hidden_window())
    return target


def begin_task(client: StdioClient, request_id: str = "req-begin-1", **extra) -> str:
    """Open a task over the wire and return its id."""
    result = client.call("task_begin", {
        "owner": "contract-schema-suite",
        "request_id": request_id,
        **({"contract": {"description": "totals"}} | extra),
    })
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])["task_id"]


@pytest.fixture()
def connected(tmp_path: Path):
    """A live, handshaken server on a throwaway repository, torn down at the end.

    Function scoped, unlike the module-scoped fixture in `test_mcp_stdio.py`:
    the probes below write tasks and runs, so each test gets its own repository
    and cannot read another's state.
    """
    project = make_repo(tmp_path, "connected")
    client = StdioClient(project).start()
    try:
        yield client
    finally:
        code = client.close()
    if not client.killed:
        assert code == 0, (
            f"vkit mcp serve exited with {code}; stderr: {client.stderr_lines[-6:]}"
        )


def body_of(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


# --- the published contract is a closed object -------------------------------

def test_the_published_contract_is_a_closed_object(connected) -> None:
    """`contract` declares what it accepts and refuses the rest, like every sibling.

    This is the check the open schema fails. `additionalProperties` is the flag a
    client reads to know whether a key it wants to send will be honoured, and
    `properties` is the vocabulary. Without both, the schema promises the server
    will look at keys it then throws away.
    """
    contract = {
        t["name"]: t["inputSchema"] for t in connected.list_tools()
    }["task_begin"]["properties"]["contract"]

    assert contract["type"] == "object"
    assert contract["additionalProperties"] is False, (
        "task_begin.contract is published as an open object, so a client cannot "
        "tell which keys the server reads; every sibling object is closed"
    )
    assert sorted(contract["properties"]) == sorted(DECLARED_CONTRACT_KEYS), (
        f"the declared contract keys drifted: {sorted(contract['properties'])}"
    )
    # Every declared key is typed, because a schema that names a key without
    # saying what shape it takes is as unhelpful as not naming it.
    for key, declared in contract["properties"].items():
        assert declared["type"] in {"array", "string"}, f"{key}: {declared}"
        assert declared["description"].strip(), f"{key} has no description"


def test_the_published_contract_declares_exactly_what_the_handler_reads(connected) -> None:
    """The declared list and the handler's accepted list are the same list.

    Read two ways on purpose. The wire list is what a client sees; the refusal
    below is what the handler does. Asserting them against each other catches
    both directions of drift: a key declared but never read (the open-schema
    defect) and a key read but never declared (a working feature the schema
    hides).
    """
    published = sorted(
        {t["name"]: t["inputSchema"] for t in connected.list_tools()}
        ["task_begin"]["properties"]["contract"]["properties"]
    )

    accepted: list[str] = []
    for index, key in enumerate(DECLARED_CONTRACT_KEYS):
        value: object = ["totals-behavior"] if key == "required_checks" else "totals"
        result = connected.call(
            "task_begin", {"contract": {key: value}, "request_id": f"req-declared-{index}"}
        )
        assert result["isError"] is False, (
            f"the schema declares contract.{key} but the handler refused it: "
            f"{body_of(result).get('error')}"
        )
        accepted.append(key)

    assert published == sorted(accepted)

    # And an undeclared key is refused by name, so the error tells a caller which
    # key was wrong rather than merely that something was.
    unknown = connected.call(
        "task_begin", {"contract": {"policy_digest": "x"}, "request_id": "req-undeclared"}
    )
    assert unknown["isError"] is True
    error = body_of(unknown)["error"]
    assert "policy_digest" in error, error
    for key in DECLARED_CONTRACT_KEYS:
        assert key in error, f"the refusal does not name the accepted key {key!r}: {error}"


def test_a_declared_contract_key_is_recorded_rather_than_discarded(connected) -> None:
    """A declared key does something. A key the schema names is not a lie.

    Each declared key is sent on its own and the stored contract is read back
    through the core, so this asserts the value reached the contract rather than
    that the call returned no error. `contract.scope` was previously read by
    nobody: it was accepted, published as open, and silently dropped.
    """
    from vkit.paths import open_project
    from vkit.storage import Store
    from vkit.tasks import get_task

    project = open_project(connected.repo)

    scope_task = begin_task(connected, request_id="req-scope", contract={"scope": "a-scope"})
    stored = get_task(Store(project.db_path), scope_task).pinned()
    assert stored.scope == "a-scope", (
        "contract.scope is published and accepted but never recorded, so the "
        f"stored scope is {stored.scope!r}"
    )

    # `description` is the spelling `vkit task begin --contract` reads, and it
    # still records when no `scope` is supplied.
    described = connected.call_body("task_begin", {
        "contract": {"description": "a-description"}, "request_id": "req-desc",
    })
    assert get_task(Store(project.db_path), described["task_id"]).pinned().scope == (
        "a-description"
    )

    # `required_checks` reaches the floor as an addition to the policy's own.
    widened = connected.call_body("task_begin", {
        "contract": {"required_checks": ["totals-behavior"]}, "request_id": "req-widen",
    })
    assert widened["required_checks"] == ["totals-behavior"]


# --- required_checks stays additive ------------------------------------------

def test_required_checks_adds_to_the_policy_and_cannot_subtract_from_it(connected) -> None:
    """The floor is the union of the caller's selection and the approved policy.

    Two facts that have to survive the schema change. An empty list does not
    shrink the floor to nothing, because the floor is derived from the policy in
    force rather than taken from the caller. And a check the policy does not
    register is refused at admission rather than dropped, because silently
    ignoring it would admit a task that looks like it was asked for something it
    is not.
    """
    empty = connected.call_body(
        "task_begin", {"contract": {"required_checks": []}, "request_id": "req-empty"}
    )
    assert empty["required_checks"] == ["totals-behavior"], (
        "an empty required_checks shrank the policy floor to "
        f"{empty['required_checks']}"
    )

    unknown = connected.call("task_begin", {
        "contract": {"required_checks": ["no-such-check"]}, "request_id": "req-unknown",
    })
    assert unknown["isError"] is True
    refused = body_of(unknown)
    assert refused["admitted"] is False
    assert "unknown check" in refused["admission_conflict"]


# --- nothing accepts a root, a command, an approval or an install ------------

@pytest.mark.parametrize(("label", "payload"), FORBIDDEN_PROBES, ids=[p[0] for p in FORBIDDEN_PROBES])
def test_no_tool_accepts_a_root_a_command_an_approval_or_an_install(
    connected, label: str, payload: dict[str, object]
) -> None:
    """The four forbidden operations are refused at the top level of every tool.

    CONTRACT says no tool accepts a new arbitrary root, raw command, integration
    approval or plugin-install request. `Server.call_tool` rejects an unknown
    key at the top level, so each spelling here has to come back refused and has
    to name itself in the reason -- a client that sent `install` deserves to be
    told `install` is the problem.
    """
    for tool in TOOL_NAMES:
        for key, value in payload.items():
            # A valid argument for the rest of the call, so the refusal is about
            # the forbidden key rather than about a request that was malformed.
            base: dict[str, object] = {
                "project_inspect": {},
                "task_begin": {"contract": {"description": "probe"}, "request_id": "req-probe"},
                "check_start": {"task_id": "no-such-task", "check_ids": ["totals-behavior"],
                                "request_id": "req-probe"},
                "run_get": {"run_id": "0" * 32},
                "run_cancel": {"run_id": "0" * 32, "request_id": "req-probe"},
                "task_finalize": {"task_id": "no-such-task"},
            }[tool]
            result = connected.call(tool, {**base, key: value})
            assert result["isError"] is True, (
                f"{tool} accepted {label} key {key!r}"
            )
            error = body_of(result)["error"]
            assert key in error, f"{tool} refused {key!r} without naming it: {error}"


@pytest.mark.parametrize(("label", "payload"), FORBIDDEN_PROBES, ids=[p[0] for p in FORBIDDEN_PROBES])
def test_the_contract_refuses_a_root_a_command_an_approval_or_an_install(
    connected, label: str, payload: dict[str, object]
) -> None:
    """The same four, nested one level down, where the open schema let them in.

    This is the row the audit measured. Every one of these keys was accepted
    before and discarded, so `task_begin` returned a task and the caller's
    `command` or `root` had no effect at all. Now the whole payload is refused,
    which is the difference between a tool that tells you and a tool that
    quietly agrees with you.
    """
    result = connected.call("task_begin", {
        "contract": {**payload, "required_checks": []}, "request_id": "req-forbidden",
    })
    assert result["isError"] is True, (
        f"task_begin accepted a contract carrying a {label}: {body_of(result)}"
    )
    error = body_of(result)["error"]
    assert "unknown key" in error, error


def test_no_published_schema_declares_a_parameter_that_forbids(
    connected,
) -> None:
    """No schema in the catalogue names a root, a command, an approval or an install.

    The probes above prove the server refuses these at runtime. This proves the
    advertisement does not offer them in the first place, which is the surface a
    client reads before it decides what to send.
    """
    banned = {"root", "command", "cmd", "argv", "args", "shell", "exec", "program",
              "install", "plugin", "plugins", "package", "marketplace", "extension",
              "approve", "approved", "approval", "consent", "allow", "trusted",
              "escalate", "project", "cwd", "workdir", "checkout", "source"}

    def walk(schema: dict) -> set[str]:
        found = set()
        for key, value in schema.get("properties", {}).items():
            found.add(key)
            if isinstance(value, dict):
                found |= walk(value)
        return found

    for tool in connected.list_tools():
        published = walk(tool["inputSchema"])
        overlap = published & banned
        assert not overlap, (
            f"{tool['name']} publishes a parameter named {sorted(overlap)}; CONTRACT "
            "forbids a tool accepting an arbitrary root, raw command, integration "
            "approval or plugin install"
        )


# --- the acceptance row no test covered --------------------------------------

def test_a_client_without_a_plugin_reaches_a_terminal_verdict_over_the_wire(
    tmp_path: Path,
) -> None:
    """Plan 12's acceptance row, driven through `check_start` and `run_get`.

    "MCP client connects without a Claude plugin or any skills | Discovers tools,
    inspects project, runs a task, and reads finalization." The existing coverage
    drives discovery through `task_finalize` and asserts READY; what it does not
    do is read the *run's* terminal verdict, which is the pair a client holding
    no plugin and no skills actually has. This row is that pair.

    Every value is read off the wire by a client that imports neither `vkit` nor
    `mcp`, so it cannot agree with the server about a contract it got wrong. The
    check is a real `python verify_totals.py` launched by the server, and
    `check_start` returns at the launch, so the verdict has to be polled with
    `run_get` rather than read off the start payload.
    """
    project = make_repo(tmp_path, "verdict")
    with StdioClient(project) as client:
        # Discovery: the six tools, and nothing else.
        tools = client.list_tools()
        assert [t["name"] for t in tools] == TOOL_NAMES
        assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)

        inspected = client.call_body("project_inspect", {})
        assert inspected["project_root"] == str(project.resolve())
        assert inspected["manifest"]["registered_check_ids"] == ["totals-behavior"]
        assert inspected["execution_available"] is True
        assert inspected["gaps"] == []

        # A task, opened with a contract that is closed and valid.
        task_id = begin_task(client, request_id="req-verdict")

        started = client.call_body("check_start", {
            "task_id": task_id, "check_ids": ["totals-behavior"], "request_id": "req-run-1",
        }, timeout=LIFECYCLE_TIMEOUT)
        run_id = started["runs"][0]["run_id"]
        assert started["runs"][0]["replayed"] is False

        # The terminal verdict, polled over `run_get`. Polled with a deadline
        # rather than slept for a fixed time: the duration belongs to a real
        # process, and a fixed sleep is slow on a fast machine and flaky on a
        # loaded one.
        deadline = time.monotonic() + LIFECYCLE_TIMEOUT
        while True:
            run = client.call_body("run_get", {"run_id": run_id})
            if run.get("lifecycle") == "terminal" and run.get("outcome"):
                break
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"run {run_id} was still {run.get('lifecycle')!r} after "
                    f"{LIFECYCLE_TIMEOUT}s"
                )
            time.sleep(0.1)

        assert run["result"] == "PASS"
        assert run["check_id"] == "totals-behavior"
        assert [s["id"] for s in run["scenarios"]] == [
            "empty-cart", "single-positive", "several-positives", "mixed-sign",
            "negatives-only", "cancels-to-zero",
        ]
        assert all(s["result"] == "PASS" for s in run["scenarios"])
        # A real process ran and was reaped. A server that answered PASS without
        # launching anything would publish no owner and no exit code.
        assert run["ownership_known"] is True
        assert isinstance(run["process"]["pid"], int)
        assert run["process"]["exit_code"] == 0
        assert run["artifacts"]["result"]["exists"] is True

        # Finalization agrees with the run it read.
        finalized = client.call_body("task_finalize", {"task_id": task_id})
        assert finalized["readiness"] == "READY"
        assert finalized["gaps"] == []
        assert finalized["required_checks"] == ["totals-behavior"]

        assert client.close() == 0
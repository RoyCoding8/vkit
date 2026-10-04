"""Standalone MCP is the portable connection, and the panel says which half works.

Two facts about an MCP connection are routinely collapsed into one, and the
collapse is what makes a broken setup look installed.

**Configured** is what a file says. A host's configuration carries a command and
its arguments, and the console did not write that file, so the console can only
read it back and report what it says. **Connected** is what a real handshake
proves: a subprocess was launched, an `initialize` frame was answered, and
`tools/list` came back. `vkit mcp serve --json` settles neither. It returns
before touching the transport, so it prints a full six-tool catalogue with the
SDK absent while the same command without `--json` exits 5. A catalogue is a
statement about the table, never about a running server.

**So this file drives the real transport**, through the hand-written
`tests/mcp_client.py`, which spawns `vkit mcp serve` and speaks JSON-RPC as
bytes. It imports neither `vkit` nor `mcp`, so it cannot agree with the server
about a changed contract by construction. That property is load-bearing here and
it is why the probe below reuses this client rather than importing the server's
own SDK session.

**The executable path is resolved from the installation, not guessed.**
`shutil.which("vkit")` answers for whatever is first on PATH, which on this host
is a different virtualenv's copy. Measured on the development machine:

    which vkit  ->  C:\\CLI\\cx\\.venv\\Scripts\\vkit.EXE
    the console's own install  ->  D:\\AI\\Poteto's Style\\.venv\\Scripts\\vkit.exe

A snippet built from the first would hand the operator a server that answers
about another install, so the panel reads the console script out of
`importlib.metadata` the way `tests/conftest.py::console_script` already does.
`shutil.which` is kept, and reported, as the thing it is: what PATH would run.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

import subproc
from mcp_client import StdioClient

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

#: The id the example manifest registers. Written out rather than read from the
#: manifest, so a manifest that stopped registering it fails here.
CHECK_ID = "totals-behavior"

#: How long one probe may take. A handshake measures at ~0.9s on this host
#: (interpreter start plus SDK import dominates), so this is generous rather than
#: tight: a loaded CI box should pass, and a hung server should fail here rather
#: than sit until pytest's own timeout.
PROBE_TIMEOUT_SECONDS = 120.0

#: The six tools, in publication order. Written out rather than imported from
#: `vkit.mcp`, because this file's whole argument is that an independent reader
#: must not take the server's word for what it publishes.
EXPECTED_TOOL_NAMES = [
    "project_inspect", "task_begin", "check_start", "run_get", "run_cancel", "task_finalize",
]


def make_repo(tmp_path: Path, name: str = "project") -> Path:
    """A throwaway Git repository holding a real copy of the example."""
    target = tmp_path / name
    shutil.copytree(EXAMPLE, target)
    subproc.run(["git", "init", "-q"], cwd=target, check=True, capture_output=True)
    subproc.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True, capture_output=True,
    )
    return target




def snippet_entry(context: Any) -> dict[str, Any]:
    """The generated `mcpServers.vkit` entry, which is what an operator pastes.

    Read through the published shape rather than a flattened helper, because the
    nesting is the contract: a host reads `mcpServers.<name>.command`, and a
    panel that flattened it for convenience would publish a shape no host reads.
    """
    return operations_mcp_connection(context)["snippet"]["mcpServers"]["vkit"]


def operations_mcp_connection(context: Any, **kwargs: Any) -> dict[str, Any]:
    from vkit.console import operations

    return operations.mcp_connection(context, **kwargs)


def test_the_generated_command_is_the_console_scripts_own_installation(tmp_path: Path) -> None:
    """The snippet names the install serving this panel, not whatever is on PATH.

    Measured on the development host: `shutil.which("vkit")` resolves to
    `C:\\CLI\\cx\\.venv\\Scripts\\vkit.EXE`, a different install from the
    `D:\\AI\\Poteto's Style\\.venv` one the console is running. A snippet built
    from the PATH answer would start a server bound to that other install and
    report success, which is the false receipt this checkpoint exists to remove.

    The assertion is on the file itself: the reported path must be the one the
    distribution installed, and it must not be the PATH answer.
    """
    from vkit.console import operations
    from vkit.paths import open_project

    context = operations.open_context(make_repo(tmp_path))
    command = snippet_entry(context)["command"]

    resolved = Path(command)
    assert resolved.is_absolute(), f"{resolved} is not an absolute path"
    assert resolved == _installed_console_script(), (
        f"the panel names {resolved} while the install that runs this console "
        f"placed its console script at {_installed_console_script()}"
    )
    on_path = shutil.which("vkit")
    if on_path is not None and Path(on_path).resolve() != resolved.resolve():
        assert on_path != command, (
            f"the panel fell back to the PATH answer {on_path!r} rather than "
            f"reading the installation. They differ on this host by "
            f"construction, so this is the defect rather than a coincidence."
        )


def _installed_console_script(name: str = "vkit") -> Path:
    """The console script this interpreter's install placed on disk.

    Read through `importlib.metadata` rather than predicted from
    `sys.executable`, because the layouts disagree: a POSIX `setup-python` puts
    the interpreter under a framework prefix and the script in `~/.local/bin`,
    so nothing need be beside it. `tests/conftest.py::console_script` documents
    this at length; the same arithmetic is used here so the panel and the suite
    cannot reach two answers for one install.
    """
    from importlib.metadata import entry_points

    for found in entry_points(group="console_scripts"):
        if found.name != name:
            continue
        distribution = found.dist
        for entry in distribution.files or ():
            if PureWindowsPath(str(entry)).stem.lower() != name.lower():
                continue
            resolved = Path(distribution.locate_file(entry))
            if resolved.is_file():
                return resolved.resolve()
    raise AssertionError(
        f"no installed console script named {name!r} was found for "
        f"{os.sys.executable}, so the panel has no real path to publish"
    )


def test_the_published_command_is_a_file_that_exists_and_can_be_executed(
    tmp_path: Path,
) -> None:
    """The path is real, and running it is what proves it is executable.

    `os.access(X_OK)` is the portable spelling, and on Windows it answers True for
    any existing file, so it is paired with the one thing that cannot be faked:
    the file is launched and reports its own version. A path that exists, is
    marked executable and then refuses to run is not a command an operator can
    paste into a host.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    command = Path(snippet_entry(context)["command"])

    assert command.is_file(), f"{command} is not a file on this machine"
    assert os.access(command, os.X_OK), f"{command} is not executable"

    launched = subproc.run(
        [str(command), "doctor", "--json"],
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=PROBE_TIMEOUT_SECONDS, check=False,
    )
    assert launched.returncode in (0, 1, 2, 3, 4, 5), (
        f"{command} could not be launched: it exited {launched.returncode} with "
        f"{launched.stderr[-400:]!r}"
    )


def test_the_project_argument_is_this_projects_real_absolute_root(tmp_path: Path) -> None:
    """`--project` carries the resolved root, resolved here and not assembled.

    `open_project` resolves the root through git, so the value in the snippet is
    the repository the console is bound to and not the directory the operator
    happened to launch from.
    """
    from vkit.console import operations
    from vkit.paths import open_project

    repo = make_repo(tmp_path)
    context = operations.open_context(repo)
    entry = snippet_entry(context)

    assert entry["args"][:3] == ["mcp", "serve", "--project"]
    assert entry["args"][3] == str(open_project(repo).root)
    assert Path(entry["args"][3]).is_absolute()


def test_the_snippet_is_host_neutral_and_never_claims_to_be_verified(tmp_path: Path) -> None:
    """The shape is `mcpServers`, and nothing in it is labelled a verified link.

    `plugin/.mcp.json` declares the same two keys but is one host's
    configuration, with its own `${user_config.project_root}` substitution and a
    bare `vkit` that only resolves under that host. The portable snippet is the
    other two things: real absolute paths, and a shape any host reading
    `mcpServers` understands.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    snippet = operations_mcp_connection(context)["snippet"]

    assert set(snippet) == {"mcpServers", "resolved", "note"}
    entry = snippet["mcpServers"]["vkit"]
    assert set(entry) == {"command", "args"}
    assert "${" not in json.dumps(snippet), (
        "the snippet carries a host substitution placeholder, so it is that "
        "host's configuration rather than a portable one"
    )
    assert "verified" not in json.dumps(snippet["mcpServers"]).lower()
    assert "connected" not in json.dumps(snippet["mcpServers"]).lower()
    assert snippet["resolved"] is True, (
        "the console resolved the installed console script, so the snippet's "
        "command is a real absolute path rather than a bare name"
    )
    assert snippet["note"], "the snippet carries no note saying what it is not"


def test_the_snippet_is_valid_json_a_host_can_parse(tmp_path: Path) -> None:
    """A snippet that only exists as a Python dict has not been shown to work.

    It is rendered for the page and written to a file by an operator, so it has
    to survive the round trip through JSON, with a non-ASCII project path
    intact rather than escaped into something a filesystem cannot use.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path / "späce"))
    text = operations.mcp_connection_text(context)

    assert text == json.dumps(
        json.loads(text), indent=2, ensure_ascii=False
    ), "the rendered snippet does not survive a JSON round trip"
    assert "mcpServers" in text
    assert "\\u" not in text, (
        "the snippet escapes non-ASCII characters, so a project path with one "
        "would be pasted as a path that does not exist"
    )




def test_configured_and_connected_are_separate_fields(tmp_path: Path) -> None:
    """The two facts have two names, and neither is derived from the other.

    `configured` reads a host's configuration file. `connected` is the result of
    a real handshake. Collapsing them into one status is the defect: a host
    configured with a path that cannot start is the case that reads green
    everywhere except here.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    document = operations.mcp_connection(context)

    assert "configured" in document and "connected" in document
    assert isinstance(document["configured"], dict)
    assert isinstance(document["connected"], dict)
    assert document["connected"]["state"] == "not_probed"
    assert document["connected"].get("handshake") in (None, False)


def test_a_configured_host_whose_command_cannot_start_reads_as_not_connected(
    tmp_path: Path,
) -> None:
    """The false-receipt case, made real.

    A host configuration is written with a command that does not exist, which is
    exactly what an operator gets from a stale snippet after an uninstall or a
    machine change. The file says the server is configured. The probe launches
    nothing, completes no handshake, and the panel reports NOT connected rather
    than accepting the file's word.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    host = operations.host_config_document(
        context,
        command=str(tmp_path / "no-such-vkit"),
        project_root=str(context.project.root),
    )
    document = operations_mcp_connection(context, host=host)

    assert document["configured"]["present"] is True, (
        "the host's own file says it is configured, and the panel reports what "
        "the file says rather than what it wishes were true"
    )
    assert document["configured"]["command"] == str(tmp_path / "no-such-vkit")
    assert document["configured"]["command_exists"] is False
    assert document["connected"]["state"] == "not_connected"
    assert document["connected"]["reason"], "a failed probe names no reason"
    assert document["connected"]["tool_names"] == []


def test_the_probe_detects_a_server_that_cannot_start(tmp_path: Path) -> None:
    """A launch that fails is a failed connection, not an unknown one.

    The probe is asked to reach a real server, so it must also be able to report
    that it reached nothing. This is the negative control for the positive one:
    without it, a probe that always answered `not_connected` would satisfy every
    other test in this file.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    working = operations.host_config_document(
        context,
        command=str(_installed_console_script()),
        project_root=str(context.project.root),
    )
    good = operations.mcp_connection(context, host=working)
    assert good["connected"]["state"] == "connected", good["connected"]
    assert good["connected"]["tool_names"] == EXPECTED_TOOL_NAMES

    broken = operations.host_config_document(
        context,
        command=str(_installed_console_script()),
        project_root=str(tmp_path / "not-a-repository"),
    )
    bad = operations.mcp_connection(context, host=broken)
    assert bad["configured"]["present"] is True
    assert bad["connected"]["state"] != "connected", (
        "a server bound to a directory that is not a repository was reported as "
        "connected"
    )


def test_a_probe_that_never_ran_is_reported_as_unknown_not_as_failed(tmp_path: Path) -> None:
    """`not_probed` and `not_connected` are different answers.

    The first says nothing was measured. The second says a measurement happened
    and failed. An operator reading a panel cannot tell the difference unless the
    panel says it, and a panel that reported every unprobed host as "not
    connected" would train the reader to ignore the field.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    document = operations_mcp_connection(context)

    assert document["connected"]["state"] == "not_probed"
    assert document["connected"]["reason"] is None, (
        "an unprobed connection has no reason to give; a populated field here "
        "reads as a measurement that happened"
    )
    assert document["connected"]["handshake"] is False


def test_the_probe_answers_from_a_real_handshake_and_names_the_tools(tmp_path: Path) -> None:
    """A connection is a handshake, and the tool list comes off the wire.

    The tools are read from `tools/list` on a real subprocess rather than from
    `vkit.mcp.tool_definitions()`. Importing the table would make this the same
    tautology the CLI catalogue already is: it would report connected whatever
    the transport did, because the answer would be the server's own constant.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    host = operations.host_config_document(
        context,
        command=str(_installed_console_script()),
        project_root=str(context.project.root),
    )
    document = operations.mcp_connection(context, host=host)

    connected = document["connected"]
    assert connected["state"] == "connected"
    assert connected["handshake"] is True
    assert connected["tool_names"] == EXPECTED_TOOL_NAMES
    assert connected["server_info"]["name"] == "project"
    assert connected["server_info"]["version"]
    assert connected["protocol_version"]
    assert connected["project_root"] == str(context.project.root)


def test_the_probe_reports_a_command_that_cannot_be_launched_at_all(tmp_path: Path) -> None:
    """`FileNotFoundError` is an answer, not a traceback.

    A host whose configured command has been uninstalled raises at `Popen`. The
    probe turns that into the same `not_connected` a server that started and
    failed produces, because an operator needs one reading for "it does not
    work", and a stack trace from a read-only panel is not one.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    host = operations.host_config_document(
        context,
        command=str(tmp_path / "definitely-not-here"),
        project_root=str(context.project.root),
    )
    document = operations.mcp_connection(context, host=host)

    assert document["connected"]["state"] == "not_connected"
    assert document["connected"]["handshake"] is False
    assert document["connected"]["reason"]


def test_a_host_without_lifecycle_hooks_is_shown_as_missing_them(tmp_path: Path) -> None:
    """A hookless host does not inherit Claude's automatic completion gate.

    This is the acceptance row "Host lacks lifecycle hooks | UI shows the missing
    automation and explicit verification still works". The two halves matter
    equally and they point opposite ways. The automatic Stop gate and automatic
    edit cleanup belong to the HOST's lifecycle, not to the MCP protocol, so a
    standalone host that speaks MCP perfectly still does not get them. Reporting
    those rows as supported would send an operator waiting for a gate that can
    never fire.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    host = operations.host_config_document(
        context,
        command=str(_installed_console_script()),
        project_root=str(context.project.root),
    )
    gaps = operations.mcp_connection(context, host=host)["capability_gaps"]

    rows = {row["capability"]: row for row in gaps["rows"]}
    assert gaps["lifecycle_hooks"] is False
    assert rows["automatic completion gate on Stop"]["supported"] is False, (
        "a host without lifecycle hooks does not get the automatic gate; the "
        "protocol has no such capability and only the host can supply it"
    )
    assert rows["automatic edit cleanup"]["supported"] is False
    assert rows["session and agent identity"]["supported"] is True
    assert rows["discover and call the six tools"]["supported"] is True
    assert rows["explicit verification and finalization"]["supported"] is True
    assert set(gaps["missing"]) == {
        "automatic completion gate on Stop",
        "automatic edit cleanup",
    }


def test_a_claude_host_is_shown_as_having_its_hooks(tmp_path: Path) -> None:
    """The positive half, so the negative one is not the only thing checked.

    A capability table that reports everything as missing is indistinguishable
    from one that never measured, which is the failure the whole CONFIGURED /
    CONNECTED split is about. Claude Code supplies all five lifecycle hooks, so
    every row is supported and `missing` is empty.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    host = operations.host_config_document(
        context,
        command="vkit",
        project_root=str(context.project.root),
        host="claude-code",
    )
    gaps = operations.mcp_connection(context, host=host)["capability_gaps"]

    assert gaps["host"] == "claude-code"
    assert gaps["lifecycle_hooks"] is True
    assert gaps["missing"] == []


def test_an_unrecognised_host_gets_an_unknown_row_not_an_optimistic_default(
    tmp_path: Path,
) -> None:
    """A host this build has no adapter for is reported as unknown.

    Checkpoint 12.4 says not to build another harness adapter until a target
    host is selected. The honest answer for a host nobody has selected is
    `unknown` with every hook-dependent capability withheld, rather than
    assuming the best case and telling an operator they have a completion gate
    that will not exist.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    host = operations.host_config_document(
        context, command="vkit", project_root=str(context.project.root), host="some-editor"
    )
    gaps = operations.mcp_connection(context, host=host)["capability_gaps"]

    assert gaps["shape"] == "unknown"
    assert gaps["lifecycle_hooks"] is False
    assert gaps["missing"], (
        "an unrecognised host reports every capability as supported, which is "
        "the optimistic default this checkpoint forbids"
    )


def test_explicit_verification_still_works_through_a_host_without_hooks(
    tmp_path: Path,
) -> None:
    """The other half of the acceptance row: missing automation is not failure.

    A standalone host that cannot be blocked at Stop can still drive a real
    verdict, and the assertion is on a real subprocess completing a real
    lifecycle rather than on the capability table's opinion of it. This is the
    row that would fail if a host without hooks inherited nothing at all.
    """
    context_dir = make_repo(tmp_path)
    with StdioClient(context_dir) as client:
        assert [tool["name"] for tool in client.list_tools()] == EXPECTED_TOOL_NAMES

        begun = client.call_body("task_begin", {
            "request_id": "portable-1", "contract": {"description": "explicit verification"},
        })
        task_id = begun["task_id"]

        started = client.call_body("check_start", {
            "task_id": task_id, "check_ids": [CHECK_ID], "request_id": "portable-2",
        })
        run_id = started["runs"][0]["run_id"]

        deadline = time.monotonic() + 300.0
        while True:
            report = client.call_body("run_get", {"run_id": run_id})
            if report["lifecycle"] == "terminal" and report["outcome"]:
                break
            assert time.monotonic() < deadline, (
                f"the run was still {report['lifecycle']!r} after 300s"
            )
            time.sleep(0.2)

        verdict = client.call_body("task_finalize", {"task_id": task_id})

    assert verdict["readiness"] == "READY", verdict["gaps"]
    assert verdict["required_checks"] == [CHECK_ID]


def test_the_probe_costs_a_subprocess_only_when_something_is_configured(
    tmp_path: Path,
) -> None:
    """Nothing configured means nothing launched, and the panel says so.

    `mcp_connection` probes on its own because `api.py` owns the route table and
    there is nowhere for a "probe" request to arrive. That is only affordable
    because the probe is spent only where it means something: with no host
    configuration there is no command to launch, and the honest answer is
    `not_probed` rather than a manufactured failure.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    document = operations.mcp_connection(context, host=operations.unknown_host(context))

    assert document["configured"]["present"] is False
    assert document["connected"]["state"] == "not_probed"
    assert document["connected"]["reason"] is None, (
        "an unprobed host carries a failure reason, which reads as a measurement "
        "that happened"
    )


def test_an_unknown_host_is_reported_as_unknown_rather_than_assumed(tmp_path: Path) -> None:
    """No host configuration was found, and that is said rather than implied.

    The panel has no `mcpServers` document for this machine, so it publishes the
    snippet to paste and says plainly that nothing has been configured. Inventing
    a default host and reporting its state would be the panel answering a
    question nobody asked.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    document = operations.mcp_connection(
        context, host=operations.unknown_host(context)
    )

    assert document["configured"]["present"] is False
    assert document["configured"]["host"] == "unknown"
    assert document["connected"]["state"] == "not_probed"




def test_the_json_catalogue_says_nothing_about_whether_a_server_can_serve(
    tmp_path: Path,
) -> None:
    """`--json` returns before the transport is touched, and the panel says so.

    Measured by the audit that scoped this checkpoint: with the SDK absent,
    `vkit mcp serve --project ROOT --json` prints a complete six-tool catalogue
    and exits 0, while the same command without `--json` exits 5. A successful
    catalogue is not evidence a server can serve, and a panel that treated it as
    one would report a host green on an environment where every connection
    attempt fails.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    document = operations.mcp_connection(context)

    catalogue = operations.mcp_catalogue(context)
    assert [entry["name"] for entry in catalogue["tools"]] == EXPECTED_TOOL_NAMES
    assert catalogue["project"] == str(context.project.root)
    assert catalogue["touches_transport"] is False
    assert catalogue["note"], "the catalogue carries no note about what it does not prove"


def test_the_catalogue_and_the_connected_probe_agree_only_about_the_tools(
    tmp_path: Path,
) -> None:
    """Two readers of the same six names, one of which cannot see the server.

    This is the check the audit's finding demands: the catalogue names the tools
    without a handshake, and the handshake names them from the wire. They must
    agree on the names -- otherwise one of the two surfaces lies -- and they must
    still be reported as different kinds of evidence.
    """
    from vkit.console import operations

    context = operations.open_context(make_repo(tmp_path))
    host = operations.host_config_document(
        context,
        command=str(_installed_console_script()),
        project_root=str(context.project.root),
    )
    document = operations.mcp_connection(context, host=host)

    from_catalogue = [entry["name"] for entry in document["catalogue"]["tools"]]
    from_wire = document["connected"]["tool_names"]
    assert from_catalogue == from_wire == EXPECTED_TOOL_NAMES




def test_the_connection_panel_does_not_add_a_seventh_tool(tmp_path: Path) -> None:
    """Six are sufficient, and the panel is a console read rather than a tool.

    The panel answers "is this host set up", which is a fact about a machine
    rather than about the project a server is bound to. Publishing it as a tool
    would put a host-management operation on a surface whose whole invariant is
    that it names nothing outside the one bound root.
    """
    from vkit.mcp import TOOL_NAMES

    assert list(TOOL_NAMES) == EXPECTED_TOOL_NAMES
    assert len(TOOL_NAMES) == 6
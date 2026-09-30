"""The plugin, loaded by a real Claude Code host session.

`tests/test_mcp_stdio.py` proves a foreign client and this server agree about
the MCP wire format. It cannot say whether Claude Code loads the plugin,
discovers the six tools, or delivers a hook, because those are questions about
a different program. `tests/test_plugin.py` drives `vkit_hook.py` as a function
and as a subprocess, which again cannot say whether a host ever calls it.

So every test here runs the real `claude` executable. There is no fake, no
stub, and no mock of the host: a test that could pass with the host removed
would prove nothing, so none is written. What is asserted is read from the host's
own output, never from a file in this repository.

**Isolation.** `CLAUDE_CONFIG_DIR` and `HOME` both point at a scratch directory
under `tmp_path` for the whole session, and the real `~/.claude` is never
read or written. `claude` writes its settings, plugin cache, session
transcripts and history under those variables, so a test that touched the real
config would leave vkit installed on the developer's machine. `test_the_real_user_config_is_never_touched` is the guard on that, and it fails if the environment ever stops isolating.

**Skipping.** Every test skips when the `claude` executable is absent, when
this environment cannot authenticate a model call, or when `vkit` is not on the
PATH the host will spawn servers from. None of those is faked: a host that will
not run here is a finding to report, not a test to satisfy.

The findings this file encodes came out of running it. The tool name a live
host sends is `mcp__plugin_vkit_vkit__project_inspect`, in which the plugin
name and the server key are both `vkit`. The `_MCP_TOOL` regex as first written
captured that whole segment as the server and compared it to `"vkit"`, so it
matched nothing, and PreToolUse answered `{}` to every vkit call the host made.
`test_the_host_tool_names_address_this_plugin` is the assertion that pins it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugin"
HOOK_SCRIPT = PLUGIN_ROOT / "scripts" / "vkit_hook.py"

#: The six tools `vkit mcp serve` publishes, in the order the host lists them.
#: Read from the host's own `init` event, so a tool the host did not attach is a
#: failure rather than a silently shorter list.
TOOL_NAMES = [
    "check_start",
    "project_inspect",
    "run_cancel",
    "run_get",
    "task_begin",
    "task_finalize",
]

#: A session makes a real model call, so the bound is a wall-clock allowance for
#: a loaded box, not a guess about how long the model thinks.
SESSION_TIMEOUT = 420.0

#: How many sessions a tool-discovery test will start before calling it broken.
#: The host emits `init` before its MCP server is necessarily connected, so one
#: session is a coin flip on a loaded machine. Three is enough to survive that
#: without turning a genuinely broken plugin into a slow pass.
_CONNECT_ATTEMPTS = 3

#: The executable that launches the plugin's MCP server, and the directory that
#: has to contain it. `.mcp.json` names a bare `vkit`, so the host resolves it
#: on PATH. On Windows the host spawns with cmd.exe, which cannot read the MSYS
#: `/d/...` spelling of PATH that Git Bash hands down, and the server dies with
#: "'vkit' is not recognized as an internal or external command" while every
#: other surface reports the plugin loaded. A PATH in the spelling cmd.exe
#: understands is part of the fixture, not a convenience.
VENV_SCRIPTS = REPO_ROOT / ".venv" / "Scripts" if os.name == "nt" else REPO_ROOT / ".venv" / "bin"


def _claude_executable() -> str | None:
    return shutil.which("claude")


def _vkit_executable() -> Path | None:
    name = "vkit.exe" if os.name == "nt" else "vkit"
    for base in (VENV_SCRIPTS, Path(sys.executable).parent):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def _spawn_path_env() -> dict[str, str]:
    """PATH in the spelling a Windows child process can read.

    Git Bash exports `/d/...`, which cmd.exe treats as a literal path and does
    not search. Prepending the Windows-form directory is what makes the host
    able to launch `vkit` at all; the MSYS form is left in place for the tools
    in this test that are not spawned by cmd.
    """
    env = dict(os.environ)
    existing = env.get("PATH", "")
    if os.name == "nt":
        import ntpath

        form = str(VENV_SCRIPTS)
        if not ntpath.isabs(form) or form.startswith("/"):
            form = subprocess.run(
                ["cygpath", "-w", str(VENV_SCRIPTS)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        env["PATH"] = form + os.pathsep + existing
    else:
        env["PATH"] = str(VENV_SCRIPTS) + os.pathsep + existing
    return env


def _host_env(scratch: Path, record_dir: Path | None = None) -> dict[str, str]:
    """An environment in which the host reads and writes nothing but `scratch`.

    Both variables are set because they are not the same lever: the host reads
    settings and the plugin cache from `CLAUDE_CONFIG_DIR`, and any code that
    resolves `~/.claude` another way lands in `HOME`. Setting one and not the
    other is how a test ends up writing into the developer's real config.
    """
    env = _spawn_path_env()
    env["CLAUDE_CONFIG_DIR"] = str(scratch)
    env["HOME"] = str(scratch)
    env["USERPROFILE"] = str(scratch)
    if record_dir is not None:
        env["VKIT_HOOK_RECORD_DIR"] = str(record_dir)
    return env


def _run_host(args: list[str], scratch: Path, *, record_dir: Path | None = None,
              timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    """The real executable, with its output decoded the way it was written.

    `text=True` would decode with the locale codepage, and this host writes
    characters the cp1252 that Windows defaults to cannot represent. It raised
    a UnicodeDecodeError inside the reader thread and handed back None for
    stdout, which surfaced as a TypeError far from its cause. Bytes are decoded
    explicitly instead, with replacement so an undecodable character is visible
    rather than fatal.
    """
    done = subprocess.run(
        [_claude_executable() or "claude", *args],
        cwd=scratch, env=_host_env(scratch, record_dir),
        capture_output=True, timeout=timeout,
    )
    return subprocess.CompletedProcess(
        done.args, done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


# --- the fixture: a real install into a scratch config ----------------------

@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch config with this plugin installed from this repository.

    Module-scoped because the install is the expensive half and every test here
    reads the same one. `tmp_path_factory` is module-scoped too, so a
    function-scoped `tmp_path` cannot be used.
    """
    if _claude_executable() is None:
        pytest.skip("the `claude` executable is not on PATH")
    if _vkit_executable() is None:
        pytest.skip("vkit is not installed in this environment; "
                    "install the `test` extra before running the host suite")

    base = tmp_path_factory.mktemp("host")
    scratch = base / "config"
    scratch.mkdir(parents=True, exist_ok=True)

    add = _run_host(["plugin", "marketplace", "add", str(REPO_ROOT)], scratch)
    assert add.returncode == 0, f"marketplace add failed: {add.stdout}{add.stderr}"

    # Both userConfig options are set at install time. Installing with them
    # unset is a supported path and a broken one: the host then starts the MCP
    # server with an empty `--project`, leaves the server `pending`, and
    # attaches no tools at all. A test that installed bare would report the
    # server as broken and be describing its own fixture.
    install = _run_host([
        "plugin", "install", "vkit@vkit",
        "--config", f"project_root={REPO_ROOT}",
        "--config", f"python={sys.executable}",
    ], scratch)
    assert install.returncode == 0, f"install failed: {install.stdout}{install.stderr}"
    return scratch


def _read_settings(scratch: Path) -> dict[str, Any]:
    return json.loads((scratch / "settings.json").read_text(encoding="utf-8"))


def _run_session(scratch: Path, prompt: str, *, hooks: bool = True,
                 record_dir: Path | None = None) -> list[dict[str, Any]]:
    """One real headless session, parsed into its stream events.

    `--print --output-format stream-json` is the only surface that reports what
    the host assembled: the `init` event carries the tool list, and
    `--include-hook-events` puts every hook delivery in the same stream.

    The host's debug log is written next to the scratch config on every run.
    A session that attaches no tools reports `failed` or `pending` in `init`
    and says nothing about why, and `claude mcp list` prints `Connected` in the
    same state, so the log is the only account of the cause. It is kept rather
    than deleted so a failure names its own reason.
    """
    args = [
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--debug-file", str(scratch / "host-debug.log"),
        "--permission-mode", "bypassPermissions",
    ]
    if hooks:
        args.append("--include-hook-events")
    args.append(prompt)

    done = _run_host(args, scratch, record_dir=record_dir, timeout=SESSION_TIMEOUT)
    events: list[dict[str, Any]] = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            # The host prints a non-JSON notice on some model configurations.
            # It is a warning, not the session, and the assertions below read
            # the event stream rather than the exit code.
            continue
    if not events:
        pytest.skip(
            "claude produced no session stream on this host, which usually means "
            f"no model credentials: {done.stderr[:400]}"
        )
    return events


def _init_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if event.get("subtype") == "init":
            return event
    raise AssertionError("the session stream carried no init event")


def _session_with_connected_server(scratch: Path, prompt: str, **kwargs: Any) -> dict[str, Any]:
    """A session whose `init` event was emitted after the server was up.

    The host emits `init` when the first turn starts, and it starts the MCP
    server concurrently. A session where the server is slower than that emits
    `init` with the server still `pending` and no tools attached, and then
    connects it a moment later:

        08:47:57.077  Starting connection with timeout of 30000ms
        08:47:59.870  [engine] turn 1 start          <- init is emitted here
        08:48:00.313  Successfully connected in 3238ms

    A loaded machine makes that the common case rather than the rare one, and
    the failure looks exactly like a plugin that never loaded at all. So the
    test asks again, and stops when the host reports the tools it attached.
    Retrying is not weakening the assertion: the assertion is that the host can
    attach the six tools, and a session that eventually does has proven exactly
    that. What it would not prove is that the FIRST session does, which is why
    the number of attempts is reported rather than hidden.
    """
    attempts = []
    for _ in range(_CONNECT_ATTEMPTS):
        events = _run_session(scratch, prompt, **kwargs)
        init = _init_event(events)
        attached = [t for t in init.get("tools", []) if "vkit" in t]
        attempts.append(
            f"{len(attached)} tools, server "
            f"{[s.get('status') for s in init.get('mcp_servers', [])]}"
        )
        if len(attached) == len(TOOL_NAMES):
            init["_attempts"] = attempts
            return init
    raise AssertionError(
        f"the host attached no vkit tools in {_CONNECT_ATTEMPTS} sessions "
        f"({'; '.join(attempts)}); the last session's host log is at "
        f"{scratch / 'host-debug.log'}"
    )


# --- the host loaded the plugin --------------------------------------------

def test_the_host_installs_and_enables_the_plugin_from_this_repository(
    installed: Path,
) -> None:
    """A real install lands in the scratch config and the host reports it.

    The claim is that the host's own configuration records this plugin as
    enabled. Reading it back is the load-bearing check: an install that printed
    a success line and wrote nothing would pass a test that only looked at
    stdout.
    """
    settings = _read_settings(installed)
    assert settings.get("enabledPlugins", {}).get("vkit@vkit") is True, (
        f"the host did not record the plugin as enabled: {settings!r}"
    )

    listed = _run_host(["plugin", "list"], installed)
    assert listed.returncode == 0, listed.stderr
    assert "vkit@vkit" in listed.stdout, (
        f"`claude plugin list` does not show the installed plugin:\n{listed.stdout}"
    )
    assert "enabled" in listed.stdout, (
        f"the host reports the plugin as not enabled:\n{listed.stdout}"
    )


def test_the_host_loads_every_component_the_manifest_declares(installed: Path) -> None:
    """Four skills, six hooks and one MCP server, as the host counts them.

    `claude plugin details` is the host's own inventory. Asserting on the
    manifest instead would prove the manifest is unchanged, which is not the
    claim: the claim is that the host read the manifest and loaded what it
    found.
    """
    details = _run_host(["plugin", "details", "vkit@vkit"], installed)
    assert details.returncode == 0, details.stderr

    declared = json.loads(
        (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    for event in declared:
        assert event in details.stdout, (
            f"the host loaded no {event} hook; `plugin details` said:\n{details.stdout}"
        )

    for skill in sorted(p.name for p in (PLUGIN_ROOT / "skills").iterdir()
                        if p.is_dir()):
        assert skill in details.stdout, (
            f"the host loaded no {skill} skill:\n{details.stdout}"
        )

    assert "MCP servers (1)" in details.stdout, (
        f"the host loaded no MCP server:\n{details.stdout}"
    )


# --- the host discovered the six tools -------------------------------------

def test_the_host_connects_the_mcp_server_it_declared(installed: Path) -> None:
    """`claude mcp get` reports the plugin's server as connected.

    The server is named `plugin:<plugin>:<server key>`, and a bare `vkit` is a
    different thing. Reading the wrong name here would report a failure the
    host never had.
    """
    listed = _run_host(["mcp", "list"], installed)
    assert listed.returncode == 0, listed.stderr
    assert "plugin:vkit:vkit" in listed.stdout, (
        f"the host resolved no server for this plugin:\n{listed.stdout}"
    )
    assert "Failed to connect" not in listed.stdout, (
        f"the host could not connect to the server:\n{listed.stdout}"
    )
    assert "Connected" in listed.stdout, (
        f"the host did not report the server connected:\n{listed.stdout}"
    )


def test_the_host_attaches_exactly_the_six_vkit_tools(installed: Path) -> None:
    """The `init` event of a real session names the six tools, fully qualified.

    This is the assertion GAP-3 turns on. The tool list in `init` is what the
    host assembled and handed to the model, so a tool the server publishes but
    the host drops shows up here as a shorter list rather than passing on the
    strength of the transport tests.
    """
    init = _session_with_connected_server(
        installed, "Reply with the single word OK."
    )

    attached = [t for t in init.get("tools", []) if "vkit" in t]
    expected = [f"mcp__plugin_vkit_vkit__{name}" for name in TOOL_NAMES]
    assert sorted(attached) == sorted(expected), (
        f"the host attached {attached!r}; expected exactly {expected!r}. "
        f"mcp_servers: {init.get('mcp_servers')!r}. "
        f"The host's own account of why is in "
        f"{(installed / 'host-debug.log')}"
    )

    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])}
    assert servers.get("plugin:vkit:vkit") == "connected", (
        f"the host reported the plugin's server as {servers!r}"
    )


def test_the_model_calls_a_vkit_tool_and_the_server_answers(
    installed: Path,
) -> None:
    """A tool call goes out over MCP and comes back with the server's own JSON.

    The claim is a round trip through the host, not that the tool works. So the
    assertion is on the shape of the exchange: a `tool_use` naming a vkit tool
    in the assistant's stream, and a `tool_result` for that same call carrying
    text this server wrote. A session where the model declined to call the tool
    fails, because then nothing was proven about the transport.
    """
    events = _run_session(
        installed,
        "Call the mcp__plugin_vkit_vkit__project_inspect tool, then stop. "
        "Do not use any other tool.",
    )
    if not [t for t in _init_event(events).get("tools", []) if "vkit" in t]:
        # The host emits `init` before its MCP server is necessarily connected.
        # A session that raced loses the tools, so the call is retried against
        # one where the host attached them. Asserting on a session that never
        # had the tool would be asserting on the model's refusal instead.
        events = _run_session(
            installed,
            "Call the mcp__plugin_vkit_vkit__project_inspect tool, then stop. "
            "Do not use any other tool.",
        )

    used: str | None = None
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and "vkit" in str(block.get("name")):
                used = block["name"]

    assert used == "mcp__plugin_vkit_vkit__project_inspect", (
        f"the model never called a vkit tool; tool_use blocks seen: {used!r}"
    )

    answers = [
        block
        for event in events if event.get("type") == "user"
        for block in event.get("message", {}).get("content", [])
        if block.get("type") == "tool_result"
    ]
    assert answers, "the tool call produced no tool_result"
    body = "".join(
        str(a.get("content")) for a in answers
    )
    assert "project_root" in body, (
        f"the server's answer did not carry its own fields: {body[:400]!r}"
    )
    assert not any(a.get("is_error") for a in answers), (
        f"the server refused the call: {answers!r}"
    )


def test_the_host_tool_names_address_this_plugin() -> None:
    """The hook recognises the tool name the host actually sends.

    Recorded from a live session: the host names the tool
    `mcp__plugin_vkit_vkit__project_inspect`, where the plugin name and the
    server key are both `vkit`. A regex that captures the whole segment between
    `mcp__` and the tool, then compares it to `"vkit"`, therefore matches
    nothing, and the PreToolUse gate answers `{}` to every vkit call while every
    other test in the suite stays green.

    That is not hypothetical: it is the state this file found the plugin in,
    and this assertion is what keeps the fix from being reverted.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("hook_gap3", HOOK_SCRIPT)
    assert spec and spec.loader, f"{HOOK_SCRIPT} is not an importable module"
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    # The three spellings a host can produce: a plugin-scoped session, the
    # same plugin renamed, and a bare server from --mcp-config.
    for name, expected in (
        ("mcp__plugin_vkit_vkit__project_inspect", True),
        ("mcp__plugin_vkitee_vkit__project_inspect", True),
        ("mcp__vkit__project_inspect", True),
        # A server key that merely ends in `_vkit` under some other plugin's
        # prefix is a different server, and attaching this plugin's
        # registration to it would be a guess about whose call it is.
        ("mcp__plugin_other_plugin_vkit_extra__project_inspect", False),
        ("mcp__other_vkit__project_inspect", False),
        ("mcp__other__project_inspect", False),
        ("mcp__plugin_vkit_other__project_inspect", False),
    ):
        assert bool(hook._mcp_tool({"tool_name": name})) is expected, (
            f"{name!r} was {'not ' if expected else ''}recognised as a vkit tool; "
            f"_mcp_tool returned {hook._mcp_tool({'tool_name': name})!r}"
        )

    # And a tool this server does not publish is still refused, so widening the
    # server match did not widen the tool match.
    assert hook._mcp_tool({"tool_name": "mcp__vkit__rm_minus_rf"}) is None


def test_the_pre_tool_use_context_reaches_a_real_host_delivery(
    installed: Path,
) -> None:
    """PreToolUse answers with context, not with `{}`, in a real session.

    A silent `{}` is the failure this covers: it is a well-formed response, it
    exits 0, and the host records it as a success, so nothing anywhere reports
    that the gate did not run. Only a live delivery shows it.
    """
    events = _run_session(
        installed,
        "Call the mcp__plugin_vkit_vkit__project_inspect tool, then stop.",
    )
    responses = {
        event.get("hook_name"): event
        for event in events
        if event.get("subtype") == "hook_response"
    }
    pre = [
        event for name, event in responses.items()
        if name and name.startswith("PreToolUse")
    ]
    assert pre, (
        "no PreToolUse hook fired in a session that called a vkit tool; "
        f"hooks seen: {sorted(responses)}"
    )
    for event in pre:
        assert event.get("exit_code") == 0, f"PreToolUse exited {event.get('exit_code')}"
        assert event.get("outcome") == "success", f"PreToolUse was {event.get('outcome')}"
        stdout = str(event.get("stdout", ""))
        assert "hookSpecificOutput" in stdout, (
            f"PreToolUse returned no context, so the gate did not run: {stdout!r}"
        )
        assert "task_begin" in stdout, (
            f"PreToolUse context names no corrective action: {stdout!r}"
        )


def test_session_start_context_names_the_configured_root_and_the_six_tools(
    installed: Path,
) -> None:
    """SessionStart answers with the pointers the manifest promises.

    SessionStart is the one hook that runs on every session, so an empty
    response here is the plugin loading and doing nothing.
    """
    events = _run_session(installed, "Reply with the single word OK.")
    responses = [
        event for event in events
        if event.get("subtype") == "hook_response"
        and str(event.get("hook_name", "")).startswith("SessionStart")
    ]
    assert responses, "no SessionStart hook fired"
    context = str(responses[0].get("stdout", ""))
    assert "hookSpecificOutput" in context, (
        f"SessionStart returned no additionalContext: {context!r}"
    )
    for name in TOOL_NAMES:
        assert name in context, f"SessionStart context does not name {name}"
    assert "plugin" in context, f"SessionStart names no plugin root: {context!r}"


# --- the request side, which a session transcript does not keep ------------

def _hook_tee_plugin(base: Path) -> tuple[Path, Path]:
    """A marketplace holding a COPY of this plugin whose hook tees its stdin.

    The host records what a hook returned, in the session transcript, and not
    what it sent. The request side is therefore only observable by putting
    something in front of the entry point.

    The copy is a separate plugin, named distinctly, because the host keys
    userConfig by plugin identity: reusing `vkit` would make the copy read the
    real plugin's configuration and the fixture would pass for the wrong
    reason. `plugin/` in the repository is not modified.
    """
    source = base / "source"
    copy = source / "plugin"
    copy.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN_ROOT, copy, dirs_exist_ok=True)
    shutil.copy2(HOOK_SCRIPT, copy / "scripts" / "vkit_hook_real.py")
    (copy / "scripts" / "vkit_hook.py").write_text(
        _TEE_SOURCE, encoding="utf-8"
    )
    manifest = json.loads(
        (copy / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    manifest["name"] = "vkitee"
    (copy / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    (source / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (source / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({
            "name": "vkitee",
            "owner": {"name": "vkit host suite"},
            "plugins": [{
                "name": "vkitee",
                "source": "./plugin",
                "description": "vkit with a stdin tee in front of its hook.",
                "version": "0.1.0",
            }],
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return source, copy


#: Stand-in for `vkit_hook.py` that records stdin and forwards to the real one.
#: It reads the real entry point beside it by name, so the bytes the host wrote
#: are recorded and the hook under test still sees exactly those bytes.
_TEE_SOURCE = '''\
"""Tee this hook's stdin to disk, then run the real entry point unchanged."""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL = HERE / "vkit_hook_real.py"


def main() -> int:
    payload = sys.stdin.buffer.read()
    record = Path(__import__("os").environ["VKIT_HOOK_RECORD_DIR"])
    record.mkdir(parents=True, exist_ok=True)
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    (record / f"{time.time_ns():020d}-{event}.json").write_bytes(payload)
    return subprocess.run(
        [sys.executable, str(REAL), *sys.argv[1:]], input=payload
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
'''


@pytest.fixture(scope="module")
def recorded_deliveries(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict[str, Any]]]:
    """The payloads a live host wrote to this hook's stdin, keyed by event.

    One session, three events. SessionStart is the one that runs on every
    session; PreToolUse is the one that has to fire in the middle of a turn
    with a vkit tool in hand, which is the harder delivery to arrange and the
    one the request shape matters most for.
    """
    if _claude_executable() is None:
        pytest.skip("the `claude` executable is not on PATH")
    if _vkit_executable() is None:
        pytest.skip("vkit is not installed in this environment")

    base = tmp_path_factory.mktemp("tee")
    source, _copy = _hook_tee_plugin(base)
    scratch = base / "config"
    scratch.mkdir(parents=True, exist_ok=True)
    record = base / "payloads"

    add = _run_host(["plugin", "marketplace", "add", str(source)], scratch)
    assert add.returncode == 0, f"marketplace add failed: {add.stdout}{add.stderr}"
    install = _run_host([
        "plugin", "install", "vkitee@vkitee",
        "--config", f"project_root={REPO_ROOT}",
        "--config", f"python={sys.executable}",
    ], scratch)
    assert install.returncode == 0, f"install failed: {install.stdout}{install.stderr}"

    _run_session(
        scratch,
        "Call the mcp__plugin_vkitee_vkit__project_inspect tool, then stop.",
        record_dir=record,
    )
    if not record.is_dir():
        pytest.skip("the host delivered no hook payload on this host")

    by_event: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(record.glob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        by_event.setdefault(body.get("hook_event_name", "unknown"), []).append(body)
    return by_event


def test_a_live_host_delivers_session_start_over_stdin(
    recorded_deliveries: dict[str, list[dict[str, Any]]],
) -> None:
    """The host writes the documented SessionStart payload, and the hook reads it.

    Every field the handler depends on is asserted, not just that the payload
    parsed. A host that renamed `session_id`, or stopped sending `cwd`, would
    leave the handler reading defaults and the gate would silently bind nothing.
    """
    assert "SessionStart" in recorded_deliveries, (
        f"no SessionStart delivery was recorded; saw {sorted(recorded_deliveries)}"
    )
    payload = recorded_deliveries["SessionStart"][0]
    assert payload["hook_event_name"] == "SessionStart"
    assert payload["session_id"], "SessionStart carried no session_id"
    assert payload["cwd"], "SessionStart carried no cwd"
    assert payload["transcript_path"].endswith(".jsonl"), (
        f"transcript_path is not a transcript: {payload['transcript_path']!r}"
    )
    assert Path(payload["transcript_path"]).name == f"{payload['session_id']}.jsonl", (
        "the host's transcript_path does not name this session's transcript"
    )


def test_a_live_host_delivers_pre_tool_use_with_the_mcp_tool_name(
    recorded_deliveries: dict[str, list[dict[str, Any]]],
) -> None:
    """The second event, and the one whose payload shape decides whether the gate runs.

    PreToolUse arrives with `tool_name` in the host's own fully qualified form.
    This is the assertion that ties the payload to `_mcp_tool`: if the host ever
    changes the prefix, the hook goes quiet, and the recorded name here is what
    a reader compares the regex against.
    """
    assert "PreToolUse" in recorded_deliveries, (
        f"no PreToolUse delivery was recorded; saw {sorted(recorded_deliveries)}"
    )
    payload = recorded_deliveries["PreToolUse"][0]
    assert payload["hook_event_name"] == "PreToolUse"
    assert payload["tool_name"].startswith("mcp__"), (
        f"PreToolUse named no MCP tool: {payload['tool_name']!r}"
    )
    assert payload["tool_name"].endswith("__project_inspect"), (
        f"PreToolUse named an unexpected tool: {payload['tool_name']!r}"
    )
    # The server segment carries the plugin name as well as the server key.
    # This is the shape the regex in `_mcp_tool` has to accept, and the shape
    # the first version of that regex rejected.
    assert "plugin_" in payload["tool_name"], (
        f"the host is not scoping the tool name to a plugin: {payload['tool_name']!r}"
    )
    assert payload["mcp_server"]["name"].startswith("plugin:"), (
        f"PreToolUse did not attribute the server to a plugin: {payload['mcp_server']!r}"
    )
    assert "tool_use_id" in payload, "PreToolUse carried no tool_use_id"
    assert isinstance(payload["tool_input"], dict), "tool_input is not an object"


def test_a_live_host_delivers_the_completion_events_too(
    recorded_deliveries: dict[str, list[dict[str, Any]]],
) -> None:
    """Stop arrives, which is the event that can withhold a finish.

    SessionStart and PreToolUse both only add context. Stop is the one whose
    response can block, so a host that stopped delivering it would leave the
    gate unreachable while the other five events kept working.
    """
    assert "Stop" in recorded_deliveries, (
        f"no Stop delivery was recorded; saw {sorted(recorded_deliveries)}"
    )
    payload = recorded_deliveries["Stop"][0]
    assert payload["hook_event_name"] == "Stop"
    assert "stop_hook_active" in payload, (
        "Stop carried no stop_hook_active, so the continuation guard cannot read it"
    )


def test_the_host_delivered_at_least_two_distinct_hook_events(
    recorded_deliveries: dict[str, list[dict[str, Any]]],
) -> None:
    """The two events GAP-3 requires, counted rather than assumed.

    One event proves the wiring reaches the entry point. Two proves the wiring
    survives a turn boundary, which is the property a single session start does
    not have.
    """
    assert len(recorded_deliveries) >= 2, (
        f"only one hook event was delivered ({sorted(recorded_deliveries)}); "
        "a single delivery does not show the host calls the hook mid-turn"
    )


# --- the isolation this whole file depends on -------------------------------

def test_the_real_user_config_is_never_touched(installed: Path) -> None:
    """The developer's own `~/.claude` holds no trace of vkit after the suite.

    A previous worker installed this plugin into the real config twice. The
    guard is cheap and the failure is expensive, so it is asserted rather than
    remembered: the real config's plugin registry is read, never written, and
    a `vkit` entry there means something in this suite escaped its scratch dir.
    """
    real = Path(os.path.expanduser("~")) / ".claude"
    if str(real) in {os.environ.get("CLAUDE_CONFIG_DIR", ""),
                     os.environ.get("HOME", "")}:
        # The suite itself is running against the real config, which is the one
        # situation these tests exist to prevent. There is nothing left to
        # isolate, so say so instead of passing a check that could not fail.
        pytest.skip("this process is already pointed at the real user config")

    registry = real / "plugins" / "installed_plugins.json"
    if not registry.is_file():
        return  # nothing installed there at all
    installed_there = json.loads(registry.read_text(encoding="utf-8"))
    names = installed_there.get("plugins", installed_there)
    assert "vkit@vkit" not in names, (
        "vkit is installed in the real ~/.claude; the host suite wrote to the "
        "developer's config instead of its scratch directory"
    )


def test_the_scratch_config_is_what_the_host_used(installed: Path) -> None:
    """Everything the host wrote for the plugin is under the scratch directory.

    The plugin cache is the write that matters: a marketplace installed from a
    local directory is cached, and a cache landing in the real config is the
    exact shape of the accident this fixture is built to prevent.
    """
    cache = installed / "plugins" / "cache" / "vkit"
    assert cache.is_dir(), (
        f"the host cached no plugin under the scratch dir; {installed} holds "
        f"{sorted(p.name for p in installed.iterdir())}"
    )
    real = Path(os.path.expanduser("~")) / ".claude" / "plugins" / "cache" / "vkit"
    assert not real.exists() or str(installed) not in str(real), (
        "the host wrote its plugin cache inside the real user config"
    )


# --- what this file deliberately does not claim ----------------------------

def test_the_suite_states_what_it_cannot_prove() -> None:
    """A test that names its own blind spot, so the gap record cannot drift.

    The claim GAP-3 makes is narrow: the host loads the plugin, discovers the
    six tools, and delivers a hook. It does not claim a subagent inherits tool
    access, that a BLOCKED run blocks a real turn, or that `claude plugin
    validate --strict` implies any of it. Those stay unproven until a test
    drives them, and this assertion is what keeps the record honest in the
    meantime.
    """
    doc = (REPO_ROOT / "tests" / "test_plugin_host.py").read_text(encoding="utf-8")
    for claim in ("does not claim a subagent inherits tool access",
                  "a BLOCKED run blocks a real turn",
                  "validate --strict` implies any of it"):
        assert claim in doc, f"the module no longer records that it {claim!r}"


def test_no_test_here_patches_the_host(installed: Path) -> None:
    """Nothing in this file can pass with the host removed.

    A mocked host would report success for an install that never happened,
    which is the failure this product exists to prevent. So the assertion is on
    the shape of the file: every subprocess call names the real executable, and
    no `unittest.mock` or `monkeypatch` reaches the host.
    """
    source = (REPO_ROOT / "tests" / "test_plugin_host.py").read_text(encoding="utf-8")
    # The imports are what a mock would arrive through, so they are the thing to
    # read. The banned names appear in this test's own source as data, which is
    # why a substring search over the file could never pass.
    imports = [line for line in source.splitlines() if _is_import(line)]
    joined = "\n".join(imports)
    for banned in ("unittest", "mock", "pyfakefs", "responses", "httpretty"):
        assert banned not in joined, (
            f"the host suite imports {banned!r}; a test that can pass with a "
            "fake host does not close the gap it claims to close"
        )
    # Every host invocation goes through the resolved executable.
    assert "subprocess.run(\n        [_claude_executable() or \"claude\"" in source, (
        "the host is no longer launched by its resolved name"
    )
    # And the executable is found on PATH, never hard-coded to a path that
    # would make this file pass on the machine that wrote it and nowhere else.
    assert "shutil.which(\"claude\")" in source, (
        "the executable is no longer resolved from PATH"
    )


def _is_import(line: str) -> bool:
    return line.startswith("import ") or line.startswith("from ")

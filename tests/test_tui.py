from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import pytest
from helpers import NO_WINDOW, ROOT, git, scenario_check
from rich.console import Console

from vkit.paths import open_project
from vkit.settings import launcher_settings_path
from vkit.tui import Launcher


@pytest.fixture
def run_ui(monkeypatch):
    def drive(project, script, *, port=0, width=180, height=40):
        output = io.StringIO()
        console = Console(file=output, width=width, height=height, color_system=None)
        lines = iter(script(output))
        monkeypatch.setattr(console, "input", lambda *args, **kwargs: next(lines))
        assert Launcher(open_project(project), port=port, console=console).run() == 0
        return output.getvalue()
    return drive


def console_status(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        connection.close()


def last_port(output):
    return int(re.findall(r"http://127\.0\.0\.1:(\d+)/", output.getvalue())[-1])


def is_listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def refresh_until(output, expected):
    deadline = time.monotonic() + 20
    while expected not in output.getvalue():
        assert time.monotonic() < deadline, f"Did not display {expected!r}"
        time.sleep(0.05)
        yield ""


def test_tabs_actual_cli_commands_and_agent_config(make_project, monkeypatch, run_ui):
    project = make_project({"README.md": "launcher\n"}, [], accept=False)
    paged = []
    monkeypatch.setattr("rich.pager.SystemPager.show", lambda self, text: paged.append(text))

    def script(output):
        yield "2"
        yield "status"
        yield from refresh_until(output, "BLOCKED")
        assert str(project) in output.getvalue()
        yield "find regex alphabet"
        yield from refresh_until(output, "compare_matchsets")
        yield "output"
        assert "compare_matchsets" in paged[-1]
        yield "4"
        assert '"mcpServers"' in output.getvalue()
        assert '"command":' in output.getvalue()
        assert "mcp" in output.getvalue() and "serve" in output.getvalue()
        yield "config"
        assert json.loads(paged[-1]) == {"mcpServers": {"vkit": {
            "command": sys.executable,
            "args": ["-m", "vkit.cli", "mcp", "serve", "--project", str(project)],
        }}}
        yield "unknown"
        assert "Unknown command for this tab" in output.getvalue()
        yield "q"

    text = run_ui(project, script)
    assert "1 Home" in text and "2 Checks" in text and "3 Settings" in text and "4 Agent" in text
    assert "│" in text and "─" in text


def test_console_lifecycle_and_browser(make_project, monkeypatch, run_ui):
    project = make_project({"README.md": "web\n"}, [], accept=False)
    opened, ports = [], []
    monkeypatch.setattr("vkit.tui.webbrowser.open", lambda url: opened.append(url) or True)

    def script(output):
        yield "open"
        ports.append(last_port(output))
        assert console_status(ports[-1])["project"] == str(project)
        assert opened == [f"http://127.0.0.1:{ports[-1]}/"]
        yield "start"
        assert last_port(output) == ports[-1]
        yield "stop"
        assert not is_listening(ports[-1])
        yield "start"
        ports.append(last_port(output))
        assert console_status(ports[-1])["project"] == str(project)
        yield "q"

    run_ui(project, script)
    assert all(not is_listening(port) for port in ports)


def test_switch_validates_before_stopping_server(make_project, tmp_path, run_ui):
    first = make_project({"README.md": "first\n"}, [], accept=False)
    second = tmp_path / "another repo"
    second.mkdir()
    git(second, "init", "-q")

    def script(output):
        yield "start"
        port = last_port(output)
        yield f"project {tmp_path / 'missing'}"
        assert "path does not exist" in output.getvalue()
        assert console_status(port)["project"] == str(first)
        yield "project"
        yield str(second)
        assert not is_listening(port)
        yield "agent"
        assert json.dumps(str(second))[1:-1] in output.getvalue()
        yield "home"
        yield "start"
        assert console_status(last_port(output))["project"] == str(second)
        yield "q"

    run_ui(first, script)


@pytest.mark.parametrize("tab", ["home", "settings"])
def test_folder_selection_cancel_validation_and_save(make_project, tmp_path, monkeypatch, run_ui, tab):
    first = make_project({"README.md": "first\n"}, [], accept=False)
    second = tmp_path / "chosen repo"
    second.mkdir()
    git(second, "init", "-q")
    choices = iter([None, tmp_path, second])
    starts = []

    def choose(initial):
        starts.append(initial)
        return next(choices)

    monkeypatch.setattr("vkit.tui._pick_directory", choose)

    def script(output):
        yield "settings"
        yield f"project {first}"
        saved = launcher_settings_path().read_bytes()
        yield "home"
        yield "start"
        port = last_port(output)
        yield tab
        yield "browse"
        assert "Selection cancelled" in output.getvalue()
        assert console_status(port)["project"] == str(first)
        assert launcher_settings_path().read_bytes() == saved
        yield "browse"
        assert "not inside a Git repository" in output.getvalue()
        assert console_status(port)["project"] == str(first)
        assert launcher_settings_path().read_bytes() == saved
        yield "browse"
        if tab == "home":
            assert not is_listening(port)
            assert launcher_settings_path().read_bytes() == saved
            yield "agent"
            assert json.dumps(str(second))[1:-1] in output.getvalue()
            yield "home"
            yield "start"
            assert console_status(last_port(output))["project"] == str(second)
        else:
            assert json.loads(launcher_settings_path().read_text())["project"] == str(second)
            assert console_status(port)["project"] == str(first)
        yield "q"

    run_ui(first, script)
    assert starts == [first, first, first]


def test_missing_tk_keeps_typed_selection_available(make_project, tmp_path, monkeypatch, run_ui):
    first = make_project({"README.md": "first\n"}, [], accept=False)
    second = tmp_path / "typed repo"
    second.mkdir()
    git(second, "init", "-q")
    monkeypatch.setitem(sys.modules, "tkinter", None)

    def script(output):
        yield "browse"
        assert "Folder picker unavailable. Use project to type a path instead." in output.getvalue()
        yield f"project {second}"
        yield "start"
        assert console_status(last_port(output))["project"] == str(second)
        yield "q"

    run_ui(first, script)


def test_busy_port_keeps_interface_usable(make_project, run_ui):
    project = make_project({"README.md": "busy\n"}, [], accept=False)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()

        def script(output):
            yield "start"
            assert any(code in output.getvalue() for code in (("10048", "10013") if sys.platform == "win32"
                                                             else ("Address already in use",)))
            yield "3"
            assert str(launcher_settings_path()) in output.getvalue()
            yield "q"

        run_ui(project, script, port=occupied.getsockname()[1])


def test_settings_save_validate_reload_and_clear_default(make_project, run_ui):
    project = make_project({"README.md": "settings\n"}, [], accept=False)

    def script(output):
        yield "settings"
        yield f"project {project}"
        yield "port"
        yield "9001"
        yield "browser off"
        expected = {"version": 1, "project": str(project), "port": 9001, "open_browser": False}
        assert json.loads(launcher_settings_path().read_text()) == expected
        original = launcher_settings_path().read_bytes()
        yield "port 65536"
        assert "65535" in output.getvalue()
        yield "browser invalid"
        assert "Browser must be on or off" in output.getvalue()
        yield f"project {project / 'missing'}"
        assert "path does not exist" in output.getvalue()
        assert launcher_settings_path().read_bytes() == original
        yield "q"

    run_ui(project, script)

    def reload(output):
        yield "3"
        assert "9001" in output.getvalue() and str(project) in output.getvalue()
        yield "project"
        yield ""
        assert json.loads(launcher_settings_path().read_text())["project"] is None
        yield "q"

    run_ui(project, reload)


@pytest.mark.parametrize("end", ["q", "cancel", "eof", "interrupt"])
def test_cancelling_active_check_reaps_its_process_tree(make_project, run_ui, end):
    source = """
import socket, time
from pathlib import Path
server = socket.socket()
server.bind(('127.0.0.1', 0))
server.listen()
Path('listening.txt').write_text(str(server.getsockname()[1]))
time.sleep(120)
"""
    check = scenario_check("slow", "slow.py", scenarios=["ok"], inputs=["slow.py"], timeout=120)
    project = make_project({"slow.py": source}, [check])
    ports = []

    def script(output):
        yield "checks"
        yield "run"
        deadline = time.monotonic() + 20
        while not (project / "listening.txt").exists():
            assert time.monotonic() < deadline, "The actual check did not start"
            time.sleep(0.05)
        ports.append(int((project / "listening.txt").read_text()))
        assert is_listening(ports[-1])
        yield "home"
        yield f"project {project}"
        assert "Cancel the active command" in output.getvalue()
        yield "3"
        assert str(launcher_settings_path()) in output.getvalue()
        if end == "eof":
            raise EOFError
        if end == "interrupt":
            raise KeyboardInterrupt
        yield end
        if end == "cancel":
            yield from refresh_until(output, "Cancelled.")
            assert not is_listening(ports[-1])
            yield "q"

    run_ui(project, script)
    assert not is_listening(ports[-1])


@pytest.mark.parametrize("width", [60, 110])
def test_repository_python_entry_point(make_project, width):
    project = make_project({"README.md": "entry\n"}, [], accept=False)
    done = subprocess.run(
        [sys.executable, str(ROOT / "tui.py"), "--project", str(project)],
        check=False, input="3\n4\nq\n", capture_output=True, text=True, timeout=30,
        env={**os.environ, "COLUMNS": str(width), "PYTHONIOENCODING": "utf-8"}, encoding="utf-8", **NO_WINDOW,
    )
    assert done.returncode == 0, done.stderr
    assert "1 Home" in done.stdout and "4 Agent" in done.stdout
    assert '"mcpServers"' in done.stdout
    assert "│" in done.stdout and "─" in done.stdout
    without_prompts = re.sub(r"(?:home|checks|settings|agent) > ", "", done.stdout)
    assert all(len(line) <= width for line in without_prompts.splitlines())


def test_tui_invalid_project_is_a_cli_error(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "vkit.cli", "tui", "--project", str(tmp_path / "missing")],
        check=False, capture_output=True, text=True, timeout=30, **NO_WINDOW,
    )
    assert done.returncode == 2
    assert "path does not exist" in done.stderr


@pytest.mark.parametrize("wrapper", ["start.bat", "start.sh"])
def test_wrapper_forwards_arguments_from_another_directory(tmp_path, wrapper):
    missing = tmp_path / "missing repo with spaces"
    if wrapper.endswith(".bat"):
        if sys.platform != "win32":
            pytest.skip("Windows batch wrapper")
        command = ["cmd.exe", "/d", "/c", str(ROOT / wrapper)]
    else:
        if sys.platform == "win32" or shutil.which("sh") is None:
            pytest.skip("POSIX shell wrapper")
        command = ["sh", str(ROOT / wrapper)]
    done = subprocess.run(
        [*command, "--project", str(missing)], cwd=tmp_path,
        check=False, capture_output=True, text=True, timeout=60, **NO_WINDOW,
    )
    assert done.returncode == 2
    assert "path does not exist" in done.stderr
    assert str(missing) in done.stderr

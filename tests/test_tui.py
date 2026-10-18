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

import pytest
from helpers import NO_WINDOW, ROOT, git
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
        yield "start"
        assert console_status(last_port(output))["project"] == str(second)
        yield "q"

    run_ui(first, script)


@pytest.mark.parametrize("inline", [True, False])
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_typed_repository_paths(make_project, tmp_path, run_ui, inline, quote):
    first = make_project({"README.md": "first\n"}, [], accept=False)
    second = tmp_path / "typed repo"
    second.mkdir()
    git(second, "init", "-q")
    typed = f"  {quote}{second}{quote}  "

    def script(output):
        if inline:
            yield f"project {typed}"
        else:
            yield "project"
            yield typed
        assert "Repository changed." in output.getvalue()
        yield "start"
        assert console_status(last_port(output))["project"] == str(second)
        assert not launcher_settings_path().exists()
        yield "q"

    run_ui(first, script)


def test_cursor_visible_at_input(make_project, monkeypatch):
    project = make_project({"README.md": "cursor\n"}, [], accept=False)
    output = io.StringIO()
    monkeypatch.setenv("TERM", "xterm-256color")
    console = Console(file=output, force_terminal=True, legacy_windows=False)

    def enter(prompt):
        assert "\x1b[?1049h" in output.getvalue()
        assert "\x1b[?25l" not in output.getvalue()
        return "q"

    monkeypatch.setattr(console, "input", enter)
    assert Launcher(open_project(project), console=console).run() == 0
    assert "\x1b[?1049l" in output.getvalue()


def test_folder_selection_cancel_and_validation(make_project, tmp_path, monkeypatch, run_ui):
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
        yield "start"
        port = last_port(output)
        yield "browse"
        assert "Selection cancelled" in output.getvalue()
        assert console_status(port)["project"] == str(first)
        yield "browse"
        assert "not inside a Git repository" in output.getvalue()
        assert console_status(port)["project"] == str(first)
        yield "browse"
        assert not is_listening(port)
        yield "start"
        assert console_status(last_port(output))["project"] == str(second)
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
            yield "unknown"
            assert "enter open for the web console" in output.getvalue()
            yield "q"

        run_ui(project, script, port=occupied.getsockname()[1])


@pytest.mark.parametrize("end", ["q", "eof", "interrupt"])
def test_exit_stops_owned_server(make_project, run_ui, end):
    project = make_project({"README.md": "shutdown\n"}, [], accept=False)
    ports = []

    def script(output):
        yield "start"
        ports.append(last_port(output))
        assert is_listening(ports[-1])
        if end == "eof":
            raise EOFError
        if end == "interrupt":
            raise KeyboardInterrupt
        yield "q"

    run_ui(project, script)
    assert not is_listening(ports[-1])


@pytest.mark.parametrize("width", [60, 110])
def test_repository_python_entry_point(make_project, width):
    project = make_project({"README.md": "entry\n"}, [], accept=False)
    done = subprocess.run(
        [sys.executable, str(ROOT / "tui.py"), "--project", str(project)],
        check=False, input="q\n", capture_output=True, text=True, timeout=30,
        env={**os.environ, "COLUMNS": str(width), "PYTHONIOENCODING": "utf-8"}, encoding="utf-8", **NO_WINDOW,
    )
    assert done.returncode == 0, done.stderr
    assert "vkit / Home" in done.stdout
    assert "Manage checks, settings, and agent connections" in done.stdout
    assert "4 Agent" not in done.stdout
    assert "\u2502" in done.stdout and "\u2500" in done.stdout
    without_prompts = re.sub(r"home > ", "", done.stdout)
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

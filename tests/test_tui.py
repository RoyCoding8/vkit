from __future__ import annotations

import asyncio
import http.client
import json
import runpy
import shutil
import socket
import subprocess
import sys
import time

import pytest
from helpers import NO_WINDOW, ROOT, git, scenario_check
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TextArea,
)

from vkit.paths import open_project
from vkit.settings import launcher_settings_path
from vkit.tui import LauncherApp


@pytest.fixture
def anyio_backend():
    return "asyncio"


def log_text(app, selector):
    return "".join(line.text for line in app.query_one(selector, RichLog).lines)


def console_status(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        connection.close()


def is_listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.anyio
async def test_tabs_commands_and_resizing(make_project):
    project = make_project({"README.md": "launcher\n"}, [], accept=False)
    app = LauncherApp(open_project(project))
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "checks"
        await pilot.click("#status")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "BLOCKED" in log_text(app, "#command-output")
        assert str(project) in log_text(app, "#command-output")
        assert not app.query_one("#status", Button).disabled
        app.query_one("#tool-query", Input).value = "regex alphabet"
        app.query_one("#find-tools", Button).scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#find-tools")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "compare_matchsets" in log_text(app, "#command-output")
        await pilot.click("#--content-tab-agent")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "agent"
        assert json.loads(app.query_one("#agent-config", TextArea).text)["mcpServers"]["vkit"] == {
            "command": sys.executable,
            "args": ["-m", "vkit.cli", "mcp", "serve", "--project", str(project)],
        }
        await pilot.click("#copy-agent")
        await pilot.pause()
        assert json.loads(app.clipboard)["mcpServers"]["vkit"]["args"][-1] == str(project)
        await pilot.resize_terminal(60, 24)
        await pilot.press("f1")
        await pilot.pause()
        assert app.query_one("#server-card").region.y > app.query_one("#repository-card").region.y
        app.query_one("#start-web", Button).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert console_status(app.server.server_port)["project"] == str(project)


@pytest.mark.anyio
async def test_console_lifecycle_and_browser(make_project, monkeypatch):
    project = make_project({"README.md": "web\n"}, [], accept=False)
    opened = []
    monkeypatch.setattr("vkit.tui.webbrowser.open", lambda url: opened.append(url) or True)
    app = LauncherApp(open_project(project))
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#open-web")
        await pilot.pause()
        port = app.server.server_port
        assert console_status(port)["project"] == str(project)
        assert opened == [f"http://127.0.0.1:{port}/"]
        await pilot.click("#stop-web")
        await pilot.pause()
        assert not is_listening(port)
        await pilot.click("#start-web")
        await pilot.pause()
        port = app.server.server_port
        assert console_status(port)["project"] == str(project)
        await pilot.press("ctrl+q")
    assert not is_listening(port)


@pytest.mark.anyio
async def test_change_project_validates_before_stopping_server(make_project, tmp_path):
    first = make_project({"README.md": "first\n"}, [], accept=False)
    second = tmp_path / "another repo"
    second.mkdir()
    git(second, "init", "-q")
    app = LauncherApp(open_project(first))
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#start-web")
        await pilot.pause()
        port = app.server.server_port
        app.query_one("#project-path", Input).value = str(tmp_path / "missing")
        await pilot.click("#switch-project")
        await pilot.pause()
        assert "path does not exist" in str(app.query_one("#home-feedback", Static).content)
        assert console_status(port)["project"] == str(first)
        app.query_one("#project-path", Input).value = str(second)
        await pilot.pause(0.3)
        await pilot.click("#switch-project")
        await pilot.pause()
        assert not is_listening(port)
        await pilot.press("f4")
        await pilot.pause()
        assert json.loads(app.query_one("#agent-config", TextArea).text)["mcpServers"]["vkit"]["args"][-1] == str(second)
        await pilot.press("f1")
        await pilot.pause()
        await pilot.click("#start-web")
        await pilot.pause()
        assert console_status(app.server.server_port)["project"] == str(second)


@pytest.mark.anyio
async def test_busy_port_keeps_interface_usable(make_project):
    project = make_project({"README.md": "busy\n"}, [], accept=False)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        app = LauncherApp(open_project(project), port=occupied.getsockname()[1])
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            await pilot.click("#start-web")
            await pilot.pause()
            message = str(app.query_one("#home-feedback", Static).content)
            assert any(code in message for code in (("10048", "10013") if sys.platform == "win32"
                                                    else ("Address already in use",)))
            assert app.server is None
            await pilot.press("f3")
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "settings"


@pytest.mark.anyio
async def test_settings_save_validate_and_reload(make_project):
    project = make_project({"README.md": "settings\n"}, [], accept=False)
    app = LauncherApp(open_project(project))
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        app.query_one("#default-project", Input).value = str(project)
        app.query_one("#default-port", Input).value = "9001"
        await pilot.click("#default-browser")
        await pilot.pause()
        await pilot.click("#save-defaults")
        await pilot.pause()
        expected = {"version": 1, "project": str(project), "port": 9001, "open_browser": False}
        assert json.loads(launcher_settings_path().read_text()) == expected
        original = launcher_settings_path().read_bytes()
        app.query_one("#default-port", Input).value = "65536"
        await pilot.pause(0.3)
        await pilot.click("#save-defaults")
        await pilot.pause()
        assert "65535" in str(app.query_one("#settings-feedback", Static).content)
        assert launcher_settings_path().read_bytes() == original
    async with LauncherApp(open_project(project)).run_test() as pilot:
        await pilot.pause()
        assert pilot.app.query_one("#default-project", Input).value == str(project)
        assert pilot.app.query_one("#default-port", Input).value == "9001"
        assert pilot.app.query_one("#default-browser", Checkbox).value is False


@pytest.mark.anyio
async def test_quit_cancels_active_check_and_its_process_tree(make_project):
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
    app = LauncherApp(open_project(project))
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        await pilot.click("#run-checks")
        await pilot.pause()
        deadline = time.monotonic() + 15
        while not (project / "listening.txt").exists():
            assert time.monotonic() < deadline, "The actual check did not start"
            await asyncio.sleep(0.05)
        port = int((project / "listening.txt").read_text())
        assert is_listening(port)
        assert app.query_one("#switch-project", Button).disabled
        await pilot.press("f3")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "settings"
        await pilot.press("ctrl+q")
    deadline = time.monotonic() + 5
    while is_listening(port) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not is_listening(port)


def test_tui_invalid_project_is_a_cli_error(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "vkit.cli", "tui", "--project", str(tmp_path / "missing")],
        check=False, capture_output=True, text=True, timeout=30, **NO_WINDOW,
    )
    assert done.returncode == 2
    assert "path does not exist" in done.stderr


@pytest.mark.parametrize("exit_code", [0, 7])
def test_repository_python_entry_point(make_project, monkeypatch, exit_code):
    project = make_project({"README.md": "entry\n"}, [], accept=False)

    async def drive(app):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert str(app.query_one("#current-project", Static).content) == str(project)
            if exit_code:
                app.exit(return_code=exit_code)
            else:
                await pilot.press("ctrl+q")
        return app.return_value

    monkeypatch.setattr(LauncherApp, "run", lambda app: asyncio.run(drive(app)))
    monkeypatch.setattr(sys, "argv", [str(ROOT / "tui.py"), "--project", str(project)])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(ROOT / "tui.py"), run_name="__main__")
    assert exited.value.code == exit_code


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

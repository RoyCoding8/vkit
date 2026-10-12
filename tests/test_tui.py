from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
import sys
import time

import pytest

from helpers import DRIVER, NO_WINDOW, ROOT, git, scenario_check
from vkit.paths import open_project
from vkit.tui import launch


def test_menu_uses_cli_and_reports_scoped_status(make_project):
    project = make_project({"README.md": "launcher\n"}, [], accept=False)
    done = subprocess.run(
        [sys.executable, "-m", "vkit.cli", "tui", "--project", str(project)],
        input="invalid\n4\n9\nregex alphabet\nm\nq\n", capture_output=True,
        text=True, timeout=30, **NO_WINDOW,
    )
    assert done.returncode == 0, done.stderr
    assert "Choose a number, m, p, e, or q." in done.stdout
    assert "BLOCKED" in done.stdout
    assert "compare_matchsets" in done.stdout
    assert '"mcpServers"' in done.stdout
    assert json.dumps(str(project)) in done.stdout


def test_subprocess_console_answers_while_menu_waits_for_input(make_project, tmp_path):
    check = scenario_check("sample", "driver.py", scenarios=["ok"], inputs=["driver.py"])
    project = make_project({"driver.py": DRIVER}, [check])
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with (tmp_path / "launcher.log").open("w+", encoding="utf-8") as output:
        proc = subprocess.Popen(
            [sys.executable, "-m", "vkit.cli", "tui", "--project", str(project), "--port", str(port)],
            stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, text=True, **NO_WINDOW,
        )
        try:
            proc.stdin.write("2\n")
            proc.stdin.flush()
            deadline = time.monotonic() + 30
            while True:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    connection.request("GET", "/api/status")
                    response = connection.getresponse()
                    assert response.status == 200
                    body = json.loads(response.read())
                    break
                except ConnectionRefusedError:
                    if proc.poll() is not None or time.monotonic() >= deadline:
                        output.seek(0)
                        pytest.fail(f"Console did not start. Launcher output:\n{output.read()}")
                    time.sleep(0.05)
                finally:
                    connection.close()
            assert body["project"] == str(project)
            assert [check["id"] for check in body["checks"]] == ["sample"]
            proc.stdin.write("q\n")
            proc.stdin.flush()
            assert proc.wait(timeout=10) == 0
        except Exception:
            output.seek(0)
            print(output.read())
            raise
        finally:
            proc.stdin.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


@pytest.mark.parametrize("ending", ["q", EOFError(), KeyboardInterrupt()])
def test_console_serves_project_and_releases_port(make_project, monkeypatch, ending):
    project = make_project({"README.md": "web\n"}, [], accept=False)
    opened = []
    monkeypatch.setattr("vkit.tui.webbrowser.open", lambda url: opened.append(url) or True)
    choices = iter(["1", "2", "3", "2", ending])
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    step = 0

    def choose(*args, **kwargs):
        nonlocal step
        choice = next(choices)
        if step:
            if step == 3:
                with socket.socket() as probe:
                    probe.settimeout(0.5)
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
            else:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", "/api/status")
                    response = connection.getresponse()
                    assert response.status == 200
                    assert json.loads(response.read())["project"] == str(project)
                finally:
                    connection.close()
        step += 1
        if isinstance(choice, BaseException):
            raise choice
        return choice

    monkeypatch.setattr("vkit.tui.Prompt.ask", choose)
    assert launch(open_project(project), port=port) == 0
    assert opened == [f"http://127.0.0.1:{port}/"]
    with socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_change_project_keeps_old_selection_on_invalid_path(make_project, tmp_path, monkeypatch, capsys):
    first = make_project({"README.md": "first\n"}, [], accept=False)
    second = tmp_path / "another repo"
    second.mkdir()
    git(second, "init", "-q")
    choices = iter(["p", str(tmp_path / "missing"), "m", "p", str(second), "m", "q"])
    monkeypatch.setattr("vkit.tui.Prompt.ask", lambda *a, **kw: next(choices))
    assert launch(open_project(first)) == 0
    output = capsys.readouterr().out
    assert "path does not exist" in output
    assert json.dumps(str(first)) in output
    assert json.dumps(str(second)) in output


def test_busy_port_returns_to_menu(make_project, monkeypatch, capsys):
    project = make_project({"README.md": "busy\n"}, [], accept=False)
    choices = iter(["2", "q"])
    monkeypatch.setattr("vkit.tui.Prompt.ask", lambda *a, **kw: next(choices))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        assert launch(open_project(project), port=occupied.getsockname()[1]) == 0
    assert "Web console: stopped" in capsys.readouterr().out


def test_tui_invalid_project_is_a_cli_error(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "vkit.cli", "tui", "--project", str(tmp_path / "missing")],
        capture_output=True, text=True, timeout=30, **NO_WINDOW,
    )
    assert done.returncode == 2
    assert "path does not exist" in done.stderr


def test_repository_python_entry_point(make_project):
    project = make_project({"README.md": "entry\n"}, [], accept=False)
    done = subprocess.run(
        [sys.executable, str(ROOT / "tui.py"), "--project", str(project)],
        input="q\n", cwd=project, capture_output=True, text=True, timeout=30, **NO_WINDOW,
    )
    assert done.returncode == 0, done.stderr
    assert str(project) in done.stdout


@pytest.mark.parametrize("wrapper", ["start.bat", "start.sh"])
def test_wrapper_from_another_directory_with_spaces(tmp_path, wrapper):
    project = tmp_path / "repo with spaces"
    project.mkdir()
    git(project, "init", "-q")
    if wrapper.endswith(".bat"):
        if sys.platform != "win32":
            pytest.skip("Windows batch wrapper")
        command = ["cmd.exe", "/d", "/c", str(ROOT / wrapper)]
    else:
        if sys.platform == "win32" or shutil.which("sh") is None:
            pytest.skip("POSIX shell wrapper")
        command = ["sh", str(ROOT / wrapper)]
    done = subprocess.run(
        [*command, "--project", str(project)], input="m\nq\n", cwd=tmp_path,
        capture_output=True, text=True, timeout=60, **NO_WINDOW,
    )
    assert done.returncode == 0, done.stderr
    assert json.dumps(str(project)) in done.stdout

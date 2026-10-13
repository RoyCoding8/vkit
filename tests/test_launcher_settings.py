from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from helpers import NO_WINDOW, git

from vkit.cli import main
from vkit.settings import (
    LauncherSettings,
    launcher_settings_path,
    load_launcher_settings,
    save_launcher_settings,
)


def test_defaults_do_not_write_a_file():
    assert load_launcher_settings().as_json() == {
        "version": 1, "project": None, "port": 0, "open_browser": True,
    }
    assert not launcher_settings_path().exists()


def test_user_config_location(monkeypatch, tmp_path):
    monkeypatch.delenv("VKIT_CONFIG")
    monkeypatch.setenv("APPDATA" if os.name == "nt" else "XDG_CONFIG_HOME", str(tmp_path))
    assert launcher_settings_path() == tmp_path / "vkit" / "config.json"


def test_json_default_repository_and_explicit_override_from_outside_git(tmp_path):
    first = tmp_path / "first repo"
    second = tmp_path / "second repo"
    for project in (first, second):
        project.mkdir()
        git(project, "init", "-q")
    path = launcher_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"project": str(first)}), encoding="utf-8")
    original = path.read_bytes()
    for extra, expected in (([], first), (["--project", str(second)], second)):
        done = subprocess.run(
            [sys.executable, "-m", "vkit.cli", "status", "--json", *extra],
            cwd=tmp_path, check=False, capture_output=True, text=True, timeout=30, **NO_WINDOW,
        )
        assert done.returncode == 0
        assert json.loads(done.stdout)["project"] == str(expected.resolve())
    assert path.read_bytes() == original


def test_web_options_use_saved_defaults_and_flags_override(make_project, monkeypatch):
    project = make_project({"README.md": "settings\n"}, [], accept=False)
    save_launcher_settings(LauncherSettings(project=str(project), port=8766, open_browser=False))
    observed = []
    monkeypatch.setattr("vkit.console.serve", lambda selected, **kwargs: observed.append(
        (selected.root, kwargs)) or 0)
    monkeypatch.setattr("vkit.tui.launch", lambda selected, **kwargs: observed.append(
        (selected.root, kwargs)) or 0)
    assert main(["console"]) == 0
    assert main(["console", "--port", "0", "--browser"]) == 0
    assert main(["console", "--port", "9000", "--no-browser"]) == 0
    assert main(["tui"]) == 0
    assert main(["tui", "--port", "0"]) == 0
    assert observed == [
        (project, {"port": 8766, "open_browser": False}),
        (project, {"port": 0, "open_browser": True}),
        (project, {"port": 9000, "open_browser": False}),
        (project, {"port": 8766}),
        (project, {"port": 0}),
    ]
    assert load_launcher_settings().port == 8766


@pytest.mark.parametrize("document", ["{broken", "null", '{"port":65536}', '{"project":"relative"}',
                                      '{"open_browser":"false"}', '{"unknown":1}'])
def test_invalid_defaults_report_error_and_preserve_file(document, capsys):
    path = launcher_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(document, encoding="utf-8")
    assert main(["status", "--json"]) == 2
    assert "invalid settings" in json.loads(capsys.readouterr().out)["error"]
    assert path.read_text(encoding="utf-8") == document


def test_out_of_range_cli_port_is_a_request_error(make_project, capsys):
    project = make_project({"README.md": "port\n"}, [], accept=False)
    assert main(["tui", "--project", str(project), "--port", "65536"]) == 2
    assert "port must be between" in capsys.readouterr().err

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from helpers import DRIVER, NO_WINDOW, scenario_check, vkit
from vkit.console import bind
from vkit.paths import open_project
from vkit.settings import launcher_settings_path

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def _get(port: int, path: str, host: str | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_console_rejects_run_path_fragments(make_project):
    project = make_project({"README.md": "run lookup\n"}, [], accept=False)
    with _serve(project) as port:
        status, raw = _get(port, "/api/run?id=..%2Fforeign")

    assert status == 400
    assert json.loads(raw) == {
        "error": "run_id must contain only letters, digits, underscores, or hyphens",
    }


@contextmanager
def _serve(project: Path):
    server = bind(open_project(project), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, path: str, *, method: str = "GET", body: str | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers or {"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def _post(port: int, path: str, payload: object, token: str | None, *, origin: str | None = "same",
          content_type: str | None = "application/json", host: str | None = None,
          declared_length: int | None = None) -> tuple[int, bytes, dict[str, str]]:
    request_host = host or f"127.0.0.1:{port}"
    headers = {"Host": request_host}
    if origin is not None:
        headers["Origin"] = f"http://{request_host}" if origin == "same" else origin
    if token is not None:
        headers["X-Vkit-Token"] = token
    if content_type is not None:
        headers["Content-Type"] = content_type
    if declared_length is not None:
        headers["Content-Length"] = str(declared_length)
    body = payload if isinstance(payload, str) or payload is None else json.dumps(payload)
    return _request(port, path, method="POST", body=body, headers=headers)


def _config(port: int) -> dict:
    status, raw = _get(port, "/api/config")
    assert status == 200
    return json.loads(raw)


def test_console_starts_when_reverse_dns_is_unavailable(make_project, monkeypatch):
    project = make_project({"README.md": "loopback console\n"}, [], accept=False)

    def unavailable(*args, **kwargs):
        raise OSError("reverse DNS is unavailable")

    monkeypatch.setattr("socket.getfqdn", unavailable)
    with _serve(project) as port:
        status, raw = _get(port, "/api/status")
        assert status == 200
        assert json.loads(raw)["project"] == str(project)


def test_the_console_shows_the_gate_checks_and_a_run_log(make_project):
    project = make_project({"ok.py": PASSES + 'print("hello from the driver")\n'},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    _, body = vkit(project, "check", "run", "--check", "ok")
    run_id = body["runs"][0]["run_id"]
    server = bind(open_project(project), 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, page = _get(port, "/")
        assert status == 200 and b"<title>vkit</title>" in page
        # The installed console serves every asset locally, including its components and font.
        for path in ("/console.js", "/console.css", "/assets/material.js", "/assets/theme.css",
                     "/assets/icons.svg", "/assets/logo.svg", "/assets/roboto.woff2"):
            status, asset = _get(port, path)
            assert status == 200 and asset, path
        status, raw = _get(port, "/api/config")
        config = json.loads(raw)
        assert status == 200 and config["definitions"]["ok"]["id"] == "ok"
        assert config["doctor"]["project"] == str(project)
        assert _get(port, "/assets/../../manifest.json")[0] == 404
        status, raw = _get(port, "/api/status")
        report = json.loads(raw)
        assert (status, report["gate"]["verdict"], [c["state"] for c in report["checks"]]) == (200, "READY", ["fresh_pass"])
        status, raw = _get(port, f"/api/run?id={run_id}&offset=-200")
        assert status == 200 and "hello from the driver" in json.loads(raw)["log"]["text"]
        status, _ = _get(port, "/api/status", host="evil.example:80")
        assert status == 403
        assert _get(port, "/assets/material.js", host="evil.example:80")[0] == 403
    finally:
        server.shutdown()
        server.server_close()


def test_console_settings_default_save_restart_and_git_status(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    settings_path = project / ".git" / "vkit" / "config.json"
    defaults = {"version": 1, "theme": "system", "sidebar_collapsed": False,
                "refresh_seconds": 2, "history_limit": 50, "disabled_tools": [],
                "disabled_categories": [], "mcp_exposure": "compact", "mcp_categories": None}
    saved = {**defaults, "theme": "dark", "sidebar_collapsed": True,
             "refresh_seconds": 11, "history_limit": 120}
    with _serve(project) as port:
        config = _config(port)
        assert config["settings"] == defaults
        assert config["settings_defaults"] == defaults
        assert config["settings_path"] == str(settings_path)
        assert not settings_path.exists()
        status, raw, headers = _post(port, "/api/settings", saved, config["csrf_token"])
        assert status == 200 and json.loads(raw) == {"settings": saved, "settings_path": str(settings_path)}
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    with _serve(project) as port:
        config = _config(port)
        assert config["settings"] == saved
        assert config["settings_path"] == str(settings_path)
    status = subprocess.run(["git", "-C", str(project), "status", "--short"], capture_output=True,
                            text=True, check=True, **NO_WINDOW)
    assert status.stdout == ""


def test_launch_defaults_save_restart_manual_edit_and_csrf(make_project):
    project = make_project({"README.md": "global preferences\n"}, [], accept=False)
    path = launcher_settings_path()
    saved = {"version": 1, "project": str(project), "port": 9010, "open_browser": False}
    with _serve(project) as port:
        config = _config(port)
        assert config["launcher_settings_path"] == str(path)
        assert config["launcher_settings"] == {
            "version": 1, "project": None, "port": 0, "open_browser": True,
        }
        assert _post(port, "/api/launcher-settings", saved, None)[0] == 403
        assert not path.exists()
        status, raw, _ = _post(port, "/api/launcher-settings", saved, config["csrf_token"])
        assert status == 200
        assert json.loads(raw) == {"settings": saved, "settings_path": str(path)}
        assert not (project / ".git" / "vkit" / "config.json").exists()
        assert _post(port, "/api/launcher-settings", {**saved, "port": -1}, config["csrf_token"])[0] == 400
        assert json.loads(path.read_text(encoding="utf-8")) == saved
    with _serve(project) as port:
        assert _config(port)["launcher_settings"] == saved
    edited = {**saved, "project": None, "port": 0}
    path.write_text(json.dumps(edited), encoding="utf-8")
    with _serve(project) as port:
        assert _config(port)["launcher_settings"] == edited
    assert subprocess.run(["git", "-C", str(project), "status", "--short"],
                          capture_output=True, text=True, check=True, **NO_WINDOW).stdout == ""


def test_repository_category_settings_filter_calls_and_persist_mcp_configuration(make_project):
    project = make_project({"README.md": "category controls\n", "ok.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])],
                           accept=False)
    with _serve(project) as port:
        config = _config(port)
        saved = {**config["settings"], "disabled_categories": ["repository"],
                 "disabled_tools": ["check_run"], "mcp_exposure": "named", "mcp_categories": []}
        assert _post(port, "/api/settings", saved, config["csrf_token"])[0] == 200
        updated = _config(port)
        assert updated["mcp_config"]["mcpServers"]["vkit"]["args"][-3:] == [
            "--exposure", "named", "--categories"]
        assert [category["id"] for category in updated["categories"]] == ["repository"]
        status, raw, _ = _post(port, "/api/call", {"name": "check_run", "arguments": {"needed": True}},
                               config["csrf_token"])
        assert status == 200 and json.loads(raw) == {
            "is_error": True, "result": {"error": "unknown or unavailable tool"}}
    with _serve(project) as port:
        config = _config(port)
        assert config["settings"] == saved
        enabled = {**saved, "disabled_categories": [], "disabled_tools": [], "mcp_categories": ["repository"]}
        assert _post(port, "/api/settings", enabled, config["csrf_token"])[0] == 200
        config = _config(port)
        names = {tool["name"] for tool in config["managed_tools"]}
        assert "check_run" in names and "run_wait" in names
        assert config["mcp_config"]["mcpServers"]["vkit"]["args"][-2:] == ["--categories", "repository"]
        status, raw, _ = _post(port, "/api/call", {"name": "check_run", "arguments": {"needed": True}},
                               config["csrf_token"])
        assert status == 200 and not json.loads(raw)["is_error"]


def test_agent_configuration_and_settings_preserve_git_metadata(make_project):
    project = make_project({"README.md": "connection\n"}, [], accept=False)
    subprocess.run(["git", "-C", str(project), "remote", "add", "origin", "https://example.com/project.git"],
                   check=True, **NO_WINDOW)
    metadata = {name: (project / ".git" / name).read_bytes() for name in ("config", "HEAD", "index")}
    with _serve(project) as port:
        config = _config(port)
        assert config["mcp_config"] == {"mcpServers": {"vkit": {
            "command": sys.executable,
            "args": ["-m", "vkit.cli", "mcp", "serve", "--project", str(project),
                     "--exposure", "compact", "--categories", "all"],
        }}}
        saved = {**config["settings"], "theme": "dark"}
        assert _post(port, "/api/settings", saved, config["csrf_token"])[0] == 200
        assert _config(port)["settings"] == saved
        assert json.loads((project / ".git" / "vkit" / "config.json").read_text()) == saved
    assert {name: (project / ".git" / name).read_bytes() for name in metadata} == metadata
    assert subprocess.run(["git", "-C", str(project), "remote", "get-url", "origin"], check=True,
                          capture_output=True, text=True, **NO_WINDOW).stdout.strip() == "https://example.com/project.git"
    assert subprocess.run(["git", "-C", str(project), "status", "--short"], check=True,
                          capture_output=True, text=True, **NO_WINDOW).stdout == ""


def test_invalid_console_settings_are_reported_without_rewriting(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    settings_path = project / ".git" / "vkit" / "config.json"
    saved = {"version": 1, "theme": "light", "sidebar_collapsed": False,
             "refresh_seconds": 2, "history_limit": 50}
    with _serve(project) as port:
        config = _config(port)
        status, _, _ = _post(port, "/api/settings", saved, config["csrf_token"])
        assert status == 200
        original = settings_path.read_bytes()
        for invalid in ({**saved, "unknown": True}, {**saved, "refresh_seconds": 0}):
            status, _, _ = _post(port, "/api/settings", invalid, config["csrf_token"])
            assert status == 400
            assert settings_path.read_bytes() == original
        malformed = b"{not-json"
        settings_path.write_bytes(malformed)
        status, raw = _get(port, "/api/config")
        assert status == 500 and b"invalid settings" in raw
        assert settings_path.read_bytes() == malformed


def test_console_tool_settings_filter_durable_job_dispatch_after_restart(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    with _serve(project) as port:
        config = _config(port)
        assert "help" not in {tool["name"] for tool in config["managed_tools"]}
        managed = {tool["name"] for tool in config["managed_tools"]}
        assert {"check_run", "run_wait", "evidence_export", "evidence_replay"} <= managed
        saved = {**config["settings"], "theme": "dark", "disabled_tools": ["check_run", "run_cancel"]}
        status, raw, _ = _post(port, "/api/settings", saved, config["csrf_token"])
        assert status == 200 and json.loads(raw)["settings"] == saved
        for name, arguments in (("check_run", {"needed": True}), ("run_cancel", {"run_id": "missing"})):
            status, raw, _ = _post(port, "/api/call", {"name": name, "arguments": arguments},
                                   config["csrf_token"])
            assert status == 200 and json.loads(raw) == {
                "is_error": True, "result": {"error": "unknown or unavailable tool"}}
        invalid = {**saved, "disabled_tools": ["help"]}
        status, _, _ = _post(port, "/api/settings", invalid, config["csrf_token"])
        assert status == 400
    with _serve(project) as port:
        config = _config(port)
        assert config["settings"] == saved
        assert "run_wait" in {tool["name"] for tool in config["managed_tools"]}
        status, raw, _ = _post(port, "/api/settings", {**saved, "disabled_tools": []}, config["csrf_token"])
        assert status == 200
        status, raw, _ = _post(port, "/api/call", {
            "name": "check_run", "arguments": {"needed": True, "wait_seconds": 0},
        }, config["csrf_token"])
        response = json.loads(raw)
        assert status == 200 and not response["is_error"] and len(response["result"]["runs"]) == 1


def test_mutating_routes_reject_untrusted_or_incomplete_requests(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    settings_path = project / ".git" / "vkit" / "config.json"
    with _serve(project) as port:
        token = _config(port)["csrf_token"]
        valid = {"version": 1, "theme": "dark", "sidebar_collapsed": False,
                 "refresh_seconds": 2, "history_limit": 50}
        requests = [
            ({"origin": "http://evil.example"}, 403),
            ({"origin": None}, 403),
            ({"token": None}, 403),
            ({"host": "evil.example:80"}, 403),
            ({"content_type": None}, 415),
            ({"body": None}, 400),
            ({"body": "{"}, 400),
            ({"body": "", "declared_length": 1_048_577}, 413),
        ]
        for overrides, expected in requests:
            options = {"origin": "same", "content_type": "application/json", "host": None}
            options.update(overrides)
            body = options.pop("body", valid)
            status, _, _ = _post(port, "/api/settings", body, options.pop("token", token), **options)
            assert status == expected
            assert not settings_path.exists()


def test_console_lists_only_core_repository_tools(make_project):
    project = make_project({"ok.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    with _serve(project) as port:
        config = _config(port)
        names = {tool["name"] for tool in config["managed_tools"]}
        assert names == {"status", "features", "check_run", "run_get", "run_cancel", "run_wait",
                         "evidence_export", "evidence_replay", "propose", "gate"}
        assert "operations" not in config
        assert all(tool["category"] == "repository" for tool in config["managed_tools"])
        status, raw, _ = _post(port, "/api/call", {
            "name": "check_run",
            "arguments": {"check_ids": ["ok"], "wait_seconds": 10},
        }, config["csrf_token"])
        response = json.loads(raw)
        assert (status, response["is_error"], response["result"]["runs"][0]["outcome"]["result"]) == (
            200, False, "PASS")
        status, raw, _ = _post(port, "/api/call", {"name": "compare_matchsets", "arguments": {}},
                               config["csrf_token"])
        assert status == 400 and json.loads(raw)["error"] == "unknown console tool"


def test_console_check_run_does_not_accept_an_unapproved_check(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])],
                           accept=False)
    with _serve(project) as port:
        token = _config(port)["csrf_token"]
        status, raw, _ = _post(port, "/api/call", {
            "name": "check_run", "arguments": {"check_ids": ["ok"], "wait_seconds": 10},
        }, token)
        response = json.loads(raw)
        run = response["result"]["runs"][0]
        assert (status, response["is_error"], run["state"], run["outcome"]["result"],
                run["outcome"]["reason"]) == (200, False, "done", "BLOCKED", "not_approved")
        status, raw = _get(port, "/api/status")
        check = json.loads(raw)
        assert status == 200 and check["gate"]["verdict"] == "BLOCKED"
        assert check["checks"][0]["state"] == "not_approved"


def test_console_run_cancel_stops_a_running_check(make_project):
    slow = DRIVER + 'import time\ntime.sleep(30)\nreport([("ok", True, "finished")])\n'
    project = make_project({"slow.py": slow},
                           [scenario_check("slow", "slow.py", scenarios=["ok"], inputs=["slow.py"], timeout=60)])
    with _serve(project) as port:
        token = _config(port)["csrf_token"]
        status, raw, _ = _post(port, "/api/call", {
            "name": "check_run", "arguments": {"check_ids": ["slow"], "wait_seconds": 0},
        }, token)
        response = json.loads(raw)
        assert status == 200 and not response["is_error"]
        run_id = response["result"]["runs"][0]["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, raw = _get(port, "/api/status")
            check = json.loads(raw)["checks"][0]
            assert status == 200
            if check["running"]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the console run never appeared as running")
        status, raw, _ = _post(port, "/api/call", {
            "name": "run_cancel", "arguments": {"run_id": run_id},
        }, token)
        response = json.loads(raw)
        assert status == 200 and response == {"result": {"run_id": run_id, "cancelled": True}, "is_error": False}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, raw = _get(port, f"/api/run?id={run_id}")
            view = json.loads(raw)
            assert status == 200
            if view["state"] == "done":
                break
            time.sleep(0.05)
        assert (view["outcome"]["result"], view["outcome"]["reason"]) == ("BLOCKED", "cancelled")


def test_console_job_outlives_server_restart(make_project):
    slow = DRIVER + 'import time\ntime.sleep(0.5)\nreport([("ok", True, "finished")])\n'
    project = make_project({"slow.py": slow},
                           [scenario_check("slow", "slow.py", scenarios=["ok"], inputs=["slow.py"])])
    with _serve(project) as port:
        token = _config(port)["csrf_token"]
        status, raw, _ = _post(port, "/api/call", {
            "name": "check_run", "arguments": {"check_ids": ["slow"], "wait_seconds": 0},
        }, token)
        response = json.loads(raw)
        assert status == 200 and not response["is_error"]
        run_id = response["result"]["runs"][0]["run_id"]
    with _serve(project) as port:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, raw = _get(port, f"/api/run?id={run_id}")
            view = json.loads(raw)
            assert status == 200
            if view["state"] == "done":
                break
            time.sleep(0.05)
        assert (view["state"], view["outcome"]["result"]) == ("done", "PASS")

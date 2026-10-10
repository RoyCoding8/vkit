from __future__ import annotations

import http.client
import json
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from helpers import DRIVER, scenario_check, vkit
from vkit.console import bind
from vkit.paths import open_project

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def _get(port: int, path: str, host: str | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


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
                "refresh_seconds": 2, "history_limit": 50}
    saved = {"version": 1, "theme": "dark", "sidebar_collapsed": True,
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
                            text=True, check=True)
    assert status.stdout == ""


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
        assert status == 500 and b"invalid console settings" in raw
        assert settings_path.read_bytes() == malformed


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


def test_console_lists_only_catalog_operations_and_uses_the_shared_dispatcher(make_project):
    pytest.importorskip("greenery")
    project = make_project({"maths.py": "def f(x):\n    return x\n", "ok.py": PASSES},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["maths.py", "ok.py"])])
    with _serve(project) as port:
        config = _config(port)
        names = {operation["name"] for operation in config["operations"]}
        assert names == {"check_c_safety", "compare_iteration_sets", "minimize_cover",
                         "check_proto_compatibility", "check_history", "compare_matchsets",
                         "check_rewrite", "simplify_function", "reduce_failure"}
        assert not names & {"status", "features", "check_run", "run_get", "run_cancel", "gate", "propose"}
        assert all("input_schema" in operation for operation in config["operations"])
        status, raw, _ = _post(port, "/api/call", {
            "name": "compare_matchsets",
            "arguments": {"old_pattern": "a|bc", "new_pattern": "a|bd", "alphabet": "abcd"},
        }, config["csrf_token"])
        response = json.loads(raw)
        assert (status, response["is_error"], response["result"]["status"],
                response["result"]["old_only"], response["result"]["new_only"]) == (
                    200, False, "COUNTEREXAMPLE", "bc", "bd")
        status, raw, _ = _post(port, "/api/call", {"name": "propose", "arguments": {}}, config["csrf_token"])
        assert status == 400 and json.loads(raw)["error"] == "unknown console operation"


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


def test_overlapping_console_starts_share_a_run_before_its_record_exists(make_project, monkeypatch):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    entered = threading.Event()
    release = threading.Event()
    started: list[str] = []

    def hold_runner(project, manifest, check_id, *, store, run_id):
        started.append(check_id)
        entered.set()
        release.wait(timeout=10)

    monkeypatch.setattr("vkit.runner.run_check", hold_runner)
    with _serve(project) as port:
        token = _config(port)["csrf_token"]
        request = {"name": "check_run", "arguments": {"check_ids": ["ok"], "wait_seconds": 0}}
        first: list[tuple[int, bytes, dict[str, str]]] = []
        first_request = threading.Thread(target=lambda: first.append(
            _post(port, "/api/call", request, token)))
        first_request.start()
        try:
            assert entered.wait(timeout=5)
            second_status, second_raw, _ = _post(port, "/api/call", request, token)
            first_request.join(timeout=5)
            assert not first_request.is_alive()
            first_status, first_raw, _ = first[0]
            first_response = json.loads(first_raw)["result"]["runs"][0]
            second_response = json.loads(second_raw)["result"]["runs"][0]
            assert (first_status, second_status, first_response["run_id"] == second_response["run_id"],
                    started) == (200, 200, True, ["ok"])
        finally:
            release.set()
            first_request.join(timeout=5)

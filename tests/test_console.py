from __future__ import annotations

import http.client
import json
import threading

from helpers import DRIVER, scenario_check, vkit
from vkit.console import bind
from vkit.paths import open_project

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def _get(port: int, path: str, host: str | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = connection.getresponse()
    return response.status, response.read()


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
        status, raw = _get(port, "/api/status")
        report = json.loads(raw)
        assert (status, report["gate"]["verdict"], [c["state"] for c in report["checks"]]) == (200, "READY", ["fresh_pass"])
        status, raw = _get(port, f"/api/run?id={run_id}&offset=-200")
        assert status == 200 and "hello from the driver" in json.loads(raw)["log"]["text"]
        status, _ = _get(port, "/api/status", host="evil.example:80")
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()

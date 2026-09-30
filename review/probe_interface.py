"""Bounded, offline probes for console and Claude-hook interface boundaries.

Run with the workspace interpreter:
    python.exe review/probe_interface.py

All projects and stores are temporary. The only persistent output is the JSON
receipt beside this script. No host process or network service is contacted.
"""
from __future__ import annotations

import http.client
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.console import api  # noqa: E402
from vkit.console.api import MAX_BODY_BYTES  # noqa: E402
from vkit.console.operations import Context  # noqa: E402
from vkit.console.server import serve  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.tasks import open_task  # noqa: E402


def _hook_module():
    import importlib.util

    path = ROOT / "plugin" / "scripts" / "vkit_hook.py"
    spec = importlib.util.spec_from_file_location("vkit_audit_hook", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load hook at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _publish_pass(store: Store, run_id: str, task_id: str, check_id: str) -> None:
    store.register_run(
        run_id, check_id, task_id=task_id, attempt=1, source={},
        configuration_digest="probe", fixture_digest=None,
    )
    store.publish(run_id, {
        "run_id": run_id,
        "lifecycle": "terminal",
        "ended_at": "probe",
        "outcome": {
            "result": "PASS",
            "scenarios": [{"id": "scenario", "result": "PASS", "observation": "probe"}],
        },
    })


def oversized_post_probe() -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    original = api.dispatch
    api.dispatch = lambda _ctx, route, query: calls.append({"route": route, "query": query}) or {"ok": True}
    server = serve(Context(None, None, None, None), port=0)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        length = MAX_BODY_BYTES + 1
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.request(
            "POST", "/api/run_check?check_id=registered", body=b"x" * length,
            headers={"Content-Length": str(length)},
        )
        response = conn.getresponse()
        response_body = response.read().decode("utf-8")
        conn.sock.settimeout(1)
        trailing = bytearray()
        try:
            while chunk := conn.sock.recv(4096):
                trailing.extend(chunk)
        except TimeoutError:
            pass
        trailing_text = trailing.decode("iso-8859-1", errors="replace")
        conn.close()
        return {
            "body_bytes": length,
            "limit_bytes": MAX_BODY_BYTES,
            "http_status": response.status,
            "response_error": json.loads(response_body).get("error"),
            "route_dispatches": calls,
            "trailing_http_statuses": [
                line for line in trailing_text.splitlines()
                if line.startswith("HTTP/1.1 ")
            ],
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        api.dispatch = original


def hook_identity_probe(base: Path) -> dict[str, Any]:
    root = base / "identity-repo"
    _git_repo(root)
    store = Store(open_project(root).db_path)
    open_task(
        store, task_id="parent-task",
        contract={"required_checks": ["selected"], "host": {"session_id": "shared-session"}},
        policy_digest="probe",
    )
    hook = _hook_module()
    payload = {"session_id": "shared-session"}  # Identity-incomplete SubagentStop shape.
    before = hook._bound_task_id(store, payload)
    open_task(
        store, task_id="second-task",
        contract={"required_checks": ["other"], "host": {"session_id": "shared-session"}},
        policy_digest="probe",
    )
    after = hook._bound_task_id(store, payload)
    return {
        "event_shape": "SubagentStop with session_id and no agent_id",
        "task_before_second_binding": before,
        "task_with_two_active_bindings": after,
        "ambiguous_session_bindings": 2,
    }


def hook_required_floor_probe(base: Path) -> dict[str, Any]:
    root = base / "floor-repo"
    _git_repo(root)
    checks = []
    for check_id in ("selected", "mandatory"):
        checks.append({
            "id": check_id,
            "description": "probe",
            "command": ["python", "-c", "pass"],
            "cwd": ".",
            "timeout_seconds": 1,
            "required_scenarios": ["scenario"],
            "artifact": f"{check_id}.json",
            "prerequisites": [],
            "inputs": [],
        })
    verification = root / "verification"
    verification.mkdir()
    (verification / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "checks": checks}), encoding="utf-8"
    )

    store = Store(open_project(root).db_path)
    open_task(
        store, task_id="floor-task",
        contract={"required_checks": ["selected"], "host": {"session_id": "floor-session"}},
        policy_digest="probe",
    )
    _publish_pass(store, "selected-run", "floor-task", "selected")
    hook = _hook_module()
    response, exit_code = hook.respond(
        "Stop", {"session_id": "floor-session", "hook_event_name": "Stop"}, str(root)
    )
    return {
        "manifest_check_ids": ["selected", "mandatory"],
        "task_contract_required_checks": ["selected"],
        "missing_manifest_check_evidence": ["mandatory"],
        "hook_response": response,
        "hook_exit_code": exit_code,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vkit-interface-audit-") as tmp:
        base = Path(tmp)
        result = {
            "python": sys.version.split()[0],
            "probes": {
                "oversized_post": oversized_post_probe(),
                "hook_identity": hook_identity_probe(base),
                "hook_required_floor": hook_required_floor_probe(base),
            },
        }
    output = Path(__file__).with_name("probe-interface-results.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"receipt: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

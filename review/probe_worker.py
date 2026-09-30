"""Independent, bounded probes of the worker's public application interface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for key in tuple(os.environ):
    if key.startswith("GIT_"):
        os.environ.pop(key)
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]

from vkit.mcp import Server
from vkit.tasks import supersede_task
from vkit.console import operations
from vkit.console.server import start_in_thread
from vkit.enroll import read_enrollment

scratch = Path(tempfile.mkdtemp(prefix="vkit-audit-"))
observations = {"scratch": str(scratch), "tests": {}}


def repo(name: str, example: bool = True) -> Path:
    target = scratch / name
    if example:
        shutil.copytree(ROOT / "examples/python-cli", target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        target.mkdir()
        (target / "source.txt").write_text("initial", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=audit@example.invalid", "-c", "user.name=Audit",
                  "commit", "-qm", "audit fixture"]):
        subprocess.run(["git", *args], cwd=target, check=True, capture_output=True)
    return target


def begin(server: Server, request: str, **extra) -> dict:
    """`task_begin` as the repaired interface accepts it.

    The caller-supplied `policy_digest` and `checkout_ref` were the F04
    counterexample: a task bound to `a-nonexistent-ref` that then compared its
    evidence against `caller-supplied-policy`. `additionalProperties: False`
    now refuses them, so the schema still refuses them here and the refusal is
    recorded as part of what this probe observes.
    """
    return server.call_tool("task_begin", {
        "contract": {"scope": "totals"}, "owner": "auditor",
        "request_id": request, **extra,
    }).content


def task_id_of(opened: dict) -> str:
    """The admitted task's id, or the refusal that must stop the caller.

    A refused admission has no task to run under, so a probe that continued past
    one would report a downstream symptom instead of the refusal.
    """
    if not isinstance(opened.get("task_id"), str) or not opened["task_id"]:
        raise AssertionError(f"task_begin was refused, so there is no task to act on: {opened}")
    return opened["task_id"]


def run(server: Server, task_id: str, request: str) -> dict:
    return server.call_tool("check_start", {
        "task_id": task_id, "check_ids": ["totals-behavior"], "request_id": request,
    }).content


def finalize(server: Server, task_id: str) -> dict:
    return server.call_tool("task_finalize", {"task_id": task_id}).content


root = repo("stale-source")
server = Server(root)
refused = server.call_tool("task_begin", {
    "contract": {"scope": "totals"}, "policy_digest": "caller-supplied-policy",
    "checkout_ref": "a-nonexistent-ref", "owner": "auditor", "request_id": "untrusted-bindings",
}).content
task = task_id_of(begin(server, "source-task"))
check = run(server, task, "source-check")
before = finalize(server, task)
source = root / "src/totals.py"
source.write_text(source.read_text(encoding="utf-8").replace("running += amount", "running += amount + 100"), encoding="utf-8")
after = finalize(server, task)
observations["tests"]["caller_supplied_bindings_are_refused"] = {
    "begin": refused,
}
observations["tests"]["source_changed_after_pass"] = {
    "run": check, "before": before, "after": after,
    "defect_was_inserted": "running += amount + 100" in source.read_text(encoding="utf-8"),
}

root = repo("unenrolled", example=False)
server = Server(root)
opened = begin(server, "empty-task")
observations["tests"]["unenrolled_zero_checks"] = {
    "begin": opened,
    "manifest_exists": server.project.manifest_path.exists(),
}

root = repo("ownership")
server = Server(root)
a = begin(server, "owner-a", claim_resource="exclusive-checkout")
b = begin(server, "owner-b", claim_resource="exclusive-checkout")
second_run = ({} if b.get("admitted") is False
              else run(server, task_id_of(b), "conflicting-run"))
observations["tests"]["claim_conflict_still_executes"] = {
    "first": a, "second": b, "second_run": second_run,
}

root = repo("retry")
server = Server(root)
task = task_id_of(begin(server, "retry-task"))
first = run(server, task, "retry-first")
supersede_task(server._store(), task)
second = run(server, task, "retry-second")
observations["tests"]["fresh_attempt_after_old_pass"] = {
    "first": first, "second": second, "finalize": finalize(server, task),
}

root = repo("blocking-start")
driver = root / "verify_totals.py"
driver.write_text(driver.read_text(encoding="utf-8").replace(
    "from __future__ import annotations", "from __future__ import annotations\nimport time\ntime.sleep(1.25)"
), encoding="utf-8")
server = Server(root)
task = task_id_of(begin(server, "slow-task"))
started = time.monotonic()
result = run(server, task, "slow-check")
observations["tests"]["check_start_is_synchronous"] = {
    "elapsed_seconds": time.monotonic() - started, "response": result,
}

context = operations.open_context(repo("http"))
http_server, thread = start_in_thread(context)
calls = []
try:
    def witness(ctx, check_id):
        calls.append(check_id)
        return {"witness": "mutation route invoked", "check_id": check_id}
    with patch.object(operations, "run_check", witness):
        url = f"http://127.0.0.1:{http_server.server_port}/api/run_check?check_id=totals-behavior"
        request = Request(url, headers={"Host": "untrusted.example", "Origin": "https://untrusted.example"})
        with urlopen(request, timeout=5) as response:
            observations["tests"]["foreign_origin_get_mutation"] = {
                "http_status": response.status, "body": json.load(response), "calls": calls,
            }
finally:
    http_server.shutdown()
    http_server.server_close()
    thread.join(timeout=5)

proposed = operations.enroll(context, accepted=False)
try:
    receipt = operations.enroll(context, accepted=True)
except Exception as exc:
    receipt = {"enrolled": None, "error": f"{type(exc).__name__}: {exc}"}
observations["tests"]["console_and_core_enrollment_disagree"] = {
    "console_proposal": proposed,
    "console_receipt": receipt,
    "core_read": read_enrollment(context.project).to_json(),
}

destination = ROOT / "review/probe-results.json"
destination.write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8")
for name, result in observations["tests"].items():
    if name == "source_changed_after_pass":
        print(name, result["before"].get("readiness"), "->", result["after"].get("readiness"))
    elif name == "caller_supplied_bindings_are_refused":
        print(name, result["begin"].get("error"))
    elif "finalize" in result:
        print(name, result["finalize"].get("readiness"), result["finalize"].get("gaps"))
    elif "manifest_exists" in result:
        print(name, "admitted=", result["begin"].get("admitted"), result["begin"].get("error"))
    elif name == "claim_conflict_still_executes":
        print(name, "admitted=", result["second"].get("admitted"),
              "conflict=", bool(result["second"].get("claim_conflict")),
              "ran=", bool(result["second_run"].get("runs")))
    elif name == "check_start_is_synchronous":
        print(name, round(result["elapsed_seconds"], 2), result["response"]["runs"][0]["result"])
    elif name == "console_and_core_enrollment_disagree":
        print(name, "console=", result["console_receipt"].get("enrolled"),
              "core=", result["core_read"]["state"])
    else:
        print(name, result)
print("Evidence:", destination)
print("Disposable repositories retained:", scratch)

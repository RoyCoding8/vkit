"""Reproduce claim release while a real example check is still running.

Run from the repository root with `.venv/Scripts/python.exe review/probe_recovery.py`.
The probe uses a temporary Git repo and never signals or kills its driver process.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.claims import ResourceSpec, acquire, holders
from vkit.execution import run_check
from vkit.identity import compute_source_identity
from vkit.manifest import parse_manifest
from vkit.paths import open_project
from vkit.procidentity import is_alive, read_identity
from vkit.recover import Action, apply_action, inspect
from vkit.storage import Store
from vkit.tasks import open_task, supersede_task


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _instrument_driver(project: Path, marker: Path) -> None:
    driver = project / "verify_totals.py"
    source = driver.read_text(encoding="utf-8")
    anchor = "    scenarios = [run_case(args.python, args.app, case) for case in CASES]\n"
    assert source.count(anchor) == 1, "expected one example-driver execution point"
    literal = repr(str(marker))
    injection = (
        f"    Path({literal}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "    time.sleep(5)\n"
        + anchor
    )
    source = source.replace("import sys\n", "import sys\nimport os\nimport time\n", 1)
    source = source.replace(anchor, injection, 1)
    driver.write_text(source, encoding="utf-8")


def _wait_for_marker(marker: Path, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            return int(marker.read_text(encoding="utf-8"))
        time.sleep(0.02)
    raise TimeoutError(f"driver did not create its marker within {timeout}s")


def main() -> int:
    result: dict = {
        "probe": "recovery releases a claim while the real example driver is alive",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "workspace_head": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            check=True, capture_output=True, text=True,
        ).stdout.strip(),
        "driver_sleep_seconds": 5,
    }
    thread: threading.Thread | None = None
    outcome: dict = {}

    with tempfile.TemporaryDirectory(prefix="vkit-live-recovery-") as raw_temp:
        temp = Path(raw_temp)
        project_root = temp / "project"
        shutil.copytree(ROOT / "examples" / "python-cli", project_root)
        marker = temp / "driver-entered.txt"
        _instrument_driver(project_root, marker)

        manifest_path = project_root / "verification" / "manifest.json"
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_doc["checks"][0]["command"][0] = "{{python}}"
        manifest_doc["checks"][0]["timeout_seconds"] = 20
        manifest_doc["checks"][0]["prerequisites"][0]["executable"] = sys.executable
        manifest_path.write_text(json.dumps(manifest_doc, indent=2) + "\n", encoding="utf-8")

        _git("init", "-q", cwd=project_root)
        _git("config", "user.email", "probe@example.invalid", cwd=project_root)
        _git("config", "user.name", "Recovery Probe", cwd=project_root)
        _git("add", "-A", cwd=project_root)
        _git("commit", "-q", "-m", "isolated recovery probe", cwd=project_root)

        project = open_project(project_root)
        store = Store(project.db_path)
        manifest = parse_manifest(project, project.runs_root / "probe-manifest")
        check_id = "totals-behavior"
        resource_key = "probe-exclusive-worktree"
        task_id = f"probe-{uuid.uuid4().hex}"
        second_task_id = f"probe-second-{uuid.uuid4().hex}"
        run_id = f"probe-run-{uuid.uuid4().hex}"
        contract = {"required_checks": [check_id]}
        first_task = open_task(store, task_id=task_id, contract=contract, policy_digest="probe")
        acquire(store, task_id, first_task.generation, [ResourceSpec(resource_key, "exclusive")])
        source = compute_source_identity(project)

        def execute() -> None:
            try:
                run = run_check(
                    manifest, check_id, store=store, source=source, run_id=run_id,
                    task_id=task_id, attempt=first_task.generation,
                )
                outcome["result"] = run.outcome.to_json()
            except BaseException as exc:
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=execute, name="isolated-vkit-run", daemon=False)
        thread.start()
        pid = _wait_for_marker(marker)
        identity = read_identity(pid)
        result["driver"] = {
            "pid": pid,
            "identity_read": identity is not None,
            "alive_before_recovery": bool(identity and is_alive(identity)),
        }
        result["run_record_before_recovery"] = {
            "process_identity": store.run_process_identity(run_id),
            "finding_kinds": [f.kind.value for f in inspect(store).findings],
        }

        superseded = supersede_task(store, task_id)
        result["superseded_generation"] = superseded.generation
        try:
            after_release = apply_action(
                store,
                Action.RELEASE_CLAIM,
                target=resource_key,
                evidence="isolated probe observed the run's driver marker and live process identity",
            )
            result["release_claim"] = {
                "succeeded": True,
                "remaining_claims": [c.__dict__ for c in holders(store)],
                "finding_kinds_after": [f.kind.value for f in after_release.findings],
            }
        except Exception as exc:
            result["release_claim"] = {
                "succeeded": False,
                "refusal": f"{type(exc).__name__}: {exc}",
                "remaining_claims": [c.__dict__ for c in holders(store)],
            }

        second = open_task(
            store, task_id=second_task_id, contract=contract, policy_digest="probe"
        )
        try:
            acquire(
                store, second_task_id, second.generation,
                [ResourceSpec(resource_key, "exclusive")],
            )
            result["second_task_claim"] = {"acquired": True}
        except Exception as exc:
            result["second_task_claim"] = {
                "acquired": False,
                "refusal": f"{type(exc).__name__}: {exc}",
            }

        current_identity = read_identity(pid)
        result["driver"]["alive_after_release_and_second_claim"] = bool(
            current_identity and is_alive(current_identity)
        )
        result["driver"]["marker_present"] = marker.is_file()
        # The child has a finite five-second delay and the manifest has a finite
        # timeout. Let it finish naturally; never terminate an unverified PID.
        thread.join(30)
        result["worker_finished_naturally"] = not thread.is_alive()
        result["worker_outcome"] = outcome

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    output = ROOT / "review" / "probe_recovery_result.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result.get("worker_finished_naturally") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""What does cancel_run actually do on POSIX when the recorded pid is a stranger?

Row 7 of acceptance02 records a live but unrelated pid with a creation time that
does not belong to it, then cancels three times from one process and requires
every cancel to return BLOCKED/ownership_lost and the bystander to survive.
On this host the row produces no output at all and its process is killed, which
is a fact about the run rather than a verdict, so this drives the same calls
directly and prints what each one returns or raises.

Run:  bash scripts/posix-run.sh scripts/diagnose_cancel_posix.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.paths import open_project  # noqa: E402
from vkit.procidentity import ProcessIdentity  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.supervisor import cancel_run, job_name_for  # noqa: E402


def main() -> int:
    if sys.platform == "win32":
        print("this diagnoses the POSIX cancel path; run it under WSL")
        return 2

    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp()) / "cancel-repeat"
    shutil.copytree(ROOT / "examples" / "python-cli", base)
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"]):
        subprocess.run(args, cwd=base, check=True)

    project = open_project(base)
    store = Store(project.db_path)
    run_id = uuid.uuid4().hex

    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    print(f"bystander pid: {bystander.pid}")
    print()

    try:
        store.register_run(run_id, "totals-behavior", task_id=None, attempt=None,
                           source={"head": "x", "inventory_digest": "y", "dirty": False},
                           configuration_digest="d", fixture_digest=None)
        store.mark_running(run_id, {
            "pid": bystander.pid,
            "ownership": "posix_process_group",
            "exit_code": None, "timed_out": False, "creation_time": 0,
            "job_name": job_name_for(run_id), "check_id": "totals-behavior",
            "command": {"argv": ["python", "x.py"], "cwd": str(base)},
        }, command={"argv": ["python", "x.py"], "cwd": str(base)})
        print("run registered with a stranger's pid and creation_time=0")
        print()

        print("== three cancels with a ProcessIdentity that cannot match ==")
        for attempt in range(3):
            try:
                outcome, _report = cancel_run(
                    store, run_id, identity=ProcessIdentity(999999, 12345)
                )
                print(f"  cancel {attempt}: {json.dumps(outcome.to_json())}")
            except BaseException as exc:
                print(f"  cancel {attempt}: RAISED {type(exc).__name__}: {exc}")
                traceback.print_exc()
        print()

        # The same call, but with the identity naming the bystander, so the
        # ownership gate passes and the termination path is actually reached.
        print("== one cancel whose identity MATCHES the bystander ==")
        try:
            outcome, _report = cancel_run(
                store, run_id, identity=ProcessIdentity(bystander.pid, 0)
            )
            print(f"  cancel: {json.dumps(outcome.to_json())}")
        except BaseException as exc:
            print(f"  cancel: RAISED {type(exc).__name__}: {exc}")
            traceback.print_exc()
        print()

        try:
            alive = bystander.poll() is None
        except Exception:
            alive = False
        print(f"bystander still running: {alive}")
    finally:
        if bystander.poll() is None:
            bystander.kill()
        try:
            bystander.wait(timeout=30)
        except Exception:
            pass
        shutil.rmtree(base.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

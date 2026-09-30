"""Prove the product's own cancel paths do not raise on POSIX.

The triage found that three call sites reached `vkit.procidentity` with no
platform guard. `read_identity` raised `UnsupportedPlatform` off Windows, so a
POSIX cancel raised rather than returning an outcome, and the console mapped an
unrecognised exception to a bare HTTP 500. That was true when the module had no
POSIX identity; it now has one, so the question is whether the callers behave.

This drives the real public functions against a real live process and prints
what each returns or raises. A raise here is a bug, not a finding about a
platform.

Run:  bash scripts/posix-run.sh scripts/verify_posix_cancel.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit import claims  # noqa: E402
from vkit.claims import ResourceSpec  # noqa: E402
from vkit.console import operations  # noqa: E402
from vkit.console.operations import Refused  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.procidentity import ProcessIdentity, read_identity  # noqa: E402
from vkit.recover import liveness  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.supervisor import cancel_run  # noqa: E402

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    if not condition:
        ok = False
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def make_repo(name: str) -> Path:
    base = Path(tempfile.mkdtemp()) / name
    shutil.copytree(ROOT / "examples" / "python-cli", base)
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"]):
        subprocess.run(args, cwd=base, check=True)
    return base


def main() -> int:
    if sys.platform == "win32":
        print("this verifies the POSIX cancel paths; run it under WSL")
        return 2

    print("== liveness now honours creation_time on POSIX ==")
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    time.sleep(0.4)
    try:
        bare = liveness(live.pid)
        real = read_identity(live.pid)
        assert real is not None
        with_identity = liveness(live.pid, creation_time=real.creation_time)
        stranger = liveness(live.pid, creation_time=real.creation_time + 1)
        check("a bare probe says ALIVE", bare.state.value == "alive", bare.detail)
        check("a matching pair says ALIVE", with_identity.state.value == "alive", with_identity.detail)
        check(
            "a mismatched pair says UNCERTAIN, not alive and not dead",
            stranger.state.value == "uncertain",
            stranger.detail,
        )
        check(
            "the detail differs, so the check is not inert",
            bare.detail != stranger.detail,
        )
    finally:
        live.kill()
        live.wait()

    print()
    print("== supervisor.cancel_run returns an outcome instead of raising ==")
    repo = make_repo("cancel-posix")
    project = open_project(repo)
    store = Store(project.db_path)
    run_id = uuid.uuid4().hex
    target = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)
    try:
        identity = read_identity(target.pid)
        check("the live process has an identity", identity is not None)
        store.register_run(run_id, "totals-behavior", task_id=None, attempt=None,
                           source={"head": "x", "inventory_digest": "y", "dirty": False},
                           configuration_digest="d", fixture_digest=None)
        store.mark_running(run_id, {
            "pid": target.pid,
            "creation_time": identity.creation_time if identity else None,
            **({"boot_id": identity.boot_id} if identity else {}),
            "ownership": "posix_process_group",
            "exit_code": None, "timed_out": False, "check_id": "totals-behavior",
            "command": {"argv": ["python", "x.py"], "cwd": str(repo)},
        }, command={"argv": ["python", "x.py"], "cwd": str(repo)})

        # A stranger's identity: the pid is live but the pair does not match.
        stranger_identity = ProcessIdentity(
            target.pid, (identity.creation_time if identity else 0) + 1,
            identity.boot_id if identity else "",
        )
        try:
            outcome, _report = cancel_run(store, run_id, identity=stranger_identity)
            body = outcome.to_json()
            check("cancel_run returned rather than raising", True)
            check(
                "a stranger is refused as ownership_lost",
                body.get("reason") == "ownership_lost",
                json.dumps(body)[:160],
            )
        except BaseException as exc:
            check("cancel_run returned rather than raising", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

        check("the stranger is still running", target.poll() is None)
    finally:
        if target.poll() is None:
            target.kill()
        target.wait(timeout=30)

    print()
    print("== the console cancel path returns a Refused, not an exception ==")
    from vkit.console.api import error_of  # noqa: E402

    class Ctx:
        def __init__(self, project, store):
            self.project = project
            self.store = store

    ctx = Ctx(project, store)
    other = uuid.uuid4().hex
    store.register_run(other, "totals-behavior", task_id=None, attempt=None,
                       source={"head": "x", "inventory_digest": "y", "dirty": False},
                       configuration_digest="d", fixture_digest=None)
    store.mark_running(other, {
        "pid": 999999, "creation_time": 12345, "ownership": "posix_process_group",
        "exit_code": None, "timed_out": False, "check_id": "totals-behavior",
        "command": {"argv": ["python", "x.py"], "cwd": str(repo)},
    }, command={"argv": ["python", "x.py"], "cwd": str(repo)})
    try:
        result = operations.cancel_check_run(ctx, other)
        check("the console cancel returned a document", isinstance(result, dict))
        check(
            "it is a refusal, not a success",
            result.get("outcome", {}).get("result") == "BLOCKED",
            json.dumps(result.get("outcome", {}))[:160],
        )
    except Refused as exc:
        check("the console cancel refused in the documented way", True, str(exc)[:120])
    except BaseException as exc:
        status = error_of(exc)
        check("the console cancel did not raise", False,
              f"{type(exc).__name__}: {exc} (api.error_of -> {status})")
        traceback.print_exc()

    shutil.rmtree(repo.parent, ignore_errors=True)
    print()
    print("VERDICT: " + ("every check passed" if ok else "at least one check FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

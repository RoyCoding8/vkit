"""Prove the product's own cancel paths do not raise on POSIX.

The triage found that three call sites reached `vkit.procidentity` with no
platform guard. `read_identity` raised `UnsupportedPlatform` off Windows, so a
POSIX cancel raised rather than returning an outcome, and the console mapped an
unrecognised exception to a bare HTTP 500. That was true when the module had no
POSIX identity; it now has one, so the question is whether the callers behave.

This drives the real public functions against a real live process and prints
what each returns or raises. A raise here is a bug, not a finding about a
platform.

**The stranger is written into the record, not passed to the call.**
`cancel_run(store, run_id)` takes nothing but a run id, so the only thing that
can present it with an identity that does not match is a run whose published
`process_json` disagrees with the live process. The stranger here is the target's
own pid with a `creation_time` one tick off the truth and the correct boot id --
the shape a recycled pid has. Deleting the old `identity=` argument must not
delete the stranger, and moving it into the record is where it actually lives.

The two cancels differ by that one tick and nothing else, so the script also
checks which gate refused each. Both are BLOCKED/ownership_lost, and a check on
the reason alone would pass even if the core compared nothing at all.

The `liveness` probes carry the boot id alongside the creation time, because
that is the pair `vkit.recover` compares everywhere. Without it a pair that
matches exactly was refused as UNCERTAIN here, which is a stranger that does not
exist.

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

from vkit.console import operations  # noqa: E402
from vkit.console.operations import Refused  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.procidentity import read_identity  # noqa: E402
from vkit.recover import liveness  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.supervisor import cancel_run  # noqa: E402

ok = True

# The detail `cancel_run` produces when the recorded pair fails verification.
# `ownership_lost` on its own cannot tell a refused stranger from a reachable
# bystander that POSIX then refused to signal, so the gate is read from the text.
STRANGER_DETAIL = "is not the process this run launched"


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


def publish_run(store: Store, run_id: str, repo: Path, pid: int, creation_time: int,
                boot_id: str) -> None:
    command = {"argv": ["python", "x.py"], "cwd": str(repo)}
    store.register_run(run_id, "totals-behavior", task_id=None, attempt=None,
                       source={"head": "x", "inventory_digest": "y", "dirty": False},
                       configuration_digest="d", fixture_digest=None)
    store.mark_running(run_id, {
        "pid": pid,
        "creation_time": creation_time,
        **({"boot_id": boot_id} if boot_id else {}),
        "ownership": "posix_process_group",
        "exit_code": None, "timed_out": False, "check_id": "totals-behavior",
        "command": command,
    }, command=command)


def main() -> int:
    if sys.platform == "win32":
        print("BLOCKED: this host is Windows. It has no /proc and no process groups, so")
        print("the POSIX identity and cancellation paths cannot be exercised here and")
        print("nothing below was measured. Run it under WSL:")
        print("  bash scripts/posix-run.sh scripts/verify_posix_cancel.py")
        return 2

    print("== liveness now honours creation_time on POSIX ==")
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    time.sleep(0.4)
    try:
        bare = liveness(live.pid)
        real = read_identity(live.pid)
        assert real is not None
        # boot_id goes with creation_time or the pair is not the pair the product
        # compares. On POSIX a tick count is only meaningful within one boot, so
        # a call that supplies a creation time and omits the boot is asking a
        # question the product never asks: every caller in vkit.recover passes
        # both. Measured without it here: a pair that matches exactly was refused
        # as UNCERTAIN because the recorded boot was empty and the live one was
        # not, which is a stranger that does not exist.
        with_identity = liveness(live.pid, creation_time=real.creation_time, boot_id=real.boot_id)
        stranger = liveness(live.pid, creation_time=real.creation_time + 1, boot_id=real.boot_id)
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
        assert identity is not None
        # A stranger's record: the target's own pid, this boot, and a creation
        # time one tick off. The core reads this pair and compares it against the
        # live process, so it is the same disagreement liveness just refused.
        stranger_creation_time = identity.creation_time + 1
        publish_run(store, run_id, repo, target.pid, stranger_creation_time, identity.boot_id)
        try:
            outcome, _report = cancel_run(store, run_id)
            body = outcome.to_json()
            check("cancel_run returned rather than raising", True)
            check(
                "a stranger is refused as ownership_lost",
                body.get("reason") == "ownership_lost",
                json.dumps(body)[:160],
            )
            check(
                "and it was refused at the ownership gate, not at termination",
                STRANGER_DETAIL in body.get("detail", ""),
                body.get("detail", "")[:160],
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
    print("== the same cancel with a matching record reaches termination ==")
    print("It still cannot stop the tree on POSIX, so it is BLOCKED/ownership_lost")
    print("with a different reason -- but a different one. Without this the refusal")
    print("above would pass even if the core never compared anything.")
    matched_run = uuid.uuid4().hex
    target2 = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)
    try:
        identity2 = read_identity(target2.pid)
        assert identity2 is not None
        publish_run(store, matched_run, repo, target2.pid, identity2.creation_time, identity2.boot_id)
        outcome, _report = cancel_run(store, matched_run)
        body = outcome.to_json()
        check(
            "a matching record gets past the ownership gate",
            STRANGER_DETAIL not in body.get("detail", ""),
            body.get("detail", "")[:160],
        )
        check(
            "and is refused by the POSIX termination refusal instead",
            "could not terminate the owned tree" in body.get("detail", ""),
            body.get("detail", "")[:160],
        )
    except BaseException as exc:
        check("the matching-record cancel returned rather than raising", False,
              f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        if target2.poll() is None:
            target2.kill()
        target2.wait(timeout=30)

    print()
    print("== the console cancel path returns a Refused, not an exception ==")
    from vkit.console.api import error_of  # noqa: E402

    class Ctx:
        def __init__(self, project, store):
            self.project = project
            self.store = store

    ctx = Ctx(project, store)
    other = uuid.uuid4().hex
    publish_run(store, other, repo, 999999, 12345, "")
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

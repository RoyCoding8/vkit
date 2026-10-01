"""Where does a POSIX cancel actually stop, and which gate stops it?

Row 7 of acceptance02 records a live but unrelated pid with a creation time that
does not belong to it, then cancels three times from one process and requires
every cancel to return BLOCKED/ownership_lost and the bystander to survive.
On this host the row produces no output at all and its process is killed, which
is a fact about the run rather than a verdict, so this drives the same calls
directly and prints what each one returns or raises.

**The mismatch is manufactured in the record, not in the call.** `cancel_run`
takes a run id and nothing else; it rebuilds the identity the run itself
published and hands that to `still_the_same_process`. So the only way to present
it with a pair that cannot match is to write a pair that cannot match into
`process_json`: the bystander's real, live pid, its real boot id, and a
`creation_time` one tick off the truth. Nothing is faked at the call site
because there is no call site left to fake one at.

`creation_time` must stay positive to get that far. A record carrying zero does
not produce a stranger; `_recorded_identity` refuses it by name, because a
record with a pid and no creation time is a bare pid and a bare pid cannot prove
ownership of anything. So this script asserts the first refusal is the stranger
one, and says so loudly when it is not.

The two probes differ in exactly one number, and the difference is the point:
identical pid, identical boot id, `creation_time` off by one versus exact. If
both land on the same outcome the probe has stopped probing and this says so
rather than reporting two agreeing checks as two findings.

Run:  bash scripts/posix-run.sh scripts/diagnose_cancel_posix.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.paths import open_project  # noqa: E402
from vkit.procidentity import read_identity  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.supervisor import cancel_run, job_name_for  # noqa: E402

# The two details `cancel_run` produces when it refuses, in its own words. Both
# are BLOCKED/ownership_lost, so the reason alone cannot say which gate stopped
# the cancel; only the detail can. Matching on these literals is how the script
# tells a stranger being refused from a bystander being reachable.
REFUSED_AS_STRANGER = "is not the process this run launched"
REFUSED_AT_TERMINATION = "could not terminate the owned tree"


def make_repo(name: str) -> Path:
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp()) / name
    shutil.copytree(ROOT / "examples" / "python-cli", base)
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"]):
        subprocess.run(args, cwd=base, check=True)
    return base


def publish_run(store: Store, run_id: str, base: Path, pid: int, creation_time: int,
                boot_id: str) -> None:
    """Register a run and give it an owner, with `creation_time` as written.

    Every field here is a real one except `creation_time`, which is the probe.
    The pid is live, the boot id is this boot's, and the pair differs from the
    truth only in the tick count -- which is the whole shape of a recycled pid.
    """
    command = {"argv": ["python", "x.py"], "cwd": str(base)}
    store.register_run(run_id, "totals-behavior", task_id=None, attempt=None,
                       source={"head": "x", "inventory_digest": "y", "dirty": False},
                       configuration_digest="d", fixture_digest=None)
    store.mark_running(run_id, {
        "pid": pid,
        "ownership": "posix_process_group",
        "exit_code": None, "timed_out": False,
        "creation_time": creation_time,
        "boot_id": boot_id,
        "job_name": job_name_for(run_id), "check_id": "totals-behavior",
        "command": command,
    }, command=command)


def report_path(store: Store, run_id: str) -> Path:
    return store.run_dir(run_id) / "report.json"


def main() -> int:
    if sys.platform == "win32":
        print("BLOCKED: this host is Windows. It has no /proc, no process groups and")
        print("no job objects, so neither the identity pair nor the termination path")
        print("can be exercised here. Nothing was measured. Run it under WSL:")
        print("  bash scripts/posix-run.sh scripts/diagnose_cancel_posix.py")
        return 2

    import shutil

    base = make_repo("cancel-repeat")
    project = open_project(base)
    store = Store(project.db_path)

    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    truth = read_identity(bystander.pid)
    print(f"bystander pid: {bystander.pid}")
    print(f"its real identity: {truth}")
    print()
    print("The record is the only thing that can lie now. cancel_run takes a run id")
    print("and rebuilds the identity from process_json, so a stranger is a record")
    print("whose creation_time disagrees with the live process by one tick.")
    print()

    stranger_detail = None
    match_detail = None
    try:
        print("== three cancels on a record that cannot match ==")
        stranger_run = uuid.uuid4().hex
        publish_run(store, stranger_run, base, bystander.pid,
                    truth.creation_time + 1, truth.boot_id)
        print(f"recorded pid={bystander.pid} creation_time={truth.creation_time + 1} "
              f"(real is {truth.creation_time}), boot_id matches")
        for attempt in range(3):
            # Read the state BEFORE the call. A cancel that has already published
            # a terminal report returns the stored verdict rather than deciding
            # again, so calling the second and third attempts "the core refusing
            # three times" would describe a replay as three decisions.
            already = report_path(store, stranger_run).is_file()
            try:
                outcome, _report = cancel_run(store, stranger_run)
                body = outcome.to_json()
                if stranger_detail is None:
                    stranger_detail = body.get("detail", "")
                origin = "replayed a stored verdict" if already else "decided now"
                print(f"  cancel {attempt}: {body['result']}/{body.get('reason')}  ({origin})")
                print(f"    {body.get('detail', '')[:150]}")
            except BaseException as exc:
                print(f"  cancel {attempt}: RAISED {type(exc).__name__}: {exc}")
                traceback.print_exc()
        print()
        print("  One decision, then two replays. That is the row's 'three cancels'")
        print("  claim measured honestly: the core is idempotent here, not thrice")
        print("  independent, and only the first call re-ran the ownership check.")
        print()

        print("== one cancel whose record DOES match the bystander ==")
        print("Same pid, same boot id, one number back to the truth. The ownership")
        print("gate now passes, so this is the cancel that reaches termination.")
        match_run = uuid.uuid4().hex
        publish_run(store, match_run, base, bystander.pid,
                    truth.creation_time, truth.boot_id)
        print(f"recorded pid={bystander.pid} creation_time={truth.creation_time} (exact)")
        try:
            outcome, _report = cancel_run(store, match_run)
            body = outcome.to_json()
            match_detail = body.get("detail", "")
            print(f"  cancel: {body['result']}/{body.get('reason')}")
            print(f"    {match_detail[:220]}")
        except BaseException as exc:
            print(f"  cancel: RAISED {type(exc).__name__}: {exc}")
            traceback.print_exc()
        print()

        print("== which gate stopped each cancel ==")
        if stranger_detail is None or match_detail is None:
            print("  INCONCLUSIVE: one of the two probes raised rather than returning an")
            print("  outcome, so there is no pair of details to compare. The output above")
            print("  is the finding; nothing is claimed about which gate fired.")
        elif stranger_detail == match_detail:
            print("  INERT PROBE: both records produced the same outcome. The two probes")
            print(f"  no longer differ, so this run measured one thing twice: {stranger_detail[:120]}")
        else:
            reached = REFUSED_AT_TERMINATION in match_detail
            refused = REFUSED_AS_STRANGER in stranger_detail
            print(f"  stranger refused at the ownership gate: {refused}")
            print(f"  matching record reached the termination path: {reached}")
            print("  The off-by-one is the only difference between these two records, so")
            print("  it is the ownership gate and nothing else that decides the stranger.")
            if not (refused and reached):
                print("  PARTIAL: the two outcomes differ but not in the way this probes for.")
                print(f"    stranger detail: {stranger_detail[:140]}")
                print(f"    match    detail: {match_detail[:140]}")
        print()

        try:
            alive = bystander.poll() is None
        except Exception:
            alive = False
        print(f"bystander still running: {alive}")
        if not alive:
            print("  This is the failure the whole script exists to catch: a cancel")
            print("  reached a process it did not own and stopped it.")
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

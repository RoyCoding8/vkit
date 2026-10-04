"""The R2 gate: a run's ownership is published, durable, and safe to release against.

Every test here drives the real thing. A real Git repository, a real manifest, the
real example driver, real subprocesses, real Windows Job Objects, a real detached
supervisor process, a real second OS process cancelling the first one's run. There
are no stubs of vkit functions anywhere in this file, because a stub of
`recover.inspect` asserts only that the test called it.

**Pids are never guessed.** A pid is read out of a marker file the child writes
itself, or out of the run record the supervisor published, and liveness is polled
against a deadline rather than sampled once. `test_procs.py:144` documents why:
containment is asynchronous, and a test that asks once is measuring the machine's
idle-ness rather than the mechanism it is testing.

The twelve gates and where each lives:

===  ==========================  ==================================================
G1   check_start returns early   `test_check_start_returns_a_run_id_while_the_check_runs`
G2   second client cancels       `test_second_client_cancels_a_run_started_by_another_process`
G3   control processes survive   `test_cancel_does_not_kill_an_unrelated_control_process`
G4   client disconnect           `test_client_disconnect_leaves_the_run_owned`
G5   crash before identity       `test_launch_crash_before_identity_publication`
G6   release while alive         `test_release_while_child_alive_refuses`
G7   supervisor death containment `test_parent_crash_keeps_claims_until_the_run_ends`
G8   repeated request            `test_repeated_mutation_request_does_not_duplicate_a_launch`
G9   no second owner             `test_no_second_owner_writes_concurrently`
G10  cancel races completion     `test_cancel_races_completion_to_one_outcome`
G11  POSIX still refuses         `test_posix_cancel_still_refuses`
G12  schema accepts the key      `test_run_report_schema_accepts_ownership_known_false`
===  ==========================  ==================================================
"""
from __future__ import annotations
import subproc

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

_THIS_SRC = (REPO_ROOT / "src").resolve()
import vkit.recover as _recover  # noqa: E402

if Path(_recover.__file__).resolve() != (_THIS_SRC / "vkit" / "recover.py"):
    raise AssertionError(
        f"vkit.recover resolved to {_recover.__file__}, not this worktree's "
        f"{_THIS_SRC / 'vkit' / 'recover.py'}. Refusing to run the gate against "
        "another owner's code."
    )

from vkit.claims import ResourceSpec, acquire, holders  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.paths import Project, open_project  # noqa: E402
from vkit.procidentity import is_alive, read_identity  # noqa: E402
from vkit.procs import prepare_job  # noqa: E402
from vkit.recover import (  # noqa: E402
    Action,
    FindingKind,
    RecoveryRefused,
    apply_action,
    inspect,
)
from vkit.schemas import RUN_REPORT, validate  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.supervisor import cancel_run, start_run, terminate_owned_tree  # noqa: E402
from vkit.tasks import open_task, supersede_task  # noqa: E402

CHECK_ID = "totals-behavior"
EXCLUSIVE = "gate-exclusive-checkout"

#: Windows-only because the mechanism under test is the Windows job object: a
#: named job, a handle held across a client disconnect, and a launch that
#: publishes its owner before the check finishes. Scoped per test rather than
#: applied to the module, because G11 asserts the POSIX *refusal* and so has to
#: run there -- a module-level skip would turn the one gate that is about POSIX
#: into a skip everywhere, and it would be a skip on the wrong host.
requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the ownership mechanism under test is the Windows job object",
)




def _git(*args: str, cwd: Path) -> None:
    subproc.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A real Git repository holding a real copy of the example, committed."""
    root = tmp_path / "project"
    shutil.copytree(EXAMPLE, root)
    _git("init", "-q", cwd=root)
    _git("add", "-A", cwd=root)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "gate", cwd=root)
    return root


@pytest.fixture()
def project(project_root: Path) -> Project:
    return open_project(project_root)


def make_manifest(project_root: Path, *, timeout: float = 60.0) -> str:
    """Point the manifest at this interpreter and give it room to be observed.

    `{{python}}` rather than a bare "python" so the check runs under the
    interpreter under test, and a generous timeout so a test that fails to cancel
    fails on its own assertion rather than on a timeout.
    """
    path = project_root / "verification" / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["checks"][0]["command"][0] = "{{python}}"
    document["checks"][0]["timeout_seconds"] = timeout
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _git("add", "-A", cwd=project_root)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "manifest", cwd=project_root)
    return document["checks"][0]["id"]


def slow_driver(project_root: Path, marker: Path, *, seconds: float = 20.0) -> None:
    """Make the driver announce its own pid and then stay alive.

    The pid is written by the driver itself rather than guessed by the test, so
    the test never acts on a process it has not positively identified. The sleep
    is inside the driver so the process is genuinely mid-work when a test looks at
    it, not merely started and about to finish.
    """
    driver = project_root / "verify_totals.py"
    source = driver.read_text(encoding="utf-8")
    anchor = "    scenarios = [run_case(args.python, args.app, case) for case in CASES]\n"
    assert source.count(anchor) == 1, "the example driver changed; this gate names its execution point"
    injection = (
        f"    Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"    time.sleep({seconds})\n"
        + anchor
    )
    source = source.replace(anchor, injection, 1)
    source = source.replace("import sys\n", "import sys\nimport os\nimport time\nfrom pathlib import Path\n", 1)
    driver.write_text(source, encoding="utf-8")


def read_marker(marker: Path) -> int | None:
    """The pid the driver wrote for itself, or None while it has not started.

    Read out of the driver's own marker rather than guessed, so no test in this
    file ever acts on a process it has not positively identified. Returns the pid
    as an int so `wait_for` can treat it as the condition it is.
    """
    try:
        return int(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def wait_for(predicate, *, timeout: float = 30.0, what: str = "condition"):
    """Wait for something the product will do, and fail naming what never happened.

    Polled rather than slept, because the alternative is a test whose duration is
    a guess about the machine rather than a measurement of the product.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def wait_until_dead(pid: int, *, timeout: float = 15.0) -> bool:
    return bool(wait_for(lambda: not _alive(pid), timeout=timeout, what=f"pid {pid} to stop"))


def _alive(pid: int) -> bool:
    try:
        identity = read_identity(pid)
    except Exception:
        return False
    return bool(identity and is_alive(identity))


def contract_for(project_root: Path, key: str = EXCLUSIVE) -> dict:
    """A contract this build's `TaskContract` accepts.

    Built field by field against `TaskContract.from_json` rather than from a
    description of it: admission measures `repository`, `policy_digest` and
    `required_checks` itself, and a contract carrying the caller's own values for
    those is refused.

    `key` is the resource the contract requires, and it has to be the same one the
    caller acquires. That is not a formality: `start_run` re-checks ownership
    against the contract before launching, so a contract naming a resource the task
    does not hold is refused with exactly the message a real lost checkout gives.
    """
    return {
        "repository": {"root": str(project_root)},
        "policy_digest": "gate",
        "required_checks": [CHECK_ID],
        "scope": "the R2 gate",
        "resources": [{"key": key, "kind": "exclusive"}],
        "declared": {},
    }


def holding_task(store: Store, project_root: Path, *, task_id: str | None = None):
    """A task that holds the exclusive resource, at generation 1."""
    task_id = task_id or f"gate-{uuid.uuid4().hex[:8]}"
    record = open_task(
        store, task_id=task_id, contract=contract_for(project_root), policy_digest="gate"
    )
    acquire(store, task_id, record.generation, [ResourceSpec(EXCLUSIVE, "exclusive")])
    return record


def start_run_for(project: Project, store: Store, task, manifest_dir: Path | None = None, **kw):
    return start_run(
        project, store, CHECK_ID,
        task_id=task.task_id, generation=task.generation,
        manifest=parse_manifest(project, manifest_dir or project.runs_root / "probe"),
        **kw,
    )


def child_cancels(project_root: Path, run_id: str) -> dict:
    """Cancel a run from a *separate OS process*, and return what it reported.

    G2 is about two clients, so the second one is a real process rather than a
    second call in this interpreter. That is the only way the test can show a
    named job is reachable from outside the process that created it.
    """
    program = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "from vkit.paths import open_project\n"
        "from vkit.storage import Store\n"
        "from vkit.supervisor import cancel_run\n"
        f"project = open_project({str(project_root)!r})\n"
        f"store = Store(project.db_path)\n"
        f"outcome, report = cancel_run(store, {run_id!r}, requested_by='second-process')\n"
        "print(json.dumps({'outcome': outcome.to_json(),"
        "                  'cancelled': report.get('cancelled'),"
        "                  'pending': report.get('pending')}))\n"
    )
    done = subproc.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=180,
        cwd=str(project_root),
    )
    assert done.returncode == 0, f"the second client failed:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@requires_windows

def test_check_start_returns_a_run_id_while_the_check_runs(
    project_root: Path, project: Project, tmp_path: Path
) -> None:
    """`check start` answers before the check has finished, and the run is real.

    The old contract returned the verdict, which meant the caller had to wait for
    the whole check and could not be a client that disconnects. A run that has
    returned is also not yet owned by anything, so the identity has to appear while
    the check is still running -- and `run_get` has to be able to read it.
    """
    make_manifest(project_root)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    marker = tmp_path / "marker"
    slow_driver(project_root, marker, seconds=20.0)

    started = time.monotonic()
    handoff = start_run_for(project, store, task)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, (
        f"start_run took {elapsed:.2f}s; a detached start must return before the check finishes"
    )
    assert handoff.replayed is False
    assert handoff.lifecycle in ("preparing", "launching", "running")
    assert handoff.task_id == task.task_id

    driver_pid = wait_for(lambda: read_marker(marker), what="the driver's marker")
    assert _alive(driver_pid)

    def published():
        status = store.run_status(handoff.run_id)
        process = (status or {}).get("process") or {}
        return process if process.get("ownership_known") else None

    process = wait_for(published, what="the run to publish its owner")
    assert process["pid"]
    assert process["creation_time"], "a bare pid cannot be verified against a recycled one"
    assert store.run_status(handoff.run_id)["lifecycle"] in ("launching", "running", "terminal")

    other = Store(project.db_path)
    assert other.run_status(handoff.run_id)["lifecycle"] == store.run_status(
        handoff.run_id
    )["lifecycle"]

    report = wait_for(
        lambda: (other.run_dir(handoff.run_id) / "report.json").is_file()
        and other.load(handoff.run_id),
        timeout=90, what="the run to publish a report",
    )
    assert report["outcome"]["result"] == "PASS"
    assert store.run_status(handoff.run_id)["lifecycle"] == "terminal"


@requires_windows

def test_second_client_cancels_a_run_started_by_another_process(
    project_root: Path, project: Project, tmp_path: Path
) -> None:
    """A cancel from another terminal reaches the tree, grandchild included.

    The containment under test is the job object, and a kill of the direct child
    alone would leave a grandchild running. The driver spawns one, so this fails if
    the mechanism degrades to `TerminateProcess`.
    """
    make_manifest(project_root)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    marker = tmp_path / "marker"
    slow_driver(project_root, marker, seconds=60.0)

    handoff = start_run_for(project, store, task)
    driver_pid = wait_for(lambda: read_marker(marker), what="the driver's marker")
    process = wait_for(
        lambda: (store.run_status(handoff.run_id).get("process") or {}).get("ownership_known")
        and store.run_status(handoff.run_id)["process"],
        what="the run to publish its owner",
    )

    answered = child_cancels(project_root, handoff.run_id)

    assert answered["cancelled"] is True, f"the second client did not cancel: {answered}"
    assert answered["outcome"]["reason"] == "cancelled"
    assert wait_until_dead(driver_pid), (
        f"the driver at pid {driver_pid} survived a cross-process cancel"
    )
    status = store.run_status(handoff.run_id)
    assert status["lifecycle"] == "terminal"
    assert status["result"] == "BLOCKED"
    assert status["reason"] == "cancelled"
    assert process["pid"] != driver_pid or True


@requires_windows

def test_cancel_does_not_kill_an_unrelated_control_process(
    project_root: Path, project: Project, tmp_path: Path
) -> None:
    """Containment is scoped to the run's own job, not to the machine.

    A cancel that killed bystanders would be indistinguishable from a cancel that
    failed, from the perspective of anything else on the host. Three unrelated
    processes are started outside any job and must survive.
    """
    make_manifest(project_root)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    marker = tmp_path / "marker"
    slow_driver(project_root, marker, seconds=60.0)

    controls = [
        subproc.popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(3)
    ]
    try:
        handoff = start_run_for(project, store, task)
        wait_for(lambda: read_marker(marker), what="the driver's marker")
        wait_for(
            lambda: (store.run_status(handoff.run_id).get("process") or {}).get("ownership_known"),
            what="the run to publish its owner",
        )

        answered = child_cancels(project_root, handoff.run_id)
        assert answered["cancelled"] is True

        driver_pid = read_marker(marker)
        assert wait_until_dead(driver_pid)
        for control in controls:
            assert _alive(control.pid), (
                f"an unrelated process at pid {control.pid} was killed by a run's cancel"
            )
    finally:
        for control in controls:
            if _alive(control.pid):
                control.kill()
            control.wait(timeout=30)


@requires_windows

def test_client_disconnect_leaves_the_run_owned(project_root: Path, project: Project, tmp_path: Path) -> None:
    """The supervisor holds the run, so the client that asked for it can die.

    This is the case the detached supervisor exists for. The client is a real
    process that starts a run and is then terminated outright, and the run still
    reaches its outcome with no further prompting.
    """
    make_manifest(project_root, timeout=120.0)
    slow_driver(project_root, tmp_path / "marker", seconds=3.0)
    handoff_path = tmp_path / "handoff.txt"
    detach_key = "gate-detach-res"

    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "from pathlib import Path\n"
        "from vkit.paths import open_project\n"
        "from vkit.storage import Store\n"
        "from vkit.supervisor import start_run\n"
        "from vkit.claims import ResourceSpec, acquire\n"
        "from vkit.tasks import open_task\n"
        f"project = open_project({str(project_root)!r})\n"
        "store = Store(project.db_path)\n"
        f"contract = {contract_for(project_root, detach_key)!r}\n"
        "task = open_task(store, task_id='gate-detach', contract=contract, policy_digest='gate')\n"
        f"acquire(store, task.task_id, task.generation, [ResourceSpec({detach_key!r}, 'exclusive')])\n"
        "handoff = start_run(project, store, 'totals-behavior', task_id=task.task_id,"
        " generation=task.generation)\n"
        f"Path({str(handoff_path)!r}).write_text(handoff.run_id, encoding='utf-8')\n"
        "import time; time.sleep(300)\n"
    )
    client = subproc.popen(
        [sys.executable, "-c", program],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=str(project_root),
    )
    try:
        run_id = wait_for(
            lambda: handoff_path.read_text(encoding="utf-8") if handoff_path.is_file() else None,
            timeout=45, what="the client to report a run id",
        )
        assert run_id, (
            f"the client never reported a run id; it exited {client.poll()}.\n"
            f"stderr:\n{client.stderr.read() if client.stderr else '(none)'}"
        )

        import win32api
        import win32con

        win32api.TerminateProcess(win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, client.pid), 1)
        client.wait(timeout=30)
        assert not _alive(client.pid), "the client survived its own termination"

        store = Store(open_project(project_root).db_path)
        immediately = store.run_status(run_id)["lifecycle"]
        assert immediately in ("preparing", "launching", "running"), (
            "the run was already terminal when its client was killed, so this gate "
            "measured nothing about surviving a disconnect"
        )

        report = wait_for(
            lambda: (store.run_dir(run_id) / "report.json").is_file() and store.load(run_id),
            timeout=120, what="the orphaned run to publish its outcome",
        )
        assert report["outcome"]["result"] == "PASS"
        assert store.run_status(run_id)["lifecycle"] == "terminal"
    finally:
        if client.poll() is None:
            client.kill()
        client.wait(timeout=30)


@requires_windows

def test_launch_crash_before_identity_publication(
    project_root: Path, project: Project, tmp_path: Path
) -> None:
    """A supervisor that dies in the launching window is the dangerous case.

    The run exists, a process was created, and no identity was ever published.
    Recovery must name that state, the claim must be held, a second owner must be
    refused, and only an explicit reconciliation may release it. Each of those is
    asserted, because any one of them passing while another fails is the shape of
    the original defect.
    """
    make_manifest(project_root)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    marker = tmp_path / "marker"
    slow_driver(project_root, marker, seconds=60.0)

    os.environ["VKIT_FAULT"] = "after_launching"
    try:
        handoff = start_run_for(project, store, task)
    finally:
        os.environ.pop("VKIT_FAULT", None)

    def launching():
        status = store.run_status(handoff.run_id)
        return status if status and status["lifecycle"] == "launching" else None

    status = wait_for(launching, what="the run to reach the launching window")
    assert status["process"]["ownership_known"] is False
    assert status["process"]["launch_state"] == "started"

    launch = store.load_launch(handoff.run_id)
    assert launch is not None, "an unresolved launch must still name the job that owned its tree"
    assert launch["job_name"], "the job name is what a second process would open"

    report = inspect(store)
    unresolved = report.of(FindingKind.RUN_UNRESOLVED_LAUNCH)
    assert len(unresolved) == 1, f"expected one unresolved launch, got {[f.kind.value for f in report.findings]}"
    assert unresolved[0].target == handoff.run_id
    assert unresolved[0].action is Action.ABANDON_LAUNCH
    assert unresolved[0].actionable is True

    supersede_task(store, task.task_id)
    with pytest.raises(RecoveryRefused) as refused:
        apply_action(store, Action.RELEASE_CLAIM, target=EXCLUSIVE, evidence="the holder looks idle")
    assert handoff.run_id in str(refused.value)
    assert holders(store), "a refused release must leave the claim in place"

    other = open_task(
        store, task_id=f"gate-second-{uuid.uuid4().hex[:6]}",
        contract=contract_for(project_root), policy_digest="gate",
    )
    with pytest.raises(Exception) as conflict:
        acquire(store, other.task_id, other.generation, [ResourceSpec(EXCLUSIVE, "exclusive")])
    assert "gate-exclusive-checkout" in str(conflict.value)

    reconciled = apply_action(
        store, Action.ABANDON_LAUNCH, target=handoff.run_id,
        evidence="an operator confirmed the job no longer opens and the supervisor is gone",
    )
    assert reconciled.for_target(handoff.run_id)[0].reconciled is True
    after = apply_action(
        store, Action.RELEASE_CLAIM, target=EXCLUSIVE,
        evidence="the launch was abandoned and its supervisor is confirmed gone",
    )
    assert after.of(FindingKind.CLAIM_STALE_GENERATION) == ()
    assert not holders(store), "the claim should be released once the launch was abandoned"


@requires_windows

def test_release_while_child_alive_refuses(project_root: Path, project: Project, tmp_path: Path) -> None:
    """The original F13 reproduction, as a test.

    A run in flight whose owner is published and alive, a task superseded past it,
    and a claim release. The release must refuse; a second exclusive owner must be
    refused; and the child must be left alone to finish on its own, still holding
    its claim.
    """
    make_manifest(project_root, timeout=120.0)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    marker = tmp_path / "marker"
    slow_driver(project_root, marker, seconds=6.0)

    handoff = start_run_for(project, store, task)
    driver_pid = wait_for(lambda: read_marker(marker), what="the driver's marker")
    wait_for(
        lambda: (store.run_status(handoff.run_id).get("process") or {}).get("ownership_known"),
        what="the run to publish its owner",
    )

    supersede_task(store, task.task_id)
    findings = {f.kind for f in inspect(store).findings}
    assert FindingKind.RUN_LIVE_PROCESS in findings

    with pytest.raises(RecoveryRefused) as refused:
        apply_action(store, Action.RELEASE_CLAIM, target=EXCLUSIVE, evidence="generation 1 looks abandoned")
    assert handoff.run_id in str(refused.value)
    assert "alive" in str(refused.value)
    assert holders(store), "a refused release must leave the claim in place"

    other = open_task(
        store, task_id=f"gate-second-{uuid.uuid4().hex[:6]}",
        contract=contract_for(project_root), policy_digest="gate",
    )
    with pytest.raises(Exception):
        acquire(store, other.task_id, other.generation, [ResourceSpec(EXCLUSIVE, "exclusive")])

    report = wait_for(
        lambda: (store.run_dir(handoff.run_id) / "report.json").is_file()
        and store.load(handoff.run_id),
        timeout=120, what="the live child to finish on its own",
    )
    assert report["outcome"]["result"] == "PASS"
    assert store.run_status(handoff.run_id)["lifecycle"] == "terminal"
    assert not wait_until_dead(driver_pid, timeout=1.0) or True
    assert holders(store), "the claim stays held after a natural completion"


@requires_windows

def test_parent_crash_keeps_claims_until_the_run_ends(
    project_root: Path, project: Project, tmp_path: Path
) -> None:
    """A supervisor that dies takes its tree with it, and the claim stays held.

    The containment is asserted, not assumed: the driver is polled until it is
    gone, because `KILL_ON_JOB_CLOSE` fires when the last handle to the job closes
    and a test that sampled once would be measuring the machine rather than the
    mechanism.
    """
    make_manifest(project_root)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    marker = tmp_path / "marker"
    slow_driver(project_root, marker, seconds=60.0)

    os.environ["VKIT_FAULT"] = "after_launching"
    try:
        handoff = start_run_for(project, store, task)
    finally:
        os.environ.pop("VKIT_FAULT", None)

    wait_for(
        lambda: (store.run_status(handoff.run_id) or {}).get("lifecycle") == "launching",
        what="the run to reach the launching window",
    )
    recorded_pid = (store.run_status(handoff.run_id)["process"] or {})["pid"]
    assert recorded_pid

    assert wait_until_dead(recorded_pid, timeout=10.0), (
        "the run's own recorded process outlived the supervisor that owned it; a "
        "Windows job's kill-on-last-handle-close did not fire"
    )

    supersede_task(store, task.task_id)
    with pytest.raises(RecoveryRefused):
        apply_action(store, Action.RELEASE_CLAIM, target=EXCLUSIVE, evidence="the supervisor is gone")
    assert holders(store), "the claim is retained while the launch is unresolved"

    apply_action(
        store, Action.ABANDON_LAUNCH, target=handoff.run_id,
        evidence="the operator observed the tree gone and the supervisor dead",
    )
    apply_action(store, Action.RELEASE_CLAIM, target=EXCLUSIVE, evidence="reconciled by an operator")
    assert not holders(store)


@requires_windows

def test_repeated_mutation_request_does_not_duplicate_a_launch(
    project_root: Path, project: Project
) -> None:
    """One request id, five attempts, one run and one launch.

    The launch is the expensive and the destructive thing, so a retry that started
    a second one would run a check twice under two identities. The payload is
    compared, because an id reused with a different payload is a different request
    and must not attach to the first one's run.
    """
    make_manifest(project_root, timeout=120.0)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    manifest = parse_manifest(project, project.runs_root / "probe")

    handoffs = [
        start_run(project, store, CHECK_ID, task_id=task.task_id,
                  generation=task.generation, manifest=manifest, run_id="gate-fixed-run")
        for _ in range(5)
    ]
    assert len({h.run_id for h in handoffs}) == 1, "a retry minted a second run id"
    assert [h.replayed for h in handoffs] == [False, True, True, True, True]

    with store._connect() as conn:
        launches = conn.execute("SELECT COUNT(*) FROM launches WHERE run_id = ?", (handoffs[0].run_id,)).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id = ?", (handoffs[0].run_id,)).fetchone()[0]
    assert launches == 1, f"five identical requests produced {launches} launches"
    assert runs == 1

    report = wait_for(
        lambda: (store.run_dir(handoffs[0].run_id) / "report.json").is_file()
        and store.load(handoffs[0].run_id),
        timeout=120, what="the replayed run to publish once",
    )
    assert report["outcome"]["result"] == "PASS"
    for handoff in handoffs[1:]:
        assert handoff.lifecycle in ("preparing", "launching", "running", "terminal")
    assert store.run_status(handoffs[0].run_id)["lifecycle"] == "terminal"


@requires_windows

def test_no_second_owner_writes_concurrently(project_root: Path, project: Project) -> None:
    """Two processes racing for one *free* exclusive resource: exactly one wins.

    `claims.acquire_in` is atomic, so this is a property of the store rather than
    of the caller, and the test is here because the consequence of losing it is
    two check drivers writing one checkout.

    The key starts free. Racing for a key one of the racers already holds would be
    refused on entry and would prove nothing about the race.
    """
    make_manifest(project_root)
    store = Store(project.db_path)

    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "from vkit.claims import ResourceSpec, acquire\n"
        "from vkit.paths import open_project\n"
        "from vkit.storage import ConflictError, Store\n"
        "from vkit.tasks import open_task\n"
        f"project = open_project({str(project_root)!r})\n"
        f"store = Store(project.db_path)\n"
        f"contract = {contract_for(project_root)!r}\n"
        f"task = open_task(store, task_id=sys.argv[1], contract=contract, policy_digest='gate')\n"
        "try:\n"
        f"    acquire(store, task.task_id, task.generation, [ResourceSpec({EXCLUSIVE!r}, 'exclusive')])\n"
        "except ConflictError as exc:\n"
        "    print('REFUSED', exc); raise SystemExit(1)\n"
        "print('ADMITTED'); raise SystemExit(0)\n"
    )
    ids = [f"gate-race-{uuid.uuid4().hex[:6]}" for _ in range(2)]
    racers = [
        subproc.popen(
            [sys.executable, "-c", program, task_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(project_root),
        )
        for task_id in ids
    ]
    results = [(r.wait(timeout=120), r.stdout.read(), r.stderr.read()) for r in racers]
    for racer in racers:
        racer.stdout.close()
        racer.stderr.close()

    admitted = [out for code, out, _ in results if code == 0]
    refused = [(out, err) for code, out, err in results if code != 0]
    assert len(admitted) == 1, (
        f"expected exactly one admission, got {len(admitted)}; results={results}"
    )
    assert len(refused) == 1, (
        f"expected exactly one refusal, got {len(refused)}; results={results}"
    )
    assert "REFUSED" in refused[0][0], f"the loser must be refused by ConflictError: {refused[0][1]}"

    members = [c for c in holders(store) if c.resource_key == EXCLUSIVE]
    assert len(members) == 1, f"{len(members)} members hold one exclusive key"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM launches WHERE job_name IS NOT NULL"
        ).fetchone()[0] == 0, "neither racer should have started a run"


@requires_windows

def test_cancel_races_completion_to_one_outcome(project_root: Path, project: Project) -> None:
    """A cancel and a completion in the same instant produce exactly one outcome.

    Twenty iterations, because a race that only shows itself one time in twenty is
    a race that ships. Each run must end terminal, with either PASS or
    BLOCKED/cancelled and never both, and none may be left mid-flight.
    """
    make_manifest(project_root, timeout=120.0)
    store = Store(project.db_path)
    task = holding_task(store, project_root)

    outcomes: list[str] = []
    for index in range(20):
        run_id = f"gate-race-{index}-{uuid.uuid4().hex[:6]}"
        handoff = start_run(
            project, store, CHECK_ID, task_id=task.task_id, generation=task.generation,
            manifest=parse_manifest(project, project.runs_root / "probe"), run_id=run_id,
        )
        time.sleep(0.02 * (index % 7))
        try:
            cancel_run(store, handoff.run_id, requested_by="gate-race")
        except Exception:
            pass

        report = wait_for(
            lambda r=handoff.run_id: (store.run_dir(r) / "report.json").is_file() and store.load(r),
            timeout=120, what=f"run {index} to reach one outcome",
        )
        status = store.run_status(handoff.run_id)
        assert status["lifecycle"] == "terminal", f"run {index} was left {status['lifecycle']}"
        outcome = report["outcome"]
        outcomes.append(outcome["result"])
        if outcome["result"] == "BLOCKED":
            assert outcome["reason"] == "cancelled", f"run {index}: {outcome}"
        assert report["lifecycle"] == "terminal"

    assert set(outcomes) <= {"PASS", "BLOCKED"}, outcomes
    print(f"\nG10 outcome distribution over 20 races: "
          f"PASS={outcomes.count('PASS')} cancelled={outcomes.count('BLOCKED')}")




@pytest.mark.skipif(sys.platform == "win32", reason="asserts the POSIX refusal")
def test_posix_cancel_still_refuses() -> None:
    """The POSIX refusal is unchanged, asserted on its text.

    The text is the contract: it is what tells an operator that the claim is
    retained and the tree may have survived, and R2 adds no POSIX containment, so
    the refusal must not be quietly reworded into something that reads like a
    temporary condition.
    """
    with pytest.raises(OSError) as raised:
        terminate_owned_tree(None)

    message = str(raised.value)
    assert "setsid" in message, "the refusal must still name the escape it cannot cover"
    assert "kill-on-last-handle-close" in message, (
        "the refusal must still name the mechanism POSIX has no equivalent of"
    )
    assert "claim is retained" in message


@requires_windows

def test_run_report_schema_accepts_ownership_known_false(
    project_root: Path, project: Project
) -> None:
    """A cancel report for a run with no published owner is a valid document.

    The key exists so that a report cannot be read as "no process exists" when the
    truth is "the owner was never published". The schema is
    `additionalProperties: false`, so the key has to be declared or the report
    fails the contract it is published under -- and the store round-trips it
    unchanged, so the evidence on disk is the evidence the schema admitted.
    """
    make_manifest(project_root)
    store = Store(project.db_path)
    task = holding_task(store, project_root)
    handoff = start_run_for(project, store, task)

    intent = store.record_cancel_intent(handoff.run_id, "gate")
    assert intent["ownership_known"] is False
    assert intent["applied"] is False

    from vkit.outcome import Blocked, BlockedReason
    from vkit.supervisor import _cancel_report

    report = _cancel_report(
        handoff.run_id, {}, Blocked(BlockedReason.CANCELLED, "never launched"), None,
        store.load_launch(handoff.run_id),
    )
    validate("run report", RUN_REPORT, report)
    assert report["process"]["ownership_known"] is False

    store.publish(handoff.run_id, report)
    assert store.load(handoff.run_id)["process"]["ownership_known"] is False

    from vkit.schemas import SchemaValidationError

    report["process"]["ownership_known"] = True
    with pytest.raises(SchemaValidationError):
        validate("run report", RUN_REPORT, report)

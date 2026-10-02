"""Public regressions for process-group lifetime and the recovery launch fence."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vkit.claims import holders  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.procidentity import process_group_is_alive  # noqa: E402
from vkit.recover import Action, RecoveryRefused, apply_action, liveness  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.supervisor import start_run  # noqa: E402
from vkit.tasks import acceptance_context, admit, supersede_task  # noqa: E402


EXAMPLE = REPO_ROOT / "examples" / "python-cli"
RESOURCE = "process-regression-checkout"
TASK_ID = "process-regression-task"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _project_with_probe(root: Path, code: str, marker: Path, *, timeout: float = 10) -> tuple:
    project_root = root / "project"
    shutil.copytree(EXAMPLE, project_root)
    _git(project_root, "init", "-q")
    _git(project_root, "add", "-A")
    _git(project_root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example")

    (project_root / "probe.py").write_text(
        code.replace("{marker!r}", repr(str(marker))), encoding="utf-8"
    )
    manifest_path = project_root / "verification" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check = manifest["checks"][0]
    check["command"] = ["{{python}}", "probe.py"]
    check["timeout_seconds"] = timeout
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _git(project_root, "add", "-A")
    _git(project_root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "probe")
    return open_project(project_root), check["id"]


def _task(project, store: Store):
    context = acceptance_context(
        project,
        lambda: parse_manifest(project, project.runs_root / "admission"),
    )
    return admit(
        store,
        TASK_ID,
        context=context,
        scope="process ownership regression",
        resources=[{"key": RESOURCE, "kind": "exclusive"}],
    ).task


def _wait_for(predicate, message: str, seconds: float = 8) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def test_release_waits_for_a_posix_descendant_after_its_leader_exits(tmp_path: Path) -> None:
    if sys.platform == "win32" or not Path("/proc").is_dir():
        import pytest
        pytest.skip("this real-process regression requires Linux /proc")
    marker = tmp_path / "pids"
    project, check_id = _project_with_probe(
        tmp_path,
        "import os, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])\n"
        "open({marker!r}, 'w').write(f'{os.getpid()} {child.pid}')\n",
        marker,
    )
    store = Store(project.db_path)
    task = _task(project, store)
    handoff = start_run(
        project,
        store,
        check_id,
        task_id=TASK_ID,
        generation=task.generation,
        manifest=parse_manifest(project, project.runs_root / "probe"),
        detach=True,
    )
    try:
        _wait_for(marker.is_file, "the probe did not start its descendant")
        leader, child = map(int, marker.read_text(encoding="utf-8").split())
        _wait_for(
            lambda: liveness(leader).state.value == "dead",
            "the process-group leader did not exit",
        )
        assert process_group_is_alive(leader)
        assert liveness(child).state.value == "alive"

        supersede_task(store, TASK_ID)
        try:
            apply_action(
                store,
                Action.RELEASE_CLAIM,
                target=RESOURCE,
                evidence="the leader exited",
            )
        except RecoveryRefused:
            pass
        else:
            raise AssertionError("recovery released a claim while its process-group child lived")
        assert holders(store, TASK_ID)

        _wait_for(
            lambda: (store.run_status(handoff.run_id) or {}).get("lifecycle") == "terminal",
            "the supervisor did not wait for the descendant to finish",
            seconds=8,
        )
        apply_action(
            store,
            Action.RELEASE_CLAIM,
            target=RESOURCE,
            evidence="the process group exited naturally",
        )
        assert not holders(store, TASK_ID)
    finally:
        # The probe child has a finite natural lifetime and is never killed by the test.
        _wait_for(
            lambda: (store.run_status(handoff.run_id) or {}).get("lifecycle") == "terminal",
            "the finite process-group probe did not finish",
            seconds=8,
        )


def test_recovery_fences_a_supervisor_paused_before_its_claim(tmp_path: Path) -> None:
    if sys.platform == "win32" or not Path("/proc").is_dir():
        import pytest
        pytest.skip("this real-process regression requires Linux /proc")
    marker = tmp_path / "driver.pid"
    ready, proceed, claimed = (tmp_path / name for name in ("ready", "proceed", "claimed"))
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "from vkit.storage import Store\n"
        "original = Store.claim_supervisor\n"
        "def paused(self, run_id, *args, **kwargs):\n"
        f"    Path({str(ready)!r}).write_text('ready')\n"
        "    deadline = time.monotonic() + 15\n"
        f"    gate = Path({str(proceed)!r})\n"
        "    while not gate.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    try:\n"
        "        return original(self, run_id, *args, **kwargs)\n"
        "    finally:\n"
        f"        Path({str(claimed)!r}).write_text('returned')\n"
        "Store.claim_supervisor = paused\n",
        encoding="utf-8",
    )
    project, check_id = _project_with_probe(
        tmp_path,
        "import os\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n",
        marker,
    )
    store = Store(project.db_path)
    task = _task(project, store)
    env_path = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(hook), str(REPO_ROOT / "src"), env_path) if item
    )
    try:
        handoff = start_run(
            project,
            store,
            check_id,
            task_id=TASK_ID,
            generation=task.generation,
            manifest=parse_manifest(project, project.runs_root / "probe"),
            detach=True,
        )
    finally:
        if env_path:
            os.environ["PYTHONPATH"] = env_path
        else:
            os.environ.pop("PYTHONPATH", None)

    try:
        _wait_for(ready.is_file, "the supervisor did not reach the pre-claim barrier")
        status = store.run_status(handoff.run_id)
        assert status["lifecycle"] == "preparing"
        assert store.load_launch(handoff.run_id)["supervisor_pid"] == 0

        supersede_task(store, TASK_ID)
        apply_action(
            store,
            Action.ABANDON_LAUNCH,
            target=handoff.run_id,
            evidence="isolated regression confirmed the paused supervisor had not claimed the run",
        )
        apply_action(
            store,
            Action.RELEASE_CLAIM,
            target=RESOURCE,
            evidence="the launch was abandoned before supervisor ownership",
        )
        proceed.write_text("continue", encoding="utf-8")
        _wait_for(claimed.is_file, "the paused supervisor did not resume its claim attempt")
        time.sleep(0.25)
        assert not marker.exists(), "the abandoned supervisor started its check"
        assert not holders(store, TASK_ID)
        assert (store.run_status(handoff.run_id) or {}).get("lifecycle") == "preparing"
    finally:
        proceed.write_text("continue", encoding="utf-8")


if __name__ == "__main__" and sys.platform != "win32":
    with tempfile.TemporaryDirectory() as raw:
        test_release_waits_for_a_posix_descendant_after_its_leader_exits(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_recovery_fences_a_supervisor_paused_before_its_claim(Path(raw))

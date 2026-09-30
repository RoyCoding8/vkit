"""The POSIX ownership path, driven through the public function on a POSIX host.

tests/test_procs.py skips off Windows, because the mechanism it exercises is a
job object. tests/test_procs_posix.py covers the control flow around
`os.killpg` but stubs both POSIX-only mechanisms, and its kill stub shells out to
`taskkill`, so it cannot run here at all.

That left the POSIX path with no test that runs it. This one does, with nothing
stubbed: the child is a real interpreter, `start_new_session` is the real call,
and the containment is measured three levels deep, because a group signal that
reaches only the direct child is not containment.

The killed processes are observed from OUTSIDE the group with a signal-zero
probe, where EPERM is positive proof of life. A reader that treats EPERM as
absence reports a live worker as dead, which is the one direction of error that
destroys a run rather than merely mislabelling it.

Skipped on Windows, where the job object is the mechanism and test_procs.py owns
it. Run:  bash scripts/posix-run.sh -m pytest tests/test_procs_posix_real.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import vkit.procs as procs  # noqa: E402

_THIS_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()
if Path(procs.__file__).resolve() != (_THIS_SRC / "vkit" / "procs.py"):
    raise AssertionError(
        f"vkit.procs resolved to {procs.__file__}, not this worktree's "
        f"{_THIS_SRC / 'vkit' / 'procs.py'}. Refusing to run the gate against "
        "another owner's code."
    )

PYTHON = sys.executable
LINE = b"\r\n" if os.name == "nt" else b"\n"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the ownership mechanism under test here is the POSIX process group; "
           "the job object is covered by tests/test_procs.py",
)


def is_alive(pid: int) -> bool:
    """Whether a pid still names a process, observed the way a bystander would.

    Signal 0 runs the kernel's existence and permission check without delivering
    anything. EPERM means the process is there and belongs to someone else, which
    is proof of life; ESRCH means it is not there. Both arms are explicit because
    collapsing them reports a live worker as dead.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def surviving_group(pgid: int) -> list[tuple[int, str]]:
    """Every live pid whose process group is `pgid`, read by walking /proc.

    Independent of the pids this test happened to remember, so a process it never
    learned about still shows up.
    """
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        try:
            fields = stat.rsplit(") ", 1)[-1].split()
            if int(fields[2]) == pgid:  # proc(5) field 5, pgrp
                found.append((int(entry.name), fields[0]))
        except (IndexError, ValueError):
            continue
    return found


def write_script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def run(script: Path, tmp_path: Path, *, timeout: float = 60.0) -> procs.ExecutionResult:
    return procs.run_command(
        [PYTHON, str(script)],
        cwd=script.parent,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=timeout,
    )


def await_pids_file(path: Path, limit: float = 60.0) -> dict:
    """Wait for the tree to announce itself, so the test never races the spawn.

    Each level of the tree appends one JSON object per line, so the file is
    read as a union. Waiting for all three keys rather than the first line is
    what makes "the great-grandchild existed" an observation instead of an
    assumption.
    """
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if path.is_file():
            recorded: dict = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    recorded.update(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a partially written line; the next read will have it
            if {"child", "grandchild", "great"} <= recorded.keys():
                return recorded
        time.sleep(0.02)
    raise AssertionError(
        f"the tree never reported all three pids to {path}; "
        f"last read: {path.read_text(encoding='utf-8') if path.is_file() else '<no file>'}"
    )


def test_a_timeout_kills_the_whole_process_group_three_levels_deep(tmp_path: Path) -> None:
    """A timeout must take the grandchild and the great-grandchild with it.

    The unit is the tree, not the process this module happened to launch. A signal
    that reached only the direct child would leave the build and the test binary
    running, still holding file locks, which is the failure this module exists to
    prevent.
    """
    pids_file = tmp_path / "pids.json"
    great = write_script(tmp_path, "great.py", "import time\ntime.sleep(120)\n")
    grand = write_script(
        tmp_path,
        "grand.py",
        "import json, os, subprocess, sys, time\n"
        f"great_proc = subprocess.Popen([{PYTHON!r}, {str(great)!r}])\n"
        # Each level APPENDS its own key rather than rewriting the file, so the
        # recorded set describes the whole tree and a test cannot lose a level by
        # reading a file the deepest writer truncated.
        f"open({str(pids_file)!r}, 'a').write(json.dumps("
        "{'grandchild': os.getpid(), 'great': great_proc.pid}) + '\\n')\n"
        "time.sleep(120)\n",
    )
    child = write_script(
        tmp_path,
        "child.py",
        "import json, os, subprocess, sys, time\n"
        f"grand_proc = subprocess.Popen([{PYTHON!r}, {str(grand)!r}])\n"
        f"open({str(pids_file)!r}, 'a').write(json.dumps("
        "{'child': os.getpid(), 'grandchild': grand_proc.pid}) + '\\n')\n"
        "time.sleep(120)\n",
    )

    result = run(child, tmp_path, timeout=3.0)

    assert result.timed_out is True
    assert result.ownership == "posix_process_group"
    # A negative code is the SIGKILL this module sent. None would be the
    # unkillable case; 1 would be indistinguishable from a command that chose to
    # fail, which is the reading the Windows path deliberately avoids.
    assert result.exit_code is not None
    assert result.pid is not None

    recorded = await_pids_file(pids_file)
    assert recorded["child"] == result.pid
    assert recorded["grandchild"] != result.pid
    assert recorded["great"] not in (None, result.pid, recorded["grandchild"])

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and surviving_group(result.pid):
        time.sleep(0.05)

    assert surviving_group(result.pid) == [], (
        f"processes survived in the group this module owns: "
        f"{surviving_group(result.pid)}"
    )
    for level in ("child", "grandchild", "great"):
        assert is_alive(recorded[level]) is False, f"the {level} outlived the group signal"


def test_a_run_that_finishes_in_time_reports_its_code_and_its_ownership(
    tmp_path: Path,
) -> None:
    """The ordinary path, so the timeout test above is not the only one that runs."""
    script = write_script(
        tmp_path,
        "ok.py",
        "import sys\nsys.stdout.write('PASS on stdout\\n')\n"
        "sys.stderr.write('PASS on stderr\\n')\n",
    )

    result = run(script, tmp_path, timeout=60.0)

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.reason is None
    assert result.ownership == "posix_process_group"
    assert (tmp_path / "out.log").read_bytes() == b"PASS on stdout" + LINE
    assert (tmp_path / "err.log").read_bytes() == b"PASS on stderr" + LINE
    assert result.to_json()["process"] == {
        "pid": result.pid,
        "ownership": "posix_process_group",
        "exit_code": 0,
        "timed_out": False,
    }


def test_a_nonzero_exit_reports_that_code_and_no_reason(tmp_path: Path) -> None:
    """A command that ran and failed is a result, not a blocked run."""
    script = write_script(
        tmp_path,
        "fail.py",
        "import sys\nsys.stderr.write('boom\\n')\nsys.exit(23)\n",
    )

    result = run(script, tmp_path, timeout=60.0)

    assert result.exit_code == 23
    assert result.reason is None
    assert result.timed_out is False
    assert (tmp_path / "err.log").read_bytes() == b"boom" + LINE


def test_a_command_that_cannot_start_is_blocked_rather_than_executed(
    tmp_path: Path,
) -> None:
    """A launch failure and a nonzero exit are different reports.

    Nothing executed, so the result must carry no pid. A pid here would let a
    caller read a blocked launch as a run that happened.
    """
    result = procs.run_command(
        [str(tmp_path / "no-such-executable")],
        cwd=tmp_path,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=30.0,
    )

    assert result.pid is None
    assert result.launched is False
    assert result.reason is not None
    assert result.reason.value == "launch_failed"
    # Both files exist and are empty, so a report can cite them without
    # special-casing the no-launch case.
    assert (tmp_path / "out.log").is_file()
    assert (tmp_path / "out.log").read_bytes() == b""


def test_the_process_group_is_the_units_own_and_not_the_callers(tmp_path: Path) -> None:
    """The launch must not put the caller's own process group at risk.

    `start_new_session` is what makes the group this module signals a group of
    only this run's tree. If it were omitted, a timeout would signal the group the
    supervisor itself belongs to, and the supervisor would die with its worker.
    """
    script = write_script(tmp_path, "idle.py", "import time\ntime.sleep(120)\n")

    result = run(script, tmp_path, timeout=3.0)

    assert result.timed_out is True
    # The child led its own group, so its pgid is its own pid and is not ours.
    assert os.getpgid(os.getpid()) != result.pid
    # This process is still here, which is the whole point.
    assert is_alive(os.getpid()) is True

"""Drive the POSIX timeout branch on a Windows host.

os.killpg and start_new_session do not exist here, so both are stubbed. What is
under test is the real control flow around them: that a timeout assigns an exit
code instead of leaving the local unbound, and that the result reports a timeout
rather than crashing.

The previous shape assigned exit_code only when the second wait ALSO timed out,
which is the rare case, so an ordinary POSIX timeout raised UnboundLocalError.
This test is the reason that is now covered rather than merely claimed untested.

**This file is for a Windows host only**, and skips elsewhere. It is a harness
that reaches the POSIX code by stubbing, so on a POSIX host it would test the
stubs rather than the mechanism, and its kill stub is itself Windows-only:
`taskkill` does not exist here, so the run died with
`FileNotFoundError: 'taskkill'` and reported a timeout that never happened.

On a POSIX host the real path is covered, unstubbed and three levels deep, by
tests/test_procs_posix_real.py.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import vkit.procs as procs  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="this harness reaches the POSIX branch by stubbing, and its kill stub "
           "shells out to taskkill; on a POSIX host the real path is covered by "
           "tests/test_procs_posix_real.py",
)


def _force_posix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    """Stub the two POSIX-only mechanisms and record which pids were signalled."""
    killed: list[int] = []

    def fake_kill(pid: int) -> None:
        killed.append(pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)

    monkeypatch.setattr(procs, "IS_WINDOWS", False)
    monkeypatch.setattr(procs, "_kill_process_group", fake_kill)
    return killed


def test_a_posix_timeout_reports_a_timeout_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    killed = _force_posix(monkeypatch, tmp_path)
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    # start_new_session is POSIX-only, so the real Popen call cannot be reused
    # verbatim. Everything else in the function is the real code.
    real_popen = subprocess.Popen

    def posix_popen(*args, **kwargs):
        kwargs.pop("start_new_session", None)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(procs.subprocess, "Popen", posix_popen)

    started = time.monotonic()
    result = procs._run_posix(
        (sys.executable, str(script)), tmp_path,
        tmp_path / "out.log", tmp_path / "err.log", 1.0,
    )
    assert result.timed_out is True
    assert result.ownership == "posix_process_group"
    # The point of the regression: a real exit code, not an unbound local.
    assert result.exit_code is not None
    assert result.pid in killed
    assert time.monotonic() - started < 30


def test_a_posix_run_that_finishes_in_time_reports_its_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_posix(monkeypatch, tmp_path)
    script = tmp_path / "quick.py"
    script.write_text("print('done')\n", encoding="utf-8")

    real_popen = subprocess.Popen

    def posix_popen(*args, **kwargs):
        kwargs.pop("start_new_session", None)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(procs.subprocess, "Popen", posix_popen)

    result = procs._run_posix(
        (sys.executable, str(script)), tmp_path,
        tmp_path / "out.log", tmp_path / "err.log", 30.0,
    )
    assert result.timed_out is False
    assert result.exit_code == 0

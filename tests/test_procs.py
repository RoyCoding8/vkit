"""Behavior tests for process ownership, driven through the public function.

Every test launches a real interpreter running a real script written into
tmp_path, then observes what actually happened: the bytes the child wrote, the
exit code the OS recorded, and whether a named process is still openable. There
are no mocks, because a mock of a job object would assert that this module
called the API it was written to call rather than that a process was contained.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_THIS_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()
import vkit.procs as _procs  # noqa: E402

if Path(_procs.__file__).resolve() != (_THIS_SRC / "vkit" / "procs.py"):
    raise AssertionError(
        f"vkit.procs resolved to {_procs.__file__}, not this worktree's "
        f"{_THIS_SRC / 'vkit' / 'procs.py'}. Refusing to run the gate against "
        "another owner's code."
    )

ExecutionResult = _procs.ExecutionResult
run_command = _procs.run_command

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the ownership mechanism under test is the Windows job object"
)

PYTHON = sys.executable

WINERROR_INVALID_PARAMETER = 87

LINE = b"\r\n" if os.name == "nt" else b"\n"


def is_alive(pid: int) -> bool:
    """Ask the kernel whether a pid still has a process behind it.

    Deliberately not `tasklist` and not a return code from the run: a dead
    process can be reaped by its parent and reported as nothing, and a pid can be
    recycled. OpenProcess is the only one of the three that asks the kernel about
    the object itself.
    """
    import pywintypes
    import win32api
    import win32con

    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
    except pywintypes.error as exc:
        assert exc.winerror == WINERROR_INVALID_PARAMETER, (
            f"pid {pid} could not be opened, but not for the expected reason: "
            f"{exc.winerror} {exc.strerror}"
        )
        return False
    handle.Close()
    return True


def write_script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def run(script: Path, tmp_path: Path, *, timeout: float = 60.0, cwd: Path | None = None,
        args: tuple[str, ...] = ()) -> ExecutionResult:
    return run_command(
        [PYTHON, str(script), *args],
        cwd=cwd or script.parent,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=timeout,
    )


def test_zero_exit_writes_to_both_streams(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "ok.py",
        "import sys\n"
        "sys.stdout.write('PASS on stdout\\n')\n"
        "sys.stderr.write('PASS on stderr\\n')\n",
    )

    result = run(script, tmp_path)

    assert result.exit_code == 0
    assert result.reason is None
    assert result.timed_out is False
    assert result.ownership == "windows_job_object"
    assert (tmp_path / "out.log").read_bytes() == b"PASS on stdout" + LINE
    assert (tmp_path / "err.log").read_bytes() == b"PASS on stderr" + LINE
    assert result.launched is True
    assert result.to_json()["process"] == {
        "pid": result.pid,
        "ownership": "windows_job_object",
        "exit_code": 0,
        "timed_out": False,
    }


def test_nonzero_exit_reports_that_code(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "fail.py",
        "import sys\n"
        "sys.stderr.write('boom\\n')\n"
        "sys.exit(23)\n",
    )

    result = run(script, tmp_path)

    assert result.exit_code == 23
    assert result.reason is None
    assert result.timed_out is False
    assert (tmp_path / "err.log").read_bytes() == b"boom" + LINE


def wait_until_dead(pid: int, *, timeout: float = 15.0) -> bool:
    """Whether a pid stops naming a process, within `timeout`.

    Containment is asynchronous: `TerminateJobObject` asks the kernel to stop
    the tree, and the processes are gone by the time the request returns on an
    idle machine and not on a loaded one. A test that asks once is measuring the
    machine's idle-ness, not the job object's reach, and it fails intermittently
    for exactly the reason it is most needed -- when six other things are
    running. So the question is asked until it has an answer.

    The conclusion is the same either way: a process still running after the
    timeout is a failure, and one that stops is the containment the plan
    required.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.05)
    return not is_alive(pid)


def test_timeout_kills_child_and_grandchild(tmp_path: Path) -> None:
    pids_file = tmp_path / "pids.json"
    grandchild = write_script(
        tmp_path,
        "grandchild.py",
        "import time\ntime.sleep(120)\n",
    )
    child = write_script(
        tmp_path,
        "child.py",
        "import json, os, subprocess, sys, time\n"
        f"grandchild = subprocess.Popen([{PYTHON!r}, {str(grandchild)!r}], "
        "creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)\n"
        f"open({str(pids_file)!r}, 'w').write(json.dumps("
        "{'child': os.getpid(), 'grandchild': grandchild.pid}))\n"
        "time.sleep(120)\n",
    )

    result = run(child, tmp_path, timeout=4.0)

    assert result.timed_out is True
    assert result.exit_code is None

    recorded = json.loads(pids_file.read_text(encoding="utf-8"))
    assert recorded["grandchild"] != recorded["child"]
    assert wait_until_dead(recorded["child"]), (
        f"the child at pid {recorded['child']} survived the job object that owned it"
    )
    assert wait_until_dead(recorded["grandchild"]), (
        f"the grandchild at pid {recorded['grandchild']} survived: a child-only kill would "
        "leave it running, so this is the assertion that distinguishes containment "
        "from killing the direct child"
    )


def test_large_output_on_both_streams_does_not_deadlock(tmp_path: Path) -> None:
    chunk = "x" * 1023 + "\n"
    iterations = 5 * 1024
    tally = tmp_path / "tally.json"
    script = write_script(
        tmp_path,
        "loud.py",
        "import json, sys\n"
        f"chunk = {chunk!r}\n"
        f"for _ in range({iterations}):\n"
        "    sys.stdout.write(chunk)\n"
        "    sys.stderr.write(chunk)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
        f"json.dump({{'chars': len(chunk) * {iterations}, 'lines': {iterations}}},"
        f"open({str(tally)!r}, 'w'))\n",
    )

    started = time.monotonic()
    result = run(script, tmp_path, timeout=180.0)
    elapsed = time.monotonic() - started

    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.reason is None
    assert elapsed < 180.0

    reported = json.loads(tally.read_text(encoding="utf-8"))
    assert reported["chars"] == 5 * 1024 * 1024
    assert reported["lines"] == 5 * 1024

    for name in ("out.log", "err.log"):
        size = (tmp_path / name).stat().st_size
        assert size >= reported["chars"], f"{name} lost bytes: {size}"

    print(f"10 MiB across both streams in {elapsed:.2f}s")


def test_paths_with_space_and_non_ascii_round_trip(tmp_path: Path) -> None:
    directory = tmp_path / "répertoire ünïcødé" / "dossier interne"
    directory.mkdir(parents=True)
    report = tmp_path / "rapport.json"
    script = write_script(
        directory,
        "chemin.py",
        "import json, os, sys\n"
        f"json.dump({{'cwd': os.getcwd(), 'argv0': sys.argv[0]}},"
        f"open({str(report)!r}, 'w', encoding='utf-8'))\n",
    )

    result = run_command(
        [PYTHON, str(script), "argument ünïcødé"],
        cwd=directory,
        stdout_path=directory / "sortie ünïcødé.log",
        stderr_path=directory / "err ünïcødé.log",
        timeout_seconds=60.0,
    )

    assert result.exit_code == 0
    assert json.loads(report.read_text(encoding="utf-8"))["cwd"] == str(directory)
    assert str(directory) in str(result.cwd)
    assert result.stdout_path.name == "sortie ünïcødé.log"
    assert result.stdout_path.is_file()
    assert result.stderr_path.is_file()


def test_cmd_launcher_runs_its_interpreter(tmp_path: Path) -> None:
    """A .cmd is not an executable, and the manifest may still list one.

    Plan 01 calls .cmd launchers a deliberate Windows case, so the interpreter has
    to be reached somehow. This is the plainest shape: a launcher under an ASCII
    path with no space in it.
    """
    directory = tmp_path / "launcher dir"
    directory.mkdir()
    helper = write_script(
        directory,
        "helper.py",
        "import sys\n"
        "sys.stdout.write('ran through the launcher\\n')\n",
    )
    launcher = directory / "run it.cmd"
    launcher.write_text(
        f'@echo off\r\n"{PYTHON}" "{helper}" %*\r\n', encoding="ascii", newline=""
    )

    result = run_command(
        [str(launcher), "plain-arg"],
        cwd=directory,
        stdout_path=directory / "out.log",
        stderr_path=directory / "err.log",
        timeout_seconds=60.0,
    )

    assert result.exit_code == 0
    assert (directory / "out.log").read_bytes() == b"ran through the launcher" + LINE


def test_a_cmd_launcher_works_from_any_repository_path(tmp_path: Path) -> None:
    """The launcher path is not restricted to ASCII, or no space.

    `procs` used to reach a batch file through `cmd.exe /c <launcher> ...`. That
    wraps the launcher path in a second pair of quotes which `/c` then strips,
    leaving the remainder to be re-split at the first space, so any launcher path
    containing a space was cut in half and reported as an unrecognized command.
    Naming the launcher directly and letting CreateProcess route it to the
    interpreter avoids the second quoting layer entirely.

    The space is the trigger. Measured on this host through the pre-fix command
    line, a non-ASCII directory with no space in it worked, and a plain ASCII
    directory containing a space did not. Both are below, because a suite that
    only tested the non-ASCII names would have let the defect through again.

    Every directory name here is one a real repository could have. The payload
    reports the argument it actually received, so a launcher that ran the wrong
    thing cannot pass.
    """
    non_ascii_argument = "argument ünïcødé"
    for name in (
        "plain", "withspace", "répertoire", "ünïcødé",
        "with space", "répertoire ünïcødé", "ünïcødé with space",
    ):
        directory = tmp_path / name
        directory.mkdir()
        helper = write_script(
            directory,
            "helper.py",
            "import json, os, pathlib, sys\n"
            "pathlib.Path('seen.json').write_text(json.dumps(\n"
            "    {'argv': sys.argv[1:], 'cwd': os.getcwd()}, ensure_ascii=False),\n"
            "    encoding='utf-8')\n",
        )
        launcher = directory / "lancer.cmd"
        launcher.write_text(
            f'@echo off\r\n"{PYTHON}" "%~dp0helper.py" %*\r\n', encoding="ascii", newline=""
        )
        seen = directory / "seen.json"

        result = run_command(
            [str(launcher), non_ascii_argument],
            cwd=directory,
            stdout_path=directory / "out.log",
            stderr_path=directory / "err.log",
            timeout_seconds=60.0,
        )

        assert result.exit_code == 0, (
            f"a launcher under {name!r} exited {result.exit_code}: "
            f"{(directory / 'err.log').read_text(encoding='utf-8', errors='replace')[:200]!r}"
        )
        assert seen.is_file(), f"the payload never ran for {name!r}"
        observed = json.loads(seen.read_text(encoding="utf-8"))
        assert observed["argv"] == [non_ascii_argument], (
            f"from {name!r} the payload received {observed['argv']!r}, not the "
            f"argument the manifest supplied"
        )
        assert observed["cwd"] == str(directory), (
            f"from {name!r} the working directory was mangled"
        )


def test_a_non_ascii_path_inside_a_cmd_launcher_is_mangled(tmp_path: Path) -> None:
    """The documented limit, demonstrated rather than restated, and it is real.

    This is the half that cannot be fixed from here. `cmd.exe` reads a batch
    file's own bytes in the active ANSI code page, so a non-ASCII path written
    *into* the file is read back as different characters. It is not vkit's
    command line, which CreateProcess hands over in Unicode and which the
    previous test proves arrives intact; it is the file `cmd.exe` then parses.

    The code page is forced to 437, whose repertoire is ASCII plus a little.
    What decides the outcome is whether that code page can represent the
    character, not whether the path is non-ASCII. Measured on this host with
    the console at 437:

      ASCII payload path                payload ran, exit 0
      payload path containing 'é'       payload never ran, exit 2

    'é' is inside cp1252, this machine's ANSI code page, and inside UTF-8,
    which is what the batch file is written in. It is outside cp437. So the
    limit is not "non-ASCII mangles" but "a character the active code page
    cannot represent mangles". The control test beside this one runs the same
    shape under the host's own code page and passes, so the pair states the
    limit rather than a correlation.

    The payload therefore never runs. The check exits nonzero, writes no
    artifact, and the run is BLOCKED with `artifact_missing`. A failed launcher
    does not read as a passing check, which is what makes this a limit to
    document rather than a defect to fix.
    """
    import ctypes

    directory = tmp_path / "répertoire"
    directory.mkdir()
    helper = write_script(
        directory,
        "générateur.py",
        "import pathlib\n"
        "pathlib.Path('ran.txt').write_text('ran', encoding='utf-8')\n",
    )
    launcher = directory / "lancer.cmd"
    launcher.write_bytes(f'@echo off\r\n"{PYTHON}" "{helper}"\r\n'.encode("utf-8"))

    kernel32 = ctypes.windll.kernel32
    original = kernel32.GetConsoleOutputCP()
    if not original:
        pytest.skip(
            "no console is attached, so there is no active code page to force "
            "and nothing here could demonstrate the limit"
        )
    kernel32.SetConsoleOutputCP(437)
    try:
        result = run_command(
            [str(launcher)],
            cwd=directory,
            stdout_path=directory / "out.log",
            stderr_path=directory / "err.log",
            timeout_seconds=60.0,
        )
        stderr = (directory / "err.log").read_text(encoding="utf-8", errors="replace")
    finally:
        kernel32.SetConsoleOutputCP(original)

    assert result.exit_code != 0, "the payload should not have run"
    assert not (directory / "ran.txt").exists(), "the payload ran after all"
    assert "can't open file" in stderr, f"expected an interpreter error, got {stderr!r}"
    assert "rateur.py" in stderr, (
        f"expected the mangled name to keep its ASCII tail, got {stderr!r}"
    )


def test_the_same_launcher_works_under_the_hosts_own_code_page(tmp_path: Path) -> None:
    """The control, so the test above cannot read as "non-ASCII always fails".

    Identical launcher, identical payload path, the code page this host actually
    runs. The payload path here is ASCII, because that is the case a maintainer
    on this machine actually hits.

    An earlier version of this test used the same non-ASCII payload as the test
    above and asserted it worked, with the reasoning that the host's own code
    page could represent it. Measured, that is false here: the OEM console code
    page is 437, which has no `é`. The test failed for the right reason -- the
    launch is correct and the payload genuinely cannot be named -- and the
    docstring's claim was the thing that was wrong. A control that cannot run on
    the host it was written for is not a control, it is a second copy of the
    failure.
    """
    directory = tmp_path / "withspace"
    directory.mkdir()
    helper = write_script(
        directory,
        "generator.py",
        "import pathlib\n"
        "pathlib.Path('ran.txt').write_text('ran', encoding='utf-8')\n",
    )
    launcher = directory / "lancer.cmd"
    launcher.write_bytes(f'@echo off\r\n"{PYTHON}" "{helper}"\r\n'.encode("utf-8"))

    result = run_command(
        [str(launcher)],
        cwd=directory,
        stdout_path=directory / "out.log",
        stderr_path=directory / "err.log",
        timeout_seconds=60.0,
    )

    assert result.exit_code == 0, (
        f"under a code page that can represent the path the launcher must run; "
        f"its stderr was "
        f"{(directory / 'err.log').read_text(encoding='utf-8', errors='replace')[:200]!r}"
    )
    assert (directory / "ran.txt").is_file(), "the payload did not run"


def test_missing_executable_reports_launch_failure(tmp_path: Path) -> None:
    result = run_command(
        [str(tmp_path / "no-such-binary-anywhere")],
        cwd=tmp_path,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=30.0,
    )

    assert result.launched is False
    assert result.pid is None
    assert result.exit_code is None
    assert result.timed_out is False
    assert result.reason.value == "launch_failed"
    assert "cannot find the file" in result.detail
    assert "process" not in result.to_json()
    assert (tmp_path / "out.log").is_file()
    assert (tmp_path / "err.log").is_file()


def test_suspended_child_never_runs_when_assignment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment failure must stop the command, not follow it.

    Refusing the job assignment is the one failure that happens while the child
    exists but has not executed an instruction. Resuming it anyway would be the
    uncontrolled work the design forbids, so the test forces the refusal and
    checks the child's marker file was never created.
    """
    import pywintypes
    import win32job

    marker = tmp_path / "should-not-exist.txt"
    script = write_script(
        tmp_path,
        "would_run.py",
        "import sys\n"
        f"open({str(marker)!r}, 'w').write('ran')\n",
    )

    def refuse(job, process_handle):
        raise pywintypes.error(5, "AssignProcessToJobObject", "Access is denied.")

    monkeypatch.setattr(win32job, "AssignProcessToJobObject", refuse)

    result = run(script, tmp_path, timeout=30.0)

    assert result.launched is False
    assert result.reason.value == "launch_failed"
    assert "Access is denied" in result.detail
    assert not marker.exists()

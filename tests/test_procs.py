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

# conftest.py puts this checkout's src ahead of the editable install, so the
# module under test is this worktree's. Assert it rather than trust it: a gate
# that silently measures another owner's code is a false pass.
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

# The interpreter under test, not a bare "python". A check runs under the venv
# that installed vkit, so a helper launched from the system interpreter would
# test a different environment than the one under use.
PYTHON = sys.executable

# WinError 87 is what OpenProcess returns for a pid with no live process behind
# it once the kernel has torn the object down. The test treats any failure to
# open as dead and this constant is only used to prove the failure is the
# expected kind rather than a permissions problem.
WINERROR_INVALID_PARAMETER = 87

# A child writing to a pipe or file in text mode gets the C runtime's newline
# translation, so "one line" is two bytes on Windows. The bytes under test are
# the child's own; this module neither rewrites nor decodes them, and a test that
# expected a bare LF would be asserting a translation this module deliberately
# does not perform.
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

    # Not 1 and not None: the exact code the OS recorded, and no reason, because
    # a command that ran and failed is a result and not a blocked run.
    assert result.exit_code == 23
    assert result.reason is None
    assert result.timed_out is False
    assert (tmp_path / "err.log").read_bytes() == b"boom" + LINE


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
        f"grandchild = subprocess.Popen([{PYTHON!r}, {str(grandchild)!r}])\n"
        f"open({str(pids_file)!r}, 'w').write(json.dumps("
        "{'child': os.getpid(), 'grandchild': grandchild.pid}))\n"
        "time.sleep(120)\n",
    )

    result = run(child, tmp_path, timeout=4.0)

    assert result.timed_out is True
    # The job was told to exit 1, so a bare 1 here would be indistinguishable
    # from a command that chose to fail. None is what makes a timeout readable.
    assert result.exit_code is None

    recorded = json.loads(pids_file.read_text(encoding="utf-8"))
    assert recorded["grandchild"] != recorded["child"]
    assert is_alive(recorded["child"]) is False
    assert is_alive(recorded["grandchild"]) is False


def test_large_output_on_both_streams_does_not_deadlock(tmp_path: Path) -> None:
    chunk = "x" * 1023 + "\n"
    iterations = 5 * 1024  # 5 MiB per stream
    # The child counts the characters it handed to each stream and reports the
    # totals. Whether the C runtime turns a newline into two bytes on the way to
    # the file depends on where its buffer boundaries fall, and that is the
    # child's business, not this module's: the module neither rewrites nor
    # decodes a byte. Asserting a byte count derived from the source text would
    # be asserting the CRT. The invariant under test is that everything the child
    # wrote reached the file, on both streams, without the run stalling.
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

    # 5 MiB per stream. A pipe would have blocked on the first 64 KB and never
    # reached these assertions; the run would have hit the timeout instead.
    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.reason is None
    assert elapsed < 180.0

    reported = json.loads(tally.read_text(encoding="utf-8"))
    assert reported["chars"] == 5 * 1024 * 1024
    assert reported["lines"] == 5 * 1024

    # Every character arrived. Text-mode translation may add a carriage return
    # per line, so the file is at least the character count, and never short.
    for name in ("out.log", "err.log"):
        size = (tmp_path / name).stat().st_size
        assert size >= reported["chars"], f"{name} lost bytes: {size}"

    # Nothing accumulated in this process. The bytes went child -> kernel ->
    # file, so the parent's own memory did not grow with the child's output. The
    # elapsed time is printed rather than asserted on a tight bound so a slow
    # host reports itself instead of flaking.
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
    # The cwd the child saw is the directory this test created, character for
    # character. A lossy round trip through an ANSI code page would show mojibake
    # here, and a shell wrapper would show a different path entirely.
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
    containing a space or a non-ASCII character was cut in half and reported as
    an unrecognized command. Naming the launcher directly and letting
    CreateProcess route it to the interpreter avoids the second quoting layer
    entirely.

    Every directory name here is one a real repository could have. The payload
    reports the argument it actually received, so a launcher that ran the wrong
    thing cannot pass.
    """
    non_ascii_argument = "argument ünïcødé"
    for name in ("plain", "with space", "répertoire ünïcødé", "ünïcødé with space"):
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
        # %~dp0 rather than the payload's own name, so the batch file's bytes are
        # ASCII and this test is about the launch, not about the batch contents.
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

    The code page is forced to 437 for the child, because this host's console
    runs at 65001 and would read the UTF-8 batch file correctly. That is the
    honest shape of the limit: it depends on the code page the user's shell
    happens to be running, which is precisely why it cannot be detected from
    inside a repository and has to be a documented boundary instead.

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
    # The non-ASCII payload path is inside the batch file. UTF-8 is what an
    # editor on this host writes; the question is what cmd.exe then reads.
    launcher.write_bytes(f'@echo off\r\n"{PYTHON}" "{helper}"\r\n'.encode("utf-8"))

    kernel32 = ctypes.windll.kernel32
    original = kernel32.GetConsoleOutputCP()
    if not original:
        pytest.skip(
            "no console is attached, so there is no active code page to force "
            "and nothing here could demonstrate the limit"
        )
    # 437 is the OEM code page for the United States, and it cannot represent
    # any character in this payload. Restored in a finally, because leaving the
    # console on a legacy code page would corrupt this session's own output.
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
    # The ASCII tail of the name survives, which is what makes this a code page
    # round trip rather than a missing file.
    assert "rateur.py" in stderr, (
        f"expected the mangled name to keep its ASCII tail, got {stderr!r}"
    )


def test_missing_executable_reports_launch_failure(tmp_path: Path) -> None:
    result = run_command(
        [str(tmp_path / "no-such-binary-anywhere")],
        cwd=tmp_path,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=30.0,
    )

    # Nothing executed, so there is no pid and no exit code to mistake for a
    # failure. A report derived from this has to say BLOCKED, not FAIL.
    assert result.launched is False
    assert result.pid is None
    assert result.exit_code is None
    assert result.timed_out is False
    assert result.reason.value == "launch_failed"
    assert "cannot find the file" in result.detail
    assert "process" not in result.to_json()
    # Both logs exist even though nothing ran, so a report can cite them.
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
    # The process was created suspended and terminated without being resumed, so
    # it never reached the line that would have created the file.
    assert not marker.exists()

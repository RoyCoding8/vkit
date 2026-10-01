"""Behavior tests for process identity, driven through the public functions.

Every identity under test belongs to a real process launched from this file, and
every assertion is a literal. Two launch mechanisms appear deliberately and the
difference between them is the point of several tests:

* `subprocess.Popen` keeps its process handle until the object is dropped, which
  is what makes a finished process still readable afterwards.
* `vkit.procs.run_command` releases its handle before returning, so the pid is
  reclaimed the moment the process ends. That is the condition a real run leaves
  behind, and it is the one the gone/cannot-confirm distinction turns on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# conftest.py puts this checkout's src ahead of the editable install, so the
# module under test is this worktree's. Assert it rather than trust it: a gate
# that silently measures another owner's code is a false pass.
_THIS_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()
import vkit.procidentity as _procidentity  # noqa: E402
import vkit.procs as _procs  # noqa: E402

if Path(_procidentity.__file__).resolve() != (_THIS_SRC / "vkit" / "procidentity.py"):
    raise AssertionError(
        f"vkit.procidentity resolved to {_procidentity.__file__}, not this "
        f"worktree's {_THIS_SRC / 'vkit' / 'procidentity.py'}. Refusing to run "
        "the gate against another owner's code."
    )

CannotConfirm = _procidentity.CannotConfirm
ProcessIdentity = _procidentity.ProcessIdentity
is_alive = _procidentity.is_alive
openprocess_failure_is_gone = _procidentity.openprocess_failure_is_gone
read_identity = _procidentity.read_identity
still_the_same_process = _procidentity.still_the_same_process

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the identity is the Windows creation FILETIME; no POSIX equivalent is established",
)

# The interpreter that launched this test file. A run spawns the venv's
# interpreter, so the identity recorded here is one a real run could record.
PYTHON = sys.executable

# The code Windows reports for a process that has not exited. An exited process
# reports the code it exited with instead, and that difference is the only thing
# separating a running process from a finished one that is still readable.
STILL_ACTIVE = 259

# Upper bound on waiting for a process this file launched to finish. It is not
# the expected wait; it exists so a wedged process fails a test rather than
# hanging the gate.
PROCESS_TIMEOUT = 30.0

# A file time Windows has not yet issued as of the year this was written. A FILETIME
# counts 100ns ticks since 1601, and the current moment is about 1.34e17. A zero or
# a value at this magnitude would mean the call reported success without writing
# the real time, so the assertion below is on the number, not just on equality.
MIN_PLAUSIBLE_CREATION_TIME = 133_000_000_000_000_000


def _write_script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _kernel_creation_time(pid: int) -> int:
    """The kernel's own creation FILETIME for `pid`, read here not via the module.

    Two independent paths to one number. Asserting the module's value equals
    this one proves the module read the file time itself, and it does so
    deterministically: a read that routed the value through a float disagrees
    with the kernel on every value a float cannot hold exactly, whereas a test
    that merely asserted "this number is not float-representable" would pass by
    luck about fifteen times in sixteen.
    """
    import ctypes
    from ctypes import wintypes

    import win32api
    import win32con

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            ctypes.c_void_p(int(handle)),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        assert ok, "the test's own GetProcessTimes call failed"
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        handle.Close()


def _launch_live(tmp_path: Path, name: str, body: str) -> int:
    """Run a script through vkit.procs and return its pid, holding no handle.

    `run_command` blocks until the command finishes, so this returns a pid whose
    process has already ended and whose process object is already released. That
    is the real shape of a run record read back later, and it is the case the
    gone/cannot-confirm distinction is about.
    """
    script = _write_script(tmp_path, name, body)
    result = _procs.run_command(
        [PYTHON, str(script)],
        cwd=tmp_path,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        timeout_seconds=PROCESS_TIMEOUT,
    )
    assert result.pid is not None, f"{name} never launched: {result.detail}"
    return result.pid


def _launch_sleeping(tmp_path: Path, name: str) -> tuple[subprocess.Popen, int]:
    """A process that is still running, still openable, and still ours.

    A throwaway interpreter runs first so the venv's import cost is paid before
    the process under test is started, which is what keeps the child comfortably
    inside its own 300 second sleep.

    The window between `Popen` returning and the first read is not closed. A
    child that died in it would make the test fail on its read rather than on
    the assertion, which reads as a defect in the module under test. That has
    not happened on this host; a failure there is worth reporting rather than
    retrying.
    """
    script = _write_script(tmp_path, name, "import time\ntime.sleep(300)\n")
    warmup = subprocess.Popen(
        [PYTHON, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    warmup.wait(timeout=PROCESS_TIMEOUT)
    del warmup

    process = subprocess.Popen(
        [PYTHON, str(script)],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process, process.pid


# ------------------------------------------------------------- the live child


def test_live_child_identity_reads_and_is_stable_across_a_reopen(tmp_path: Path) -> None:
    """One pid, one creation time, however many handles are opened on it.

    This is the property a reattaching supervisor depends on: it has no handle
    from the launch moment, so an identity that shifted when the handle closed
    would report every live process as gone.
    """
    process, pid = _launch_sleeping(tmp_path, "sleeper.py")
    try:
        first = read_identity(pid)
        second = read_identity(pid)
        third = read_identity(pid)

        # Three separate opens, each with its own handle opened and closed by
        # read_identity. Equal identity, not merely an equal pid.
        assert first is not None and second is not None and third is not None
        assert first == second == third
        assert first.pid == pid
        assert first.creation_time == second.creation_time == third.creation_time
        # The kernel's own file time, not a value this module composed. A zero
        # would mean the call reported success without writing one.
        assert first.creation_time > MIN_PLAUSIBLE_CREATION_TIME
        # And it is the file time itself, with no precision lost on the way. A
        # creation time routed through a float64 comes back as a neighbouring
        # integer, and every later equality then rejects a process that never
        # moved.
        assert first.creation_time == _kernel_creation_time(pid)

        assert still_the_same_process(first) is True
        assert is_alive(first) is True
    finally:
        process.kill()
        process.wait(timeout=PROCESS_TIMEOUT)


def test_exited_process_is_gone_or_fails_the_equality_check(tmp_path: Path) -> None:
    """The property the identity exists for: an exited pid never verifies.

    Both readings are accepted and neither verifies. Windows holds a finished
    process object only while some handle remains, so which one happens is
    decided by handle ownership outside this test. The recorded pair is the
    thing that must stop matching.
    """
    pid = _launch_live(tmp_path, "quitter.py", "raise SystemExit(7)\n")

    current = read_identity(pid)

    if current is None:
        # The pid was reclaimed the moment the last handle closed, so nothing is
        # left that could be the same process.
        assert still_the_same_process(ProcessIdentity(pid, 1)) is False
    else:
        # The object is still open somewhere and can still be measured. It is
        # then a process, and it is not the one that was recorded.
        assert still_the_same_process(
            ProcessIdentity(pid, current.creation_time + 1)
        ) is False
        assert still_the_same_process(current) is True
    assert is_alive(ProcessIdentity(pid, 1)) is False


def test_exited_process_reports_its_exit_code_and_not_still_active(tmp_path: Path) -> None:
    """An exited process is distinguishable from a running one.

    Read while the process object is still held open, which is the only state in
    which the question can be asked: once the last handle closes, the pid is
    gone and there is nothing left to ask about.
    """
    process = subprocess.Popen(
        [PYTHON, _write_script(tmp_path, "quitter.py", "raise SystemExit(7)\n")],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    exit_code = process.wait(timeout=PROCESS_TIMEOUT)

    assert exit_code == 7
    assert exit_code != STILL_ACTIVE

    # The Popen handle is open, so the object and its pid survive the exit.
    identity = read_identity(process.pid)

    assert identity is not None, "the pid read as gone while its handle was still open"
    assert identity.pid == process.pid
    assert identity.creation_time > MIN_PLAUSIBLE_CREATION_TIME
    # Same pid, same creation time, and yet not running. This is the case that
    # makes is_alive a function of its own: the identity cannot answer it.
    assert still_the_same_process(identity) is True
    assert is_alive(identity) is False


# ------------------------------------------------------------------- the pairs


def test_a_live_pid_with_a_wrong_creation_time_does_not_verify(tmp_path: Path) -> None:
    """The pid matches and the pair still fails, which is the whole design.

    A bare-pid check passes this. It is the one test that separates an identity
    from a number.
    """
    process, pid = _launch_sleeping(tmp_path, "sleeper.py")
    try:
        identity = read_identity(pid)
        assert identity is not None

        fabricated = ProcessIdentity(identity.pid, identity.creation_time + 1)

        # One tick past the real one. The pid is bit-for-bit the live one.
        assert fabricated.pid == identity.pid
        assert fabricated.creation_time == identity.creation_time + 1
        assert still_the_same_process(fabricated) is False
        assert still_the_same_process(identity) is True
    finally:
        process.kill()
        process.wait(timeout=PROCESS_TIMEOUT)


def test_two_live_processes_never_share_a_creation_time(tmp_path: Path) -> None:
    """Distinct processes are distinct identities, not two views of one number.

    Worth pinning alone because it is the assumption the equality rests on. If
    two live processes could report one creation time, the equality could not
    separate them and a stale record would verify against a stranger.
    """
    first_process, first_pid = _launch_sleeping(tmp_path, "one.py")
    second_process, second_pid = _launch_sleeping(tmp_path, "two.py")
    try:
        first = read_identity(first_pid)
        second = read_identity(second_pid)
        assert first is not None and second is not None

        assert first.pid != second.pid
        assert first.creation_time != second.creation_time
        assert still_the_same_process(first) is True
        assert still_the_same_process(second) is True
        # Crossed: each live process rejects the other one's creation time.
        assert still_the_same_process(ProcessIdentity(first_pid, second.creation_time)) is False
        assert still_the_same_process(ProcessIdentity(second_pid, first.creation_time)) is False
    finally:
        for process in (first_process, second_process):
            process.kill()
            process.wait(timeout=PROCESS_TIMEOUT)


def test_this_test_processes_own_identity_verifies() -> None:
    """A process that exists and cannot die verifies.

    A supervisor reattaching with no launch handle reading its own still-running
    process is the case where a broken implementation would be most obvious and
    most likely to be tolerated as an edge case.
    """
    identity = read_identity(os.getpid())

    assert identity is not None
    assert identity.pid == os.getpid()
    assert identity.creation_time > MIN_PLAUSIBLE_CREATION_TIME
    assert still_the_same_process(identity) is True
    assert is_alive(identity) is True


# ---------------------------------------------------------- gone, not unknown


def test_a_pid_with_no_process_reports_not_found_rather_than_raising() -> None:
    """A pid that names nothing is answered, never raised on.

    pid 0 is not a process, and it is also the value a zeroed record carries, so
    it is the pid a truncated or defaulted record is most likely to hold.
    """
    assert read_identity(0) is None
    assert still_the_same_process(ProcessIdentity(0, 1)) is False
    assert is_alive(ProcessIdentity(0, 1)) is False


def test_a_pid_above_the_maximum_process_id_reports_not_found() -> None:
    """A pid Windows could never have issued is answered, not raised on.

    0x7FFFFFF0 is four times the 32-bit maximum process id, and 0xFFFFFFFE is the
    largest value a signed pid field can hold. Both are unreachable, so both
    must come back as not-found.
    """
    assert read_identity(0x7FFF_FFF0) is None
    assert read_identity(0xFFFF_FFFE) is None
    assert still_the_same_process(ProcessIdentity(0xFFFF_FFFE, 1)) is False


def _access_denied_from_openprocess():
    """The error OpenProcess raises when a pid is in use but out of reach.

    Recorded from a real denial rather than invented. On this host
    `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, 4)` raises a
    pywintypes.error whose winerror is 5 and whose strerror is "Access is
    denied.", and the object built below carries those same two fields, which are
    the only two this module reads. Constructed here rather than imported at
    module scope because pywintypes does not exist on a POSIX host, and this file
    is imported during collection on one.
    """
    import pywintypes

    return pywintypes.error(5, "OpenProcess", "Access is denied.")


def _a_pid_this_token_cannot_open() -> int | None:
    """A live pid this process may not open, or None if it may open all of them.

    Enumerated rather than hard-coded to the System process, because whether pid
    4 is readable is a fact about the caller's token and not about this module.
    Measured: a non-privileged token on this host cannot open 175 of 347 live
    pids, while the windows-latest runner can open pid 4.
    """
    import pywintypes
    import win32api
    import win32con
    import win32process

    for pid in win32process.EnumProcesses():
        if pid == 0:
            # The pid a zeroed record carries. It names no process, so it is the
            # one pid whose answer must be None rather than a refusal.
            continue
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
        except pywintypes.error as exc:
            if exc.winerror == 5:
                return pid
        else:
            handle.Close()
    return None


@pytest.fixture
def unreadable_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A pid that is in use, and then made unreadable the way the kernel does it.

    The two halves of the precondition are established separately because they
    are not equally available. In use is real: a process this file launched is
    running, and the assertion below proves the kernel answers a query for its
    pid right now. Unreadable is replayed at the module's single OS seam, because
    a Windows host issues a denial only to a token that lacks the right, and a
    runner holding SeDebugPrivilege is issued none. Without the replay these two
    tests pass on one kind of Windows host and fail on the other, which is a fact
    about the runner rather than about the code under test.

    Everything downstream of the syscall is the real thing. `read_identity` runs
    the module's own `_open`, takes the same branch a kernel denial takes, and
    builds its message from the same error fields the kernel populates.
    """
    import win32api

    process, pid = _launch_sleeping(tmp_path, "unreadable.py")
    try:
        # The pid is in use. Read it once, unpatched, so a pid that named nothing
        # could not pass as one that is in use and out of reach.
        assert read_identity(pid) is not None, "the pid this fixture denies is not in use"

        def refused(access: int, inherit: bool, target: int):
            raise _access_denied_from_openprocess()

        monkeypatch.setattr(win32api, "OpenProcess", refused)
        yield pid
    finally:
        process.kill()
        process.wait(timeout=PROCESS_TIMEOUT)


def test_a_pid_in_use_but_unreadable_is_cannot_confirm(unreadable_pid: int) -> None:
    """A pid that IS in use and cannot be opened is not reported as gone.

    The pid belongs to a live process, so returning None would be a false
    statement about the world. It cannot be read, so there is nothing to measure,
    and the caller is told it cannot confirm, which means it must keep its claim.
    """
    with pytest.raises(CannotConfirm) as caught:
        read_identity(unreadable_pid)

    assert caught.value.pid == unreadable_pid
    assert "Access is denied" in caught.value.detail
    # The two callers answer differently, and the difference is deliberate.
    # still_the_same_process folds the denial into False because the report for
    # "unproven" and "gone" is the same. is_alive raises instead, because False
    # there would be a claim the process is not running and nothing established
    # that. What neither may do is answer True.
    assert still_the_same_process(ProcessIdentity(unreadable_pid, 1)) is False
    with pytest.raises(CannotConfirm):
        is_alive(ProcessIdentity(unreadable_pid, 1))


def test_cannot_confirm_does_not_launder_into_a_different_process(
    unreadable_pid: int,
) -> None:
    """An unreadable identity is not evidence of a different process.

    Asserted through read_identity rather than still_the_same_process, because
    the latter folds this into False by design and would hide the very
    distinction this test keeps visible.
    """
    with pytest.raises(CannotConfirm) as caught:
        read_identity(unreadable_pid)

    message = str(caught.value)
    assert f"cannot read the identity of pid {unreadable_pid}" in message
    # It says the read failed. It does not say the process is a different one,
    # which is a claim nothing established.
    assert "different" not in message


def test_one_classifier_answers_the_gone_question() -> None:
    """87 is gone, and nothing else is.

    This is the whole table, and it is the table `vkit.recover` calls. Two
    modules once each kept their own: `recover` additionally read 6 and 1168 as
    death, which released a claim on a code that never established the process had
    ended. One function is where that question is answered now, and this asserts
    the shape of the answer rather than the four codes anyone remembered: 87 is
    the only one that reads gone, and an unknown code falls on the safe side.

    The unknown code is the load-bearing half. A classifier written as a list of
    codes that mean death would pass every other assertion here and still release
    a claim the day Windows answered with something nobody anticipated.
    """
    gone = openprocess_failure_is_gone

    assert gone(87) is True
    for code in (6, 1168, 5, 1, 9999, 0, -1, None):
        assert gone(code) is False, (
            f"Windows error {code!r} does not establish that no process carries "
            "the pid, so it must not be a basis for releasing a claim"
        )


def test_a_failure_that_does_not_establish_death_is_cannot_confirm(
    unreadable_pid: int,
) -> None:
    """The same rule at this module's own seam, reached through the public API.

    `test_one_classifier_answers_the_gone_question` pins the function. This pins
    that `_open` still routes through it, which is the wiring that would let the
    two answers drift apart again: a reader would see a correct classifier and a
    call site that ignores it.
    """
    with pytest.raises(CannotConfirm) as caught:
        read_identity(unreadable_pid)

    assert caught.value.pid == unreadable_pid
    assert "Access is denied" in caught.value.detail


def test_a_denial_the_kernel_itself_issues_is_cannot_confirm() -> None:
    """The same classification against a denial this host really issues.

    The test above pins `_open` by replaying a recorded kernel error, which is
    the only way to pin it on every Windows host. What a replay cannot show is
    that the kernel's real denial carries the error fields the classification
    reads. This test closes that gap where the host permits it, and it is
    therefore not pinned by a green gate on a host that issues no denial.

    Measured: the windows-latest runner reads pid 4 successfully and so has no
    unreadable pid to hand this module. It is skipped there, not passed. A green
    gate over this file on a privileged Windows host therefore rests on the
    replayed classification alone.
    """
    pid = _a_pid_this_token_cannot_open()
    if pid is None:
        pytest.skip(
            "this token may open every live pid, so the kernel issues no denial "
            "to classify. A token holding SeDebugPrivilege, which the "
            "windows-latest runner's does, has no unreadable pid to offer."
        )

    with pytest.raises(CannotConfirm) as caught:
        read_identity(pid)

    assert caught.value.pid == pid
    assert "Access is denied" in caught.value.detail
    assert still_the_same_process(ProcessIdentity(pid, 1)) is False


# ------------------------------------------------------------- over the wire


def test_to_json_from_json_round_trips_exactly() -> None:
    """A record survives the trip through JSON with both halves intact.

    The real magnitude, not a round number. A FILETIME is about 1.34e17, past
    2**53, so a pipeline that routed this through a float would return a
    neighbouring integer and the equality in still_the_same_process would
    reject a live process.
    """
    identity = ProcessIdentity(pid=47329, creation_time=134352082314085023)

    text = json.dumps(identity.to_json())
    restored = ProcessIdentity.from_json(json.loads(text))

    assert restored == identity
    assert restored.pid == 47329
    assert restored.creation_time == 134352082314085023
    assert identity.creation_time > 2**53
    assert identity.to_json() == {"pid": 47329, "creation_time": 134352082314085023}
    assert text == '{"pid": 47329, "creation_time": 134352082314085023}'


def test_a_record_missing_either_half_is_refused() -> None:
    """A truncated record raises rather than becoming a plausible identity.

    A defaulted creation_time of 0 would compare unequal to every real process,
    so a corrupt file would be reported as "the process we launched is gone" --
    a claim about the world made from a bad file.
    """
    for broken in ({"pid": 47329}, {"creation_time": 1}, {}):
        with pytest.raises(KeyError):
            ProcessIdentity.from_json(broken)


def test_a_recorded_identity_cannot_be_edited() -> None:
    """A recorded identity cannot be turned into a different one.

    A mutable pair is a way to record something false, and nothing in this
    module has a reason to change one once it has been read.
    """
    identity = ProcessIdentity(pid=47329, creation_time=134352082314085023)

    with pytest.raises(Exception):
        identity.creation_time = 1  # type: ignore[misc]


# ------------------------------------------------------------ the POSIX edge


@pytest.mark.skipif(sys.platform == "win32", reason="exercised by the Windows host")
def test_a_non_windows_host_is_refused_rather_than_answered() -> None:
    """Every public function refuses a host where the identity means nothing.

    The POSIX equivalent of a creation time is not established, so there is
    nothing here that could be right. Returning False would be indistinguishable
    from a verification that failed, and returning True from a guess would kill
    a stranger; a named exception is the only answer that cannot be mistaken for
    either.

    This is skipped on Windows, and it is therefore NOT pinned by a green gate
    here. Removing the guard leaves every other test in this file passing;
    measured, by breaking it and running the suite. The rule is only enforced
    where it can be observed, which is on a POSIX host running this file.
    """
    for call in (
        lambda: read_identity(os.getpid()),
        lambda: still_the_same_process(ProcessIdentity(1, 1)),
        lambda: is_alive(ProcessIdentity(1, 1)),
    ):
        with pytest.raises(_procidentity.UnsupportedPlatform) as caught:
            call()
        assert "POSIX equivalent is not established" in str(caught.value)

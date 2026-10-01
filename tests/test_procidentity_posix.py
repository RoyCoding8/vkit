"""The POSIX process identity, driven against real processes on a POSIX host.

tests/test_procidentity.py covers the Windows pair. The POSIX pair is
(pid, /proc/<pid>/stat field 22, boot_id), and this file is what establishes
that it is an identity rather than a second number that happens to be readable.

The test that matters is the one about a RECYCLED pid. A pid is not reused while
the process holding it is live, so two live processes never share one. What has
to hold is that a reused pid arrives with a different start time, because a
recorded pair that matches a stranger is how a cancellation kills someone else's
process. That case is produced here from sibling pid namespaces rather than
assumed: on a live host the counter would have to wrap past pid_max, which is
4194304.

Run:  bash scripts/posix-run.sh -m pytest tests/test_procidentity_posix.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import requires_pid_namespace_for_reuse, requires_procfs  # noqa: E402

from vkit.procidentity import (  # noqa: E402
    CannotConfirm,
    ProcessIdentity,
    boot_id,
    is_alive,
    read_identity,
    still_the_same_process,
)

THIS_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()
import vkit.procidentity as _pi  # noqa: E402

if Path(_pi.__file__).resolve() != (THIS_SRC / "vkit" / "procidentity.py"):
    raise AssertionError(
        f"vkit.procidentity resolved to {_pi.__file__}, not this worktree's "
        f"{THIS_SRC / 'vkit' / 'procidentity.py'}. Refusing to run the gate "
        "against another owner's code."
    )

#: Two reasons this file does not run, kept separate because they are different
#: claims. The first is ownership: on Windows the pair is a FILETIME and
#: tests/test_procidentity.py covers it, so running these would assert the POSIX
#: mechanism where there is none. The second is a missing facility: macOS is
#: POSIX, so the first condition passes and the file ran there, and every test
#: then failed reading a /proc that does not exist. Only the second is a defect
#: in how this file was gated; the first is the arrangement working.
requires_windows_counterpart = pytest.mark.skipif(
    sys.platform == "win32",
    reason="this covers the POSIX identity; the Windows FILETIME pair is covered "
           "by tests/test_procidentity.py",
)

pytestmark = [requires_windows_counterpart, requires_procfs]

HERE = Path(__file__).resolve().parent.parent / "scripts" / "report_identity.py"


def live_process() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    time.sleep(0.3)
    return proc


def test_a_live_process_has_a_readable_identity() -> None:
    proc = live_process()
    try:
        identity = read_identity(proc.pid)
        assert identity is not None
        assert identity.pid == proc.pid
        assert identity.boot_id == boot_id()
        # A start time is ticks since boot, so it cannot exceed the machine's
        # uptime in ticks. This is the check that catches an off-by-one reading
        # of the stat field, which would return vsize and look plausible.
        uptime_ticks = float(Path("/proc/uptime").read_text().split()[0]) * os.sysconf("SC_CLK_TCK")
        assert 0 < identity.creation_time <= uptime_ticks, (
            f"starttime {identity.creation_time} is outside 0..{uptime_ticks:.0f}, "
            "so the field index is probably wrong"
        )
    finally:
        proc.kill()
        proc.wait()


def test_a_mismatched_start_time_is_a_stranger_not_our_process() -> None:
    """The direction of error that matters most.

    A record whose start time belongs to some other process must not match. If it
    did, `still_the_same_process` would authorise signalling a process that never
    ran the check.
    """
    proc = live_process()
    try:
        real = read_identity(proc.pid)
        assert real is not None
        stranger = ProcessIdentity(proc.pid, real.creation_time + 1, real.boot_id)
        assert still_the_same_process(stranger) is False
        # The pid IS alive, which is a different fact and is why the answer is
        # False rather than a death: the report must not drift toward "finished".
        assert is_alive(stranger) is True
    finally:
        proc.kill()
        proc.wait()


def test_a_record_from_another_boot_is_a_stranger() -> None:
    """A tick count is only meaningful within one boot.

    WSL restarts the init namespace, so a process that outlives a restart can
    carry a start time a later process also carries. Same pid and same tick
    count, different boot, so the pair must not match.
    """
    proc = live_process()
    try:
        real = read_identity(proc.pid)
        assert real is not None
        other_boot = ProcessIdentity(proc.pid, real.creation_time, "some-other-boot")
        assert still_the_same_process(other_boot) is False
    finally:
        proc.kill()
        proc.wait()


def test_an_exited_process_reads_as_gone() -> None:
    """A pid nothing holds is None, not an unreadable record.

    The kernel removes /proc/<pid> at exit, before the number can be reused, so
    the gone case is the same shape as the Windows one.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    time.sleep(0.5)
    assert read_identity(pid) is None
    assert still_the_same_process(ProcessIdentity(pid, 1, boot_id())) is False
    assert is_alive(ProcessIdentity(pid, 1, boot_id())) is False


def test_a_pid_above_the_maximum_reads_as_gone() -> None:
    pid_max = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    assert read_identity(pid_max + 1000) is None


@pytest.mark.parametrize("sentinel", [0, -1])
def test_a_pid_that_names_no_single_process_is_refused(sentinel: int) -> None:
    """`kill(0, sig)` addresses the caller's process group, not a process.

    Measured on this host: `os.kill(0, 0)` returned without error for any caller,
    so a signal-zero probe on pid 0 proves nothing. Answering it would report a
    process group as a process. CannotConfirm is the honest refusal, and it is
    the direction that keeps a claim.
    """
    with pytest.raises(CannotConfirm):
        read_identity(sentinel)


@requires_pid_namespace_for_reuse
def test_a_recycled_pid_carries_a_new_start_time() -> None:
    """The case the whole module exists for, produced for real.

    Sibling pid namespaces each restart their counter near 2, so the Nth
    namespace's 5th process is the same kernel pid as the first namespace's 5th
    process, without a live host ever wrapping past pid_max. If the pair could
    not tell those apart, a recorded identity would answer to a stranger.

    The namespace is required, not preferred: `pid_max` is 4194304, so without
    one the only way to reach a reuse is to spawn four million processes, which
    is not a test. The gate is therefore on the CAPABILITY (can this host
    create a pid namespace), and the skip reason names which half was missing --
    no `unshare` binary at all, or the binary present and the syscall refused,
    which is a container that dropped CAP_SYS_ADMIN. Those are different
    problems for whoever reads the log, and "unshare is unavailable" would
    have been wrong for the second.
    """
    assert HERE.is_file(), f"{HERE} is missing, so reuse cannot be produced"

    def collect() -> list[tuple[int, int]]:
        code = (
            "import subprocess, sys\n"
            f"child = {str(HERE)!r}\n"
            "for i in range(12):\n"
            "    subprocess.run([sys.executable, child, 'n%d' % i, '0.0'],\n"
            "                   check=True, stdout=None)\n"
        )
        done = subprocess.run(
            ["unshare", "--fork", "--pid", "--mount-proc", sys.executable, "-c", code],
            capture_output=True, text=True, timeout=600,
        )
        assert done.returncode == 0, f"unshare failed: {done.stderr[-800:]}"
        rows = []
        for line in done.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1])))
        return rows

    first = collect()
    second = collect()
    assert first and second, "no processes were observed, so nothing was established"

    by_pid: dict[int, list[int]] = {}
    for pid, starttime in [*first, *second]:
        by_pid.setdefault(pid, []).append(starttime)

    reused = {pid: times for pid, times in by_pid.items() if len(times) > 1}
    assert reused, (
        f"no pid was recycled across {len(first) + len(second)} processes in two "
        "namespaces, so the reuse case was not established and this test proves "
        "nothing. Raise the per-namespace count until the counter wraps."
    )
    # The load-bearing assertion: no pid ever came back with a start time another
    # process at that pid already had.
    collisions = {pid: times for pid, times in reused.items() if len(set(times)) != len(times)}
    assert not collisions, (
        f"these pids were recycled AND repeated a start time, so a recorded pair "
        f"would match a stranger: {collisions}"
    )
    differing = sum(1 for times in reused.values() if len(set(times)) > 1)
    assert differing == len(reused), (
        f"{len(reused)} pids were reused but only {differing} carried a new start time"
    )


def test_the_pair_round_trips_through_json() -> None:
    """A record read back must still match, or a reattach reports a stranger."""
    proc = live_process()
    try:
        identity = read_identity(proc.pid)
        assert identity is not None
        restored = ProcessIdentity.from_json(identity.to_json())
        assert restored == identity
        assert still_the_same_process(restored) is True
    finally:
        proc.kill()
        proc.wait()


def test_a_windows_shaped_record_carries_no_boot_id() -> None:
    """The two platforms' records are not interchangeable, and must not be.

    A FILETIME and a tick count are different numbers, so a record written on
    one platform cannot be read on the other. That is deliberate: comparing them
    would be comparing a pid against a stranger.
    """
    assert "boot_id" not in ProcessIdentity(1, 2).to_json()
    # A record with no boot_id must not match a POSIX one that has a boot, which
    # is the direction that keeps a claim rather than releasing it.
    proc = live_process()
    try:
        identity = read_identity(proc.pid)
        assert identity is not None
        legacy = ProcessIdentity(identity.pid, identity.creation_time)
        assert still_the_same_process(legacy) is False
    finally:
        proc.kill()
        proc.wait()


def test_a_record_missing_either_half_raises() -> None:
    """A truncated record must not become a value that matches nothing.

    `creation_time=0` by default would compare unequal to every real process and
    read as "the process we launched is gone", which is a report about the world
    made from a corrupt file.
    """
    for document in ({"pid": 1}, {"creation_time": 1}):
        with pytest.raises(KeyError):
            ProcessIdentity.from_json(document)

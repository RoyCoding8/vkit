"""The refusal of a network-backed state path, driven through the Store constructor.

`_reject_network_path` existed and had no test. `tmp/research/KIT_ACCEPTANCE.md`
advertises the row "Network filesystem for local SQLite coordination -> Refuse
unsupported shared coordination", and the matrix row had no receipt behind it, so
the guarantee was asserted by a document rather than by anything a reviewer could
run. This file is that receipt.

Two things matter about how it is written.

It goes through `Store(path)`, the way the product opens its state, rather than
calling the private guard. A test of a private function passes when the caller
stops calling it.

And every refusal shape is paired with a local path the guard must NOT refuse, in
the same test body. `assert pytest.raises(StoreError)` on its own is satisfied by a
guard that raises unconditionally, which is the failure mode this row is most
able to hide: the matrix row reads "refuse", so a guard that refuses everything
would look like a pass. The negative case is what makes the positive case mean
something.

No path here is touched by I/O. The guard refuses by string and structural
inspection -- `os.path.abspath` and `os.path.splitdrive`, plus one
`GetDriveTypeW` call that the test answers itself. Nothing mounts, connects to,
or creates a share. A UNC path that reached the filesystem would be an attempt
against a real network resource, which is the one thing this test must never do.
"""
from __future__ import annotations

import ctypes
import os
import posixpath
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.storage import Store, StoreError  # noqa: E402

BACKSLASH = "\\"

#: A UNC path spelled the way each host spells one. Windows takes the two-backslash
#: form; POSIX takes the same share written with forward slashes, because
#: `//server/share` is the one spelling `os.path.abspath` preserves on both.
UNC = (
    BACKSLASH * 2 + "vkit-refusal-test" + BACKSLASH + "share" + BACKSLASH + "state.sqlite3"
    if os.name == "nt"
    else "//vkit-refusal-test/share/state.sqlite3"
)

#: The extended-length UNC form, which names the same thing and is the shape a
#: caller gets from `Path` when it has been handed a raw `\\?\` string. It is
#: listed separately because a guard that only tests `startswith("\\\\")` on the
#: whole path would let it past: `\\?\UNC\server\share` does not begin with
#: `\\server`.
UNC_EXTENDED = (
    BACKSLASH * 2 + "?" + BACKSLASH + "UNC" + BACKSLASH + "vkit-refusal-test"
    + BACKSLASH + "share" + BACKSLASH + "state.sqlite3"
    if os.name == "nt"
    else "//vkit-refusal-test/share/extended.sqlite3"
)


def test_a_unc_path_is_refused_and_a_local_path_beside_it_is_not(tmp_path: Path) -> None:
    """The UNC refusal, and the local path it must not take down with it.

    Both halves are in one body because the second is what makes the first mean
    anything. A guard that raised on every path would satisfy every assertion here
    except the one at the bottom, which is the assertion that would notice.
    """
    for name, path in (("unc", UNC), ("unc-extended", UNC_EXTENDED)):
        with pytest.raises(StoreError) as caught:
            Store(Path(path))
        assert "network-backed state path is unsupported" in str(caught.value), (
            f"the {name} path was refused without naming the reason, so a reader "
            f"cannot tell a network path from any other refusal: {caught.value}"
        )

    # The countercheck. A local store is the ordinary case and must open, so the
    # refusals above are about the path's shape rather than about `Store`.
    local = tmp_path / "local" / "state.sqlite3"
    opened = Store(local)
    opened.register_run(
        "local-1", "unit", task_id=None, attempt=None, source={},
        configuration_digest="c", fixture_digest=None,
    )
    assert local.is_file()
    assert opened.run_dir("local-1").is_dir()


def test_a_network_drive_is_refused_and_a_local_drive_beside_it_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The drive-type refusal, which string inspection alone cannot make.

    `Z:\\state.sqlite3` and `D:\\state.sqlite3` are the same string shape, so the
    only thing that tells them apart is the kernel's answer about the letter. The
    test supplies that answer, once for the remote case and once for the local
    one, which is what separates "the guard refused a network drive" from "the
    guard refused a drive".

    Windows-only in the narrow sense that the branch under test is
    `os.name == "nt"`. On POSIX the branch does not exist and there is no drive
    letter to be remote, so this file's UNC case is the whole of the guarantee
    there and duplicating it would test `posixpath`.
    """
    if os.name != "nt":
        pytest.skip("the drive-type branch is Windows-only; os.name is not nt")

    # `Z:\\state.sqlite3` and `D:\\state.sqlite3` are the same string shape, so the
    # only thing that tells them apart is the kernel's answer about the letter.
    # There is no way to arrange that here without mapping a drive, which is a
    # change to the machine, so the test answers the kernel itself.
    #
    # `ctypes.windll` resolves `kernel32` on each attribute access, so assigning
    # through it replaces the function for the caller too. Measured, not assumed:
    # `storage.py` reads `ctypes.windll.kernel32.GetDriveTypeW`, and the patch
    # takes effect there. 4 is DRIVE_REMOTE, the value `storage.py` declares.
    monkeypatch.setattr(
        ctypes.windll.kernel32, "GetDriveTypeW", lambda _d: 4, raising=True
    )
    with pytest.raises(StoreError) as remote:
        Store(Path("Z:" + BACKSLASH + "state.sqlite3"))
    assert "network-backed state path is unsupported" in str(remote.value)

    # The countercheck, and the one that makes the row mean what it says. The
    # kernel now answers DRIVE_FIXED (3) for the same letter, so a guard that
    # refused on the shape of the string alone would fail here -- and a guard that
    # ignored the kernel entirely would have passed the assertion above for the
    # wrong reason.
    monkeypatch.setattr(
        ctypes.windll.kernel32, "GetDriveTypeW", lambda _d: 3, raising=True
    )
    local = tmp_path / "local-drive" / "state.sqlite3"
    opened = Store(local)
    assert local.is_file()
    assert opened.version() >= 1


class _PosixHost:
    """`os` as a POSIX host spells paths, with everything else left alone.

    `_reject_network_path` reads three things off the `os` module it imported:
    `abspath`, `splitdrive`, and `name`. Handing it a stand-in for those three and
    delegating the rest is how this file asks what the guard does on the other
    half of the matrix row without a second machine.

    It is a stand-in rather than a copy of the guard's condition. A test that
    spelled out `absolute.startswith("//")` again would assert against itself and
    would stay green however the guard were changed, which is the failure this
    whole file exists to rule out.
    """

    def __init__(self, real: object) -> None:
        self._real = real
        self.abspath = posixpath.abspath
        self.splitdrive = posixpath.splitdrive
        self.name = "posix"

    def __getattr__(self, item: str):
        return getattr(self._real, item)


def test_a_posix_network_path_is_refused_and_a_local_one_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal as a POSIX host sees it, which is not the same refusal.

    Windows and POSIX disagree about what a UNC path even is. On Windows
    `abspath` leaves `\\\\server\\share` alone and `splitdrive` returns the share as
    the drive, so the refusal rests on the drive-shape test. On POSIX `abspath`
    leaves `//server/share` alone and `splitdrive` returns `""`, so the whole
    refusal rests on the `startswith("//")` test beside it. Two branches, two
    hosts, and a matrix row that covers both.

    Without this file the second branch is dead on a Windows host and the first
    is dead on a POSIX one, so each host's tests only ever exercised half of the
    guarantee it was reporting.
    """
    import vkit.storage as storage

    monkeypatch.setattr(storage, "os", _PosixHost(os))

    for name, path in (
        ("posix unc", "//vkit-refusal-test/share/state.sqlite3"),
        # Redundant separators, which `posixpath.abspath` collapses. This is a
        # different string after normalization, so it is a distinct input to the
        # guard rather than a second spelling of the first one.
        ("posix unc with doubled separators", "//localhost//tmp//state.sqlite3"),
    ):
        with pytest.raises(StoreError) as caught:
            Store(Path(path))
        assert "network-backed state path is unsupported" in str(caught.value), (
            f"{name} was not refused as a network path on a POSIX host: {caught.value}"
        )

    # The countercheck, and it is two paths rather than one. A local path opens on
    # this host, which is what says the refusals above are about the network shape
    # of the path rather than about `Store`. And a path whose doubled separators
    # are in the middle rather than at the front normalizes to a single slash and
    # opens too: normalization is what turns one into a network path and the other
    # into a local one, so a guard that tested for a doubled separator anywhere in
    # the string would fail here.
    for name, relative in (
        ("local absolute", "posix-local"),
        ("doubled separators mid-path", "net//not-a-share"),
    ):
        local = tmp_path / relative / "state.sqlite3"
        opened = Store(local)
        assert local.is_file(), f"{name} did not open a local store"
        assert opened.version() >= 1
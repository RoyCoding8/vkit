"""Make the tests exercise THIS checkout, not whichever one an install points at.

A worktree worker runs against a shared virtualenv, and an editable install puts
one absolute `src` directory on sys.path. Without this, `import vkit` from a
worktree silently resolves to the main checkout, so a worker's suite can pass
against code it did not write, or miss its own module entirely. That is a false
pass, the one failure mode this product exists to prevent.
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

import pytest

import subproc

_SRC = Path(__file__).resolve().parents[1] / "src"

if _SRC.is_dir():
    # Drop other entries that point at a vkit src tree, then put this one first.
    # sys.path[0] is the test directory, so index 0 beats the .pth entry that the
    # editable install appends at interpreter start.
    _mine = str(_SRC)
    sys.path[:] = [e for e in sys.path if e != _mine]
    sys.path.insert(0, _mine)

from vkit.paths import Project, open_project  # noqa: E402


# --------------------------------------------------------------- capabilities
#
# A capability probe answers "can this host produce the evidence this test reads?"
# and is deliberately separate from `sys.platform`. The platform says which OS
# this is; it does not say what the OS offers. macOS is POSIX and has no /proc,
# and a Windows or Linux host inside a container can have capabilities the
# platform name says nothing about. A test that gates on the platform alone runs
# on a host that cannot support it, and its failure reads as a product defect
# when it is a missing facility.
#
# The probes live here rather than in each file because the question is
# host-shaped, not file-shaped, and three files need the same answer computed
# once. `requires_procfs` is a MARKER, not a fixture: it is consumed at import
# time by `pytestmark`, and a fixture cannot be read from a module-level
# expression. The underlying check is a plain function so it stays testable.


@lru_cache(maxsize=None)
def procfs_available() -> bool:
    """Whether this host exposes the `/proc` the POSIX identity reader needs.

    `vkit.procidentity` reads `/proc/<pid>/stat` for a process's start time and
    `/proc/sys/kernel/random/boot_id` for the boot. Both come from procfs, so
    one mount check settles the whole layer: if `/proc` is not there, no test
    in this layer has anything to read and the honest report is a skip naming
    the missing facility.

    The check is for the mount, not for a file inside it. Probing for a
    particular file would make the probe's answer depend on which test asked,
    and a file can be unreadable for reasons that are not "no procfs" (a hidepid
    mount, a permission change) while procfs itself is present. A mount check
    keeps one answer for the whole layer.
    """
    return Path("/proc").is_dir()


#: The POSIX process-identity layer reads /proc, so it cannot run on a host
#: without it. Applied per module via `pytestmark`, because the whole layer is
#: unrunnable rather than one test: every test in these files reads procfs, so a
#: per-test skip would repeat the same reason on every line of the report and
#: imply that some of them could have run. macOS is the case this exists for --
#: POSIX, so the platform-only gate the files already used let them through, and
#: every one of them then failed on a missing /proc.
#:
#: A MARKER, not a fixture: it is consumed at import time by `pytestmark`, and a
#: fixture cannot be read from a module-level expression. The underlying check
#: stays a plain function so the platform and the facility remain separate
#: questions.
requires_procfs = pytest.mark.skipif(
    not procfs_available(),
    reason="requires /proc (procfs); macOS and other POSIX hosts without it "
           "cannot read the start time or boot id this layer is built on",
)


@lru_cache(maxsize=None)
def pid_namespace_available() -> bool:
    """Whether this host can create a pid namespace, and say why not when it cannot.

    Reaching a genuine pid reuse needs the counter to wrap past `pid_max`, which
    is 4194304, so an ordinary host would have to spawn four million processes
    before the number came round. A fresh pid namespace restarts that counter
    near 2, so the same kernel pid reaches a different process within a few dozen
    spawns. There is no other cheap route, and that is why the recycled-pid test
    needs this rather than merely preferring it.

    **This probes the syscall, not the binary.** The two fail independently and
    the difference is the whole point. On a host without util-linux `unshare` is
    not installed, and `FileNotFoundError` is the whole story. On a Linux
    container that dropped CAP_SYS_ADMIN the binary is present and the *kernel*
    refuses, which is the ubuntu-runner failure: "unshare: unshare failed:
    Operation not permitted". A probe that only asked `shutil.which("unshare")`
    would pass that host and the test would then fail with a raw EPERM, which
    reads as a defect in the product rather than a missing capability in the
    runner.

    So the binary is located first -- to keep the two causes apart in the reason
    -- and then the namespace is actually created, because only the attempt
    distinguishes "not installed" from "not permitted". The error is returned
    rather than a bool, so the skip reason can quote the kernel's own words.
    """
    from shutil import which

    import subproc

    binary = which("unshare")
    if binary is None:
        return False
    try:
        probe = subproc.run(
            [binary, "--fork", "--pid", "--mount-proc", sys.executable, "-c", "pass"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError:
        return False
    # Any non-zero exit is a refusal to create the namespace. The reason string
    # built by the caller quotes stderr, so EPERM names the missing capability.
    return probe.returncode == 0


def requires_pid_namespace() -> str | None:
    """Why this host cannot create a pid namespace, or None if it can.

    Split from the probe so the MARKER can be a constant while the reason stays
    a function of what actually failed. A reason that named only "unshare is
    unavailable" would be wrong on the runner that matters: there `unshare` is
    installed and the kernel denies the syscall, and a reader of that log needs
    to know the difference to know whether the skip is a runner's fault or a
    test's.
    """
    from shutil import which

    if which("unshare") is None:
        return (
            "requires a pid namespace to reach pid reuse, and this host has no "
            "`unshare` binary (util-linux is not installed). The binary is also "
            "absent on macOS, where it does not exist; there the /proc gate "
            "above skips this file before this one is consulted."
        )
    return (
        "requires the CAP_SYS_ADMIN capability to create a pid namespace, which "
        "is what makes pid reuse reachable without wrapping past pid_max. "
        "This host has `unshare` installed but the kernel refused the syscall, "
        "which is a container that dropped the capability. Grant CAP_SYS_ADMIN "
        "or run this file on a host with an unrestricted pid namespace."
    )


#: Applied per test, not per module: the recycled-pid test needs a namespace and
#: the rest of tests/test_procidentity_posix.py does not. A module-level marker
#: here would skip the file's other coverage on the same runner, and those tests
#: read nothing but /proc, which the runner does have.
requires_pid_namespace_for_reuse = pytest.mark.skipif(
    not pid_namespace_available(),
    reason=requires_pid_namespace(),
)


@lru_cache(maxsize=None)
def console_script(name: str = "vkit") -> Path:
    """The real path of an installed console script, read from the installation.

    Tests here drive the installed command rather than importing vkit, because the
    thing under test is what a user or an agent types. Finding that command is a
    host-shaped question, and asking it by convention -- `sys.executable`'s parent
    plus a guess at the filename -- assumes an install layout:

      * macOS `setup-python` puts the interpreter under
        `/Library/Frameworks/Python.framework/Versions/3.13/bin/python` and pip
        installs the script into `~/.local/bin/`, so nothing is beside it.
      * A framework or embedded Windows layout puts the interpreter somewhere else
        again while the script lands in `Scripts/`.

    Both were found by hardcoding one layout, so the suite passed on the
    maintainer's venv and failed on every runner. `shutil.which` is no better: it
    answers for whatever is first on PATH, which on a shared host is another
    virtualenv's copy, and a test that then measures the wrong install is worse
    than one that reports it cannot find one.

    The installer already recorded the answer. `RECORD` names every file the
    distribution installed, relative to site-packages, and `locate_file` resolves
    that relative path against the directory the metadata came from -- which is
    the same arithmetic the installer did, so it lands on the script wherever pip
    chose to put it. Reading the installation rather than predicting it is what
    makes this work on both layouts instead of one.

    Entry points are read first so the script is the one belonging to the
    distribution that declares `name`, not any file of that name in the install.
    """
    from importlib.metadata import entry_points

    for found in entry_points(group="console_scripts"):
        if found.name != name:
            continue
        distribution = found.dist
        for entry in distribution.files or ():
            if PureWindowsPath(str(entry)).stem.lower() != name.lower():
                continue
            resolved = Path(distribution.locate_file(entry))
            if resolved.is_file():
                # `locate_file` leaves the `..` an installer recorded in RECORD,
                # because that IS how pip spelled the path relative to
                # site-packages. Resolving collapses it to the one real location,
                # which is what a caller comparing two answers needs to compare.
                return resolved.resolve()

    raise AssertionError(
        f"no installed console script named {name!r} was found for "
        f"{sys.executable}. The suite drives the real command rather than an "
        f"import, so it needs the distribution installed: pip install -e \".[test]\"."
    )


def init_repo(path: Path, message: str = "fixture") -> None:
    """Turn a directory into a real repository, because the product requires one.

    `open_project` resolves the root and the shared Git directory through git,
    so a directory that is not a repository cannot be opened at all. The commit
    is real for the same reason: source identity is built from the index and
    HEAD, and a fixture with no commit has no HEAD to record.
    """
    path.mkdir(parents=True, exist_ok=True)
    subproc.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subproc.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message],
        cwd=path, check=True, capture_output=True,
    )


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Callable[..., Project]:
    """Build a repository under pytest's tmp_path and return its resolved Project.

    The caller writes the files it wants, because what a repository contains is
    the thing under test. This fixture guarantees only the two things every Plan
    06 test needs: a real git repository, and a Project the core can operate on.
    """
    counter = {"n": 0}

    def build(files: dict[str, str] | None = None, *, commit: bool = True) -> Project:
        counter["n"] += 1
        root = tmp_path / f"repo{counter['n']}"
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in (files or {}).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if commit:
            init_repo(root)
        return open_project(root)

    return build

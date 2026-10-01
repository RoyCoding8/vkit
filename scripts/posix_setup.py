#!/usr/bin/env python3
"""The environment a POSIX verification run needs, in one definition.

Two things live here and nothing else does: the venv a run executes against,
and the PATH that venv needs. Both were duplicated across shell scripts and each
copy was missing part of one, so a run through the wrong entry point silently
lost a piece of the environment.

The PATH is three directories, each for a measured reason.

    <venv>/bin    the example manifests and several acceptance rows name the
                  interpreter `python`, and this host ships only `python3`.
                  Measured without it: every check reported BLOCKED with
                  `prerequisite_missing: python: 'python' is not on PATH`, which
                  is a missing name rather than a product defect.
    ~/.local/bin  the `claude` CLI, which tests/test_console.py drives to prove
                  the host accepts the plugin. Measured without it: 8 tests
                  skipped, which is a hole rather than a reason. The
                  distribution's default PATH does not carry this directory in a
                  login or a non-login shell, so it is the only reason those 8
                  tests run at all.

Both are added by `run_env`, called by every subcommand of `scripts/posix_harness.py`,
so no entry point has a copy of the list to drift from. The skip count is the
receipt and it is per-entry-point, so a fix that does not hold at every entry
point does not hold.

The reports directory is here for the same reason. It was defined twice with two
different values, so a run through one script wrote to `tmp/` and a read through
another looked in `$HOME`, and the totals silently described a run that had
already finished.

The venv lives on the WSL-native filesystem, not beside the repository.
Measured: `python3 -m venv` under /mnt/d did not finish in ten minutes, because
every file it writes crosses the DrvFs boundary into NTFS. On /home it takes
seconds. The repository stays on /mnt/d so the worktree is this checkout; only
the interpreter is relocated.

Run:  python3 scripts/posix_setup.py
Then: python3 scripts/posix_harness.py suite
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every requirement a POSIX run of this suite needs, in one list.
#
# `hypothesis` and `hatchling` were both absent from the setup the shell scripts
# performed, and each absence was a hole rather than an error. hypothesis is
# imported at module scope by tests/test_formal_correspondence.py through
# importorskip, so without it the whole file skips and the property tests that
# check the core against its reference model never run while the suite still
# reads green. hatchling is the build backend pyproject.toml names, so a
# `pip wheel` or an editable install fails on the missing backend rather than on
# anything about this product; that one omission was the whole of the three
# wheel-test errors measured on a POSIX run.
#
# pywin32 is deliberately absent. pyproject.toml declares it
# `sys_platform == 'win32'` and nothing on the POSIX path imports it.
REQUIREMENTS = (
    "jsonschema>=4.23,<5",
    "pytest>=8.0",
    "mcp>=2.2,<3",
    "anyio>=4.5",
    "hypothesis>=6.100",
    "hatchling",
)

# The packages whose versions are worth reporting after a setup, so a reader can
# see which requirement is missing without re-running it.
REPORTED = ("jsonschema", "pytest", "mcp", "anyio", "hypothesis", "hatchling", "vkit")


def venv_dir() -> Path:
    """The venv a POSIX run executes against. `VKIT_POSIX_VENV` overrides it."""
    override = os.environ.get("VKIT_POSIX_VENV")
    return Path(override) if override else Path.home() / ".venvs" / "vkit-posix"


def reports_dir() -> Path:
    """Where every POSIX run writes its reports, and where `totals` reads them.

    Not under the repository, and not on the /mnt side. A whole-suite run was
    killed repeatedly part-way through on this host with no traceback and no
    summary, and the venv had already been moved off /mnt/d for the same reason:
    every file written across the DrvFs boundary is slow enough to matter and the
    long run is the thing that dies. The cause was never established, so this is
    a mitigation rather than a fix, and recorded as one.
    """
    override = os.environ.get("VKIT_POSIX_REPORTS")
    if override:
        return Path(override)
    return Path.home() / "vkit-posix-reports" / "posix-suite"


def posix_python() -> Path:
    """The interpreter a POSIX run executes, named so nothing falls back to the
    system python3, which has no pytest and no mcp."""
    return venv_dir() / "bin" / "python"


def run_env() -> dict[str, str]:
    """The environment a POSIX child process needs, PATH already extended.

    One function, so there is no second copy of the directory list to fall out
    of date. Callers hand this to `subprocess` rather than mutating their own
    environment, which keeps the harness honest about what it did.
    """
    env = dict(os.environ)
    entries = [str(venv_dir() / "bin"), str(Path.home() / ".local" / "bin")]
    env["PATH"] = os.pathsep.join([*entries, env.get("PATH", "")])
    return env


def require_venv() -> Path:
    """The venv interpreter, or exit with the one command that would fix it."""
    python = posix_python()
    if not python.is_file():
        print(
            f"no POSIX venv at {venv_dir()}; run: python3 scripts/posix_setup.py",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return python


def _installed_version(program: str) -> str:
    try:
        done = subprocess.run([program, "--version"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "MISSING"
    return (done.stdout or done.stderr).strip() or "MISSING"


def _report(python: Path) -> None:
    """Print what the venv ended up with, read by the venv's own interpreter.

    Reading installed metadata rather than echoing the requirements list, so a
    requirement that failed to install shows as `NOT INSTALLED` here instead of
    passing silently into the next run.
    """
    program = (
        "import importlib.metadata as m\n"
        f"for name in {REPORTED!r}:\n"
        "    try:\n"
        "        print(f'  {name}: {m.version(name)}')\n"
        "    except m.PackageNotFoundError:\n"
        "        print(f'  {name}: NOT INSTALLED')\n"
    )
    done = subprocess.run([str(python), "-c", program], env=run_env(),
                          capture_output=True, text=True)
    print("== installed ==")
    print(done.stdout.rstrip() or done.stderr.rstrip())
    print(f"  node: {_installed_version('node')}")
    print(f"  vkit console script: {_installed_version(str(python.parent / 'vkit'))}")


def _install_node() -> None:
    """Node, because examples/node-cli and examples/node-http are Node programs.

    tests/test_features.py skips honestly when node is missing, but a skipped
    suite verifies nothing about them, so node is installed rather than
    tolerated.
    """
    if os.system("command -v node >/dev/null 2>&1") == 0:
        return
    elevate = "" if os.geteuid() == 0 else "sudo "
    print("\nnode is not installed; installing it for the Node examples")
    os.system(f"{elevate}apt-get update -qq && {elevate}apt-get install -y -qq nodejs")


def setup() -> int:
    """Build the venv and install what the suite needs into it.

    Deliberately runnable with the stock interpreter, because the venv it
    creates is what the rest of this tree needs and nothing here may depend on
    it already existing.
    """
    if os.name != "posix":
        print("this is the POSIX setup; a Windows host uses uv and pywin32",
              file=sys.stderr)
        return 2

    venv = venv_dir()
    print(f"repo: {REPO_ROOT}")
    print(f"venv: {venv}")

    if not venv.is_dir():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)

    python = require_venv()
    env = run_env()
    # The PATH is applied as soon as the venv it points at exists, and before
    # anything is installed, so the install itself runs against that venv rather
    # than the interpreter that made it.
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                   check=True, env=env)
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", *REQUIREMENTS],
                   check=True, env=env)

    # The editable install puts this checkout's src on sys.path, so an acceptance
    # script that spawns the vkit console script exercises this tree and not
    # whichever vkit happens to be importable.
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--no-deps",
                    "-e", str(REPO_ROOT)], check=True, env=env)

    _install_node()
    _report(python)
    print("\ndone. Run the suite with:\n  python3 scripts/posix_harness.py suite")
    return 0


if __name__ == "__main__":
    sys.exit(setup())

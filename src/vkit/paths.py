"""Where everything lives, resolved from a repository root.

The evidence location is the one decision that shapes the rest of the system.
State goes under the Git common directory rather than the working tree so that
two clones of one repository share evidence, and so that deleting a worker's
checkout cannot delete the record of what ran in it. Plan 01 acceptance has a
row for exactly that: retire the copied test checkout and the evidence must
survive.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .nowindow import hidden_window

STATE_DIR_NAME = "verification-kit"
MANIFEST_RELATIVE = Path("verification") / "manifest.json"
DB_NAME = "state.sqlite3"
RUNS_DIR_NAME = "runs"

# The exit code that means "this is not a git repository", handled separately so
# it does not read as a generic command failure.
_GIT_NOT_A_REPO = 128


class ProjectError(Exception):
    """The path is not a project vkit can operate on."""


def _git(args: list[str], cwd: Path) -> str:
    """Run git and return its output as text.

    The decode is pinned to UTF-8 rather than inherited from the locale. A path
    read back with the wrong encoding is a *different* string, not an error, and
    the mismatch then surfaces much later as WinError 267 from an unrelated
    process launch. See tests/test_encoding.py.
    """
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
            **hidden_window(),
        )
    except FileNotFoundError as exc:
        raise ProjectError("git is not installed or not on PATH") from exc
    except NotADirectoryError as exc:
        raise ProjectError(f"{cwd} is not a usable working directory: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()
        message = detail[-1] if detail else f"git {' '.join(args)} failed"
        raise ProjectError(message)
    return done.stdout.strip()


@dataclass(frozen=True)
class Project:
    """A resolved repository. Paths are absolute; nothing here re-reads the disk."""

    root: Path
    git_common_dir: Path

    @property
    def state_root(self) -> Path:
        return self.git_common_dir / STATE_DIR_NAME

    @property
    def runs_root(self) -> Path:
        return self.state_root / RUNS_DIR_NAME

    @property
    def db_path(self) -> Path:
        return self.state_root / DB_NAME

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_RELATIVE


def open_project(path: str | os.PathLike[str]) -> Project:
    """Resolve a project root and its shared Git directory.

    Accepts a subdirectory of the repository. Raises ProjectError rather than
    returning a half-resolved project, because every caller needs both paths.
    """
    start = Path(path).expanduser()
    if not start.is_absolute():
        start = Path.cwd() / start
    if not start.exists():
        raise ProjectError(f"path does not exist: {start}")
    if start.is_file():
        start = start.parent

    try:
        top = Path(_git(["rev-parse", "--show-toplevel"], start))
    except ProjectError as exc:
        raise ProjectError(f"{start} is not inside a Git repository ({exc})") from exc

    common = Path(_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], top))
    return Project(root=top.resolve(), git_common_dir=common.resolve())

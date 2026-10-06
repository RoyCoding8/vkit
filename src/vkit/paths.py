"""Resolves a repository root and its Git common directory, which is shared by worktrees and so
outlives any one checkout.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .nowindow import hidden_window

MANIFEST_RELATIVE = Path("verification") / "manifest.json"


class ProjectError(Exception):
    """The path is not a project vkit can operate on."""


def _git(args: list[str], cwd: Path) -> str:
    """Run git and return stdout. Decoding is pinned to UTF-8 because a mis-decoded path is a
    different string, not an error.
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
    """A resolved repository with absolute paths."""

    root: Path
    git_common_dir: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_RELATIVE


def open_project(path: str | os.PathLike[str]) -> Project:
    """Resolve the repository containing `path`, which may be a subdirectory."""
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

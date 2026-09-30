"""Make the tests exercise THIS checkout, not whichever one an install points at.

A worktree worker runs against a shared virtualenv, and an editable install puts
one absolute `src` directory on sys.path. Without this, `import vkit` from a
worktree silently resolves to the main checkout, so a worker's suite can pass
against code it did not write, or miss its own module entirely. That is a false
pass, the one failure mode this product exists to prevent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

if _SRC.is_dir():
    # Drop other entries that point at a vkit src tree, then put this one first.
    # sys.path[0] is the test directory, so index 0 beats the .pth entry that the
    # editable install appends at interpreter start.
    _mine = str(_SRC)
    sys.path[:] = [e for e in sys.path if e != _mine]
    sys.path.insert(0, _mine)

from vkit.paths import Project, open_project  # noqa: E402


def init_repo(path: Path, message: str = "fixture") -> None:
    """Turn a directory into a real repository, because the product requires one.

    `open_project` resolves the root and the shared Git directory through git,
    so a directory that is not a repository cannot be opened at all. The commit
    is real for the same reason: source identity is built from the index and
    HEAD, and a fixture with no commit has no HEAD to record.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
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

"""The test fixture must not be able to write to the developer's own repository.

An integration fixture builds a throwaway git repository and writes to its
config. That is safe while `git` means "the directory I was run in", and unsafe
the moment something aims it elsewhere: `GIT_DIR` is inherited by every child
process, so one export anywhere in a test run redirects every `git init`,
`git config` and `git commit` the fixture performs into the repository that
export names.

That is not hypothetical. It happened, and it wrote `core.worktree` into the real
repository of the checkout the tests were run from. Git then refused to operate
there at all -- `git status`, `git log`, `git worktree list`, and even
`git config --global --get` returned `fatal: Invalid path '/mnt'` -- on the
developer's own machine, not just in the test run.

These tests reproduce that shape and assert the fixture ignores it. The
assertion is about the fixture writing somewhere else, not about git's
behaviour, because git behaving as documented is not what is in doubt.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixtures import git  # noqa: E402


def _real_config() -> str:
    """This repository's own config, as the damage would have left it."""
    done = subprocess.run(
        ["git", "config", "--local", "--list"],
        cwd=REPO_ROOT, capture_output=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
        env={k: v for k, v in os.environ.items() if k != "GIT_DIR"},
    )
    return done.stdout


def test_a_stray_git_dir_cannot_redirect_the_fixture_into_the_real_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `GIT_DIR` in the environment must not change where the fixture writes.

    The damage took two shapes and this covers the quieter one. `git init` under
    a stray `GIT_DIR` is the loud case: the fixture's own assertion fires, so the
    run is red and the cause is visible. `git config` is the silent one -- it
    succeeds against the wrong repository and the run stays green, which is why
    the breakage was discovered by a human noticing a dead checkout rather than
    by a failing test.
    """
    before = _real_config()

    # Point every child at this repository, which is what actually happened.
    monkeypatch.setenv("GIT_DIR", str(REPO_ROOT / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(REPO_ROOT))

    throwaway = tmp_path / "shop"
    throwaway.mkdir()
    git(throwaway, "init", "-q", "-b", "main")
    git(throwaway, "config", "user.email", "fixture@example.invalid")
    git(throwaway, "config", "user.name", "Fixture")

    after = _real_config()
    assert after == before, (
        "the fixture wrote to the real repository's config while GIT_DIR pointed "
        f"at it.\nbefore:\n{before}\nafter:\n{after}"
    )


def test_the_fixture_git_helper_strips_the_steering_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stripping is the behaviour, not an implementation detail.

    A test that only proved "no damage occurred" would pass on a fixture that
    simply got lucky. This asserts the properties that make it safe: the
    repository it writes to is the one it was handed, whatever the environment
    says.
    """
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        monkeypatch.setenv(name, str(REPO_ROOT / ".git" / "does-not-exist"))

    target = tmp_path / "shop"
    target.mkdir()
    git(target, "init", "-q", "-b", "main")
    (target / "a.txt").write_text("x", encoding="utf-8")
    git(target, "add", "-A")

    # The throwaway repository is a real repository, and it is the right one.
    assert (target / ".git").is_dir()
    # The verification here is deliberately NOT the fixture's helper: it runs its
    # own git with its own clean environment, so a bug in the helper cannot hide
    # itself by being the thing that reports the answer.
    clean = {k: v for k, v in os.environ.items() if k not in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")}
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=target,
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=60, check=False, env=clean,
    )
    assert staged.stdout.strip() == "a.txt", (
        "the staged content is not the throwaway repository's, so the writes "
        f"went somewhere else: {staged.stdout!r} {staged.stderr!r}"
    )

"""Behavior of the source fingerprint, against real Git repositories.

Nothing here is mocked. Each test builds a throwaway repository, changes it the
way a person or a rogue process would, and asks vkit what it sees, because the
whole point of the module is whether a real `git status` and a real worktree
produce a usable answer. A mocked `git status` would only prove the parser can
read the shape it was handed.

The digests are never written down as expected constants. They depend on the
absolute path of the temporary directory and on a commit hash, so a literal
would assert nothing true. What is asserted is the contract a report relies on:
the digest is a 64-character sha256, it is stable when nothing moved, and it
moves for exactly the changes that should invalidate a run.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from vkit.identity import compute_source_identity, source_unchanged  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.schemas import RUN_REPORT, validate  # noqa: E402


_SRC = Path(__file__).resolve().parents[1] / "src"


def test_these_tests_exercise_this_worktree() -> None:
    """Guard the guard. Without this, a path-shadowing slip would let every
    other test pass against a neighbouring worker's copy of the module."""
    import vkit.identity

    assert Path(vkit.identity.__file__).resolve().parent == _SRC / "vkit"


def git(repo: Path, *args: str) -> str:
    """Run a real git command. Failing loudly beats a test that quietly
    asserts something about a repository that was never really built."""
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-b", "main", "-q", ".")
    # A machine with no global identity, or a signing key, must not change what
    # these tests prove.
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    return root


def commit_all(repo: Path, message: str = "initial") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "project")


@pytest.fixture()
def committed(repo: Path) -> Path:
    """A repository with one commit holding an application and its manifest."""
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    (repo / "verification").mkdir()
    (repo / "verification" / "manifest.json").write_text(
        '{"schema_version": 1, "checks": []}\n', encoding="utf-8"
    )
    commit_all(repo)
    return repo


def test_unchanged_repo_yields_the_same_digest_twice(committed: Path) -> None:
    project = open_project(committed)
    first = compute_source_identity(project)
    second = compute_source_identity(project)

    assert first.inventory_digest == second.inventory_digest
    assert first.head == second.head
    assert first.to_json() == second.to_json()
    assert source_unchanged(first, second)


def test_digest_is_a_sha256_over_the_actual_file_bytes(committed: Path) -> None:
    identity = compute_source_identity(open_project(committed))

    assert len(identity.inventory_digest) == 64
    assert set(identity.inventory_digest) <= set("0123456789abcdef")
    assert identity.tracked_files == 2
    # Recomputed the obvious way, so the digest is not an opaque token that
    # only this module can reproduce. Git's index hash would not answer this:
    # it describes what is staged, and an unstaged edit is the thing being
    # detected.
    expected = hashlib.sha256()
    for name in ("app.py", "verification/manifest.json"):
        for field in (name, "file", hashlib.sha256((committed / name).read_bytes()).hexdigest()):
            expected.update(field.encode())
            expected.update(b"\x00")
    assert identity.inventory_digest == expected.hexdigest()


def test_editing_tracked_content_changes_the_digest(committed: Path) -> None:
    project = open_project(committed)
    before = compute_source_identity(project)

    # Same path, same file list, different bytes. A digest built from names
    # would not notice, and would let a broken build stand as evidence.
    (committed / "app.py").write_text(
        "def total(xs):\n    return 0\n", encoding="utf-8"
    )
    after = compute_source_identity(project)

    assert before.inventory_digest != after.inventory_digest
    assert not source_unchanged(before, after)
    assert before.tracked_files == after.tracked_files == 2


def test_editing_nested_tracked_content_changes_the_digest(committed: Path) -> None:
    manifest = committed / "verification" / "manifest.json"
    before = compute_source_identity(open_project(committed))

    manifest.write_text('{"schema_version": 1, "checks": ["other"]}\n', encoding="utf-8")
    after = compute_source_identity(open_project(committed))

    assert not source_unchanged(before, after)
    assert after.dirty_paths == ("verification/manifest.json",)


def test_adding_an_untracked_source_file_changes_the_digest(committed: Path) -> None:
    project = open_project(committed)
    before = compute_source_identity(project)

    (committed / "patched.py").write_text("print('unexpected')\n", encoding="utf-8")
    after = compute_source_identity(project)

    assert before.inventory_digest != after.inventory_digest
    assert not source_unchanged(before, after)
    # The reported count is the tracked inventory, so an untracked addition
    # does not inflate a number a reader would take as the manifest size.
    assert after.tracked_files == 2


def test_untracked_file_is_included_whatever_its_extension(committed: Path) -> None:
    """Untracked files have no suffix filter, so there is no list of "source"
    extensions in the implementation to drift from what a repository actually
    contains. These are the extensions a reader would expect to be special."""
    for name in (
        "extra.py",
        "notes.md",
        "data.bin",
        "Makefile",
        "Dockerfile",
        "no-extension",
        "weird.name.with.dots.py",
    ):
        project = open_project(committed)
        before = compute_source_identity(project)
        (committed / name).write_text("content\n", encoding="utf-8")
        after = compute_source_identity(project)
        assert not source_unchanged(before, after), f"{name} was not covered"
        (committed / name).unlink()


def test_clean_repo_reports_not_dirty(committed: Path) -> None:
    identity = compute_source_identity(open_project(committed))

    assert identity.dirty is False
    assert identity.dirty_paths == ()
    assert identity.head == git(committed, "rev-parse", "HEAD")


def test_modified_tracked_file_reports_dirty_with_its_path(committed: Path) -> None:
    (committed / "app.py").write_text("def total(xs):\n    return 0\n", encoding="utf-8")

    identity = compute_source_identity(open_project(committed))

    assert identity.dirty is True
    assert identity.dirty_paths == ("app.py",)


def test_untracked_file_reports_dirty_with_its_path(committed: Path) -> None:
    (committed / "patched.py").write_text("print('unexpected')\n", encoding="utf-8")

    identity = compute_source_identity(open_project(committed))

    assert identity.dirty is True
    assert identity.dirty_paths == ("patched.py",)


def test_staged_but_uncommitted_change_is_dirty(committed: Path) -> None:
    (committed / "app.py").write_text("def total(xs):\n    return 0\n", encoding="utf-8")
    git(committed, "add", "app.py")

    identity = compute_source_identity(open_project(committed))

    assert identity.dirty is True
    assert identity.dirty_paths == ("app.py",)


def test_deleted_tracked_file_is_dirty_and_moves_the_digest(committed: Path) -> None:
    project = open_project(committed)
    before = compute_source_identity(project)

    (committed / "app.py").unlink()
    after = compute_source_identity(project)

    assert after.dirty is True
    assert after.dirty_paths == ("app.py",)
    assert not source_unchanged(before, after)


def test_rename_reports_one_path_not_two(committed: Path) -> None:
    project = open_project(committed)
    before = compute_source_identity(project)

    git(committed, "mv", "app.py", "renamed.py")
    after = compute_source_identity(project)

    assert after.dirty is True
    # `git status -z` writes a rename origin as a second field of the same
    # entry. Reporting it would name a path that no longer exists in the tree.
    assert after.dirty_paths == ("renamed.py",)
    assert not source_unchanged(before, after)


def test_dirty_paths_are_sorted_and_use_posix_separators(repo: Path) -> None:
    (repo / "z.py").write_text("print('z')\n", encoding="utf-8")
    commit_all(repo)
    (repo / "zeta.py").write_text("print('z')\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "alpha.py").write_text("print('a')\n", encoding="utf-8")

    identity = compute_source_identity(open_project(repo))

    assert identity.dirty_paths == ("pkg/alpha.py", "zeta.py")


def test_empty_commit_still_yields_a_valid_head(repo: Path) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty start")

    identity = compute_source_identity(open_project(repo))

    assert identity.head == git(repo, "rev-parse", "HEAD")
    assert len(identity.head) == 40
    assert identity.tracked_files == 0
    assert identity.dirty is False
    assert identity.inventory_digest == hashlib.sha256(b"").hexdigest()


def test_repository_with_no_commits_yields_the_unborn_branch(repo: Path) -> None:
    identity = compute_source_identity(open_project(repo))

    # Not a commit hash, but the report schema requires a non-empty string and
    # there is genuinely no commit to name yet.
    assert identity.head == "refs/heads/main"
    assert identity.dirty is False


def test_ignored_output_and_secrets_do_not_move_the_digest(committed: Path) -> None:
    (committed / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    commit_all(committed)
    project = open_project(committed)
    before = compute_source_identity(project)

    # The exclusion policy, all three parts: the repository's own ignore rules
    # cover generated output and logs, and the two known secret inputs are
    # never opened.
    (committed / "build").mkdir()
    (committed / "build" / "output.txt").write_text("stale\n", encoding="utf-8")
    (committed / "run.log").write_text("noise\n", encoding="utf-8")
    (committed / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    (committed / "server.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\n", encoding="utf-8"
    )

    after = compute_source_identity(project)
    assert source_unchanged(before, after)
    assert after.dirty is False


def test_a_committed_secret_is_still_excluded(repo: Path) -> None:
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    commit_all(repo)

    identity = compute_source_identity(open_project(repo))

    # The policy is applied to the union of tracked and untracked paths, so
    # committing a secret does not pull it into a persisted report.
    assert identity.tracked_files == 1
    assert identity.dirty is False


def test_two_trees_with_identical_content_share_a_digest(tmp_path: Path) -> None:
    """The digest is a function of content, not of where the checkout lives."""
    for name in ("one", "two"):
        other = make_repo(tmp_path / name)
        (other / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (other / "pkg").mkdir()
        (other / "pkg" / "b.py").write_text("print('b')\n", encoding="utf-8")
        (other / "a.py").write_text("print('a')\n", encoding="utf-8")
        commit_all(other)

    first = compute_source_identity(open_project(tmp_path / "one"))
    second = compute_source_identity(open_project(tmp_path / "two"))

    assert first.inventory_digest == second.inventory_digest
    assert first.tracked_files == second.tracked_files == 3


def test_identity_satisfies_the_run_report_schema(committed: Path) -> None:
    (committed / "app.py").write_text("def total(xs):\n    return 0\n", encoding="utf-8")
    payload = compute_source_identity(open_project(committed)).to_json()

    validate(
        "run report",
        RUN_REPORT,
        {
            "schema_version": 1,
            "run_id": "run-1",
            "check_id": "unit",
            "lifecycle": "terminal",
            "started_at": "2026-09-29T10:00:00+00:00",
            "ended_at": "2026-09-29T10:00:01+00:00",
            "outcome": {
                "result": "PASS",
                "scenarios": [{"id": "s1", "result": "PASS", "observation": "total 0"}],
            },
            "source": payload,
            "command": {"argv": ["python", "-c", "pass"], "cwd": str(committed)},
            "configuration_digest": "c" * 64,
        },
    )
    assert payload == {
        "head": payload["head"],
        "inventory_digest": payload["inventory_digest"],
        "dirty": True,
        "tracked_files": 2,
        "dirty_paths": ["app.py"],
    }

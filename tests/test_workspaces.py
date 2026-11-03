from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from helpers import git

from vkit.cli import EXIT_INTERNAL, main
from vkit.paths import open_project
from vkit.workspaces import (
    WorkspaceError,
    known_workspaces,
    registry_path,
    remember,
    resolve_workspace,
)


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "user-settings" / "config.json"
    monkeypatch.setenv("VKIT_CONFIG", str(config))
    return config


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("repo\n", encoding="utf-8")
    git(path, "init", "-q")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")
    return path


def test_registry_lives_beside_launcher_settings(isolated_registry: Path) -> None:
    assert registry_path() == isolated_registry.parent / "workspaces.sqlite3"


def test_worktrees_have_separate_contexts_grouped_by_common_directory(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    worktree = tmp_path / "feature"
    git(root, "worktree", "add", "-qb", "feature", str(worktree))

    primary = remember(open_project(root), "cli")
    secondary = remember(open_project(worktree), "mcp")

    assert primary.id != secondary.id
    assert primary.repository_id == secondary.repository_id
    assert {workspace.path for workspace in known_workspaces()} == {str(root), str(worktree)}
    assert resolve_workspace(primary.id).root == root.resolve()
    assert resolve_workspace(secondary.id).root == worktree.resolve()
    assert set(primary.as_json()) == {
        "id", "name", "path", "repository_id", "first_seen", "last_seen", "source",
    }


def test_duplicate_registration_updates_one_row_and_preserves_first_seen(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")

    first = remember(open_project(root), "cli")
    duplicate = remember(open_project(root), "mcp")

    assert first.id == duplicate.id
    assert first.first_seen == duplicate.first_seen
    assert duplicate.last_seen >= first.last_seen
    assert duplicate.source == "mcp"
    assert len(known_workspaces()) == 1


def test_concurrent_registrations_converge_by_worktree_identity(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    worktree = tmp_path / "feature"
    git(root, "worktree", "add", "-qb", "feature", str(worktree))
    projects = [open_project(root), open_project(worktree)]

    with ThreadPoolExecutor(max_workers=12) as pool:
        saved = list(pool.map(lambda i: remember(projects[i % 2], "worker"), range(36)))

    assert len({workspace.id for workspace in saved}) == 2
    assert {workspace.id for workspace in known_workspaces()} == {workspace.id for workspace in saved}


def test_missing_or_replaced_path_is_never_redirected(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    worktree = tmp_path / "feature"
    git(root, "worktree", "add", "-qb", "feature", str(worktree))
    remembered = remember(open_project(worktree), "cli")

    git_file = worktree / ".git"
    assert git_file.is_file()
    git_file.unlink()
    git(worktree, "init", "-q")

    with pytest.raises(WorkspaceError, match="identity changed"):
        resolve_workspace(remembered.id)
    assert known_workspaces()[0].id == remembered.id


def test_reinitialized_repository_at_same_path_gets_new_identity(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    remembered = remember(open_project(root), "cli")
    (root / ".git").rename(tmp_path / "old-git")
    git(root, "init", "-q")

    with pytest.raises(WorkspaceError, match="unavailable"):
        resolve_workspace(remembered.id)

    replacement = remember(open_project(root), "cli")

    assert replacement.id != remembered.id
    assert replacement.repository_id != remembered.repository_id
    with pytest.raises(WorkspaceError, match="identity changed"):
        resolve_workspace(remembered.id)
    assert resolve_workspace(replacement.id).root == root.resolve()


def test_interrupted_identity_write_does_not_publish_an_unreadable_marker(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")

    def interrupted(*args, **kwargs):
        raise OSError("interrupted write")

    with monkeypatch.context() as patch:
        patch.setattr("vkit.workspaces.json.dump", interrupted)
        with pytest.raises(OSError, match="interrupted write"):
            remember(open_project(root), "cli")
    assert not (root / ".git" / "vkit" / "workspace-id.json").exists()
    saved = remember(open_project(root), "cli")
    assert resolve_workspace(saved.id).root == root.resolve()


def test_moved_path_remains_known_but_unavailable(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    remembered = remember(open_project(root), "cli")
    moved = tmp_path / "moved"
    root.rename(moved)

    with pytest.raises(WorkspaceError, match="unavailable"):
        resolve_workspace(remembered.id)
    assert known_workspaces()[0].path == str(root)


def test_cli_remembers_the_project_it_uses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path / "repo")

    exit_code = main(["doctor", "--project", str(root), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code != EXIT_INTERNAL
    assert output["project"] == str(root.resolve())
    assert known_workspaces()[0].path == str(root)


def test_registry_write_failure_does_not_fail_cli_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(tmp_path / "repo")
    config_parent = tmp_path / "blocked-config"
    config_parent.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("VKIT_CONFIG", str(config_parent / "config.json"))

    exit_code = main(["doctor", "--project", str(root), "--json"])
    captured = capsys.readouterr()

    assert exit_code != EXIT_INTERNAL
    assert "warning: could not remember VKit workspace" in captured.err


def test_mcp_startup_remembers_its_bound_project(tmp_path: Path) -> None:
    from helpers import McpClient

    root = make_repo(tmp_path / "repo")
    client = McpClient(root)
    try:
        assert client.initialize["serverInfo"]["name"] == "repo"
        assert known_workspaces()[0].path == str(root)
    finally:
        client.close()

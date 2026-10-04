"""Git paths and output retain non-ASCII characters."""
from pathlib import Path

from fixtures import git as fixture_git
from vkit.integration.gitidentity import git
from vkit.paths import open_project


def test_git_output_preserves_unicode_paths_and_null_separators(tmp_path: Path) -> None:
    repo = tmp_path / "資料 café"
    repo.mkdir()
    fixture_git(repo, "init", "-q")
    names = [" café.py", "資料.py"]
    for name in names:
        (repo / name).write_text("value = 1\n", encoding="utf-8")
    fixture_git(repo, "add", "--", *names)

    output = git(open_project(repo), "ls-files", "-z")

    assert output == " café.py\x00資料.py\x00"

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace

import pytest
from helpers import NO_WINDOW, git

from vkit.inputs import capture_git_commit, capture_working_tree
from vkit.paths import ProjectError


def _repository(path):
    path.mkdir()
    git(path, "init", "-q")
    (path / "source.rs").write_bytes(b"fn main() {\r\n}\r\n")
    (path / "run.sh").write_bytes(b"#!/bin/sh\n")
    (path / "run.sh").chmod(0o755)
    git(path, "add", "source.rs", "run.sh")
    git(path, "update-index", "--chmod=+x", "run.sh")
    git(path, "commit", "-qm", "initial")
    return path


def _head(path):
    return (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            **NO_WINDOW,
        )
        .stdout.decode("ascii")
        .strip()
    )


def test_git_capture_records_commit_tree_modes_and_raw_bytes(tmp_path):
    repo = _repository(tmp_path / "repo")
    capture = capture_git_commit(repo, _head(repo))
    assert capture.kind == "git_commit"
    assert capture.commit == _head(repo)
    assert len(capture.tree) == 40
    assert len(capture.parents) == 0
    assert [(file.path, file.mode) for file in capture.files] == [
        ("run.sh", "100755"),
        ("source.rs", "100644"),
    ]
    source = capture.files[1]
    assert source.content == b"fn main() {\r\n}\r\n"
    assert source.raw_sha256 == hashlib.sha256(source.content).hexdigest()
    assert source.blob_oid == "ed4816ba9ab60dca535ebfa387b045aab5e3abcb"
    assert capture.to_json()["files"][1]["path"] == "source.rs"
    assert capture.digest == capture.digest
    (repo / "source.rs").write_bytes(b"dirty")
    (repo / "untracked.rs").write_bytes(b"untracked")
    assert capture_git_commit(repo, _head(repo)).digest == capture.digest


def test_working_tree_capture_is_separate_from_commit_and_materializes_bytes(tmp_path):
    repo = _repository(tmp_path / "repo")
    (repo / "source.rs").write_bytes(b"working\r\n")
    (repo / "new.rs").write_bytes(b"new\x00bytes")
    (repo / "ignored.tmp").write_bytes(b"ignored")
    (repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")

    capture = capture_working_tree(repo)
    assert capture.kind == "working_tree"
    assert capture.commit == _head(repo)
    assert [file.path for file in capture.files] == [
        ".gitignore",
        "new.rs",
        "run.sh",
        "source.rs",
    ]
    assert capture.files[-1].content == b"working\r\n"
    target = tmp_path / "captured"
    target.mkdir()
    written = capture.materialize(target)
    assert tuple(path.relative_to(target).as_posix() for path in written) == tuple(
        file.path for file in capture.files
    )
    assert (target / "source.rs").read_bytes() == b"working\r\n"


def test_git_capture_supports_sha256_object_format(tmp_path):
    repo = tmp_path / "sha256"
    repo.mkdir()
    initialized = subprocess.run(
        ["git", "init", "--object-format=sha256", "-q"],
        cwd=repo,
        capture_output=True,
        check=False,
        **NO_WINDOW,
    )
    if initialized.returncode:
        pytest.skip("installed Git does not support SHA-256 repositories")
    (repo / "source.rs").write_bytes(b"sha256 source")
    git(repo, "add", "source.rs")
    git(repo, "commit", "-qm", "initial")

    capture = capture_git_commit(repo, _head(repo))
    assert capture.object_format == "sha256"
    assert len(capture.commit) == 64
    assert len(capture.files[0].blob_oid) == 64


@pytest.mark.parametrize("kind", ["commit", "working_tree"])
def test_formal_capture_rejects_secret_paths(tmp_path, kind):
    repo = _repository(tmp_path / "repo")
    (repo / ".env.local").write_bytes(b"secret")
    if kind == "commit":
        git(repo, "add", ".env.local")
        git(repo, "commit", "-qm", "secret path")
        capture = lambda: capture_git_commit(repo, _head(repo))
    else:
        capture = lambda: capture_working_tree(repo)
    with pytest.raises(ProjectError, match="secret-matched"):
        capture()


def test_capture_materialization_rejects_symlink_root_and_escaping_path(tmp_path):
    repo = _repository(tmp_path / "repo")
    capture = capture_git_commit(repo, _head(repo))
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "capture-link"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip(
            "creating a symlink requires Windows developer mode or elevated privileges"
        )
    with pytest.raises(ProjectError, match="symlink or reparse point"):
        capture.materialize(symlink)

    unsafe_file = replace(capture.files[0], path="../escape")
    unsafe = replace(capture, files=(unsafe_file,))
    with pytest.raises(ProjectError, match="unsafe source path"):
        unsafe.materialize(outside)

"""Refusals and fingerprints whose guarantee is stated but nothing checked.

Two claims in the tree stand on nothing a test exercised.

`_resources` documents that "a mapping is accepted because an MCP client
naturally has one", and every one of its refusal messages names what would fix
the request. Neither the mapping form nor any of the four refusals it raises
had a check standing behind it.

`_worktree_record` documents that the kind is explicit "so a recorded record can
never be mistaken for a content hash", and the two non-file kinds exist so the
digest can tell a tracked path with no worktree file from a tracked file. No
test named either kind.
"""
from __future__ import annotations
import subproc

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.identity import compute_source_identity, source_unchanged  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.tasks import AdmissionRefused, _resources  # noqa: E402




def test_a_mapping_names_each_resource_by_its_key() -> None:
    """The mapping form the docstring promises an MCP client normalises to the list form."""
    assert _resources({"checkout": {"kind": "exclusive"}}) == (
        {"key": "checkout", "kind": "exclusive", "capacity": None},
    )
    assert _resources({"pool": {"kind": "capacity", "capacity": 2}}) == (
        {"key": "pool", "kind": "capacity", "capacity": 2},
    )


def test_a_mapping_and_the_list_it_normalises_to_are_one_shape() -> None:
    """Both spellings a client may send produce the same stored declaration."""
    assert _resources({"checkout": {"kind": "exclusive"}}) == _resources(
        [{"key": "checkout", "kind": "exclusive"}]
    )


def test_a_mapping_value_that_is_not_an_object_is_refused() -> None:
    """A shorthand like `{"checkout": "exclusive"}` refuses, it does not raise out.

    `{"key": key, **value}` unpacks the value, so a client sending a bare string
    would otherwise get a `TypeError` out of the admission path instead of the
    refusal that names what to send.
    """
    with pytest.raises(AdmissionRefused) as raised:
        _resources({"checkout": "exclusive"})
    assert str(raised.value) == (
        "required resource 'checkout' must name an object, not str"
    )


def test_a_mapping_value_naming_its_own_key_is_refused() -> None:
    """Two names for one resource is ambiguous, and resolving it silently is wrong.

    Unpacking the value after the key would let the inner `key` win, so
    `{"checkout": {"key": "publish"}}` would claim `publish` while the caller
    reads the declaration as being about `checkout`.
    """
    with pytest.raises(AdmissionRefused) as raised:
        _resources({"checkout": {"key": "publish"}})
    assert str(raised.value) == (
        "required resource 'checkout' is named twice: by the mapping key and by a "
        "key field; give it one name"
    )


@pytest.mark.parametrize(
    "raw, message",
    [
        ("checkout", "required_resources must be a list of resource objects"),
        ([["checkout"]], "each required resource must be an object"),
        (
            [{"key": "checkout", "holds": 2}],
            "a required resource has unsupported key(s) holds; a resource declares "
            "key, kind and capacity",
        ),
        ([{"key": "   "}], "a required resource needs a nonempty key"),
        (
            [{"key": "checkout"}, {"key": "checkout"}],
            "resource 'checkout' is required twice in one contract",
        ),
        (
            [{"key": "checkout", "kind": "shared"}],
            "resource 'checkout' has unknown kind 'shared'; use exclusive or capacity",
        ),
        (
            [{"key": "checkout", "kind": "exclusive", "capacity": 2}],
            "resource 'checkout' is exclusive and cannot declare a capacity",
        ),
    ],
)
def test_a_malformed_declaration_is_refused_by_name(raw, message: str) -> None:
    """Every refusal names the resource, so a caller knows which declaration to fix."""
    with pytest.raises(AdmissionRefused) as raised:
        _resources(raw)
    assert str(raised.value) == message


def test_nothing_is_declared_by_omitting_it() -> None:
    """The absence of a declaration is not a malformed one."""
    assert _resources(None) == ()




def git(repo: Path, *args: str) -> str:
    return subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


def test_tracked_files_are_the_ones_this_worktree_holds(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "app.py")
    git(root, "commit", "-qm", "initial")

    identity = compute_source_identity(open_project(root))
    assert identity.tracked_files == 1
    assert not identity.dirty


def test_a_tracked_file_deleted_from_the_worktree_changes_the_digest(tmp_path: Path) -> None:
    """A tracked path with no worktree file is recorded, not dropped.

    `git ls-files` still lists it, so it stays in the fingerprint and the digest
    moves. Silently omitting it would let a checkout with a deleted source read
    as the commit it no longer is.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "side.py").write_text("y = 2\n", encoding="utf-8")
    git(root, "add", "app.py", "side.py")
    git(root, "commit", "-qm", "initial")
    before = compute_source_identity(open_project(root))

    (root / "side.py").unlink()
    after = compute_source_identity(open_project(root))

    assert not source_unchanged(before, after), (
        "a deleted tracked file is still tracked, so the fingerprint must move"
    )


def test_a_submodule_is_recorded_by_kind_and_not_by_its_contents(tmp_path: Path) -> None:
    """A tracked path with no worktree file of its own is a gitlink, not a file.

    A submodule's contents live in another repository, so hashing what is under
    the path would fingerprint the wrong repository. The digest has to record
    that the path exists, and that it is a gitlink.
    """
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q")
    (inner / "lib.py").write_text("z = 3\n", encoding="utf-8")
    git(inner, "add", "lib.py")
    git(inner, "commit", "-qm", "inner")
    inner_sha = git(inner, "rev-parse", "HEAD").strip()

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "app.py")
    git(root, "commit", "-qm", "initial")
    (root / "vendor").mkdir()
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{inner_sha},vendor")
    git(root, "commit", "-qm", "add submodule")

    identity = compute_source_identity(open_project(root))
    assert identity.tracked_files == 2, "the gitlink is tracked and must be counted"

    (inner / "lib.py").write_text("z = 4\n", encoding="utf-8")
    assert source_unchanged(identity, compute_source_identity(open_project(root))), (
        "a submodule's contents belong to another repository and must not move this digest"
    )


def _repo_with(root: Path, tmp_path: Path, *, kind: str) -> Path:
    """One repository whose `vendor` path is either a gitlink or a deleted file.

    Everything else about the tree is identical, including the path name, so the
    only thing that can move the digest between the two is the recorded kind.
    """
    root.mkdir()
    git(root, "init", "-q")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "app.py")
    git(root, "commit", "-qm", "initial")
    if kind == "gitlink":
        inner = tmp_path / f"inner-{root.name}"
        inner.mkdir()
        git(inner, "init", "-q")
        (inner / "lib.py").write_text("z = 3\n", encoding="utf-8")
        git(inner, "add", "lib.py")
        git(inner, "commit", "-qm", "inner")
        sha = git(inner, "rev-parse", "HEAD").strip()
        (root / "vendor").mkdir()
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor")
    else:
        (root / "vendor").write_text("y = 2\n", encoding="utf-8")
        git(root, "add", "vendor")
        (root / "vendor").unlink()
    git(root, "commit", "-qm", f"add vendor as {kind}")
    return root


def test_a_gitlink_and_a_deleted_file_are_not_the_same_record(tmp_path: Path) -> None:
    """The two non-file kinds exist to be told apart, so the digest must differ.

    Both are "tracked, and there is no file here to hash". If they produced one
    record, swapping a submodule for a deleted file would be invisible.
    """
    as_gitlink = _repo_with(tmp_path / "repo-gitlink", tmp_path, kind="gitlink")
    as_deleted = _repo_with(tmp_path / "repo-absent", tmp_path, kind="absent")

    digests = {
        name: compute_source_identity(open_project(path)).inventory_digest
        for name, path in (("gitlink", as_gitlink), ("absent", as_deleted))
    }
    assert digests["gitlink"] != digests["absent"], (
        "a submodule and a deleted file are different records of the same tree"
    )


def test_a_tracked_file_deleted_from_the_worktree_is_still_counted_as_tracked(
    tmp_path: Path,
) -> None:
    """`ls-files` names both, so the record count cannot tell them apart either.

    The kind is the only thing in the record that does, which is why the previous
    test has to look at the digest rather than at `tracked_files`.
    """
    root = _repo_with(tmp_path / "repo-absent", tmp_path, kind="absent")
    identity = compute_source_identity(open_project(root))
    assert identity.tracked_files == 2, (
        "git still tracks both app.py and the deleted vendor"
    )
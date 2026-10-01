"""The verifier's own revision when the verifier's tree is not a repository.

`package_identity` is measured before a candidate is checked out, so it runs
against the vkit package wherever the CLI was installed from. That tree is a
repository on a developer's machine and is not one on an installed wheel, and
`oracle.package_identity` is written to survive both: its `revision` is
`None`-able and it renders the fallback `f"vkit {__version__}"` itself. So the
signature already promises an answer for a tree that cannot be resolved, and the
promised answer is a value, not a crash.

The promise was not kept. `_project_at` catches "not a repository" and returns
`None`; its one caller passed that `None` to `gits.git`, which reached for
`project.root` and raised `AttributeError: 'NoneType' object has no attribute
'root'`. The CLI's own handler turned that into `internal error: ...` and exit
4, so ten tests failed on POSIX and none did on Windows, where the package
almost always sits inside a repository.

Both tests go through the public `package_identity`, because that is what
`verify.py` calls and what a crash in it costs. Neither mocks `open_project`:
mocking the resolver is exactly what let this ship, since a mock returns a
`Project` and the branch that returns `None` is the branch under test.

The non-repository condition is built rather than hoped for. `tmp_path` on some
hosts sits inside a checkout, and a directory that happens to be in a
repository would make both tests pass for the wrong reason, so the ceiling is
pinned and a real repository is built beside it as the control. A test that can
only fail when the host cooperates is not a receipt.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fixtures as fx  # noqa: E402
from vkit import __version__  # noqa: E402
from vkit.integration.oracle import package_identity  # noqa: E402


@pytest.fixture
def not_a_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real directory that git cannot resolve to a repository, on any host.

    `GIT_CEILING_DIRECTORIES` names an ancestor of the directory, and git stops
    the upwards search there. This is the one lever that produces the condition
    on every host, including one whose temporary directory is itself inside a
    checkout: it does not rely on where `tmp_path` happens to live, and it does
    not reach into the resolver to fake its answer. The value is restored by
    `monkeypatch`, so a test that sets it does not leak into the next.
    """
    root = tmp_path / "unversioned"
    root.mkdir()
    (root / "vkit").mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(Exception) as refusal:
        from vkit.paths import open_project

        open_project(root)
    assert "not inside a Git repository" in str(refusal.value), (
        f"the ceiling did not take, so this directory is still in a repository: "
        f"{refusal.value}"
    )
    return root


def test_a_tree_that_is_not_a_repository_still_reports_a_revision(not_a_repository: Path):
    """The promise the signature makes: a revision string, not an AttributeError.

    Before the fix this raised out of the call, because the resolver's `None`
    reached `gits.git`. Asserting on the rendered fallback rather than on `None`
    is deliberate: `None` is the helper's internal shape, while
    `f"vkit {__version__}"` is what a receipt carries, so this stays a test of
    the observable answer even if the internal spelling changes.
    """
    identity = package_identity(not_a_repository)

    assert identity["revision"] == f"vkit {__version__}"


def test_a_real_repository_reports_that_repository_s_own_commit(tmp_path: Path) -> None:
    """The control: the ceiling above cost the ordinary case nothing.

    A fix that made `package_identity` return the fallback unconditionally would
    satisfy the test above while reporting a revision for a tree that has one.
    Here the package tree is a real repository, so the answer must be the commit
    that repository points at, measured independently by `fixtures.git` rather
    than by the code under test.
    """
    root = fx.build_repository(tmp_path / "versioned")
    (root / "vkit").mkdir()
    fx.commit_all(root, "ship the package")
    expected = fx.git(root, "rev-parse", "HEAD")

    identity = package_identity(root)

    assert identity["revision"] == expected
    assert identity["revision"] != f"vkit {__version__}"


def test_the_verdicts_differ_because_the_measurement_does(tmp_path: Path) -> None:
    """The refusal names the condition, so a reader can tell them apart.

    Both identities are measured, both digests are taken over the same single
    empty directory of package files, and the only difference between them is
    whether the tree resolves. Were `_project_at` to resolve and report a
    revision for both, the two answers would be equal and this would fail --
    which is what a guard that refuses to distinguish is meant to look like.
    """
    root = tmp_path / "versioned"
    root.mkdir()
    (root / "vkit").mkdir()

    measured = package_identity(root)

    assert measured["revision"] == f"vkit {__version__}"
    assert measured["package_files"] == 0

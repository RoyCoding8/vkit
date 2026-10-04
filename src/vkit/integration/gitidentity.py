"""Commits are the identity. A branch name is a label that can be moved.

Everything the integration operation needs to say "this is the code I decided
about" reduces to four things: which commit the candidate is, which commit it
was built on, which commit the target is, and which repository it all came from.
The first three are Git facts and Git answers them exactly. The fourth is
computed here because no Git plumbing call states it.

**What a repository identity is.** The hash of the first commit reachable from
the candidate. Two clones of one history share it, so it answers "are these the
same project" and not "are these the same directory". The remote URL is
recorded beside it as evidence and a remote can be added, changed or removed
after the fact, so it never decides identity. A repository with no commits
cannot be an integration candidate at all, which is why this raises rather than
returning a placeholder.

**Why containment is the argument.** A candidate that does not contain the
target cannot be published onto it, whatever the checks say. Verifying that up
front means the slow path only runs for a candidate that could actually be
published, and a REJECTED answer about an unrelated commit never gets to read as
an integration decision. `git merge-base --is-ancestor` is the exact predicate
and it costs one call.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..nowindow import hidden_window

from ..paths import Project


class GitError(Exception):
    """A Git query failed, or answered something this module will not guess at."""


def git(project: Project, *args: str) -> str:
    """Run git in the project and return stripped UTF-8 output.

    UTF-8 is pinned rather than inherited, for the reason `paths._git` records:
    a ref or path read back with the wrong encoding is a different string, not
    an error, and the mismatch surfaces later as something unrelated.
    """
    try:
        done = subprocess.run(
            ["git", *args], cwd=project.root, capture_output=True,
            encoding="utf-8", errors="replace", timeout=60, check=False,
            **hidden_window(),
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except NotADirectoryError as exc:
        raise GitError(f"{project.root} is not a usable working directory: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()
        raise GitError(detail[-1] if detail else f"git {' '.join(args)} failed")
    return done.stdout if "-z" in args else done.stdout.strip()


def resolve_commit(project: Project, ref: str, *, what: str) -> str:
    """The commit a ref names, or a refusal naming the ref.

    `ref^{commit}` is deliberate rather than `ref` alone: a tag, a branch and an
    annotated tag all resolve, and so does an expression like `main~3`. What it
    refuses is a ref that does not exist, which must be a BLOCKED answer with a
    name in it rather than a stack trace.
    """
    if not ref or not ref.strip():
        raise GitError(f"no {what} reference was given")
    try:
        return git(project, "rev-parse", "--verify", f"{ref.strip()}^{{commit}}")
    except GitError as exc:
        raise GitError(f"{what} {ref!r} does not resolve to a commit: {exc}") from exc


def is_ancestor(project: Project, ancestor: str, descendant: str) -> bool:
    """Whether `ancestor` is reachable from `descendant`."""
    done = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=project.root, capture_output=True, timeout=60, check=False,
        **hidden_window(),
    )
    if done.returncode == 0:
        return True
    if done.returncode == 1:
        return False
    raise GitError(
        f"could not compare {ancestor} and {descendant}: "
        f"{done.stderr.decode('utf-8', 'replace').strip() or 'git merge-base failed'}"
    )


@dataclass(frozen=True)
class RepositoryIdentity:
    """Which project this is, independently of where it is checked out."""

    root_commit: str
    remote_url: str | None

    def to_json(self) -> dict[str, str | None]:
        return {"root_commit": self.root_commit, "remote_url": self.remote_url}

    def describe(self) -> str:
        return f"root {self.root_commit[:12]}" + (
            f" remote {self.remote_url}" if self.remote_url else " (no remote)"
        )


def repository_identity(project: Project) -> RepositoryIdentity:
    """The identity of the project, computed from its own history.

    The root commit is the earliest commit reachable from HEAD, which is the
    same value in every clone of that history, however far the branches differ.
    `rev-list --max-parents=0` returns all roots, so the smallest of them is
    taken: a repository that somehow has two roots is malformed, and naming the
    oldest of them is stable where naming the first returned would not be.
    """
    roots = [
        line for line in git(project, "rev-list", "--max-parents=0", "HEAD").splitlines()
        if line.strip()
    ]
    if not roots:
        raise GitError(
            f"{project.root} has no commits, so there is nothing to identify and "
            "nothing an integration candidate could be built from"
        )
    root_commit = min(roots)
    return RepositoryIdentity(root_commit=root_commit, remote_url=remote_url(project))


def remote_url(project: Project) -> str | None:
    """The configured remote, or None. Evidence, never identity.

    A missing remote is normal for a local repository and must not read as a
    failure, so this returns None rather than raising.
    """
    try:
        value = git(project, "remote", "get-url", "origin")
    except GitError:
        return None
    return value or None


@dataclass(frozen=True)
class CommitFact:
    """One commit as the evidence needs it, read from Git rather than believed."""

    sha: str
    tree: str
    parents: tuple[str, ...]
    subject: str
    committed_at: str

    def to_json(self) -> dict[str, object]:
        return {
            "sha": self.sha,
            "tree": self.tree,
            "parents": list(self.parents),
            "subject": self.subject,
            "committed_at": self.committed_at,
        }


_FIELD = "%H%x00%T%x00%P%x00%aI%x00%s"


def commit_fact(project: Project, sha: str) -> CommitFact:
    """Read one commit's identity and content address.

    The tree hash is the important field. It is the address of everything the
    commit contains, so a later read of `git ls-tree <sha>` can prove that a
    byte came from that commit rather than from whatever the checkout holds now.
    """
    raw = git(project, "show", "--no-patch", f"--format={_FIELD}", sha)
    if "\x00" not in raw:
        raise GitError(f"could not read commit facts for {sha}")
    head, tree, parents, committed_at, subject = raw.split("\x00", 4)
    return CommitFact(
        sha=head.strip(),
        tree=tree.strip(),
        parents=tuple(p for p in parents.split() if p),
        subject=subject.strip(),
        committed_at=committed_at.strip(),
    )


def git_blob(project: Project, commit: str, path: str) -> bytes:
    """The exact bytes of one file at one commit.

    `Path.as_posix` because Git always names repository paths with forward
    slashes, on every platform, and a Windows separator here silently reads a
    file that does not exist. `cat-file blob` is used rather than `show` so a
    path that also names a ref cannot change which object is read.
    """
    done = subprocess.run(
        ["git", "cat-file", "blob", f"{commit}:{Path(path).as_posix()}"],
        cwd=project.root, capture_output=True, timeout=60, check=False,
        **hidden_window(),
    )
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(
            f"{path} does not exist at commit {commit}: "
            f"{detail[-1] if detail else 'git cat-file failed'}"
        )
    return done.stdout


def digest_bytes(*chunks: bytes) -> str:
    """One sha256 over several byte strings, unambiguously separated.

    NUL cannot appear in any of the inputs here (they are JSON documents and
    decoded file bytes), and separating anyway means two different splits can
    never hash to the same value.
    """
    running = hashlib.sha256()
    for chunk in chunks:
        running.update(chunk)
        running.update(b"\x00")
    return running.hexdigest()

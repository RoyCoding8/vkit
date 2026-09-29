"""A reproducible fingerprint of the repository's source, at one moment.

A run computes this before the check and again after it. If the two digests
differ, the code moved underneath the process and the evidence describes
something that no longer exists, which is a BLOCKED outcome with reason
`source_changed` rather than a pass or a fail.

The fingerprint hashes file CONTENT, not names. A repository where one file was
edited and the file list is otherwise identical has to produce a different
digest, or the whole check is theatre. Git already has a content hash for every
blob, but the index hash describes what is *staged*; an unstaged edit is
exactly the thing being detected, so the bytes are read from the worktree and
hashed here.

## What is in the inventory, and what is left out

In: every path tracked in the index, plus every untracked path that the
repository has not ignored. The manifest, the drivers and the declared fixture
inputs need no special case — they are repository files like any other, and a
rule that re-derives them from the tree cannot drift from the manifest the way
a second, hand-maintained list would.

Out, and why:

* Paths the repository's own ignore rules exclude. The `.gitignore` chain *is*
  the repository declaring what is generated output and scratch, so honouring
  it means one declaration rather than two that can disagree. Gitignored
  build output therefore cannot make a run look changed.
* Known secret inputs (`.env*`, private keys, credential files). They are never
  opened, so no secret material can reach a persisted report, and their
  presence does not dirty a tree. The cost is a stated limit: a check whose
  behaviour genuinely depends on a secret file's content is not covered by
  this fingerprint.
* The Git administrative directory, which is not source.

Stated limits, per CONTRACT.md's requirement that they be described rather than
implied: this cannot detect a transient edit that is reverted during the run,
and it hashes the bytes the worktree holds, so a symbolic link is followed to
its target and a tracked path with no worktree file (a submodule) is recorded
by kind rather than by content.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import Project, ProjectError

_SECRET_NAMES = frozenset(
    {
        ".htpasswd",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_SECRET_SUFFIXES = frozenset({".key", ".keystore", ".p12", ".pem", ".pfx"})
_ENV_PREFIX = ".env"

_HEAD_SEPARATOR = b"\x00"


def _is_excluded(relative: str) -> bool:
    """Whether a repository-relative path is left out of the fingerprint.

    Applied once to the union of tracked and untracked paths, so a secret is
    excluded whether or not someone committed it. Untracked paths have already
    been through the repository's ignore rules before they reach here.
    """
    for part in relative.split("/"):
        if part == _ENV_PREFIX or part.startswith(_ENV_PREFIX + "."):
            return True
        if part in _SECRET_NAMES or Path(part).suffix.lower() in _SECRET_SUFFIXES:
            return True
    return False


def _run(project: Project, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args], cwd=project.root, capture_output=True, timeout=30, check=False
        )
    except FileNotFoundError as exc:
        raise ProjectError("git is not installed or not on PATH") from exc


def _git(project: Project, *args: str) -> bytes:
    """Raw output. NUL-delimited records must survive intact, and the text
    helper in `paths` returns stripped text, so identity runs git itself."""
    done = _run(project, *args)
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProjectError(detail[-1] if detail else f"git {' '.join(args)} failed")
    return done.stdout


def _list_paths(project: Project, *args: str) -> set[str]:
    """The paths a NUL-delimited `git ls-files` lists.

    `surrogateescape` round-trips whatever bytes git emitted, so a path that is
    not valid UTF-8 is still hashed and reported exactly as git names it.
    """
    return {
        raw.decode("utf-8", "surrogateescape")
        for raw in _git(project, *args).split(b"\x00")
        if raw
    }


def _head(project: Project) -> str:
    """The resolved commit, or the branch a first commit will land on.

    `rev-parse` fails on a repository with no commits, but the report schema
    requires a non-empty string, and the unborn branch name is the honest
    description of a repository that has nothing to identify yet.
    """
    done = _run(project, "rev-parse", "--verify", "--quiet", "HEAD")
    if done.returncode == 0 and done.stdout.strip():
        return done.stdout.decode("ascii").strip()
    return _git(project, "symbolic-ref", "HEAD").decode("utf-8").strip()


def _worktree_record(root: Path, relative: str) -> tuple[str, str]:
    """(kind, value) for one path, read from the worktree.

    The kind is explicit rather than folded into the value, so a recorded
    record can never be mistaken for a content hash.
    """
    path = root / relative
    if path.is_dir():
        return ("gitlink", "")
    if not path.is_file():
        return ("absent", "")
    return ("file", hashlib.sha256(path.read_bytes()).hexdigest())


def _digest(records: list[tuple[str, str, str]]) -> str:
    """One sha256 over every record, sorted.

    Sorting is what makes the digest independent of the order git happens to
    list things in. NUL cannot appear in a path, so it separates the three
    fields unambiguously.
    """
    running = hashlib.sha256()
    for path, kind, value in sorted(records):
        for field in (path.encode("utf-8", "surrogateescape"), kind.encode("ascii"), value.encode("ascii")):
            running.update(field)
            running.update(_HEAD_SEPARATOR)
    return running.hexdigest()


def _dirty_paths(project: Project) -> tuple[str, ...]:
    """Every path whose state differs from HEAD, tracked or untracked.

    An untracked source file counts: HEAD does not describe it, so the tree is
    not a clean checkout of the commit the report will name.

    `--untracked-files=all` is required, not a preference. The default
    collapses a wholly untracked directory to a single `dir/` entry, which
    names no file and would leave a new file invisible in the report.
    """
    fields = _git(
        project, "status", "--porcelain", "-z", "--untracked-files=all"
    ).split(b"\x00")
    dirty: list[str] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        code = record[:2].decode("ascii", "replace")
        if code[0] in ("R", "C"):
            # `git status -z` writes a rename or copy origin as its own
            # NUL-terminated field right after the entry. It is the same
            # change, not a second one, and the path reported is the one that
            # exists in the tree.
            index += 1
        path = record[3:].decode("utf-8", "surrogateescape")
        if not _is_excluded(path):
            dirty.append(path)
    return tuple(sorted(dirty))


@dataclass(frozen=True)
class SourceIdentity:
    """The `sourceIdentity` $def of schemas/run-report.v1.json, field for field.

    The field names are the schema's property names on purpose. The schema sets
    `additionalProperties: false`, so a report that fails its own contract is
    worse than no report, and matching the names here makes the mapping to
    `to_json` a copy rather than a translation somebody has to remember.
    """

    head: str
    inventory_digest: str
    dirty: bool
    tracked_files: int
    dirty_paths: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "inventory_digest": self.inventory_digest,
            "dirty": self.dirty,
            "tracked_files": self.tracked_files,
            "dirty_paths": list(self.dirty_paths),
        }


def compute_source_identity(project: Project) -> SourceIdentity:
    """Fingerprint the project's source as it stands right now.

    Call once before a check runs and once after; hand both to
    `source_unchanged`.
    """
    tracked = _list_paths(project, "ls-files", "-z")
    untracked = _list_paths(project, "ls-files", "--others", "--exclude-standard", "-z")
    included = {p for p in tracked | untracked if not _is_excluded(p)}

    records = [
        (path, *_worktree_record(project.root, path)) for path in included
    ]

    dirty = _dirty_paths(project)
    return SourceIdentity(
        head=_head(project),
        inventory_digest=_digest(records),
        dirty=bool(dirty),
        tracked_files=len(tracked & included),
        dirty_paths=dirty,
    )


def source_unchanged(before: SourceIdentity, after: SourceIdentity) -> bool:
    """Whether the run executed the source its report describes.

    The digest decides, and HEAD deliberately does not. A commit that rewrites
    no file content — a rebase, an empty commit — moves HEAD without changing
    the code that ran, and calling that a change would throw away evidence
    that is still accurate. HEAD is recorded as provenance, not as the
    change detector.
    """
    return before.inventory_digest == after.inventory_digest

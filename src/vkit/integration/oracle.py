"""The bytes an ACCEPTED rests on, measured rather than named.

An integration decision is only as trustworthy as the code that produced it, and
two sets of bytes produce it: this vkit package, and the verification code the
approved manifest names. Neither of those belongs to the candidate. The
candidate's *product* is the thing under test, so its bytes are free to move;
the code that decides whether those bytes pass is not, and this module is the
one place that says so.

**Everything is read from commits and from the imported package, never from a
checkout.** The context is therefore a property of the request rather than of
whether a checkout happened to exist when it was computed, which is what lets
one function answer for every decision path, including the ones that refuse
before any checkout is made.

**The boundary that matters.** A trusted command string naming a path inside the
candidate checkout is not a trusted oracle: the candidate supplies those bytes.
So the script an approved check names is materialized from the approved revision
and executed from there, while the working directory stays in the candidate
checkout. The verification code and the expectations are approved; the product
under test is the candidate's. That is the whole of the rule.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..manifest import Manifest
from ..paths import Project
from . import gitidentity as gits


def package_root() -> Path:
    """The tree that holds this vkit package -- its parent, not the package.

    `sandbox._package_root` resolves a plugin root by looking for
    `<root>/vkit/__init__.py`, so the root is the directory CONTAINING the
    package. Passing the package directory itself is a mistake that only shows
    up when a check actually launches, which is why the name is explicit here
    and the value is derived rather than spelled.
    """
    from .. import __file__ as package_init

    return Path(package_init).resolve().parent.parent


def package_identity(root: Path | None = None) -> dict[str, Any]:
    """The bytes of this verifier, as a digest over every file it ships.

    A commit cannot be the identity of the code that ran, because a working tree
    may be dirty and an installed wheel has no commit at all. The digest of the
    actual files can be neither. The revision is recorded beside it as
    provenance, and is explicitly not what the identity rests on.

    The digest covers the `vkit` package directory, which is the code that read
    the result, and not `root`, which also holds anything else a developer keeps
    beside it. `__pycache__` and compiled bytecode are excluded because they are
    not code anyone shipped, and including them would make a receipt change every
    time the interpreter ran.
    """
    from .. import __version__

    root = (root or package_root()).resolve()
    package = root / "vkit"
    files = sorted(
        (path.relative_to(package).as_posix(), path)
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    digest = gits.digest_bytes(
        *[f"{name}\n".encode("utf-8") + path.read_bytes() for name, path in files]
    )
    return {
        "revision": _package_revision(root) or f"vkit {__version__}",
        "package_root": str(root),
        "package_digest": digest,
        "package_files": len(files),
    }


def _package_revision(root: Path) -> str | None:
    """The commit the verifier's own checkout points at, when there is one."""
    try:
        return gits.git(_project_at(root), "rev-parse", "HEAD")
    except gits.GitError:
        return None


def _project_at(root: Path):
    from ..paths import open_project

    try:
        return open_project(root)
    except Exception:  # noqa: BLE001 - not being in a repository is normal here
        return None


def environment_identity() -> dict[str, Any]:
    """The interpreter and platform the decision was made on, and nothing else.

    A full environment dump is a credential leak waiting to happen, and a reader
    needs only to know which Python ran the checks.
    """
    import platform
    import sys

    return {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
    }


def scripts_of(manifest: Manifest) -> dict[str, str]:
    """The repository-relative script each check executes as its own code.

    A check's first argument is usually a program name and the next is the file
    that program runs. A check that names a module (`-m pkg`) or an absolute
    path outside the tree names no file the approved revision can pin, and is
    reported as pinning nothing rather than guessed at -- which is also why the
    caller records the list: an empty one is a fact a reviewer needs.

    Elements are matched by shape rather than by flag arity, because no manifest
    declares which flag takes a value and a rule that guessed wrong would pin
    the wrong file, which is worse than pinning none.
    """
    scripts: dict[str, str] = {}
    for check_id, check in manifest.checks.items():
        for part in check.argv:
            if part.startswith("-"):
                continue
            if "{{" in part:
                continue
            if part == check.argv[0] and "/" not in part and "\\" not in part:
                continue
            candidate = Path(part)
            if candidate.is_absolute() or ".." in candidate.parts:
                break
            if candidate.suffix:
                scripts[check_id] = candidate.as_posix()
                break
    return scripts


def blob_digest(project: Project, commit: str, path: str) -> str | None:
    """The sha256 of one file at one commit, or None when it is not there."""
    try:
        return hashlib.sha256(gits.git_blob(project, commit, path)).hexdigest()
    except gits.GitError:
        return None


def repoint_approved(manifest: Manifest, approved_root: Path, scripts: dict[str, str]) -> Manifest:
    """The same manifest, with every check's script resolved to the approved bytes.

    Only the script argument moves. The interpreter, the flags, the working
    directory and the required scenarios are the approved ones already, and the
    working directory deliberately stays in the candidate checkout so that the
    approved driver imports the candidate's product and nothing else.

    A check whose script is not in `scripts` was pinned to nothing by
    `scripts_of`, and is left exactly as the approved manifest declared it. That
    is a refusal by the caller rather than a guess here: a check the shape cannot
    explain has no approved code identified, and silently keeping the candidate's
    path would be the substitution this module exists to stop.
    """
    from dataclasses import replace

    checks = {}
    for check_id, check in manifest.checks.items():
        script = scripts.get(check_id)
        if script is None:
            checks[check_id] = check
            continue
        target = approved_root / script
        argv = tuple(
            str(target) if part == script else part for part in check.argv
        )
        checks[check_id] = replace(check, argv=argv)
    return replace(manifest, checks=checks)


@dataclass(frozen=True)
class Oracle:
    """The trusted side of one verification: what decided, and what was pinned.

    `approved_revision` is the commit the policy names, and `approved` holds the
    digests of the verification code as that commit shipped it. `candidate` holds
    the digests of the same paths as the candidate shipped them. A path present in
    one and not the other is a difference, and that comparison is the only thing
    that stands between a candidate's own driver and a decision about it.
    """

    approved_revision: str | None
    approved: dict[str, str]
    candidate: dict[str, str]
    source: str

    @property
    def changed(self) -> dict[str, tuple[str | None, str | None]]:
        """{path: (approved digest, candidate digest)} where they disagree."""
        differing: dict[str, tuple[str | None, str | None]] = {}
        for path in sorted(set(self.approved) | set(self.candidate)):
            before, after = self.approved.get(path), self.candidate.get(path)
            if before != after:
                differing[path] = (before, after)
        return differing

    def to_json(self) -> dict[str, Any]:
        return {
            "approved_revision": self.approved_revision,
            "source": self.source,
            "approved_digests": self.approved,
            "candidate_digests": self.candidate,
        }


def approved_oracle(
    project: Project, candidate: str, manifest: Manifest, approved_revision: str | None
) -> Oracle:
    """Measure the trusted verification code and the candidate's copy of it.

    With a pinned revision the approved side is read at that revision and the
    candidate side at the candidate commit, and any difference is a refusal. With
    no pinned revision there is no approved side to compare against: a local
    policy pins nothing, so the candidate's own verification code is what runs.
    Both sides are then the candidate's bytes and nothing differs, which is the
    honest reading rather than a difference manufactured to make the run refuse.
    `source` says which of the two it was, and a caller that wants approved
    verification code is the caller that has to pin a revision.
    """
    scripts = scripts_of(manifest)
    candidate = {path: blob_digest(project, candidate, path) for path in scripts.values()}
    if approved_revision is None:
        return Oracle(
            approved_revision=None,
            approved=dict(candidate),
            candidate=candidate,
            source="candidate-manifest: the policy pinned no approved revision, so the "
                   "candidate's own verification code is what ran",
        )
    return Oracle(
        approved_revision=approved_revision,
        approved={
            path: blob_digest(project, approved_revision, path) for path in scripts.values()
        },
        candidate=candidate,
        source="approved-manifest: the verification code is the policy's pinned revision",
    )


def fixture_identity(
    project: Project, candidate: str, manifest: Manifest, scripts: dict[str, str]
) -> dict[str, Any]:
    """What the checks were given, measured at the candidate commit.

    The manifest declares which files a check reads, which is a claim; the
    digest of those files as the candidate shipped them is a fact. The two are
    kept apart by role, because a check's product input and its verification
    code are not the same kind of thing and a candidate is allowed to change
    only the first.

    Read from the commit rather than from the checkout so that a decision's
    receipt describes the candidate even after the checkout is gone. A declared
    input the candidate does not ship is recorded as absent rather than skipped,
    because an input that cannot be accounted for is a gap and gaps are not
    success.
    """
    declared: set[str] = set()
    for check in manifest.checks.values():
        declared.update(check.inputs)
    executable = set(scripts.values())
    inputs = [
        {
            "path": path,
            "role": "checker" if path in executable else "product",
            "sha256": blob_digest(project, candidate, path),
        }
        for path in sorted(declared)
    ]
    return {
        "inputs": inputs,
        "missing": [entry["path"] for entry in inputs if entry["sha256"] is None],
        "digest": gits.digest_bytes(
            *[f"{e['path']}\n{e['role']}\n{e['sha256'] or ''}\n".encode("utf-8")
              for e in inputs]
        ),
    }

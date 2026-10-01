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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..manifest import Manifest
from ..paths import Project, ProjectError, open_project
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
    """The commit the verifier's own checkout points at, when there is one.

    `None` is a real answer here and not a failure to be worked around. The
    verifier is measured before any candidate is checked out, so this runs
    against the vkit tree wherever the CLI was installed from. That tree is a
    repository in a checkout and is not one in an installed wheel or in a
    worktree git cannot resolve, and `package_identity` renders the fallback
    itself. A directory with no repository therefore has to arrive here as a
    value.

    Both reasons a revision is unavailable are named, and neither is a guess:
    `ProjectError` is what `open_project` raises for a path that is not a
    project, and `GitError` is what git raises for a repository with no `HEAD`
    to read. The earlier code caught the first as a bare `Exception` inside a
    helper that returned `None`, and handed that `None` to `git` as if it were
    a `Project`. The condition is caught where the answer is produced now,
    which is also why the helper is gone: it had one caller, and the `None` it
    returned was an untyped sentinel that only this line knew to mean anything.
    """
    try:
        return gits.git(open_project(root), "rev-parse", "HEAD")
    except (ProjectError, gits.GitError):
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

    The operand of `-m` is a module name and is skipped before the shape test
    runs, because a module name and a path are told apart by their position and
    not by their spelling. `pkg.mod` is a file name to every path reader in this
    module, so the shape test cannot refuse it: the earlier code ran the shape
    test on it, pinned `pkg.mod`, and the three things that follow from pinning
    a path that exists in no commit all went wrong together. `repoint_approved`
    substituted the approved tree's path for the module name, so the approved
    command was rewritten into one Python cannot resolve; `approved_oracle`
    digested a path absent from both commits and reported no change; and
    `verify.py`'s `checker_not_identifiable` refusal, which covers exactly this
    case, never fired because the check looked pinned.

    Only the element immediately after `-m` is skipped. Going further would mean
    knowing which flags take a value, and no manifest declares that:
    `pytest -m "not slow" verify.py` is a filter followed by a script this
    module can pin, while `python -m pkg.mod verify.py` passes `verify.py` to
    the module as an argument. The two are indistinguishable without flag arity,
    and the shape rule above exists precisely because a rule that guessed wrong
    here would pin the wrong file.

    Elements are otherwise matched by shape rather than by flag arity, for the
    same reason. Whether a part pins a file is decided by `_pins_a_file`, which
    reads the string in both path flavours. Deciding it with the host's own
    `Path` was the defect: a manifest is portable data, so
    `C:/elsewhere/verify_price.py` is a legitimate spelling that a POSIX host
    parses as relative, and such a check was pinned as a repository-relative
    script. `repoint_approved` then joined that onto `approved_root`, so the
    approved run executed a path outside the approved tree -- on the very host
    the boundary exists to hold.
    """
    scripts: dict[str, str] = {}
    for check_id, check in manifest.checks.items():
        module_names = {
            part for before, part in zip(check.argv, check.argv[1:]) if before == "-m"
        }
        for part in check.argv:
            if part.startswith("-"):
                continue
            if "{{" in part:
                continue
            if part in module_names:
                continue
            if part == check.argv[0] and "/" not in part and "\\" not in part:
                continue
            if _pins_a_file(part):
                scripts[check_id] = Path(part).as_posix()
                break
    return scripts


def _pins_a_file(part: str) -> bool:
    """True when `part` is a repository-relative path to a script.

    A check pins a file only when the string names one that lives inside the tree
    the approved revision materializes. Everything else pins nothing: a module
    name, a path with no suffix, an absolute path in either spelling, and a `..`
    escape in either spelling.

    Both flavours are read because the spelling is not the host's to decide.
    `pathlib.Path` parses with the running platform's rules, so a Windows path in
    a manifest is a relative path to a POSIX host, and a POSIX path is not
    absolute to Windows at all. Testing only the host's reading is what let a
    POSIX run pin `C:/elsewhere/verify_price.py` as a file of the repository, and
    `repoint_approved` then joined it onto `approved_root` and ran whatever it
    named.
    """
    if not PureWindowsPath(part).suffix and not PurePosixPath(part).suffix:
        return False
    for flavour in (PureWindowsPath, PurePosixPath):
        candidate = flavour(part)
        if candidate.is_absolute() or candidate.anchor or candidate.drive:
            return False
        if ".." in candidate.parts:
            return False
    return True


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

    This runs only when every check the policy REQUIRES is pinnable, so a
    required check never reaches this branch unrepointed. A check the policy does
    not require, but the approved manifest happens to define, can reach it. That
    is why the branch is here rather than an assertion: the loop covers the whole
    manifest while the refusal covers only the required ids.
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

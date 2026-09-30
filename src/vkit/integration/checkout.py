"""A candidate gets its own checkout, and only this module makes one.

The integration decision is only about the merged candidate if the code that was
executed is byte-for-byte that commit and nothing else wrote to it while the
checks ran. Both halves of that are this module's job.

**Why a Git worktree rather than a copy.** `git worktree add --detach <sha>`
materializes a commit exactly, including its submodule links and file modes,
and Git records it as a checkout of this repository. A copy can drift from the
commit (line endings, executability, a filter smudge) and would leave the
evidence describing a tree that no commit ever had. The worktree lives under the
Git common directory rather than under `tmp`, because `git clean` and
`git worktree prune` reach the common directory and nothing reaches `tmp`.

**Why the checkout is disposable and not the worker's.** The worker's own
checkout is where a developer edits, so it is dirty in the ordinary way, and it
can hold anything. A decision made there would be a decision about a tree whose
relationship to any commit is unknown. So the worker keeps its checkout and this
creates a separate one, takes the writer lock on it, and the checks run there.

**What "exclusive ownership" means concretely.** Two things, both mechanical. An
exclusive claim in the app's own claim table, so a second integration
verification for the same repository cannot be admitted while this one runs.
And a filesystem check that the tree is clean before and after, so a process
that bypassed the app -- a stray editor, another agent, a build step with a
side effect -- is caught rather than absorbed into the evidence.
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..paths import Project
from . import gitidentity as gits
from .gitidentity import GitError

CHECKOUTS_DIRNAME = "checkouts"

# Where approved verification code is materialized for a run. It is a sibling of
# the checkouts rather than a directory inside one, because a checkout is
# asserted clean against its commit and a tree holding verifier-owned bytes would
# be dirty by construction.
ORACLE_DIRNAME = "approved"


class CheckoutError(Exception):
    """A candidate checkout could not be created, verified, or retired."""


@dataclass(frozen=True)
class CandidateCheckout:
    """One checkout of one commit, owned exclusively for the length of a run."""

    project: Project
    path: Path
    commit: str
    integration_id: str

    @property
    def name(self) -> str:
        return self.path.name


def checkouts_root(project: Project) -> Path:
    """Where candidate checkouts live for this repository.

    Under the Git common directory, next to the run evidence, so that retiring a
    worker checkout cannot reach them and so that `git worktree list` can see
    them. The directory is created by the caller that needs it; this only names
    it.
    """
    return project.state_root / "integration" / CHECKOUTS_DIRNAME


def materialize_approved(
    project: Project, revision: str, scripts: dict[str, str], run_dir: Path
) -> Path:
    """Write the approved revision's verification code into a verifier-owned tree.

    The approved check names a script by repository-relative path, and the
    candidate checkout holds the candidate's copy of that path. Executing the
    name resolves to the candidate, so this writes the approved bytes to
    `<run_dir>/approved/<script>` and returns that directory to run them from,
    while the working directory stays in the candidate checkout. The verification
    code and the expectations are therefore the approved ones, and the product
    under test is still the candidate's.

    A path the approved revision does not ship is a refusal, not a silent
    fallback to the candidate's copy: a check whose approved script is missing
    has no approved oracle to run, and running the candidate's would be exactly
    the substitution this exists to prevent.

    Nothing is returned but the directory, because the digests of what was written
    are already measured from the commit in `oracle.approved_oracle`. Measuring
    them twice would be a second authority for one fact, and one that reads the
    tree rather than the revision the policy pinned.
    """
    root = run_dir / ORACLE_DIRNAME
    missing: list[str] = []
    for script in sorted(set(scripts.values())):
        try:
            blob = gits.git_blob(project, revision, script)
        except GitError:
            missing.append(script)
            continue
        target = root / script
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    if missing:
        raise CheckoutError(
            f"the approved revision {revision[:12]} does not ship {', '.join(missing)}, "
            f"so the check that runs it has no approved verification code to execute"
        )
    return root


def create(project: Project, commit: str, integration_id: str) -> CandidateCheckout:
    """Materialize one commit in a fresh, detached, exclusively owned checkout."""
    root = checkouts_root(project)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{integration_id}-{uuid.uuid4().hex[:12]}"
    try:
        gits.git(project, "worktree", "add", "--detach", "--force", str(path), commit)
    except GitError as exc:
        raise CheckoutError(
            f"could not check out {commit[:12]} at {path}: {exc}"
        ) from exc
    return CandidateCheckout(project=project, path=path, commit=commit,
                             integration_id=integration_id)


def project_for(checkout: CandidateCheckout) -> Project:
    """The checkout seen as a project, sharing the repository's state directory.

    `open_project` is not used because it resolves through the checkout's own
    `.git` file, and a linked worktree's `git-common-dir` does point at the
    parent repository's common directory, so the two agree. Going through the
    public resolver anyway would mean the identity of the state directory is a
    property of Git's plumbing rather than a fact this module states, so the
    check below confirms it instead of assuming it.
    """
    from ..paths import open_project

    resolved = open_project(checkout.path)
    if resolved.git_common_dir != checkout.project.git_common_dir:
        raise CheckoutError(
            f"checkout {checkout.path} resolves to a different repository's state "
            f"directory ({resolved.git_common_dir}); refusing to write evidence there"
        )
    return resolved


def assert_clean(checkout: CandidateCheckout, *, when: str) -> None:
    """Refuse unless the checkout is an untouched materialization of its commit.

    Both the porcelain status and the recorded HEAD are checked, because they
    fail differently: a status is about the worktree against the index, and HEAD
    is about which commit the index was built from. A checkout whose HEAD is not
    the candidate is not that candidate no matter how clean it looks.
    """
    head = gits.git(checkout.project, "-C", str(checkout.path), "rev-parse", "HEAD")
    if head != checkout.commit:
        raise CheckoutError(
            f"{when}, the candidate checkout is at {head[:12]}, not the candidate "
            f"{checkout.commit[:12]}"
        )
    status = gits.git(
        checkout.project, "-C", str(checkout.path),
        "status", "--porcelain", "-z", "--untracked-files=all",
    )
    if status:
        paths = sorted(p for p in status.split("\x00") if p)
        raise CheckoutError(
            f"{when}, the candidate checkout is not clean: "
            + ", ".join(paths[:8])
            + (" ..." if len(paths) > 8 else "")
        )


def retire(checkout: CandidateCheckout) -> list[str]:
    """Remove the checkout, reporting anything the checkout still holds.

    Never removes a tree that has uncommitted work in it. `git worktree remove`
    already refuses that, and this checks first so the refusal is this module's
    message with the preserved paths in it rather than git's, and so the caller
    can archive what was preserved instead of discovering later that it is gone.
    Returns the preserved paths, empty when the checkout was clean.
    """
    preserved = dirty_paths(checkout)
    if preserved:
        raise CheckoutError(
            f"refusing to retire {checkout.path}: it holds uncommitted work in "
            + ", ".join(preserved[:8])
            + (" ..." if len(preserved) > 8 else "")
            + ". Archive it, or commit the work, and retry."
        )
    try:
        gits.git(checkout.project, "worktree", "remove", "--force", str(checkout.path))
    except GitError:
        # An already-removed checkout, or a worktree whose administrative file
        # is gone. The directory is still ours to clean, and prune clears the
        # administrative record either way.
        shutil.rmtree(checkout.path, ignore_errors=True)
        gits.git(checkout.project, "worktree", "prune")
    return preserved


def dirty_paths(checkout: CandidateCheckout) -> list[str]:
    """The uncommitted work a checkout holds, as a list a caller can archive."""
    status = gits.git(
        checkout.project, "-C", str(checkout.path),
        "status", "--porcelain", "-z", "--untracked-files=all",
    )
    fields = status.split("\x00")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        if record[:2] in ("R", "C"):
            index += 1
        paths.append(record[3:])
    return paths

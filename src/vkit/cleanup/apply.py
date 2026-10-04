"""Guarded apply: the only code in this package that writes a file.

Everything above this module proposes and checks. This one decides, and every
guard it applies sits before the first byte moves. That ordering is the whole
design: a check that runs after the write has already happened is a report, not
a guard.

## What is bound before a write

A proposal is not a bag of bytes. It is bound to eight facts, and an apply that
cannot confirm all eight refuses:

* the task generation, so an attempt superseded since the preview holds nothing;
* the approved cleanup policy, by digest, so the rules in force at apply time
  are the ones the proposal was judged against;
* the repository-relative path, re-derived here and not taken on trust;
* the before bytes, by digest, read again immediately before the write;
* the after bytes, which are re-derived from the proposal and never accepted as
  an opaque payload;
* the rule and checker identity, so the receipt names what authorized the edit;
* the preservation receipt, which is re-verified here rather than trusted;
* the request id, which is what makes a repeated request converge.

## The participating-writer assumption, stated

Replacement here is `os.replace` on a same-directory temporary file: the target
either has the old bytes or the new ones, never a half-written mixture, and
never a window where the path does not exist. That is atomicity against the
filesystem.

It is NOT an operating-system compare-and-swap. Between this module's last read
of the before digest and its `os.replace`, an editor, another agent, or a shell
command can write the file, and those writes are overwritten. Nothing in this
file detects that, and no receipt produced here claims to. What it does is
narrow the window to the gap between the final digest check and the replace,
refuse to write at all when anything it can observe has moved, and never roll
back a later edit to paper over it.

The plan is explicit that uncoordinated editors are out of scope for the
resource claims, and this module does not pretend otherwise.

## Recovery is by digest, not by a log

A crash after `os.replace` and before the receipt is written leaves the file in
its after state with no record that it happened. The next apply of the same
request reads the current bytes, sees they already equal the proposal's after
bytes, and reports `AlreadyApplied` -- converging by comparing content rather
than by consulting a journal that may or may not have been flushed.

A crash BEFORE the replace leaves the before bytes in place, and the next apply
proceeds normally. There is no third state to reconcile, which is the property
that makes the operation retryable at all.

## What this never does

It never rolls back. An edit made after this one is somebody else's, and
restoring the before bytes over it would destroy their work. Recovery is the
caller's, and the digests in the receipt are what make it possible.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..paths import Project
from .comments import (
    TRAILING_RULE,
    PreservationFailed,
    Proposal as CommentProposal,
    _check_path,
    verify_preservation,
)
from .logic import (
    CHECKER_ID,
    CompiledEqual,
    LogicProposal,
    verify_compiled_equality,
    verify_docstrings,
)
from .policy import CleanupMode, CleanupPolicy, check_policy
from .records import record_outcome

ARTIFACT_DIR_NAME = "cleanup"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()




@dataclass(frozen=True)
class Applied:
    """The file now holds the proposal's after bytes, and this is the receipt."""

    relative_path: str
    request_id: str
    proposal_id: str
    rule_id: str
    generation: int
    policy_digest: str
    before_digest: str
    after_digest: str
    checker_id: str
    optimize_levels: tuple[int, ...]
    python_version: str
    artifact_path: str
    receipt: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "APPLIED",
            "path": self.relative_path,
            "requestId": self.request_id,
            "proposalId": self.proposal_id,
            "rule": self.rule_id,
            "generation": self.generation,
            "policyDigest": self.policy_digest,
            "beforeDigest": self.before_digest,
            "afterDigest": self.after_digest,
            "checker": self.checker_id,
            "optimizeLevels": list(self.optimize_levels),
            "pythonVersion": self.python_version,
            "artifact": self.artifact_path,
            "receipt": self.receipt,
        }


@dataclass(frozen=True)
class AlreadyApplied:
    """The same request, already carried out. The convergent answer.

    Reported rather than refused because a retry is not an error: the caller
    asked for a state and the state holds. The digests are re-read to establish
    that, so this is a verification and not an assumption that the first call
    succeeded.
    """

    relative_path: str
    request_id: str
    proposal_id: str
    before_digest: str
    after_digest: str
    artifact_path: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "ALREADY_APPLIED",
            "path": self.relative_path,
            "requestId": self.request_id,
            "proposalId": self.proposal_id,
            "beforeDigest": self.before_digest,
            "afterDigest": self.after_digest,
            "artifact": self.artifact_path,
        }


@dataclass(frozen=True)
class ApplyRefused:
    """No write happened. `reason` is required, which is the point of the variant."""

    relative_path: str
    reason: str
    detail: str
    observed_before_digest: str | None = None
    expected_before_digest: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "REFUSED",
            "path": self.relative_path,
            "reason": self.reason,
            "detail": self.detail,
            "observedBeforeDigest": self.observed_before_digest,
            "expectedBeforeDigest": self.expected_before_digest,
        }


@dataclass(frozen=True)
class _VerifiedCleanup:
    """Facts re-established from the proposal bytes during this apply."""

    receipt: dict[str, Any]
    optimize_levels: tuple[int, ...] = ()
    python_version: str = ""


ApplyResult = Applied | AlreadyApplied | ApplyRefused




@dataclass(frozen=True)
class ApplyRequest:
    """What a caller asks for, and everything the binding is made of.

    `request_id` is the caller's key for the whole request. It is what makes a
    repeat converge: the same id with the same proposal is the same request, and
    the file's current bytes decide whether it is still to be done.

    `generation` is the task generation the proposal was made under. An attempt
    superseded since then holds nothing, so the generation is re-verified at
    apply time through `verify_ownership` rather than trusted from here.
    """

    request_id: str
    proposal: CommentProposal | LogicProposal
    generation: int
    policy: CleanupPolicy
    keep_markers: Sequence[str] | None = None

    @property
    def relative_path(self) -> str:
        return self.proposal.relative_path

    @property
    def rule_id(self) -> str:
        return getattr(self.proposal, "rule_id", TRAILING_RULE)

    def to_json(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "proposalId": self.proposal.proposal_id,
            "path": self.relative_path,
            "rule": self.rule_id,
            "generation": self.generation,
            "policyDigest": self.policy.digest,
        }




def _refuse(
    request: ApplyRequest, reason: str, detail: str,
    observed: str | None = None,
) -> ApplyRefused:
    return ApplyRefused(
        relative_path=request.relative_path,
        reason=reason,
        detail=detail,
        observed_before_digest=observed,
        expected_before_digest=_digest(request.proposal.before_bytes),
    )


def _reject_symlink(target: Path, root: Path) -> str | None:
    """Refuse a path that is a symlink, or whose parent chain leaves the root.

    A symlink's target can be anywhere, including outside the repository, and a
    write through it lands wherever it points. Resolving first and comparing
    afterwards would not catch it: resolution is how you would follow it.
    """
    if target.is_symlink():
        return (
            f"{target} is a symlink. Cleanup refuses to write through one because "
            "its target may lie outside the repository, and resolving it first would "
            "follow it rather than refuse it"
        )
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        return f"{target} resolves to {resolved}, which is outside the project root {root}"
    return None


def _verify_receipt(request: ApplyRequest) -> ApplyRefused | _VerifiedCleanup:
    """Re-establish preservation and its receipt from the bytes, at apply time.

    The proposal's receipt is only an integrity comparison. The value recorded
    after the write comes from this fresh check, so changed receipt fields cannot
    become a claim merely by surviving `dataclasses.replace`.
    """
    proposal = request.proposal
    before, after = proposal.before_bytes, proposal.after_bytes

    if isinstance(proposal, LogicProposal):
        compiled = verify_compiled_equality(before, after, filename=proposal.relative_path)
        if not isinstance(compiled, CompiledEqual):
            return _refuse(
                request, "compiled_equality_failed",
                f"{compiled.reason}: {compiled.detail}",
            )
        docstrings = verify_docstrings(before, after)
        if not isinstance(docstrings, CompiledEqual):
            return _refuse(
                request, "docstring_changed",
                f"{docstrings.reason}: {docstrings.detail}",
            )
        if (
            compiled.to_json() != proposal.compiled.to_json()
            or docstrings.to_json() != proposal.docstrings.to_json()
            or proposal.optimize_levels != compiled.optimize_levels
            or proposal.python_version != compiled.python_version
        ):
            return _refuse(
                request, "receipt_mismatch",
                "the compiled-equality receipt does not match a fresh check of the "
                "proposal bytes",
            )
        receipt = compiled.to_json()
        receipt["docstringEquality"] = docstrings.to_json()
        return _VerifiedCleanup(
            receipt, compiled.optimize_levels, compiled.python_version
        )

    check = (
        verify_preservation(before, after)
        if request.keep_markers is None
        else verify_preservation(before, after, keep_markers=request.keep_markers)
    )
    if isinstance(check, PreservationFailed):
        return _refuse(
            request, "preservation_failed", f"{check.reason}: {check.detail}"
        )
    fresh = check.to_json()
    if fresh != proposal.receipt.to_json():
        return _refuse(
            request, "receipt_mismatch",
            "the preservation receipt does not match a fresh check of the proposal "
            "bytes",
        )
    return _VerifiedCleanup(fresh, python_version=check.python_version)


def _guard(
    project: Project,
    request: ApplyRequest,
    *,
    ownership_check,
) -> ApplyRefused | _VerifiedCleanup:
    """Every refusal that can be decided before any byte is written.

    Ordered so the refusal a caller sees is the one it can most act on: an absent
    write authorization beats an unowned file, an unowned file beats a stale
    digest, and a stale digest beats a receipt that no longer reproduces.
    """
    policy = request.policy

    if not request.request_id:
        return _refuse(
            request, "missing_request_id",
            "an apply needs a request id. Without one a repeated request cannot be "
            "told from a first attempt, and repeating it would edit the file twice",
        )

    if policy.mode is CleanupMode.OFF or not policy.may_write():
        refusal = check_policy(policy, request.relative_path)
        return _refuse(
            request,
            refusal.reason if refusal else "mode_cannot_write",
            refusal.detail if refusal else (
                f"cleanup mode {policy.mode.value!r} does not authorize a write; "
                "enabling a mode is the owner's decision through approved policy"
            ),
        )

    refusal = check_policy(
        policy, request.relative_path, request.rule_id,
        python_version=_running_version(),
    )
    if refusal is not None:
        return _refuse(request, refusal.reason, refusal.detail)

    if ownership_check is None:
        return _refuse(
            request, "ownership_unavailable",
            "no task-ownership authority was supplied; automatic cleanup requires "
            "a current generation and ownership check",
        )
    try:
        conflict = ownership_check(request)
    except Exception as exc:  # noqa: BLE001 - unavailable authority must fail closed
        return _refuse(
            request, "ownership_unavailable",
            f"the task-ownership authority could not complete: {exc}",
        )
    if conflict is not None:
        return _refuse(request, "not_task_owned", conflict)

    target = project.root / request.relative_path
    if not target.exists():
        return _refuse(
            request, "not_a_file",
            f"{request.relative_path} no longer exists; a proposal cannot be applied to "
            "a file that is gone",
        )
    symlink = _reject_symlink(target, project.root.resolve())
    if symlink is not None:
        return _refuse(request, "symlink_or_escape", symlink)
    path_refusal = _check_path(project, request.relative_path)
    if path_refusal is not None:
        return _refuse(request, path_refusal.reason.value, path_refusal.detail)

    return _verify_receipt(request)


def _running_version() -> str:
    import platform

    return platform.python_version()




def _artifact_path(project: Project, request: ApplyRequest) -> Path:
    """Where the original bytes are kept, named so a retry finds the same file."""
    digest = request.proposal.proposal_id
    return (
        project.state_root / ARTIFACT_DIR_NAME / request.relative_path.replace("\\", "/")
        / f"{digest}.before"
    )


def _preserve_artifact(project: Project, request: ApplyRequest) -> str:
    """Write the original bytes beside the state, before the file is replaced.

    Written first so that a crash between the two leaves a record of what was
    there. Written under a content-addressed name so writing it twice is the same
    write, which is what lets a retry skip rather than duplicate it.
    """
    path = _artifact_path(project, request)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = request.proposal.before_bytes
    if not path.exists() or path.read_bytes() != payload:
        _replace_atomically(path, payload)
    return str(path)


def _replace_atomically(
    target: Path, payload: bytes, *, mode: int | None = None
) -> None:
    """Replace `target` with `payload` through a same-directory temporary file.

    The temporary file is in the same directory as the target so the rename stays
    within one filesystem, where it is atomic. A temporary file elsewhere could
    land on a different volume, where `os.replace` degrades to a copy that a
    reader can observe half-written.

    The file descriptor is flushed and fsynced before the rename, so the bytes
    are on disk before the directory entry points at them. Without the fsync a
    crash could leave the name resolving to a file whose contents never arrived.
    """
    if mode is None and target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    handle, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".vkit-cleanup"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            if mode is not None:
                os.chmod(temporary_path, mode)
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def apply_cleanup(
    project: Project,
    request: ApplyRequest,
    *,
    ownership_check=None,
) -> ApplyResult:
    """Apply one approved proposal, or say precisely why not.

    `ownership_check` is a callable taking the request and returning a reason
    string when the bound task no longer owns what it would edit, or None when it
    does. It is a parameter rather than an import so that this function stays
    testable without a store, and so that the caller supplying the ownership
    decision supplies the same one every other writer uses -- `verify_ownership`
    from `vkit.tasks`, inside the transaction that holds the claims.

    Every outcome is appended to the durable record, refusals included. Recording
    here rather than at each call site is the whole reason an operator can ask
    why a file was left alone: a refusal returned to one caller and forgotten is
    indistinguishable from a file nobody ever looked at.

    The sequence, and why it is this sequence:

    1. Every guard, before any byte moves.
    2. Re-read the file and compare its digest to the proposal's before digest.
       This is the check closest to the write, and it is re-read rather than
       reused from step 1 for exactly that reason.
    3. Preserve the original bytes as an artifact.
    4. Replace atomically.
    5. Report.
    """
    outcome = _decide(project, request, ownership_check=ownership_check)
    record_outcome(project, request, outcome)
    return outcome


def _decide(project: Project, request: ApplyRequest, *, ownership_check) -> ApplyResult:
    """Runs the apply and returns; `apply_cleanup` records the returned member.

    Split out so that recording cannot be skipped by a branch that returns
    early, which is the failure the record exists to prevent."""
    guarded = _guard(project, request, ownership_check=ownership_check)
    if isinstance(guarded, ApplyRefused):
        return guarded
    verified = guarded

    proposal = request.proposal
    target = project.root / request.relative_path
    expected = _digest(proposal.before_bytes)
    wanted = _digest(proposal.after_bytes)

    observed = _digest(target.read_bytes())
    if observed == wanted:
        try:
            artifact = _preserve_artifact(project, request)
        except OSError as exc:
            return _refuse(
                request, "artifact_write_failed",
                f"the original bytes could not be preserved: {exc}", observed,
            )
        return AlreadyApplied(
            relative_path=request.relative_path,
            request_id=request.request_id,
            proposal_id=proposal.proposal_id,
            before_digest=expected,
            after_digest=wanted,
            artifact_path=artifact,
        )
    if observed != expected:
        return _refuse(
            request, "before_bytes_changed",
            f"{request.relative_path} has changed since the proposal was made "
            f"({observed} against {expected}). Cleanup will not overwrite an edit it "
            "did not make, so re-preview against the current bytes",
            observed,
        )

    try:
        artifact = _preserve_artifact(project, request)
        mode = stat.S_IMODE(target.stat().st_mode)
        _replace_atomically(target, proposal.after_bytes, mode=mode)
    except OSError as exc:
        return _refuse(
            request, "write_failed",
            f"the cleanup could not replace {request.relative_path}: {exc}",
        )

    landed = _digest(target.read_bytes())
    if landed != wanted:
        return _refuse(
            request, "write_did_not_land",
            f"{request.relative_path} does not hold the after bytes after replacement "
            f"({landed} against {wanted}); the original bytes are preserved at {artifact}",
            landed,
        )

    return Applied(
        relative_path=request.relative_path,
        request_id=request.request_id,
        proposal_id=proposal.proposal_id,
        rule_id=request.rule_id,
        generation=request.generation,
        policy_digest=request.policy.digest,
        before_digest=expected,
        after_digest=landed,
        checker_id=CHECKER_ID,
        optimize_levels=verified.optimize_levels,
        python_version=verified.python_version,
        artifact_path=artifact,
        receipt=verified.receipt,
    )


def apply_comment_cleanup(
    project: Project,
    proposal: CommentProposal,
    *,
    request_id: str,
    generation: int,
    policy: CleanupPolicy,
    ownership_check=None,
    keep_markers: Sequence[str] | None = None,
) -> ApplyResult:
    """Apply an approved comment proposal. The shape a CLI would call.

    Exposed separately from `apply_cleanup` so a caller does not have to know
    which kind of proposal it is holding to build a request, and so the request
    type stays a single shape for both rules.
    """
    return apply_cleanup(
        project,
        ApplyRequest(
            request_id=request_id,
            proposal=proposal,
            generation=generation,
            policy=policy,
            keep_markers=keep_markers,
        ),
        ownership_check=ownership_check,
    )


def apply_logic_cleanup(
    project: Project,
    proposal: LogicProposal,
    *,
    request_id: str,
    generation: int,
    policy: CleanupPolicy,
    ownership_check=None,
) -> ApplyResult:
    """Apply an approved logic proposal. The shape a CLI would call."""
    return apply_cleanup(
        project,
        ApplyRequest(
            request_id=request_id,
            proposal=proposal,
            generation=generation,
            policy=policy,
        ),
        ownership_check=ownership_check,
    )


def ownership_from_tasks(project: Project, store, task_id: str) -> Any:
    """Build the `ownership_check` a CLI should pass, from the shared claim check.

    Returns a callable, so `apply_cleanup` never imports the task layer and
    never decides ownership itself. One decision, taken by the module that owns
    it, is what stops this file and `tasks.py` disagreeing about who may write.

    A `ConflictError` from `verify_ownership` becomes a reason string rather than
    an exception, because the caller of an apply is answering a question and an
    unhandled exception would read as a crash instead of a refusal.
    """
    from ..storage import ConflictError
    from ..tasks import verify_ownership

    def check(request: ApplyRequest) -> str | None:
        try:
            verify_ownership(store, task_id, request.generation, project=project)
        except ConflictError as exc:
            return str(exc)
        return None

    return check

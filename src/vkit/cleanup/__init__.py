"""Plan 11 checkpoint 1: cleanup of Python comments, preview only.

Re-exports the cleanup surface. Nothing in this package writes to disk: a preview
proposes, a checker decides, and the caller decides whether to act.
"""
from .comments import (
    CleanupPreview,
    PreservationCheck,
    PreservationFailed,
    PreservationPassed,
    Proposal,
    Refusal,
    RefusalReason,
    RemovedComment,
    preview_comment_cleanup,
    verify_preservation,
)

__all__ = [
    "CleanupPreview",
    "PreservationCheck",
    "PreservationFailed",
    "PreservationPassed",
    "Proposal",
    "Refusal",
    "RefusalReason",
    "RemovedComment",
    "preview_comment_cleanup",
    "verify_preservation",
]

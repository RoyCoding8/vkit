"""What each cleanup did, kept where a dashboard and a later process can read it.

A cleanup apply returns a receipt to the caller that asked for it. That is the
right shape for a function and the wrong shape for an operator: once the process
exits, the only thing on disk is the original bytes, so a dashboard cannot list
what was cleaned, and a refusal leaves nothing at all to explain why a file was
left alone. Checkpoint 12.2 named those four panels as absent, which was the
honest answer with this backend and is the wrong answer now that it exists.

## What is a record

One row per apply outcome. `Applied`, `AlreadyApplied` and `ApplyRefused` are
already a closed sum in `apply.py`, and a record is that sum projected onto
durable storage: one row shape with an `outcome` the reader branches on once,
rather than three record types that differ only in which fields they leave null.

The outcome is the load-bearing field and it is CHECK-enforced in the table. A
refusal records the checker's own `reason` and `detail` verbatim, because the
operator's question is "why was this file left alone" and a row that says
`refused` without the cause answers nothing.

## What a record does not hold

No file contents. The columns are digests, rule ids, line locators, the
checker's preservation receipt, and a path to the preserved original. Checkpoint
11.2 already writes the before bytes as an artifact beside this database, so a
copy inside the row would be a second copy of a thing that already has one, in
a table nobody reads. The receipt is the checker's own structured document and
is stored verbatim rather than reshaped into columns, because the compiled
equality receipt and the comment receipt disagree on nearly every field and
neither owns a fixed schema.

## The bound

`MAX_RECORDS` rows, trimmed in the same transaction as the insert. The failure
this prevents is a state directory that fills a disk at 3am: cleanup runs on
every hook, so a hook loop produces records at host speed with no operator
present. Five hundred rows is roughly a working day of a busy session, and the
newest surviving are the ones an operator is asking about.

An absent record reads as an empty list. Nothing here synthesises a placeholder,
because a placeholder row is indistinguishable from a real outcome and that is
the exact failure the named-absent panels were written to avoid.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..paths import Project

#: The most outcomes kept. A round number rather than a measured one, because the
#: question it answers is "how far back can an operator look", not "how many does
#: a busy day produce": the newest rows are the ones being asked about, and a
#: larger bound buys rows nobody reads at the cost of a table that grows.
MAX_RECORDS = 500

#: The rows one console page asks for. Smaller than `MAX_RECORDS` on purpose, so
#: a busy project does not serialize five hundred records into a browser to fill
#: a panel. The total comes back beside them, so a truncated page says so.
DEFAULT_LIMIT = 50

#: The outcomes, named once so the writer and the reader cannot disagree about the
#: spelling. The table CHECKs the same three values in migration 7; that CHECK is
#: the authority and is exercised against a fourth value, so this tuple is not a
#: second place that has to agree with it.
APPLIED = "applied"
ALREADY_APPLIED = "already_applied"
REFUSED = "refused"


@dataclass(frozen=True)
class CleanupRecord:
    """One apply outcome, as a reader receives it.

    `before_digest` and `after_digest` are `str | None` because a refusal has no
    after: a file that was not written has no resulting content to name, and
    filling in the before digest there would claim a write that did not happen.

    On a refusal `before_digest` is what the file held as far as the checker could
    observe, and it is NOT the same as the proposal's expected digest when the
    refusal was a changed file. `detail` is what distinguishes them, which is why a
    refusal panel shows the detail and never the digest alone.

    `receipt` is empty for a refusal and for an `AlreadyApplied`, because neither
    earned one. An `AlreadyApplied` is a convergent retry that found the after
    bytes already in place, and the receipt that justified the first write is on
    the row that recorded it.
    """

    relative_path: str
    outcome: str
    rule_id: str
    request_id: str
    proposal_id: str
    generation: int
    policy_digest: str
    before_digest: str | None = None
    after_digest: str | None = None
    reason: str | None = None
    detail: str | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    sites: list[dict[str, Any]] = field(default_factory=list)
    artifact_path: str | None = None
    recorded_at: str = ""

    def to_json(self) -> dict[str, Any]:
        """The shape the console serializes.

        Camel-cased to match the rest of the API, and flat: a panel renders a row,
        and a row a reader cannot address by key is a row nobody can filter.
        """
        return {
            "path": self.relative_path,
            "outcome": self.outcome,
            "rule": self.rule_id,
            "requestId": self.request_id,
            "proposalId": self.proposal_id,
            "generation": self.generation,
            "policyDigest": self.policy_digest,
            "beforeDigest": self.before_digest,
            "afterDigest": self.after_digest,
            "reason": self.reason,
            "detail": self.detail,
            "receipt": self.receipt,
            "sites": self.sites,
            "artifact": self.artifact_path,
            "recordedAt": self.recorded_at,
        }


@dataclass(frozen=True)
class RecordView:
    """The bounded listing, and what it left out.

    `total` and `truncated` travel together with the rows. A bounded read that
    quietly returns fewer leaves the reader unable to tell "that is everything"
    from "there is more", which is the silent-gap failure the console exists to
    avoid.
    """

    records: tuple[CleanupRecord, ...]
    total: int
    limit: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.records)

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index):
        return self.records[index]

    def to_json(self) -> dict[str, Any]:
        return {
            "records": [record.to_json() for record in self.records],
            "total": self.total,
            "limit": self.limit,
            "truncated": self.truncated,
        }


def _store(project: Project):
    from ..storage import Store

    return Store(project.db_path)


def record_outcome(project: Project, request, result) -> None:
    """Append one apply outcome to the durable record.

    Called by `apply_cleanup` on every path, so a refusal is recorded as
    reliably as an application. `request` is the `ApplyRequest` the outcome came
    from, because the refusals that matter most carry facts the result alone
    does not: the generation and the policy digest are on the request, and a
    refusal recorded without them could not say what was attempted.

    A store that cannot be opened must not fail the cleanup. The bytes are
    already written (or deliberately not written) by the time this runs, and a
    cleanup that succeeded must not report a failure because its receipt could
    not be filed. The absence is visible instead: `cleanup_records` returns
    nothing for it, and a dashboard showing no record for a file that did change
    is a worse but honest state than a raised exception after a committed write.
    """
    from ..storage import StoreError

    try:
        store = _store(project)
        store.record_cleanup(_row(request, result), keep=MAX_RECORDS)
    except (StoreError, OSError):
        pass


def _sites(proposal) -> list[dict[str, Any]]:
    """Where the edit landed, addressed by line. Never the text it removed.

    A comment's own text is file content, and it is already preserved with the
    before bytes as an artifact. The line number is what a reader needs to go
    and look, and it is the only part of a site that is not the file.
    """
    removed = getattr(proposal, "removed", None)
    if removed is not None:
        return [
            {"line": item.line, "byteStart": item.byte_start, "byteEnd": item.byte_end}
            for item in removed
        ]
    sites = getattr(proposal, "sites", None)
    if sites is None:
        return []
    return [
        {
            "line": site.line,
            "startLine": site.start_line,
            "endLine": site.end_line,
            "description": site.description,
        }
        for site in sites
    ]


def _row(request, result) -> dict[str, Any]:
    """The durable projection of one outcome.

    Branches on the `ApplyResult` sum exactly once. The refusals and the
    applications genuinely differ in what they can state, and flattening them
    into one row with nullable everything is what puts a `NULL` in an operator's
    panel where a reason should be.
    """
    from .apply import AlreadyApplied, Applied, ApplyRefused

    common = {
        "relative_path": request.relative_path,
        "rule_id": request.rule_id,
        "request_id": request.request_id,
        "proposal_id": request.proposal.proposal_id,
        "generation": request.generation,
        "policy_digest": request.policy.digest,
        "sites": _sites(request.proposal),
    }

    if isinstance(result, Applied):
        return {
            **common,
            "outcome": APPLIED,
            "before_digest": result.before_digest,
            "after_digest": result.after_digest,
            "reason": None,
            "detail": None,
            "receipt": result.receipt,
            "artifact_path": result.artifact_path,
        }

    if isinstance(result, AlreadyApplied):
        # Digests, no receipt. The receipt belongs to the row that recorded the
        # write; re-deriving it here would duplicate the same document on every
        # retry of a request a host fired twice.
        return {
            **common,
            "outcome": ALREADY_APPLIED,
            "before_digest": result.before_digest,
            "after_digest": result.after_digest,
            "reason": None,
            "detail": None,
            "receipt": {},
            "artifact_path": result.artifact_path,
        }

    if isinstance(result, ApplyRefused):
        # `observed` is None for a refusal decided before any bytes were read, so
        # the fallback is the proposal's expected digest. `detail` says which.
        observed = result.observed_before_digest or result.expected_before_digest
        return {
            **common,
            "outcome": REFUSED,
            "before_digest": observed,
            "after_digest": None,
            "reason": result.reason,
            "detail": result.detail,
            "receipt": {},
            "artifact_path": None,
        }

    raise TypeError(f"an apply result is one of Applied/AlreadyApplied/ApplyRefused, "
                    f"not {type(result).__name__}")


def _record(raw: dict[str, Any]) -> CleanupRecord:
    return CleanupRecord(**raw)


def cleanup_records(
    project: Project, *, limit: int = DEFAULT_LIMIT,
) -> RecordView:
    """The recorded outcomes for this repository, newest first.

    Bounded on read the way `runs_view` is, because the alternative is a console
    that opens a project with a long cleanup history and serializes all of it
    into a browser. The bound is clamped rather than trusted: a caller passing
    zero or a huge number gets the default or the ceiling, never an unbounded
    query.
    """
    ceiling = min(int(limit), MAX_RECORDS) if int(limit) > 0 else DEFAULT_LIMIT
    rows, total = _store(project).list_cleanup(limit=ceiling)
    return RecordView(
        records=tuple(_record(row) for row in rows), total=total, limit=ceiling,
    )


def cleanup_summary(project: Project, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """What an operator's first question needs: what was applied, what was refused.

    Split rather than filtered client-side, because the two lists have different
    shapes. A refusal's row is a reason and a digest that were never written; an
    application's is a receipt and an artifact that can be restored from.

    `applied_total` counts the applications IN THIS LISTING, not in the table. A
    page that truncated and then reported the table's whole size under a key
    reading "applied" would claim more applications were shown than the reader
    can see. `total` is the table's size and `truncated` says whether the listing
    holds all of it, so the reader can always tell which of the two they have.

    `already_applied_total` counts the convergent retries. They belong to neither
    list because an `AlreadyApplied` is not a second application of anything: it
    is the same request arriving twice, and an operator reading two applied rows
    for one edit would reasonably ask what cleaned it twice.
    """
    view = cleanup_records(project, limit=limit)
    return {
        "applied": [
            record.to_json() for record in view.records if record.outcome == APPLIED
        ],
        "refusals": [
            record.to_json() for record in view.records if record.outcome == REFUSED
        ],
        "applied_total": sum(
            1 for record in view.records if record.outcome == APPLIED
        ),
        "refusal_total": sum(
            1 for record in view.records if record.outcome == REFUSED
        ),
        "already_applied_total": sum(
            1 for record in view.records if record.outcome == ALREADY_APPLIED
        ),
        "total": view.total,
        "truncated": view.truncated,
        "limit": view.limit,
    }
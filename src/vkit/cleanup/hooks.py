"""Plan 11 checkpoint 3: the two paths that turn cleanup into a gate.

Checkpoints 1 and 2 built a tool a person runs. This module is what makes it a
gate, and it has two paths because Claude Code's hook reference says a
`PostToolUse` hook matching `Edit|Write` does not run when a `Bash` command or a
process outside Claude Code rewrites the same file. One path cannot do the job,
and a hook that claimed to would leave a shell edit invisible until long after
evidence was collected.

## The division

`cleanup_paths` is the authority. It runs before a run captures its source
identity, reads the changed set out of the checkout rather than taking one from
a host event, and returns a verdict the caller turns into a blocked check. It
does not know or care how a change arrived, which is what makes it the path a
shell command lands on: `git status` and `git diff` describe a rewritten file
exactly as they describe an edited one.

`accelerate` is the fast one the host calls after an editing tool. It handles
the single file the payload names, which is what makes it fast, and it is
explicitly an accelerator rather than the authority. A file it never saw still
reaches the check through `cleanup_paths`.

Both drive `_clean_path`, so there is one place a cleanup is decided and one
place a refusal is named. A hook carrying its own policy would be a second
policy, and two policies are how a gate ends up disagreeing with itself about
the same file.

## Required, optional, and why the second never blocks

A case is REQUIRED when the policy enables its rule and the checker offered an
edit, and that write did not land. It blocks, carrying the refusal's own reason.

A case is a SUGGESTION when a rule this build can apply is one the approved
policy does not enable, and the checker still found a candidate for it. It is
reported and it NEVER blocks. An unapproved optional suggestion is not a defect
in the product, and a gate that blocked on it would teach operators to switch
cleanup off rather than to write a policy.

A preview that REFUSES is neither. The checker decided there is nothing safe to
write, so nothing is required. That is also why a file with no candidate at all
reports nothing: there is no work, and a gate that reported empty work would
cost a git call per file for a message nobody could act on. Surfacing refused
suggestions to a person belongs to checkpoint 4's dashboard, not to a hot path.

## What is never done here

Nothing writes except `_clean_path`, and only through `apply_cleanup`, which is
the single writer in this package. `freshness` and `candidate_gaps` are
non-mutating by construction: they preview and compare, and the only policy they
can hold is one whose mode does not authorize a write. That is enforced by
`_read_only` rather than promised in a docstring, because a non-mutating
function that accepted a write-capable policy would be one refactor away from
being a second writer.

Evidence is invalidated by the existing source inventory and by nothing else. A
`Cleaned` verdict carries the `SourceIdentity` measured before and after the
write, which is the same digest `execution` and `tasks._identity_gaps` already
compare. This module adds no second invalidation mechanism, and adding one
would let a cleanup write go unseen by the one place that decides whether
evidence still describes the tree.

## What this does not decide

File-level ownership. `verify_ownership` establishes that a generation still
holds its checkout, its generation and its resource claims, and that is the only
ownership decision in the system. Whether one particular changed file belongs to
one task is recorded nowhere in this build, so the changed set is the
checkout's own dirty set filtered by that ownership check, and this module says
so rather than inventing a narrower claim it cannot support. Checkpoint 4 is
where per-file scope belongs, and it belongs there with a record to back it.

Policy is read from `verification/cleanup.json`, a plain document
`parse_policy` validates, with absence meaning `OFF_POLICY`. One file and one
reader, because a second place to enable automatic writes is a second authority
over them.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from ..identity import SourceIdentity, compute_source_identity
from ..nowindow import hidden_window
from ..paths import Project
from .apply import AlreadyApplied, Applied, ApplyRequest, apply_cleanup, ownership_from_tasks
from .comments import TRAILING_RULE, Proposal as CommentProposal, preview_comment_cleanup
from .logic import REGISTERED_RULES, LogicProposal, preview_logic_cleanup
from .policy import CleanupMode, CleanupPolicy, OFF_POLICY, parse_policy

#: Where an owner approves a cleanup policy for one project.
POLICY_RELATIVE = "verification/cleanup.json"

#: Comment and logic rules act on the same bytes, so a file can need more than
#: one pass: removing an outer logic site can expose an inner one whose spans
#: previously overlapped. Bounded because an unbounded convergence loop is a
#: hang. Hitting the cap is not a silent success -- the loop falls through to
#: the reporting below, so a file with work left names it as pending.
MAX_ROUNDS = 3

#: Every rule this build can apply, comment first so the cheapest and most
#: common edit lands before the logic rules ask the compiler anything.
RULE_ORDER: tuple[str, ...] = (TRAILING_RULE, *REGISTERED_RULES)


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


#: The three per-file kinds. A closed vocabulary, because the caller branches
#: on it and a branch on a free string is a branch nobody can review.
APPLIED = "applied"
REQUIRED_UNMET = "required_unmet"
SUGGESTED = "suggested"


@dataclass(frozen=True)
class Pending:
    """One file's outcome, and why it is what it is.

    `detail` is required. A block a caller cannot explain is a gate an operator
    turns off, and the refusal's own reason is more specific than anything this
    layer could invent from the outside.
    """

    relative_path: str
    kind: str
    rule_id: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "kind": self.kind,
            "rule": self.rule_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Cleared:
    """Nothing was required, so a check may capture its source identity.

    Also the verdict for a repository holding only optional suggestions, which
    is the case that most often looks like a bug in review and is not one.
    """

    inspected: tuple[str, ...]
    suggestions: tuple[Pending, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "CLEARED",
            "inspected": list(self.inspected),
            "suggestions": [item.to_json() for item in self.suggestions],
        }


@dataclass(frozen=True)
class Cleaned:
    """Verified cleanup was applied, so the source identity moved.

    A caller MUST capture a fresh `SourceIdentity` after this. Carrying the one
    it held before the write would describe code that no longer exists, and the
    digest pair below is the only honest statement of what happened.
    """

    applied: tuple[Pending, ...]
    before: SourceIdentity
    after: SourceIdentity
    inspected: tuple[str, ...]
    suggestions: tuple[Pending, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "CLEANED",
            "applied": [item.to_json() for item in self.applied],
            "inspected": list(self.inspected),
            "suggestions": [item.to_json() for item in self.suggestions],
            "sourceBefore": self.before.inventory_digest,
            "sourceAfter": self.after.inventory_digest,
        }


@dataclass(frozen=True)
class Blocked:
    """A required cleanup remains pending or failed, so the check is blocked.

    The reason is required, which is the point of the variant.
    """

    reason: str
    detail: str
    pending: tuple[Pending, ...]
    suggestions: tuple[Pending, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "BLOCKED",
            "reason": self.reason,
            "detail": self.detail,
            "pending": [item.to_json() for item in self.pending],
            "suggestions": [item.to_json() for item in self.suggestions],
        }


#: The sum. A pre-verification clears, cleans, or blocks. Never a boolean: the
#: caller's three obligations differ, and a boolean loses the difference between
#: "nothing to do" and "I changed your files and you must re-measure".
PreVerification = Cleared | Cleaned | Blocked


class CleanupUnavailable(Exception):
    """The gate could not ask its question, so it has no answer to give.

    Distinct from a `Blocked` verdict. Blocked is a decision about the code; a
    caller that cannot open the store has no decision, and reporting the two
    alike would let a broken installation read as a clean repository.
    """


# --------------------------------------------------------------------------- #
# Boundary reads
# --------------------------------------------------------------------------- #


def load_policy(project: Project) -> CleanupPolicy:
    """The approved policy for this project, or `OFF_POLICY` when there is none.

    A malformed document is `OFF_POLICY` rather than an exception, because a
    caller on the pre-verification path has to produce a verdict and cannot
    afford to raise from inside one. The failure is not silent: `policy_problem`
    names it separately, so an operator's typo is not reported as a project that
    never opted in.
    """
    path = project.root / POLICY_RELATIVE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return OFF_POLICY
    try:
        return parse_policy(document)
    except Exception:  # noqa: BLE001 - every `parse_policy` refusal means off
        return OFF_POLICY


def policy_problem(project: Project) -> str | None:
    """Why this project has no usable policy, or None when it has one.

    Separate from `load_policy` so that "cleanup is off" and "the file you
    wrote is not a policy" stay two answers.
    """
    path = project.root / POLICY_RELATIVE
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"{POLICY_RELATIVE} could not be read: {exc}"
    try:
        parse_policy(document)
    except Exception as exc:  # noqa: BLE001 - every refusal names itself
        return f"{POLICY_RELATIVE} is not a usable cleanup policy: {exc}"
    return None


def resolve_repository_path(project: Project, raw: str) -> str | None:
    """The repository-relative POSIX path a host payload names, or None.

    A Windows payload carries a backslash absolute path, a POSIX one a forward
    slash, and either may arrive relative to the payload's `cwd`. All three
    normalize to one form here, because a comparison downstream that understood
    only one of them would look at a file the host just edited and still
    conclude it was not there.

    None means the path lies outside the project or names no file. It is never
    a guess at the nearest file inside the root.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(os.path.normpath(raw.strip()))
    if not candidate.is_absolute():
        candidate = project.root / candidate
    try:
        resolved = candidate.resolve()
        root = project.root.resolve()
    except OSError:
        return None
    if root not in resolved.parents:
        return None
    return resolved.relative_to(root).as_posix()


# --------------------------------------------------------------------------- #
# Changed-line evidence
# --------------------------------------------------------------------------- #


class _AllLines:
    """Every line of the file is a changed line.

    A sentinel object rather than a large integer or a string, because both of
    those survive being passed where a line-number iterable belongs and then
    mean something else: an integer iterates to nothing, and a string iterates
    to its own characters. Iterating this raises, so a caller that forgot to
    expand it fails at the point of the mistake rather than silently selecting
    no line and quietly cleaning nothing.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "ALL_LINES"

    def __iter__(self):
        raise TypeError(
            "ALL_LINES is a sentinel, not a line-number iterable. Call "
            "hooks.all_lines(data) to get the numbers the preview expects."
        )


#: Every line of the file. Distinct from an empty iterable, because the two mean
#: opposite things: every line is a candidate for cleanup, and no line is.
ALL_LINES = _AllLines()

#: A callable from a repository-relative path to its changed lines: `ALL_LINES`,
#: an iterable of 1-based line numbers, or `None` for no evidence.
ChangedLines = Callable[[str], Any]

#: A factory for the above. Re-derived once per round on the authority path,
#: because a logic rule that removed lines shifted every line number after it
#: and a second round reading the pre-write diff would attribute a comment to a
#: line the task never changed.
ChangedLinesFactory = Callable[[], ChangedLines]


def _git(project: Project, *args: str) -> bytes:
    """Git output, or empty bytes on any failure.

    Empty means "no evidence", never "nothing changed": a git that is missing,
    slow or erroring must not make a cleanup look unnecessary, and the direction
    that loses is a missed cleanup rather than an edit outside scope.
    """
    try:
        done = subprocess.run(
            ["git", *args], cwd=project.root, capture_output=True, timeout=30,
            check=False, **hidden_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    return done.stdout if done.returncode == 0 else b""


def changed_lines_from_git(project: Project) -> ChangedLines:
    """Changed lines per relative path, read from the checkout's own diff.

    This is the evidence the hook cannot supply, which is why the
    pre-verification path needs its own. A shell command that rewrote a file
    leaves exactly the diff a tracked edit does, and nothing in the source
    inventory says which host action produced it.

    A path the index does not track is new, so every line of it is changed. A
    file git could not describe reports `None`, which selects no line, and the
    comment rule then removes nothing on it.
    """
    zero_context = _git(project, "diff", "-U0", "--no-color", "--no-ext-diff", "HEAD")
    untracked = _git(project, "ls-files", "--others", "--exclude-standard", "-z")
    new_paths = {
        raw.decode("utf-8", "surrogateescape")
        for raw in untracked.split(b"\x00") if raw
    }

    by_path: dict[str, set[int]] = {}
    current: str | None = None
    for raw in zero_context.split(b"\n"):
        if raw.startswith(b"+++ "):
            name = raw[4:].decode("utf-8", "surrogateescape").strip()
            if name == "/dev/null":
                current = None
                continue
            current = name[2:] if name.startswith("b/") else name
            by_path.setdefault(current, set())
        elif raw.startswith(b"@@") and current is not None:
            new_range = raw.split(b"+", 1)[1].split(b" ", 1)[0].decode("ascii", "replace")
            start_text, _, count_text = new_range.partition(",")
            try:
                start = int(start_text)
                count = int(count_text) if count_text else 1
            except ValueError:
                continue
            by_path[current].update(range(start, start + max(count, 1)))

    def resolve(relative_path: str) -> Any:
        if relative_path in new_paths:
            return ALL_LINES
        lines = by_path.get(relative_path)
        return None if lines is None else frozenset(lines)

    return resolve


def all_lines(data: bytes) -> list[int]:
    """Every 1-based line number in `data`.

    The comment preview takes an iterable of line numbers rather than a
    sentinel, so the expansion happens here, once, against the bytes the
    preview is about to read.
    """
    return list(range(1, data.count(b"\n") + 2))


def changed_files(project: Project) -> tuple[str, ...]:
    """Every path whose bytes differ from HEAD, tracked or untracked.

    `git status --porcelain` rather than the source inventory's own dirty set,
    because this needs the paths and not a digest, and because routing through
    the inventory's secret exclusions would skip source files whose names
    happen to match. The inventory decides whether evidence still describes the
    tree; it is not a list of files to edit.
    """
    raw = _git(project, "status", "--porcelain", "-z", "--untracked-files=all")
    paths: list[str] = []
    fields = raw.split(b"\x00")
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        if record[:1] in (b"R", b"C"):
            # A rename or copy writes its origin as its own NUL-terminated
            # field. It is the same change, and the path reported after it is
            # the one that exists in the tree.
            index += 1
        paths.append(record[3:].decode("utf-8", "surrogateescape"))
    return tuple(dict.fromkeys(sorted(paths)))


# --------------------------------------------------------------------------- #
# One file
# --------------------------------------------------------------------------- #


def _request_key(
    task_id: str, generation: int, relative_path: str, before: bytes, after: bytes
) -> str:
    """A request key derived from content, with no clock and no counter.

    The same edit replayed under the same attempt is the same request, which is
    what makes a host that fires this twice on one edit converge rather than
    apply twice. Deriving the key here rather than minting one per call is the
    whole of loop prevention on this path; the apply module's `AlreadyApplied`
    is the second half, and either alone would do.
    """
    running = hashlib.sha256()
    for value in (
        task_id, str(generation), relative_path,
        hashlib.sha256(before).hexdigest(),
        hashlib.sha256(after).hexdigest(),
    ):
        running.update(value.encode("utf-8", "surrogateescape"))
        running.update(b"\x00")
    return f"hook:{running.hexdigest()}"


def _sites(proposal: Any) -> Sequence[Any]:
    """What a proposal would remove, or empty for a refusal.

    A refusal is one of the two preview sums' members and carries neither
    `removed` nor `sites`, so asking it for either is the wrong question: it has
    nothing to remove, because the checker already decided that nothing is safe
    to write. Returning empty here is what keeps a refusal from becoming a
    pending requirement, and it is the only place that decision is made.
    """
    if isinstance(proposal, CommentProposal):
        return proposal.removed
    if isinstance(proposal, LogicProposal):
        return proposal.sites
    return ()


def _propose(
    project: Project, relative_path: str, rule_id: str, lines: Any,
) -> CommentProposal | LogicProposal | None:
    """The edit one rule would make right now, or None if there is none.

    The previews do the reading, the tokenizing and the preservation checking.
    This only chooses between their two shapes and hands back `None` for
    anything that is not an offered edit, which is where a malformed file, an
    unsupported language, an excluded path and an unproved equality all end up.
    """
    try:
        if rule_id == TRAILING_RULE:
            if lines is ALL_LINES:
                lines = all_lines((project.root / relative_path).read_bytes())
            return preview_comment_cleanup(project, relative_path, changed_lines=lines)
        if rule_id in REGISTERED_RULES:
            return preview_logic_cleanup(project, relative_path, rule_ids=[rule_id])
    except OSError:
        return None
    return None


def _clean_path(
    project: Project,
    relative_path: str,
    policy: CleanupPolicy,
    lines_for: ChangedLinesFactory,
    *,
    task_id: str,
    generation: int,
    ownership_check=None,
) -> tuple[list[Pending], list[Pending]]:
    """Bring one file to the state the policy requires. Decides nothing itself.

    Returns `(applied, unresolved)`, where `unresolved` is every required case
    that was not written followed by every optional suggestion. The caller
    decides whether any of it blocks, because only the caller knows whether a
    check is being gated.

    Comment and logic rules get separate turns because a file can hold both, and
    a later turn previews the bytes an earlier one left, which is the only way
    it sees a truthful `before`. The loop ends on the first round that writes
    nothing, so a file with nothing to do costs one preview per enabled rule.

    A round that reaches `MAX_ROUNDS` falls through to the reporting below
    rather than returning quietly, so a file that somehow keeps producing work
    is named as pending instead of being declared clean.
    """
    enabled = [rule for rule in RULE_ORDER if policy.admits(relative_path, rule)]
    if not enabled:
        return [], []

    applied: list[Pending] = []
    unmet: dict[str, Pending] = {}

    for _round in range(MAX_ROUNDS):
        wrote = False
        lines = lines_for()
        for rule_id in enabled:
            proposal = _propose(project, relative_path, rule_id, lines(relative_path))
            if proposal is None or not _sites(proposal):
                continue
            result = apply_cleanup(
                project,
                ApplyRequest(
                    request_id=_request_key(
                        task_id, generation, relative_path,
                        proposal.before_bytes, proposal.after_bytes,
                    ),
                    proposal=proposal,
                    generation=generation,
                    policy=policy,
                ),
                ownership_check=ownership_check,
            )
            if isinstance(result, Applied):
                applied.append(Pending(
                    relative_path, APPLIED, rule_id,
                    f"{len(_sites(proposal))} site(s) removed; before "
                    f"{result.before_digest[:12]}, after {result.after_digest[:12]}, "
                    f"preservation receipt {result.receipt.get('result', 'unknown')}",
                ))
                wrote = True
            elif isinstance(result, AlreadyApplied):
                # The request already carried out, found by re-reading the file
                # rather than by consulting a log. That is a success and not a
                # requirement left unmet, and it is the branch a repeated host
                # invocation lands on. Calling it unmet would name a gate that
                # does not exist and, because `AlreadyApplied` carries no
                # reason, would raise rather than report.
                applied.append(Pending(
                    relative_path, APPLIED, rule_id,
                    f"already applied under request {result.request_id}; after "
                    f"{result.after_digest[:12]} was already in place",
                ))
                wrote = True
            else:
                unmet[rule_id] = Pending(
                    relative_path, REQUIRED_UNMET, rule_id,
                    f"{result.reason}: {result.detail}",
                )
        if not wrote:
            break

    # Rules the policy does not enable are read once, after the writes, so a
    # suggestion describes the file as it will be left rather than as it was
    # found.
    suggestions: list[Pending] = []
    lines = lines_for()
    for rule_id in RULE_ORDER:
        if rule_id in enabled:
            continue
        proposal = _propose(project, relative_path, rule_id, lines(relative_path))
        if proposal is not None and _sites(proposal):
            suggestions.append(Pending(
                relative_path, SUGGESTED, rule_id,
                f"{len(_sites(proposal))} site(s) the approved policy does not enable "
                f"for {relative_path}; the file is unchanged",
            ))

    return applied, [*unmet.values(), *suggestions]


# --------------------------------------------------------------------------- #
# The authority
# --------------------------------------------------------------------------- #


def _generation(store, task_id: str) -> int:
    from ..tasks import TaskError, get_task

    try:
        return get_task(store, task_id).generation
    except TaskError as exc:
        raise CleanupUnavailable(f"task {task_id} cannot be read: {exc}") from exc


def cleanup_paths(
    project: Project,
    store,
    task_id: str,
    *,
    policy: CleanupPolicy | None = None,
    paths: Sequence[str] | None = None,
) -> PreVerification:
    """Apply every required cleanup in this checkout, then say what the caller owes.

    The authority path. It reads the changed set itself rather than accepting
    one from a host event, which is what lets a change made by a shell command
    reach the same gate as a change made by an editor.

    The source identity is captured before the first write and again after the
    last, and both are on the verdict. A caller that captured one before calling
    this must discard it: the digest moved, and that is the existing
    invalidation mechanism, used rather than duplicated.
    """
    active = load_policy(project) if policy is None else policy
    problem = policy_problem(project) if policy is None else None
    if problem is not None:
        return Blocked("cleanup_policy_unusable", problem, pending=())
    if not active.may_write():
        return Cleared(inspected=())

    generation = _generation(store, task_id)
    ownership_check = ownership_from_tasks(project, store, task_id)
    targets = tuple(paths) if paths is not None else changed_files(project)

    before = compute_source_identity(project)
    applied: list[Pending] = []
    required: list[Pending] = []
    suggestions: list[Pending] = []

    for relative_path in targets:
        file_applied, unresolved = _clean_path(
            project, relative_path, active,
            lambda: changed_lines_from_git(project),
            task_id=task_id, generation=generation, ownership_check=ownership_check,
        )
        applied.extend(file_applied)
        for item in unresolved:
            (suggestions if item.kind == SUGGESTED else required).append(item)

    if required:
        first = required[0]
        return Blocked(
            f"cleanup_required_unmet:{first.rule_id}",
            f"{len(required)} required cleanup(s) could not be applied. {first.detail}",
            pending=tuple(required),
            suggestions=tuple(suggestions),
        )
    if applied:
        return Cleaned(
            applied=tuple(applied), before=before,
            after=compute_source_identity(project),
            inspected=targets, suggestions=tuple(suggestions),
        )
    return Cleared(inspected=targets, suggestions=tuple(suggestions))


def accelerate(
    project: Project,
    store,
    task_id: str,
    relative_path: str,
    *,
    policy: CleanupPolicy | None = None,
) -> PreVerification:
    """Clean the one file a host payload named, right now.

    The accelerator, not the authority. It is fast because it does no status and
    no diff: the host already told it which file changed, and the authority
    re-derives the same set from the checkout when the check runs. A hook that
    stopped here would be the bug this function's existence prevents.

    Every line of the named file is treated as changed, because the payload
    says the file was edited and does not say which lines. That is the fast
    path's one liberty, and it is why a file edited through a shell still needs
    `cleanup_paths`: this function has no way to learn about it.
    """
    active = load_policy(project) if policy is None else policy
    problem = policy_problem(project) if policy is None else None
    if problem is not None:
        return Blocked("cleanup_policy_unusable", problem, pending=())
    if not active.may_write():
        return Cleared(inspected=())

    generation = _generation(store, task_id)
    before = compute_source_identity(project)
    applied, unresolved = _clean_path(
        project, relative_path, active, lambda: (lambda _path: ALL_LINES),
        task_id=task_id, generation=generation,
        ownership_check=ownership_from_tasks(project, store, task_id),
    )
    required = [item for item in unresolved if item.kind != SUGGESTED]
    suggestions = [item for item in unresolved if item.kind == SUGGESTED]

    if required:
        return Blocked(
            f"cleanup_required_unmet:{required[0].rule_id}",
            required[0].detail,
            pending=tuple(required), suggestions=tuple(suggestions),
        )
    if applied:
        return Cleaned(
            applied=tuple(applied), before=before,
            after=compute_source_identity(project),
            inspected=(relative_path,), suggestions=tuple(suggestions),
        )
    return Cleared(inspected=(relative_path,), suggestions=tuple(suggestions))


# --------------------------------------------------------------------------- #
# Non-mutating
# --------------------------------------------------------------------------- #


def _read_only(policy: CleanupPolicy) -> CleanupPolicy:
    """The same policy with the write bit removed.

    A copy, not a mutation. `CleanupPolicy` is frozen and an operator's approved
    policy must not be narrowed as a side effect of asking a question about it.
    """
    return replace(policy, mode=CleanupMode.PREVIEW)


def freshness(project: Project, *, policy: CleanupPolicy | None = None) -> tuple[str, ...]:
    """The changed files still holding a required cleanup, as `path:rule` pairs.

    Reads only. What finalization and any other post-evidence caller asks. It
    previews and compares and never applies, so the answer cannot move the source
    it describes, and a stop hook that called it cannot invalidate the evidence
    it is checking. The policy is demoted to `preview` for that reason, so the
    guarantee is a property of this code rather than a promise about its
    caller.

    Only required cases appear. An unapproved suggestion is not a thing
    finalization must refuse to complete over.
    """
    active = _read_only(load_policy(project) if policy is None else policy)
    enabled = [rule for rule in RULE_ORDER if rule in active.enabled_rules]
    if not enabled:
        return ()

    lines_for = changed_lines_from_git(project)
    pending: list[str] = []
    for relative_path in changed_files(project):
        if not active.rules_for(relative_path):
            continue
        lines = lines_for(relative_path)
        for rule_id in enabled:
            proposal = _propose(project, relative_path, rule_id, lines)
            if proposal is not None and _sites(proposal):
                pending.append(f"{relative_path}:{rule_id}")
                break
    return tuple(pending)


def candidate_gaps(checkout: Project, *, policy: CleanupPolicy | None = None) -> tuple[str, ...]:
    """The cleanup a protected candidate still owes, in a checkout that is not written.

    For the integration check. It reports the gap and asks for a new candidate
    commit. It does not clean the tree and then attest to the commit it was
    cleaning, because that ordering verifies something no reviewer ever
    approved and no afterwards-the-fact evidence repairs.

    This is `freshness` under the name the integration path reads it by, and it
    is spelled as its own function so the integration call site names the rule
    it depends on rather than reaching for a general helper and inheriting its
    meaning by accident.
    """
    return freshness(checkout, policy=policy)


def to_json(verdict: PreVerification) -> dict[str, Any]:
    """The bounded structured result a CLI, an MCP tool or a hook returns."""
    return verdict.to_json()
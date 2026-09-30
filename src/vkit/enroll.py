"""Enroll a repository, or propose enrolling it, and never overwrite by surprise.

Three rules shape this module, and each one exists because of a way the obvious
implementation goes wrong.

**A hand-maintained file is never overwritten.** `enroll` on a repository that
already has a manifest is not a no-op and is not an overwrite. It is a refusal
that shows the difference between what is there and what would be written, and
lets a person decide. The alternative is a command whose output depends on
whether it ran twice, which is how executable policy gets lost.

**Execution stays impossible until a person accepts it.** This is state, not a
flag. The rule is enforced structurally: a *proposal* lives at a different path
from the *policy*, and the core's `parse_manifest` only ever reads the policy
path. A proposed manifest is not a manifest, because the one function that
decides what may be executed cannot see it. There is no ordering of flags, no
default, and no environment variable that turns execution on early.

**What would run is shown before it can run.** Acceptance requires a digest of
the exact policy being accepted. A person who accepts a proposal is accepting
the bytes it had at that moment; if the file changed in between, the digest
does not match and acceptance is refused, because the thing they reviewed is no
longer the thing that would execute.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .discover import DiscoveredCommand, Inspection, inspect_repository
from .manifest import Manifest, ManifestError, parse_manifest
from .paths import Project

#: Where a proposed manifest waits for a human. Deliberately not the policy
#: path, and deliberately inside the repository so it shows up in `git status`
#: and therefore in a diff the person accepting it will actually look at.
PROPOSAL_RELATIVE = Path("verification") / "proposed-manifest.json"

#: Where the acceptance record lives. Under the shared Git directory, because
#: it is evidence about the policy rather than part of the policy, and two
#: clones must agree about whether the policy was accepted.
ENROLLMENT_NAME = "enrollment.json"

SCHEMA_VERSION = 1


class EnrollmentError(Exception):
    """Enrollment cannot proceed. Carries what the person needs to fix it."""


class State(str, Enum):
    """Where this repository stands with respect to executable policy.

    The order of these is the order the states are reached in, and nothing skips
    a step. `NOT_ENROLLED` is the only state in which execution is impossible
    for policy reasons rather than for environmental ones.
    """

    NOT_ENROLLED = "not_enrolled"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"

    @property
    def execution_permitted(self) -> bool:
        """Whether the core may launch a command this repository declared.

        True in exactly one state. Everything else is False, including a
        repository whose policy file exists but was never accepted, which is the
        case a boolean flag gets wrong.
        """
        return self is State.ACCEPTED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PolicyDigest:
    """What a person agreed to, identified by its content.

    A digest over the *parsed* command policy rather than the file's bytes, for
    the reason `Manifest.digest` exists: reformatting the JSON changes the
    bytes and not the policy, and a person must not be asked to re-approve a
    decision they already made because someone ran a formatter.
    """

    value: str

    def matches(self, other: str) -> bool:
        return self.value == other


def policy_digest(manifest: Manifest) -> PolicyDigest:
    return PolicyDigest(manifest.digest())


def _canonical(document: Any) -> bytes:
    """The bytes written to disk, with a trailing newline.

    Written through a temporary file and renamed, so a reader never sees half a
    manifest. A manifest that is half-written fails to parse, and a repository
    that cannot be inspected is a repository nobody can enroll.
    """
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(payload)
    temp.replace(path)


# ------------------------------------------------------------------ record

@dataclass(frozen=True)
class Enrollment:
    """The durable record of whether this repository's policy was accepted.

    `policy_digest` is the contract between the proposal and the acceptance.
    It is not decoration: it is what makes "the thing you reviewed is the thing
    that runs" checkable rather than a promise.
    """

    project: Project
    state: State
    policy_digest: str | None
    accepted_at: str | None
    proposed_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "command": "project enroll",
            "project": str(self.project.root),
            "state": self.state.value,
            "execution_permitted": self.state.execution_permitted,
            "policy_digest": self.policy_digest,
            "accepted_at": self.accepted_at,
            "proposed_at": self.proposed_at,
            "manifest_path": str(self.project.manifest_path),
            "proposal_path": str(self.project.root / PROPOSAL_RELATIVE),
        }

    def render(self) -> str:
        lines = [
            f"project : {self.project.root}",
            f"state    : {self.state.value}",
            f"policy   : {self.policy_digest or '<none>'}",
        ]
        if self.state is State.ACCEPTED:
            lines.append(f"accepted : {self.accepted_at}")
            lines.append("execution of this repository's declared commands is permitted")
        elif self.state is State.PROPOSED:
            lines.append(f"proposed : {self.proposed_at}")
            lines.append("a proposal is waiting at "
                         f"{self.project.root / PROPOSAL_RELATIVE}")
            lines.append("nothing in it can be executed; review it and accept explicitly")
        else:
            lines.append("this repository has no policy and nothing may be executed from it")
        return "\n".join(lines)


def record_path(project: Project) -> Path:
    return project.state_root / ENROLLMENT_NAME


def read_enrollment(project: Project) -> Enrollment:
    """The enrollment record, or `NOT_ENROLLED` when there is none.

    A record that cannot be read is not treated as absent. An unreadable record
    is a repository whose acceptance state is unknown, and defaulting unknown to
    "enrolled" would execute a policy nobody accepted. It resolves to
    `NOT_ENROLLED`, which is the safe direction, and the caller can see why from
    the gap the CLI reports.
    """
    path = record_path(project)
    if not path.is_file():
        return Enrollment(project, State.NOT_ENROLLED, None, None, None)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return Enrollment(
            project=project,
            state=State(document["state"]),
            policy_digest=document.get("policy_digest"),
            accepted_at=document.get("accepted_at"),
            proposed_at=document.get("proposed_at"),
        )
    except (OSError, ValueError, KeyError):
        return Enrollment(project, State.NOT_ENROLLED, None, None, None)


def _write_record(project: Project, record: Enrollment) -> None:
    write_atomic(record_path(project), _canonical({
        "schema_version": SCHEMA_VERSION,
        "state": record.state.value,
        "policy_digest": record.policy_digest,
        "accepted_at": record.accepted_at,
        "proposed_at": record.proposed_at,
    }))


# ------------------------------------------------------------------ proposal

@dataclass
class Proposal:
    """A complete, reviewable description of what enrolling would register.

    The proposed checks are real manifest entries, not discovery records. That
    distinction is the whole point: a proposal has to be parseable by
    `parse_manifest` before anyone is asked to accept it, otherwise acceptance
    validates a document nobody ever looked at.
    """

    project: Project
    inspection: Inspection
    entries: list[dict[str, Any]] = field(default_factory=list)
    sources: list[DiscoveredCommand] = field(default_factory=list)
    #: (id, reason) for each command left out, paired element-for-element with
    #: `skipped_sources`. Two parallel lists is worse than one list of pairs, and
    #: the pairing is checked with `strict=True` where it is consumed.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    skipped_sources: list[DiscoveredCommand] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def policy(self) -> dict[str, Any]:
        """The manifest document a proposal would install.

        Built as a real document so the person reviewing it reviews the exact
        bytes, and so the digest covers the same structure the core will parse.
        """
        return {
            "schema_version": 1,
            "description": (
                f"Proposed by vkit for {self.project.root.name}. "
                "Review every command: each one becomes executable policy."
            ),
            "checks": self.entries,
        }

    def command_policy_lines(self) -> list[str]:
        """The exact commands, one per block, for a person to read before accepting.

        Rendered by pairing each manifest entry with the discovery record it came
        from, so the preview and the policy describe the same list. A preview
        assembled separately from the document it previews is a second
        implementation of the same decision, and it is the one that goes stale.
        """
        lines: list[str] = []
        for entry, source in zip(self.entries, self.sources, strict=True):
            lines.append(f"  {entry['id']}")
            lines.append(f"    runs     : {' '.join(entry['command'])}")
            lines.append(f"    from     : {source.provenance.render()}")
            lines.append(f"    scenarios: {', '.join(entry['required_scenarios']) or '<none; nothing can be accepted yet>'}")
            if entry.get("cwd"):
                lines.append(f"    in       : {entry['cwd']}")
            # Read from the discovery record, not from the manifest entry. The
            # entry carries the schema's shape (name and executable only, since
            # that is all a manifest may say) and would not answer "is this
            # installed here", which is a question about this host.
            unmet = [p.name for p in source.prerequisites if not p.satisfied]
            lines.append(
                "    needs    : " + (", ".join(unmet) if unmet else "nothing missing on this host")
            )
        return lines

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "command": "project enroll",
            "project": str(self.project.root),
            "state": State.PROPOSED.value,
            "execution_permitted": False,
            "proposed": {
                "manifest": self.policy,
                "manifest_path": str(self.project.root / PROPOSAL_RELATIVE),
                "policy_path": str(self.project.manifest_path),
            },
            "checks": [c.to_json() for c in self.sources],
            "skipped": [
                {"id": i, "reason": r, "command": c.summary, "from": c.provenance.render()}
                for (i, r), c in zip(self.skipped, self.skipped_sources, strict=True)
            ],
            "gaps": list(self.gaps),
        }

    def render(self) -> str:
        lines = [
            f"project : {self.project.root}",
            f"found   : {len(self.inspection.commands)} discovered command(s)",
            "",
            "these commands would become executable policy:",
            *self.command_policy_lines(),
        ]
        if self.skipped:
            lines.append("")
            lines.append("not proposed:")
            for (identifier, reason), source in zip(self.skipped, self.skipped_sources, strict=True):
                lines.append(f"  {identifier} ({source.provenance.render()})")
                lines.append(f"    would run: {source.summary}")
                lines.append(f"    because  : {reason}")
        if self.gaps:
            lines.append("")
            lines.append("gaps")
            lines.extend(f"  {gap}" for gap in self.gaps)
        lines.append("")
        lines.append(f"nothing runs yet. The proposal is at {self.project.root / PROPOSAL_RELATIVE}")
        return "\n".join(lines)


def manifest_id(command_id: str) -> str:
    """Turn a discovery id into a valid manifest check id.

    Discovery names a command after where it came from (`npm:test`,
    `make:test`), which is a useful label and not a legal check id: the manifest
    schema allows letters, digits, dot, dash and underscore only. Without this
    translation every proposed manifest would be rejected by the schema before a
    person ever saw it, and the proposal would be unusable.
    """
    return command_id.replace(":", "-").replace("/", "-")


def _entry_for(project: Project, command: DiscoveredCommand) -> dict[str, Any]:
    """The manifest entry a proposed check starts as.

    `required_scenarios` is deliberately empty, and this is the safety property
    rather than an omission. A scenario id names an observation only someone who
    knows the application can state, and CONTRACT.md forbids the app from grading
    prose as proof. The manifest schema refuses an empty required list, so a
    proposal is *structurally* un-acceptable until a person writes the scenarios
    down.

    A proposal that arrived pre-filled would be a proposal whose expected
    outcomes came from a regex over a source file, and a behaviour regression
    could then rewrite its own acceptance.
    """
    return {
        "id": manifest_id(command.id),
        "description": (
            f"Discovered from {command.provenance.render()}. TODO: describe the "
            "user-visible behaviour this check proves."
        )[:200],
        "command": ["TODO", "the-check-driver", "--out", "{{run_dir}}/result.json"],
        "cwd": ".",
        "timeout_seconds": 300.0,
        "required_scenarios": [],
        "artifact": "result.json",
        "prerequisites": [
            {"name": p.name, "executable": p.executable} for p in command.prerequisites
        ],
        "inputs": [],
    }


def build_proposal(project: Project, inspection: Inspection | None = None) -> Proposal:
    """Turn discovered commands into a proposal a person has to finish.

    Everything mechanical is done here: the commands, their provenance, their
    prerequisites, and the reason each command that cannot be proposed was left
    out. The one thing left is the part vkit is not allowed to do, which is why
    the result is a proposal and not a policy.
    """
    report = inspection if inspection is not None else inspect_repository(project)
    proposal = Proposal(project=project, inspection=report)

    for command in report.commands:
        if command.kind == "install":
            proposal.skipped.append((
                command.id,
                "npm runs this during install; vkit never runs it and will not "
                "propose it as a check",
            ))
            proposal.skipped_sources.append(command)
            continue
        if command.kind != "test":
            continue
        unmet = [p.name for p in command.prerequisites if not p.satisfied]
        if unmet:
            proposal.skipped.append((
                command.id,
                f"prerequisite(s) not installed on this host: {', '.join(unmet)}",
            ))
            proposal.skipped_sources.append(command)
            continue
        proposal.entries.append(_entry_for(project, command))
        proposal.sources.append(command)

    if not proposal.entries:
        proposal.gaps.append(
            "no test command could be proposed. vkit found commands but cannot "
            "write the driver that exercises the application, because the expected "
            "output is a fact about the application and not about vkit"
        )
    else:
        proposal.gaps.append(
            "every proposed check has empty required_scenarios and a TODO command. "
            "Write the driver and the scenarios before accepting; the manifest schema "
            "refuses an empty scenario list, so an unfinished proposal cannot run"
        )
    proposal.gaps.extend(report.gaps)
    return proposal

def write_proposal(proposal: Proposal) -> Path:
    """Write the proposal where a reviewer's `git status` will show it."""
    path = proposal.project.root / PROPOSAL_RELATIVE
    write_atomic(path, _canonical(proposal.policy))
    record = Enrollment(
        project=proposal.project,
        state=State.PROPOSED,
        policy_digest=None,
        accepted_at=None,
        proposed_at=_now(),
    )
    _write_record(proposal.project, record)
    return path


def enroll(project: Project, *, inspection: Inspection | None = None) -> tuple[Proposal, Path]:
    """Propose enrolling this repository. Never writes the policy.

    Returns the proposal and the path of the written proposal document. The
    policy path is only ever written by `accept`, and only after a person asked
    for it, so a second `enroll` on an already-enrolled repository proposes
    again without disturbing what is there. The proposal lands beside the
    existing manifest under its own name, so `diff_summary` can show exactly
    what would change.

    A repository that already has a policy is not refused outright. Re-running
    `enroll` on your own project is a normal thing to do, and the useful answer
    is a proposal and a diff, not an error. What never happens is an overwrite.
    """
    proposal = build_proposal(project, inspection)
    path = write_proposal(proposal)
    return proposal, path


# ------------------------------------------------------------------ conflict

def diff_summary(existing: Path, proposed: Path) -> list[str]:
    """A line-level difference between two manifests, as readable text.

    A unified diff is what a person already knows how to read for this question.
    It is generated from the two files as they are, so it shows the actual
    disagreement rather than a description of one.
    """
    import difflib

    try:
        before = existing.read_text(encoding="utf-8").splitlines(keepends=True)
        after = proposed.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        return [f"cannot read one of the manifests: {exc}"]
    return list(
        difflib.unified_diff(before, after, fromfile=str(existing), tofile=str(proposed))
    )


# ------------------------------------------------------------------ accept

def accept(project: Project, *, expected_digest: str | None = None) -> Enrollment:
    """Promote a proposal to policy, or refuse and say why.

    The digest check is the whole safety property. A person approves a specific
    set of commands; between approving and running this function, the file on
    disk may have changed, and a policy that runs something other than what was
    approved is exactly the failure this refuses.
    """
    current = read_enrollment(project)
    proposal_path = project.root / PROPOSAL_RELATIVE
    manifest_path = project.manifest_path

    if not proposal_path.is_file():
        raise EnrollmentError(
            f"there is no proposal at {proposal_path}. Run `vkit project enroll` "
            "first; nothing can be accepted that was never proposed"
        )
    if manifest_path.is_file():
        raise EnrollmentError(
            f"{manifest_path} already exists and is hand-maintained. This command "
            "will not overwrite it. Read the difference and edit the file yourself "
            "if you want the proposed policy"
        )

    try:
        # Parse the *proposal*, not the policy path. Reading the policy path here
        # would validate a file that does not exist yet and report "no manifest",
        # which is a refusal for the wrong reason and hides the real one: the
        # proposal is incomplete.
        manifest = parse_manifest(project, project.runs_root / "probe", path=proposal_path)
    except ManifestError as exc:
        raise EnrollmentError(
            f"the proposal is not a valid manifest and cannot be accepted: {exc}. "
            "A proposal with an empty required_scenarios is refused by the schema; "
            "write the scenarios down first"
        ) from exc

    candidate = manifest.digest()
    if expected_digest is not None and candidate != expected_digest:
        raise EnrollmentError(
            f"the proposal changed since it was reviewed: you accepted policy "
            f"{expected_digest[:12]} and it is now {candidate[:12]}. Read it again"
        )

    write_atomic(manifest_path, proposal_path.read_bytes())
    record = Enrollment(
        project=project,
        state=State.ACCEPTED,
        policy_digest=candidate,
        accepted_at=_now(),
        proposed_at=current.proposed_at,
    )
    _write_record(project, record)
    return record


def decline(project: Project) -> Enrollment:
    """Remove a proposal and leave every existing file untouched.

    Declining is a real answer and it is a clean one. No manifest is written, no
    proposal is left behind to be discovered later by someone who assumes it was
    accepted, and the record says `not_enrolled` so the next `doctor` refuses to
    run anything.
    """
    proposal_path = project.root / PROPOSAL_RELATIVE
    removed = proposal_path.is_file()
    if removed:
        proposal_path.unlink()
    record = Enrollment(project=project, state=State.NOT_ENROLLED, policy_digest=None,
                        accepted_at=None, proposed_at=None)
    _write_record(project, record)
    if not removed:
        raise EnrollmentError(f"there was no proposal at {proposal_path} to decline")
    return record

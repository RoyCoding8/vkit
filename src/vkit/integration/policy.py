"""What an integration run requires comes from trusted configuration. The
candidate's own manifest is only ever a claim about what it does.

This is the module the whole plan turns on, so the reasoning is worth stating
plainly.

**The attack.** A candidate edits `verification/manifest.json` to drop the check
that catches its defect, or to lower a threshold, and ships the edit. If the
required set were read from the candidate's manifest, the edit would be
invisible: the manifest would declare a smaller obligation and the integration
run would dutifully verify the smaller obligation.

**The defense, in order of how much it costs.**

1. The policy is resolved before the candidate is checked out, so there is
   nothing read from the candidate bytes for it to influence.
2. The policy pins one approved commit that defines the whole manifest. The
   checks run from THAT manifest, re-rooted at the candidate checkout. Editing
   `verification/manifest.json` in the candidate therefore changes nothing
   about what executes. This is why the policy carries a single revision rather
   than a per-check one: one revision is one coherent policy, and merging
   manifests from several revisions would invent a policy nobody approved.
3. The candidate's manifest is read anyway and compared. A candidate that
   removed a required check or stopped reporting a required scenario is a
   POLICY_REVIEW finding, which refuses in the protected context and is never a
   quiet pass.
4. A policy supplied as a local path is LOCAL evidence. Local is not weaker, it
   is differently scoped: it produces a real refusal, and it can never be the
   protected integration decision. The context appears in every output.

**What "weakening" means here, precisely.** A required scenario may gain
scenarios; it may not lose one, and its timeout may not rise. A check the policy
requires must exist in the candidate's manifest with the same required
scenarios. A check the policy does not require is the candidate's business and
is recorded, not judged. The comparison is on parsed records, so reformatting a
manifest is not a policy change and cannot smuggle one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..claimkind import ClaimCategory
from ..manifest import Manifest, ManifestError
from ..paths import open_project
from ..verifiers import (
    Obligation,
    TheoremObligation,
    describe_obligation,
    obligation_from_json,
    obligation_to_json,
)
from .gitidentity import GitError, digest_bytes, git_blob, resolve_commit

POLICY_SCHEMA_VERSION = 1
MANIFEST_RELATIVE = "verification/manifest.json"

LOCAL = "local"
PROTECTED = "protected"
CONTEXTS = (LOCAL, PROTECTED)

POLICY_KEYS = frozenset({
    "schema_version", "description", "context", "manifest_revision", "required_checks",
})
CHECK_KEYS = frozenset({"id", "obligations", "evidence_kind"})

SEVERITIES = ("REJECT", "REVIEW")


class PolicyError(Exception):
    """The policy is unusable, or names a check that cannot be required."""


@dataclass(frozen=True)
class PolicyCheck:
    """One check the policy requires, and the obligations it must discharge.

    `evidence_kind` is an assertion, not a grant. vkit derives the category from
    the check's variant and refuses a policy whose assertion disagrees, so a
    candidate cannot downgrade by writing `scenario` in its policy or promote by
    writing `property`.
    """

    id: str
    obligations: tuple[Obligation, ...] = ()
    evidence_kind: ClaimCategory | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "obligations": [obligation_to_json(o) for o in self.obligations],
            "evidence_kind": self.evidence_kind.value if self.evidence_kind else None,
        }


@dataclass(frozen=True)
class Policy:
    """The approved obligation for an integration run.

    `manifest_revision` is a commit, never a ref, by the time it reaches here. A
    ref could move between the policy being written and the run that uses it,
    which would make the required bar a function of when the run happened.
    """

    description: str
    context: str
    required: tuple[PolicyCheck, ...]
    manifest_revision: str | None = None
    origin: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.context not in CONTEXTS:
            raise PolicyError(
                f"unknown policy context {self.context!r}; expected one of {', '.join(CONTEXTS)}"
            )
        if not self.required:
            raise PolicyError("a policy must require at least one check")
        if self.context == PROTECTED and not self.manifest_revision:
            raise PolicyError(
                "a protected policy must pin manifest_revision to the approved commit "
                "that defines the checks. Without it the run would execute the "
                "candidate's own manifest, and a candidate that edited "
                f"{MANIFEST_RELATIVE} would be lowering its own bar."
            )

    @property
    def required_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.required)

    def digest(self) -> str:
        """Identity of the obligation, independent of how it was supplied.

        Not a digest of the file. A policy that arrived as `--policy @<commit>`
        and one that arrived as a copied file with the same contents require the
        same thing, and evidence from the two must be comparable. The context is
        inside the digest because a local policy and a protected one are not the
        same obligation even with identical check lists.
        """
        body = json.dumps(
            {
                "context": self.context,
                "manifest_revision": self.manifest_revision,
                "required_checks": [c.to_json() for c in self.required],
            },
            sort_keys=True, separators=(",", ":"),
        )
        return digest_bytes(body.encode("utf-8"))

    @property
    def policy_id(self) -> str:
        return f"policy-{self.digest()[:16]}"

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "description": self.description,
            "context": self.context,
            "digest": self.digest(),
            "manifest_revision": self.manifest_revision,
            "required_checks": [c.to_json() for c in self.required],
            "origin": self.origin,
        }


def _parse_document(raw: Any, origin: dict[str, Any]) -> Policy:
    """Validate one policy document. Every rejection is a PolicyError."""
    if not isinstance(raw, dict):
        raise PolicyError("a policy must be a JSON object")
    unknown = sorted(set(raw) - POLICY_KEYS)
    if unknown:
        raise PolicyError(
            f"unsupported policy key(s): {', '.join(unknown)}. A policy declares "
            f"{', '.join(sorted(POLICY_KEYS))}"
        )
    if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise PolicyError(
            f"unsupported policy schema_version {raw.get('schema_version')!r}; "
            f"this build supports {POLICY_SCHEMA_VERSION}"
        )
    context = raw.get("context", LOCAL)
    if context not in CONTEXTS:
        raise PolicyError(
            f"policy declares context {context!r}; expected one of {', '.join(CONTEXTS)}"
        )
    entries = raw.get("required_checks")
    if not isinstance(entries, list) or not entries:
        raise PolicyError("policy.required_checks must be a nonempty list")
    required: list[PolicyCheck] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise PolicyError("each required check must be an object")
        unexpected = sorted(set(entry) - CHECK_KEYS)
        if unexpected:
            raise PolicyError(
                f"unsupported key(s) {', '.join(unexpected)} on required check "
                f"{entry.get('id', '<unnamed>')}"
            )
        check_id = entry.get("id")
        if not isinstance(check_id, str) or not check_id:
            raise PolicyError("each required check needs a nonempty id string")
        if check_id in seen:
            raise PolicyError(f"required check {check_id!r} is listed twice")
        seen.add(check_id)

        declared = entry.get("evidence_kind")
        category = None
        if declared is not None:
            try:
                category = ClaimCategory(declared)
            except ValueError:
                known = ", ".join(c.value for c in ClaimCategory)
                raise PolicyError(
                    f"required check {check_id!r}: evidence_kind {declared!r} is not "
                    f"one of {known}. The category is derived from the check's "
                    f"variant, so a policy may only assert one that already holds."
                ) from None

        raw_obligations = entry.get("obligations", [])
        if not isinstance(raw_obligations, list) or any(
            not isinstance(o, dict) for o in raw_obligations
        ):
            raise PolicyError(
                f"required check {check_id!r}: obligations must be a list of "
                "obligation objects"
            )
        try:
            obligations = tuple(obligation_from_json(o) for o in raw_obligations)
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError(
                f"required check {check_id!r}: cannot read obligations: {exc}"
            ) from exc
        duplicates = {o for o in obligations if obligations.count(o) > 1}
        if duplicates:
            named = ", ".join(sorted(describe_obligation(o) for o in duplicates))
            raise PolicyError(f"required check {check_id!r}: duplicate obligations {named}")

        required.append(PolicyCheck(id=check_id, obligations=obligations,
                                    evidence_kind=category))

    manifest_revision = raw.get("manifest_revision")
    if manifest_revision is not None and (not isinstance(manifest_revision, str)
                                          or not manifest_revision.strip()):
        raise PolicyError("manifest_revision must be a nonempty commit reference when present")
    return Policy(
        description=raw.get("description", ""),
        context=context,
        required=tuple(required),
        manifest_revision=manifest_revision.strip() if manifest_revision else None,
        origin=origin,
    )


def from_file(project, path: Path) -> Policy:
    """A policy the operator selected from the local filesystem.

    This is LOCAL evidence, and the returned policy says so in its origin. A
    local policy can produce a real refusal and that refusal is a real answer;
    it is simply not the protected integration decision, and nothing downstream
    reports it as one.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{path} is not valid JSON: {exc}") from exc

    claimed = raw.get("context") if isinstance(raw, dict) else None
    if claimed == PROTECTED:
        raise PolicyError(
            f"{path} declares context {PROTECTED!r}. A local file cannot confer "
            "protected integration context: only an approved reference passed as "
            f"`--policy @<ref>` can, because the reference is what a reviewer "
            "approved. Use the path for local evidence, or name the approved commit."
        )
    return _parse_document(raw, origin={
        "source": "local-file",
        "path": str(path),
        "authority": "user-selected; local evidence, not protected integration policy",
        "claimed_context": claimed,
    })


def from_commit(project, ref: str) -> Policy:
    """A policy read from a commit in this repository, spelled `@<ref>`.

    The ref is resolved to a commit first, so the policy bytes come from a fixed
    point in history rather than from wherever a branch happens to point, and so
    the accepted reference is itself an identity the acceptance record carries.

    A policy file in a repository may declare `context`, but the context is
    decided here, from the spelling the operator used, and the document's own
    claim is recorded as unverified. A candidate that shipped a policy saying
    `"context": "protected"` does not become trusted by writing the word down:
    `@<ref>` is the protected configuration and a path is not.
    """
    revision = resolve_commit(project, ref, what="policy")
    try:
        blob = git_blob(project, revision, "policy.json")
    except GitError as exc:
        raise PolicyError(f"commit {revision[:12]} has no policy.json at its root: {exc}") from exc
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(
            f"policy.json at {revision[:12]} is not valid UTF-8 JSON: {exc}"
        ) from exc
    parsed = _parse_document(raw, origin={
        "source": "approved-reference",
        "ref": ref,
        "commit": revision,
    })
    if parsed.context != PROTECTED:
        parsed = Policy(
            description=parsed.description, context=PROTECTED, required=parsed.required,
            manifest_revision=parsed.manifest_revision,
            origin={**parsed.origin, "claimed_context": raw.get("context", LOCAL)},
        )
    return parsed


def from_request(request: str, project) -> Policy:
    """The `--policy` value: either `@<ref>` or a filesystem path.

    Resolved in one place so a caller cannot accidentally treat a local file as
    an approved reference, and so the two are told apart in the evidence.
    """
    if request.startswith("@"):
        return from_commit(project, request[1:])
    return from_file(project, Path(request).expanduser())


@dataclass(frozen=True)
class PolicyFinding:
    """One difference between the required policy and the candidate's manifest.

    `severity` is load bearing. A missing required check is a REJECT in every
    context; an optional check the candidate altered is a REVIEW at most,
    because the policy never spoke about it and a candidate may have checks the
    policy does not know.
    """

    kind: str
    check_id: str
    severity: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "check_id": self.check_id,
            "severity": self.severity, "detail": self.detail,
        }


def compare(candidate: Manifest, policy: Policy, approved: Manifest | None) -> tuple[PolicyFinding, ...]:
    """Whether the candidate's manifest still meets the required policy.

    `approved` is the manifest the policy pinned, parsed against the same
    checkout. It is the comparison's reference for timeouts, and the reason a
    raised ceiling is a REVIEW rather than a silent pass. It is None only when
    the policy pinned nothing, which the protected context refuses outright.
    """
    findings: list[PolicyFinding] = []
    for required in policy.required:
        try:
            spec = candidate.require(required.id)
        except ManifestError:
            findings.append(PolicyFinding(
                "required_check_absent", required.id, "REJECT",
                f"the trusted policy requires {required.id!r} and the candidate's "
                "manifest does not define it",
            ))
            continue

        lost = [o for o in required.obligations if o not in spec.obligations()]
        if lost:
            findings.append(PolicyFinding(
                "required_scenario_removed", required.id, "REJECT",
                f"the policy requires {', '.join(describe_obligation(o) for o in lost)} "
                "and the candidate's manifest no longer lists them, so the check would "
                "stop reporting them",
            ))

        actual_kind = spec.evidence_kind()
        if required.evidence_kind is not None and required.evidence_kind is not actual_kind:
            findings.append(PolicyFinding(
                "evidence_kind_downgraded", required.id, "REJECT",
                f"the policy requires {required.id!r} as evidence_kind "
                f"{required.evidence_kind.value!r} and the candidate's manifest "
                f"declares it as kind {spec.kind.value!r}, which licenses "
                f"{actual_kind.value!r}. A check cannot claim a category its "
                f"interpreter cannot produce, so the theorem obligation(s) "
                f"{', '.join(describe_obligation(o) for o in required.obligations if isinstance(o, TheoremObligation)) or 'it names'}"
                f" are no longer carried by anything that can discharge them.",
            ))

        if approved is None:
            continue
        try:
            original = approved.require(required.id)
        except ManifestError:
            findings.append(PolicyFinding(
                "approved_check_absent", required.id, "REJECT",
                f"the approved manifest at the policy's pinned revision does not define "
                f"{required.id!r}, so there is no approved definition for this check to run",
            ))
            continue

        if spec.timeout_seconds > original.timeout_seconds:
            findings.append(PolicyFinding(
                "timeout_raised", required.id, "REVIEW",
                f"the approved manifest bounded this check at "
                f"{original.timeout_seconds:g}s and the candidate raised it to "
                f"{spec.timeout_seconds:g}s",
            ))
        original_obligations = original.obligations()
        if original_obligations and spec.obligations() != original_obligations:
            extra = [o for o in spec.obligations() if o not in original_obligations]
            if extra:
                findings.append(PolicyFinding(
                    "scenario_added", required.id, "REVIEW",
                    f"the approved manifest required "
                    f"{', '.join(describe_obligation(o) for o in original_obligations)} "
                    f"and the candidate also demands "
                    f"{', '.join(describe_obligation(o) for o in extra)}",
                ))
    return tuple(findings)


def worst(findings) -> str | None:
    """The highest severity among the findings, or None when there are none."""
    for severity in SEVERITIES:
        if any(f.severity == severity for f in findings):
            return severity
    return None


def load_approved(project, checkout, revision: str, run_dir: Path) -> Manifest:
    """The approved manifest, re-rooted at the candidate checkout.

    The bytes come from `git cat-file` at the pinned commit and are parsed by
    the same parser the ordinary path uses, then re-rooted so every check's
    working directory points into the candidate tree and the post-run source
    identity is computed for the tree that actually ran. Re-rooting preserves
    the approved commands: the candidate's edit to its own manifest never
    reaches anything that executes.
    """
    from ..manifest import parse_manifest_bytes

    try:
        blob = git_blob(project, revision, MANIFEST_RELATIVE)
    except GitError as exc:
        raise PolicyError(
            f"the policy pins {revision[:12]} as the approved manifest revision, but "
            f"{MANIFEST_RELATIVE} does not exist there: {exc}"
        ) from exc
    return parse_manifest_bytes(
        blob, project=open_project(checkout), run_dir=run_dir,
        origin=f"{revision[:12]}:{MANIFEST_RELATIVE}",
    )


def candidate_manifest_bytes(project, commit: str) -> bytes | None:
    """The candidate's manifest as that commit shipped it, or None if it has none."""
    try:
        return git_blob(project, commit, MANIFEST_RELATIVE)
    except GitError:
        return None

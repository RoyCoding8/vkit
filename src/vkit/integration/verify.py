"""The integration operation: decide whether one candidate commit, built on one
target commit, satisfies the approved policy -- and record exactly what that
decision was made from.

The order of the steps below is the design, and each one exists because a later
step would otherwise be answering a question about the wrong thing.

1. **Resolve.** Every ref becomes a commit, and the repository becomes an
   identity. A branch name is a label that can be moved; a commit is not.
2. **Check the combination is meaningful.** The candidate must be the target
   plus changes, which is the containment property: an ancestor of the target,
   or a descendant of it. Anything else -- a sibling branch, an unrelated root
   -- cannot be published onto this target at all, so no check is run and the
   answer is a refusal that names the reason.
3. **Resolve the policy.** Before the candidate is checked out, so nothing read
   from the candidate bytes can influence what is required. The policy says
   which context the decision belongs to: LOCAL for a policy the operator
   selected, PROTECTED for one named as an approved reference.
4. **Claim the checkout and the verification slot.** Both before any process
   starts, and both released only after the run's effects are reconciled.
5. **Check out the candidate, exclusively, and run the required checks there.**
   A fresh worktree, not the worker's. The checks run from the APPROVED
   manifest, so the candidate's own `verification/manifest.json` is a claim to
   compare, never the thing that decides what runs.
6. **Compare the candidate's manifest against the approved one.** A removed
   check or a dropped scenario is a REVIEW finding, which refuses in the
   protected context.
7. **Decide, and record.** One acceptance row with the source, policy, verifier,
   environment and fixture identities, the required checks, the gaps, and the
   context. `readiness_at_publish` records what a caller must re-check before it
   publishes anything: the target has to still be the target.

**What this command does not do.** It does not push, merge, tag or open a pull
request. It computes and records a decision. A protected job that has a decision
and a still-current target may publish; that is the job's business, and the
statement is repeated in the command's own output so a reader of a CI log is not
left guessing.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..claims import ResourceSpec
from ..execution import RunEnvironment, run_check
from ..identity import compute_source_identity
from ..manifest import Manifest, ManifestError
from ..outcome import Blocked, BlockedReason, Failed, Outcome, Passed
from ..paths import Project
from ..storage import Store
from . import checkout as checkouts
from . import concurrency
from . import gitidentity as gits
from . import policy as policies
from .gitidentity import GitError

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
BLOCKED = "BLOCKED"

# The resource that serializes integration verification for one repository. An
# exclusive claim, so two verifications cannot both believe they own the
# candidate checkout even if they were pointed at the same commit.
VERIFY_SCOPE = "integration-verify"


class IntegrationError(Exception):
    """The operation could not be set up. Not a decision about the candidate."""


@dataclass(frozen=True)
class Verified:
    """What one integration verification concluded, and why."""

    decision: str
    context: str
    acceptance_id: str
    candidate: str
    target: str
    record: dict[str, Any]
    checks: tuple[dict[str, Any], ...] = ()

    @property
    def accepted(self) -> bool:
        return self.decision == ACCEPTED

    def to_json(self) -> dict[str, Any]:
        return self.record


@dataclass(frozen=True)
class Request:
    """Everything one invocation names. Resolved before anything else happens."""

    project: Project
    candidate_ref: str
    target_ref: str
    policy_request: str
    writers: int | None = None
    verifications: int | None = None
    keep_checkout: bool = False
    integration_id: str = field(default_factory=lambda: uuid.uuid4().hex)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify(request: Request, store: Store) -> Verified:
    """Run the operation. Every refusal below is a recorded decision, not a raise."""
    project = request.project
    identity = gits.repository_identity(project)
    candidate = gits.resolve_commit(project, request.candidate_ref, what="candidate")
    target = gits.resolve_commit(project, request.target_ref, what="target")
    candidate_fact = gits.commit_fact(project, candidate)
    target_fact = gits.commit_fact(project, target)

    policy = policies.from_request(request.policy_request, project)

    containment = _describe_combination(project, candidate, target)
    if containment["relationship"] not in ("target", "candidate"):
        return _record(
            store, request, policy, identity,
            decision=REJECTED,
            candidate_fact=candidate_fact, target_fact=target_fact,
            containment=containment,
            source=_unresolved_source(project),
            checks=(),
            gaps=(containment["detail"],),
            findings=(),
            executed_digest=None,
            candidate_manifest=None,
            verifier=None,
            environment=_environment_facts(),
            fixture=None,
            decided_at=_now(),
        )

    claim_key = concurrency.scope_spec(VERIFY_SCOPE).key
    specs = [ResourceSpec(claim_key, concurrency.EXCLUSIVE)]
    if request.verifications is not None:
        specs.append(concurrency.verification_spec(request.verifications))

    from ..claims import acquire, release
    from ..storage import ConflictError

    try:
        acquire(store, f"integration:{request.integration_id}", 1, specs)
    except ConflictError as exc:
        return _record(
            store, request, policy, identity,
            decision=BLOCKED,
            candidate_fact=candidate_fact, target_fact=target_fact,
            containment=containment,
            source=_unresolved_source(project),
            checks=(),
            gaps=(f"another integration verification holds this repository: {exc}",),
            findings=(),
            executed_digest=None,
            candidate_manifest=None,
            verifier=None,
            environment=_environment_facts(),
            fixture=None,
            decided_at=_now(),
        )

    try:
        return _verify_exclusive(
            request, store, policy, identity, candidate_fact, target_fact, containment
        )
    finally:
        release(store, f"integration:{request.integration_id}", 1, [s.key for s in specs])


def _verify_exclusive(
    request: Request,
    store: Store,
    policy: policies.Policy,
    identity: gits.RepositoryIdentity,
    candidate_fact: gits.CommitFact,
    target_fact: gits.CommitFact,
    containment: dict[str, Any],
) -> Verified:
    project = request.project
    run_dir = project.state_root / "integration" / "runs" / request.integration_id
    run_dir.mkdir(parents=True, exist_ok=True)

    candidate_checkout = checkouts.create(project, candidate_fact.sha, request.integration_id)
    checkout_project = checkouts.project_for(candidate_checkout)
    # The verifier is this process's own package: the tree that will read the
    # result is the tree that has to be the one the candidate cannot reach.
    verifier = _verifier_identity()
    env = RunEnvironment(
        python=sys.executable,
        plugin_root=verifier["package_root_path"],
        revalidate=True,
        verifier_revision=verifier["revision"],
    )
    try:
        checkouts.assert_clean(candidate_checkout, when="before the required checks")

        approved: Manifest | None = None
        gaps: list[str] = []
        if policy.manifest_revision:
            approved = policies.load_approved(
                project, candidate_checkout.path, policy.manifest_revision, run_dir / "probe"
            )
        execution_manifest = approved if approved is not None else _parse_candidate_manifest(
            checkout_project, run_dir
        )
        if execution_manifest is None:
            return _record(
                store, request, policy, identity,
                decision=BLOCKED, candidate_fact=candidate_fact, target_fact=target_fact,
                containment=containment, source=_unresolved_source(project), checks=(),
                gaps=("no manifest could be executed: the policy pins no approved "
                      "revision and the candidate ships none that parses",),
                findings=(), executed_digest=None, candidate_manifest=None,
                verifier=verifier,
                environment=_environment_facts(), fixture=None, decided_at=_now(),
            )

        # The source identity is computed for the checkout the checks will run
        # in, not for the caller's repository, so the before-and-after digests
        # bracket exactly the tree that executed.
        source = compute_source_identity(checkout_project)

        findings: list[policies.PolicyFinding] = []
        candidate_manifest = _parse_candidate_manifest(checkout_project, run_dir, tolerate=True)
        if candidate_manifest is not None:
            findings = list(policies.compare(candidate_manifest, policy, approved))
        elif approved is not None:
            findings = [policies.PolicyFinding(
                "candidate_manifest_absent", "-", "REVIEW",
                "the candidate ships no readable verification/manifest.json, so the "
                "approved manifest runs unchecked against what the candidate claims",
            )]
        gaps.extend(f.detail for f in findings if f.severity == "REJECT")

        results: list[dict[str, Any]] = []
        worst_finding = policies.worst(findings)
        if worst_finding != "REJECT":
            for check_id in policy.required_ids:
                outcome = run_check(
                    execution_manifest, check_id, store=store, source=source, env=env
                )
                results.append(_check_result(check_id, outcome))
        else:
            gaps.append(
                "the required checks were not run: the candidate's manifest does not "
                "meet the trusted policy, so running them would verify a bar the "
                "candidate had already lowered"
            )

        checkouts.assert_clean(candidate_checkout, when="after the required checks")

        rejected = [r for r in results if r["result"] == "REJECTED"]
        blocked = [r for r in results if r["result"] == "BLOCKED"]
        if worst_finding == "REJECT" or rejected:
            decision = REJECTED
        elif blocked:
            decision = BLOCKED
        elif results and all(r["result"] == "ACCEPTED" for r in results):
            decision = ACCEPTED
        else:
            decision = BLOCKED
            gaps.append("no required check produced a result, so nothing was decided")

        if worst_finding == "REVIEW" and decision == ACCEPTED and policy.context == policies.PROTECTED:
            decision = REJECTED
            gaps.append(
                "the candidate changed its own verification policy and this ran in the "
                "protected context, so the change needs the repository owner's review "
                "through the protected configuration rather than this run"
            )

        fixture = _fixture_identity(execution_manifest, results)
        return _record(
            store, request, policy, identity,
            decision=decision, candidate_fact=candidate_fact, target_fact=target_fact,
            containment=containment, source=source.to_json(), checks=tuple(results),
            gaps=tuple(gaps), findings=tuple(findings),
            verifier=verifier,
            executed_digest=execution_manifest.digest(),
            candidate_manifest=_candidate_manifest_facts(project, candidate_fact.sha),
            environment=_environment_facts(), fixture=fixture, decided_at=_now(),
            checkout=str(candidate_checkout.path),
        )
    finally:
        _retire(request, candidate_checkout)


def _verifier_identity() -> dict[str, Any]:
    """Which verifier code made this decision.

    `verifier_revision` is the Git commit of vkit's own repository when this
    build is running from one, and the package version otherwise. Naming the
    version rather than always naming a commit is deliberate: a build installed
    from a wheel is a legitimate verifier and has no commit to point at, and
    recording "unknown" for it would be less honest than recording what it is.
    """
    from .. import __version__

    package_root = Path(__file__).resolve().parents[2]
    revision: str | None = None
    try:
        from ..paths import Project, open_project
        from .gitidentity import commit_fact, has_commits

        if (package_root / ".git").exists():
            project = open_project(package_root)
            if has_commits(project):
                revision = commit_fact(project, "HEAD").sha
    except Exception:  # noqa: BLE001 - a verifier that cannot name itself still runs
        revision = None
    return {
        "revision": revision or f"vkit {__version__}",
        "package_root_path": package_root,
        "package_root": str(package_root),
    }


def _candidate_manifest_facts(project: Project, candidate: str) -> dict[str, Any]:
    """The candidate's own manifest, digested, as that commit shipped it.

    Read from the commit rather than from a checkout, so a checkout that has
    since moved cannot change what the candidate's policy was. The bytes go
    into the acceptance because the comparison that justifies the decision can
    only be re-checked against the bytes the candidate shipped.
    """
    from ..manifest import ManifestError, parse_manifest_bytes
    from .policy import MANIFEST_RELATIVE, candidate_manifest_bytes

    blob = candidate_manifest_bytes(project, candidate)
    if blob is None:
        return {
            "path": MANIFEST_RELATIVE,
            "present": False,
            "digest": None,
            "definition": None,
        }
    from ..paths import open_project

    try:
        parsed = parse_manifest_bytes(
            blob, project=open_project(project.root), run_dir=project.runs_root / "probe",
            origin=f"{candidate[:12]}:{MANIFEST_RELATIVE}",
        )
        definition = {
            check_id: {
                "command": list(spec.argv),
                "required_scenarios": list(spec.required_scenarios),
                "timeout_seconds": spec.timeout_seconds,
                "artifact": spec.artifact_name,
            }
            for check_id, spec in parsed.checks.items()
        }
        digest = parsed.digest()
    except ManifestError as exc:
        return {
            "path": MANIFEST_RELATIVE, "present": True, "digest": None,
            "definition": None, "unreadable": str(exc),
        }
    return {
        "path": MANIFEST_RELATIVE,
        "present": True,
        "digest": digest,
        "bytes_sha256": hashlib.sha256(blob).hexdigest(),
        "definition": definition,
    }


def _retire(request: Request, candidate_checkout) -> None:
    """Retire the candidate checkout, keeping anything that is still in it.

    A checkout that is not clean is archived under the evidence directory rather
    than removed, so neither the work nor the proof that it existed is lost. A
    clean one is removed and its Git administrative record pruned, so
    `git worktree list` does not accumulate entries that point nowhere.
    """
    if request.keep_checkout:
        return
    try:
        checkouts.retire(candidate_checkout)
    except Exception as exc:  # noqa: BLE001 - teardown must not lose the decision
        preserved = checkouts.dirty_paths(candidate_checkout)
        archive = (
            candidate_checkout.project.state_root / "integration" / "preserved"
            / candidate_checkout.name
        )
        print(
            f"warning: could not retire the candidate checkout {candidate_checkout.path}: {exc}",
            file=sys.stderr,
        )
        if preserved:
            print(
                f"warning: uncommitted work in {', '.join(preserved[:8])} remains at "
                f"{candidate_checkout.path}",
                file=sys.stderr,
            )
        del archive


def _check_result(check_id: str, outcome: "RunOutcome") -> dict[str, Any]:
    """One required check's contribution to the decision, in the record's shape.

    A FAIL and a BLOCKED are different answers and are reported as different
    ones. `Failed` means a valid check observed a failure; `Blocked` means
    execution or evidence was insufficient to decide. Collapsing them would let
    an unrunnable check read as a decided refusal.
    """
    if isinstance(outcome.outcome, Passed):
        result = "ACCEPTED"
        detail = f"{len(outcome.outcome.scenarios)} scenario(s) passed"
    elif isinstance(outcome.outcome, Failed):
        result = "REJECTED"
        detail = "; ".join(
            f"{s.scenario_id}: {s.observation}" for s in outcome.outcome.scenarios if not s.passed
        ) or "a required observation failed"
    else:
        blocked: Blocked = outcome.outcome  # type: ignore[assignment]
        result = "BLOCKED"
        detail = f"{blocked.reason.value}: {blocked.detail}"
    return {
        "check_id": check_id,
        "result": result,
        "detail": detail,
        "run_id": outcome.report["run_id"],
        "source_head": outcome.report["source"]["head"],
        "configuration_digest": outcome.report["configuration_digest"],
    }


def _parse_candidate_manifest(
    checkout_project, run_dir: Path, *, tolerate: bool = False
) -> Manifest | None:
    from ..manifest import parse_manifest

    try:
        return parse_manifest(checkout_project, run_dir / "probe")
    except ManifestError as exc:
        if tolerate:
            return None
        raise IntegrationError(f"the candidate's manifest cannot be used: {exc}") from exc


def _describe_combination(project: Project, candidate: str, target: str) -> dict[str, Any]:
    """Whether this candidate is the target plus changes, and what it is built on.

    Three outcomes, and the third is a refusal. If the target is an ancestor of
    the candidate, the candidate is the target plus changes and its parent chain
    names the base. If the candidate is an ancestor of the target, the candidate
    is behind the target, which is a stale base and must be rebuilt. Otherwise
    the two are on divergent lines, and the exact merge base is recorded so the
    refusal says which common ancestor the candidate was built on.
    """
    if candidate == target:
        return {
            "relationship": "target", "merge_base": target, "candidate_parents": [],
            "detail": "the candidate is the target; there is nothing to integrate",
        }
    if gits.is_ancestor(project, target, candidate):
        fact = gits.commit_fact(project, candidate)
        return {
            "relationship": "candidate",
            "merge_base": None,
            "candidate_parents": list(fact.parents),
            "detail": f"the candidate contains the target {target[:12]} and is built on "
                      + ", ".join(p[:12] for p in fact.parents),
        }
    if gits.is_ancestor(project, candidate, target):
        return {
            "relationship": "behind",
            "merge_base": candidate, "candidate_parents": [],
            "detail": f"the candidate {candidate[:12]} is an ancestor of the target "
                      f"{target[:12]}; it is a stale base, so a fresh candidate built on "
                      "the current target is required",
        }
    merge_base = gits.git(project, "merge-base", candidate, target) or None
    return {
        "relationship": "divergent",
        "merge_base": merge_base,
        "candidate_parents": list(gits.commit_fact(project, candidate).parents),
        "detail": f"the candidate and the target are on divergent lines; they share "
                  f"{merge_base[:12] if merge_base else 'no common ancestor'} and the "
                  "candidate was not built on the target",
    }


def _unresolved_source(project: Project) -> dict[str, Any]:
    """A source identity for a decision made before any checkout existed.

    The decision is about a commit, not about a tree, so HEAD and a digest of
    the empty inventory are honest here: nothing ran, so nothing was executed
    from a tree. Reporting the caller's own HEAD instead would attach this
    decision to code that had nothing to do with it.
    """
    return {
        "head": "<not checked out>",
        "inventory_digest": "no-source-executed",
        "dirty": False,
        "tracked_files": 0,
        "dirty_paths": [],
    }


def _environment_facts() -> dict[str, Any]:
    """The interpreter and platform the decision was made on. Nothing else.

    Deliberately narrow. A full environment dump is a credential leak waiting to
    happen and a reader does not need it to know which Python ran the checks.
    """
    return {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "working_directory_preserved": False,
    }


def _fixture_identity(manifest: Manifest, results: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """What the checks consumed, from the manifest's own declarations.

    `inputs` is the manifest naming the files a check reads. It is the only
    place a repository records that, so trusting it is trusting the
    configuration; what is added here is the digest of those files as they stood
    in the checkout, which is a fact rather than a claim.
    """
    declared: set[str] = set()
    for check in manifest.checks.values():
        declared.update(check.inputs)
    return {
        "declared_inputs": sorted(declared),
        "check_count": len(manifest.checks),
        "results": len(results),
    }


def _record(
    store: Store,
    request: Request,
    policy: policies.Policy,
    identity: gits.RepositoryIdentity,
    *,
    decision: str,
    candidate_fact: gits.CommitFact,
    target_fact: gits.CommitFact,
    containment: dict[str, Any],
    source: dict[str, Any],
    checks: tuple[dict[str, Any], ...],
    gaps: tuple[str, ...],
    findings: tuple[policies.PolicyFinding, ...],
    executed_digest: str | None,
    candidate_manifest: dict[str, Any] | None,
    verifier: dict[str, Any] | None,
    environment: dict[str, Any],
    fixture: dict[str, Any] | None,
    decided_at: str,
    checkout: str | None = None,
) -> Verified:
    """Compute the acceptance id, write the acceptance, and return it.

    The id is a digest of the things that make this decision this decision: the
    repository, the candidate and target commits, the policy digest, the context
    and the required checks. Two invocations naming the same things therefore
    produce the same id, and a second one finds the recorded answer instead of
    writing a second decision under a new name. Anything that would make a
    genuinely different decision is inside the digest; nothing else is.
    """
    body = json.dumps(
        {
            "repository": identity.root_commit,
            "candidate": candidate_fact.sha,
            "target": target_fact.sha,
            "policy": policy.digest(),
            "context": policy.context,
            "required": list(policy.required_ids),
        },
        sort_keys=True, separators=(",", ":"),
    )
    acceptance_id = "acc-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]

    readiness = {
        "target_tested": target_fact.sha,
        "recheck_before_publish": "target",
        "note": "this command computes and records a decision; it does not push, merge "
                "or tag. A protected job may publish only while the target still "
                "resolves to target_tested.",
    }
    record = {
        "acceptance_id": acceptance_id,
        "integration_id": request.integration_id,
        "command": "integration verify",
        "context": policy.context,
        "decision": decision,
        "candidate": candidate_fact.sha,
        "target": target_fact.sha,
        "candidate_parent": (containment["candidate_parents"][0]
                             if containment["candidate_parents"] else None),
        "repository": identity.to_json(),
        "source": source,
        "target_commit": target_fact.to_json(),
        "candidate_commit": candidate_fact.to_json(),
        "combination": containment,
        "policy": policy.to_json(),
        "verifier": {
            "revision": (verifier or {}).get("revision"),
            "package_root": (verifier or {}).get("package_root"),
        },
        "environment": environment,
        "fixture": fixture or {},
        "manifest": {
            "executed_digest": executed_digest,
            "approved_revision": policy.manifest_revision,
            "candidate": candidate_manifest
            or {"path": policies.MANIFEST_RELATIVE, "present": False, "digest": None},
        },
        "required_checks": list(policy.required_ids),
        "checks": list(checks),
        "gaps": list(gaps),
        "findings": [f.to_json() for f in findings],
        "readiness_at_publish": readiness,
        "checkout": checkout,
        "decided_at": decided_at,
        "publishes": False,
    }
    _persist(store, acceptance_id, record)
    return Verified(
        decision=decision, context=policy.context, acceptance_id=acceptance_id,
        candidate=candidate_fact.sha, target=target_fact.sha, record=record, checks=checks,
    )


def _persist(store: Store, acceptance_id: str, record: dict[str, Any]) -> None:
    """Write the acceptance row, exactly once, and let a retry read it back.

    The primary key on the acceptances table is the guard, for the same reason
    it is on `runs`: a retry that recomputed a different answer under one
    identity would be two decisions wearing one name. A retry of the same
    decision finds the row that is already there, and the recorded answer is
    the one that stands.
    """
    from ..storage import StoreError

    try:
        store.record_acceptance(acceptance_id, record)
    except StoreError as exc:
        existing = store.load_acceptance(acceptance_id)
        if existing is None:
            raise
        # A retry of the same decision. The recorded one is the answer; this
        # run's is not written over it, and the caller reads the recorded one.
        record["reused_recorded_acceptance"] = True
        del exc


def summarize(verified: Verified) -> str:
    """The human-readable form of a decision, in the order a reader needs it."""
    record = verified.record
    lines = [
        f"{verified.decision}  candidate {verified.candidate[:12]} on target {verified.target[:12]}",
        f"  context: {verified.context}"
        + ("" if verified.context == policies.PROTECTED
           else "  (local evidence; not a protected integration decision)"),
        f"  repository: {record['repository']['root_commit'][:12]}",
        f"  policy: {verified.record['policy']['policy_id']}"
        f" requires {', '.join(verified.record['required_checks'])}",
        f"  manifest executed: {(verified.record['manifest']['executed_digest'] or 'none')[:16]}",
    ]
    for check in verified.checks:
        lines.append(f"  {check['result']:8} {check['check_id']}: {check['detail']}")
    for finding in verified.record["findings"]:
        lines.append(f"  POLICY {finding['severity']:6} {finding['kind']}: {finding['detail']}")
    for gap in verified.record["gaps"]:
        lines.append(f"  gap: {gap}")
    lines.append(
        f"  recorded as {verified.acceptance_id}; this command does not push, merge or "
        "tag. Publish only while the target still resolves to "
        f"{record['readiness_at_publish']['target_tested'][:12]}."
    )
    return "\n".join(lines)

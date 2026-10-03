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
7. **Hold the verification code to the approved revision too.** An approved
   manifest naming a script path does not make that script trusted: the path
   resolves inside the candidate checkout, so the candidate supplies the bytes
   that decide whether it passes. The script an approved check names is measured
   at both the approved revision and the candidate, and any difference refuses
   before a process starts. When they agree, the approved bytes are what execute
   and the working directory stays in the candidate, so approved verification
   code observes candidate product bytes. See `oracle.py`.
8. **Decide, and record.** One acceptance row with the source, policy, verifier,
   environment and fixture identities, the required checks, the gaps, and the
   context. `readiness_at_publish` records what a caller must re-check before it
   publishes anything: the target has to still be the target. A replay of a
   recorded request returns the recorded answer; a fresh execution that reaches a
   different answer under the same identity is a conflict, keeps both
   observations, and answers BLOCKED rather than picking one.

**What this command does not do.** It does not push, merge, tag or open a pull
request. It computes and records a decision. A protected job that has a decision
and a still-current target may publish; that is the job's business, and the
statement is repeated in the command's own output so a reader of a CI log is not
left guessing.
"""
from __future__ import annotations

import hashlib
import json
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
from ..outcome import Blocked, Failed, Passed
from ..paths import Project
from ..storage import Store
from . import checkout as checkouts
from . import concurrency
from . import gitidentity as gits
from . import oracle as oracles
from . import policy as policies
from .policy import MANIFEST_RELATIVE

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
            environment=oracles.environment_identity(),
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
            environment=oracles.environment_identity(),
            fixture=None,
            decided_at=_now(),
        )

    try:
        return _verify_checked_out(
            request, store, policy, identity, candidate_fact, target_fact, containment
        )
    finally:
        release(store, f"integration:{request.integration_id}", 1, [s.key for s in specs])


def _verify_checked_out(
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

    # The verifier is this process's own package, measured rather than named, and
    # the environment is the interpreter that will execute the checks. Both are
    # measured before the candidate is checked out, so nothing the candidate ships
    # can influence what is being measured.
    verifier = oracles.package_identity()
    env = RunEnvironment(
        python=sys.executable,
        plugin_root=Path(verifier["package_root"]),
        revalidate=True,
        verifier_revision=verifier["revision"],
    )
    environment = oracles.environment_identity()

    candidate_checkout = checkouts.create(project, candidate_fact.sha, request.integration_id)
    checkout_project = checkouts.project_for(candidate_checkout)
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
                verifier=verifier, environment=environment, fixture=None,
                decided_at=_now(), context_facts=None,
            )

        # The trusted side of the verification, measured at both commits before
        # anything runs. The check names a script by repository-relative path;
        # the approved revision supplies those bytes and the candidate supplies
        # the product. Anything that is not equal is a refusal, and the
        # comparison is the whole of the oracle boundary.
        measured = oracles.approved_oracle(
            project, candidate_fact.sha, execution_manifest, policy.manifest_revision
        )
        scripts = oracles.scripts_of(execution_manifest)
        expectations = oracles.expectations_of(execution_manifest)
        unpinned = sorted(check_id for check_id in policy.required_ids if check_id not in scripts)
        oracle_findings: list[policies.PolicyFinding] = []
        if unpinned:
            oracle_findings.append(policies.PolicyFinding(
                "checker_not_identifiable", ", ".join(unpinned), "REJECT",
                f"the approved command for {', '.join(unpinned)} names no repository-relative "
                "script, so there are no trusted verification bytes this run can pin and "
                "execute; review the command and approve one that names a file",
            ))
        unresolved = sorted(
            check_id for check_id in policy.required_ids
            if (execution_manifest.checks.get(check_id) is not None
                and execution_manifest.checks[check_id].expectations is None)
        )
        if policy.context == policies.PROTECTED and unresolved:
            oracle_findings.append(policies.PolicyFinding(
                "expectation_boundary_unresolved", ", ".join(unresolved), "REJECT",
                "the approved check does not say whether expected observations are embedded "
                "in its driver or supplied by files; declare expectations: [] for embedded "
                "values, or name each approved expectation file",
            ))
        if policy.context == policies.PROTECTED and not policy.manifest_revision:
            oracle_findings.append(policies.PolicyFinding(
                "expectation_oracle_unpinned", ", ".join(policy.required_ids), "REJECT",
                "protected verification needs a pinned manifest revision to establish the "
                "approved driver and expectation declaration",
            ))

        # The candidate is not dirty relative to its own index. It is dirty
        # relative to its TARGET, which is the only comparison a checkout this
        # clean can answer and the one an operator actually means by "does this
        # candidate still owe cleanup". Scoped by the diff and previewed over
        # every line, because a candidate commit is not a working tree with a
        # task's changed-lines record behind it.
        owed = _required_cleanup(
            project, candidate_fact.sha, target_fact.sha, checkout_project
        )
        for path, rule_id in owed:
            oracle_findings.append(policies.PolicyFinding(
                "required_cleanup_pending", f"{path}:{rule_id}", "REJECT",
                f"{path} still owes cleanup the approved policy requires: {rule_id}. "
                "A protected check does not clean the candidate and then attest to "
                "its original commit, so this cannot be repaired by this run. Apply "
                f"the cleanup to {path} and make a new candidate commit",
            ))

        expected_paths = expectations
        executed_expectations = oracles.expectation_identity(
            candidate_checkout.path, expected_paths
        )
        missing_expectations = sorted(
            path for path in expected_paths
            if measured.approved.get(path) is None
            or measured.candidate.get(path) is None
            or executed_expectations.get(path) is None
        )
        if missing_expectations:
            oracle_findings.append(policies.PolicyFinding(
                "expectation_bytes_missing", ", ".join(missing_expectations), "REJECT",
                "an approved expectation file is absent from the approved or candidate "
                "commit or owned checkout, so its executed bytes cannot be established",
            ))
        changed = measured.changed
        changed_expectations = {path: pair for path, pair in changed.items()
                                if path in expected_paths}
        if changed_expectations:
            oracle_findings.append(policies.PolicyFinding(
                "expectation_bytes_changed", ", ".join(sorted(changed_expectations)), "REJECT",
                "the candidate changed expected observations from the approved revision "
                + (f"{measured.approved_revision[:12]} " if measured.approved_revision else "")
                + "the policy pins; changing what a check expects requires an explicit "
                "policy review that pins the new expectation bytes",
            ))
        script_paths = set(scripts.values())
        changed_checkers = {path: pair for path, pair in changed.items()
                            if path in script_paths}
        if changed_checkers:
            oracle_findings.append(policies.PolicyFinding(
                "checker_bytes_changed", ", ".join(sorted(changed_checkers)), "REJECT",
                "the candidate's verification code differs from the approved revision "
                + (f"{measured.approved_revision[:12]} " if measured.approved_revision else "")
                + "the policy pins, in "
                + ", ".join(sorted(changed_checkers))
                + ". A candidate that edits the code that decides whether it passes has "
                "changed the oracle rather than the product. The way to change what a check "
                "requires is an explicit policy review that establishes the new trusted bytes, "
                "not a candidate commit.",
            ))

        # The source identity is computed for the checkout the checks will run
        # in, not for the caller's repository, so the before-and-after digests
        # bracket exactly the tree that executed.
        source = compute_source_identity(checkout_project)

        findings: list[policies.PolicyFinding] = list(oracle_findings)
        manifest_blob, candidate_manifest, unreadable = _read_candidate_manifest(
            project, candidate_fact.sha, run_dir
        )
        if candidate_manifest is not None:
            findings.extend(policies.compare(candidate_manifest, policy, approved))
        elif unreadable is not None:
            # The candidate's manifest is there and the product will not accept
            # it. Saying "absent" here would be a different, weaker claim, and
            # it would hide the reason a reviewer needs: a manifest with no
            # checks is the shape a candidate uses to declare a zero-width bar.
            findings.append(policies.PolicyFinding(
                "candidate_manifest_rejected", "-", "REJECT",
                f"the candidate's {MANIFEST_RELATIVE} is not usable, so what "
                f"it requires cannot be established: {unreadable}",
            ))
        else:
            findings.append(policies.PolicyFinding(
                "candidate_manifest_absent", "-", "REVIEW",
                "the candidate ships no verification/manifest.json, so the approved "
                "manifest runs unchecked against what the candidate claims",
            ))
        gaps.extend(f.detail for f in findings if f.severity == "REJECT")

        results: list[dict[str, Any]] = []
        worst_finding = policies.worst(findings)
        if worst_finding != "REJECT":
            if measured.approved_revision and scripts:
                approved_root = checkouts.materialize_approved(
                    project, measured.approved_revision, scripts, run_dir
                )
                execution_manifest = oracles.repoint_approved(
                    execution_manifest, approved_root, scripts
                )
            for check_id in policy.required_ids:
                outcome = run_check(
                    execution_manifest, check_id, store=store, source=source, env=env
                )
                results.append(_check_result(check_id, outcome))
        else:
            gaps.append(
                "the required checks were not run: the trusted verification code and the "
                "approved policy do not both hold for this candidate, so running them "
                "would verify a bar the candidate had already lowered"
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

        fixture = oracles.fixture_identity(
            project, candidate_fact.sha, execution_manifest, scripts, expected_paths,
            executed_expectations,
        )
        return _record(
            store, request, policy, identity,
            decision=decision, candidate_fact=candidate_fact, target_fact=target_fact,
            containment=containment, source=source.to_json(), checks=tuple(results),
            gaps=tuple(gaps), findings=tuple(findings),
            verifier=verifier,
            executed_digest=execution_manifest.digest(),
            candidate_manifest=_candidate_manifest_facts(
                manifest_blob, candidate_manifest, unreadable
            ),
            environment=environment, fixture=fixture, decided_at=_now(),
            checkout=str(candidate_checkout.path),
            context_facts=measured.to_json(),
        )
    finally:
        _retire(request, candidate_checkout)


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


def _read_candidate_manifest(
    project: Project, candidate: str, run_dir: Path
) -> tuple[bytes | None, Manifest | None, str | None]:
    """The candidate's own manifest as that commit shipped it.

    One read, because the comparison and the receipt are about the same bytes
    and two reads of a blob that cannot change is one more thing to keep in
    step. The result is `(bytes, parsed, why it was rejected)`: the bytes for the
    digest the receipt carries, the parse for the comparison, and the reason when
    the candidate ships something the product will not accept.

    Read from the commit rather than from the checkout. The checkout has already
    been verified clean, so the two agree today, but a decision that depends on
    which one it read is a decision that a later checkout could change, and the
    commit is the thing the decision is about.
    """
    from ..manifest import ManifestError, parse_manifest_bytes
    from ..paths import open_project
    from .policy import candidate_manifest_bytes

    blob = candidate_manifest_bytes(project, candidate)
    if blob is None:
        return None, None, None
    try:
        return blob, parse_manifest_bytes(
            blob, project=open_project(project.root), run_dir=run_dir / "probe",
            origin=f"{candidate[:12]}:{MANIFEST_RELATIVE}",
        ), None
    except ManifestError as exc:
        return blob, None, str(exc)


def _candidate_manifest_facts(
    blob: bytes | None, parsed: Manifest | None, unreadable: str | None
) -> dict[str, Any]:
    """The candidate's manifest as the acceptance records it.

    The bytes go in because the comparison that justifies the decision can only
    be re-checked against the bytes the candidate shipped. A manifest the product
    refuses is recorded as unreadable rather than absent, because "absent" is a
    weaker claim and would hide the reason a reviewer needs.
    """
    if blob is None:
        return {"path": MANIFEST_RELATIVE, "present": False, "digest": None}
    facts: dict[str, Any] = {
        "path": MANIFEST_RELATIVE, "present": True, "digest": None,
        "bytes_sha256": hashlib.sha256(blob).hexdigest(),
    }
    if unreadable is not None:
        return {**facts, "unreadable": unreadable}
    assert parsed is not None, "a blob that parsed cannot carry no parse"
    return {
        **facts,
        "digest": parsed.digest(),
        "definition": {
            check_id: {
                "command": list(spec.argv),
                "required_scenarios": list(spec.required_scenarios),
                "timeout_seconds": spec.timeout_seconds,
                "artifact": spec.artifact_name,
            }
            for check_id, spec in parsed.checks.items()
        },
    }


def _parse_candidate_manifest(checkout_project, run_dir: Path) -> Manifest | None:
    from ..manifest import parse_manifest

    try:
        return parse_manifest(checkout_project, run_dir / "probe")
    except ManifestError as exc:
        raise IntegrationError(f"the candidate's manifest cannot be used: {exc}") from exc


def _required_cleanup(
    project: Project, candidate: str, target: str, checkout_project
) -> tuple[tuple[str, str], ...]:
    """The cleanup this candidate still owes, as `(path, rule_id)` pairs.

    **Why the scope is the diff against the target and not the checkout's own
    changed set.** `cleanup.candidate_gaps` is `cleanup.freshness` under a name,
    and `freshness` reads `changed_files(checkout)`, which is
    `git status --porcelain`. This module calls `checkouts.assert_clean` twice,
    and `assert_clean` raises unless that status is empty. So at the instant the
    gap would be read, the checkout is clean by assertion and the changed set is
    empty: `candidate_gaps(checkout)` returns () for every candidate, and
    wiring it in beside `assert_clean` would refuse nothing.

    What is actually wrong with a candidate is not that its checkout is dirty. It
    is that its commit carries cleanup the owner approved and the commit has not
    applied. The set of paths the candidate changed against the target is the only
    question a clean checkout can answer that means that, and it is the question
    an operator is asking anyway.

    **Every line is a changed line here.** `candidate_gaps` scopes the comment
    rule to the lines a task changed, because it reads a worker's diff and a
    worker's change has an owner. A candidate commit has no such record: the
    whole candidate is under review, and its trailing comments are its own.

    **Nothing is written.** `_propose` reaches `preview_comment_cleanup` and
    `preview_logic_cleanup`, and both propose and compare. `apply_cleanup` is the
    single writer in the cleanup package and is not called from here; the policy is
    demoted with `_read_only` so the guarantee is a property of the code rather
    than a promise by this caller, which is the same device `freshness` uses.

    `_propose` and `_sites` are imported rather than restated. They are the two
    functions that decide whether a rule has work on a file, and a gate holding a
    third copy of that decision is how two gates come to disagree about the same
    file. Only the scope differs from `freshness`, and the scope is the whole
    reason this function exists. `integration/sandbox.py` and
    `verifiers/dispatch.py` already reach into a sibling module's private
    helper for the same reason.

    A path the checker cannot read, a malformed file, an excluded path and a
    language this build does not apply are all refusals rather than pending work,
    and none of them is a gap: the checker decided there was nothing safe to
    write, so nothing is required.
    """
    from ..cleanup import hooks as cleanup

    active = cleanup._read_only(cleanup.load_policy(checkout_project))
    enabled = [rule for rule in cleanup.RULE_ORDER if rule in active.enabled_rules]
    if not enabled:
        return ()

    return tuple(
        (path, rule_id)
        for path in _candidate_scope(project, candidate, target)
        if active.rules_for(path)
        if (checkout_project.root / path).is_file()
        for rule_id in enabled
        if _has_work(cleanup, checkout_project, path, rule_id)
    )


def _has_work(cleanup, checkout_project, path: str, rule_id: str) -> bool:
    """Whether one rule offers an edit on one path, read through the cleanup package.

    Every line counts as changed here, and the comment preview takes a line
    iterable rather than a sentinel, so `all_lines` expands it once against the
    bytes the preview is about to read.
    """
    if rule_id == cleanup.TRAILING_RULE:
        lines: Any = cleanup.all_lines((checkout_project.root / path).read_bytes())
    else:
        # A logic rule reads the whole file regardless of which lines changed, so
        # it needs no line evidence and must not be handed any.
        lines = ()
    return bool(cleanup._sites(cleanup._propose(checkout_project, path, rule_id, lines)))


def _candidate_scope(project: Project, candidate: str, target: str) -> tuple[str, ...]:
    """Every path the candidate changed against the target, repository-relative.

    `--name-only` rather than a diff parse, so this is Git's own answer to "what
    did this candidate change" and not a reading of a patch. Both endpoints are
    commits, never refs, so the scope cannot move under a decision that is being
    recorded against them.

    A rename reports the path that exists in the tree, which is what a reader
    needs to act on and what the cleanup preview can read. `--diff-filter=ACMR`
    drops deletions: a deleted file is nothing left to clean.

    A path listed here that the candidate tree does not hold is skipped by the
    caller before any read. That is a Git answer about two commits and a
    filesystem answer about the checkout, and the second is the one that decides
    whether there is a file to preview. A subdirectory path cannot appear here,
    because `--name-only` lists blobs.
    """
    try:
        listing = gits.git(
            project, "diff", "--name-only", "--diff-filter=ACMR",
            f"{target}..{candidate}",
        )
    except gits.GitError:
        return ()
    return tuple(dict.fromkeys(line.strip() for line in listing.splitlines() if line.strip()))


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
    context_facts: dict[str, Any] | None = None,
) -> Verified:
    """Compute the acceptance id, write the acceptance, and return it.

    The id is a digest of the things that make this decision this decision: the
    repository, the candidate and target commits, the policy, the context, the
    required checks, and the measured verification context -- the verifier's own
    bytes, the interpreter, and the fixture digests the checks were given. The
    operative context is inside the id because two runs that differ only in which
    bytes decided are not the same decision, and a receipt that claimed they were
    would make a later replay return an answer computed by different code.

    Two invocations naming the same things therefore produce the same id, and a
    second one finds the recorded answer instead of writing a second decision
    under a new name.
    """
    body = json.dumps(
        {
            "repository": identity.root_commit,
            "candidate": candidate_fact.sha,
            "target": target_fact.sha,
            "policy": policy.digest(),
            "context": policy.context,
            "required": list(policy.required_ids),
            "verifier": (verifier or {}).get("package_digest"),
            "environment": {k: environment[k] for k in sorted(environment)},
            "fixture": (fixture or {}).get("digest"),
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
        # The verifier, the measured verification context and the publish
        # readiness are carried inside columns the acceptances table persists, so
        # a replayed decision returns a record that still states all of them.
        "verifier": {**(verifier or {}), "verification_context": context_facts or {}},
        "environment": environment,
        "fixture": fixture or {},
        "manifest": {
            "executed_digest": executed_digest,
            "approved_revision": policy.manifest_revision,
            "candidate": candidate_manifest
            or {"path": MANIFEST_RELATIVE, "present": False, "digest": None},
            "readiness_at_publish": readiness,
            "publishes": False,
        },
        "required_checks": list(policy.required_ids),
        "checks": list(checks),
        "gaps": list(gaps),
        "findings": [f.to_json() for f in findings],
        "checkout": checkout,
        "decided_at": decided_at,
    }
    return _settle(store, acceptance_id, record)


def _settle(store: Store, acceptance_id: str, record: dict[str, Any]) -> Verified:
    """Record the decision, and answer with the one that is authoritative.

    Three outcomes, and only one of them is a write. The first time a decision is
    reached it is recorded, and it is the answer. A replay that reaches the same
    decision finds the recorded one and returns it -- the stored record, not the
    fresh copy, so what a caller reads is what is durable. A fresh execution that
    reaches a *different* decision under the same id is a contradiction this
    module cannot resolve by choosing, so it records the new observation as its
    own acceptance under a distinct id, answers BLOCKED, and leaves the recorded
    one untouched. Reusing the id while returning a different decision is the one
    outcome that is not available.

    The contradiction is stated as a gap, because a gap is what it is: the
    evidence now holds two incompatible observations, so neither can decide
    alone. Stating it there rather than in a field of its own is what makes it
    survive -- a returned record carrying a key the acceptances table does not
    persist would describe a conflict the next reader could not see.
    """
    from ..storage import StoreError

    try:
        store.record_acceptance(acceptance_id, record)
    except StoreError:
        recorded = store.load_acceptance(acceptance_id)
        if recorded is None:
            raise
        if recorded["decision"] == record["decision"]:
            return Verified(
                decision=recorded["decision"], context=recorded["context"],
                acceptance_id=acceptance_id, candidate=recorded["candidate"],
                target=recorded["target"], record=recorded,
                checks=tuple(recorded["checks"]),
            )
        conflict_id = _conflict_id(acceptance_id, record)
        record["gaps"] = [
            f"CONFLICT: this observation decided {record['decision']} and disagrees "
            f"with the recorded acceptance {acceptance_id}, which decided "
            f"{recorded['decision']} at {recorded['decided_at']} in integration "
            f"{recorded['integration_id']}. Both are retained under their own ids "
            "and neither was overwritten. This run is recorded as the contradiction "
            "and answered BLOCKED rather than picking one.",
            *record["gaps"],
        ]
        try:
            store.record_acceptance(conflict_id, record)
        except StoreError:
            pass  # a third observation of the same contradiction keeps the first two
        return Verified(
            decision=BLOCKED, context=record["context"], acceptance_id=conflict_id,
            candidate=record["candidate"], target=record["target"], record=record,
            checks=tuple(record["checks"]),
        )
    return Verified(
        decision=record["decision"], context=record["context"],
        acceptance_id=acceptance_id, candidate=record["candidate"],
        target=record["target"], record=record, checks=tuple(record["checks"]),
    )


def _conflict_id(acceptance_id: str, record: dict[str, Any]) -> str:
    """The distinct identity a contradicting observation is recorded under."""
    body = json.dumps(
        {
            "conflicts_with": acceptance_id,
            "decision": record["decision"],
            "gaps": record["gaps"],
            "findings": [f["kind"] for f in record["findings"]],
        },
        sort_keys=True, separators=(",", ":"),
    )
    return "acc-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]


def summarize(verified: Verified) -> str:
    """The human-readable form of a decision, in the order a reader needs it.

    Every field read here is one the acceptances table keeps, so the summary of a
    replayed decision is written from the same durable record the caller is
    being handed rather than from the copy that was not stored. A contradiction
    needs no line of its own: it is a gap, and the gaps are printed below.
    """
    record = verified.record
    policy = record["policy"]
    lines = [
        f"{verified.decision}  candidate {verified.candidate[:12]} on target {verified.target[:12]}",
        f"  context: {verified.context}"
        + ("" if verified.context == policies.PROTECTED
           else "  (local evidence; not a protected integration decision)"),
        f"  policy: {policy.get('policy_id', 'unknown')}"
        f" requires {', '.join(record['required_checks'])}",
        f"  manifest executed: {(record['manifest'].get('executed_digest') or 'none')[:16]}",
    ]
    for check in verified.checks:
        lines.append(f"  {check['result']:8} {check['check_id']}: {check['detail']}")
    for finding in record["findings"]:
        lines.append(f"  POLICY {finding['severity']:6} {finding['kind']}: {finding['detail']}")
    for gap in record["gaps"]:
        lines.append(f"  gap: {gap}")
    context = (record.get("verifier") or {}).get("verification_context") or {}
    if context.get("approved_revision"):
        lines.append(f"  verification code: approved {context['approved_revision'][:12]}")
    lines.append(
        f"  recorded as {verified.acceptance_id}; this command does not push, merge or "
        f"tag. Publish only while the target still resolves to {verified.target[:12]}."
    )
    return "\n".join(lines)

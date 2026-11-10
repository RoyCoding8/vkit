"""What is known about each check right now, and whether the tree is ready."""

from __future__ import annotations

import enum
import hashlib
import secrets
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .formal.policy import FormalComponent
from .inputs import InputSet, Snapshot, capture_working_tree
from .job import FormalGoal, RegisteredCheck, seal_job
from .manifest import Manifest
from .paths import ProjectError
from .requests import PackageVerificationRequest, WorkingTree
from .store import Store
from .verifiers.spec import CheckKind, CheckSpec


class Freshness(str, enum.Enum):
    FRESH_PASS = "fresh_pass"
    FRESH_FAIL = "fresh_fail"
    FRESH_BLOCKED = "fresh_blocked"
    STALE = "stale"
    MISSING = "missing"
    NOT_APPROVED = "not_approved"
    BLOCKED = "blocked"


class Verdict(str, enum.Enum):
    READY = "READY"
    READY_SCOPED = "READY_SCOPED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Doubt:
    field: str
    effect: str
    clearing: str
    falsifier: tuple[str, str]


DOUBTS = (
    Doubt("fresh_fail", "reject", "fix the subject until the check passes",
          ("test_a_failing_check_rejects_the_gate", "test_fixing_the_subject_clears_a_fresh_fail")),
    Doubt("not_approved", "reject", "authority revision: accept the definition",
          ("test_an_added_unaccepted_check_is_rejected", "test_definition_change_still_requires_reaccept")),
    Doubt("obligation_inventory", "reject", "authority revision: accept the manifest to bind the inventory",
          ("test_a_formal_only_manifest_without_an_inventory_is_rejected",
           "test_dropping_a_passing_obligation_clears_the_gate")),
    Doubt("obligation_removed", "reject", "authority revision: re-accept the manifest",
          ("test_removing_a_failing_check_cannot_reach_ready",
           "test_dropping_a_fresh_fail_obligation_stays_rejected_until_an_acceptance_rebinds_the_slot")),
    Doubt("fresh_blocked", "block", "clear the run blocker and re-run",
          ("test_a_blocked_run_blocks_the_gate", "test_a_cleared_run_reaches_ready")),
    Doubt("stale", "block", "re-run the check on the current inputs",
          ("test_a_stale_check_blocks_the_gate", "test_a_cleared_run_reaches_ready")),    Doubt("missing", "block", "run the check to produce evidence",
          ("test_a_partial_accept_cannot_rebind_a_removed_obligation", "test_fixing_the_subject_clears_a_fresh_fail")),
    Doubt("blocked", "block", "clear the reported blocker and re-run",
          ("test_a_blocked_formal_component_blocks_the_gate", "test_a_cleared_formal_component_reaches_ready")),
    Doubt("uncovered_change", "scope", "extend coverage or declare does_not_establish",
          ("test_uncovered_requested_paths_scope_the_ready_verdict", "test_a_cleared_run_reaches_ready")),
    Doubt("weakened_check", "reject", "fix the evaluator or accept the weakening with both runs displayed",
          ("test_a_weakened_evaluator_refuses_acceptance", "test_a_labeled_false_positive_fix_is_fixed_evaluator")),
    Doubt("incomparable", "block", "re-accept the reference",
          ("test_an_unrunnable_accepted_evaluator_is_incomparable",
           "test_restoring_the_reference_clears_an_incomparable_comparison")),
)
CI_VERDICTS = {"READY": Verdict.READY, "FAILED": Verdict.REJECTED, "BLOCKED": Verdict.BLOCKED}
_EFFECT_ORDER = ("reject", "block", "scope")


_FRESH = {
    "PASS": Freshness.FRESH_PASS,
    "FAIL": Freshness.FRESH_FAIL,
    "BLOCKED": Freshness.FRESH_BLOCKED,
}


SENTINEL_KINDS = frozenset({CheckKind.SCENARIO, CheckKind.PYTEST, CheckKind.NODE_TEST, CheckKind.PROPERTY})
SENTINEL_TIMEOUT = 3.0


def approved_inventory(approved: dict[str, Any]) -> dict[str, Any] | None:
    record = approved.get("__inventory__")
    if not isinstance(record, dict):
        return None
    return {"checks": record.get("checks", {}), "formal_required": record.get("formal_required", {}),
            "dropped": record.get("dropped", {})}


def _variant(check: CheckSpec) -> Any:
    return getattr(check, "variant", None) or check


def _evaluator_names(variant: Any) -> tuple[str, ...]:
    from .verifiers.spec import LeanCheck, TlcCheck

    if hasattr(variant, "command"):
        return tuple(variant.command)
    if hasattr(variant, "required_tests"):
        return tuple(test.split("::")[0] for test in variant.required_tests)
    if isinstance(variant, TlcCheck):
        return (variant.model.path, variant.config)
    if isinstance(variant, LeanCheck):
        return (variant.challenge.path, *((variant.solution.path,) if variant.solution else ()))
    return ()


def evaluator(check: CheckSpec) -> dict[str, bytes]:
    from .verifiers.spec import ExternalCheck

    variant = _variant(check)
    if isinstance(variant, ExternalCheck):
        return {}
    root = check.cwd.resolve()
    files = {}
    for name in _evaluator_names(variant):
        if "{{" in name:
            continue
        path = (root / name).resolve()
        if path.is_file() and path.is_relative_to(root):
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def evaluator_gaps(check: CheckSpec) -> list[str]:
    from .verifiers.spec import ExternalCheck

    variant = _variant(check)
    if isinstance(variant, ExternalCheck):
        return []
    root = check.cwd.resolve()
    gaps = {}
    for name in _evaluator_names(variant):
        if "{{" in name:
            if name != "{{python}}" and not name.startswith("{{run_dir}}"):
                gaps[name] = "templated token"
            continue
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            gaps[name] = "outside cwd"
        elif not path.is_file():
            gaps[name] = "missing"
    return [f"{name}: {reason}" for name, reason in sorted(gaps.items())]


def evaluator_digests(check: CheckSpec) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(evaluator(check).items())}


def oracle_matches(approved: dict[str, Any], check: CheckSpec, digest: str) -> bool:
    from .verifiers.spec import ExternalCheck

    record = approved.get(digest, {})
    if record.get("kind") != "check_definition":
        return False
    files = evaluator_digests(check)
    if isinstance(_variant(check), ExternalCheck):
        return record.get("oracle", {}).get("external") is True
    return bool(files) and record.get("oracle", {}).get("files") == files


def sentinel_subject(check: CheckSpec) -> list[str]:
    oracle = set(evaluator(check))
    return sorted(set(check.subject.paths) - oracle) or sorted(set(check.inputs) - oracle)


def _probe_run(project, check: CheckSpec, evaluator_files: dict[str, bytes],
               tree: dict[str, bytes]) -> str:
    from . import proc
    from .outcome import Passed
    from .runner import _child_env
    from .verifiers import dispatch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for path, content in tree.items():
            target = root.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for name, data in evaluator_files.items():
            target = root.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        run_dir = root / f".probe-{secrets.token_hex(8)}"
        run_dir.mkdir()
        try:
            location = root.joinpath(*check.cwd.resolve().relative_to(project.root.resolve()).parts)
        except ValueError:
            location = root
        location.mkdir(parents=True, exist_ok=True)
        finished = proc.run(dispatch.argv_for(check, run_dir, None), cwd=location,
                            stdout_path=run_dir / "stdout.log", stderr_path=run_dir / "stderr.log",
                            timeout_seconds=min(float(check.timeout_seconds), SENTINEL_TIMEOUT), env=_child_env())
        artifact = run_dir / check.artifact_name
        raw = artifact.read_bytes() if artifact.is_file() else None
    if raw is None:
        return "timeout" if finished is not None and finished.timed_out else "BLOCKED"
    try:
        passing = isinstance(dispatch.outcome_from_reading(dispatch.interpret(check, raw)), Passed)
    except (ValueError, KeyError, TypeError):
        return "BLOCKED"
    return "PASS" if passing else "FAIL"


def _null_probe(project, check: CheckSpec, emptied: set[str]) -> str:
    from .inputs import capture_working_tree, matches
    source = capture_working_tree(project)
    tree = {item.path: (b"" if any(matches(item.path, e) for e in emptied) else item.content)
            for item in source.files}
    return {"PASS": "vacuous", "FAIL": "rejects", "timeout": "timeout", "BLOCKED": "error"}[
        _probe_run(project, check, evaluator(check), tree)]


def comparison(store: Store, project, check: CheckSpec, prior: dict[str, Any] | None) -> dict[str, Any]:
    from .inputs import capture_working_tree
    identity = ((prior or {}).get("oracle") or {}).get("digest")
    if not identity:
        return {"outcome": "unchanged", "clearing": None, "receipts": []}
    accepted = store.evaluator_bytes(check.id, identity)
    if accepted is None:
        return {"outcome": "incomparable", "clearing": "re-accept the reference",
                "receipts": [{"run": "accepted evaluator bytes", "result": "not retained"}]}
    receipts = []
    try:
        tree = {item.path: item.content for item in capture_working_tree(project).files}
        candidate = _probe_run(project, check, accepted, tree)
    except (OSError, ValueError, RuntimeError, ProjectError):
        candidate = "BLOCKED"
    receipts.append({"run": "accepted evaluator vs current candidate", "result": candidate})
    if candidate == "BLOCKED":
        return {"outcome": "incomparable", "clearing": "re-accept the reference", "receipts": receipts}
    current = evaluator(check)
    if current == accepted or not set(accepted) & set(current):
        return {"outcome": "unchanged", "clearing": None, "receipts": receipts}
    snapshot = store.fail_snapshot(check.id, identity)
    if snapshot:
        try:
            snap = _probe_run(project, check, evaluator(check), snapshot["files"])
        except (OSError, ValueError, RuntimeError, ProjectError):
            snap = "BLOCKED"
        receipts.append({"run": f"candidate evaluator vs retained FAIL (label {snapshot['label']})",
                         "was": "FAIL", "result": snap})
        if snap == "PASS":
            if snapshot["label"] == "bad":
                return {"outcome": "fixed_evaluator", "clearing": "accept the fixed evaluator",
                        "receipts": receipts}
            return {"outcome": "weakened_check",
                    "clearing": "fix the evaluator or accept the weakening with both runs displayed",
                    "receipts": receipts}
    return {"outcome": "unchanged", "clearing": None, "receipts": receipts}


def sentinel(project, check: CheckSpec) -> dict[str, Any]:
    if check.kind not in SENTINEL_KINDS:
        return {"verdict": "exempt"}
    subject = sentinel_subject(check)
    if not subject:
        return {"verdict": "not_bound"}
    try:
        verdict = _null_probe(project, check, set(subject))
    except (OSError, ValueError, RuntimeError, ProjectError) as exc:
        return {"verdict": "unavailable", "detail": str(exc)}
    return {"verdict": verdict, "subject": subject}


def check_digests(manifest: Manifest) -> dict[str, str]:
    return {check_id: manifest.digest(check_id) for check_id in manifest.checks}


@dataclass(frozen=True)
class CheckState:
    check: CheckSpec
    digest: str
    key: str
    inputs: InputSet
    freshness: Freshness
    current: dict[str, Any] | None
    last: dict[str, Any] | None
    running: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        from .evidence import assurance

        category = self.check.evidence_kind()
        return {
            "id": self.check.id,
            "description": self.check.description,
            "kind": self.check.kind.value,
            "category": category.value,
            "assurance": assurance(self.check),
            "model_scope": getattr(self.check, "model_scope", category.establishes),
            "assumptions": list(
                getattr(
                    self.check,
                    "assumptions",
                    getattr(self.check, "permitted_axioms", ()),
                )
            ),
            "establishes": category.establishes,
            "does_not_establish": category.does_not_establish,
            "state": self.freshness.value,
            "key": self.key,
            "inputs": {
                "scope": self.inputs.scope,
                "declared": list(self.inputs.declared),
                "unmatched": list(self.inputs.unmatched),
            },
            "current": self.current,
            "last": self.last,
            "running": list(self.running),
        }


@dataclass(frozen=True)
class FormalState:
    component: FormalComponent
    key: str | None
    freshness: Freshness
    current: dict[str, Any] | None
    last: dict[str, Any] | None
    running: tuple[str, ...]
    detail: str | None = None

    @property
    def required(self) -> bool:
        return self.component.required

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.component.id,
            "kind": "formal_goal",
            "description": self.component.contract,
            "category": "source_verification",
            "assurance": "trusted_engine",
            "scope": "required_api",
            "required": self.required,
            "state": self.freshness.value,
            "key": self.key,
            "policy_sha256": self.component.digest,
            "package_sha256": self.component.package.sha256,
            "current": self.current,
            "last": self.last,
            "running": list(self.running),
            "detail": self.detail,
        }


def _resolve_execution(component: FormalComponent) -> dict[str, Any]:
    from .formal.verus import resolve_execution

    return resolve_execution(component)


def _formal_approval_matches(
    approved: dict[str, Any], component: FormalComponent, checker_sha256: str | None = None
) -> bool:
    record = approved.get(component.digest, {})
    return (
        record.get("kind") == "formal_policy"
        and record.get("policy_sha256") == component.digest
        and record.get("package_sha256") == component.package.sha256
        and (checker_sha256 is None or record.get("checker_sha256") == checker_sha256)
    )


def check_states(
    manifest: Manifest, store: Store, snapshot: Snapshot
) -> list[CheckState]:
    digests = check_digests(manifest)
    approved = store.approved()
    baselines = store.baselines()
    running: dict[str, list[str]] = {}
    for record in store.runs(limit=200):
        if record["state"] in ("queued", "running"):
            running.setdefault(record.get("job_id", record["key"]), []).append(
                record["run_id"]
            )
    states = []
    for check_id in sorted(manifest.checks):
        check = manifest.checks[check_id]
        inputs = snapshot.inputs(check.inputs)
        source = capture_working_tree(
            snapshot.project, [name for name, _ in inputs.files]
        )
        from .evidence import backend_identity

        try:
            backend = backend_identity(check)
        except (OSError, ValueError) as exc:
            backend = {"unavailable": str(exc)}
        key = seal_job(
            RegisteredCheck.resolve(
                manifest,
                check_id,
                baselines.get(check_id),
                backend,
                inputs.to_json(),
                source,
            ).payload()
        )["job_id"]
        current = store.evidence(check_id, key)
        last = store.latest(check_id)
        if not oracle_matches(approved, check, digests[check_id]):
            freshness = Freshness.NOT_APPROVED
        elif current is not None:
            freshness = _FRESH[current["result"]]
        elif last is not None:
            freshness = Freshness.STALE
        else:
            freshness = Freshness.MISSING
        states.append(
            CheckState(
                check,
                digests[check_id],
                key,
                inputs,
                freshness,
                current,
                last,
                tuple(running.get(key, ())),
            )
        )
    return states


def formal_states(manifest: Manifest, store: Store, snapshot: Snapshot) -> list[FormalState]:
    approved = store.approved()
    running: dict[str, list[str]] = {}
    for record in store.runs(limit=200):
        if (record["state"] in ("queued", "running")
                and record.get("subject", {}).get("kind") == "formal_goal"):
            running.setdefault(record["job_id"], []).append(record["run_id"])
    states = []
    source = None
    for component in sorted(manifest.components.values(), key=lambda item: item.id):
        last = store.latest_for("formal_goal", component.id)
        key, current, detail = None, None, "formal policy and package are not accepted"
        freshness = Freshness.NOT_APPROVED
        if _formal_approval_matches(approved, component):
            try:
                identity = _resolve_execution(component)
                from .inputs import canonical_digest

                checker = canonical_digest(identity["semantic"])
                if _formal_approval_matches(approved, component, checker):
                    if source is None:
                        source = capture_working_tree(snapshot.project)
                    request = PackageVerificationRequest(component.claim, WorkingTree())
                    key = seal_job(FormalGoal(request, component, checker, source).payload())["job_id"]
                    current = store.evidence_for("formal_goal", component.id, key)
                    freshness = (_FRESH[current["result"]] if current is not None else
                                 Freshness.STALE if last is not None else Freshness.MISSING)
                    detail = None
                else:
                    detail = "formal checker identity is not accepted"
            except (OSError, ValueError, RuntimeError, ProjectError) as exc:
                freshness, detail = Freshness.BLOCKED, str(exc)
        states.append(FormalState(component, key, freshness, current, last,
                                  tuple(running.get(key, ())), detail))
    return states


def needs_run(state: CheckState | FormalState) -> bool:
    if state.freshness not in (Freshness.STALE, Freshness.MISSING) or state.running:
        return False
    return not isinstance(state, FormalState) or state.key is not None


def gate(
    states: Iterable[CheckState], formal: Iterable[FormalState] = (),
    obligations: dict[str, Any] | None = None,
    uncovered: Iterable[str] = (),
    sentinels: dict[str, str] | None = None,
    comparisons: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    states = list(states)
    formal = list(formal)
    required = [state for state in formal if state.required]
    ghosts = []
    weak = sorted(cid for cid, out in (comparisons or {}).items() if out["outcome"] == "weakened_check")
    incomp = sorted(cid for cid, out in (comparisons or {}).items() if out["outcome"] == "incomparable")
    if weak:
        verdict, reason = Verdict.REJECTED, "weakened_check: " + ", ".join(weak) + " passes a retained legitimate FAIL"
    elif incomp:
        verdict, reason = Verdict.BLOCKED, "incomparable: " + ", ".join(incomp) + " cannot be compared; re-accept the reference"
    else:
        if obligations is not None:
            registered = {s.check.id for s in states}
            removed = set(obligations.get("checks", ())) - registered
            removed |= {rid for rid in obligations.get("formal_required", ())
                        if not any(s.component.id == rid and s.required for s in formal)}
            removed |= set(obligations.get("dropped", ())) - registered
            ghosts = [{"id": rid, "state": "obligation_removed", "run_id": None} for rid in sorted(removed)]
        if not states and not required and not ghosts:
            verdict, reason = Verdict.BLOCKED, "no checks are registered"
        elif obligations is None and (states or required):
            verdict, reason = Verdict.REJECTED, "obligation_inventory: no accepted inventory binds this manifest"
        elif ghosts:
            verdict, reason = Verdict.REJECTED, ("obligation_removed: " + ", ".join(g["id"] for g in ghosts)
                                                 + " were accepted but are no longer registered or required")
        elif any(
            state.freshness is Freshness.FRESH_FAIL
            for state in (*states, *required)
        ):
            verdict, reason = Verdict.REJECTED, "fresh_fail: a required check failed against the current inputs"
        elif any(state.freshness is Freshness.NOT_APPROVED for state in (*states, *required)):
            verdict, reason = Verdict.REJECTED, "not_approved: a required check definition is not accepted"
        elif all(state.freshness is Freshness.FRESH_PASS for state in states) and all(
            state.freshness is Freshness.FRESH_PASS for state in required
        ):
            gaps = list(uncovered)
            if gaps:
                verdict, reason = Verdict.READY_SCOPED, (f"uncovered_change: ready, but {len(gaps)} requested paths are outside "
                                                         f"every check's inputs: {', '.join(gaps)}")
            else:
                verdict, reason = Verdict.READY, "every required check passed against the current inputs"
        else:
            gap = "some required checks have no passing evidence for the current inputs"
            stuck = {s.freshness for s in (*states, *required)}
            reasons = {Freshness.FRESH_BLOCKED: f"fresh_blocked: {gap}", Freshness.BLOCKED: f"blocked: {gap}",
                       Freshness.STALE: f"stale: {gap}", Freshness.MISSING: f"missing: {gap}"}
            verdict, reason = Verdict.BLOCKED, next(reasons[fresh] for fresh in reasons if fresh in stuck)
    fields = {s.freshness.value for s in (*states, *required) if s.freshness is not Freshness.FRESH_PASS}
    fields.add(reason.split(":")[0])
    doubts = sorted((d for d in DOUBTS if d.field in fields), key=lambda d: _EFFECT_ORDER.index(d.effect))
    return {
        "verdict": verdict.value,
        "reason": reason,
        "clearing": doubts[0].clearing if doubts else None,
        "doubts": [{"field": d.field, "effect": d.effect, "clearing": d.clearing} for d in doubts],
        "checks": [
            {
                "id": state.check.id,
                "state": state.freshness.value,
                "run_id": (state.current or {}).get("run_id"),
                **({"sentinel": sentinels[state.check.id]} if sentinels and state.check.id in sentinels else {}),
            }
            for state in states
        ] + ghosts,
        "formal": [
            {
                "id": state.component.id,
                "state": state.freshness.value,
                "required": state.required,
                "run_id": (state.current or {}).get("run_id"),
            }
            for state in formal
        ],
    }

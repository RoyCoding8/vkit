"""Answers about a project: what is known, what is stale, what ran. Every view renders these."""
from __future__ import annotations

import os
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .features import FeatureError, load_features
from .inputs import Snapshot, matches
from .manifest import Manifest, ManifestError, parse_manifest
from .paths import Project, open_project
from .proposals import pending
from .state import CheckState, FormalState, Freshness, _resolve_execution, check_states, formal_states as _formal_states, gate, needs_run
from .store import Store

DEFAULT_LOG_LIMIT = 16_000
MAX_ARTIFACTS = 100
RUN_SECTIONS = ("summary", "diagnostics", "obligations", "provenance", "artifacts", "log")


@dataclass(frozen=True)
class Context:
    project: Project
    store: Store
    manifest: Manifest | None
    manifest_error: str | None

    def require_manifest(self) -> Manifest:
        if self.manifest is None:
            raise ManifestError(self.manifest_error or "no manifest")
        return self.manifest


def open_context(path: str | Path) -> Context:
    project = open_project(path)
    try:
        manifest, error = parse_manifest(project), None
    except ManifestError as exc:
        manifest, error = None, str(exc)
    return Context(project, Store.of(project), manifest, error)


def states(ctx: Context) -> list[CheckState]:
    if ctx.manifest is None:
        return []
    return check_states(ctx.manifest, ctx.store, Snapshot(ctx.project))


def formal_states(ctx: Context) -> list[FormalState]:
    if ctx.manifest is None:
        return []
    return _formal_states(ctx.manifest, ctx.store, Snapshot(ctx.project))

def _features(ctx: Context, by_id: dict[str, CheckState]) -> tuple[list[dict[str, Any]], str | None]:
    try:
        features = load_features(ctx.project.root)
    except FeatureError as exc:
        return [], str(exc)
    rows = []
    for feature in features:
        check_rows = [{"id": c, "state": by_id[c].freshness.value if c in by_id else "unregistered"}
                      for c in feature.covered_by]
        problems = feature.audit(ctx.project.root, by_id)
        verified = not problems and not feature.gaps and all(
            r["state"] == Freshness.FRESH_PASS.value for r in check_rows)
        rows.append({**feature.to_json(), "checks": check_rows, "problems": problems, "verified": verified})
    return rows, None


def status(ctx: Context, paths: Iterable[str] = ()) -> dict[str, Any]:
    """Every check's freshness and the gate; narrowed to checks reading `paths` when given."""
    all_states = states(ctx)
    all_formal = formal_states(ctx)
    by_id = {s.check.id: s for s in all_states}
    wanted = [p.replace("\\", "/") for p in paths]
    shown = [s for s in all_states if not wanted or any(s.inputs.covers(p) for p in wanted)]
    features, feature_error = _features(ctx, by_id)
    if wanted:
        ids = {s.check.id for s in shown}
        features = [f for f in features if ids & set(f["covered_by"])
                    or any(matches(p, e) for p in wanted for e in f["entry_points"])]
    return {
        "project": str(ctx.project.root),
        "manifest_error": ctx.manifest_error,
        "gate": gate(all_states, all_formal) if ctx.manifest is not None else
        {"verdict": "BLOCKED", "reason": ctx.manifest_error, "checks": []},
        "paths": wanted,
        "checks": [s.to_json() for s in shown],
        "formal": [s.to_json() for s in all_formal],
        "unmapped_paths": [p for p in wanted if not any(s.inputs.covers(p) for s in all_states)],
        "feature_paths_unmapped": [p for p in wanted if not any(matches(p, e) for f in features
                                                                for e in f["entry_points"])],
        "features": features,
        "feature_error": feature_error,
        "needs_run": [s.check.id for s in all_states if needs_run(s)],
        "formal_needs_run": [s.component.id for s in all_formal if needs_run(s)],
        "proposals": [{"digest": p.digest, "rationale": p.body["rationale"],
                       "checks": [c["id"] for c in p.body["checks"]],
                       "features": [f["id"] for f in p.body["features"]], "files": sorted(p.body["files"]),
                       "accept": f"vkit accept --proposal {p.digest[:12]}"} for p in pending(ctx.store)],
    }


def _run_log(ctx: Context, run_id: str, log: str, offset: int, limit: int) -> dict[str, Any]:
    if log not in ("stdout", "stderr"):
        raise ValueError("log must be stdout or stderr")
    path = ctx.store.run_dir(run_id) / f"{log}.log"
    size, start, window = 0, max(0, offset), b""
    if path.is_file():
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size + offset) if offset < 0 else offset
                handle.seek(start)
                window = handle.read(max(0, limit))
        except FileNotFoundError:
            pass
    return {"stream": log, "offset": start, "size": size,
            "text": window.decode("utf-8", "replace")}


def _obligation_counts(record: dict[str, Any]) -> dict[str, int]:
    obligations = record.get("obligations")
    if isinstance(obligations, dict):
        satisfied = obligations.get("satisfied") or []
        failed = obligations.get("counterexamples") or []
        total = len(satisfied) + len(failed)
        return {"total": total, "satisfied": len(satisfied), "failed": len(failed), "unknown": 0}
    outcome = record.get("outcome") or {}
    scenarios = outcome.get("scenarios", [])
    if isinstance(obligations, list):
        observed = {item.get("id"): item.get("result") for item in scenarios
                    if item.get("result") in ("PASS", "FAIL") and item.get("id") in obligations}
        satisfied = sum(result == "PASS" for result in observed.values())
        failed = sum(result == "FAIL" for result in observed.values())
        return {"total": len(obligations), "satisfied": satisfied, "failed": failed,
                "unknown": len(obligations) - satisfied - failed}
    satisfied = sum(item.get("result") == "PASS" for item in scenarios)
    failed = sum(item.get("result") == "FAIL" for item in scenarios)
    return {"total": len(scenarios), "satisfied": satisfied, "failed": failed, "unknown": 0}


def run_summary(record: dict[str, Any]) -> dict[str, Any]:
    outcome = record.get("outcome") or {}
    scenarios = outcome.get("scenarios", [])
    failed = [item.get("id") for item in scenarios if item.get("result") == "FAIL" and item.get("id")]
    obligations = record.get("obligations")
    if isinstance(obligations, dict):
        failed.extend(item.get("obligation", {}).get("obligation") for item in obligations.get("counterexamples", [])
                      if item.get("obligation", {}).get("obligation"))
    result = {key: record.get(key) for key in (
        "job_id", "run_id", "subject", "check_id", "state", "method", "category", "scope",
        "assurance", "claim_outcome", "receipt")}
    result["outcome"] = {key: outcome[key] for key in ("result", "reason") if key in outcome}
    result["counts"] = {"obligations": _obligation_counts(record)}
    result["failure_ids"] = list(dict.fromkeys(failed))
    result["next_action"] = (
        "poll" if record.get("state") in ("queued", "running") else
        "inspect_diagnostics" if outcome.get("result") == "BLOCKED" else
        "inspect_obligations" if outcome.get("result") == "FAIL" else "none")
    return result


def _diagnostics(ctx: Context, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    run_dir = ctx.store.run_dir(run_id)
    outcome = record.get("outcome") or {}
    container_log = run_dir / "container.stderr"
    detail = ""
    if container_log.is_file() and not container_log.is_symlink() and container_log.stat().st_size <= 1_048_576:
        detail = container_log.read_text(encoding="utf-8", errors="replace")
    phase = None
    for token in ("phase_exit:", "formal_obligation:", "phase_timeout:", "tool_launch_error:"):
        match = re.search(rf"{re.escape(token)}([A-Za-z0-9_-]+)", detail)
        if match:
            phase = match.group(1)
            break
    diagnostics = []
    if outcome.get("detail"):
        diagnostics.append({"kind": outcome.get("reason", "blocked"), "summary": outcome["detail"][:500]})
    if phase:
        log_path = run_dir / "formal-output" / f"{phase}.log"
        phase_detail = ""
        if log_path.is_file() and not log_path.is_symlink():
            with log_path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - 2048))
                phase_detail = handle.read(2048).decode("utf-8", "replace").strip()[-500:]
        item = {"phase": phase, "summary": phase_detail or detail.strip()[-500:],
                "artifact": f"formal-output/{phase}.log", "container_log": "container.stderr"}
        if log_path.is_file():
            item["size"] = log_path.stat().st_size
        diagnostics.append(item)
    return {"diagnostics": diagnostics, "container_log": "container.stderr" if detail else None}


def _provenance(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("source") or {}
    inputs = record.get("inputs") or {}
    verification = record.get("verification") or {}
    return {
        "job_id": record.get("job_id"), "subject": record.get("subject"),
        "source": {key: source.get(key) for key in ("kind", "digest", "commit") if key in source}
                  | {"file_count": len(source.get("files") or [])},
        "inputs": {key: inputs.get(key) for key in ("digest", "unmatched") if key in inputs}
                  | {"file_count": len(inputs.get("files") or [])},
        "policy_sha256": record.get("policy_sha256"), "package_sha256": record.get("package_sha256"),
        "checker_sha256": record.get("checker_sha256"),
        "execution_identity": (verification.get("execution_identity") or {}).get("digest"),
        "receipt": record.get("receipt"),
    }


def _artifacts(ctx: Context, run_id: str, record: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    refs = []
    for item in (record.get("verification") or {}).get("evidence", []):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            refs.append({"path": item["path"], "sha256": item.get("sha256"), "kind": "verification"})
    receipt_path = ctx.store.run_dir(run_id) / "receipt.json"
    if receipt_path.is_file() and not receipt_path.is_symlink() and receipt_path.stat().st_size <= 1_048_576:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            refs.extend({"path": path, "sha256": digest, "kind": "receipt"}
                        for path, digest in receipt.get("artifacts", {}).items())
        except (OSError, ValueError, AttributeError):
            pass
    unique = {item["path"]: item for item in refs}
    items = sorted(unique.values(), key=lambda item: item["path"])
    start = max(0, offset)
    page = items[start:start + min(max(0, limit), MAX_ARTIFACTS)]
    next_offset = start + len(page) if start + len(page) < len(items) else None
    return {"items": page, "total": len(items), "offset": start, "next_offset": next_offset}


def run_view(ctx: Context, run_id: str, *, sections: Iterable[str] | None = None,
             log: str = "stdout", offset: int = 0, limit: int = DEFAULT_LOG_LIMIT,
             artifact_offset: int = 0, artifact_limit: int = MAX_ARTIFACTS) -> dict[str, Any] | None:
    record = ctx.store.record(run_id)
    if record is None:
        return None
    selected = set(normalize_run_sections(sections))
    view = {"sections": [section for section in RUN_SECTIONS if section in selected],
            "summary": run_summary(record)}
    if "diagnostics" in selected:
        view["diagnostics"] = _diagnostics(ctx, run_id, record)
    if "obligations" in selected:
        view["obligations"] = record.get("obligations")
    if "provenance" in selected:
        view["provenance"] = _provenance(record)
    if "artifacts" in selected:
        view["artifacts"] = _artifacts(ctx, run_id, record, artifact_offset, artifact_limit)
    if "log" in selected:
        view["log"] = _run_log(ctx, run_id, log, offset, min(max(0, limit), 64_000))
    return view


def normalize_run_sections(sections: Iterable[str] | None) -> tuple[str, ...]:
    selected = {"summary"} if sections is None else set(sections)
    if not selected or selected - set(RUN_SECTIONS) - {"all"} or ("all" in selected and len(selected) != 1):
        raise ValueError("sections must select known sections; all must be selected alone")
    if "all" in selected:
        return RUN_SECTIONS
    return tuple(section for section in RUN_SECTIONS if section in selected)


def run_view_full(ctx: Context, run_id: str, *, log: str = "stdout", offset: int = 0,
                  limit: int = DEFAULT_LOG_LIMIT) -> dict[str, Any] | None:
    """Internal full record view for the console, which needs historical detail."""
    record = ctx.store.record(run_id)
    if record is None:
        return None
    return {**record, "log": _run_log(ctx, run_id, log, offset, limit)}


def runs(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in ("run_id", "subject", "check_id", "state", "started_at", "ended_at", "worktree")}
            | {"result": (r.get("outcome") or {}).get("result"),
               "reason": (r.get("outcome") or {}).get("reason")}
            for r in ctx.store.runs(limit)]


def doctor(ctx: Context) -> dict[str, Any]:
    findings = []
    if ctx.manifest is None:
        findings.append({"check": "<manifest>", "ok": False, "detail": ctx.manifest_error})
    else:
        for check in ctx.manifest.checks.values():
            for need in check.prerequisites:
                found = shutil.which(need.executable)
                findings.append({"check": check.id, "prerequisite": need.name, "ok": found is not None,
                                 "detail": found or f"{need.executable!r} is not on PATH"})
        for component in ctx.manifest.components.values():
            try:
                _resolve_execution(component)
            except Exception as exc:
                findings.append({"check": component.id, "kind": "formal_goal",
                                 "prerequisite": "Verus setup", "ok": False,
                                 "detail": f"{type(exc).__name__}: {exc}"})
            else:
                findings.append({"check": component.id, "kind": "formal_goal",
                                 "prerequisite": "Verus setup", "ok": True,
                                 "detail": "pinned Verus assets and container image are available"})
    return {"project": str(ctx.project.root), "state_root": str(ctx.store.root),
            "ok": all(f["ok"] for f in findings), "findings": findings}

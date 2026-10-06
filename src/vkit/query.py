"""Answers about a project: what is known, what is stale, what ran. Every view renders these."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .features import FeatureError, load_features
from .inputs import Snapshot, matches
from .manifest import Manifest, ManifestError, parse_manifest
from .paths import Project, open_project
from .proposals import pending
from .state import CheckState, Freshness, check_states, gate, needs_run
from .store import Store

DEFAULT_LOG_LIMIT = 16_000


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
        "gate": gate(all_states) if ctx.manifest is not None else
        {"verdict": "BLOCKED", "reason": ctx.manifest_error, "checks": []},
        "paths": wanted,
        "checks": [s.to_json() for s in shown],
        "unmapped_paths": [p for p in wanted if not any(s.inputs.covers(p) for s in all_states)],
        "feature_paths_unmapped": [p for p in wanted if not any(matches(p, e) for f in features
                                                                for e in f["entry_points"])],
        "features": features,
        "feature_error": feature_error,
        "needs_run": [s.check.id for s in all_states if needs_run(s)],
        "proposals": [{"digest": p.digest, "rationale": p.body["rationale"],
                       "checks": [c["id"] for c in p.body["checks"]],
                       "features": [f["id"] for f in p.body["features"]], "files": sorted(p.body["files"]),
                       "accept": f"vkit accept --proposal {p.digest[:12]}"} for p in pending(ctx.store)],
    }


def run_view(ctx: Context, run_id: str, *, log: str = "stdout", offset: int = 0,
             limit: int = DEFAULT_LOG_LIMIT) -> dict[str, Any] | None:
    record = ctx.store.record(run_id)
    if record is None:
        return None
    if log not in ("stdout", "stderr"):
        raise ValueError("log must be stdout or stderr")
    path = ctx.store.run_dir(run_id) / f"{log}.log"
    data = path.read_bytes() if path.is_file() else b""
    start = max(0, len(data) + offset) if offset < 0 else offset
    window = data[start:start + max(0, limit)]
    return {**record, "log": {"stream": log, "offset": start, "size": len(data),
                              "text": window.decode("utf-8", "replace")}}


def runs(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in ("run_id", "check_id", "state", "started_at", "ended_at", "worktree")}
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
    return {"project": str(ctx.project.root), "state_root": str(ctx.store.root),
            "ok": all(f["ok"] for f in findings), "findings": findings}

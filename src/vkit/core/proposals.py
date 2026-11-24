"""Changes an agent may suggest and only a human may apply: checks, features, and new files they need."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from vkit.core.features import FEATURES_RELATIVE, FeatureError, parse_feature, read_document
from vkit.core.inputs import canonical_digest, is_secret
from vkit.core.manifest import ManifestError, parse_manifest_data
from vkit.runtime.paths import Project
from vkit.core.store import Store, now, read_json, write_json

MAX_FILE_BYTES = 200_000


class ProposalError(Exception):
    """The proposal is malformed, or cannot be applied to this tree."""


@dataclass(frozen=True)
class Proposal:
    digest: str
    body: dict[str, Any]

    def describe(self) -> str:
        lines = [f"proposal {self.digest[:12]}: {self.body.get('rationale', '')}"]
        for entry in self.body.get("checks", []):
            lines += [f"check {entry['id']}:", json.dumps(entry, indent=2)]
        for entry in self.body.get("features", []):
            lines += [f"feature {entry['id']}:", json.dumps(entry, indent=2)]
        if "formal" in self.body:
            lines += ["formal policy (complete replacement):", json.dumps(self.body["formal"], indent=2)]
        for path, text in self.body.get("files", {}).items():
            lines += [f"new file {path}:", text]
        return "\n".join(lines)


def _relative_new_file(project: Project, raw: str) -> str:
    parts = PurePosixPath(raw.replace("\\", "/")).parts
    if not parts or raw.startswith(("/", "\\")) or ":" in raw or ".." in parts or parts[0] == ".git":
        raise ProposalError(f"file {raw!r} must be a relative path inside the repository")
    relative = "/".join(parts)
    if is_secret(relative):
        raise ProposalError(f"file {raw!r} looks like a secret and cannot be proposed")
    target = project.root / relative
    if target.exists() or target.is_symlink():
        raise ProposalError(f"file {raw!r} already exists; proposals may only add files")
    if not target.resolve(strict=False).is_relative_to(project.root.resolve()):
        raise ProposalError(f"file {raw!r} escapes the repository root")
    return relative


def validate(project: Project, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) - {"checks", "features", "files", "formal", "rationale"}:
        raise ProposalError("a proposal is an object with checks, features, files, optional formal policy and rationale")
    checks, features, files = body.get("checks", []), body.get("features", []), body.get("files", {})
    if not isinstance(body.get("rationale", ""), str) or not body.get("rationale"):
        raise ProposalError("a proposal needs a rationale a reviewer can read")
    if not isinstance(checks, list) or not isinstance(features, list) or not isinstance(files, dict):
        raise ProposalError("checks and features are lists; files maps paths to text")
    if not (checks or features or files or "formal" in body):
        raise ProposalError("a proposal must change something")
    seen_features: set[str] = set()
    try:
        for entry in features:
            parsed = parse_feature(entry, "proposal")
            if parsed.id in seen_features:
                raise ProposalError(f"duplicate feature id {parsed.id!r} in proposal")
            seen_features.add(parsed.id)
    except (ManifestError, FeatureError) as exc:
        raise ProposalError(str(exc)) from exc
    clean = {"rationale": body["rationale"], "checks": checks, "features": features, "files": {}}
    if "formal" in body:
        clean["formal"] = body["formal"]
    if checks or "formal" in body:
        _resulting_manifest(project, clean)
    clean_files = {}
    for raw, text in files.items():
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ProposalError(f"file {raw!r} must be text of at most {MAX_FILE_BYTES} bytes")
        relative = _relative_new_file(project, raw)
        if relative in clean_files:
            raise ProposalError(f"duplicate proposed file {relative!r}")
        clean_files[relative] = text
    clean["files"] = clean_files
    return clean


def submit(project: Project, store: Store, body: Any) -> Proposal:
    clean = validate(project, body)
    digest = canonical_digest(clean)
    write_json(store.root / "proposals" / f"{digest}.json", {"body": clean, "proposed_at": now(), "state": "pending"})
    return Proposal(digest, clean)


def pending(store: Store) -> list[Proposal]:
    base = store.root / "proposals"
    found = []
    for path in sorted(base.glob("*.json")) if base.is_dir() else []:
        record = read_json(path)
        if not isinstance(record, dict):
            raise ValueError(f"proposal record {path.name} must be a JSON object")
        if record.get("state") == "pending":
            body = record.get("body")
            if not isinstance(body, dict):
                raise ValueError(f"pending proposal {path.name} is missing a body object")
            found.append(Proposal(path.stem, body))
    return found


def find(store: Store, prefix: str) -> Proposal:
    matches = [p for p in pending(store) if p.digest.startswith(prefix)] if len(prefix) >= 6 else []
    if len(matches) != 1:
        raise ProposalError(f"{prefix!r} matches {len(matches)} pending proposals; give at least 6 digest characters")
    return matches[0]


def _merge(entries: list[dict], updates: list[dict]) -> list[dict]:
    if not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get("id"), str) for e in entries):
        raise ProposalError("existing entries must be objects with string ids")
    by_id = {e["id"]: i for i, e in enumerate(entries)}
    merged = list(entries)
    seen: set[str] = set()
    for update in updates:
        if not isinstance(update, dict) or not isinstance(update.get("id"), str):
            raise ProposalError("proposal entries must be objects with string ids")
        entry_id = update["id"]
        if entry_id in seen:
            raise ProposalError(f"duplicate id {entry_id!r} in proposal")
        seen.add(entry_id)
        if entry_id in by_id:
            merged[by_id[entry_id]] = update
        else:
            merged.append(update)
    return merged


def _resulting_manifest(project: Project, body: dict[str, Any]):
    manifest_path = project.manifest_path
    try:
        document = (json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file()
                    else {"schema_version": 3, "description": "", "checks": []})
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalError(f"could not read manifest {manifest_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ProposalError("the project manifest must be a JSON object")
    document["checks"] = _merge(document.get("checks", []), body["checks"])
    if "formal" in body:
        if body["formal"] == {"components": []}:
            document.pop("formal", None)
        else:
            document["formal"] = body["formal"]
    try:
        manifest = parse_manifest_data(document, project=project, origin=str(manifest_path))
    except ManifestError as exc:
        raise ProposalError(str(exc)) from exc
    return document, manifest


def apply(project: Project, store: Store, proposal: Proposal) -> list[str]:
    """Write the proposal into this worktree and accept its check definitions. Returns the accepted check ids."""
    body = validate(project, proposal.body)
    manifest_path = project.manifest_path
    document = manifest = None
    if body["checks"] or "formal" in body:
        document, manifest = _resulting_manifest(project, body)
    approvals = {}
    if "formal" in body:
        from vkit.formal import ledger, verus

        try:
            for component in manifest.components.values():
                ledger.verify_package(component)
                execution = verus.resolve_execution(component)
                approvals[component.digest] = {
                    "kind": "formal_policy",
                    "policy_sha256": component.digest,
                    "package_sha256": component.package.sha256,
                    "checker_sha256": canonical_digest(execution["semantic"]),
                    "accepted_at": now(),
                }
        except (ValueError, RuntimeError) as exc:
            raise ProposalError(str(exc)) from exc
    if body["checks"]:
        from vkit.core.evidence import backend_identity

        approvals.update({manifest.digest(e["id"]): {"kind": "check_definition", "check_id": e["id"], "accepted_at": now(),
                                                       "backend": backend_identity(manifest.require(e["id"])),
                                                       "proposal": proposal.digest} for e in body["checks"]})
    if body["features"]:
        try:
            features = read_document(project.root)
        except FeatureError as exc:
            raise ProposalError(str(exc)) from exc
        features["features"] = _merge(features.get("features", []), body["features"])
    for relative, text in body["files"].items():
        target = project.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if body["checks"] or "formal" in body:
        assert document is not None
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if approvals:
        store.approve(approvals)
    if body["features"]:
        path = project.root / FEATURES_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(features, indent=2) + "\n", encoding="utf-8")
    write_json(store.root / "proposals" / f"{proposal.digest}.json",
               {"body": body, "state": "accepted", "accepted_at": now()})
    return [e["id"] for e in body["checks"]]


def reject(store: Store, proposal: Proposal) -> None:
    write_json(store.root / "proposals" / f"{proposal.digest}.json",
               {"body": proposal.body, "state": "rejected", "rejected_at": now()})

"""Changes an agent may suggest and only a human may apply: checks, features, and new files they need."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .features import FEATURES_RELATIVE, FeatureError, parse_feature, read_document
from .inputs import canonical_digest, is_secret
from .manifest import ManifestError, parse_manifest_data
from .paths import Project
from .store import Store, now, read_json, write_json

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
    if (project.root / relative).exists():
        raise ProposalError(f"file {raw!r} already exists; proposals may only add files")
    return relative


def validate(project: Project, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) - {"checks", "features", "files", "rationale"}:
        raise ProposalError("a proposal is an object with checks, features, files and rationale")
    checks, features, files = body.get("checks", []), body.get("features", []), body.get("files", {})
    if not isinstance(body.get("rationale", ""), str) or not body.get("rationale"):
        raise ProposalError("a proposal needs a rationale a reviewer can read")
    if not isinstance(checks, list) or not isinstance(features, list) or not isinstance(files, dict):
        raise ProposalError("checks and features are lists; files maps paths to text")
    if not (checks or features or files):
        raise ProposalError("a proposal must change something")
    try:
        if checks:
            parse_manifest_data({"schema_version": 2, "checks": checks}, project=project, origin="proposal")
        for entry in features:
            parse_feature(entry, "proposal")
    except (ManifestError, FeatureError) as exc:
        raise ProposalError(str(exc)) from exc
    clean_files = {}
    for raw, text in files.items():
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ProposalError(f"file {raw!r} must be text of at most {MAX_FILE_BYTES} bytes")
        clean_files[_relative_new_file(project, raw)] = text
    return {"rationale": body["rationale"], "checks": checks, "features": features, "files": clean_files}


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
        if record and record["state"] == "pending":
            found.append(Proposal(path.stem, record["body"]))
    return found


def find(store: Store, prefix: str) -> Proposal:
    matches = [p for p in pending(store) if p.digest.startswith(prefix)] if len(prefix) >= 6 else []
    if len(matches) != 1:
        raise ProposalError(f"{prefix!r} matches {len(matches)} pending proposals; give at least 6 digest characters")
    return matches[0]


def _merge(entries: list[dict], updates: list[dict]) -> list[dict]:
    by_id = {e["id"]: i for i, e in enumerate(entries)}
    merged = list(entries)
    for update in updates:
        if update["id"] in by_id:
            merged[by_id[update["id"]]] = update
        else:
            merged.append(update)
    return merged


def apply(project: Project, store: Store, proposal: Proposal) -> list[str]:
    """Write the proposal into this worktree and accept its check definitions. Returns the accepted check ids."""
    body = validate(project, proposal.body)
    manifest_path = project.manifest_path
    if body["checks"]:
        document = (json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file()
                    else {"schema_version": 2, "description": "", "checks": []})
        document["checks"] = _merge(document.get("checks", []), body["checks"])
        manifest = parse_manifest_data(document, project=project, origin=str(manifest_path))
    if body["features"]:
        features = read_document(project.root)
        features["features"] = _merge(features.get("features", []), body["features"])
    for relative, text in body["files"].items():
        target = project.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if body["checks"]:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        store.approve({manifest.digest(e["id"]): {"check_id": e["id"], "accepted_at": now(),
                                                  "proposal": proposal.digest} for e in body["checks"]})
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

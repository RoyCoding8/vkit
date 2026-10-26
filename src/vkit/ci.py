"""Fresh candidate checking under policy supplied by a trusted CI host."""

from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import container
from .evidence import _retained_path, policy_identity, validate_record
from .formal.policy import GitCommit, VerificationRequest
from .inputs import CapturedSource, capture_git_commit, git_ancestor, load_capture
from .manifest import Manifest, parse_manifest_data
from .outcome import Blocked, BlockedReason
from .paths import MANIFEST_RELATIVE, Project
from .store import Store, write_json
from .verifiers import dispatch

@dataclass(frozen=True)
class TargetAuthority:
    target_commit: str
    target_tree: str
    manifest_blob_oid: str
    manifest: dict[str, Any]
    implementation: dict[str, str]

    @classmethod
    def capture(cls, source: CapturedSource) -> TargetAuthority:
        entry = next((f for f in source.files if f.path == MANIFEST_RELATIVE.as_posix()), None)
        if entry is None:
            raise ValueError("target commit has no VKit policy")
        return cls(source.commit, source.tree, entry.blob_oid,
                   json.loads(entry.content.decode("utf-8")), policy_identity())

    def to_json(self) -> dict[str, Any]:
        return {"kind": "target_policy", **asdict(self)}


def validate_authority(job: dict[str, Any], project: Project) -> Manifest:
    authority = job["authority"]
    if authority["implementation"] != policy_identity():
        raise ValueError("target authority belongs to a different VKit installation")
    if job["source"]["kind"] != "git_commit":
        raise ValueError("target authority requires an exact Git candidate")
    actual = TargetAuthority.capture(capture_git_commit(project, authority["target_commit"]))
    if actual.to_json() != authority:
        raise ValueError("target authority differs from its raw Git policy")
    source = capture_git_commit(project, job["source"]["commit"])
    if {**source.to_json(), "digest": source.digest} != job["source"]:
        raise ValueError("authority candidate differs from its raw Git source")
    if not git_ancestor(project, authority["target_commit"], source):
        raise ValueError("authority target is not an ancestor of its candidate")
    manifest = parse_manifest_data(authority["manifest"], project=project, origin="target policy")
    subject = job["subject"]
    if subject["kind"] == "formal_goal":
        component = manifest.require_component(subject["id"])
        if not component.required or component.to_json() != job["component"]:
            raise ValueError("formal job differs from required target policy")
        if job["request"]["candidate"].get("target_commit") != authority["target_commit"]:
            raise ValueError("formal job target differs from target authority")
    elif manifest.entries.get(subject["id"]) != job["definition"]:
        raise ValueError("registered check differs from target policy")
    return manifest


def empirical_identity(check, image: str | None = None) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise ValueError("sealed CI empirical execution requires Linux and Docker")
    if check.kind.value != "scenario" or check.argv[:3] != ("{{python}}", "-I", "-c"):
        raise ValueError("sealed CI requires a target-policy Python driver declared as {{python}} -I -c CODE; candidate-owned checkers are unsupported")
    image = image or os.environ.get("VKIT_CI_IMAGE", "")
    if not image.startswith("sha256:") or container.image_id(image) is None:
        raise ValueError("VKIT_CI_IMAGE must name an installed immutable Docker image ID")
    return {"kind": "sealed_empirical", "image_id": image,
            "implementation": policy_identity(),
            "argv": list(dispatch.argv_for(check, PurePosixPath("/output"), "/usr/bin/python3"))}


def execute_empirical(check, store: Store, record: dict[str, Any], job: dict[str, Any]):
    from .runner import _derive
    run_dir = store.run_dir(record["run_id"])
    snapshot = run_dir / "snapshot"
    backend = job["backend"]
    if empirical_identity(check, backend["image_id"]) != backend:
        raise ValueError("sealed empirical execution identity changed")
    output = run_dir / "check-output"
    output.mkdir()
    output.chmod(0o777)
    options = [*container.sealed_options(),
               "--workdir", "/source/" + check.cwd.relative_to(snapshot).as_posix(),
               "--env", "PYTHONDONTWRITEBYTECODE=1",
               *container.mount(snapshot, "/source", True),
               *container.mount(output, "/output", False)]
    result = container.run_container(
        image=backend["image_id"], run_id=f"{run_dir.name}-{uuid.uuid4().hex}",
        options=options, command=backend["argv"], run_dir=run_dir,
        timeout_seconds=check.timeout_seconds,
        should_cancel=lambda: store.cancel_requested(record["run_id"]),
    )
    metadata = {"execution": "sealed_empirical", "image_id": backend["image_id"],
                "argv": backend["argv"], "container_id": result.container_id,
                "removed": result.removed}
    if not result.removed:
        return Blocked(BlockedReason.INTERNAL_ERROR, result.cleanup_error or "container cleanup failed"), None, result.exit, metadata
    # Candidate output crosses back into the trusted host only after the container exits.
    artifact = _retained_path(output, check.artifact_name)
    if artifact.exists() and (not artifact.is_file() or artifact.stat().st_size > 16 * 1024 * 1024):
        raise ValueError("empirical artifact must be a regular file of at most 16 MiB")
    outcome, reading = _derive(check, result.exit, artifact, None)
    if outcome.to_json()["result"] == "PASS" and result.exit.code != 0:
        outcome, reading = Blocked(BlockedReason.ARTIFACT_MALFORMED, "a passing protected check must exit zero"), None
    load_capture(job["source"], snapshot)
    if empirical_identity(check, backend["image_id"]) != backend:
        raise ValueError("sealed empirical execution identity changed during execution")
    return outcome, reading, result.exit, metadata


def run_gate(project: Project, candidate: str, target: str, output: Path) -> dict[str, Any]:
    from .formal.verus import resolve_execution
    from .runner import execute, prepare, prepare_formal

    source = capture_git_commit(project, candidate)
    target_source = capture_git_commit(project, target)
    if not git_ancestor(project, target, source):
        raise ValueError("target commit must be an ancestor of the combined candidate")
    authority = TargetAuthority.capture(target_source).to_json()
    manifest = parse_manifest_data(authority["manifest"], project=project, origin="target policy")
    required = [("registered_check", id) for id in manifest.checks]
    required += [("formal_goal", c.id) for c in manifest.components.values() if c.required]
    if not required:
        raise ValueError("target policy has no required checks or formal components")
    output = output.resolve()
    if output.is_relative_to(project.root) or output.is_relative_to(project.git_common_dir):
        raise ValueError("CI output must be a new private directory outside the candidate repository")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    store = Store(output / "store")
    results = []
    for kind, id in required:
        try:
            if kind == "formal_goal":
                component = manifest.require_component(id)
                record = prepare_formal(
                    project, VerificationRequest(id, GitCommit(candidate, target)), component,
                    resolve_execution(component), store=store, source=source, authority=authority,
                )
            else:
                record = prepare(project, manifest, id, store=store, source=source,
                                 authority=authority, backend=empirical_identity(manifest.require(id)))
            record = execute(project, record["run_id"], store=store)
            valid = validate_record(record, store.run_dir(record["run_id"]))
            valid = valid and record.get("authority") == authority and record["source"] == {
                **source.to_json(), "digest": source.digest}
            results.append({"kind": kind, "id": id, "run_id": record["run_id"],
                            "job_id": record["job_id"], "receipt_valid": valid,
                            "receipt": record.get("receipt"), "outcome": record["outcome"]})
        except (OSError, ValueError, RuntimeError) as exc:
            results.append({"kind": kind, "id": id, "receipt_valid": False,
                            "outcome": Blocked(BlockedReason.INTERNAL_ERROR, str(exc)).to_json()})
    ready = all(r["receipt_valid"] and r["outcome"]["result"] == "PASS" for r in results)
    verdict = "READY" if ready else "FAILED" if any(r["outcome"]["result"] == "FAIL" for r in results) else "BLOCKED"
    report = {"verdict": verdict, "candidate": candidate, "target": target,
              "authority": authority, "checks": results,
              "reason": "all target requirements passed on the exact candidate" if ready else "target requirements are not established"}
    write_json(output / "gate.json", report)
    return report

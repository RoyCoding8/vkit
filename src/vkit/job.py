"""Closed semantic payloads for the shared registered-check and formal job lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from .formal.policy import FormalComponent, VerificationRequest
from .inputs import CapturedSource, canonical_digest
from .manifest import Manifest
from .schemas import JOB, validate
from .verifiers.spec import CheckSpec


def _source_json(source: CapturedSource) -> dict[str, Any]:
    return {**source.to_json(), "digest": source.digest}


@dataclass(frozen=True)
class RegisteredCheck:
    check: CheckSpec
    definition: dict[str, Any]
    check_digest: str
    baseline: dict[str, Any] | None
    backend: dict[str, Any]
    inputs: dict[str, Any]
    source: CapturedSource

    @classmethod
    def resolve(
        cls,
        manifest: Manifest,
        check_id: str,
        baseline: dict[str, Any] | None,
        backend: dict[str, Any],
        inputs: dict[str, Any],
        source: CapturedSource,
    ) -> RegisteredCheck:
        return cls(
            manifest.require(check_id),
            manifest.entries[check_id],
            manifest.digest(check_id),
            baseline,
            backend,
            inputs,
            source,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "subject": {"kind": "registered_check", "id": self.check.id},
            "definition": self.definition,
            "check_digest": self.check_digest,
            "baseline": self.baseline,
            "backend": self.backend,
            "inputs": self.inputs,
            "source": _source_json(self.source),
        }


@dataclass(frozen=True)
class FormalGoal:
    request: VerificationRequest
    component: FormalComponent
    execution_identity: dict[str, Any]
    source: CapturedSource

    def payload(self) -> dict[str, Any]:
        semantic = self.execution_identity["semantic"]
        return {
            "schema_version": 2,
            "subject": {"kind": "formal_goal", "id": self.component.id},
            "request": self.request.semantic_json(),
            "component": self.component.to_json(),
            "policy_sha256": self.component.digest,
            "package_sha256": self.component.package.sha256,
            "checker_sha256": canonical_digest(semantic),
            "execution_identity": self.execution_identity,
            "source": _source_json(self.source),
        }


ResolvedJob: TypeAlias = RegisteredCheck | FormalGoal


def job_id(payload: dict[str, Any]) -> str:
    semantic = {key: value for key, value in payload.items() if key != "job_id"}
    if payload.get("subject", {}).get("kind") == "formal_goal":
        identity = dict(semantic["execution_identity"])
        identity.pop("paths", None)
        semantic["execution_identity"] = identity
    return canonical_digest(semantic)


def seal_job(payload: dict[str, Any]) -> dict[str, Any]:
    job = {**payload, "job_id": job_id(payload)}
    validate("resolved job", JOB, job)
    return job


def validate_job(payload: Any) -> dict[str, Any]:
    validate("resolved job", JOB, payload)
    if payload["job_id"] != job_id(payload):
        raise ValueError("resolved job semantic identity does not match its job ID")
    return payload

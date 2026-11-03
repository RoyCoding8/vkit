"""Closed semantic payloads for the shared job lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from .certification import ChangeCertificationRequest
from .change_methods import ChangeMethodPlan
from .formal.policy import FormalComponent
from .inputs import CapturedSource, canonical_digest
from .intent import BoundChangeRequest
from .manifest import Manifest
from .requests import GitCommit, PackageVerificationRequest
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
    authority: dict[str, Any] | None = None

    @classmethod
    def resolve(
        cls,
        manifest: Manifest,
        check_id: str,
        baseline: dict[str, Any] | None,
        backend: dict[str, Any],
        inputs: dict[str, Any],
        source: CapturedSource,
        authority: dict[str, Any] | None = None,
    ) -> RegisteredCheck:
        return cls(
            manifest.require(check_id),
            manifest.entries[check_id],
            manifest.digest(check_id),
            baseline,
            backend,
            inputs,
            source,
            authority,
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
            **({"authority": self.authority} if self.authority is not None else {}),
        }


@dataclass(frozen=True)
class FormalGoal:
    request: PackageVerificationRequest
    component: FormalComponent
    checker_sha256: str
    source: CapturedSource
    authority: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "subject": {"kind": "formal_goal", "id": self.component.id},
            "request": self.request.semantic_json(),
            "component": self.component.to_json(),
            "policy_sha256": self.component.digest,
            "package_sha256": self.component.package.sha256,
            "checker_sha256": self.checker_sha256,
            "source": _source_json(self.source),
            **({"authority": self.authority} if self.authority is not None else {}),
        }


@dataclass(frozen=True)
class ChangeCertification:
    bound: BoundChangeRequest
    base: CapturedSource
    candidate: CapturedSource
    methods: ChangeMethodPlan
    implementation: dict[str, str]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "subject": {"kind": "change_certification", "id": self.bound.intent.digest},
            "source": _source_json(self.candidate),
            "target_source": _source_json(self.base),
            "trusted_target_oid": self.base.commit,
            "change_request": self.bound.request.to_json(),
            "bound_request_sha256": self.bound.digest,
            "method_plan": self.methods.to_json(),
            "implementation": self.implementation,
        }


ResolvedJob: TypeAlias = RegisteredCheck | FormalGoal | ChangeCertification


def job_id(payload: dict[str, Any]) -> str:
    semantic = {key: value for key, value in payload.items() if key != "job_id"}
    return canonical_digest(semantic)


def seal_job(payload: dict[str, Any]) -> dict[str, Any]:
    job = {**payload, "job_id": job_id(payload)}
    return validate_job(job)


def validate_job(payload: Any) -> dict[str, Any]:
    validate("resolved job", JOB, payload)
    if payload["job_id"] != job_id(payload):
        raise ValueError("resolved job semantic identity does not match its job ID")
    if payload["subject"]["kind"] == "change_certification":
        request = ChangeCertificationRequest.from_json(payload["change_request"])
        methods = ChangeMethodPlan.from_json(payload["method_plan"])
        if (request.to_json() != payload["change_request"]
                or methods.to_json() != payload["method_plan"]
                or payload["subject"]["id"] != request.declared_intent_sha256
                or payload["trusted_target_oid"] != request.base_oid
                or payload["source"]["kind"] != "git_commit"
                or payload["source"]["commit"] != request.candidate_oid
                or payload["target_source"]["kind"] != "git_commit"
                or payload["target_source"]["commit"] != request.base_oid
                or payload["source"]["object_format"] != payload["target_source"]["object_format"]):
            raise ValueError("change job request, target, sources and method plan differ")
    if payload["subject"]["kind"] == "formal_goal":
        request = PackageVerificationRequest.from_json(payload["request"])
        try:
            component = FormalComponent.from_json(payload["component"])
        except (KeyError, TypeError) as exc:
            raise ValueError("formal job component is malformed") from exc
        if (request.semantic_json() != payload["request"]
                or payload["subject"]["id"] != component.id
                or component.to_json() != payload["component"]
                or request.claim != component.claim
                or payload["policy_sha256"] != component.digest
                or payload["package_sha256"] != component.package.sha256):
            raise ValueError("formal job request, component, and package identity differ")
        if isinstance(request.candidate, GitCommit):
            if (payload["source"]["kind"] != "git_commit"
                    or payload["source"]["commit"] != request.candidate.oid):
                raise ValueError("formal job source differs from its requested Git candidate")
        elif (payload["source"]["kind"] != "working_tree"
              or payload["source"]["commit"] is not None
              or payload["source"]["tree"] is not None
              or payload["source"]["parents"]):
            raise ValueError("formal job source differs from its working-tree request")
    return payload

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .certification import (
    ChangeCertificationRequest,
    EntailmentKind,
    IntentRevision,
    intent_entails_claim,
)
from .inputs import (
    CapturedFile,
    CapturedSource,
    _commit_metadata,
    canonical_digest,
    capture_git_commit,
    git_ancestor,
)
from .paths import Project
from .schemas import validate

_ACCEPTED_SCHEMA = "accepted-intents.v1.json"
_INTENT_DIRECTORY = "verification/change-intents"
_ACCEPTED_PATH = f"{_INTENT_DIRECTORY}/accepted.v1.json"


@dataclass(frozen=True)
class BoundChangeRequest:
    request: ChangeCertificationRequest
    intent: IntentRevision
    target_tree_oid: str
    candidate_tree_oid: str
    accepted_intents_blob_oid: str
    intent_revision_blob_oid: str
    base_capture_sha256: str
    candidate_capture_sha256: str

    def to_json(self) -> dict[str, Any]:
        return {
            "scope": "request_preflight",
            "candidate_behavior_checked": False,
            "request": self.request.to_json(),
            "intent": self.intent.to_json(),
            "target_tree_oid": self.target_tree_oid,
            "candidate_tree_oid": self.candidate_tree_oid,
            "accepted_intents_blob_oid": self.accepted_intents_blob_oid,
            "intent_revision_blob_oid": self.intent_revision_blob_oid,
            "base_capture_sha256": self.base_capture_sha256,
            "candidate_capture_sha256": self.candidate_capture_sha256,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _strict_json(file: CapturedFile, label: str) -> Any:
    try:
        return json.loads(file.content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _required_file(files: dict[str, CapturedFile], path: str, label: str) -> CapturedFile:
    file = files.get(path)
    if file is None:
        raise ValueError(f"{label} is missing from the captured Git tree: {path}")
    return file


def _prepare_change_request(
    project: Project, raw_request: Any, *, trusted_target_oid: str
) -> tuple[BoundChangeRequest, CapturedSource, CapturedSource]:
    request = ChangeCertificationRequest.from_json(raw_request)
    if request.base_oid != trusted_target_oid:
        raise ValueError("request base must equal the trusted target commit")

    base = capture_git_commit(project, trusted_target_oid)
    candidate = capture_git_commit(project, request.candidate_oid)
    bound = _bind_captured_change_request(
        project, request, base, candidate, trusted_target_oid=trusted_target_oid
    )
    return bound, base, candidate


def _bind_captured_change_request(
    project: Project,
    raw_request: Any,
    base: CapturedSource,
    candidate: CapturedSource,
    *,
    trusted_target_oid: str,
) -> BoundChangeRequest:
    request = (
        raw_request
        if isinstance(raw_request, ChangeCertificationRequest)
        else ChangeCertificationRequest.from_json(raw_request)
    )
    if request.base_oid != trusted_target_oid:
        raise ValueError("request base must equal the trusted target commit")
    if (
        base.kind != "git_commit"
        or candidate.kind != "git_commit"
        or base.commit != trusted_target_oid
        or candidate.commit != request.candidate_oid
        or not base.tree
        or not candidate.tree
        or base.object_format != candidate.object_format
    ):
        raise ValueError("captured source identities do not match the change request")
    for source in (base, candidate):
        commit, tree, parents = _commit_metadata(
            project.root, source.commit, source.object_format
        )
        if (commit, tree, parents) != (source.commit, source.tree, source.parents):
            raise ValueError("captured commit metadata differs from its Git object")
    if not git_ancestor(project, trusted_target_oid, candidate):
        raise ValueError("candidate must descend from the trusted target commit")

    target_files = {file.path: file for file in base.files}
    candidate_files = {file.path: file for file in candidate.files}
    accepted_file = _required_file(target_files, _ACCEPTED_PATH, "accepted-intents document")
    accepted_raw = _strict_json(accepted_file, "accepted-intents document")
    validate("accepted intents", _ACCEPTED_SCHEMA, accepted_raw)
    if request.declared_intent_sha256 not in accepted_raw["accepted"]:
        raise ValueError("intent revision is not accepted by the trusted target")

    revision_file = _required_file(
        target_files,
        f"{_INTENT_DIRECTORY}/{request.declared_intent_sha256}.v1.json",
        "intent revision",
    )
    for label, target_file in (
        ("accepted-intents document", accepted_file),
        ("intent revision", revision_file),
    ):
        candidate_file = candidate_files.get(target_file.path)
        if candidate_file is None or candidate_file.content != target_file.content:
            raise ValueError(f"candidate {label} differs from trusted target")
    revision_raw = _strict_json(revision_file, "intent revision")
    revision = IntentRevision.from_json(revision_raw)
    if revision.digest != request.declared_intent_sha256:
        raise ValueError("intent revision digest does not match its accepted identity")

    for artifact in revision.artifacts:
        target_artifact = _required_file(
            target_files, artifact.path, f"target {artifact.role} artifact"
        )
        candidate_artifact = _required_file(
            candidate_files, artifact.path, f"candidate {artifact.role} artifact"
        )
        if target_artifact.raw_sha256 != artifact.sha256:
            raise ValueError(
                f"target {artifact.role} artifact hash does not match accepted intent"
            )
        if candidate_artifact.raw_sha256 != artifact.sha256:
            raise ValueError(
                f"candidate {artifact.role} artifact differs from accepted intent"
            )

    entailment = intent_entails_claim(revision.contract, request.claim)
    if entailment.kind is not EntailmentKind.ENTAILED:
        details = []
        if entailment.kind is EntailmentKind.VACUOUS:
            details.append("accepted intent has no admissible inputs")
        elif entailment.kind is EntailmentKind.UNKNOWN:
            details.append("request claim domain differs from accepted intent")
        if entailment.input_witness is not None:
            details.append(f"legal input {entailment.input_witness!r} is excluded")
        if entailment.output_witness is not None:
            input_token, output_token = entailment.output_witness
            details.append(
                f"legal output {output_token!r} for input {input_token!r} is excluded"
            )
        detail = f": {'; '.join(details)}" if details else ""
        raise ValueError(f"request claim is not entailed by accepted intent ({entailment.kind.value}){detail}")

    return BoundChangeRequest(
        request=request,
        intent=revision,
        target_tree_oid=base.tree,
        candidate_tree_oid=candidate.tree,
        accepted_intents_blob_oid=accepted_file.blob_oid,
        intent_revision_blob_oid=revision_file.blob_oid,
        base_capture_sha256=base.digest,
        candidate_capture_sha256=candidate.digest,
    )


def preflight_change_request(
    project: Project, raw_request: Any, *, trusted_target_oid: str
) -> BoundChangeRequest:
    bound, _, _ = _prepare_change_request(
        project, raw_request, trusted_target_oid=trusted_target_oid
    )
    return bound

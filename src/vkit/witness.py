from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypeAlias

from .evidence import _retained_path
from .inputs import canonical_digest
from .intent import BoundChangeRequest, _unique_object
from .schemas import SchemaValidationError, validate

_SCHEMA = "witness.v1.json"


class WitnessError(ValueError):
    pass


@dataclass(frozen=True)
class WitnessBinding:
    request_sha256: str
    claim_sha256: str
    intent_sha256: str
    base_capture_sha256: str
    candidate_capture_sha256: str
    codec_sha256: str
    observation_sha256: str
    environment_sha256: str
    runner_identity_sha256: str
    runtime_identity_sha256: str

    @classmethod
    def from_bound_request(
        cls,
        bound: BoundChangeRequest,
        *,
        runner_identity_sha256: str,
        runtime_identity_sha256: str,
    ) -> WitnessBinding:
        claim = bound.request.claim
        domain = claim.domain
        if bound.request.declared_intent_sha256 != bound.intent.digest:
            raise WitnessError("bound request does not name its accepted intent revision")
        if domain.to_json() != bound.intent.contract.domain.to_json():
            raise WitnessError("bound request and accepted intent use different finite domains")
        return cls(
            request_sha256=bound.digest,
            claim_sha256=canonical_digest(claim.to_json()),
            intent_sha256=bound.intent.digest,
            base_capture_sha256=bound.base_capture_sha256,
            candidate_capture_sha256=bound.candidate_capture_sha256,
            codec_sha256=domain.codec_sha256,
            observation_sha256=domain.observation_sha256,
            environment_sha256=domain.environment_sha256,
            runner_identity_sha256=runner_identity_sha256,
            runtime_identity_sha256=runtime_identity_sha256,
        )

    def to_json(self) -> dict[str, str]:
        fields = {
            "request_sha256": self.request_sha256,
            "claim_sha256": self.claim_sha256,
            "intent_sha256": self.intent_sha256,
            "base_capture_sha256": self.base_capture_sha256,
            "candidate_capture_sha256": self.candidate_capture_sha256,
            "codec_sha256": self.codec_sha256,
            "observation_sha256": self.observation_sha256,
            "environment_sha256": self.environment_sha256,
            "runner_identity_sha256": self.runner_identity_sha256,
            "runtime_identity_sha256": self.runtime_identity_sha256,
        }
        return {**fields, "binding_sha256": canonical_digest(fields)}

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> WitnessBinding:
        names = (
            "request_sha256", "claim_sha256", "intent_sha256", "base_capture_sha256",
            "candidate_capture_sha256", "codec_sha256", "observation_sha256",
            "environment_sha256", "runner_identity_sha256", "runtime_identity_sha256",
        )
        values = {name: raw[name] for name in names}
        binding = cls(**values)
        if raw != binding.to_json():
            raise WitnessError("witness binding digest does not match its identities")
        return binding


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str
    media_type: str

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "media_type": self.media_type}

    @classmethod
    def from_json(cls, raw: dict[str, str]) -> ArtifactRef:
        return cls(raw["path"], raw["sha256"], raw["media_type"])


@dataclass(frozen=True)
class ProofRejected:
    binding: WitnessBinding
    diagnostic: ArtifactRef

    def to_json(self) -> dict[str, object]:
        return {"schema_version": 1, "kind": "proof_rejected", "binding": self.binding.to_json(),
                "diagnostic": self.diagnostic.to_json()}


@dataclass(frozen=True)
class Unknown:
    binding: WitnessBinding
    reason: Literal["bound_exhausted", "timeout", "interrupted", "unsupported", "incomplete"]

    def to_json(self) -> dict[str, object]:
        return {"schema_version": 1, "kind": "unknown", "binding": self.binding.to_json(),
                "reason": self.reason}


@dataclass(frozen=True)
class InfrastructureFailure:
    binding: WitnessBinding
    stage: Literal["engine", "import", "compile", "native_playback", "protocol"]
    message: str
    diagnostic: ArtifactRef | None = None

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1, "kind": "infrastructure_failure", "binding": self.binding.to_json(),
            "stage": self.stage, "message": self.message,
        }
        if self.diagnostic is not None:
            result["diagnostic"] = self.diagnostic.to_json()
        return result


@dataclass(frozen=True)
class ModelReplayNotAttempted:
    def to_json(self) -> dict[str, str]:
        return {"kind": "not_attempted"}


@dataclass(frozen=True)
class ModelReplaySameEngineOnly:
    diagnostic: ArtifactRef

    def to_json(self) -> dict[str, object]:
        return {"kind": "same_engine_only", "diagnostic": self.diagnostic.to_json()}


ModelReplay: TypeAlias = ModelReplayNotAttempted | ModelReplaySameEngineOnly


@dataclass(frozen=True)
class ModelWitness:
    binding: WitnessBinding
    model: ArtifactRef
    config: ArtifactRef
    property: str
    trace: ArtifactRef
    replay: ModelReplay

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 1, "kind": "model_witness", "binding": self.binding.to_json(),
            "model": self.model.to_json(), "config": self.config.to_json(),
            "property": self.property, "trace": self.trace.to_json(), "replay": self.replay.to_json(),
        }


@dataclass(frozen=True)
class PlaybackNotAttempted:
    def to_json(self) -> dict[str, str]:
        return {"kind": "not_attempted"}


@dataclass(frozen=True)
class PlaybackNative:
    runtime_identity_sha256: str
    runtime_identity: ArtifactRef
    observation: ArtifactRef
    disposition: Literal["reproduced", "not_reproduced"]

    def to_json(self) -> dict[str, object]:
        return {
            "kind": "native", "runtime_identity_sha256": self.runtime_identity_sha256,
            "runtime_identity": self.runtime_identity.to_json(),
            "observation": self.observation.to_json(), "disposition": self.disposition,
        }


@dataclass(frozen=True)
class PlaybackUnknown:
    reason: Literal["timeout", "interrupted", "unsupported", "incomplete"]

    def to_json(self) -> dict[str, str]:
        return {"kind": "unknown", "reason": self.reason}


@dataclass(frozen=True)
class PlaybackInfrastructureFailure:
    stage: Literal["compile", "native_playback", "protocol"]
    message: str
    diagnostic: ArtifactRef | None = None

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": "infrastructure_failure", "stage": self.stage,
                                     "message": self.message}
        if self.diagnostic is not None:
            result["diagnostic"] = self.diagnostic.to_json()
        return result


EnginePlayback: TypeAlias = (
    PlaybackNotAttempted | PlaybackNative | PlaybackUnknown | PlaybackInfrastructureFailure
)


@dataclass(frozen=True)
class EngineCounterexample:
    binding: WitnessBinding
    counterexample: ArtifactRef
    input: ArtifactRef
    playback: EnginePlayback

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 1, "kind": "engine_counterexample", "binding": self.binding.to_json(),
            "counterexample": self.counterexample.to_json(), "input": self.input.to_json(),
            "playback": self.playback.to_json(),
        }


Witness: TypeAlias = ProofRejected | Unknown | InfrastructureFailure | ModelWitness | EngineCounterexample


def encode_witness(witness: Witness) -> bytes:
    raw = witness.to_json()
    _validate_schema(raw)
    return (json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def parse_witness(raw: bytes, *, expected: WitnessBinding, artifact_root: Path) -> Witness:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WitnessError(f"witness is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WitnessError("witness must be a JSON object")
    _validate_schema(value)
    binding = WitnessBinding.from_json(value["binding"])
    if binding != expected:
        raise WitnessError("witness binding differs from the trusted request and runner context")
    witness = _typed_witness(value, binding)
    for artifact in _artifact_refs(witness):
        _validate_artifact(artifact, artifact_root)
    return witness


def import_kani_u32_input(raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WitnessError("Kani playback artifact is not UTF-8") from exc
    if "kani::concrete_playback_run(concrete_vals, regression);" not in text:
        raise WitnessError("Kani playback artifact is not the regression harness")
    if re.search(r"\bfn\s+kani_concrete_playback_regression_\d+\s*\(", text) is None:
        raise WitnessError("Kani playback artifact is not the concrete regression test")
    sections = re.findall(
        r"let concrete_vals:\s*Vec<Vec<u8>>\s*=\s*vec!\[(.*?)\];", text, re.DOTALL
    )
    if len(sections) != 1:
        raise WitnessError("Kani playback artifact must contain one concrete input vector")
    body = sections[0]
    if not re.fullmatch(r"\s*(?://[^\r\n]*\r?\n\s*)?vec!\[\s*[0-9,\s]+\s*\]\s*,?\s*", body):
        raise WitnessError("Kani playback artifact must contain one concrete input vector")
    vectors = re.findall(r"vec!\[([^\]]*)\]", body)
    if len(vectors) != 1 or not re.fullmatch(r"[\s0-9,]*", vectors[0]):
        raise WitnessError("Kani playback input is not one declared u32 value")
    try:
        octets = [int(part.strip()) for part in vectors[0].split(",") if part.strip()]
    except ValueError as exc:
        raise WitnessError("Kani playback input contains a non-integer byte") from exc
    if len(octets) != 4 or any(value < 0 or value > 255 for value in octets):
        raise WitnessError("Kani playback input must be exactly one u32 little-endian value")
    return bytes(octets)


def _validate_schema(raw: dict[str, object]) -> None:
    try:
        validate("witness", _SCHEMA, raw)
    except SchemaValidationError as exc:
        raise WitnessError(exc.reason) from exc


def _typed_witness(raw: dict[str, object], binding: WitnessBinding) -> Witness:
    kind = raw["kind"]
    if kind == "proof_rejected":
        return ProofRejected(binding, ArtifactRef.from_json(raw["diagnostic"]))
    if kind == "unknown":
        return Unknown(binding, raw["reason"])
    if kind == "infrastructure_failure":
        diagnostic = raw.get("diagnostic")
        return InfrastructureFailure(
            binding, raw["stage"], raw["message"],
            ArtifactRef.from_json(diagnostic) if diagnostic is not None else None,
        )
    if kind == "model_witness":
        replay = raw["replay"]
        if replay["kind"] == "not_attempted":
            typed_replay: ModelReplay = ModelReplayNotAttempted()
        else:
            typed_replay = ModelReplaySameEngineOnly(ArtifactRef.from_json(replay["diagnostic"]))
        return ModelWitness(
            binding, ArtifactRef.from_json(raw["model"]), ArtifactRef.from_json(raw["config"]),
            raw["property"], ArtifactRef.from_json(raw["trace"]), typed_replay,
        )
    playback = raw["playback"]
    if playback["kind"] == "not_attempted":
        typed_playback: EnginePlayback = PlaybackNotAttempted()
    elif playback["kind"] == "native":
        if playback["runtime_identity_sha256"] != binding.runtime_identity_sha256:
            raise WitnessError("native playback runtime differs from the trusted runner context")
        runtime_identity = ArtifactRef.from_json(playback["runtime_identity"])
        if runtime_identity.sha256 != playback["runtime_identity_sha256"]:
            raise WitnessError("native playback runtime identity artifact does not match its digest")
        typed_playback = PlaybackNative(
            playback["runtime_identity_sha256"], runtime_identity,
            ArtifactRef.from_json(playback["observation"]), playback["disposition"],
        )
    elif playback["kind"] == "unknown":
        typed_playback = PlaybackUnknown(playback["reason"])
    else:
        diagnostic = playback.get("diagnostic")
        typed_playback = PlaybackInfrastructureFailure(
            playback["stage"], playback["message"],
            ArtifactRef.from_json(diagnostic) if diagnostic is not None else None,
        )
    return EngineCounterexample(
        binding, ArtifactRef.from_json(raw["counterexample"]), ArtifactRef.from_json(raw["input"]),
        typed_playback,
    )


def _artifact_refs(witness: Witness) -> tuple[ArtifactRef, ...]:
    if isinstance(witness, ProofRejected):
        return (witness.diagnostic,)
    if isinstance(witness, InfrastructureFailure):
        return (witness.diagnostic,) if witness.diagnostic else ()
    if isinstance(witness, ModelWitness):
        refs = (witness.model, witness.config, witness.trace)
        if isinstance(witness.replay, ModelReplaySameEngineOnly):
            return (*refs, witness.replay.diagnostic)
        return refs
    if isinstance(witness, EngineCounterexample):
        refs = (witness.counterexample, witness.input)
        if isinstance(witness.playback, PlaybackNative):
            return (*refs, witness.playback.runtime_identity, witness.playback.observation)
        if isinstance(witness.playback, PlaybackInfrastructureFailure) and witness.playback.diagnostic:
            return (*refs, witness.playback.diagnostic)
        return refs
    return ()


def _validate_artifact(ref: ArtifactRef, root: Path) -> None:
    posix = PurePosixPath(ref.path)
    if (not ref.path or posix.is_absolute() or posix.as_posix() != ref.path
            or any(part in {"", ".", ".."} for part in posix.parts)
            or "\\" in ref.path or ":" in ref.path):
        raise WitnessError("artifact path is not a normalized retained relative path")
    try:
        path = _retained_path(root, ref.path)
    except (OSError, ValueError) as exc:
        raise WitnessError(f"artifact cannot be read from the retained bundle: {ref.path}") from exc
    if not path.is_file():
        raise WitnessError(f"artifact is not a regular file in the retained bundle: {ref.path}")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise WitnessError(f"artifact cannot be read from the retained bundle: {ref.path}") from exc
    if hashlib.sha256(content).hexdigest() != ref.sha256:
        raise WitnessError(f"artifact bytes do not match the retained hash: {ref.path}")

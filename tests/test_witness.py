from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from vkit.certification import (
    ChangeCertificationRequest,
    FiniteContract,
    FiniteDomain,
    IntentRevision,
    OutputRow,
    SemanticArtifact,
)
from vkit.intent import BoundChangeRequest
from vkit.witness import (
    ArtifactRef,
    EngineCounterexample,
    InfrastructureFailure,
    ModelReplayNotAttempted,
    ModelReplaySameEngineOnly,
    ModelWitness,
    PlaybackNative,
    ProofRejected,
    Unknown,
    WitnessBinding,
    WitnessError,
    encode_witness,
    import_kani_u32_input,
    parse_witness,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


_RUNTIME_IDENTITY = b'{"compiler":"fixture rustc"}'


def _binding(**changes: str) -> WitnessBinding:
    domain = FiniteDomain(
        "SC-03/u32", ("10",), ("before=10,after=9",), "a" * 64, "b" * 64, "c" * 64
    )
    contract = FiniteContract(domain, ("10",), (OutputRow("10", ("before=10,after=9",)),))
    intent = IntentRevision(
        "SC-03 fixture", "The upper cap is ten.", contract,
        (
            SemanticArtifact("binding", "binding.json", "d" * 64),
            SemanticArtifact("codec", "codec.json", domain.codec_sha256),
            SemanticArtifact("observation", "observation.json", domain.observation_sha256),
            SemanticArtifact("environment", "environment.json", domain.environment_sha256),
        ),
    )
    request = ChangeCertificationRequest("0" * 40, "1" * 40, intent.digest, contract)
    bound = BoundChangeRequest(
        request, intent, "2" * 40, "3" * 40, "4" * 40, "5" * 40, "6" * 64, "7" * 64
    )
    values = WitnessBinding.from_bound_request(
        bound, runner_identity_sha256="8" * 64, runtime_identity_sha256=_sha(_RUNTIME_IDENTITY)
    ).to_json()
    values.pop("binding_sha256")
    values.update(changes)
    return WitnessBinding(**values)


def _artifact(root, name: str, content: bytes, media_type: str = "application/octet-stream") -> ArtifactRef:
    path = root / name
    path.write_bytes(content)
    return ArtifactRef(name, _sha(content), media_type)


def _engine_witness(root, binding: WitnessBinding):
    return EngineCounterexample(
        binding,
        _artifact(root, "engine.txt", b"bounded engine input: 10"),
        _artifact(root, "input.u32le", b"\x0a\x00\x00\x00"),
        PlaybackNative(
            binding.runtime_identity_sha256,
            _artifact(root, "runtime-identity.json", _RUNTIME_IDENTITY, "application/json"),
            _artifact(root, "observation.json", b'{"input":10,"before":10,"after":9}\n', "application/json"),
            "reproduced",
        ),
    )


@pytest.mark.parametrize("kind", ["proof", "unknown", "infra", "model", "engine"])
def test_closed_outcomes_round_trip_with_retained_artifact_bytes(tmp_path, kind):
    binding = _binding()
    if kind == "proof":
        witness = ProofRejected(binding, _artifact(tmp_path, "diagnostic.txt", b"claim rejected"))
    elif kind == "unknown":
        witness = Unknown(binding, "bound_exhausted")
    elif kind == "infra":
        witness = InfrastructureFailure(binding, "engine", "tool could not start")
    elif kind == "model":
        witness = ModelWitness(
            binding,
            _artifact(tmp_path, "model.tla", b"---- MODULE CounterModel ----\n", "text/plain"),
            _artifact(tmp_path, "model.cfg", b"INVARIANT SafeInvariant\n", "text/plain"),
            "SafeInvariant",
            _artifact(tmp_path, "trace.json", b'{"states":[0,1]}\n', "application/json"),
            ModelReplaySameEngineOnly(_artifact(tmp_path, "model-replay.txt", b"TLC replayed trace")),
        )
    else:
        witness = _engine_witness(tmp_path, binding)
    assert parse_witness(encode_witness(witness), expected=binding, artifact_root=tmp_path) == witness


def test_model_replay_is_model_only_and_proof_rejection_has_no_playback(tmp_path):
    binding = _binding()
    model = ModelWitness(
        binding,
        _artifact(tmp_path, "model.tla", b"model", "text/plain"),
        _artifact(tmp_path, "model.cfg", b"config", "text/plain"),
        "Invariant",
        _artifact(tmp_path, "trace.json", b"{}", "application/json"), ModelReplayNotAttempted(),
    ).to_json()
    model["replay"] = {"kind": "native", "runtime_identity_sha256": binding.runtime_identity_sha256}
    with pytest.raises(WitnessError, match="replay|additional properties|oneOf"):
        parse_witness(json.dumps(model).encode(), expected=binding, artifact_root=tmp_path)

    rejected = ProofRejected(binding, _artifact(tmp_path, "diagnostic.txt", b"reject")).to_json()
    rejected["playback"] = {"kind": "not_attempted"}
    with pytest.raises(WitnessError, match="additional properties|playback|oneOf"):
        parse_witness(json.dumps(rejected).encode(), expected=binding, artifact_root=tmp_path)


def test_model_and_config_bytes_are_bound_as_retained_artifacts(tmp_path):
    binding = _binding()
    witness = ModelWitness(
        binding, _artifact(tmp_path, "model.tla", b"MODULE M", "text/plain"),
        _artifact(tmp_path, "model.cfg", b"INVARIANT I", "text/plain"), "I",
        _artifact(tmp_path, "trace.json", b"{}", "application/json"), ModelReplayNotAttempted(),
    )
    raw = encode_witness(witness)
    (tmp_path / "model.cfg").write_bytes(b"INVARIANT Other")
    with pytest.raises(WitnessError, match="hash"):
        parse_witness(raw, expected=binding, artifact_root=tmp_path)


def test_duplicate_keys_and_unknown_discriminants_are_rejected(tmp_path):
    binding = _binding()
    duplicate = b'{"schema_version":1,"schema_version":1,"kind":"unknown"}'
    with pytest.raises(WitnessError, match="duplicate JSON"):
        parse_witness(duplicate, expected=binding, artifact_root=tmp_path)
    unknown_kind = {"schema_version": 1, "kind": "passed", "binding": binding.to_json()}
    with pytest.raises(WitnessError):
        parse_witness(json.dumps(unknown_kind).encode(), expected=binding, artifact_root=tmp_path)


def test_sha256_with_trailing_newline_is_rejected_by_schema():
    binding = _binding(request_sha256="f" * 64 + "\n")
    with pytest.raises(WitnessError, match="request_sha256"):
        encode_witness(ProofRejected(binding, ArtifactRef("diagnostic.txt", "a" * 64, "text/plain")))


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_sha256", "f" * 64),
        ("claim_sha256", "f" * 64),
        ("intent_sha256", "f" * 64),
        ("base_capture_sha256", "f" * 64),
        ("candidate_capture_sha256", "f" * 64),
        ("codec_sha256", "f" * 64),
        ("observation_sha256", "f" * 64),
        ("environment_sha256", "f" * 64),
        ("runner_identity_sha256", "f" * 64),
        ("runtime_identity_sha256", "f" * 64),
    ],
)
def test_mismatched_trusted_request_or_execution_context_is_rejected(tmp_path, field, value):
    binding = _binding()
    witness = _engine_witness(tmp_path, binding)
    expected = _binding(**{field: value})
    with pytest.raises(WitnessError, match="binding"):
        parse_witness(encode_witness(witness), expected=expected, artifact_root=tmp_path)


def test_tampered_retained_bytes_and_traversal_are_rejected(tmp_path):
    binding = _binding()
    witness = _engine_witness(tmp_path, binding)
    raw = encode_witness(witness)
    (tmp_path / "input.u32le").write_bytes(b"\x09\x00\x00\x00")
    with pytest.raises(WitnessError, match="hash"):
        parse_witness(raw, expected=binding, artifact_root=tmp_path)

    value = witness.to_json()
    value["input"]["path"] = "../input.u32le"
    value["input"]["sha256"] = _sha(b"\x0a\x00\x00\x00")
    with pytest.raises(WitnessError):
        parse_witness(json.dumps(value).encode(), expected=binding, artifact_root=tmp_path)


def test_retained_artifact_symlink_is_rejected(tmp_path):
    binding = _binding()
    target = tmp_path / "target.txt"
    target.write_bytes(b"diagnostic")
    link = tmp_path / "diagnostic.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    witness = ProofRejected(binding, ArtifactRef("diagnostic.txt", _sha(b"diagnostic"), "text/plain"))
    with pytest.raises(WitnessError, match="retained bundle"):
        parse_witness(encode_witness(witness), expected=binding, artifact_root=tmp_path)


def test_retained_fifo_is_rejected_before_reading(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    fifo = tmp_path / "diagnostic.txt"
    try:
        os.mkfifo(fifo)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"FIFO creation unavailable: {exc}")
    binding = _binding()
    witness = ProofRejected(binding, ArtifactRef("diagnostic.txt", "a" * 64, "text/plain"))
    with pytest.raises(WitnessError, match="regular file"):
        parse_witness(encode_witness(witness), expected=binding, artifact_root=tmp_path)


def test_native_playback_runtime_must_match_trusted_context(tmp_path):
    binding = _binding()
    witness = _engine_witness(tmp_path, binding).to_json()
    witness["playback"]["runtime_identity_sha256"] = "f" * 64
    with pytest.raises(WitnessError, match="runtime"):
        parse_witness(json.dumps(witness).encode(), expected=binding, artifact_root=tmp_path)

    witness = _engine_witness(tmp_path, binding).to_json()
    witness["playback"]["runtime_identity"]["sha256"] = "f" * 64
    with pytest.raises(WitnessError, match="runtime identity artifact"):
        parse_witness(json.dumps(witness).encode(), expected=binding, artifact_root=tmp_path)


def test_kani_u32_importer_accepts_one_typed_concrete_value_only():
    raw = (Path(__file__).parents[1] / "examples/change-certification/witness-fixtures/kani-u32-regression.txt").read_bytes()
    assert import_kani_u32_input(raw) == b"\x0a\x00\x00\x00"
    with pytest.raises(WitnessError, match="one concrete input"):
        import_kani_u32_input(raw.replace(b"vec![10, 0, 0, 0]", b"vec![10, 0, 0, 0], vec![9, 0, 0, 0]"))

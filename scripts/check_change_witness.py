from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from vkit.certification import (
    ChangeCertificationRequest,
    FiniteContract,
    FiniteDomain,
    IntentRevision,
    OutputRow,
    SemanticArtifact,
)
from vkit.evidence import _retained_path, is_link
from vkit.inputs import canonical_digest, capture_git_commit
from vkit.intent import preflight_change_request
from vkit.paths import open_project
from vkit.proc import run as run_process
from vkit.witness import (
    ArtifactRef,
    EngineCounterexample,
    PlaybackNative,
    WitnessBinding,
    encode_witness,
    import_kani_u32_input,
    parse_witness,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/change-certification/witness-fixtures"
DEFAULT_OUTPUT = ROOT / "tmp/change-witness"
MAX_PROCESS_OUTPUT = 1_000_000


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Qualification:
    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self.logs = run_root / "process"
        self.logs.mkdir()
        self.calls = 0

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        input_path: Path | None = None,
        timeout_seconds: float = 30,
    ) -> ProcessResult:
        self.calls += 1
        stdout_path = self.logs / f"{self.calls:03}-stdout.bin"
        stderr_path = self.logs / f"{self.calls:03}-stderr.bin"
        done = run_process(
            argv,
            cwd=cwd,
            env=env,
            stdin_path=input_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
            max_output_bytes=MAX_PROCESS_OUTPUT,
        )
        if done.launch_error or done.timed_out or done.cancelled or done.output_limited:
            raise RuntimeError(
                f"process did not complete within its limits: {Path(argv[0]).name}: {done}"
            )
        return ProcessResult(done.code if done.code is not None else -1,
                             stdout_path.read_bytes(), stderr_path.read_bytes())


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write_artifact(bundle: Path, relative: str, content: bytes,
                    media_type: str = "application/octet-stream") -> ArtifactRef:
    path = _retained_path(bundle, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    _retained_path(bundle, relative).write_bytes(content)
    return ArtifactRef(relative, sha256(content), media_type)


def _git_text(qualification: Qualification, *args: str, cwd: Path, env: dict[str, str]) -> str:
    done = qualification.run(["git", *args], cwd=cwd, env=env)
    if done.returncode:
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip()
                           or f"git {' '.join(args)} failed")
    return done.stdout.decode("utf-8", "replace").strip()


def _git_commit(qualification: Qualification, repo: Path, env: dict[str, str], message: str) -> str:
    done = qualification.run(
        ["git", "-c", "user.name=VKit Witness Fixture", "-c",
         "user.email=witness@vkit.invalid", "commit", "-qm", message],
        cwd=repo, env=env,
    )
    if done.returncode:
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip()
                           or f"fixture commit failed: {message}")
    return _git_text(qualification, "rev-parse", "HEAD", cwd=repo, env=env)


def _captured_file(capture: Any, relative: str) -> bytes:
    files = [file for file in capture.files if file.path == relative]
    if len(files) != 1:
        raise RuntimeError(f"Git capture did not contain exactly {relative}")
    return files[0].content


def _materialize_capture(bundle: Path, capture: Any, relative: str, name: str) -> bytes:
    content = _captured_file(capture, relative)
    path = _retained_path(bundle, f"source/{name}/cap.rs")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if path.read_bytes() != content:
        raise RuntimeError(f"materialized source bytes changed: {name}")
    return content


def _build_fixture(
    qualification: Qualification,
    run_root: Path,
    env: dict[str, str],
    *,
    source_before: bytes,
    source_after: bytes,
    artifacts: dict[str, bytes],
    contract: FiniteContract,
) -> tuple[Path, str, str, IntentRevision]:
    repo = run_root / "target"
    repo.mkdir()
    init = qualification.run(["git", "init", "-q"], cwd=repo, env=env)
    if init.returncode:
        raise RuntimeError("could not initialize disposable Git target")
    environment_path = "verification/change-intents/artifacts/environment-identity.json"
    artifact_paths = {
        "binding": "verification/change-intents/artifacts/u32-cap-binding.json",
        "codec": "verification/change-intents/artifacts/u32-le.codec.json",
        "observation": "verification/change-intents/artifacts/u32-cap-observation.v1.json",
        "environment": environment_path,
    }
    for role, path in artifact_paths.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifacts[role])
    intent = IntentRevision(
        "SC-03 fixture", "Return min(x, 10) for the supported u32 range.", contract,
        tuple(SemanticArtifact(role, artifact_paths[role], sha256(artifacts[role]))
              for role in ("binding", "codec", "observation", "environment")),
    )
    revision = repo / f"verification/change-intents/{intent.digest}.v1.json"
    revision.write_bytes(canonical_bytes(intent.to_json()) + b"\n")
    accepted = repo / "verification/change-intents/accepted.v1.json"
    accepted.write_bytes(canonical_bytes({"schema_version": 1, "accepted": [intent.digest]}) + b"\n")
    candidate_path = repo / "src/cap.rs"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_bytes(source_before)
    add = qualification.run(["git", "add", "."], cwd=repo, env=env)
    if add.returncode:
        raise RuntimeError("could not stage trusted target fixture")
    target_oid = _git_commit(qualification, repo, env, "trusted target fixture")
    candidate_path.write_bytes(source_after)
    add = qualification.run(["git", "add", "src/cap.rs"], cwd=repo, env=env)
    if add.returncode:
        raise RuntimeError("could not stage candidate source fixture")
    candidate_oid = _git_commit(qualification, repo, env, "candidate source fixture")
    return repo, target_oid, candidate_oid, intent


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify retained native witness playback.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.expanduser().absolute()
    if is_link(output) or (output.exists() and not output.is_dir()):
        raise RuntimeError("output path must be a plain directory")
    output.mkdir(parents=True, exist_ok=True)
    if is_link(output) or not output.resolve().is_dir():
        raise RuntimeError("output path is not a plain directory")
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    qualification = Qualification(run_root)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))]
    )
    rustup = shutil.which("rustup", path=env.get("PATH"))
    if rustup is None:
        raise RuntimeError("rustup must be available to resolve the installed Rust compiler")
    compiler_path = qualification.run([rustup, "which", "rustc"], cwd=ROOT, env=env)
    if compiler_path.returncode:
        raise RuntimeError("could not resolve the installed Rust compiler")
    compiler_binary = Path(compiler_path.stdout.decode("utf-8").strip()).resolve()
    if not compiler_binary.is_file() or is_link(compiler_binary):
        raise RuntimeError("resolved Rust compiler is not a regular file")
    compiler_version = qualification.run([str(compiler_binary), "-vV"], cwd=ROOT, env=env)
    if compiler_version.returncode:
        raise RuntimeError("resolved Rust compiler identity probe failed")
    compiler_text = compiler_version.stdout.decode("utf-8", "replace").strip()
    target = next((line[6:] for line in compiler_text.splitlines()
                   if line.startswith("host: ")), "unknown")

    fixture_files = {
        "base": FIXTURES / "u32-cap-before.rs",
        "candidate": FIXTURES / "u32-cap-after.rs",
        "playback": FIXTURES / "u32-playback.rs",
        "kani": FIXTURES / "kani-u32-regression.txt",
        "binding": FIXTURES / "u32-cap-binding.json",
        "codec": FIXTURES / "u32-le.codec.json",
        "observation": FIXTURES / "u32-cap-observation.v1.json",
    }
    fixture_bytes = {name: path.read_bytes() for name, path in fixture_files.items()}
    input_bytes = import_kani_u32_input(fixture_bytes["kani"])
    if len(input_bytes) != 4 or int.from_bytes(input_bytes, "little") != 10:
        raise RuntimeError("retained transcript fixture did not import the known u32 input")

    source_before = fixture_bytes["base"]
    source_after = fixture_bytes["candidate"].replace(b"cap_after", b"cap")
    if source_before == source_after or b"pub fn cap(" not in source_after:
        raise RuntimeError("candidate source fixture does not encode a distinct cap implementation")
    env_sha256 = canonical_digest({"variables": sorted(env.items()), "platform": platform.platform()})
    environment_bytes = canonical_bytes({"effective_environment_sha256": env_sha256, "target": target})
    codec_sha256 = sha256(fixture_bytes["codec"])
    observation_sha256 = sha256(fixture_bytes["observation"])
    environment_sha256 = sha256(environment_bytes)
    output_10 = '{"input":10,"before":10,"after":10}'
    output_9 = '{"input":10,"before":10,"after":9}'
    domain = FiniteDomain(
        "u32-cap", ("x=10",), (output_10, output_9),
        codec_sha256, observation_sha256, environment_sha256,
    )
    contract = FiniteContract(
        domain, ("x=10",), (OutputRow("x=10", (output_10,)),)
    )
    repo, target_oid, candidate_oid, intent = _build_fixture(
        qualification, run_root, env,
        source_before=source_before, source_after=source_after,
        artifacts={"binding": fixture_bytes["binding"], "codec": fixture_bytes["codec"],
                   "observation": fixture_bytes["observation"], "environment": environment_bytes},
        contract=contract,
    )
    request = ChangeCertificationRequest(target_oid, candidate_oid, intent.digest, contract)
    bound = preflight_change_request(
        open_project(repo), request.to_json(), trusted_target_oid=target_oid
    )
    if (bound.request.base_oid != target_oid or bound.request.candidate_oid != candidate_oid
            or bound.candidate_capture_sha256 == bound.base_capture_sha256):
        raise RuntimeError("preflight did not bind the distinct trusted target and candidate")

    base_capture = capture_git_commit(open_project(repo), target_oid)
    candidate_capture = capture_git_commit(open_project(repo), candidate_oid)
    if (base_capture.digest != bound.base_capture_sha256
            or candidate_capture.digest != bound.candidate_capture_sha256):
        raise RuntimeError("independent Git captures differ from preflight source captures")
    base_source = _materialize_capture(run_root, base_capture, "src/cap.rs", "base")
    candidate_source = _materialize_capture(run_root, candidate_capture, "src/cap.rs", "candidate")
    input_ref = _write_artifact(run_root, "input.u32le", input_bytes)
    counterexample_ref = _write_artifact(
        run_root, "kani-transcript-fixture.txt", fixture_bytes["kani"], "text/plain"
    )
    playback_path = _retained_path(run_root, "native/playback.rs")
    playback_path.parent.mkdir(parents=True)
    playback_path.write_bytes(fixture_bytes["playback"])
    base_path = _retained_path(run_root, "source/base/cap.rs")
    candidate_path = _retained_path(run_root, "source/candidate/cap.rs")
    binary_path = _retained_path(run_root, "native/u32-playback.exe" if os.name == "nt"
                                 else "native/u32-playback")
    compile_result = qualification.run(
        [str(compiler_binary), "--edition=2021", str(playback_path), "-o", str(binary_path)],
        cwd=run_root / "native", env=env,
    )
    if compile_result.returncode or not binary_path.is_file():
        raise RuntimeError("Rust native playback compilation failed: "
                           + compile_result.stderr.decode("utf-8", "replace"))
    runtime_identity = {
        "compiler_binary_path": str(compiler_binary),
        "compiler_binary_sha256": sha256(compiler_binary.read_bytes()),
        "compiler_version": compiler_text,
        "target": target,
        "environment_sha256": env_sha256,
        "base_source_sha256": sha256(base_source),
        "candidate_source_sha256": sha256(candidate_source),
        "playback_source_sha256": sha256(fixture_bytes["playback"]),
        "binary_sha256": sha256(binary_path.read_bytes()),
    }
    runtime_bytes = canonical_bytes(runtime_identity)
    runtime_sha256 = sha256(runtime_bytes)
    runtime_ref = _write_artifact(run_root, "runtime-identity.json", runtime_bytes, "application/json")
    expected = WitnessBinding.from_bound_request(
        bound,
        runner_identity_sha256=canonical_digest({
            "script_sha256": sha256(Path(__file__).read_bytes()),
            "witness_module_sha256": sha256((ROOT / "src/vkit/witness.py").read_bytes()),
            "schema_sha256": sha256((ROOT / "schemas/witness.v1.json").read_bytes()),
            "counterexample_fixture_sha256": counterexample_ref.sha256,
        }),
        runtime_identity_sha256=runtime_sha256,
    )
    playback = qualification.run(
        [str(binary_path)], cwd=run_root / "native", env=env, input_path=input_ref_path(run_root, input_ref),
    )
    if playback.returncode:
        raise RuntimeError("Rust native playback failed: "
                           + playback.stderr.decode("utf-8", "replace"))
    try:
        observation = json.loads(playback.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Rust playback did not emit valid JSON: {exc}") from exc
    Draft202012Validator(json.loads(fixture_bytes["observation"])).validate(observation)
    observation_ref = _write_artifact(run_root, "observation.json", playback.stdout, "application/json")
    if observation != {"input": 10, "before": 10, "after": 9}:
        raise RuntimeError(f"native pair did not reproduce expected after-9 behavior: {observation!r}")
    witness = EngineCounterexample(
        expected, counterexample_ref, input_ref,
        PlaybackNative(runtime_sha256, runtime_ref, observation_ref, "reproduced"),
    )
    witness_bytes = encode_witness(witness)
    witness_path = _retained_path(run_root, "witness.json")
    witness_path.write_bytes(witness_bytes)
    parsed = parse_witness(witness_path.read_bytes(), expected=expected, artifact_root=run_root)
    if not isinstance(parsed, EngineCounterexample):
        raise RuntimeError("native playback witness did not round-trip as an engine counterexample")
    if (base_path.read_bytes() != _captured_file(base_capture, "src/cap.rs")
            or candidate_path.read_bytes() != _captured_file(candidate_capture, "src/cap.rs")):
        raise RuntimeError("native compilation inputs differ from exact Git source captures")
    print(json.dumps({
        "result": "native_witness_reproduced",
        "input_u32": int.from_bytes(input_bytes, "little"),
        "observation": observation,
        "witness_sha256": sha256(witness_bytes),
        "binding_sha256": expected.to_json()["binding_sha256"],
        "target_commit": target_oid,
        "candidate_commit": candidate_oid,
        "base_capture_sha256": bound.base_capture_sha256,
        "candidate_capture_sha256": bound.candidate_capture_sha256,
        "runner_identity_sha256": expected.runner_identity_sha256,
        "runtime_identity_sha256": runtime_sha256,
        "domain_observations": [output_10, output_9],
        "accepted_outputs": [output_10],
        "kani_executed": False,
        "transcript_is_fixture": True,
        "source_proof": False,
        "candidate_behavior_checked_by_preflight": False,
        "run_directory": str(run_root),
    }, sort_keys=True))
    return 0


def input_ref_path(bundle: Path, ref: ArtifactRef) -> Path:
    path = _retained_path(bundle, ref.path)
    if sha256(path.read_bytes()) != ref.sha256:
        raise RuntimeError("retained native input bytes do not match their digest")
    return path


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"WITNESS_GATE_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)

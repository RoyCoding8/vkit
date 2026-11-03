from __future__ import annotations

import hashlib
import math
import platform
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from . import proc
from .evidence import _retained_path, policy_identity, retained_files, sha256
from .kani_result import classify_kani_result
from .kani_toolchain import KaniToolchain, KaniToolchainError, validate_kani_toolchain
from .paired import (
    _artifact, _artifact_mismatches, _fresh_directory, _json_bytes,
    _write_artifact,
)
from .rust_relational import RustRelationalProgram
from .rust_source import admit_rust_source

KaniExecutionKind = Literal["verified", "proof_rejected", "unknown", "infrastructure_failure"]
_OUTPUT_LIMIT = 1024 * 1024
_RESULT_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class KaniExecution:
    kind: KaniExecutionKind
    reason: str
    report_path: Path
    report_sha256: str

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind, "reason": self.reason,
            "report_path": str(self.report_path), "report_sha256": self.report_sha256,
            "source_proof": False, "approval_authority": False,
        }


def run_kani_relational(
    program: RustRelationalProgram, *, toolchain: KaniToolchain, output_dir: Path,
    timeout_seconds: float = 90, should_cancel: Callable[[], bool] = lambda: False,
) -> KaniExecution:
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
        raise ValueError("Kani process timeout must be finite and in (0, 300] seconds")
    root = _fresh_directory(output_dir)
    report = {
        "schema_version": 1, "scope": "captured_rust_kani_execution",
        "source_proof": False, "approval_authority": False,
        "native_compiler_correspondence": False, "os_sandbox": False,
        "staged_executable_modes_checked": False,
        "program_sha256": program.digest,
        "toolchain": toolchain.to_json(),
        "platform": {"system": platform.system(), "machine": platform.machine(),
                     "release": platform.release(), "libc": list(platform.libc_ver())},
        "executed": False, "toolchain_validated_before": False,
        "toolchain_validated_after": False,
        "limits": {"process_timeout_seconds": timeout_seconds,
                   "combined_stdout_stderr_bytes": _OUTPUT_LIMIT,
                   "export_bytes": _RESULT_LIMIT,
                   "scope": "Engine process only; capture, parser and toolchain validation are outside this deadline.",
                   "memory_quota": False, "filesystem_quota": False},
        "assumptions": [
            "Kani's default compiler and CBMC checks generate a complete property inventory for the admitted source.",
            "The selected Kani and Rust installation trees are pinned before and after execution.",
            "The host OS, kernel, Python runtime, host utilities and unpinned host libraries remain trusted.",
            "Pre/post identity checks do not prevent a concurrent writer from changing and restoring a tool during execution.",
            "A failed proof without a concrete input is not a native counterexample.",
            "Captured Git modes are retained; this profile compiles source bytes and never executes captured files.",
        ],
        "artifacts": [],
    }

    def retain(relative: str, content: bytes, media_type: str = "application/json") -> None:
        report["artifacts"].append(_write_artifact(root, relative, content, media_type).to_json())

    def finish(kind: KaniExecutionKind, reason: str) -> KaniExecution:
        if _artifact_mismatches(root, report):
            kind, reason = "infrastructure_failure", "retained_artifact_changed"
        report["outcome"] = {"kind": kind, "reason": reason}
        path = root / "report.json"
        with path.open("xb") as stream:
            stream.write(_json_bytes(report))
        return KaniExecution(kind, reason, path, sha256(path))

    if platform.system() != "Linux" or platform.machine() != "x86_64":
        return finish("unknown", "unsupported_platform")
    if should_cancel():
        return finish("unknown", "cancelled")
    try:
        retain("program.json", _json_bytes(program.to_json()))
        retain("implementation.json", _json_bytes(policy_identity()))
        manifests = validate_kani_toolchain(toolchain, should_cancel=should_cancel)
        for name, content in manifests:
            retain("toolchain/before/" + name, content, "application/x-ndjson")
        report["toolchain_validated_before"] = True
        engine = root / "engine"
        engine.mkdir()
        for side, capture, profile in (
            ("base", program.base, program.base_profile),
            ("candidate", program.candidate, program.candidate_profile),
        ):
            source_root = engine / side
            source_root.mkdir()
            for path in capture.materialize(source_root):
                report["artifacts"].append(_artifact(root, path, "application/octet-stream").to_json())
            retain(f"captures/{side}.json", _json_bytes(capture.to_json()))
            admitted = admit_rust_source((source_root / program.binding.source).read_bytes(), program.binding)
            if admitted != profile:
                return finish("infrastructure_failure", "source_admission_identity_changed")
        retain("engine/harness.rs", program.harness, "text/x-rust")
        if should_cancel():
            return finish("unknown", "cancelled")
        if not _sources_match(program, engine) or _artifact_mismatches(root, report):
            return finish("infrastructure_failure", "prepared_artifact_changed")
        argv = [str(toolchain.driver), "harness.rs", "--harness", "vkit_relational",
                "-Z", "unstable-options", "--export-json", "kani-results.json"]
        retain("invocation.json", _json_bytes({"argv": argv, "cwd": "engine",
                                             "environment": toolchain.environment()}))
        result = proc.run(
            argv, cwd=engine, stdout_path=root / "stdout.txt", stderr_path=root / "stderr.txt",
            timeout_seconds=timeout_seconds, env=toolchain.environment(),
            should_cancel=should_cancel, max_output_bytes=_OUTPUT_LIMIT,
        )
        report["executed"] = result.launch_error is None
        retain("process.json", _json_bytes(asdict(result)))
        for name in ("stdout.txt", "stderr.txt"):
            report["artifacts"].append(_artifact(root, root / name, "text/plain").to_json())
        if result.cancelled or should_cancel():
            return finish("unknown", "cancelled")
        if result.timed_out:
            return finish("unknown", "process_timeout")
        if result.output_limited:
            return finish("unknown", "process_output_limit")
        if result.launch_error:
            return finish("infrastructure_failure", "process_launch_failed")
        if result.code not in (0, 1):
            return finish("unknown", "unexpected_process_exit")
        if not _sources_match(program, engine) or _artifact_mismatches(root, report):
            return finish("infrastructure_failure", "execution_artifact_changed")
        after = validate_kani_toolchain(toolchain, should_cancel=should_cancel)
        for name, content in after:
            retain("toolchain/after/" + name, content, "application/x-ndjson")
        if manifests != after:
            return finish("infrastructure_failure", "toolchain_manifest_changed")
        report["toolchain_validated_after"] = True
        exported = _retained_path(root, "engine/kani-results.json")
        info = exported.lstat()
        if not stat.S_ISREG(info.st_mode):
            return finish("infrastructure_failure", "result_not_regular")
        if info.st_size > _RESULT_LIMIT:
            return finish("unknown", "result_size_limit")
        with exported.open("rb") as stream:
            raw = stream.read(_RESULT_LIMIT + 1)
        if len(raw) > _RESULT_LIMIT:
            return finish("unknown", "result_size_limit")
        ref = _artifact(root, exported, "application/json")
        report["artifacts"].append(ref.to_json())
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            return finish("infrastructure_failure", "result_changed_while_reading")
        outcome = classify_kani_result(raw, exit_code=result.code, harness=program.harness)
        retain("classification.json", _json_bytes(outcome.to_json()))
        if should_cancel():
            return finish("unknown", "cancelled")
        if not _sources_match(program, engine):
            return finish("infrastructure_failure", "execution_artifact_changed")
        return finish(outcome.kind, outcome.reason)
    except KaniToolchainError as exc:
        report["toolchain_error"] = {"reason": exc.reason, "message": str(exc)}
        kind = "unknown" if exc.reason in {"cancelled", "unsupported_platform"} else "infrastructure_failure"
        return finish(kind, "toolchain_" + exc.reason)
    except (OSError, ValueError) as exc:
        report["infrastructure_error"] = str(exc)
        return finish("infrastructure_failure", "execution_artifact_unavailable")


def _sources_match(program: RustRelationalProgram, engine: Path) -> bool:
    try:
        for side, capture in (("base", program.base), ("candidate", program.candidate)):
            root = engine / side
            actual = {path.relative_to(root).as_posix(): sha256(path) for path in retained_files(root)}
            if actual != {file.path: file.raw_sha256 for file in capture.files}:
                return False
        return True
    except (OSError, ValueError):
        return False

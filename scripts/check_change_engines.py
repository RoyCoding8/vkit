from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


CORPUS = ROOT / "examples/change-certification"
SNAPSHOTS = {
    "manifest.json": CORPUS / "manifest.json",
    "scalar.rs": CORPUS / "fixtures/scalar.rs",
    "experiment-kani.rs": CORPUS / "proofs/experiment-kani.rs",
    "experiment-verus-positive.rs": CORPUS / "proofs/experiment-verus-positive.rs",
    "experiment-verus-negative.rs": CORPUS / "proofs/experiment-verus-negative.rs",
}
KANI_EXPECTATIONS = {
    "refactor": (0, "proof_success", ("VERIFICATION:- SUCCESSFUL", "Complete - 1 successfully verified harnesses, 0 failures, 1 total.")),
    "regression": (1, "seeded_semantic_failure", ("VERIFICATION:- FAILED", "assertion failed: scalar::cap_before(x) == scalar::cap_after(x)")),
    "exhausted": (1, "unwind_exhaustion", ("VERIFICATION:- FAILED", "exhausted.unwind.0", "unwinding assertion loop 0")),
    "undefined_behavior": (1, "ub_safety_failure", (
        "VERIFICATION:- FAILED", "Check for `safety_check`", "Offset result and original pointer must point to the same allocation",
        "Concrete playback unit test for `undefined_behavior`")),
    "observation_mismatch": (1, "observation_mismatch", ("VERIFICATION:- FAILED", "assertion failed: before == after")),
}
VERUS_EXPECTATIONS = {
    "positive": (0, "proof_success", ("verification results:: 4 verified, 0 errors",)),
    "negative": (1, "seeded_semantic_failure", ("assertion failed", "verification results:: 3 verified, 1 errors")),
}


class ProbeStop(Exception):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_kani(name: str, returncode: int | None, output: str) -> str:
    if returncode is None:
        return "timeout"
    expected_code, classification, markers = KANI_EXPECTATIONS[name]
    if returncode == expected_code and all(marker in output for marker in markers):
        return classification
    return "infrastructure_failure"


def classify_verus(name: str, returncode: int | None, output: str) -> str:
    if returncode is None:
        return "timeout"
    expected_code, classification, markers = VERUS_EXPECTATIONS[name]
    if returncode == expected_code and all(marker in output for marker in markers):
        return classification
    return "infrastructure_failure"


def classify_playback(name: str, returncode: int | None, output: str) -> str:
    if returncode is None:
        return "timeout"
    failures = re.findall(r"(?m)^test (kani_concrete_playback_regression_\d+) \.\.\. FAILED$", output)
    summary = re.search(
        r"(?m)^test result: FAILED\. 0 passed; 1 failed; 0 ignored; 0 measured; \d+ filtered out; finished in \d+(?:\.\d+)?s$",
        output,
    )
    if (returncode != 0 and failures == [name] and summary
            and "assertion `left == right` failed" in output and "left: 10" in output and "right: 9" in output):
        return "native_regression_playback"
    return "infrastructure_failure"


def resolve_tool(value: str) -> Path | None:
    found = shutil.which(value)
    if found is None:
        return None
    path = Path(found).resolve()
    return path if path.is_file() and os.access(path, os.X_OK) else None


def write_report(output_dir: Path, report: dict[str, object]) -> None:
    (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def run_command(name: str, argv: list[str], output_dir: Path, timeout: float) -> tuple[dict[str, object], str]:
    from vkit.proc import run as run_process

    stdout_name, stderr_name = f"{name}.stdout.log", f"{name}.stderr.log"
    stdout_path, stderr_path = output_dir / stdout_name, output_dir / stderr_name
    record: dict[str, object] = {"name": name, "argv": argv, "cwd": str(output_dir), "returncode": None,
                                 "timed_out": False, "launch_error": None,
                                 "classification": "infrastructure_failure",
                                 "stdout_log": stdout_name, "stderr_log": stderr_name}
    result = run_process(argv, cwd=output_dir, stdout_path=stdout_path, stderr_path=stderr_path,
                         timeout_seconds=timeout)
    record.update({"returncode": result.code, "timed_out": result.timed_out,
                   "cancelled": result.cancelled, "launch_error": result.launch_error,
                   "output_limited": result.output_limited})
    if result.timed_out:
        record["classification"] = "timeout"
    elif result.code is not None and result.launch_error is None and not result.output_limited and not result.cancelled:
        record["classification"] = "completed"
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    return record, stdout + "\n" + stderr


def required_run(name: str, argv: list[str], output_dir: Path, timeout: float, classifier,
                 report: dict[str, object]) -> tuple[dict[str, object], str]:
    record, output = run_command(name, argv, output_dir, timeout)
    if record["classification"] == "completed":
        record["classification"] = classifier(record["returncode"], output)
    report["runs"].append(record)
    if record["classification"] == "timeout":
        raise ProbeStop("TIMEOUT", f"{name} timed out")
    if record["classification"] == "infrastructure_failure":
        raise ProbeStop("INFRASTRUCTURE_FAILURE", f"{name} return code or required diagnostic did not match")
    return record, output


def tool_identity(name: str, path: Path, output_dir: Path, timeout: float,
                  report: dict[str, object]) -> None:
    record, output = run_command(f"{name}-version", [str(path), "--version"], output_dir, timeout)
    report["runs"].append(record)
    if record["classification"] == "timeout":
        raise ProbeStop("TIMEOUT", f"{name} --version timed out")
    version = output.strip()
    if record["returncode"] != 0 or not version:
        raise ProbeStop("INFRASTRUCTURE_FAILURE", f"{name} --version failed")
    record["classification"] = "tool_identity_only"
    identity = {"path": str(path), "sha256": sha256(path), "version": version,
                "stdout_log": record["stdout_log"], "stderr_log": record["stderr_log"]}
    report["tools"][name] = identity


def regression_playback(output: str) -> tuple[str, str]:
    section = re.search(r"Concrete playback unit test for `regression`:\s*```[^\r\n]*\r?\n(.*?)```", output, re.S)
    if section is None:
        raise ProbeStop("INFRASTRUCTURE_FAILURE", "Kani did not emit the regression playback test")
    snippet = re.search(r"(?ms)(/// Test generated for harness `regression`.*?^})", section.group(1))
    if snippet is None or snippet.group(1).count("#[test]") != 1:
        raise ProbeStop("INFRASTRUCTURE_FAILURE", "Kani playback did not contain exactly one generated test")
    names = re.findall(r"(?m)^fn (kani_concrete_playback_regression_\d+)\(\)", snippet.group(1))
    if (len(names) != 1 or "kani::concrete_playback_run(concrete_vals, regression);" not in snippet.group(1)
            or not re.search(r"//\s*10\s*vec!\[\s*10,\s*0,\s*0,\s*0\s*\]", snippet.group(1))):
        raise ProbeStop("INFRASTRUCTURE_FAILURE", "Kani regression playback did not encode the required x=10 witness")
    return names[0], snippet.group(1)


def snapshot_inputs(output_dir: Path) -> dict[str, str]:
    hashes = {}
    for name, source in SNAPSHOTS.items():
        destination = output_dir / name
        destination.write_bytes(source.read_bytes())
        hashes[name] = sha256(destination)
    return hashes


def make_playback_wrapper(snapshot: Path, destination: Path, generated_test: str) -> str:
    destination.write_bytes(snapshot.read_bytes() + b"\n\n" + generated_test.strip().encode("utf-8") + b"\n")
    return sha256(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded Kani and Verus change-certification feasibility probes.")
    parser.add_argument("--kani", required=True)
    parser.add_argument("--verus", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            print("BLOCKED: output path must be an empty directory; choose a new directory.", file=sys.stderr)
            return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"status": "RUNNING", "scope_limit": (
        "Small finite Rust fixture probes only. Kani verifies the copied scalar function bodies. Verus proves a separate arithmetic adaptation. "
        "These results do not prove the VKit ledger, arbitrary Rust, or certification effectiveness."),
        "runs": [], "tools": {}, "snapshots_sha256": {}}
    try:
        kani = resolve_tool(args.kani)
        verus = resolve_tool(args.verus)
        if kani is None or verus is None:
            missing = ", ".join(name for name, value in (("kani", kani), ("verus", verus)) if value is None)
            raise ProbeStop("BLOCKED", f"missing executable(s): {missing}")

        report["snapshots_sha256"] = snapshot_inputs(output_dir)
        report["tools"] = {}
        tool_identity("kani", kani, output_dir, args.timeout_seconds, report)
        tool_identity("verus", verus, output_dir, args.timeout_seconds, report)

        kani_file = output_dir / "experiment-kani.rs"
        snapshot_wrapper_sha256 = report["snapshots_sha256"]["experiment-kani.rs"]
        for harness in KANI_EXPECTATIONS:
            if sha256(kani_file) != snapshot_wrapper_sha256:
                raise ProbeStop("INFRASTRUCTURE_FAILURE", "Kani changed the frozen wrapper snapshot")
            argv = [str(kani), str(kani_file), "--harness", harness,
                    "-Z", "concrete-playback", "--concrete-playback", "print"]
            record, output = required_run(f"kani-{harness}", argv, output_dir, args.timeout_seconds,
                                          lambda code, text, name=harness: classify_kani(name, code, text), report)
            if sha256(kani_file) != snapshot_wrapper_sha256:
                raise ProbeStop("INFRASTRUCTURE_FAILURE", "Kani changed the frozen wrapper snapshot")
            if harness == "regression":
                test_name, generated_test = regression_playback(output)
                playback_file = output_dir / "playback.rs"
                playback_wrapper_sha256 = make_playback_wrapper(kani_file, playback_file, generated_test)
                report["generated_regression_test"] = {"name": test_name, "wrapper": playback_file.name,
                    "snapshot_wrapper_sha256": snapshot_wrapper_sha256,
                    "playback_wrapper_sha256": playback_wrapper_sha256}
                required_run(
                    "kani-regression-playback",
                    [str(kani), "playback", str(playback_file), "-Z", "concrete-playback", "--", "--nocapture", test_name],
                    output_dir, args.timeout_seconds,
                    lambda code, text, name=test_name: classify_playback(name, code, text), report)
            if harness == "undefined_behavior":
                report["ub_safety"] = {"proof_result": record["classification"],
                                       "playback_generation": "GENERATED",
                                       "native_execution": "NOT_EXECUTED_UNSAFE"}

        for version in VERUS_EXPECTATIONS:
            verus_file = output_dir / f"experiment-verus-{version}.rs"
            argv = [str(verus), str(verus_file)]
            required_run(f"verus-{version}", argv, output_dir, args.timeout_seconds,
                         lambda code, text, name=version: classify_verus(name, code, text), report)

        report["status"] = "PASS_WITH_UB_PLAYBACK_NOT_EXECUTED"
        write_report(output_dir, report)
        print("PASS_WITH_UB_PLAYBACK_NOT_EXECUTED kani=5 regression_playback=1 verus=2 "
              "ub_proof=SAFETY_FAILURE playback=GENERATED native=NOT_EXECUTED_UNSAFE")
        return 0
    except ProbeStop as error:
        report["status"] = error.status
        report["error"] = str(error)
        write_report(output_dir, report)
        print(f"{error.status}: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        report["status"] = "INFRASTRUCTURE_FAILURE"
        report["error"] = str(error)
        write_report(output_dir, report)
        print(f"INFRASTRUCTURE_FAILURE: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

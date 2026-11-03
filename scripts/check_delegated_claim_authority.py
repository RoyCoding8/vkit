from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "verification" / "models"
CONFIG_DIR = MODEL_DIR / "delegated-claim"
DEFAULT_OUTPUT = ROOT / "tmp" / "delegation-tlc-output"
EXPECTED_JAR_DIGEST = ROOT / "examples" / "formal-ci" / "tla2tools.sha256"

CASES = {
    "good": None,
    "omit_untrusted_target": "AcceptedHasTrustedTargetBasis",
    "omit_target_identity": "AcceptedHasTrustedTargetBasis",
    "omit_entailment": "AcceptedHasEntailment",
    "omit_strict_strengthening": "AcceptedIsStrictStrengthening",
    "omit_complete_satisfaction": "AcceptedHasCompleteSatisfaction",
    "sample_authorizes_claim": "SamplesCannotAuthorize",
    "omit_meaningful_near_miss": "AcceptedHasRejectedMeaningfulNearMiss",
    "omit_fresh_receipt_cancel": "AcceptedHasFreshUncancelledReceipt",
    "omit_model_scope": "AcceptedUsesRestrictedModel",
    "allow_self_justifying_basis": "CurrentClaimCannotBeItsOwnBasis",
    "claim_approves_code": "ClaimAcceptanceDoesNotApproveCode",
}


class ToolchainUnavailable(RuntimeError):
    pass


def resolve_toolchain(java_arg: Path | None = None,
                      jar_arg: Path | None = None) -> tuple[Path, Path]:
    java_name = "java.exe" if os.name == "nt" else "java"
    if java_arg is not None:
        configured_java = java_arg.expanduser()
        located_java = shutil.which(str(configured_java))
        java = configured_java.resolve() if configured_java.is_file() else (
            Path(located_java).resolve() if located_java else None
        )
    else:
        java_home = os.environ.get("JAVA_HOME")
        java_home_bin = Path(java_home) / "bin" / java_name if java_home else None
        located_java = shutil.which("java")
        java = java_home_bin.resolve() if java_home_bin and java_home_bin.is_file() else (
            Path(located_java).resolve() if located_java else None
        )
    if java is None or not java.is_file():
        raise ToolchainUnavailable("Java is unavailable; set JAVA_HOME or pass --java")

    configured_jar = jar_arg or (Path(os.environ["VKIT_TLA2TOOLS_JAR"])
                                 if os.environ.get("VKIT_TLA2TOOLS_JAR") else None)
    if configured_jar is None:
        raise ToolchainUnavailable(
            "TLC jar is unavailable; set VKIT_TLA2TOOLS_JAR or pass --jar"
        )
    jar = configured_jar.expanduser().resolve()
    if not jar.is_file():
        raise ToolchainUnavailable(f"TLC jar does not exist: {jar}")

    expected = EXPECTED_JAR_DIGEST.read_text(encoding="ascii").strip()
    actual = hashlib.sha256(jar.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(f"TLC jar SHA-256 mismatch: {actual}")
    return java, jar


def run_case(name: str, expected: str | None, output: Path,
             java: Path, jar: Path) -> dict[str, object]:
    config = CONFIG_DIR / f"{name}.cfg"
    model = MODEL_DIR / "DelegatedClaimAuthority.tla"
    if not config.is_file():
        raise RuntimeError(f"missing TLC configuration: {config}")
    states = output / f"{name}-states"
    traces = output / f"{name}-traces"
    states.mkdir(parents=True, exist_ok=True)
    traces.mkdir(parents=True, exist_ok=True)
    options = {}
    if os.name == "nt":
        from vkit.nowindow import hidden_window

        options = hidden_window()
    done = subprocess.run(
        [str(java), "-cp", str(jar), "tlc2.TLC", "-config", str(config),
         "-metadir", str(states), "-teSpecOutDir", str(traces), str(model)],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        **options,
    )
    transcript = done.stdout + ("\n--- stderr ---\n" + done.stderr if done.stderr else "")
    (output / f"{name}.tlc.txt").write_text(transcript, encoding="utf-8")
    violations = re.findall(r"Invariant ([A-Za-z][A-Za-z0-9_]*) is violated", transcript)
    passed = not violations and "No error has been found" in transcript
    if expected is not None:
        passed = expected in violations
    if done.returncode not in {0, 12} or not passed:
        raise RuntimeError(
            f"{name}: TLC exit={done.returncode}, expected={expected!r}, "
            f"violations={violations!r}\n{transcript}"
        )
    states = re.search(r"([0-9,]+) distinct states found", transcript)
    return {
        "case": name,
        "expected_counterexample": expected,
        "observed_counterexamples": violations,
        "result": "counterexample" if expected else "invariants_hold",
        "distinct_states": int(states.group(1).replace(",", "")) if states else None,
        "tlc_exit_code": done.returncode,
        "transcript": f"{name}.tlc.txt",
    }


def run_suite(output: Path, java_arg: Path | None = None,
              jar_arg: Path | None = None) -> dict[str, object]:
    java, jar = resolve_toolchain(java_arg, jar_arg)
    output.mkdir(parents=True, exist_ok=True)
    results = [run_case(name, expected, output, java, jar)
               for name, expected in CASES.items()]
    report = {
        "tool": "TLC",
        "jar_sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
        "scope": "finite delegated-claim authority abstraction; restricted captured Rust Kani model",
        "limit": "This finite model is not a Python refinement proof, native-code proof, or general implementation proof.",
        "cases": results,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run finite delegated-claim authority models with pinned TLC.")
    parser.add_argument("--java", type=Path)
    parser.add_argument("--jar", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run_suite(args.output.resolve(), args.java, args.jar)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

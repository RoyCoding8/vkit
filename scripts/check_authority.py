from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODEL = Path("verification/certification/Authority.tla")
CONFIGS = {
    "good": ("Authority.cfg", None),
    "bad_claim": ("Authority.bad-claim.cfg", "ClaimPinned"),
    "candidate_target": ("Authority.candidate-target.cfg", "TargetAuthorityPinned"),
    "wrong_capture": ("Authority.wrong-capture.cfg", "CandidateCapturePinned"),
    "swapped_receipt": ("Authority.swapped-receipt.cfg", "ReceiptBound"),
    "stale_reuse": ("Authority.stale-reuse.cfg", "ReuseDecisionBound"),
    "false_reject": ("Authority.false-reject.cfg", "ReuseRejectedBound"),
    "preflight_pass": ("Authority.preflight-pass.cfg", "PreflightNotProof"),
}
PROPERTIES = [
    "TypeOK",
    "ClaimPinned",
    "TargetAuthorityPinned",
    "SnapshotPinned",
    "CandidateCapturePinned",
    "ReceiptBound",
    "PublishedPassBound",
    "PreflightNotProof",
    "ReuseDecisionBound",
    "ReuseRejectedBound",
]
CHECK_ID = "authority-model"


def _environment(output: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))]
    )
    env["VKIT_CONFIG"] = str(output / "settings.json")
    java_home = Path(env.get("JAVA_HOME", ""))
    java_bin = java_home / "bin"
    if (java_bin / ("java.exe" if os.name == "nt" else "java")).is_file():
        env["PATH"] = str(java_bin) + os.pathsep + env.get("PATH", "")
    if shutil.which("java", path=env.get("PATH")) is None:
        raise RuntimeError("the VKit TLC runner needs Java on PATH or under JAVA_HOME")
    return env


def _run(argv: list[str], cwd: Path, env: dict[str, str], allowed: set[int]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        from vkit.nowindow import hidden_window

        options = hidden_window()
    done = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=240, check=False, **options,
    )
    if done.returncode not in allowed:
        raise RuntimeError(
            f"command returned {done.returncode}, expected {sorted(allowed)}: {argv!r}\n"
            f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
        )
    try:
        body = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"VKit returned non-JSON output: {done.stdout}\n{done.stderr}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("VKit returned a non-object JSON response")
    return {"returncode": done.returncode, "stdout": done.stdout,
            "stderr": done.stderr, "body": body}


def _project(case_dir: Path, config_name: str, env: dict[str, str]) -> Path:
    project = case_dir / "project"
    cert_dir = project / "verification" / "certification"
    cert_dir.mkdir(parents=True)
    shutil.copyfile(ROOT / MODEL, project / MODEL)
    shutil.copyfile(ROOT / "verification/certification" / config_name,
                    cert_dir / config_name)
    raw = json.loads((ROOT / "verification/manifest.json").read_text(encoding="utf-8"))
    check = next(item for item in raw["checks"] if item["id"] == "lifecycle-model")
    model_path = MODEL.as_posix()
    config_path = (MODEL.parent / config_name).as_posix()
    check.update(
        id=CHECK_ID,
        description="Exhaustively checks policy, source, target, reuse, and receipt bindings.",
        inputs=[model_path, config_path],
        subject={"paths": [model_path, config_path], "digest": None},
        claim_id=CHECK_ID,
        model={"module": "Authority", "path": model_path},
        config=config_path,
        properties=PROPERTIES,
        bounds={
            "Fault": 1, "ComponentId": 1, "TargetPolicy": 1,
            "CandidatePolicy": 1, "TargetPackage": 1, "CandidatePackage": 1,
            "CheckerA": 1, "CheckerB": 1, "SourceA": 1, "SourceB": 1,
            "ProtectedTarget": 1, "CandidateTarget": 1,
        },
    )
    raw["checks"] = [check]
    (project / "verification" / "manifest.json").write_text(
        json.dumps(raw, indent=2) + "\n", encoding="utf-8"
    )
    git = ["git", "-c", "user.name=VKit Authority Model",
           "-c", "user.email=authority-model@vkit.invalid"]
    subprocess.run(["git", "init", "-q"], cwd=project, env=env, check=True)
    subprocess.run(git + ["add", "."], cwd=project, env=env, check=True)
    subprocess.run(git + ["commit", "-qm", "authority model fixture"],
                   cwd=project, env=env, check=True)
    return project


def _run_case(output: Path, name: str, config_name: str,
              expected_invariant: str | None, env: dict[str, str]) -> dict[str, Any]:
    case_dir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=output))
    project = _project(case_dir, config_name, env)
    cli = [sys.executable, "-m", "vkit.cli"]
    _run(cli + ["accept", "--yes", "--project", str(project), "--json"],
         project, env, {0})
    result = _run(cli + ["check", "run", "--check", CHECK_ID,
                         "--project", str(project), "--json"],
                  project, env, {0} if expected_invariant is None else {1})
    records = result["body"].get("runs") or []
    record = records[0] if records else result["body"]
    actual = (record.get("outcome") or {}).get("result")
    expected_result = "PASS" if expected_invariant is None else "FAIL"
    if actual != expected_result:
        raise RuntimeError(f"{name}: got {actual!r}, expected {expected_result!r}: {record!r}")
    obligations = record.get("obligations") or {}
    satisfied = obligations.get("satisfied", [])
    satisfied_names = [
        item.get("obligation", {}).get("obligation") for item in satisfied
    ]
    counterexamples = obligations.get("counterexamples", [])
    if expected_invariant is None and set(satisfied_names) != set(PROPERTIES):
        missing = sorted(set(PROPERTIES) - set(satisfied_names))
        extra = sorted(set(satisfied_names) - set(PROPERTIES))
        raise RuntimeError(
            f"{name}: satisfied invariant set differs from configured properties; "
            f"missing={missing}, extra={extra}"
        )
    if expected_invariant is not None and not any(
        item.get("obligation", {}).get("obligation") == expected_invariant
        for item in counterexamples
    ):
        raise RuntimeError(f"{name}: TLC did not report {expected_invariant}: {counterexamples!r}")
    (case_dir / "vkit.stdout.json").write_text(result["stdout"], encoding="utf-8")
    (case_dir / "vkit.stderr.txt").write_text(result["stderr"], encoding="utf-8")
    return {
        "case": name,
        "result": actual,
        "expected_counterexample": expected_invariant,
        "satisfied_invariants": [
            item.get("obligation", {}).get("obligation") for item in satisfied
        ],
        "exploration": next((item.get("states") for item in satisfied if item.get("states")), None),
        "counterexample_obligations": [
            item.get("obligation", {}).get("obligation") for item in counterexamples
        ],
        "project": str(project),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify the bounded authority model through VKit TLC.")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp" / "authority-model")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = _environment(output)
    sys.path.insert(0, str(ROOT / "src"))
    import vkit

    if Path(vkit.__file__).resolve().parent != (ROOT / "src/vkit").resolve():
        raise RuntimeError(f"authority qualification imported the wrong VKit: {vkit.__file__}")
    if not env.get("VKIT_TLA2TOOLS_JAR"):
        raise RuntimeError("VKIT_TLA2TOOLS_JAR must name the jar pinned by examples/formal-ci/tla2tools.sha256")
    observed = hashlib.sha256(Path(env["VKIT_TLA2TOOLS_JAR"]).read_bytes()).hexdigest()
    expected = (ROOT / "examples/formal-ci/tla2tools.sha256").read_text(encoding="ascii").strip()
    manifest = json.loads((ROOT / "verification/manifest.json").read_text(encoding="utf-8"))
    lifecycle = next(item for item in manifest["checks"] if item["id"] == "lifecycle-model")
    toolchain = lifecycle["toolchain"]
    if toolchain["jar_sha256"] != expected:
        raise RuntimeError("lifecycle check and repository TLC checksum pins disagree")
    if observed != expected:
        raise RuntimeError(f"TLC jar digest mismatch: expected {expected}, observed {observed}")

    results = []
    for name, (config_name, invariant) in CONFIGS.items():
        results.append(_run_case(output, name, config_name, invariant, env))
    report = {
        "schema_version": 1,
        "scope": "bounded package authority, source capture, receipt, reuse, and preflight policy model",
        "model": str(MODEL),
        "toolchain": {"version": toolchain["version"], "jar_sha256": observed},
        "attempts": "one package request/job; no scheduler or retry state",
        "bounds": "one component, two policy/package/checker/source revisions, one protected and one candidate target",
        "assumptions": [
            "digest atoms are collision-free labels; Git objects and hash algorithms are abstracted",
            "the host selects protected target authority; the package request has no target field",
            "a valid receipt remains historically valid after current policy drift, while a reuse attempt checks current policy, checker, source, and claim pins",
            "finite request preflight returns without job, receipt, PASS, or READY",
        ],
        "results": results,
        "limit": "Published PASS records are historical outcomes, not a statement about current gate readiness. TLC checks only this finite abstraction; it is not source proof or an implementation-refinement proof.",
    }
    report_path = output / "authority-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": "PASS", "cases": len(results),
                      "negative_controls": len(results) - 1,
                      "report": str(report_path)}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f"authority qualification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

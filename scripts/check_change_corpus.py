from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "examples/change-certification/manifest.json"
FIXTURE_ROOT = ROOT / "examples/change-certification/fixtures"
ALLOWED_JUDGMENTS = {
    "equivalent_on_recorded_rows",
    "approved_guarded_bugfix",
    "approved_guarded_feature",
    "reject_unapproved_behavior_change",
    "needs_contract",
    "reject_observation_tampering",
    "reject_authority_tampering",
}


class CorpusError(Exception):
    pass


def load_manifest(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"cannot read manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CorpusError("manifest must be an object with schema_version 1")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != 12:
        raise CorpusError("manifest must contain exactly twelve cases")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or not all(isinstance(case_id, str) for case_id in ids) or len(set(ids)) != 12:
        raise CorpusError("case ids must be present and unique")

    for case in cases:
        if not isinstance(case.get("id"), str) or not isinstance(case.get("intent"), str):
            raise CorpusError("every case needs an id and intent")
        if not isinstance(case.get("requirement"), str) or not isinstance(case.get("supported_domain"), str):
            raise CorpusError(f"{case['id']} needs a requirement and supported_domain")
        if not isinstance(case.get("guards"), list) or not all(isinstance(item, str) for item in case["guards"]):
            raise CorpusError(f"{case['id']} guards must be a list of strings")
        expected = case.get("expected")
        if not isinstance(expected, dict) or expected.get("judgment") not in ALLOWED_JUDGMENTS:
            raise CorpusError(f"{case['id']} has an unsupported expected judgment")
        witness = expected.get("witness")
        if witness is not None and not isinstance(witness, str):
            raise CorpusError(f"{case['id']} expected witness must be a string or null")
        observations = case.get("observations")
        if not isinstance(observations, list):
            raise CorpusError(f"{case['id']} observations must be a list")
        if case.get("kind") == "runtime":
            if not isinstance(case.get("source"), str) or not isinstance(case.get("test"), str):
                raise CorpusError(f"{case['id']} runtime case needs source and test")
            for row in observations:
                if not isinstance(row, dict) or any(not isinstance(row.get(key), str)
                                                     for key in ("input", "before", "after")):
                    raise CorpusError(f"{case['id']} has an invalid expected observation")
        elif case.get("kind") == "policy_only":
            if observations or "source" in case or "test" in case:
                raise CorpusError(f"{case['id']} policy-only case cannot claim runtime observations")
            if not isinstance(case.get("policy_evidence"), dict) or witness is None:
                raise CorpusError(f"{case['id']} policy-only case needs policy_evidence and a witness")
        else:
            raise CorpusError(f"{case['id']} has an unknown case kind")

    runtime_count = sum(case["kind"] == "runtime" for case in cases)
    policy_count = sum(case["kind"] == "policy_only" for case in cases)
    if (runtime_count, policy_count) != (8, 4):
        raise CorpusError("manifest must contain eight runtime and four policy-only cases")
    if manifest.get("status") != "development_only":
        raise CorpusError("manifest status must remain development_only")
    held_out = manifest.get("held_out")
    if (not isinstance(held_out, dict) or held_out.get("status") != "reserved_empty"
            or not isinstance(held_out.get("evaluation_status"), str)):
        raise CorpusError("held_out must describe a reserved empty location with no evaluation results")
    if not isinstance(manifest.get("scope_limit"), str):
        raise CorpusError("manifest needs a finite scope_limit")
    return manifest, hashlib.sha256(raw).hexdigest()


def expected_rows(cases: list[dict[str, object]]) -> Counter[tuple[str, str, str, str]]:
    return Counter(
        (case["id"], row["input"], row["before"], row["after"])
        for case in cases if case["kind"] == "runtime"
        for row in case["observations"]
    )


def observed_rows(output: str) -> Counter[tuple[str, str, str, str]]:
    rows: Counter[tuple[str, str, str, str]] = Counter()
    for line in output.splitlines():
        if not line.startswith("OBS|"):
            continue
        parts = line.split("|")
        if len(parts) != 5 or not parts[3].startswith("before=") or not parts[4].startswith("after="):
            raise CorpusError(f"malformed observation line: {line}")
        rows[(parts[1], parts[2], parts[3][7:], parts[4][6:])] += 1
    return rows


def write_report(output_dir: Path, report: dict[str, object]) -> None:
    (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def run_native(rustc: str, source: Path, binary: Path, timeout: float) -> tuple[str, list[list[str]]]:
    commands: list[list[str]] = []
    compile_argv = [rustc, "--test", str(source), "-o", str(binary)]
    commands.append(compile_argv)
    compile_result = subprocess.run(compile_argv, cwd=ROOT, capture_output=True, text=True,
                                    timeout=timeout, check=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if compile_result.returncode:
        raise CorpusError(f"rustc failed for {source.name}: {compile_result.stderr.strip() or compile_result.stdout.strip()}")
    run_argv = [str(binary), "--nocapture", "--test-threads=1"]
    commands.append(run_argv)
    run_result = subprocess.run(run_argv, cwd=ROOT, capture_output=True, text=True,
                                timeout=timeout, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    combined = run_result.stdout + "\n" + run_result.stderr
    if run_result.returncode:
        raise CorpusError(f"native tests failed for {source.name}: {combined.strip()}")
    if not re.search(r"(?m)^test result: ok\. \d+ passed; 0 failed;", combined):
        raise CorpusError(f"native test summary missing or failed for {source.name}: {combined.strip()}")
    return combined, commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the finite change-certification Rust fixture corpus.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"status": "FAIL", "manifest": str(args.manifest.resolve())}

    try:
        manifest_path = args.manifest.resolve()
        manifest, manifest_digest = load_manifest(manifest_path)
        cases = manifest["cases"]
        runtime_cases = [case for case in cases if case["kind"] == "runtime"]
        policy_cases = [case for case in cases if case["kind"] == "policy_only"]
        report.update({"case_count": len(cases), "runtime_case_count": len(runtime_cases),
                       "policy_only_case_count": len(policy_cases),
                       "finite_scope": manifest["scope_limit"]})
        rustc = shutil.which("rustc")
        if rustc is None:
            report.update({"status": "BLOCKED", "reason": "rustc is unavailable"})
            write_report(output_dir, report)
            print("BLOCKED: rustc is unavailable; native fixture evidence was not run.")
            return 2

        version_argv = [rustc, "--version"]
        version_result = subprocess.run(version_argv, cwd=ROOT, capture_output=True, text=True,
                                        timeout=args.timeout_seconds, check=False,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if version_result.returncode or not version_result.stdout.strip():
            raise CorpusError(f"rustc --version failed: {version_result.stderr.strip() or version_result.stdout.strip()}")

        sources = sorted({case["source"] for case in runtime_cases})
        all_actual: Counter[tuple[str, str, str, str]] = Counter()
        all_expected = expected_rows(runtime_cases)
        commands: list[list[str]] = [version_argv]
        source_hashes: dict[str, str] = {}
        native_logs: dict[str, str] = {}
        report["provenance"] = {"manifest_sha256": manifest_digest,
                                 "fixture_source_sha256": source_hashes,
                                 "rustc_version": version_result.stdout.strip(),
                                 "native_output_logs": native_logs}
        native_tests = 0
        suffix = ".exe" if os.name == "nt" else ""
        for source_name in sources:
            if Path(source_name).name != source_name:
                raise CorpusError(f"fixture source must be a file name: {source_name}")
            source = (FIXTURE_ROOT / source_name).resolve()
            if not source.is_relative_to(FIXTURE_ROOT.resolve()) or not source.is_file():
                raise CorpusError(f"fixture source is missing or outside the fixture directory: {source_name}")
            source_hashes[source_name] = hashlib.sha256(source.read_bytes()).hexdigest()
            suite_cases = [case for case in runtime_cases if case["source"] == source_name]
            binary = output_dir / f"change-corpus-{source.stem}{suffix}"
            output, run_commands = run_native(rustc, source, binary, args.timeout_seconds)
            log_path = output_dir / f"{source.stem}.native.log"
            log_path.write_text(output, encoding="utf-8")
            native_logs[source_name] = log_path.name
            commands.extend(run_commands)
            test_count = int(re.search(r"(?m)^test result: ok\. (\d+) passed; 0 failed;", output).group(1))
            if test_count != len(suite_cases):
                raise CorpusError(f"native test count differs for {source_name}: expected {len(suite_cases)}, got {test_count}")
            native_tests += len(suite_cases)
            actual = observed_rows(output)
            expected = expected_rows(suite_cases)
            if actual != expected:
                missing = list((expected - actual).elements())
                unexpected = list((actual - expected).elements())
                raise CorpusError(f"observations differ for {source_name}; missing={missing}; unexpected={unexpected}")
            all_actual.update(actual)

        if all_actual != all_expected:
            raise CorpusError("combined observations differ from the frozen manifest")
        policy_summary = [
            {"id": case["id"], "expected_judgment": case["expected"]["judgment"],
             "witness": case["expected"]["witness"], "kind": "policy_only"}
            for case in policy_cases
        ]
        runtime_summary = [
            {"id": case["id"], "expected_judgment": case["expected"]["judgment"],
             "observations_checked": len(case["observations"]), "kind": "runtime"}
            for case in runtime_cases
        ]
        report.update({"status": "PASS", "runtime_observation_count": sum(all_actual.values()),
                       "native_test_count": native_tests, "commands": commands,
                       "runtime_cases": runtime_summary, "policy_only_cases": policy_summary})
        write_report(output_dir, report)
        for case in policy_cases:
            print(f"POLICY-ONLY {case['id']} {case['expected']['judgment']}: {case['expected']['witness']}")
        print(f"PASS cases=12 runtime=8 policy_only=4 observations={sum(all_actual.values())} "
              f"native_tests={native_tests} scope=finite_literal_rows")
        return 0
    except (CorpusError, OSError, subprocess.SubprocessError, ValueError) as error:
        report.update({"status": "FAIL", "error": str(error)})
        try:
            write_report(output_dir, report)
        except OSError:
            pass
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

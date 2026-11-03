from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
import time
from collections import Counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "verification" / "certification"
SCENARIOS = {
    "bounded": "Differences.cfg",
    "invalid_shrink": "Differences.invalid-shrink.cfg",
    "violation_missing": "Differences.violation-missing.cfg",
    "guard_overlap": "Differences.guard-overlap.cfg",
    "guard_duplicate": "Differences.guard-duplicate.cfg",
    "repair": "Differences.repair.cfg",
    "empty": "Differences.empty.cfg",
}
MARKER = "VKIT_DIFF_DECISION_V1"
MAX_OUTPUT = 8 * 1024 * 1024
CASE_TIMEOUT = 120


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode_tla_tuple(raw: str) -> tuple[Any, int]:
    depth = 0
    quoted = False
    escaped = False
    end = None
    cursor = 0
    while cursor < len(raw) - 1:
        char = raw[cursor]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif raw[cursor:cursor + 2] == "<<":
            depth += 1
            cursor += 1
        elif raw[cursor:cursor + 2] == ">>":
            depth -= 1
            cursor += 1
            if depth == 0:
                end = cursor + 1
                break
        cursor += 1
    if end is None:
        raise ValueError("unterminated TLC output sequence")
    raw = raw[:end]
    token = re.compile(r'\s*(<<|>>|,|TRUE\b|FALSE\b|-?[0-9]+|"(?:\\.|[^"\\])*"|.)', re.DOTALL)
    tokens = []
    pos = 0
    while pos < len(raw):
        match = token.match(raw, pos)
        if not match:
            raise ValueError("invalid TLC tuple token")
        value = match.group(1)
        pos = match.end()
        if value.isspace():
            continue
        if value not in {"<<", ">>", ",", "TRUE", "FALSE"} and not (
            value.startswith('"') or re.fullmatch(r"-?[0-9]+", value)
        ):
            if value.strip() == "":
                continue
            raise ValueError(f"unsupported TLC tuple token: {value!r}")
        tokens.append(value)

    index = 0

    def parse() -> Any:
        nonlocal index
        if index >= len(tokens):
            raise ValueError("truncated TLC tuple")
        value = tokens[index]
        index += 1
        if value == "<<":
            result = []
            if index < len(tokens) and tokens[index] == ">>":
                index += 1
                return result
            while True:
                result.append(parse())
                if index >= len(tokens):
                    raise ValueError("unterminated TLC sequence")
                separator = tokens[index]
                index += 1
                if separator == ">>":
                    return result
                if separator != ",":
                    raise ValueError("invalid TLC sequence separator")
        if value == "TRUE":
            return True
        if value == "FALSE":
            return False
        if value.startswith('"'):
            return json.loads(value)
        if re.fullmatch(r"-?[0-9]+", value):
            return int(value)
        raise ValueError("unexpected TLC tuple delimiter")

    parsed = parse()
    if index != len(tokens) or not isinstance(parsed, list):
        raise ValueError("extra tokens or non-sequence TLC output")
    return tuple(parsed), end


def terminal_rows(stdout: str) -> list[tuple[Any, ...]]:
    rows = []
    needle = json.dumps(MARKER)
    offset = 0
    while (marker_at := stdout.find(needle, offset)) >= 0:
        start = stdout.rfind("<<", 0, marker_at)
        if start < 0:
            raise ValueError("TLC marker is not inside a sequence")
        parsed, consumed = decode_tla_tuple(stdout[start:])
        offset = start + consumed
        if len(parsed) != 9 or parsed[0] != MARKER:
            raise ValueError("unexpected terminal row shape")
        rows.append(parsed)
    return rows


def validate_record(record: Any) -> None:
    if (not isinstance(record, list) or len(record) != 5
            or type(record[0]) is not int or type(record[1]) is not bool
            or type(record[2]) is not bool
            or not isinstance(record[3], list) or not isinstance(record[4], list)
            or any(type(value) is not int for value in (*record[3], *record[4]))):
        raise ValueError("TLC record does not match the integer/boolean witness shape")


def validate_terminal_row(row: tuple[Any, ...]) -> None:
    if (len(row) != 9 or row[0] != MARKER or not isinstance(row[1], str)
            or not isinstance(row[2], list) or row[3] not in {
                "conformance", "observed_violation", "unknown"
            } or type(row[4]) is not bool or not isinstance(row[5], list)
            or any(type(value) is not int for value in row[5])):
        raise ValueError("TLC terminal tuple has the wrong field types")
    for sequence in (row[2], row[6], row[7], row[8]):
        if not isinstance(sequence, list):
            raise ValueError("TLC terminal record collection is not a sequence")
        for record in sequence:
            validate_record(record)


def expected_raw_count(pool_size: int) -> int:
    return sum(pool_size**length for length in range(3))


def make_intent(inputs: tuple[str, ...]):
    from vkit.certification import IntentRevision

    hashes = {name: (str(index) * 64) for index, name in enumerate(
        ("binding", "codec", "observation", "environment"), start=1
    )}
    raw = {
        "schema_version": 1,
        "requirement": {"source": "model bridge", "statement": "finite u32 output relation"},
        "contract": {
            "domain": {
                "binding": "u32-le",
                "inputs": ["1", "2"],
                "observations": ["0", "1", "2", "3"],
                "codec_sha256": hashes["codec"],
                "observation_sha256": hashes["observation"],
                "environment_sha256": hashes["environment"],
            },
            "admissible_inputs": list(inputs),
            "allowed_outputs": [
                {"input": token, "outputs": ["1", "2"] if token == "1" else ["2"]}
                for token in inputs
            ],
        },
        "artifacts": {
            role: {"path": f"model/{role}", "sha256": digest}
            for role, digest in hashes.items()
        },
    }
    return IntentRevision.from_json(raw)


def make_policy(intent, guarded: bool):
    from vkit.certification import DifferencePolicy

    guards = []
    if guarded:
        guards = [
            {"id": "guard-a", "requirements": [{"input": "1", "outputs": ["1", "2"]}]},
            {"id": "guard-b", "requirements": [{"input": "1", "outputs": ["2"]}]},
        ]
    return DifferencePolicy.from_json({
        "schema_version": 1,
        "kind": "finite_differences",
        "intent_sha256": intent.digest,
        "guards": guards,
    }, intent=intent)


def record_key(record: tuple[Any, ...]) -> tuple[Any, ...]:
    return (record[0], record[1], record[2], tuple(record[3]), tuple(record[4]))


def finding_counter(findings, kind: str) -> Counter:
    return Counter(
        (finding.input, finding.base_output, finding.candidate_output)
        for finding in findings if finding.kind == kind
    )


def counter_json(counts: Counter) -> list[dict[str, Any]]:
    return [
        {"input": key[0], "base_output": key[1], "candidate_output": key[2], "count": count}
        for key, count in sorted(counts.items(), key=lambda item: repr(item[0]))
    ]


def status_matches(model_status: str, python_status: str) -> bool:
    projected = {
        "conformance": "observed_conformance",
        "observed_violation": "observed_violation",
        "unknown": "unknown",
    }[model_status]
    return projected == python_status


def compare_terminal(name: str, row: tuple[Any, ...], intent, policy) -> list[dict[str, Any]]:
    from vkit.certification import PairedObservation, assess_differences

    _, _, raw_records, model_status, model_coverage, model_missing, model_violations, model_unknowns, model_repairs = row
    raw_sequence = tuple(record_key(record) for record in raw_records)
    observations = tuple(
        PairedObservation(
            str(record[0]), tuple(str(value) for value in record[3]),
            tuple(str(value) for value in record[4]),
            "paired_execution_failed" if record[2] else None, bool(record[1]),
        )
        for record in raw_records
    )
    assessment = assess_differences(intent, policy, observations, repetitions=2)
    actual = assessment.to_json()
    mismatches = []
    if not status_matches(model_status, actual["status"]):
        mismatches.append({"records": raw_sequence, "field": "status", "model": model_status, "python": actual["status"]})
    if actual["coverage_complete"] is not model_coverage:
        mismatches.append({"records": raw_sequence, "field": "coverage_complete", "model": model_coverage, "python": actual["coverage_complete"]})
    if set(actual["missing_inputs"]) != {str(value) for value in model_missing}:
        mismatches.append({"records": raw_sequence, "field": "missing_inputs", "model": model_missing, "python": actual["missing_inputs"]})

    model_unknown_inputs = {str(value[0]) for value in model_unknowns}
    model_unknown_inputs.update(str(value) for value in model_missing)
    python_unknown_inputs = {finding.input for finding in assessment.findings if finding.kind == "unknown"}
    if model_unknown_inputs != python_unknown_inputs:
        mismatches.append({"records": raw_sequence, "field": "unknown_inputs", "model": sorted(model_unknown_inputs), "python": sorted(python_unknown_inputs)})

    model_violation_counts = Counter(
        (str(value[0]), str(value[3][0]) if value[3] else None,
         str(value[4][0]) if value[4] else None)
        for value in model_violations
    )
    python_violation_counts = finding_counter(assessment.findings, "violation")
    if model_violation_counts != python_violation_counts:
        mismatches.append({"records": raw_sequence, "field": "violations", "model": counter_json(model_violation_counts), "python": counter_json(python_violation_counts)})

    selected_counts = Counter(str(value[0]) for value in raw_sequence if value[1])
    model_repair_counts = Counter(
        (str(value[0]), str(value[3][0]) if value[3] else None,
         str(value[4][0]) if value[4] else None)
        for value in model_repairs if value[1] and selected_counts[str(value[0])] == 1
    )
    python_repair_counts = finding_counter(assessment.findings, "standing_intent_repair")
    if model_repair_counts != python_repair_counts:
        mismatches.append({"records": raw_sequence, "field": "repairs", "model": counter_json(model_repair_counts), "python": counter_json(python_repair_counts)})
    if actual["source_proof"] is not False or actual["approval_authority"] is not False:
        mismatches.append({"records": raw_sequence, "field": "authority", "python": actual})
    return mismatches


def compare_case(name: str, rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    raw_sequences = set()
    for row in rows:
        _, scenario, raw_records, *_ = row
        if scenario != name:
            raise ValueError(f"TLC scenario mismatch: expected {name}, got {scenario}")
        validate_terminal_row(row)
        frozen = tuple(record_key(record) for record in raw_records)
        raw_sequences.add(frozen)
    pool_sizes = {
        "bounded": 13, "invalid_shrink": 2, "violation_missing": 2,
        "guard_overlap": 2, "guard_duplicate": 1, "repair": 1, "empty": 0,
    }
    expected = expected_raw_count(pool_sizes[name])
    if len(raw_sequences) != expected:
        raise ValueError(f"{name}: expected {expected} unique raw sequences, got {len(raw_sequences)}")

    tokens = ("1", "2") if name in {"bounded", "violation_missing"} else ("1",) if name != "empty" else ()
    intent = make_intent(tokens)
    policy = make_policy(intent, name in {"guard_overlap", "guard_duplicate"})
    mismatches = []
    for row in rows:
        mismatches.extend(compare_terminal(name, row, intent, policy))

    negative_control = None
    if name == "bounded" and raw_sequences:
        sample = next(row for row in rows if row[6])
        mutated = (*sample[:6], sample[6][1:], *sample[7:])
        negative_mismatches = compare_terminal(name, mutated, intent, policy)
        changed_status = next(
            status for status in ("conformance", "observed_violation", "unknown")
            if status != sample[3]
        )
        status_mutation = (*sample[:3], changed_status, *sample[4:])
        status_mismatches = compare_terminal(name, status_mutation, intent, policy)
        status_control = {
            "control": "same raw sequence with changed terminal status",
            "same_raw_sequence": tuple(record_key(record) for record in status_mutation[2])
            == tuple(record_key(record) for record in sample[2]),
            "mismatches": status_mismatches,
            "decision_rejected": any(item["field"] == "status" for item in status_mismatches),
        }
        violation_control = {
            "control": "remove one actual modeled violation",
            "mismatches": negative_mismatches,
            "decision_rejected": any(item["field"] == "violations" for item in negative_mismatches),
        }
        negative_control = {
            "controls": [violation_control, status_control],
            "decision_rejected": all(item["decision_rejected"] for item in (violation_control, status_control)),
        }
    return {
        "scenario": name,
        "terminal_rows": len(rows),
        "terminal_rows_compared": len(rows),
        "duplicate_rows_counted_once_for_coverage": len(rows) - len(raw_sequences),
        "unique_raw_sequences": len(raw_sequences),
        "expected_raw_sequences": expected,
        "comparator_negative_control": negative_control,
        "mismatches": mismatches,
    }


def run_case(name: str, cfg_name: str, run_dir: Path, cancel: threading.Event,
             java: Path, tla_jar: Path) -> dict[str, Any]:
    from vkit.proc import run as proc_run

    module_copy = run_dir / "Differences.tla"
    wrapper_copy = run_dir / "DifferenceDecisionBridge.tla"
    config_copy = run_dir / cfg_name
    shutil.copyfile(MODEL_DIR / "Differences.tla", module_copy)
    shutil.copyfile(MODEL_DIR / "DifferenceDecisionBridge.tla", wrapper_copy)
    if (sha256(module_copy) != sha256(MODEL_DIR / "Differences.tla")
            or sha256(wrapper_copy) != sha256(MODEL_DIR / "DifferenceDecisionBridge.tla")):
        raise ValueError("TLC input module copy does not match its reviewed source")
    config = (MODEL_DIR / cfg_name).read_text(encoding="utf-8")
    config_copy.write_text(config.rstrip() + "\nINVARIANT BridgeOutput\n", encoding="utf-8")

    env_names = ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TZ")
    env = {key: os.environ[key] for key in env_names if key in os.environ}
    java_bin = str(java.parent)
    env["PATH"] = java_bin + os.pathsep + env.get("PATH", "")
    stdout_path, stderr_path = run_dir / "tlc.stdout.log", run_dir / "tlc.stderr.log"
    started = time.monotonic()
    result = proc_run(
        [str(java), "-cp", str(tla_jar), "tlc2.TLC", "-workers", "1", "-cleanup",
         "-config", config_copy.stem, "DifferenceDecisionBridge"],
        cwd=run_dir, stdout_path=stdout_path, stderr_path=stderr_path,
        timeout_seconds=CASE_TIMEOUT, env=env, should_cancel=cancel.is_set,
        max_output_bytes=MAX_OUTPUT,
    )
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    if result.code != 0 or result.timed_out or result.cancelled or result.output_limited or result.launch_error:
        raise RuntimeError(f"TLC failed for {name}: {result}; stdout={stdout[-1000:]!r}; stderr={stderr[-1000:]!r}")
    rows = terminal_rows(stdout)
    comparison = compare_case(name, rows)
    comparison.update({
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "source_config_sha256": sha256(MODEL_DIR / cfg_name),
        "instrumented_config_sha256": sha256(config_copy),
        "tool_output_header": stdout.splitlines()[:3],
        "stdout_sha256": sha256(stdout_path),
        "stderr_sha256": sha256(stderr_path),
    })
    return comparison


def resolve_java() -> Path:
    executable = "java.exe" if os.name == "nt" else "java"
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / executable
        if candidate.is_file():
            return candidate.resolve()
    discovered = shutil.which("java") or (shutil.which("java.exe") if os.name == "nt" else None)
    if discovered:
        return Path(discovered).resolve()
    raise FileNotFoundError("Java not found; set JAVA_HOME or add java to PATH")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare bounded TLC terminal decisions with VKit's paired evaluator.")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp" / "difference-decisions")
    parser.add_argument("--jar", type=Path, default=os.environ.get("VKIT_TLA2TOOLS_JAR"))
    args = parser.parse_args()
    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output = output_root / run_id
    output.mkdir()
    cancel = threading.Event()
    previous_handlers = {}

    def request_cancel(signum, _frame):
        cancel.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, request_cancel)
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "bounded TLC terminal-state comparison with the public finite paired-observation evaluator",
        "limitations": [
            "finite model and declared record pools only",
            "does not establish source proof, dependency closure, or approval authority",
            "empty admissible set is evaluated within a parsed nonempty finite universe",
        ],
        "source_proof": False,
        "approval_authority": False,
        "model": str(MODEL_DIR / "Differences.tla"),
        "wrapper": str(MODEL_DIR / "DifferenceDecisionBridge.tla"),
        "model_sha256": sha256(MODEL_DIR / "Differences.tla"),
        "wrapper_sha256": sha256(MODEL_DIR / "DifferenceDecisionBridge.tla"),
        "bridge_script_sha256": sha256(Path(__file__).resolve()),
        "python_evaluator": str(ROOT / "src" / "vkit" / "certification.py"),
        "python_evaluator_sha256": sha256(ROOT / "src" / "vkit" / "certification.py"),
        "tlc_jar": str(args.jar) if args.jar else None,
        "tlc_jar_sha256": None,
        "java_runtime": None,
        "java_runtime_sha256": None,
        "tlc_version": None,
        "limits": {"per_case_timeout_seconds": CASE_TIMEOUT, "per_process_output_bytes": MAX_OUTPUT},
        "evidence_dir": str(output),
        "cases": [],
        "errors": [],
    }
    try:
        sys.path.insert(0, str(ROOT / "src"))
        import vkit

        expected_package = (ROOT / "src" / "vkit").resolve()
        if Path(vkit.__file__).resolve().parent != expected_package:
            raise RuntimeError(f"bridge imported VKit from an unexpected location: {vkit.__file__}")
        if args.jar is None:
            raise FileNotFoundError("set VKIT_TLA2TOOLS_JAR or pass --jar")
        tla_jar = args.jar.resolve()
        java = resolve_java()
        if not tla_jar.is_file():
            raise FileNotFoundError(f"TLC jar does not exist: {tla_jar}")
        expected_jar_hash = (ROOT / "examples" / "formal-ci" / "tla2tools.sha256").read_text(encoding="ascii").strip()
        lifecycle = json.loads((ROOT / "verification" / "manifest.json").read_text(encoding="utf-8"))
        toolchain = next(item for item in lifecycle["checks"] if item["id"] == "lifecycle-model")["toolchain"]
        if toolchain["jar_sha256"] != expected_jar_hash:
            raise ValueError("lifecycle check and repository TLC checksum pins disagree")
        if sha256(tla_jar) != expected_jar_hash:
            raise ValueError("TLC jar does not match examples/formal-ci/tla2tools.sha256")
        report.update({
            "tlc_jar": str(tla_jar),
            "tlc_jar_sha256": sha256(tla_jar),
            "tlc_version": toolchain["version"],
            "java_runtime": str(java),
            "java_runtime_sha256": sha256(java),
        })
        for name, cfg_name in SCENARIOS.items():
            case_dir = output / name
            case_dir.mkdir()
            try:
                case = run_case(name, cfg_name, case_dir, cancel, java, tla_jar)
                report["cases"].append(case)
            except Exception as exc:
                report["errors"].append({"scenario": name, "error": f"{type(exc).__name__}: {exc}"})
                break
    except Exception as exc:
        report["errors"].append({"setup": f"{type(exc).__name__}: {exc}"})
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    report["unique_raw_sequences"] = sum(case["unique_raw_sequences"] for case in report["cases"])
    report["expected_raw_sequences"] = sum(case["expected_raw_sequences"] for case in report["cases"])
    report["mismatch_count"] = sum(len(case["mismatches"]) for case in report["cases"])
    report["observed_tool_output_header"] = next(
        (case["tool_output_header"] for case in report["cases"] if case.get("tool_output_header")),
        [],
    )
    report["passed"] = (
        not report["errors"] and len(report["cases"]) == len(SCENARIOS)
        and report["unique_raw_sequences"] == report["expected_raw_sequences"] == 211
        and report["mismatch_count"] == 0
        and bool(report["cases"])
        and report["cases"][0].get("comparator_negative_control", {}).get("decision_rejected") is True
    )
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"], "cases": len(report["cases"]),
        "unique_raw_sequences": report["unique_raw_sequences"],
        "expected_raw_sequences": report["expected_raw_sequences"],
        "mismatch_count": report["mismatch_count"], "errors": report["errors"],
        "report": str(report_path),
    }, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

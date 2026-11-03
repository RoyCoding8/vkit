from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "verification" / "models" / "FiniteTransitionComparison.tla"
CONFIG = ROOT / "verification" / "models" / "finite-transition" / "complete.cfg"
EXPECTED_JAR = ROOT / "examples" / "formal-ci" / "tla2tools.sha256"
DEFAULT_OUTPUT = ROOT / "tmp" / "transition-comparison"
MARKER = "VKIT_FINITE_TRANSITION_PAIR_V1"
DELTA_KINDS = ("initial_states", "reachable_states", "assumptions", "guards",
               "transitions", "observations")
MODEL_COUNT = 192
PAIR_COUNT = MODEL_COUNT * MODEL_COUNT
TIMEOUT_SECONDS = 900


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_toolchain(java_arg: Path | None, jar_arg: Path | None) -> tuple[Path, Path]:
    executable = "java.exe" if os.name == "nt" else "java"
    if java_arg is not None:
        configured = str(java_arg.expanduser())
        located = shutil.which(configured)
        java = Path(configured).resolve() if Path(configured).is_file() else (
            Path(located).resolve() if located else None
        )
    else:
        java_home = os.environ.get("JAVA_HOME")
        home_candidate = Path(java_home) / "bin" / executable if java_home else None
        located = shutil.which("java")
        java = home_candidate.resolve() if home_candidate and home_candidate.is_file() else (
            Path(located).resolve() if located else None
        )
    if java is None or not java.is_file():
        raise RuntimeError("Java is unavailable; set JAVA_HOME or pass --java")
    configured_jar = jar_arg or (Path(os.environ["VKIT_TLA2TOOLS_JAR"])
                                 if os.environ.get("VKIT_TLA2TOOLS_JAR") else None)
    if configured_jar is None:
        raise RuntimeError("TLC jar is unavailable; set VKIT_TLA2TOOLS_JAR or pass --jar")
    jar = configured_jar.expanduser().resolve()
    if not jar.is_file():
        raise RuntimeError(f"TLC jar does not exist: {jar}")
    expected = EXPECTED_JAR.read_text(encoding="ascii").strip()
    observed = sha256(jar)
    if observed != expected:
        raise RuntimeError(f"TLC jar SHA-256 mismatch: expected {expected}, observed {observed}")
    return java, jar


def resolve_source(source_arg: Path | None) -> Path:
    selected = source_arg
    if selected is None and os.environ.get("VKIT_TRANSITION_SOURCE"):
        selected = Path(os.environ["VKIT_TRANSITION_SOURCE"])
    if selected is None:
        selected = ROOT / "src"
    if not (selected / "vkit" / "formal" / "transitions.py").is_file():
        raise RuntimeError("VKit transition comparator is unavailable; set --vkit-source")
    return selected.resolve()


def hidden_options() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


def decode_tla_tuple(raw: str) -> tuple[Any, int]:
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from check_difference_decisions import decode_tla_tuple as decode

    return decode(raw)


def terminal_rows(stdout: str) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    marker = json.dumps(MARKER)
    offset = 0
    while (at := stdout.find(marker, offset)) >= 0:
        start = stdout.rfind("<<", 0, at)
        if start < 0:
            raise ValueError("TLC bridge marker is not inside a tuple")
        row, consumed = decode_tla_tuple(stdout[start:start + 4096])
        offset = start + consumed
        if len(row) != 10 or row[0] != MARKER:
            raise ValueError(f"unexpected TLC bridge row: {row!r}")
        rows.append(row)
    return rows


def model_from_id(model_id: int) -> dict[str, Any]:
    if not 0 <= model_id < MODEL_COUNT:
        raise ValueError(f"model id out of range: {model_id}")
    edge_mask = model_id % 16
    assumption_mask = model_id // 16 % 4
    initial_mask = model_id // 64 + 1
    states = ("s0", "s1")
    contexts = tuple((state, "step", "x") for state in states)
    edges = (("s0", "step", "x", "s0"), ("s0", "step", "x", "s1"),
             ("s1", "step", "x", "s0"), ("s1", "step", "x", "s1"))
    rules = []
    for source in states:
        targets = [edge[3] for index, edge in enumerate(edges)
                   if edge[0] == source and edge_mask & (1 << index)]
        if targets:
            rules.append({"source": source, "action": "step", "inputs": ["x"],
                          "targets": sorted(targets)})
    return {
        "states": list(states), "actions": ["step"], "inputs": ["x"],
        "observations": ["one", "zero"],
        "initial_states": [state for index, state in enumerate(states)
                           if initial_mask & (1 << index)],
        "assumptions": [dict(state=state, action=action, input=value)
                        for index, (state, action, value) in enumerate(contexts)
                        if assumption_mask & (1 << index)],
        "rules": rules,
        "state_observations": [
            {"state": "s0", "value": "zero"}, {"state": "s1", "value": "one"}
        ],
    }


def request_from_ids(reference_id: int, candidate_id: int) -> dict[str, Any]:
    return {"schema_version": 1, "reference": model_from_id(reference_id),
            "candidate": model_from_id(candidate_id)}


def run_tlc(output: Path, java: Path, jar: Path) -> tuple[list[tuple[Any, ...]], dict[str, Any]]:
    case = output / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    case.mkdir(parents=True, exist_ok=False)
    model_copy, config_copy = case / MODEL.name, case / CONFIG.name
    shutil.copyfile(MODEL, model_copy)
    shutil.copyfile(CONFIG, config_copy)
    if sha256(model_copy) != sha256(MODEL) or sha256(config_copy) != sha256(CONFIG):
        raise RuntimeError("TLC input copies do not match the checked-in model and config")
    meta = case / "states"
    command = [str(java), "-cp", str(jar), "tlc2.TLC", "-workers", "1", "-cleanup",
               "-config", config_copy.name, "-metadir", str(meta), model_copy.name]
    (case / "command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
    started = time.monotonic()
    done = subprocess.run(command, cwd=case, capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS, check=False,
                          **hidden_options())
    (case / "tlc.stdout.log").write_text(done.stdout, encoding="utf-8")
    (case / "tlc.stderr.log").write_text(done.stderr, encoding="utf-8")
    transcript = "--- stdout ---\n" + done.stdout + "\n--- stderr ---\n" + done.stderr
    (case / "tlc.transcript.txt").write_text(transcript, encoding="utf-8")
    if done.returncode != 0 or "No error has been found" not in done.stdout:
        raise RuntimeError(f"TLC failed ({done.returncode}):\n{transcript[-5000:]}")
    rows = terminal_rows(done.stdout)
    distinct = re.search(r"([0-9,]+) distinct states found", done.stdout)
    if distinct is None:
        raise RuntimeError("TLC transcript has no distinct-state count")
    counts = Counter(row[3] for row in rows)
    summary = {
        "output": str(case), "tlc_exit_code": done.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "distinct_states": int(distinct.group(1).replace(",", "")),
        "emitted_rows": len(rows), "expected_pairs": PAIR_COUNT,
        "kind_counts": dict(sorted(counts.items())),
        "tlc_version": next((line.strip() for line in done.stdout.splitlines()
                              if line.startswith("TLC2 Version")), None),
        "jar_sha256": sha256(jar), "java_sha256": sha256(java),
        "model_sha256": sha256(model_copy), "config_sha256": sha256(config_copy),
        "transcript_sha256": sha256(case / "tlc.transcript.txt"),
        "stdout_sha256": sha256(case / "tlc.stdout.log"),
        "stderr_sha256": sha256(case / "tlc.stderr.log"),
    }
    return rows, {"case": case, **summary}


def check_rows(rows: list[tuple[Any, ...]], output: Path, source: Path) -> dict[str, Any]:
    sys.path.insert(0, str(source))
    from vkit.formal import transitions

    imported = Path(transitions.__file__).resolve()
    if not imported.is_relative_to(source.resolve()):
        raise RuntimeError(f"comparator import {imported} is outside selected source {source}")
    FiniteTransitionModel = transitions.FiniteTransitionModel
    compare_transition_models = transitions.compare_transition_models

    models = [FiniteTransitionModel.from_validated_json(model_from_id(model_id))
              for model_id in range(MODEL_COUNT)]

    seen: set[tuple[int, int]] = set()
    mismatches: list[dict[str, Any]] = []
    kinds: Counter[str] = Counter()
    deltas: Counter[str] = Counter()
    expectations: dict[tuple[int, int], tuple[str, list[str]]] = {}
    bridge_path = output / "bridge.jsonl"
    with bridge_path.open("w", encoding="utf-8") as bridge:
        for row in rows:
            _, reference_id, candidate_id, expected_kind, flags, counts, ref_reach, cand_reach, ref_edges, cand_edges = row
            pair = (reference_id, candidate_id)
            if pair in seen:
                raise ValueError(f"duplicate TLC pair: {pair}")
            seen.add(pair)
            if (type(reference_id) is not int or type(candidate_id) is not int
                    or not 0 <= reference_id < MODEL_COUNT or not 0 <= candidate_id < MODEL_COUNT
                    or expected_kind not in {"equivalent", "different"}
                    or not isinstance(flags, list) or len(flags) != len(DELTA_KINDS)
                    or any(type(flag) is not bool for flag in flags)
                    or not isinstance(counts, list) or len(counts) != len(DELTA_KINDS)):
                raise ValueError(f"invalid TLC pair row: {row!r}")
            expected_deltas = [kind for kind, changed in zip(DELTA_KINDS, flags) if changed]
            expectations[pair] = (expected_kind, expected_deltas)
            report = compare_transition_models(models[reference_id], models[candidate_id])
            actual_deltas = report["deltas"]
            actual_kinds = [item["kind"] for item in actual_deltas]
            reasons = []
            if report["kind"] != expected_kind:
                reasons.append("kind")
            if report["complete"] is not True:
                reasons.append("incomplete")
            if actual_kinds != expected_deltas:
                reasons.append("delta_kinds")
            if report["source_proof"] is not False or report["approval_authority"] is not False:
                reasons.append("authority_scope")
            actual_by_kind = {item["kind"]: item for item in actual_deltas}
            for index, (kind, expected_count) in enumerate(zip(DELTA_KINDS, counts)):
                item = actual_by_kind.get(kind, {"removed": 0, "added": 0})
                if item["removed"] != expected_count[0] or item["added"] != expected_count[1]:
                    reasons.append(f"delta_count:{DELTA_KINDS[index]}")
            before, after = report["counts"]["reference"], report["counts"]["candidate"]
            if (before["reachable_states"], after["reachable_states"],
                    before["effective_edges"], after["effective_edges"]) != (
                    ref_reach, cand_reach, ref_edges, cand_edges):
                reasons.append("reachable_counts")
            if report["counts"]["reference"]["expanded_rule_edges"] != (
                    (reference_id % 16).bit_count()):
                reasons.append("reference_declared_edges")
            if report["counts"]["candidate"]["expanded_rule_edges"] != (
                    (candidate_id % 16).bit_count()):
                reasons.append("candidate_declared_edges")
            if reasons and len(mismatches) < 20:
                mismatches.append({"pair": list(pair), "reasons": reasons,
                                   "expected": expected_kind, "observed": report})
            kinds[report["kind"]] += 1
            deltas.update(actual_kinds)
            bridge.write(json.dumps({"reference_id": reference_id,
                                     "candidate_id": candidate_id,
                                     "kind": report["kind"],
                                     "deltas": [{"kind": item["kind"],
                                                 "removed": item["removed"],
                                                 "added": item["added"]}
                                                for item in actual_deltas],
                                     "matched": not reasons}, sort_keys=True) + "\n")
    mismatch_path = output / "bridge-mismatches.json"
    mismatch_path.write_text(json.dumps(mismatches, indent=2) + "\n", encoding="utf-8")
    if len(seen) != PAIR_COUNT or mismatches:
        raise RuntimeError(f"bridge mismatch: pairs={len(seen)}, examples={mismatches[:3]!r}")
    controls = check_controls(compare_transition_models, models, expectations)
    return {"pairs_compared": len(seen), "mismatches": 0,
            "kind_counts": dict(sorted(kinds.items())),
            "delta_counts": dict(sorted(deltas.items())),
            "comparator_source": {"root": str(source.resolve()),
                                  "transitions_sha256": sha256(imported)},
            "checker_sha256": sha256(Path(__file__).resolve()),
            "imported_comparator_sha256": sha256(imported),
            "source_proof": False, "approval_authority": False,
            "bridge_sha256": sha256(bridge_path), "negative_controls": controls}


def check_controls(compare_transition_models, models, expectations) -> list[dict[str, Any]]:
    assumption_pair = ((50, 18), "assumptions")
    transition_pair = ((58, 54), "transitions")
    controls = []
    for name, (pair, expected_delta) in zip(
            ("ignore_assumptions", "ignore_reachable_transitions"),
            (assumption_pair, transition_pair)):
        report = compare_transition_models(models[pair[0]], models[pair[1]])
        found = [item["kind"] for item in report["deltas"]]
        expected_kind, expected_deltas = expectations[pair]
        mutated_deltas = [kind for kind in found if kind != expected_delta]
        mutated_kind = "different" if mutated_deltas else "equivalent"
        caught = (expected_kind == "different" and expected_deltas == [expected_delta]
                  and (mutated_kind, mutated_deltas) != (expected_kind, expected_deltas))
        controls.append({"mutant": name, "expected_only_delta": expected_delta,
                         "observed_kind": report["kind"], "observed_deltas": found,
                         "mutated_kind": mutated_kind, "mutated_deltas": mutated_deltas,
                         "caught_by_tlc_comparison": caught})
    if not all(item["caught_by_tlc_comparison"] for item in controls):
        raise RuntimeError(f"comparator omission control escaped: {controls!r}")
    return controls


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-check finite transition comparison against TLC.")
    parser.add_argument("--java", type=Path)
    parser.add_argument("--jar", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--vkit-source", type=Path)
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--replay", type=Path,
                        help="reuse a retained TLC run directory instead of running TLC")
    args = parser.parse_args()
    java, jar = resolve_toolchain(args.java, args.jar)
    source = None if args.reference_only else resolve_source(args.vkit_source)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.replay is None:
        rows, run = run_tlc(output, java, jar)
        run["execution"] = "fresh_tlc_run"
    else:
        case = args.replay.resolve()
        saved_report_path = case / "report.json"
        if not saved_report_path.is_file():
            raise RuntimeError("replay requires a retained report with original TLC hashes")
        saved_report = json.loads(saved_report_path.read_text(encoding="utf-8"))
        saved_tlc = saved_report.get("tlc", {})
        stdout_path, stderr_path = case / "tlc.stdout.log", case / "tlc.stderr.log"
        transcript_path = case / "tlc.transcript.txt"
        stdout = stdout_path.read_text(encoding="utf-8")
        stderr = stderr_path.read_text(encoding="utf-8")
        if "No error has been found" not in stdout or stderr.strip():
            raise RuntimeError("saved TLC run is not a successful clean execution")
        model_hash, config_hash = sha256(case / MODEL.name), sha256(case / CONFIG.name)
        transcript_hash = sha256(transcript_path)
        if (model_hash != sha256(MODEL) or config_hash != sha256(CONFIG)
                or saved_tlc.get("model_sha256") != model_hash
                or saved_tlc.get("config_sha256") != config_hash
                or saved_tlc.get("transcript_sha256") != transcript_hash
                or saved_tlc.get("jar_sha256") != sha256(jar)):
            raise RuntimeError("saved TLC report, inputs, transcript, or selected jar do not match")
        rows = terminal_rows(stdout)
        distinct = re.search(r"([0-9,]+) distinct states found", stdout)
        if distinct is None:
            raise RuntimeError("saved TLC transcript has no distinct-state count")
        run = {"case": case, "output": str(case), "tlc_exit_code": 0,
               "distinct_states": int(distinct.group(1).replace(",", "")),
               "emitted_rows": len(rows), "expected_pairs": PAIR_COUNT,
               "kind_counts": dict(sorted(Counter(row[3] for row in rows).items())),
               "tlc_version": next((line.strip() for line in stdout.splitlines()
                                     if line.startswith("TLC2 Version")), None),
               "jar_sha256": saved_tlc["jar_sha256"],
               "java_sha256": saved_tlc["java_sha256"],
               "model_sha256": model_hash, "config_sha256": config_hash,
               "transcript_sha256": transcript_hash,
               "stdout_sha256": sha256(stdout_path), "stderr_sha256": sha256(stderr_path),
               "execution": "replayed_retained_transcript"}
    if len(rows) != PAIR_COUNT:
        raise RuntimeError(f"TLC emitted {len(rows)} pairs; expected {PAIR_COUNT}")
    pairs = {(row[1], row[2]) for row in rows}
    if len(pairs) != PAIR_COUNT:
        raise RuntimeError(f"TLC emitted only {len(pairs)} distinct pairs")
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "complete two-state, one-action, one-input, two-observation model-pair comparison",
        "bounds": {"models_per_side": MODEL_COUNT, "pairs": PAIR_COUNT,
                   "directed_edges": 4, "assumption_contexts": 2,
                   "nonempty_initial_sets": 3},
        "tlc": {key: value for key, value in run.items() if key != "case"},
        "bridge": None,
        "limit": "This cross-check covers the declared 192-model space. It is not a Python refinement proof, source proof, or claim about larger domains.",
    }
    if not args.reference_only:
        report["bridge"] = check_rows(rows, run["case"], source)
    report["passed"] = report["bridge"] is not None and report["bridge"]["mismatches"] == 0
    report_path = run["case"] / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = {"result": "REFERENCE_ONLY" if args.reference_only else "PASS",
               "reference_only": args.reference_only,
               "tlc_execution": run["execution"],
               "models_per_side": MODEL_COUNT, "pairs": len(pairs),
               "tlc_distinct_states": run["distinct_states"],
               "output": str(run["case"]), "report": str(report_path),
               "jar_sha256": run["jar_sha256"]}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f"transition comparison check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

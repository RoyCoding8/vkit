"""Runs TLC once over one model at one configuration and writes the JSON report `tlc_adapter`
reads; it decides nothing. It is launched by file path (`python tlc_runner.py`) and puts the
source root on `sys.path` for `vkit.nowindow`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPORT_VERSION = 3

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vkit.nowindow import hidden_window  # noqa: E402
from vkit.verifiers.tlc_witness import same_state_sequence, source_locations, trace_states, without_internal_state  # noqa: E402

OUTPUT_LIMIT = 40000
TRACE_REPLAY_TIMEOUT_SECONDS = 5
TRACE_MINIMIZATION_SECONDS = 12.0
TRACE_MINIMIZATION_MAX_ATTEMPTS = 12
UNTRUSTED_ENVIRONMENT = (
    "TLA_LIBRARY", "CLASSPATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
)


def _digest(path: Path) -> str:
    """sha256 over the jar's exact bytes; no CRLF folding, since a jar is binary."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str], cwd: Path, timeout: float = 7200) -> dict:
    done = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False, env=_java_environment(), **hidden_window(),
    )
    return {
        "argv": list(argv),
        "exit_code": int(done.returncode),
        "stdout": (done.stdout or "")[:OUTPUT_LIMIT],
        "stderr": (done.stderr or "")[:OUTPUT_LIMIT],
    }


def refuse_java_overrides(directory: Path, model_paths: tuple[str, ...]) -> str | None:
    """Why a candidate-supplied Java override blocks this run, or None.

    TLC loads `<Module>.class` beside a `.tla` from the JVM classpath, so a candidate `.class`,
    a `classpath.txt`, or a symlink leaving the project could replace a trusted operator. The
    whole project tree is scanned.
    """
    found = sorted(
        entry.relative_to(directory).as_posix()
        for entry in directory.rglob("*")
        if entry.is_file() and entry.suffix.lower() == ".class"
    )
    classpaths = sorted(
        entry.relative_to(directory).as_posix()
        for entry in directory.rglob("classpath.txt") if entry.is_file()
    )
    external_links = []
    root = directory.resolve()
    for entry in directory.rglob("*"):
        if not entry.is_symlink():
            continue
        try:
            entry.resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError):
            external_links.append(entry.relative_to(directory).as_posix())
    external_links.sort()
    if not found and not classpaths and not external_links:
        return None
    detail = []
    if found:
        detail.append(
            "candidate Java override classes are present in the project tree: "
            f"{', '.join(found)}"
        )
    if classpaths:
        detail.append(
            "candidate classpath.txt files can add unpinned module or Java "
            f"override paths: {', '.join(classpaths)}"
        )
    if external_links:
        detail.append(
            "symlinks can resolve model dependencies outside the admitted project: "
            f"{', '.join(external_links)}"
        )
    return (
        "java_override_unsupported: " + "; ".join(detail) + ". TLC's module "
        "loader could let these replace a trusted operator or resolve a "
        "dependency outside the admitted project. Declared model paths: "
        f"{', '.join(model_paths)}. The run did not start."
    )


def _java_environment() -> dict[str, str]:
    """A child environment without external TLA or JVM configuration."""
    environment = dict(os.environ)
    for name in UNTRUSTED_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def _environment_problem() -> str | None:
    present = sorted(
        name for name in UNTRUSTED_ENVIRONMENT
        if os.environ.get(name, "").strip()
    )
    if not present:
        return None
    return (
        "environment_unsupported: external TLC/JVM configuration is set in "
        f"{', '.join(present)}; the runner cannot bind those dependencies"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", required=True)
    parser.add_argument("--jar", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--model", required=True, help="model module name, no extension")
    parser.add_argument(
        "--model-path", required=True, help="model source path, relative to --root",
    )
    parser.add_argument("--config", required=True, help="config path, relative to --root")
    parser.add_argument(
        "--root", required=True, help="project directory containing declared inputs",
    )
    parser.add_argument("--constants-from-config", choices=("true", "false"), required=True)
    parser.add_argument("--checksum-states", choices=("true", "false"), required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--metadir", required=True)
    parser.add_argument("--jar-sha256", default="", help="expected jar digest, if pinned")
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    report: dict = {
        "version": REPORT_VERSION,
        "complete": True,
        "tool": "tlc",
        "tool_version": "",
        "expected_version": args.expected_version,
        "model": args.model,
        "model_path": args.model_path,
        "config": args.config,
        "fingerprint": {
            "constants_from_config": args.constants_from_config == "true",
            "checksum_states": args.checksum_states == "true",
            "workers": args.workers,
        },
        "jar_sha256": "",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "trace_json_supported": False,
        "witness_validation": None,
    }

    if (args.constants_from_config, args.checksum_states, args.workers) != (
        "true", "false", 1,
    ):
        report["complete"] = False
        report["failure"] = (
            "fingerprint_unsupported: this runner supports only constants from "
            "the declared config, no state checksums, and one worker"
        )
        return _write(report, Path(args.report))
    if not args.expected_version.strip():
        report["complete"] = False
        report["failure"] = (
            "toolchain_unpinned: the check declares no TLC version"
        )
        return _write(report, Path(args.report))
    if not args.jar_sha256:
        report["complete"] = False
        report["failure"] = (
            "jar_unpinned: the check declares no tla2tools jar digest"
        )
        return _write(report, Path(args.report))
    environment_problem = _environment_problem()
    if environment_problem is not None:
        report["complete"] = False
        report["failure"] = environment_problem
        return _write(report, Path(args.report))

    root = Path(args.root).resolve()
    model_source = (root / args.model_path).resolve()
    config_source = (root / args.config).resolve()
    if not _within(root, model_source) or not _within(root, config_source):
        report["complete"] = False
        report["failure"] = "the declared model or config path escapes the project root"
        return _write(report, Path(args.report))
    if model_source.name != f"{args.model}.tla":
        report["complete"] = False
        report["failure"] = (
            f"the declared model path {args.model_path!r} does not name "
            f"{args.model}.tla"
        )
        return _write(report, Path(args.report))
    if not model_source.is_file() or not config_source.is_file():
        report["complete"] = False
        report["failure"] = "the declared model or config file does not exist"
        return _write(report, Path(args.report))

    workdir = model_source.parent
    override = refuse_java_overrides(
        root, (config_source.name, f"{args.model}.tla")
    )
    if override is not None:
        report["complete"] = False
        report["failure"] = override
        return _write(report, Path(args.report))

    jar = Path(args.jar)

    if not jar.is_file():
        report["complete"] = False
        report["failure"] = f"the pinned tla2tools jar {jar} does not exist"
        return _write(report, Path(args.report))
    report["jar_sha256"] = _digest(jar)
    if args.jar_sha256 and args.jar_sha256 != report["jar_sha256"]:
        report["complete"] = False
        report["failure"] = (
            f"the tla2tools jar measures {report['jar_sha256']} and the check pinned "
            f"{args.jar_sha256}. TLC loads its Java operators from this jar, so a "
            "jar vkit cannot name is a jar vkit cannot audit. The run did not start."
        )
        return _write(report, Path(args.report))

    metadir = Path(args.metadir)
    metadir.mkdir(parents=True, exist_ok=True)

    stem = (
        str(config_source.with_suffix("")).strip()
        if config_source.suffix == ".cfg" else str(config_source)
    )
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    original_trace = report_path.parent / "tlc-counterexample-original.json"
    final_trace = report_path.parent / "tlc-counterexample.json"
    validation_path = report_path.parent / "tlc-witness-validation.json"
    argv_tlc = [
        args.java, "-XX:+UseParallelGC", "-Xmx2g", "-cp", str(jar), "tlc2.TLC",
        "-nowarning", "-cleanup", "-workers", str(args.workers), "-metadir", str(metadir),
        "-config", stem, args.model,
    ]
    try:
        version = _run([args.java, "-cp", str(jar), "tlc2.TLC", "-help"], workdir)
        report["tool_version"] = _version_of(version)
        report["version_argv"] = version["argv"]
        actual_version = _numeric_version(report["tool_version"])
        if actual_version != args.expected_version:
            report["complete"] = False
            report["failure"] = (
                f"tool_version_mismatch: the check pins TLC {args.expected_version}, "
                f"but the jar reports {actual_version or 'no readable version'}"
            )
            return _write(report, Path(args.report))
        help_text = _ANSI.sub("", (version.get("stdout") or "") + (version.get("stderr") or ""))
        trace_json_supported = "-dumpTrace" in help_text and "-loadTrace" in help_text
        report["trace_json_supported"] = trace_json_supported
        if trace_json_supported:
            argv_tlc[-1:-1] = ["-dumpTrace", "json", str(original_trace)]
    except (OSError, subprocess.SubprocessError) as exc:
        report["complete"] = False
        report["failure"] = f"the TLC toolchain could not be launched: {exc}"
        return _write(report, Path(args.report))
    source_identity = ( _digest(model_source), _digest(config_source) )
    report["source_identity"] = {
        "model_sha256": source_identity[0],
        "config_sha256": source_identity[1],
    }
    try:
        step = _run(argv_tlc, workdir)
    except (OSError, subprocess.SubprocessError) as exc:
        report["complete"] = False
        report["failure"] = f"the TLC toolchain could not be launched: {exc}"
        return _write(report, Path(args.report))

    report.update(step)
    report["metadir"] = str(metadir)
    if not _sources_unchanged(model_source, config_source, *source_identity):
        report["complete"] = False
        report["failure"] = "source_changed: model or config changed during TLC execution"
        return _write(report, Path(args.report))
    invariant = re.search(r"Error: Invariant ([A-Za-z_][A-Za-z0-9_]*) is violated\.", report["stdout"])
    if invariant:
        validation = _validate_witness(
            args, jar, stem, workdir, model_source, config_source,
            original_trace, final_trace, validation_path, metadir,
            invariant.group(1), source_identity[0], source_identity[1], trace_json_supported,
        )
        report["witness_validation"] = validation
        if validation.get("status") == "validated":
            report["trace_artifact"] = final_trace.name
            report["validation_artifact"] = validation_path.name
    return _write(report, Path(args.report))


def _validate_witness(
    args, jar: Path, stem: str, workdir: Path, model_source: Path, config_source: Path,
    original_path: Path, final_path: Path, validation_path: Path, metadir: Path,
    property_name: str, model_digest: str, config_digest: str, trace_json_supported: bool,
) -> dict:
    result: dict = {
        "status": "unsupported",
        "method": "same_engine_trace_replay",
        "property": property_name,
        "source": {"model": model_digest, "config": config_digest},
        "minimization": {
            "method": "bounded_state_deletion",
            "budget_seconds": TRACE_MINIMIZATION_SECONDS,
            "max_attempts": TRACE_MINIMIZATION_MAX_ATTEMPTS,
            "attempts": 0,
            "accepted_removals": 0,
            "budget_exhausted": False,
            "minimal_within_budget": False,
        },
    }
    if not trace_json_supported:
        result["reason"] = "pinned TLC does not support JSON trace dump and load"
        _write(result, validation_path)
        return result
    try:
        original = json.loads(original_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["status"] = "rejected"
        result["reason"] = f"TLC did not produce a readable JSON counterexample: {exc}"
        _write(result, validation_path)
        return result
    parsed = trace_states(original)
    if parsed is None or _has_lasso(original):
        result["status"] = "unsupported"
        result["reason"] = "only complete finite safety traces in TLC JSON format are supported"
        _write(result, validation_path)
        return result

    candidate = original
    replay_output = final_path.parent / "tlc-witness-replay.json"
    replayed, reason = _replay_candidate(
        args, jar, stem, workdir, candidate, replay_output, metadir / "witness-replay",
        property_name, 0,
    )
    (final_path.parent / "tlc-replay-input-0.json").unlink(missing_ok=True)
    if replayed is None:
        result["status"] = "rejected"
        result["reason"] = reason
        _write(result, validation_path)
        return result
    candidate = replayed
    replay_output.replace(final_path)

    deadline = time.monotonic() + TRACE_MINIMIZATION_SECONDS
    state_count = len(trace_states(candidate)[1])
    index = 2
    while (state_count > 2 and index < state_count and result["minimization"]["attempts"]
           < TRACE_MINIMIZATION_MAX_ATTEMPTS and time.monotonic() < deadline):
        reduced = without_internal_state(candidate, index)
        if reduced is None:
            index += 1
            continue
        attempt = result["minimization"]["attempts"] + 1
        input_path = final_path.parent / "tlc-replay-candidate.json"
        input_path.write_text(json.dumps(reduced, sort_keys=True), encoding="utf-8")
        replayed, _ = _replay_candidate(
            args, jar, stem, workdir, reduced, replay_output,
            metadir / "witness-replay", property_name, attempt,
            input_path=input_path,
        )
        result["minimization"]["attempts"] = attempt
        if replayed is not None:
            candidate = replayed
            replay_output.replace(final_path)
            state_count = len(trace_states(candidate)[1])
            result["minimization"]["accepted_removals"] += 1
            index = 2
        else:
            index += 1
        input_path.unlink(missing_ok=True)

    result["minimization"]["budget_exhausted"] = (
        result["minimization"]["attempts"] >= TRACE_MINIMIZATION_MAX_ATTEMPTS
        or time.monotonic() >= deadline
    )
    result["minimization"]["minimal_within_budget"] = not result["minimization"]["budget_exhausted"]
    if not _sources_unchanged(model_source, config_source, model_digest, config_digest):
        result["status"] = "rejected"
        result["reason"] = "model or config changed during witness validation"
        final_path.unlink(missing_ok=True)
        _write(result, validation_path)
        return result

    replayed_json = json.loads(final_path.read_text(encoding="utf-8"))
    locations, locations_truncated = source_locations(
        replayed_json, args.model, args.model_path, model_digest,
    )
    result.update(
        status="validated",
        trace_sha256=_digest(final_path),
        state_sequence_sha256=_state_sequence_digest(replayed_json),
        source_locations=locations,
        source_locations_truncated=locations_truncated,
    )
    _write(result, validation_path)
    return result


def _replay_candidate(
    args, jar: Path, stem: str, workdir: Path, candidate: dict,
    replay_output: Path, metadir: Path, property_name: str, attempt: int,
    input_path: Path | None = None,
) -> tuple[dict | None, str]:
    candidate_path = input_path or replay_output.parent / f"tlc-replay-input-{attempt}.json"
    if input_path is None:
        candidate_path.write_text(json.dumps(candidate, sort_keys=True), encoding="utf-8")
    replay_output.unlink(missing_ok=True)
    metadir.mkdir(parents=True, exist_ok=True)
    argv = [
        args.java, "-XX:+UseParallelGC", "-Xmx2g", "-cp", str(jar), "tlc2.TLC",
        "-nowarning", "-cleanup", "-workers", str(args.workers), "-metadir", str(metadir),
        "-config", stem, "-loadTrace", "json", str(candidate_path),
        "-dumpTrace", "json", str(replay_output), args.model,
    ]
    try:
        run = _run(argv, workdir, timeout=TRACE_REPLAY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"native trace replay failed: {exc}"
    match = re.search(
        r"Error: Invariant ([A-Za-z_][A-Za-z0-9_]*) is violated\.", run["stdout"],
    )
    if run["exit_code"] != 12 or not match or match.group(1) != property_name:
        return None, "native trace replay did not reproduce the same invariant violation"
    try:
        replayed = json.loads(replay_output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"TLC replay did not produce a readable counterexample: {exc}"
    if not same_state_sequence(candidate, replayed):
        return None, "TLC replay returned a different state sequence than the submitted candidate"
    if input_path is None:
        candidate_path.unlink(missing_ok=True)
    return replayed, ""


def _has_lasso(document: dict) -> bool:
    counterexample = document.get("counterexample")
    actions = counterexample.get("action", []) if isinstance(counterexample, dict) else []
    return any(action[2][0] <= action[0][0] for action in actions
               if isinstance(action, list) and len(action) == 3
               and isinstance(action[0], list) and isinstance(action[2], list))


def _state_sequence_digest(document: dict) -> str:
    parsed = trace_states(document)
    if parsed is None:
        raise ValueError("invalid TLC trace")
    digest = hashlib.sha256(json.dumps(
        {"vars": parsed[0], "states": [state for _, state in parsed[1]]},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    return digest.hexdigest()


def _sources_unchanged(model_source: Path, config_source: Path,
                       model_digest: str, config_digest: str) -> bool:
    try:
        return (_digest(model_source), _digest(config_source)) == (model_digest, config_digest)
    except OSError:
        return False


def _numeric_version(text: str) -> str | None:
    match = re.search(r"\bVersion\s+([0-9]+(?:\.[0-9]+)+)\b", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\bTLC\s+([0-9]+(?:\.[0-9]+)+)\b", text, re.IGNORECASE)
    return match.group(1) if match else None


def _within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _version_of(help_run: dict) -> str:
    """TLC's version string from `tlc2.TLC -help`.

    Matches the NAME line's "Version X of DATE" after stripping ANSI escapes, falling back to a
    `TLC2 Version` banner line.
    """
    blob = (help_run.get("stdout") or "") + (help_run.get("stderr") or "")
    plain = _ANSI.sub("", blob)
    match = re.search(
        r"TLC - provides model checking and simulation of TLA\+ specifications"
        r" - Version ([^\r\n]+)",
        plain,
    )
    if match:
        return f"TLC {match.group(1).strip()}"
    for line in plain.splitlines():
        stripped = line.strip()
        if stripped.startswith("TLC2 Version"):
            return stripped
    return "unreported"


def _write(report: dict, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(target.suffix + ".partial")
    staged.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    staged.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Check one directional refinement between two captured LLVM IR functions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path, PureWindowsPath
from typing import Any

from .. import proc
from ._runtime import pinned_binary

ALIVE2_BIN_ENV = "VKIT_ALIVE2_BIN"
ALIVE2_SHA_ENV = "VKIT_ALIVE2_SHA256"
VALIDATOR_BIN_ENV = "VKIT_LLVM_IR_VALIDATOR_BIN"
VALIDATOR_SHA_ENV = "VKIT_LLVM_IR_VALIDATOR_SHA256"
ALIVE2_VERSION = "v20.0"
ALIVE2_COMMIT = "c0f5434f402ad91714ee0952f686cd0f524920ad"
LLVM_VERSION = "20.1.8"
MAX_SOURCE_BYTES = 262_144
MAX_OUTPUT_BYTES = 512_000
MAX_TIMEOUT_SECONDS = 60
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_SCOPE = (
    "One selected scalar integer LLVM function, i1 through i64, with matching signatures. "
    "The validator accepts acyclic control-flow graphs and integer arithmetic, comparisons, "
    "select, integer casts, branches, phi nodes, and returns. It rejects loops, memory, calls, "
    "globals, metadata, inline assembly, explicit undef and poison constants, and unsupported "
    "flags. Function arguments retain Alive2's default undef and poison input semantics. "
    "Alive2 checks whether the after function refines the before function under LLVM 20.1.8 "
    "semantics. This is not a whole-program or source/compiler correspondence claim."
)


def check_llvm_refinement(root: Path, before_path: str, after_path: str,
                          function: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Check whether `after_path` refines `before_path` for one function."""
    common: dict[str, Any] = {
        "operation": "check_llvm_refinement",
        "engine": f"Alive2 {ALIVE2_VERSION} ({ALIVE2_COMMIT}) with LLVM {LLVM_VERSION}",
        "model_scope": _SCOPE,
        "direction": "after refines before",
        "proof_trust": "Alive2 trusted-engine result; no independent counterexample replay",
    }
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        return {**common, "status": "UNSUPPORTED",
                "detail": f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]"}
    if not isinstance(function, str) or not _IDENTIFIER.fullmatch(function):
        return {**common, "status": "UNSUPPORTED", "detail": "function must be an ASCII LLVM identifier"}
    try:
        before, before_sha = _capture(root, before_path)
        after, after_sha = _capture(root, after_path)
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        return {**common, "status": "UNSUPPORTED", "detail": str(exc)}
    common.update({"before_path": before_path, "after_path": after_path, "function": function,
                   "before_sha256": before_sha, "after_sha256": after_sha})
    alive = pinned_binary(ALIVE2_BIN_ENV, ALIVE2_SHA_ENV)
    validator = pinned_binary(VALIDATOR_BIN_ENV, VALIDATOR_SHA_ENV)
    if alive is None or validator is None:
        return {**common, "status": "UNAVAILABLE",
                "detail": f"configure the pinned Alive2 and LLVM validator binaries with {ALIVE2_BIN_ENV}, "
                          f"{ALIVE2_SHA_ENV}, {VALIDATOR_BIN_ENV}, and {VALIDATOR_SHA_ENV}"}

    alive_binary, alive_sha = alive
    validator_binary, validator_sha = validator
    common.update({"alive2_binary_sha256": alive_sha, "validator_binary_sha256": validator_sha})
    deadline = time.monotonic() + timeout_seconds

    with tempfile.TemporaryDirectory(prefix="vkit-llvm-refinement-") as temporary:
        work = Path(temporary)
        before_file, after_file = work / "before.ll", work / "after.ll"
        before_file.write_bytes(before)
        after_file.write_bytes(after)
        version = _run(
            [str(validator_binary), "--version"], work, "validator-version", deadline,
        )
        if version["status"] == "UNAVAILABLE":
            return {**common, "status": "UNAVAILABLE", "detail": "configured LLVM validator has the wrong version"}
        if version["status"] != "OK":
            return {**common, "status": "UNKNOWN", "detail": "LLVM validator version check did not complete"}
        if version["stdout"] != f"vkit-alive2-validate 1 LLVM {LLVM_VERSION}\n":
            return {**common, "status": "UNAVAILABLE", "detail": "configured LLVM validator has the wrong version"}
        alive_version = _run([str(alive_binary), "--version"], work, "alive2-version", deadline)
        if alive_version["status"] == "UNAVAILABLE":
            return {**common, "status": "UNAVAILABLE", "detail": "configured Alive2 binary could not start"}
        if alive_version["status"] != "OK":
            return {**common, "status": "UNKNOWN", "detail": "Alive2 LLVM version check did not complete"}
        if not _alive2_version_matches(alive_version["stdout"]):
            return {**common, "status": "UNAVAILABLE", "detail": "configured Alive2 binary uses the wrong LLVM version"}
        help_result = _run([str(alive_binary), "--help"], work, "alive2-help", deadline)
        if help_result["status"] == "UNAVAILABLE":
            return {**common, "status": "UNAVAILABLE", "detail": "configured Alive2 binary could not start"}
        if help_result["status"] != "OK":
            return {**common, "status": "UNKNOWN", "detail": "Alive2 version check did not complete"}
        if not _alive2_help_matches(help_result["stdout"]):
            return {**common, "status": "UNAVAILABLE", "detail": "configured Alive2 binary has the wrong version"}

        checked: list[dict[str, Any]] = []
        for label, path in (("before", before_file), ("after", after_file)):
            validation = _run(
                [str(validator_binary), str(path), function], work, f"validate-{label}", deadline,
            )
            if validation["status"] == "UNAVAILABLE":
                return {**common, "status": "UNAVAILABLE", "detail": "LLVM validator binary could not start"}
            if validation["status"] != "OK":
                return {**common, "status": "UNKNOWN", "detail": f"LLVM validator {validation['status'].lower()}"}
            try:
                report = json.loads(validation["stdout"])
            except json.JSONDecodeError:
                return {**common, "status": "UNKNOWN", "detail": "LLVM validator returned invalid JSON"}
            if not isinstance(report, dict) or type(report.get("valid")) is not bool:
                return {**common, "status": "UNKNOWN", "detail": "LLVM validator returned an invalid report"}
            if not report["valid"]:
                return {**common, "status": "UNSUPPORTED", "detail": report.get("reason", "unsupported LLVM IR")}
            if not _valid_validation_report(report, function):
                return {**common, "status": "UNKNOWN", "detail": "LLVM validator returned an incomplete report"}
            checked.append(report)
        if any(checked[0].get(key) != checked[1].get(key)
               for key in ("function", "signature", "target_triple", "data_layout")):
            return {**common, "status": "UNSUPPORTED",
                    "detail": "the selected functions must have identical signatures, target triples, and data layouts"}
        common["validation"] = {"before": checked[0], "after": checked[1]}

        result = _run([str(alive_binary), str(before_file), str(after_file)], work, "alive2", deadline)
        if result["status"] == "UNAVAILABLE":
            return {**common, "status": "UNAVAILABLE", "detail": "Alive2 binary could not start"}
        if result["status"] != "OK":
            return {**common, "status": "UNKNOWN", "detail": f"Alive2 {result['status'].lower()}"}
        classification = _classify_alive2(result["stdout"])
        if classification is None:
            return {**common, "status": "UNKNOWN", "detail": "Alive2 output did not match a complete recognized result"}
        status, summary = classification
        output = {**common, "status": status, "engine_summary": summary}
        if status == "COUNTEREXAMPLE":
            output["engine_witness"] = result["stdout"][-32_000:]
        return output


def _capture(root: Path, path: str) -> tuple[bytes, str]:
    if (not isinstance(path, str) or not path or "\0" in path or Path(path).is_absolute()
            or PureWindowsPath(path).is_absolute() or PureWindowsPath(path).drive):
        raise ValueError("LLVM IR paths must be non-empty repository-relative paths")
    parts = path.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("LLVM IR paths must not contain empty, '.' or '..' segments")
    relative = Path(*parts)
    try:
        base = Path(root).resolve(strict=True)
        target = base / relative
        current = base
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("LLVM IR paths must not contain symlinks")
        target = target.resolve(strict=True)
        if not target.is_relative_to(base) or not target.is_file():
            raise ValueError("LLVM IR path must identify a regular file inside the repository")
        with target.open("rb") as source:
            raw = source.read(MAX_SOURCE_BYTES + 1)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot capture LLVM IR file: {exc}") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"LLVM IR files must be at most {MAX_SOURCE_BYTES} bytes")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("LLVM IR files must be UTF-8") from exc
    return raw, hashlib.sha256(raw).hexdigest()


def _run(argv: list[str], work: Path, label: str, deadline: float) -> dict[str, str]:
    out, err = work / f"{label}.out", work / f"{label}.err"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"status": "TIMED_OUT", "stdout": ""}
    process = proc.run(
        argv, cwd=work, stdout_path=out, stderr_path=err, timeout_seconds=remaining,
        max_output_bytes=MAX_OUTPUT_BYTES,
    )
    stdout, stderr = _read(out), _read(err)
    if process.timed_out:
        return {"status": "TIMED_OUT", "stdout": stdout}
    if process.output_limited:
        return {"status": "OUTPUT_LIMIT", "stdout": stdout}
    if process.launch_error:
        return {"status": "UNAVAILABLE", "stdout": ""}
    if process.code != 0 or stderr:
        return {"status": "FAILED", "stdout": stdout}
    return {"status": "OK", "stdout": stdout}


def _read(path: Path) -> str:
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_OUTPUT_BYTES + 1)
    except OSError:
        return ""
    if len(raw) > MAX_OUTPUT_BYTES:
        return ""
    return raw.decode("utf-8", errors="replace")


def _alive2_help_matches(output: str) -> bool:
    return (output.startswith("OVERVIEW: Alive2 stand-alone translation validator:\nversion v20.0\n")
            and "USAGE: alive-tv [options] first_bitcode_file [second_bitcode_file]" in output)


def _alive2_version_matches(output: str) -> bool:
    return bool(re.fullmatch(r"(?:Ubuntu )?LLVM version 20\.1\.8\n(?:  Optimized build\.\n)?", output))


def _valid_validation_report(report: dict[str, Any], function: str) -> bool:
    return (
        report.get("function") == function
        and isinstance(report.get("signature"), str)
        and isinstance(report.get("target_triple"), str)
        and isinstance(report.get("data_layout"), str)
        and type(report.get("blocks")) is int and report["blocks"] >= 1
        and type(report.get("instructions")) is int and report["instructions"] >= 1
    )


_SUMMARY = re.compile(
    r"Summary:\n"
    r"  (?P<correct>[0-9]+) correct transformations\n"
    r"  (?P<incorrect>[0-9]+) incorrect transformations\n"
    r"  (?P<failed>[0-9]+) failed-to-prove transformations\n"
    r"  (?P<errors>[0-9]+) Alive2 errors\n\Z"
)


def _classify_alive2(output: str) -> tuple[str, str] | None:
    match = _SUMMARY.search(output)
    if match is None or output.count("Summary:\n") != 1:
        return None
    counts = match.groupdict()
    if counts == {"correct": "1", "incorrect": "0", "failed": "0", "errors": "0"} \
            and output.count("Transformation seems to be correct!\n") == 1 \
            and "Transformation doesn't verify!" not in output:
        summary = match.group(0)
        return "REFINES", summary
    if (counts == {"correct": "0", "incorrect": "1", "failed": "0", "errors": "0"}
            and output.count("Transformation doesn't verify!\n") == 1
            and "Transformation seems to be correct!" not in output
            and "ERROR: Value mismatch\n" in output and "Example:\n" in output):
        summary = match.group(0)
        return "COUNTEREXAMPLE", summary
    return None

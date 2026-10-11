"""Classify regex matching complexity with Recheck's automaton model."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from ._data import _object_without_duplicates, _reject_constant

BACKEND_VERSION = "4.5.0"
NODE_VERSION = "v24.16.0"
PROTOCOL_VERSION = 1
MAX_PATTERN_BYTES = 2_048
MAX_HELPER_BYTES = 32_768
MAX_LOCK_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 262_144
MAX_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 10

_HELPER_ENV = "VKIT_REGEX_HELPER"
_HELPER_SHA_ENV = "VKIT_REGEX_SHA256"
_LOCK_ENV = "VKIT_REGEX_LOCKFILE"
_LOCK_SHA_ENV = "VKIT_REGEX_LOCK_SHA256"
_NODE_ENV = "VKIT_NODE_BIN"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FLAGS = frozenset("gimsuy")
_SAFE_COMPLEXITIES = frozenset({"constant", "linear", "safe"})
_VULNERABLE_COMPLEXITIES = frozenset({"polynomial", "exponential"})

MODEL_SCOPE = (
    "Estimated regular-expression matching complexity from Recheck 4.5.0's automaton checker. "
    "A vulnerable result includes a theoretical attack description and hotspot, not a replay against "
    "a specific runtime; engine optimizations can change real behavior. SAFE_MODEL is limited to the "
    "completed, non-fuzz automaton classification and is not an absolute security guarantee."
)

Status = Literal["SAFE_MODEL", "VULNERABLE_MODEL", "UNKNOWN", "UNSUPPORTED", "UNAVAILABLE"]


def analyze_regex_cost(root: Path, pattern: str, flags: str = "",
                       timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Classify a bounded pattern using the pinned Recheck automaton checker."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    if not isinstance(pattern, str):
        raise TypeError("pattern must be a string")
    if not isinstance(flags, str):
        raise TypeError("flags must be a string")
    if len(pattern) > MAX_PATTERN_BYTES:
        return _result("UNSUPPORTED", pattern, flags,
                       reason=f"pattern exceeds {MAX_PATTERN_BYTES} UTF-8 bytes")
    try:
        pattern_size = len(pattern.encode("utf-8"))
    except UnicodeEncodeError:
        return _result("UNSUPPORTED", pattern, flags, reason="pattern must contain Unicode scalar values")
    if pattern_size > MAX_PATTERN_BYTES:
        return _result("UNSUPPORTED", pattern, flags,
                       reason=f"pattern exceeds {MAX_PATTERN_BYTES} UTF-8 bytes")
    if len(flags) > len(_FLAGS) or any(flag not in _FLAGS for flag in flags) or len(set(flags)) != len(flags):
        return _result("UNSUPPORTED", pattern, flags,
                       reason="flags must be distinct members of the Recheck-supported gimsuy set")

    base = _result("UNKNOWN", pattern, flags)
    runtime = _runtime(root)
    if runtime is None:
        return {**base, "status": "UNAVAILABLE", "backend_identity": None,
                "detail": "the configured helper or pinned Recheck package lock is unavailable or failed its SHA-256 check"}
    helper, helper_sha256, _, lock_sha256, node = runtime
    identity = {"recheck": BACKEND_VERSION, "node": NODE_VERSION,
                "helper_sha256": helper_sha256, "package_lock_sha256": lock_sha256}
    base["backend_identity"] = identity
    deadline = time.monotonic() + timeout_seconds
    environment = _node_environment()
    try:
        version = subprocess.run([str(node), "--version"], cwd=helper.parent, env=environment,
                                 capture_output=True, timeout=min(5, _remaining(deadline)), check=False,
                                 **_hidden_window())
    except subprocess.TimeoutExpired:
        return {**base, "status": "UNKNOWN", "detail": "Node version check exceeded the time budget"}
    except OSError:
        return {**base, "status": "UNAVAILABLE", "detail": "the configured Node executable could not be started"}
    if version.returncode != 0 or version.stderr or version.stdout.decode("utf-8", "replace").strip() != NODE_VERSION:
        return {**base, "status": "UNAVAILABLE", "detail": f"the configured Node executable is not {NODE_VERSION}"}

    remaining = _remaining(deadline)
    if remaining <= 0:
        return {**base, "status": "UNKNOWN", "detail": "regex analysis exceeded its total time budget"}
    request = json.dumps({"protocol_version": PROTOCOL_VERSION, "pattern": pattern, "flags": flags,
                          "timeout_ms": max(1, int(remaining * 1_000) - 100)},
                         ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    try:
        result = subprocess.run([str(node), str(helper)], input=request, cwd=helper.parent, env=environment,
                                capture_output=True, timeout=remaining, check=False, **_hidden_window())
    except subprocess.TimeoutExpired:
        return {**base, "status": "UNKNOWN", "detail": "regex analysis exceeded its total time budget"}
    except OSError:
        return {**base, "status": "UNAVAILABLE", "detail": "the pinned regex helper could not be started"}
    if result.returncode != 0:
        return {**base, "status": "UNKNOWN", "detail": "the pinned regex helper exited without diagnostics"}
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        return {**base, "status": "UNKNOWN", "detail": "Recheck diagnostics exceeded the output limit"}
    try:
        response = json.loads(result.stdout.decode("utf-8"), object_pairs_hook=_object_without_duplicates,
                              parse_constant=_reject_constant)
        response = _response(response, pattern, flags)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return {**base, "status": "UNKNOWN", "detail": "the pinned regex helper returned malformed diagnostics"}

    if response is None:
        return {**base, "status": "UNAVAILABLE", "detail": "the loaded Recheck package does not match version 4.5.0"}
    return _classify(base, response)


def _result(status: Status, pattern: str, flags: str, *, reason: str | None = None) -> dict[str, Any]:
    try:
        digest = hashlib.sha256(pattern.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        digest = None
    value: dict[str, Any] = {
        "operation": "analyze_regex_cost",
        "status": status,
        "pattern": pattern if digest is not None and len(pattern) <= MAX_PATTERN_BYTES else None,
        "pattern_sha256": digest,
        "flags": flags,
        "backend": f"recheck/{BACKEND_VERSION}",
        "backend_identity": None,
        "diagnostics": None,
        "model_scope": MODEL_SCOPE,
    }
    if reason is not None:
        value["detail"] = reason
    return value


def _runtime(root: Path) -> tuple[Path, str, Path, str, Path] | None:
    raw_helper = os.environ.get(_HELPER_ENV)
    expected_helper = os.environ.get(_HELPER_SHA_ENV, "").lower()
    raw_lock = os.environ.get(_LOCK_ENV)
    expected_lock = os.environ.get(_LOCK_SHA_ENV, "").lower()
    raw_node = os.environ.get(_NODE_ENV)
    if not raw_helper or not raw_lock or not raw_node \
            or not _SHA256.fullmatch(expected_helper) or not _SHA256.fullmatch(expected_lock):
        return None
    try:
        base = Path(root).resolve(strict=True)
        if not base.is_dir():
            return None
        paths = [Path(value).expanduser() for value in (raw_helper, raw_lock, raw_node)]
        helper, lockfile, node = (path if path.is_absolute() else base / path for path in paths)
        helper, lockfile, node = (path.resolve(strict=True) for path in (helper, lockfile, node))
        if helper.name != "helper.cjs" or lockfile != helper.parent / "package-lock.json":
            return None
        if not helper.is_file() or not lockfile.is_file() or not node.is_file():
            return None
        with helper.open("rb") as source:
            helper_bytes = source.read(MAX_HELPER_BYTES + 1)
        with lockfile.open("rb") as source:
            lock_bytes = source.read(MAX_LOCK_BYTES + 1)
    except (OSError, RuntimeError, ValueError):
        return None
    if len(helper_bytes) > MAX_HELPER_BYTES or len(lock_bytes) > MAX_LOCK_BYTES:
        return None
    helper_sha256 = hashlib.sha256(helper_bytes).hexdigest()
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    if helper_sha256 != expected_helper or lock_sha256 != expected_lock:
        return None
    try:
        lock = json.loads(lock_bytes.decode("utf-8"), object_pairs_hook=_object_without_duplicates,
                          parse_constant=_reject_constant)
        package = lock["packages"]["node_modules/recheck"]
        root_dependency = lock["packages"][""]["dependencies"]["recheck"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, RecursionError):
        return None
    if (not isinstance(lock, dict) or lock.get("lockfileVersion") != 3
            or not isinstance(package, dict) or package.get("version") != BACKEND_VERSION
            or root_dependency != BACKEND_VERSION):
        return None
    return helper, helper_sha256, lockfile, lock_sha256, node


def _response(value: Any, pattern: str, flags: str) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"protocol_version", "backend_version", "diagnostics"}:
        raise ValueError("invalid helper response shape")
    if type(value["protocol_version"]) is not int or value["protocol_version"] != PROTOCOL_VERSION:
        raise ValueError("helper protocol version mismatch")
    if value["backend_version"] != BACKEND_VERSION:
        return None
    diagnostics = value["diagnostics"]
    fields = {"source", "flags", "status", "checker", "complexity", "attack", "hotspot", "error"}
    if not isinstance(diagnostics, dict) or set(diagnostics) != fields:
        raise ValueError("invalid diagnostics shape")
    if diagnostics["source"] != pattern or diagnostics["flags"] != flags:
        raise ValueError("helper changed the requested pattern or flags")
    if diagnostics["status"] == "unknown":
        error = diagnostics["error"]
        if (not isinstance(error, dict) or error.get("kind") not in
                {"timeout", "cancel", "unsupported", "invalid", "unexpected"}
                or (error.get("message") is not None and not isinstance(error["message"], str))
                or diagnostics["checker"] not in {None, "automaton"}):
            raise ValueError("unknown diagnostics omitted its error kind")
        return diagnostics
    if diagnostics["status"] not in {"safe", "vulnerable"} or diagnostics["checker"] != "automaton":
        raise ValueError("completed diagnostics did not use the automaton checker")
    complexity = diagnostics["complexity"]
    if not isinstance(complexity, dict) or complexity.get("isFuzz") is not False \
            or not isinstance(complexity.get("summary"), str):
        raise ValueError("completed diagnostics omitted non-fuzz complexity")
    if diagnostics["status"] == "safe" and complexity.get("type") not in _SAFE_COMPLEXITIES:
        raise ValueError("safe diagnostics have an unknown complexity class")
    if diagnostics["status"] == "vulnerable":
        if complexity.get("type") not in _VULNERABLE_COMPLEXITIES:
            raise ValueError("vulnerable diagnostics have an unknown complexity class")
        degree = complexity.get("degree")
        if complexity["type"] == "polynomial" and (
                type(degree) not in {int, float} or not math.isfinite(degree) or degree < 1):
            raise ValueError("polynomial diagnostics omitted the positive degree")
        _validate_attack(diagnostics["attack"])
        _validate_hotspots(diagnostics["hotspot"], len(pattern.encode("utf-16-le")) // 2)
    elif diagnostics["attack"] is not None or diagnostics["hotspot"] != [] or diagnostics["error"] is not None:
        raise ValueError("safe diagnostics contain inconsistent vulnerability fields")
    return diagnostics


def _validate_attack(attack: Any) -> None:
    if not isinstance(attack, dict) or set(attack) != {"pattern", "base", "suffix", "pumps"}:
        raise ValueError("vulnerable diagnostics omitted the bounded attack description")
    if not isinstance(attack["pattern"], str) or len(attack["pattern"].encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("attack description exceeded the output limit")
    if type(attack["base"]) is not int or attack["base"] < 0 or not isinstance(attack["suffix"], str):
        raise ValueError("attack description has invalid repetition data")
    if not isinstance(attack["pumps"], list) or not 1 <= len(attack["pumps"]) <= 256:
        raise ValueError("attack description has invalid pumps")
    for pump in attack["pumps"]:
        if (not isinstance(pump, dict) or set(pump) != {"prefix", "pump", "bias"}
                or not isinstance(pump["prefix"], str) or not isinstance(pump["pump"], str)
                or type(pump["bias"]) not in {int, float} or not math.isfinite(pump["bias"])):
            raise ValueError("attack description has invalid pump data")


def _validate_hotspots(hotspots: Any, pattern_length: int) -> None:
    if not isinstance(hotspots, list) or not hotspots or len(hotspots) > 256:
        raise ValueError("vulnerable diagnostics omitted bounded hotspots")
    for hotspot in hotspots:
        if (not isinstance(hotspot, dict) or set(hotspot) != {"start", "end", "temperature"}
                or type(hotspot["start"]) is not int or type(hotspot["end"]) is not int
                or not 0 <= hotspot["start"] <= hotspot["end"] <= pattern_length
                or hotspot["temperature"] not in {"heat", "normal"}):
            raise ValueError("diagnostics contain invalid hotspot ranges")


def _classify(base: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    status = diagnostics["status"]
    if status == "safe":
        return {**base, "status": "SAFE_MODEL", "diagnostics": diagnostics}
    if status == "vulnerable":
        return {**base, "status": "VULNERABLE_MODEL", "diagnostics": diagnostics}
    error = diagnostics["error"]
    if error["kind"] in {"invalid", "unsupported"}:
        return {**base, "status": "UNSUPPORTED", "detail": error.get("message", error["kind"]),
                "diagnostics": diagnostics}
    return {**base, "status": "UNKNOWN", "detail": error.get("message", error["kind"]),
            "diagnostics": diagnostics}


def _node_environment() -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _hidden_window() -> dict[str, int]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

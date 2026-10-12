"""Compare captured OpenAPI files with their local-parser JSON controls."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from . import openapi
from ._runtime import pinned_binary

MAX_TIMEOUT_SECONDS = openapi.MAX_TIMEOUT_SECONDS
MAX_CONTROL_FILE_BYTES = openapi.MAX_OPENAPI_FILE_BYTES
MAX_CONTROL_TOTAL_BYTES = openapi.MAX_OPENAPI_TOTAL_BYTES
MAX_DIFFERENCES = 32
MAX_VALUE_PREVIEW = 160
MAX_RESULT_BYTES = 262_144


class _UnrepresentableValue(Exception):
    def __init__(self, pointer: str, value_type: str):
        super().__init__(pointer, value_type)
        self.pointer = pointer
        self.value_type = value_type


def _pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in parts)


def _validate_json_value(value: Any, parts: tuple[str, ...] = ()) -> None:
    value_type = type(value)
    if value is None or value_type in (bool, int, str):
        return
    if value_type is float:
        if not math.isfinite(value):
            raise _UnrepresentableValue(_pointer(parts), "non-finite number")
        return
    if value_type is list:
        for index, item in enumerate(value):
            _validate_json_value(item, (*parts, str(index)))
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _UnrepresentableValue(_pointer(parts), f"mapping key {type(key).__name__}")
            _validate_json_value(item, (*parts, key))
        return
    raise _UnrepresentableValue(_pointer(parts), value_type.__name__)


def _json_bytes(value: Any) -> bytes:
    _validate_json_value(value)
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _same(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _value_evidence(value: Any) -> dict[str, Any]:
    if value is _MISSING:
        return {"type": "missing"}
    kind = type(value)
    if value is None:
        return {"type": "null"}
    if kind is bool:
        return {"type": "boolean", "value": value}
    if kind is int:
        return {"type": "integer", "value": str(value)}
    if kind is float:
        return {"type": "number", "value": repr(value)}
    if kind is str:
        encoded = value.encode("utf-8", errors="surrogatepass")
        preview = value[:MAX_VALUE_PREVIEW]
        if len(value) > MAX_VALUE_PREVIEW:
            preview += "…"
        return {"type": "string", "length": len(value),
                "sha256": hashlib.sha256(encoded).hexdigest(), "preview": preview}
    encoded = _json_bytes(value)
    return {"type": "array" if kind is list else "object", "length": len(value),
            "sha256": hashlib.sha256(encoded).hexdigest()}


_MISSING = object()


def _differences(left: Any, right: Any) -> tuple[list[dict[str, Any]], bool]:
    found: list[dict[str, Any]] = []
    truncated = False
    stack: list[tuple[Any, Any, tuple[str, ...]]] = [(left, right, ())]
    while stack:
        before, after, parts = stack.pop()
        if _same(before, after):
            continue
        if type(before) is dict and type(after) is dict:
            keys = sorted(before.keys() | after.keys(), reverse=True)
            for key in keys:
                stack.append((before.get(key, _MISSING), after.get(key, _MISSING), (*parts, key)))
            continue
        if type(before) is list and type(after) is list:
            max_length = max(len(before), len(after))
            for index in range(max_length - 1, -1, -1):
                stack.append((before[index] if index < len(before) else _MISSING,
                              after[index] if index < len(after) else _MISSING,
                              (*parts, str(index))))
            continue
        if len(found) == MAX_DIFFERENCES:
            truncated = True
            break
        found.append({"pointer": _pointer(parts), "original": _value_evidence(before),
                      "local_json_control": _value_evidence(after)})
    return found, truncated


def _parse_backend_output(result: Any) -> dict[str, Any]:
    if result.stderr:
        raise ValueError("oasdiff emitted unexpected error output")
    if result.returncode == 102:
        return {"status": "REJECTED", "output_sha256": hashlib.sha256(result.stdout).hexdigest()}
    if result.returncode != 0:
        raise ValueError("oasdiff returned an unexpected status")
    try:
        output = json.loads(result.stdout, object_pairs_hook=openapi._json_object,
                            parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, openapi._UnsupportedDocument) as exc:
        raise ValueError("oasdiff returned malformed flattened JSON") from exc
    if type(output) is not dict:
        raise ValueError("oasdiff flattened output must be a JSON object")
    return {"status": "PARSED", "value": output,
            "output_sha256": hashlib.sha256(result.stdout).hexdigest()}


def _observations_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["status"] != right["status"]:
        return False
    if left["status"] == "REJECTED":
        return left["output_sha256"] == right["output_sha256"]
    return _same(left["value"], right["value"])


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number {value}")


def _flatten(binary: Path, entry: Path, runner: Path, env: dict[str, str],
             config: Path, deadline: float) -> dict[str, Any]:
    result = openapi._run(
        binary,
        ["flatten", "--format=json", "--allow-external-refs=true", f"--config={config}",
         "--", str(entry)],
        runner, env, deadline,
    )
    return _parse_backend_output(result)


def _within_result_limit(result: dict[str, Any]) -> bool:
    try:
        return len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) \
            <= MAX_RESULT_BYTES
    except (TypeError, UnicodeEncodeError):
        return False


def inspect_openapi_parser_consistency(root: Path, path: str,
                                       timeout_seconds: int = 20) -> dict[str, Any]:
    """Compare oasdiff's flattened model for each captured file and its local JSON control."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    started = time.monotonic()
    deadline = started + timeout_seconds
    result: dict[str, Any] = {
        "operation": "inspect_openapi_parser_consistency",
        "status": "UNKNOWN",
        "path": path,
        "backend": f"oasdiff/{openapi.OASDIFF_VERSION}",
        "backend_sha256": None,
        "comparison": "per-file flattened JSON model from original captured bytes versus local-parser JSON controls",
        "scope": "captured repository-local OpenAPI JSON/YAML files; not all parser behavior or outside consumers",
    }
    try:
        repo = root.resolve(strict=True)
        if not repo.is_dir():
            raise ValueError("repository root must be a directory")
        source, relative = openapi._source_file(repo, path)
        content = openapi._read_source(source, deadline)
        result["path"] = relative
        result["entry_source_sha256"] = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory(prefix="vkit-parser-consistency-") as temporary:
            work = Path(temporary)
            original = work / "original"
            control = work / "local-json-control"
            runner = work / "runner"
            original.mkdir()
            control.mkdir()
            runner.mkdir()
            snapshot_sha256, file_count = openapi._snapshot(repo, relative, original, deadline, content)
            result["snapshot_sha256"] = snapshot_sha256
            result["file_count"] = file_count

            files: list[tuple[str, str, str]] = []
            total_control_bytes = 0
            for captured in sorted(original.rglob("*")):
                if not captured.is_file():
                    continue
                openapi._check_deadline(deadline)
                file_relative = captured.relative_to(original).as_posix()
                raw = captured.read_bytes()
                model = openapi._parse_document(file_relative, raw, file_relative == relative)
                try:
                    encoded = _json_bytes(model)
                except _UnrepresentableValue as exc:
                    detail = (f"the local parser produced a non-JSON {exc.value_type} value"
                              if exc.value_type in {"date", "datetime", "time"}
                              else "the local parser produced a value outside the JSON control model")
                    result.update({"status": "UNSUPPORTED", "unsupported_file": file_relative,
                                   "unsupported_pointer": exc.pointer, "unsupported_type": exc.value_type,
                                   "detail": detail})
                    return result
                if len(encoded) > MAX_CONTROL_FILE_BYTES:
                    result.update({"status": "UNSUPPORTED", "unsupported_file": file_relative,
                                   "detail": "the local parser JSON control exceeds the per-file size limit"})
                    return result
                total_control_bytes += len(encoded)
                if total_control_bytes > MAX_CONTROL_TOTAL_BYTES:
                    result.update({"status": "UNSUPPORTED", "unsupported_file": file_relative,
                                   "detail": "the local parser JSON closure exceeds the total size limit"})
                    return result
                control_path = control.joinpath(*PurePosixPath(file_relative).parts)
                control_path.parent.mkdir(parents=True, exist_ok=True)
                control_path.write_bytes(encoded)
                files.append((file_relative, hashlib.sha256(raw).hexdigest(),
                              hashlib.sha256(encoded).hexdigest()))
            result["files"] = [{"path": name, "source_sha256": source_sha,
                                "local_json_sha256": control_sha}
                               for name, source_sha, control_sha in files]

            runtime = pinned_binary(openapi._BIN_ENV, openapi._SHA_ENV)
            if runtime is None:
                result.update({"status": "UNAVAILABLE",
                               "detail": f"the configured oasdiff {openapi.OASDIFF_VERSION} binary is unavailable or failed its SHA-256 check"})
                return result
            binary, backend_sha256 = runtime
            result["backend_sha256"] = backend_sha256
            env = openapi._process_env(work)
            version = openapi._run(binary, ["--version"], runner, env, deadline)
            if version.returncode != 0 or version.stdout.decode("utf-8", "replace").strip() \
                    != f"oasdiff version {openapi.OASDIFF_VERSION}" or version.stderr:
                result.update({"status": "UNAVAILABLE",
                               "detail": f"the configured executable did not identify itself as oasdiff {openapi.OASDIFF_VERSION}"})
                return result

            config = work / "fixed-config.yaml"
            config.write_text("{}\n", encoding="utf-8", newline="\n")
            files_checked = 0
            for file_relative, source_sha, control_sha in files:
                original_path = original.joinpath(*PurePosixPath(file_relative).parts)
                control_path = control.joinpath(*PurePosixPath(file_relative).parts)
                try:
                    original_observation = _flatten(binary, original_path, runner, env, config, deadline)
                    control_observation = _flatten(binary, control_path, runner, env, config, deadline)
                except ValueError as exc:
                    result.update({"status": "UNKNOWN", "detail": str(exc), "file": file_relative})
                    return result
                files_checked += 1
                result["files_checked"] = files_checked
                file_result = result["files"][files_checked - 1]
                file_result.update({
                    "original_backend_status": original_observation["status"],
                    "local_json_backend_status": control_observation["status"],
                    "original_output_sha256": original_observation["output_sha256"],
                    "local_json_output_sha256": control_observation["output_sha256"],
                })
                if original_observation["status"] == control_observation["status"] == "REJECTED":
                    result.update({"status": "UNSUPPORTED", "file": file_relative,
                                   "detail": "oasdiff could not flatten either representation of a captured file"})
                    return result
                differs = (original_observation["status"] != control_observation["status"]
                           or original_observation["status"] == "PARSED"
                           and not _same(original_observation["value"], control_observation["value"]))
                if not differs:
                    continue

                try:
                    replay_original = _flatten(binary, original_path, runner, env, config, deadline)
                    replay_control = _flatten(binary, control_path, runner, env, config, deadline)
                except ValueError as exc:
                    result.update({"status": "UNKNOWN", "detail": str(exc), "file": file_relative})
                    return result
                if not _observations_equal(original_observation, replay_original) \
                        or not _observations_equal(control_observation, replay_control):
                    result.update({"status": "UNKNOWN", "file": file_relative,
                                   "detail": "fresh-process replay did not preserve the flattened outputs"})
                    return result
                diffs: list[dict[str, Any]] = []
                truncated = False
                if original_observation["status"] == control_observation["status"] == "PARSED":
                    diffs, truncated = _differences(original_observation["value"],
                                                    control_observation["value"])
                result.update({
                    "status": "DISAGREEMENT",
                    "file": file_relative,
                    "file_source_sha256": source_sha,
                    "local_json_sha256": control_sha,
                    "original_backend_status": original_observation["status"],
                    "local_json_backend_status": control_observation["status"],
                    "original_output_sha256": original_observation["output_sha256"],
                    "local_json_output_sha256": control_observation["output_sha256"],
                    "differences": diffs,
                    "differences_truncated": truncated,
                    "fresh_process_replay_count": 1,
                })
                if not _within_result_limit(result):
                    result = {"operation": result["operation"], "status": "UNKNOWN", "path": relative,
                              "backend": result["backend"], "backend_sha256": backend_sha256,
                              "detail": "parser disagreement evidence exceeded the result size limit"}
                return result

            result.update({"status": "CONSISTENT_IN_FLATTENED_MODEL",
                           "files_checked": files_checked,
                           "fresh_process_replay_count": 0})
            if not _within_result_limit(result):
                return {"operation": result["operation"], "status": "UNKNOWN", "path": relative,
                        "backend": result["backend"], "backend_sha256": backend_sha256,
                        "detail": "parser consistency evidence exceeded the result size limit"}
            return result
    except openapi._ParserUnavailable as exc:
        result.update({"status": "UNAVAILABLE", "detail": str(exc)})
    except openapi._UnsupportedDocument as exc:
        result.update({"status": "UNSUPPORTED", "detail": str(exc)})
    except openapi._DeadlineExceeded:
        result.update({"status": "UNKNOWN", "detail": "parser consistency inspection exceeded its total time budget"})
    except openapi._BackendOutputExceeded:
        result.update({"status": "UNKNOWN", "detail": "oasdiff output exceeded the size limit"})
    except subprocess.TimeoutExpired:
        result.update({"status": "UNKNOWN", "detail": "parser consistency inspection exceeded its total time budget"})
    except OSError:
        result.update({"status": "UNAVAILABLE", "detail": "the configured oasdiff executable or source file could not be accessed"})
    return result

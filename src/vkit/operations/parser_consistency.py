"""Compare captured OpenAPI files with their local-parser JSON controls."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from . import openapi
from ._runtime import pinned_binary

MAX_TIMEOUT_SECONDS = openapi.MAX_TIMEOUT_SECONDS
MAX_CONTROL_FILE_BYTES = openapi.MAX_OPENAPI_FILE_BYTES
MAX_CONTROL_TOTAL_BYTES = openapi.MAX_OPENAPI_TOTAL_BYTES
MAX_MODEL_NODES = 100_000
MAX_MODEL_DEPTH = 128
MAX_DIFFERENCES = 32
MAX_VALUE_PREVIEW = 160
MAX_RESULT_BYTES = 262_144


class _UnrepresentableValue(Exception):
    def __init__(self, pointer: str, value_type: str):
        super().__init__(pointer, value_type)
        self.pointer = pointer
        self.value_type = value_type


class _TraversalLimitExceeded(Exception):
    pass


class _ControlSizeExceeded(Exception):
    pass


def _pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in parts)


def _validate_json_value(value: Any, deadline: float) -> None:
    stack: list[tuple[Any, tuple[str, ...], int]] = [(value, (), 0)]
    nodes = 0
    while stack:
        openapi._check_deadline(deadline)
        current, parts, depth = stack.pop()
        nodes += 1
        if nodes > MAX_MODEL_NODES:
            raise _TraversalLimitExceeded("model node limit exceeded")
        if depth > MAX_MODEL_DEPTH:
            raise _TraversalLimitExceeded("model depth limit exceeded")
        value_type = type(current)
        if current is None or value_type in (bool, int, str):
            continue
        if value_type is float:
            if not math.isfinite(current):
                raise _UnrepresentableValue(_pointer(parts), "non-finite number")
            continue
        if value_type is list:
            if nodes + len(stack) + len(current) > MAX_MODEL_NODES:
                raise _TraversalLimitExceeded("model node limit exceeded")
            for index, item in enumerate(current):
                openapi._check_deadline(deadline)
                stack.append((item, (*parts, str(index)), depth + 1))
            continue
        if value_type is dict:
            if nodes + len(stack) + 2 * len(current) > MAX_MODEL_NODES:
                raise _TraversalLimitExceeded("model node limit exceeded")
            nodes += len(current)
            for key, item in current.items():
                openapi._check_deadline(deadline)
                if type(key) is not str:
                    raise _UnrepresentableValue(_pointer(parts), f"mapping key {type(key).__name__}")
                stack.append((item, (*parts, key), depth + 1))
            continue
        raise _UnrepresentableValue(_pointer(parts), value_type.__name__)


def _iter_json_ascii(value: Any, deadline: float, depth: int = 0) -> Iterator[str]:
    openapi._check_deadline(deadline)
    if depth > MAX_MODEL_DEPTH:
        raise _TraversalLimitExceeded("model depth limit exceeded")
    kind = type(value)
    if kind is str:
        yield '"'
        for offset in range(0, len(value), 4096):
            openapi._check_deadline(deadline)
            encoded = json.dumps(value[offset:offset + 4096], ensure_ascii=True)
            yield encoded[1:-1]
        yield '"'
    elif kind is list:
        yield "["
        for index, item in enumerate(value):
            openapi._check_deadline(deadline)
            if index:
                yield ","
            yield from _iter_json_ascii(item, deadline, depth + 1)
        yield "]"
    elif kind is dict:
        yield "{"
        for index, key in enumerate(sorted(value)):
            openapi._check_deadline(deadline)
            if index:
                yield ","
            yield from _iter_json_ascii(key, deadline, depth)
            yield ":"
            yield from _iter_json_ascii(value[key], deadline, depth + 1)
        yield "}"
    else:
        try:
            yield json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        except (OverflowError, RecursionError, ValueError) as exc:
            raise _UnrepresentableValue("", "value rejected by the JSON encoder") from exc


def _json_chunks(value: Any, deadline: float, byte_limit: int) -> Iterator[bytes]:
    _validate_json_value(value, deadline)
    emitted = 0
    for chunk in _iter_json_ascii(value, deadline):
        openapi._check_deadline(deadline)
        if emitted + len(chunk) > byte_limit:
            raise _ControlSizeExceeded
        emitted += len(chunk)
        yield chunk.encode("ascii")


def _json_bytes(value: Any, deadline: float) -> bytearray:
    encoded = bytearray()
    for chunk in _json_chunks(value, deadline, MAX_CONTROL_FILE_BYTES):
        encoded.extend(chunk)
    return encoded


def _json_sha256(value: Any, deadline: float) -> str:
    digest = hashlib.sha256()
    for chunk in _json_chunks(value, deadline, openapi.MAX_BACKEND_OUTPUT_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _value_evidence(value: Any, deadline: float) -> dict[str, Any]:
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
    digest = _json_sha256(value, deadline)
    return {"type": "array" if kind is list else "object", "length": len(value),
            "sha256": digest}


_MISSING = object()


def _differences(left: Any, right: Any, deadline: float) -> tuple[list[dict[str, Any]], bool]:
    found: list[dict[str, Any]] = []
    truncated = False
    stack: list[tuple[Any, Any, tuple[str, ...], int]] = [(left, right, (), 0)]
    nodes = 0
    while stack:
        openapi._check_deadline(deadline)
        before, after, parts, depth = stack.pop()
        nodes += 1
        if nodes > MAX_MODEL_NODES:
            raise _TraversalLimitExceeded("model node limit exceeded")
        if depth > MAX_MODEL_DEPTH:
            raise _TraversalLimitExceeded("model depth limit exceeded")
        if before is not _MISSING and after is not _MISSING and type(before) is type(after) \
                and type(before) is dict:
            if nodes + len(stack) + len(before) + len(after) > MAX_MODEL_NODES:
                raise _TraversalLimitExceeded("model node limit exceeded")
            keys = sorted(before.keys() | after.keys(), reverse=True)
            if nodes + len(stack) + len(keys) > MAX_MODEL_NODES:
                raise _TraversalLimitExceeded("model node limit exceeded")
            for key in keys:
                openapi._check_deadline(deadline)
                stack.append((before.get(key, _MISSING), after.get(key, _MISSING),
                              (*parts, key), depth + 1))
            continue
        if before is not _MISSING and after is not _MISSING and type(before) is type(after) \
                and type(before) is list:
            max_length = max(len(before), len(after))
            if nodes + len(stack) + max_length > MAX_MODEL_NODES:
                raise _TraversalLimitExceeded("model node limit exceeded")
            for index in range(max_length - 1, -1, -1):
                openapi._check_deadline(deadline)
                stack.append((before[index] if index < len(before) else _MISSING,
                              after[index] if index < len(after) else _MISSING,
                              (*parts, str(index)), depth + 1))
            continue
        if before is not _MISSING and after is not _MISSING and type(before) is type(after):
            same = repr(before) == repr(after) if type(before) is float else before == after
            if same:
                continue
        if len(found) == MAX_DIFFERENCES:
            truncated = True
            break
        found.append({"pointer": _pointer(parts), "original": _value_evidence(before, deadline),
                      "local_json_control": _value_evidence(after, deadline)})
    return found, truncated


def _parse_backend_output(result: Any, deadline: float) -> dict[str, Any]:
    if result.stderr:
        raise ValueError("oasdiff emitted unexpected error output")
    if result.returncode == 102:
        return {"status": "REJECTED", "output_sha256": hashlib.sha256(result.stdout).hexdigest()}
    if result.returncode != 0:
        raise ValueError("oasdiff returned an unexpected status")
    try:
        output = json.loads(result.stdout, object_pairs_hook=openapi._json_object,
                            parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError,
            openapi._UnsupportedDocument) as exc:
        raise ValueError("oasdiff returned malformed flattened JSON") from exc
    if type(output) is not dict:
        raise ValueError("oasdiff flattened output must be a JSON object")
    try:
        model_sha256 = _json_sha256(output, deadline)
    except _UnrepresentableValue as exc:
        raise ValueError("oasdiff flattened output contains a non-JSON value") from exc
    return {"status": "PARSED", "value": output,
            "model_sha256": model_sha256,
            "output_sha256": hashlib.sha256(result.stdout).hexdigest()}


def _observations_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["status"] != right["status"]:
        return False
    if left["status"] == "REJECTED":
        return left["output_sha256"] == right["output_sha256"]
    return left["model_sha256"] == right["model_sha256"]


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
    return _parse_backend_output(result, deadline)


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
                    encoded = _json_bytes(model, deadline)
                except _UnrepresentableValue as exc:
                    detail = (f"the local parser produced a non-JSON {exc.value_type} value"
                              if exc.value_type in {"date", "datetime", "time"}
                              else "the local parser produced a value outside the JSON control model")
                    result.update({"status": "UNSUPPORTED", "unsupported_file": file_relative,
                                   "unsupported_pointer": exc.pointer, "unsupported_type": exc.value_type,
                                   "detail": detail})
                    return result
                except _TraversalLimitExceeded as exc:
                    result.update({"status": "UNSUPPORTED", "unsupported_file": file_relative,
                                   "detail": f"the local parser JSON control {exc}"})
                    return result
                except _ControlSizeExceeded:
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
                if original_observation["status"] == "PARSED":
                    file_result["original_model_sha256"] = original_observation["model_sha256"]
                if control_observation["status"] == "PARSED":
                    file_result["local_json_model_sha256"] = control_observation["model_sha256"]
                if original_observation["status"] == control_observation["status"] == "REJECTED":
                    result.update({"status": "UNSUPPORTED", "file": file_relative,
                                   "detail": "oasdiff could not flatten either representation of a captured file"})
                    return result
                differs = (original_observation["status"] != control_observation["status"]
                           or original_observation["status"] == "PARSED"
                           and original_observation["model_sha256"] != control_observation["model_sha256"])
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
                                                    control_observation["value"], deadline)
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
                if original_observation["status"] == "PARSED":
                    result["original_model_sha256"] = original_observation["model_sha256"]
                if control_observation["status"] == "PARSED":
                    result["local_json_model_sha256"] = control_observation["model_sha256"]
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
    except _ControlSizeExceeded:
        result.update({"status": "UNKNOWN", "detail": "flattened model evidence exceeded the size limit"})
    except _TraversalLimitExceeded as exc:
        result.update({"status": "UNKNOWN", "detail": f"flattened model {exc}"})
    except subprocess.TimeoutExpired:
        result.update({"status": "UNKNOWN", "detail": "parser consistency inspection exceeded its total time budget"})
    except OSError:
        result.update({"status": "UNAVAILABLE", "detail": "the configured oasdiff executable or source file could not be accessed"})
    return result

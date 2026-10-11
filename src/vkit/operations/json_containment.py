"""Compare bounded Draft 4 JSON Schema languages with a fixed fragment."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from jsonschema import Draft4Validator, SchemaError

from ._data import _object_without_duplicates, _reject_constant, read_json

BACKEND = "jsonsubschema"
BACKEND_VERSION = "0.0.8"
RUNTIME_VERSIONS = {
    "jsonsubschema": "0.0.8",
    "portion": "2.6.3",
    "greenery": "4.2.2",
    "jsonref": "1.1.0",
    "jsonschema": "4.26.0",
}
MAX_SCHEMA_BYTES = 512 * 1024
MAX_SCHEMA_DEPTH = 64
MAX_SCHEMA_NODES = 2_048
MAX_TIMEOUT_SECONDS = 60
_DRAFT4_URI = "http://json-schema.org/draft-04/schema#"
MODEL_SCOPE = (
    "Directional language containment for captured JSON Schema Draft 4 documents in vkit's "
    "fixed type, object, and homogeneous-array fragment, using jsonsubschema 0.0.8 and "
    "its pinned runtime dependencies. Results concern only this model and do not establish "
    "application behavior."
)
_SCHEMA_KEYWORDS = {"$schema", "type", "properties", "required", "additionalProperties", "items"}
_TYPES = {"null", "boolean", "integer", "number", "string", "array", "object"}
_UNSUPPORTED_STATUS = "UNSUPPORTED"


class _UnsupportedSchema(ValueError):
    pass


def _validate_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise _UnsupportedSchema("schema roots and nested schemas must be objects")
    dialect = schema.get("$schema")
    if dialect is not None and dialect != _DRAFT4_URI:
        raise _UnsupportedSchema("only JSON Schema Draft 4 is supported")

    nodes = 0

    def visit(value: Any, depth: int, *, root: bool = False) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SCHEMA_NODES:
            raise _UnsupportedSchema(f"schema exceeds {MAX_SCHEMA_NODES} nodes")
        if depth > MAX_SCHEMA_DEPTH:
            raise _UnsupportedSchema(f"schema nesting exceeds {MAX_SCHEMA_DEPTH} levels")
        if not isinstance(value, dict):
            raise _UnsupportedSchema("schema roots and nested schemas must be objects")
        unknown = set(value) - _SCHEMA_KEYWORDS
        if unknown:
            keyword = sorted(unknown)[0]
            raise _UnsupportedSchema(f"schema keyword {keyword!r} is outside the supported fragment")
        if "$schema" in value:
            if not root or value["$schema"] != _DRAFT4_URI:
                raise _UnsupportedSchema("only a root Draft 4 $schema declaration is supported")

        schema_type = value.get("type")
        constraints = set(value) - {"$schema", "type"}
        if schema_type is None:
            if constraints:
                raise _UnsupportedSchema("object and array constraints require an explicit type")
            return
        if not isinstance(schema_type, str) or schema_type not in _TYPES:
            raise _UnsupportedSchema("type must be one supported JSON type name")

        object_constraints = constraints & {"properties", "required", "additionalProperties"}
        if object_constraints and schema_type != "object":
            raise _UnsupportedSchema("object constraints require type 'object'")
        if "items" in constraints and schema_type != "array":
            raise _UnsupportedSchema("items requires type 'array'")

        if "properties" in value:
            properties = value["properties"]
            if not isinstance(properties, dict):
                raise _UnsupportedSchema("properties must be an object of schemas")
            for child in properties.values():
                visit(child, depth + 1)
        if "required" in value:
            required = value["required"]
            if (not isinstance(required, list) or any(not isinstance(item, str) for item in required)
                    or len(set(required)) != len(required)):
                raise _UnsupportedSchema("required must contain unique property names")
            properties = value.get("properties", {})
            if any(name not in properties for name in required):
                raise _UnsupportedSchema("required names must be declared in properties")
        if "additionalProperties" in value and type(value["additionalProperties"]) is not bool:
            raise _UnsupportedSchema("additionalProperties must be a boolean")
        if "items" in value:
            visit(value["items"], depth + 1)

    visit(schema, 1, root=True)
    try:
        Draft4Validator.check_schema(schema)
    except SchemaError as exc:
        raise _UnsupportedSchema("schema is invalid under Draft 4") from exc
    normalized = dict(schema)
    normalized.pop("$schema", None)
    return normalized


def _runtime_versions() -> tuple[dict[str, str], str | None]:
    installed: dict[str, str] = {}
    for package, expected in RUNTIME_VERSIONS.items():
        try:
            actual = version(package)
        except PackageNotFoundError:
            return installed, f"required {package} {expected} is not installed"
        installed[package] = actual
        if actual != expected:
            return installed, f"requires {package} {expected}; found {actual}"
    return installed, None


def _worker(request: Any) -> dict[str, Any]:
    installed, unavailable = _runtime_versions()
    if unavailable:
        return {"status": "UNAVAILABLE", "backend_version": installed.get(BACKEND),
                "runtime_versions": installed, "reason": unavailable}
    if not isinstance(request, dict) or set(request) != {"schema_a", "schema_b"}:
        return {"status": "UNKNOWN", "backend_version": BACKEND_VERSION,
                "runtime_versions": installed, "reason": "worker request is malformed"}
    try:
        from jsonsubschema import isSubschema
        from jsonsubschema.exceptions import _UnsupportedCase
    except Exception as exc:
        return {"status": "UNAVAILABLE", "backend_version": BACKEND_VERSION,
                "runtime_versions": installed, "reason": f"backend import failed: {type(exc).__name__}"}
    try:
        answer = isSubschema(request["schema_a"], request["schema_b"])
    except _UnsupportedCase as exc:
        return {"status": _UNSUPPORTED_STATUS, "backend_version": BACKEND_VERSION,
                "runtime_versions": installed, "reason": type(exc).__name__}
    except Exception as exc:
        return {"status": "UNKNOWN", "backend_version": BACKEND_VERSION,
                "runtime_versions": installed, "reason": f"backend raised {type(exc).__name__}"}
    if answer is None:
        status = "UNKNOWN"
    elif answer is True:
        status = "COMPATIBLE"
    elif answer is False:
        status = "BREAKING"
    else:
        return {"status": "UNKNOWN", "backend_version": BACKEND_VERSION,
                "runtime_versions": installed, "reason": "backend returned a non-boolean answer"}
    return {"status": status, "backend_version": BACKEND_VERSION,
            "runtime_versions": installed, "backend_answer": answer, "reason": None}


def _result(status: str, old_path: str, new_path: str, *, reason: str | None = None,
            old_digest: str | None = None, new_digest: str | None = None,
            backend_version: str | None = None, runtime_versions: Any = None,
            backend_answer: bool | None = None, elapsed_ms: int | None = None) -> dict[str, Any]:
    return {
        "operation": "compare_contracts",
        "profile": "json-containment",
        "status": status,
        "old_path": old_path,
        "new_path": new_path,
        "old_sha256": old_digest,
        "new_sha256": new_digest,
        "relation": "old_subset_new",
        "backend": BACKEND,
        "backend_version": backend_version,
        "runtime_versions": runtime_versions,
        "backend_answer": backend_answer,
        "witness": None,
        "witness_status": "not_generated",
        "reason": reason,
        "elapsed_ms": elapsed_ms,
        "model_scope": MODEL_SCOPE,
    }


def compare_json_containment(root: Path, old_path: str, new_path: str,
                             timeout_seconds: int = 20) -> dict[str, Any]:
    """Decide whether every JSON instance accepted by old is accepted by new."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")
    started = time.monotonic()
    old_digest: str | None = None
    new_digest: str | None = None
    try:
        old_schema, old_digest = read_json(
            root, old_path, max_bytes=MAX_SCHEMA_BYTES, reject_symlinks=True)
        new_schema, new_digest = read_json(
            root, new_path, max_bytes=MAX_SCHEMA_BYTES, reject_symlinks=True)
        # Two independently capped captures cannot exceed the combined limit.
        old_schema = _validate_schema(old_schema)
        new_schema = _validate_schema(new_schema)
    except (ValueError, OSError, RuntimeError) as exc:
        return _result(_UNSUPPORTED_STATUS, old_path, new_path, reason=str(exc),
                       old_digest=old_digest, new_digest=new_digest,
                       elapsed_ms=round((time.monotonic() - started) * 1000))

    request = json.dumps({"schema_a": old_schema, "schema_b": new_schema},
                         ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    remaining = timeout_seconds - (time.monotonic() - started)
    if remaining <= 0:
        return _result("UNKNOWN", old_path, new_path, reason="request exceeded its deadline",
                       old_digest=old_digest, new_digest=new_digest,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "vkit.operations.json_containment", "--worker"],
            input=request, capture_output=True, encoding="utf-8", timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result("UNKNOWN", old_path, new_path, reason="containment worker exceeded its deadline",
                       old_digest=old_digest, new_digest=new_digest,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    except OSError as exc:
        return _result("UNAVAILABLE", old_path, new_path,
                       reason=f"cannot start containment worker: {exc}",
                       old_digest=old_digest, new_digest=new_digest,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    if completed.returncode != 0:
        return _result("UNAVAILABLE", old_path, new_path,
                       reason="containment worker exited without a result",
                       old_digest=old_digest, new_digest=new_digest,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    try:
        response = json.loads(completed.stdout, object_pairs_hook=_object_without_duplicates,
                              parse_constant=_reject_constant)
    except (ValueError, json.JSONDecodeError, RecursionError):
        return _result("UNAVAILABLE", old_path, new_path,
                       reason="containment worker returned invalid JSON",
                       old_digest=old_digest, new_digest=new_digest,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    if not isinstance(response, dict):
        return _result("UNAVAILABLE", old_path, new_path,
                       reason="containment worker returned an invalid result",
                       old_digest=old_digest, new_digest=new_digest,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    status = response.get("status")
    backend_version = response.get("backend_version")
    runtime_versions = response.get("runtime_versions")
    if status == "UNAVAILABLE":
        return _result(status, old_path, new_path, reason=response.get("reason"),
                       old_digest=old_digest, new_digest=new_digest,
                       backend_version=backend_version, runtime_versions=runtime_versions,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    if (backend_version != BACKEND_VERSION or runtime_versions != RUNTIME_VERSIONS
            or status not in {"COMPATIBLE", "BREAKING", "UNKNOWN", _UNSUPPORTED_STATUS}):
        return _result("UNAVAILABLE", old_path, new_path,
                       reason="containment worker returned an invalid runtime or status",
                       old_digest=old_digest, new_digest=new_digest,
                       backend_version=backend_version, runtime_versions=runtime_versions,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
    answer = response.get("backend_answer")
    if status == "COMPATIBLE" and answer is not True:
        status = "UNAVAILABLE"
    elif status == "BREAKING" and answer is not False:
        status = "UNAVAILABLE"
    elif status == "UNKNOWN" and answer is not None:
        status = "UNAVAILABLE"
    return _result(status, old_path, new_path, reason=response.get("reason"),
                   old_digest=old_digest, new_digest=new_digest,
                   backend_version=backend_version, runtime_versions=runtime_versions,
                   backend_answer=answer if type(answer) is bool else None,
                   elapsed_ms=round((time.monotonic() - started) * 1000))


def _main() -> int:
    try:
        request = json.load(sys.stdin, object_pairs_hook=_object_without_duplicates,
                            parse_constant=_reject_constant)
        result = _worker(request)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
        return 0
    except (IndexError, KeyError, TypeError, ValueError):
        return 1


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_main())

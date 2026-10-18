"""Find cache-key collisions in the shared pure-function model."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .expressions import MODEL_SCOPE, FunctionModel, ParseFailure, ResourceLimits, parse_function
from ..nowindow import hidden_window
from .rewrites import _evaluate, _to_solver
from .synthesis import BACKEND, _load_cvc5

MAX_SOURCE_BYTES = 65_536
MAX_AST_NODES = 1_000
MAX_TIMEOUT_MS = 60_000
MAX_WORKER_INPUT_BYTES = MAX_SOURCE_BYTES * 8 + 1_024
MAX_WORKER_OUTPUT_BYTES = 200_000
_STATUSES = {"COLLISION", "NO_COLLISION_WITHIN_MODEL", "UNKNOWN", "UNSUPPORTED", "UNAVAILABLE"}


def _result(status: str, digest: str | None, **extra: Any) -> dict[str, Any]:
    return {"status": status, "model_scope": MODEL_SCOPE, "backend": BACKEND,
            "backend_version": None, "source_sha256": digest, "collision": None, **extra}


def _same_domain(value: FunctionModel, key: FunctionModel) -> bool:
    return (tuple(parameter.sort for parameter in value.parameters)
            == tuple(parameter.sort for parameter in key.parameters))


def _solve(source: str, function: str, key_function: str, timeout_ms: int) -> dict[str, Any]:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    limits = ResourceLimits(timeout_ms=timeout_ms, max_source_bytes=MAX_SOURCE_BYTES,
                            max_ast_nodes=MAX_AST_NODES)
    value_model = parse_function(source, function, limits)
    if isinstance(value_model, ParseFailure):
        return _result(value_model.status, digest, reason=f"value function: {value_model.reason}")
    key_model = parse_function(source, key_function, limits)
    if isinstance(key_model, ParseFailure):
        return _result(key_model.status, digest, reason=f"key function: {key_model.reason}")
    if not _same_domain(value_model, key_model):
        return _result("UNSUPPORTED", digest,
                       reason="key and value functions need the same positional parameter sorts")

    cvc5, unavailable = _load_cvc5()
    if cvc5 is None:
        return _result("UNAVAILABLE", digest, reason=unavailable)
    version = getattr(cvc5, "__version__", None)
    try:
        solver = cvc5.Solver()
        solver.setLogic("ALL")
        solver.setOption("produce-models", "true")
        solver.setOption("tlimit-per", str(timeout_ms))
        sorts = {"int": solver.getIntegerSort(), "bool": solver.getBooleanSort()}
        left_terms = [solver.mkConst(sorts[parameter.sort], f"left_{index}_{parameter.name}")
                      for index, parameter in enumerate(value_model.parameters)]
        right_terms = [solver.mkConst(sorts[parameter.sort], f"right_{index}_{parameter.name}")
                       for index, parameter in enumerate(value_model.parameters)]
        left_value = {parameter.name: term for parameter, term in zip(value_model.parameters, left_terms)}
        right_value = {parameter.name: term for parameter, term in zip(value_model.parameters, right_terms)}
        left_key = {parameter.name: term for parameter, term in zip(key_model.parameters, left_terms)}
        right_key = {parameter.name: term for parameter, term in zip(key_model.parameters, right_terms)}
        key_equal = solver.mkTerm(
            cvc5.Kind.EQUAL,
            _to_solver(key_model.body, left_key, solver, cvc5),
            _to_solver(key_model.body, right_key, solver, cvc5),
        )
        values_differ = solver.mkTerm(
            cvc5.Kind.NOT,
            solver.mkTerm(cvc5.Kind.EQUAL,
                          _to_solver(value_model.body, left_value, solver, cvc5),
                          _to_solver(value_model.body, right_value, solver, cvc5)),
        )
        solver.assertFormula(solver.mkTerm(cvc5.Kind.AND, key_equal, values_differ))
        checked = solver.checkSat()
        if checked.isUnsat():
            return _result("NO_COLLISION_WITHIN_MODEL", digest, backend_version=version)
        if checked.isUnknown():
            return _result("UNKNOWN", digest, backend_version=version, reason=str(checked))

        def arguments(model: FunctionModel, variables: dict[str, Any]) -> dict[str, int | bool]:
            result: dict[str, int | bool] = {}
            for parameter in model.parameters:
                term = solver.getValue(variables[parameter.name])
                result[parameter.name] = (bool(term.getBooleanValue()) if parameter.sort == "bool"
                                           else int(term.getIntegerValue()))
            return result

        left_arguments = arguments(value_model, left_value)
        right_arguments = arguments(value_model, right_value)
        left_key_arguments = arguments(key_model, left_key)
        right_key_arguments = arguments(key_model, right_key)
        left_key_value = _evaluate(key_model.body, left_key_arguments)
        right_key_value = _evaluate(key_model.body, right_key_arguments)
        left_result = _evaluate(value_model.body, left_arguments)
        right_result = _evaluate(value_model.body, right_arguments)
        if left_key_value != right_key_value or left_result == right_result:
            return _result("UNKNOWN", digest, backend_version=version,
                           reason="cvc5 model did not replay as a key collision with different values")
        return _result("COLLISION", digest, backend_version=version,
                       collision={
                           "left": {"arguments": left_arguments, "key": left_key_value, "value": left_result},
                           "right": {"arguments": right_arguments, "key": right_key_value, "value": right_result},
                       })
    except Exception as exc:
        return _result("UNKNOWN", digest, backend_version=version,
                       reason=f"cvc5 could not decide the relational query: {type(exc).__name__}: {exc}")


def _worker() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            raise ValueError("worker request exceeds its byte limit")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or set(request) != {"source", "function", "key_function", "timeout_ms"} \
                or not isinstance(request["source"], str) \
                or not isinstance(request["function"], str) or not request["function"] \
                or not isinstance(request["key_function"], str) or not request["key_function"] \
                or type(request["timeout_ms"]) is not int \
                or not 1 <= request["timeout_ms"] <= MAX_TIMEOUT_MS:
            raise ValueError("worker request has invalid fields")
        result = _solve(request["source"], request["function"], request["key_function"], request["timeout_ms"])
    except Exception as exc:
        result = _result("UNKNOWN", None, reason=f"isolated worker could not complete: {type(exc).__name__}")
    try:
        output = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        result = _result("UNKNOWN", None, reason="worker could not encode its result")
        output = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    if len(output.encode("utf-8")) > MAX_WORKER_OUTPUT_BYTES:
        result = _result("UNKNOWN", None, reason="worker result exceeds its byte limit")
        output = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.write(output)
    return 0


def find_cache_key_collision(root: Path, path: str, function: str, key_function: str,
                             timeout_ms: int = 2_000) -> dict[str, Any]:
    """Search the full supported input domain for equal keys with different values."""
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be an integer from 1 to {MAX_TIMEOUT_MS}")
    from . import _source

    source = _source(root, path)
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return _result("UNSUPPORTED", None, reason="source exceeds the cache-key analysis byte limit",
                       operation="find_cache_key_collision", path=path,
                       function=function, key_function=key_function)
    request = json.dumps({"source": source, "function": function, "key_function": key_function,
                          "timeout_ms": timeout_ms}, ensure_ascii=True,
                         allow_nan=False, separators=(",", ":"))
    if len(request.encode("utf-8")) > MAX_WORKER_INPUT_BYTES:
        return _result("UNSUPPORTED", hashlib.sha256(source.encode("utf-8")).hexdigest(),
                       reason="worker request exceeds its byte limit", operation="find_cache_key_collision",
                       path=path, function=function, key_function=key_function)
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "vkit.operations.cache_keys", "--worker"], input=request,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
            timeout=timeout_ms / 1_000, check=False, **hidden_window(),
        )
    except subprocess.TimeoutExpired:
        result = _result("UNKNOWN", hashlib.sha256(source.encode("utf-8")).hexdigest(),
                         reason="isolated cache-key solver exceeded its wall deadline")
    except OSError as exc:
        result = _result("UNAVAILABLE", hashlib.sha256(source.encode("utf-8")).hexdigest(),
                         reason=f"cannot start isolated cache-key worker: {exc}")
    else:
        if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > MAX_WORKER_OUTPUT_BYTES:
            result = _result("UNKNOWN", hashlib.sha256(source.encode("utf-8")).hexdigest(),
                             reason="isolated cache-key worker returned no bounded result")
        else:
            try:
                result = json.loads(completed.stdout)
            except json.JSONDecodeError:
                result = _result("UNKNOWN", hashlib.sha256(source.encode("utf-8")).hexdigest(),
                                 reason="isolated cache-key worker returned invalid JSON")
            if not isinstance(result, dict) or result.get("status") not in _STATUSES:
                result = _result("UNKNOWN", hashlib.sha256(source.encode("utf-8")).hexdigest(),
                                 reason="isolated cache-key worker returned an invalid result")
    return {"operation": "find_cache_key_collision", "path": path, "function": function,
            "key_function": key_function, **result}


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker())

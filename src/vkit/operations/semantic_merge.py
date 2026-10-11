"""Check branch-change preservation for four captured pure function models."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import _source
from ._data import encode_json, read_json
from .expressions import ParseFailure, ResourceLimits, parse_function
from .rewrites import _evaluate, _same_signature, _to_solver

BACKEND_VERSION = "1.4.2"
MAX_REQUEST = 2_000_000
MAX_OUTPUT = 80_000
ROLES = ("base", "left", "right", "merged")
MODEL_SCOPE = (
    "Pointwise scalar return preservation over the supported pure integer/boolean function model. "
    "Every left or right return change relative to base must be retained by merged; inputs unchanged "
    "by both branches must retain base. Covers mathematical integers and booleans, not side effects, "
    "exceptions, source-edit intent, application integration, or whole-repository merge safety."
)


def _violations(values: dict[str, int | bool]) -> list[str]:
    base, left, right, merged = (values[role] for role in ROLES)
    return [name for name, failed in (
        ("left_change_lost", left != base and merged != left),
        ("right_change_lost", right != base and merged != right),
        ("unchanged_input_modified", left == base and right == base and merged != base),
    ) if failed]


def _check(sources: dict[str, str], function: str, timeout_ms: int) -> dict[str, Any]:
    limits = ResourceLimits(timeout_ms=timeout_ms, max_source_bytes=65_536, max_ast_nodes=1_000)
    models = {role: parse_function(sources[role], function, limits) for role in ROLES}
    for role, model in models.items():
        if isinstance(model, ParseFailure):
            return {"status": model.status, "reason": f"{role}: {model.reason}"}
    base = models["base"]
    if any(not _same_signature(base, model) for model in models.values()):
        return {"status": "UNSUPPORTED", "reason": "all four functions need identical names and annotated signatures"}
    try:
        import cvc5
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": "pinned cvc5 is not installed"}
    backend = {"name": "cvc5", "version": getattr(cvc5, "__version__", None)}
    if backend["version"] != BACKEND_VERSION:
        return {"status": "UNAVAILABLE", "backend": backend, "reason": "cvc5 1.4.2 is required"}
    solver = cvc5.Solver()
    solver.setLogic("ALL")
    solver.setOption("produce-models", "true")
    solver.setOption("tlimit-per", str(timeout_ms))
    sorts = {"int": solver.getIntegerSort(), "bool": solver.getBooleanSort()}
    variables = {parameter.name: solver.mkConst(sorts[parameter.sort], f"arg_{index}")
                 for index, parameter in enumerate(base.parameters)}
    terms = {role: _to_solver(model.body, variables, solver, cvc5) for role, model in models.items()}
    kind = cvc5.Kind
    def equal(a: str, b: str):
        return solver.mkTerm(kind.EQUAL, terms[a], terms[b])
    def different(a: str, b: str):
        return solver.mkTerm(kind.NOT, equal(a, b))
    lost_left = solver.mkTerm(kind.AND, different("left", "base"), different("merged", "left"))
    lost_right = solver.mkTerm(kind.AND, different("right", "base"), different("merged", "right"))
    unexpected = solver.mkTerm(kind.AND, equal("left", "base"), equal("right", "base"), different("merged", "base"))
    solver.assertFormula(solver.mkTerm(kind.OR, lost_left, lost_right, unexpected))
    answer = solver.checkSat()
    if answer.isUnsat():
        return {"status": "PROVED", "backend": backend}
    if not answer.isSat():
        return {"status": "UNKNOWN", "backend": backend, "reason": str(answer)}
    inputs = {}
    for parameter in base.parameters:
        value = solver.getValue(variables[parameter.name])
        inputs[parameter.name] = (bool(value.getBooleanValue()) if parameter.sort == "bool"
                                  else int(value.getIntegerValue()))
    observations = {role: _evaluate(model.body, inputs) for role, model in models.items()}
    failed = _violations(observations)
    if not failed:
        return {"status": "UNKNOWN", "backend": backend, "reason": "solver witness failed independent IR replay"}
    return {"status": "COUNTEREXAMPLE", "backend": backend, "arguments": inputs,
            "returns": observations, "violations": failed, "replayed": True}


def _worker() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("worker request exceeds the byte limit")
        request = json.loads(raw)
        if (not isinstance(request, dict) or set(request) != {"sources", "function", "timeout_ms"}
                or not isinstance(request["sources"], dict) or set(request["sources"]) != set(ROLES)
                or any(not isinstance(source, str) for source in request["sources"].values())
                or not isinstance(request["function"], str)
                or type(request["timeout_ms"]) is not int or not 1 <= request["timeout_ms"] <= 60_000):
            raise ValueError("invalid semantic merge worker request")
        result = _check(**request)
        print(encode_json(result, max_bytes=MAX_OUTPUT))
    except Exception as exc:
        print(encode_json({"status": "UNKNOWN", "reason": f"merge analysis could not complete: {type(exc).__name__}"},
                          max_bytes=MAX_OUTPUT))


def check_semantic_merge(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be from 1 to 60")
    result = {"operation": "check_semantic_merge", "model_scope": MODEL_SCOPE,
              "timeout_seconds": timeout_seconds}
    try:
        bundle, digest = read_json(root, path, max_bytes=16_000, reject_symlinks=True)
        result["input_sha256"] = digest
        if (not isinstance(bundle, dict) or set(bundle) != {"version", "function", *ROLES}
                or type(bundle["version"]) is not int or bundle["version"] != 1
                or not isinstance(bundle["function"], str) or not bundle["function"].isidentifier()
                or any(not isinstance(bundle[role], str) or not bundle[role] for role in ROLES)):
            raise ValueError("bundle needs version 1, function, and base/left/right/merged Python paths")
        result["function"] = bundle["function"]
        sources = {role: _source(root, bundle[role]) for role in ROLES}
        result["sources"] = {role: {"path": bundle[role], "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
                             for role, source in sources.items()}
    except (ValueError, OSError, RecursionError) as exc:
        return {**result, "status": "UNSUPPORTED", "reason": str(exc)}
    from ..nowindow import hidden_window

    try:
        worker = subprocess.run([sys.executable, "-I", "-m", "vkit.operations.semantic_merge"],
                                input=encode_json({"sources": sources, "function": bundle["function"],
                                                   "timeout_ms": timeout_seconds * 1_000}, max_bytes=MAX_REQUEST),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                timeout=timeout_seconds, **hidden_window())
        if worker.returncode or len(worker.stdout.encode("utf-8")) > MAX_OUTPUT:
            raise ValueError("merge worker did not return bounded evidence")
        evidence = json.loads(worker.stdout)
        if not isinstance(evidence, dict):
            raise ValueError("merge worker returned a non-object")
        return {**result, **evidence}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**result, "status": "UNKNOWN", "reason": str(exc)}


if __name__ == "__main__":
    _worker()

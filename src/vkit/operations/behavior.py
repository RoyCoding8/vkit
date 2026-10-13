"""Replay concrete behavioral differences found in a closed Python language profile."""
from __future__ import annotations

import ast
import copy
import importlib
import json
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ._data import encode_json, read_json

BACKEND_VERSION = "0.0.111"
MAX_BYTES = 40_000
MAX_OUTPUT = 40_000
MODEL_SCOPE = (
    "Concrete CPython behavior of two selected functions in the installed closed Python profile. "
    "Supports integers, booleans, strings, integer lists, local computation, branches and loops. "
    "Module imports, global access, arbitrary attributes and calls are rejected before execution. "
    "A witness is replayed twice in fresh CPython processes, including returned values, exceptions "
    "and mutated arguments. No witness found is UNKNOWN, never equivalence. "
    "Worker deadlines are resource limits, not an operating-system sandbox."
)
_CALLS = {"abs": abs, "len": len, "range": range, "min": min, "max": max, "sum": sum, "sorted": sorted}
_TYPES = {"int": int, "bool": bool, "str": str, "list": list}
MULTISET_RELATION = "integer-multiset-statistic"
_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
    ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass, ast.Expr,
    ast.Name, ast.Constant, ast.List, ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp,
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Attribute,
    ast.Load, ast.Store, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn,
)


def _annotation(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name) and node.id in {"int", "bool", "str"}:
        return node.id
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id == "list" and isinstance(node.slice, ast.Name) and node.slice.id == "int"):
        return "list[int]"
    raise ValueError("annotations must be int, bool, str or list[int]")


def _parse(source: str, function: str) -> tuple[ast.Module, dict[str, str]]:
    if not isinstance(source, str) or len(source.encode("utf-8")) > 16_000:
        raise ValueError("each function source must be at most 16000 UTF-8 bytes")
    tree = ast.parse(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("source must contain exactly one plain function definition")
    fn = tree.body[0]
    args = fn.args
    if (fn.name != function or fn.decorator_list or getattr(fn, "type_params", [])
            or args.posonlyargs or args.kwonlyargs or args.defaults or args.kw_defaults
            or args.vararg or args.kwarg or len(args.args) > 4):
        raise ValueError("function needs at most four plain annotated parameters and no decorators/defaults")
    annotations = {arg.arg: _annotation(arg.annotation) for arg in args.args}
    _annotation(fn.returns)
    if len(annotations) != len(args.args):
        raise ValueError("duplicate parameter names")
    protected = set(_CALLS) | set(_TYPES) | {function}
    locals_ = set(annotations) | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if locals_ & protected or any("__" in name for name in locals_):
        raise ValueError("local names must not shadow builtins or the function")
    nodes = list(ast.walk(tree))
    if len(nodes) > 1_000:
        raise ValueError("function exceeds AST node limit")
    annotation_nodes = {id(n) for annotation in [fn.returns, *(a.annotation for a in args.args)]
                        for n in ast.walk(annotation)}
    for node in nodes:
        if not isinstance(node, _NODES):
            raise ValueError(f"unsupported Python syntax {type(node).__name__}")
        if isinstance(node, ast.Name) and id(node) not in annotation_nodes:
            if node.id not in locals_ and node.id not in _CALLS:
                raise ValueError(f"global or unknown name {node.id!r}")
        if isinstance(node, ast.Constant) and (
                type(node.value) not in {int, bool, str, type(None)}
                or type(node.value) is int and node.value.bit_length() > 128
                or isinstance(node.value, str) and len(node.value) > 256):
            raise ValueError("unsupported or oversized Python literal")
        if isinstance(node, ast.Attribute):
            if not (node.attr == "append" and isinstance(node.value, ast.Name) and node.value.id in locals_):
                raise ValueError("only local integer-list append attributes are supported")
        if isinstance(node, ast.Call):
            if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                raise ValueError("keyword and expanded calls are unsupported")
            if isinstance(node.func, ast.Name) and node.func.id in _CALLS:
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "append" and len(node.args) == 1:
                continue
            raise ValueError("only installed builtin operations and local list append may be called")
    return tree, annotations


def _validate(raw: Any) -> dict[str, Any]:
    if (not isinstance(raw, dict) or set(raw) != {"version", "function", "before", "after"}
            or type(raw["version"]) is not int or raw["version"] != 1
            or not isinstance(raw["function"], str) or not raw["function"].isascii()
            or not raw["function"].isidentifier() or "__" in raw["function"]
            or raw["function"] in set(_CALLS) | set(_TYPES)):
        raise ValueError("bundle needs version 1, a plain function name, before and after source")
    _, before = _parse(raw["before"], raw["function"])
    _, after = _parse(raw["after"], raw["function"])
    if before != after or list(before) != list(after):
        raise ValueError("both functions must have identical parameter names, order and annotations")
    return raw


def _input_bundle(raw: Any) -> tuple[dict[str, Any], str | None]:
    if not isinstance(raw, dict) or "relation" not in raw:
        return _validate(raw), None
    if (set(raw) != {"version", "function", "source", "relation"}
            or raw["relation"] != MULTISET_RELATION):
        raise ValueError("relation bundle needs version, function, source and integer-multiset-statistic relation")
    pair = _validate({"version": raw["version"], "function": raw["function"],
                      "before": raw["source"], "after": raw["source"]})
    tree, arguments = _parse(raw["source"], raw["function"])
    function = tree.body[0]
    if len(arguments) != 1 or list(arguments.values()) != ["list[int]"] or _annotation(function.returns) not in {"int", "bool"}:
        raise ValueError("multiset statistic needs one list[int] argument and an int or bool return")
    if any(isinstance(node, (ast.Attribute, ast.AugAssign))
           or isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
           for node in ast.walk(function)):
        raise ValueError("multiset statistics cannot mutate lists or use augmented assignments")
    name = next(iter(arguments))
    function.body.insert(0, ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())],
                                     value=ast.Call(func=ast.Name(id="sorted", ctx=ast.Load()),
                                                    args=[ast.Name(id=name, ctx=ast.Load())], keywords=[])))
    pair["after"] = ast.unparse(ast.fix_missing_locations(tree))
    return _validate(pair), MULTISET_RELATION


def _function(source: str, name: str):
    tree, _ = _parse(source, name)
    namespace = {"__builtins__": {**_CALLS, **_TYPES}, "__name__": "vkit_closed_profile"}
    exec(compile(tree, "<vkit-closed-function>", "exec"), namespace)
    return namespace[name]


def _value(value: Any) -> Any:
    if type(value) is int and value.bit_length() <= 256 or type(value) in {bool, type(None)}:
        return value
    if type(value) is str and len(value) <= 256:
        return value
    if type(value) is list and len(value) <= 64 and all(type(v) is int and v.bit_length() <= 256 for v in value):
        return value
    raise ValueError("concrete value is outside the replay evidence profile")


def _arguments(bundle: dict[str, Any], raw: Any) -> dict[str, Any]:
    _, annotations = _parse(bundle["before"], bundle["function"])
    if not isinstance(raw, dict) or set(raw) != set(annotations):
        raise ValueError("witness arguments do not match parameters")
    for name, kind in annotations.items():
        value = _value(raw[name])
        expected = {"int": int, "bool": bool, "str": str, "list[int]": list}[kind]
        if type(value) is not expected:
            raise ValueError("witness argument has the wrong concrete type")
    return raw


def _search(bundle: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    importlib.import_module("crosshair.core_and_libs")
    from crosshair.diff_behavior import diff_behavior
    from crosshair.fnutil import FunctionInfo
    from crosshair.options import DEFAULT_OPTIONS

    options = DEFAULT_OPTIONS.overlay(max_iterations=128, per_path_timeout=1.0,
                                      per_condition_timeout=float(timeout_seconds),
                                      max_uninteresting_iterations=32)
    differences = diff_behavior(FunctionInfo.from_fn(_function(bundle["before"], bundle["function"])),
                                FunctionInfo.from_fn(_function(bundle["after"], bundle["function"])), options)
    if isinstance(differences, str):
        return {"status": "UNKNOWN", "reason": "symbolic search could not analyze the function pair"}
    for difference in differences:
        try:
            if any(len(value) > 16_000 for value in difference.args.values()):
                continue
            arguments = {name: ast.literal_eval(value) for name, value in difference.args.items()}
            return {"status": "CANDIDATE", "arguments": _arguments(bundle, arguments)}
        except (ValueError, SyntaxError, TypeError, RecursionError):
            continue
    return {"status": "UNKNOWN", "reason": "no replayable counterexample found; equivalence is not established"}


def _replay(bundle: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    arguments = _arguments(bundle, arguments)
    observations = {}
    for side in ("before", "after"):
        args = copy.deepcopy(arguments)
        try:
            returned = _function(bundle[side], bundle["function"])(**args)
        except Exception as exc:
            outcome = {"exception": {"type": type(exc).__name__, "message": str(exc)[:1_000]}}
        else:
            outcome = {"return": _value(returned)}
        observations[side] = {**outcome, "arguments_after": {k: _value(v) for k, v in args.items()}}
    return observations


def _worker() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("worker input exceeded its byte limit")
        request = json.loads(raw)
        if not isinstance(request, dict) or request.get("mode") not in {"search", "replay"}:
            raise ValueError("unknown worker mode")
        expected = {"bundle", "mode", "timeout_seconds"} if request["mode"] == "search" else {"bundle", "mode", "arguments"}
        if set(request) != expected:
            raise ValueError("unknown or missing worker fields")
        if request["mode"] == "search" and (type(request["timeout_seconds"]) is not int
                                            or not 1 <= request["timeout_seconds"] <= 60):
            raise ValueError("invalid worker search deadline")
        bundle = _validate(request["bundle"])
        if request["mode"] == "search":
            result = _search(bundle, request["timeout_seconds"])
        else:
            result = _replay(bundle, request["arguments"])
        print(encode_json(result, max_bytes=MAX_OUTPUT))
    except Exception as exc:
        print(encode_json({"status": "UNKNOWN", "reason": f"behavior worker could not complete: {type(exc).__name__}"},
                          max_bytes=MAX_OUTPUT))


def find_behavioral_difference(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be from 1 to 60")
    result = {"status": "UNSUPPORTED", "model_scope": MODEL_SCOPE}
    try:
        raw, digest = read_json(root, path, max_bytes=MAX_BYTES, reject_symlinks=True)
        bundle, relation = _input_bundle(raw)
        result["input_sha256"] = digest
        if relation is not None:
            result["relation"] = relation
            result["model_scope"] = (
                "Order independence of a declared integer multiset statistic in the installed closed Python profile. "
                "The installed transformation compares the selected function on a list and its sorted permutation. "
                "Only returned int/bool values and exceptions are compared; input mutation is unsupported. "
                "A violation is replayed twice in fresh CPython processes. No witness is UNKNOWN, never a proof. "
                "The declaration that the function is intended to be order independent is supplied by the caller."
            )
    except (ValueError, SyntaxError, RecursionError) as exc:
        return {**result, "reason": str(exc)}
    try:
        installed = version("crosshair-tool")
    except PackageNotFoundError:
        installed = None
    result["backend"] = {"name": "crosshair-tool", "version": installed, "python": sys.version.split()[0]}
    if installed != BACKEND_VERSION or sys.implementation.name != "cpython":
        return {**result, "status": "UNAVAILABLE", "reason": "pinned CrossHair 0.0.111 on CPython is required"}
    deadline = time.monotonic() + timeout_seconds
    from ..nowindow import hidden_window

    def run(request):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("behavior deadline exceeded")
        process = subprocess.run([sys.executable, "-I", "-m", "vkit.operations.behavior"],
                                 input=encode_json({"bundle": bundle, **request}, max_bytes=MAX_BYTES),
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, encoding="utf-8", timeout=remaining,
                                 **hidden_window())
        if process.returncode or len(process.stdout.encode("utf-8")) > MAX_OUTPUT:
            raise ValueError("behavior worker did not produce bounded evidence")
        output = json.loads(process.stdout)
        if not isinstance(output, dict):
            raise ValueError("behavior worker returned a non-object result")
        return output

    try:
        candidate = run({"mode": "search", "timeout_seconds": max(1, timeout_seconds // 3)})
        if candidate.get("status") != "CANDIDATE":
            return {**result, "status": "UNKNOWN", "reason": candidate.get("reason", "no witness found")}
        arguments = _arguments(bundle, candidate["arguments"])
        first = run({"mode": "replay", "arguments": arguments})
        second = run({"mode": "replay", "arguments": arguments})
        if (set(first) != {"before", "after"} or first != second
                or encode_json(first["before"], max_bytes=MAX_OUTPUT) == encode_json(first["after"], max_bytes=MAX_OUTPUT)):
            return {**result, "status": "UNKNOWN", "reason": "candidate did not reproduce a stable concrete difference"}
        relation_evidence = {}
        if relation is not None:
            for observation in first.values():
                if (observation["arguments_after"] != arguments
                        or "return" in observation and type(observation["return"]) not in {int, bool}):
                    return {**result, "status": "UNKNOWN", "reason": "replayed statistic violated the non-mutating int/bool profile"}
            relation_evidence["transformed_arguments"] = {name: sorted(values) for name, values in arguments.items()}
        return {**result, "status": "COUNTEREXAMPLE", "arguments": arguments,
                "observations": first, "replay_count": 2, **relation_evidence}
    except (OSError, ValueError, TypeError, KeyError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return {**result, "status": "UNKNOWN", "reason": str(exc)}


if __name__ == "__main__":
    _worker()

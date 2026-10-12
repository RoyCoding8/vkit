"""Exact named-array overlap and dependence relations for affine loop nests."""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import keyword
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import _source
from ._data import encode_json
from .affine import (
    BACKEND_VERSION,
    MAX_COORDINATES,
    MAX_LOOP_DEPTH,
    MAX_PARAMETERS,
    MAX_SET_PIECES,
    MAX_SOURCE_BYTES,
    _Affine,
    _Path,
    _Unsupported,
    _affine,
    _dnf,
    _extend_paths,
    _find_function,
    _parse_module,
    _parameters,
    _range,
    _tree_size,
    _unsupported,
)

BACKEND = "islpy"
MODEL_SCOPE = (
    "Exact mathematical-integer access relations for one captured Python function and one loop nest. "
    "Subscript base names identify distinct abstract arrays by assumption; loop order follows Python range order. "
    "Array aliasing, bounds, object effects, calls, and source outside the selected function are not modeled."
)
MAX_REQUEST = 12 * MAX_SOURCE_BYTES + 1_024
MAX_OUTPUT = 200_000
MAX_ACCESS_EVENTS = 256
MAX_RELATIONS = 256
MAX_TIMEOUT_MS = 60_000

Status = Literal["ANALYZED", "UNSUPPORTED", "UNAVAILABLE", "UNKNOWN"]


@dataclass(frozen=True)
class _Access:
    site: int
    array: str
    mode: Literal["READ", "WRITE"]
    ordinal: int
    line: int
    loop_names: tuple[str, ...]
    loop_values: tuple[_Affine, ...]
    piece: _Path
    coordinates: tuple[_Affine, ...]


def _access_index(node: ast.Subscript, scope: dict[str, _Affine]) -> tuple[_Affine, ...]:
    if not isinstance(node.value, ast.Name):
        raise _unsupported(node, "array accesses need a single named base")
    if node.value.id in scope:
        raise _unsupported(node, "array base cannot be a scalar parameter or loop variable")
    index = node.slice
    values = index.elts if isinstance(index, ast.Tuple) else (index,)
    if not values or len(values) > MAX_COORDINATES or any(isinstance(item, ast.Slice) for item in values):
        raise _unsupported(node, "array accesses need one to eight affine integer coordinates")
    return tuple(_affine(item, scope) for item in values)


def _validate_value(node: ast.AST, scope: dict[str, _Affine],
                    visit_access: Any) -> None:
    if isinstance(node, ast.Subscript):
        visit_access(node)
        return
    if isinstance(node, ast.Constant) and type(node.value) in (int, bool, str, bytes, type(None)):
        if type(node.value) not in (int, bool):
            raise _unsupported(node, "array expressions support only numeric scalar constants")
        return
    if isinstance(node, ast.Name):
        if node.id not in scope:
            raise _unsupported(node, f"name {node.id!r} is not a scalar loop value or parameter")
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        _validate_value(node.operand, scope, visit_access)
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        _validate_value(node.left, scope, visit_access)
        _validate_value(node.right, scope, visit_access)
        return
    raise _unsupported(node, "array values permit only integer expressions and named array reads")


def _lower_accesses(source: str, function_name: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[_Access, ...]]:
    if (not isinstance(function_name, str) or len(function_name) > 256
            or not function_name.isidentifier() or keyword.iskeyword(function_name)):
        raise _Unsupported("function must be an identifier of at most 256 characters")
    module = _parse_module(source)
    function = _find_function(module, function_name)
    _tree_size(function)
    parameters = _parameters(function)
    if len(parameters) > MAX_PARAMETERS:
        raise _unsupported(function, "function exceeds the parameter limit")
    scope = {name: _Affine.variable(f"p{index}") for index, name in enumerate(parameters)}
    accesses: list[_Access] = []
    loop_names: list[str] = []
    loop_symbols: list[str] = []
    loop_values: list[_Affine] = []
    loop_targets: set[str] = set()
    next_loop = 0
    next_site = 0
    next_ordinal = 0
    array_ranks: dict[str, int] = {}

    body = list(function.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body.pop(0)

    def emit(node: ast.Subscript, mode: Literal["READ", "WRITE"], paths: tuple[_Path, ...],
             current_scope: dict[str, _Affine]) -> None:
        nonlocal next_site, next_ordinal
        if not loop_symbols:
            raise _unsupported(node, "array accesses must be inside one affine range loop nest")
        if not isinstance(node.value, ast.Name):
            raise _unsupported(node, "array accesses need a single named base")
        coordinates = _access_index(node, current_scope)
        array = node.value.id
        previous_rank = array_ranks.setdefault(array, len(coordinates))
        if previous_rank != len(coordinates):
            raise _unsupported(node, f"array {array!r} is accessed with inconsistent coordinate counts")
        site = next_site
        next_site += 1
        ordinal = next_ordinal
        next_ordinal += 1
        for path in paths:
            accesses.append(_Access(site, array, mode, ordinal, getattr(node, "lineno", 1),
                                    tuple(loop_names), tuple(loop_values), path, coordinates))
        if len(accesses) > MAX_ACCESS_EVENTS:
            raise _unsupported(node, "source exceeds the access-event limit")

    def read(node: ast.Subscript, paths: tuple[_Path, ...], current_scope: dict[str, _Affine]) -> None:
        emit(node, "READ", paths, current_scope)

    def visit_value(node: ast.AST, paths: tuple[_Path, ...], current_scope: dict[str, _Affine]) -> None:
        def visit_subscript(access: ast.Subscript) -> None:
            read(access, paths, current_scope)
        _validate_value(node, current_scope, visit_subscript)

    def visit(statements: list[ast.stmt], paths: tuple[_Path, ...],
              current_scope: dict[str, _Affine], depth: int) -> None:
        nonlocal next_loop, next_site, next_ordinal
        for statement in statements:
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.For):
                if statement.orelse or not isinstance(statement.target, ast.Name):
                    raise _unsupported(statement, "loops need one name target and no else clause")
                target = statement.target.id
                if target in current_scope or target in loop_targets or target == "range":
                    raise _unsupported(statement.target, "loop target cannot shadow a parameter or another loop")
                if depth >= MAX_LOOP_DEPTH:
                    raise _unsupported(statement, "loop nesting exceeds the depth limit")
                symbol = f"q{next_loop}"
                next_loop += 1
                index, constraints = _range(statement.iter, current_scope, symbol)
                loop_targets.add(target)
                loop_names.append(target)
                loop_symbols.append(symbol)
                loop_values.append(index)
                nested_scope = dict(current_scope)
                nested_scope[target] = index
                nested_paths = tuple(_Path(path.variables + (symbol,), path.constraints + constraints)
                                     for path in paths)
                if len(nested_paths) > MAX_SET_PIECES:
                    raise _unsupported(statement, "source expands beyond the set-piece limit")
                visit(statement.body, nested_paths, nested_scope, depth + 1)
                loop_values.pop()
                loop_symbols.pop()
                loop_names.pop()
                continue
            if isinstance(statement, ast.If):
                if any(isinstance(item, ast.Subscript) for item in ast.walk(statement.test)):
                    raise _unsupported(statement.test, "array-dependent branch conditions are unsupported")
                yes = _extend_paths(paths, _dnf(statement.test, current_scope), statement)
                no = _extend_paths(paths, _dnf(statement.test, current_scope, negated=True), statement)
                visit(statement.body, yes, current_scope, depth)
                visit(statement.orelse, no, current_scope, depth)
                continue
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Subscript):
                    raise _unsupported(statement, "only one named-array store per assignment is supported")
                visit_value(statement.value, paths, current_scope)
                target = statement.targets[0]
                if not isinstance(target.value, ast.Name):
                    raise _unsupported(target, "array stores need a single named base")
                coordinates = _access_index(target, current_scope)
                array = target.value.id
                previous_rank = array_ranks.setdefault(array, len(coordinates))
                if previous_rank != len(coordinates):
                    raise _unsupported(target, f"array {array!r} is accessed with inconsistent coordinate counts")
                site = next_site
                next_site += 1
                ordinal = next_ordinal
                next_ordinal += 1
                for path in paths:
                    accesses.append(_Access(site, array, "WRITE", ordinal, getattr(target, "lineno", 1),
                                            tuple(loop_names), tuple(loop_values), path, coordinates))
                continue
            if isinstance(statement, ast.AugAssign):
                if not isinstance(statement.target, ast.Subscript) or not isinstance(statement.op, (ast.Add, ast.Sub, ast.Mult)):
                    raise _unsupported(statement, "augmented stores must use a named array and +, -, or *")
                target = statement.target
                read(target, paths, current_scope)
                visit_value(statement.value, paths, current_scope)
                coordinates = _access_index(target, current_scope)
                site = next_site
                next_site += 1
                ordinal = next_ordinal
                next_ordinal += 1
                for path in paths:
                    accesses.append(_Access(site, target.value.id if isinstance(target.value, ast.Name) else "",
                                            "WRITE", ordinal, getattr(target, "lineno", 1),
                                            tuple(loop_names), tuple(loop_values), path, coordinates))
                continue
            if isinstance(statement, ast.Expr):
                visit_value(statement.value, paths, current_scope)
                continue
            raise _unsupported(statement, f"unsupported statement {type(statement).__name__}")

    visit(body, (_Path(),), scope, 0)
    if not accesses:
        raise _unsupported(function, "function has no array access inside a loop")
    signatures = {access.loop_names for access in accesses}
    if len(signatures) != 1:
        raise _Unsupported("all modeled accesses must share the same lexical loop nest")
    return parameters, accesses[0].loop_names, tuple(accesses)


def _rename(text: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    return text


def _affine_text(value: _Affine, mapping: dict[str, str]) -> str:
    return _rename(value.text(), mapping)


def _guard_text(access: _Access, mapping: dict[str, str]) -> list[str]:
    return [_rename(value, mapping) for value in access.piece.constraints]


def _eval_linear(node: ast.AST, values: dict[str, int]) -> int:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_linear(node.operand, values)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left, right = _eval_linear(node.left, values), _eval_linear(node.right, values)
        return left + right if isinstance(node.op, ast.Add) else left - right if isinstance(node.op, ast.Sub) else left * right
    raise ValueError("unsupported affine witness expression")


def _constraint_holds(text: str, values: dict[str, int]) -> bool:
    parsed = ast.parse(text, mode="eval").body
    if not isinstance(parsed, ast.Compare) or len(parsed.ops) != 1 or len(parsed.comparators) != 1:
        raise ValueError("invalid affine constraint")
    left, right = _eval_linear(parsed.left, values), _eval_linear(parsed.comparators[0], values)
    op = parsed.ops[0]
    return (left == right if isinstance(op, ast.Eq) else left != right if isinstance(op, ast.NotEq)
            else left < right if isinstance(op, ast.Lt) else left <= right if isinstance(op, ast.LtE)
            else left > right if isinstance(op, ast.Gt) else left >= right if isinstance(op, ast.GtE)
            else False)


def _evaluate_affine(value: _Affine, values: dict[str, int]) -> int:
    return value.constant + sum(coefficient * values[name] for name, coefficient in value.terms)


def _set_for_pair(left: _Access, right: _Access, params_count: int,
                  iter_count: int, isl: Any) -> Any:
    context = isl.DEFAULT_CONTEXT
    dimensions = [f"p{i}" for i in range(params_count)]
    dimensions.extend(f"l{i}" for i in range(iter_count))
    dimensions.extend(f"r{i}" for i in range(iter_count))
    left_map = {f"q{i}": f"l{i}" for i in range(iter_count)}
    right_map = {f"q{i}": f"r{i}" for i in range(iter_count)}
    constraints = _guard_text(left, left_map) + _guard_text(right, right_map)
    constraints.extend(
        f"{_affine_text(a, left_map)} = {_affine_text(b, right_map)}"
        for a, b in zip(left.coordinates, right.coordinates)
    )
    schedule: list[str] = []
    for index in range(iter_count):
        equal_prefix = [f"l{prior} = r{prior}" for prior in range(index)]
        schedule.append(" and ".join([*equal_prefix, f"l{index} < r{index}"]))
    equal_iteration = [f"l{i} = r{i}" for i in range(iter_count)]
    if left.ordinal < right.ordinal:
        schedule.append(" and ".join(equal_iteration))
    if not schedule:
        return isl.Set.read_from_str(context, "{ [" + ", ".join(dimensions) + "] : 1 = 0 }")
    schedule_formula = " or ".join(f"({part})" for part in schedule)
    body = " and ".join([*constraints, f"({schedule_formula})"])
    return isl.Set.read_from_str(context, "{ [" + ", ".join(dimensions) + "] : " + body + " }")


def _check_witness(point: Any, isl: Any, left: _Access, right: _Access,
                   parameter_names: tuple[str, ...], loop_names: tuple[str, ...]) -> dict[str, Any] | None:
    count = len(parameter_names)
    values: list[int] = []
    for index in range(count + 2 * len(loop_names)):
        value = point.get_coordinate_val(isl.dim_type.set, index).to_python()
        if type(value) is not int or abs(value).bit_length() > 4096:
            return None
        values.append(value)
    parameters = dict(zip(parameter_names, values[:count]))
    left_q = values[count:count + len(loop_names)]
    right_q = values[count + len(loop_names):]
    left_values = {f"p{i}": value for i, value in enumerate(values[:count])}
    right_values = dict(left_values)
    left_values.update({f"q{i}": value for i, value in enumerate(left_q)})
    right_values.update({f"q{i}": value for i, value in enumerate(right_q)})
    if not all(_constraint_holds(text, left_values) for text in left.piece.constraints):
        return None
    if not all(_constraint_holds(text, right_values) for text in right.piece.constraints):
        return None
    if tuple(_evaluate_affine(x, left_values) for x in left.coordinates) != tuple(
            _evaluate_affine(x, right_values) for x in right.coordinates):
        return None
    left_schedule = (*left_q, left.ordinal)
    right_schedule = (*right_q, right.ordinal)
    if not left_schedule < right_schedule:
        return None
    def source_iterations(access: _Access, q_values: list[int]) -> dict[str, int]:
        env = {f"p{i}": value for i, value in enumerate(values[:count])}
        env.update({f"q{i}": value for i, value in enumerate(q_values)})
        return {name: _evaluate_affine(affine, env) for name, affine in zip(loop_names, access.loop_values)}
    coordinate = [_evaluate_affine(x, left_values) for x in left.coordinates]
    earlier_iterations = source_iterations(left, left_q)
    later_iterations = source_iterations(right, right_q)
    if any(abs(value).bit_length() > 4096 for value in [*coordinate, *earlier_iterations.values(), *later_iterations.values()]):
        return None
    return {
        "parameters": parameters,
        "earlier": {"iterations": earlier_iterations, "line": left.line},
        "later": {"iterations": later_iterations, "line": right.line},
        "coordinate": coordinate,
        "replayed": True,
    }


def _worker(source: str, function: str) -> dict[str, Any]:
    try:
        parameter_names, loop_names, accesses = _lower_accesses(source, function)
    except _Unsupported as exc:
        return {"status": "UNSUPPORTED", "reason": str(exc), "backend_version": None}
    if sys.platform == "win32":
        return {"status": "UNAVAILABLE", "reason": "islpy has no supported native backend on Windows",
                "backend_version": None}
    try:
        version = importlib.metadata.version(BACKEND)
        if version != BACKEND_VERSION:
            return {"status": "UNAVAILABLE", "reason": f"requires islpy {BACKEND_VERSION}; found {version}",
                    "backend_version": version}
        import islpy as isl
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return {"status": "UNAVAILABLE", "reason": f"islpy {BACKEND_VERSION} is not installed",
                "backend_version": None}
    try:
        groups: dict[tuple[int, int, str], list[tuple[_Access, _Access, Any]]] = {}
        pair_count = 0
        for left in accesses:
            for right in accesses:
                if left.array != right.array or len(left.coordinates) != len(right.coordinates):
                    continue
                if left.mode == right.mode == "READ":
                    kind = "READ_READ"
                elif left.mode == "WRITE" and right.mode == "READ":
                    kind = "RAW"
                elif left.mode == "READ" and right.mode == "WRITE":
                    kind = "WAR"
                else:
                    kind = "WAW"
                pair_count += 1
                if pair_count > MAX_RELATIONS * 64:
                    return {"status": "UNKNOWN", "reason": "access-pair limit exceeded", "backend_version": version}
                relation = _set_for_pair(left, right, len(parameter_names), len(loop_names), isl)
                if relation.is_empty():
                    continue
                key = (left.site, right.site, kind)
                groups.setdefault(key, []).append((left, right, relation))
                if len(groups) > MAX_RELATIONS:
                    return {"status": "UNKNOWN", "reason": "relation count limit exceeded", "backend_version": version}
        output: list[dict[str, Any]] = []
        for (source_site, target_site, kind), candidates in sorted(groups.items()):
            merged = candidates[0][2]
            for _, _, relation in candidates[1:]:
                merged = merged.union(relation)
            witness = None
            for left, right, relation in candidates:
                witness = _check_witness(relation.sample_point(), isl, left, right, parameter_names, loop_names)
                if witness is not None:
                    break
            if witness is None:
                return {"status": "UNKNOWN", "reason": "ISL relation witness failed independent replay",
                        "backend_version": version}
            source_access, target_access = candidates[0][:2]
            output.append({
                "array": source_access.array,
                "kind": kind,
                "source": {"site": source_site, "line": source_access.line, "mode": source_access.mode},
                "target": {"site": target_site, "line": target_access.line, "mode": target_access.mode},
                "relation": str(merged),
                "witness": witness,
            })
        return {"status": "ANALYZED", "backend_version": version, "relations": output,
                "access_events": len(accesses), "assumptions": [
                    "different named array bases denote distinct arrays",
                    "subscripts denote pure access to abstract arrays with unbounded coordinates",
                    "integer parameters and indexes use mathematical-integer semantics",
                    "all accesses occur in one lexical range-loop nest",
                ]}
    except Exception:
        return {"status": "UNKNOWN", "reason": "ISL could not complete the exact access relation analysis",
                "backend_version": version}


def _worker_main() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            result = {"status": "UNSUPPORTED", "reason": "worker request exceeds source limit", "backend_version": None}
        else:
            request = json.loads(raw)
            if (not isinstance(request, dict) or set(request) != {"source", "function"}
                    or not isinstance(request["source"], str) or not isinstance(request["function"], str)):
                result = {"status": "UNKNOWN", "reason": "worker request has invalid shape", "backend_version": None}
            else:
                result = _worker(request["source"], request["function"])
        try:
            payload = encode_json(result, max_bytes=MAX_OUTPUT)
        except ValueError:
            payload = encode_json({"status": "UNKNOWN", "reason": "relation output exceeds the byte limit",
                                   "backend_version": result.get("backend_version")}, max_bytes=MAX_OUTPUT)
        print(payload)
    except Exception:
        print(json.dumps({"status": "UNKNOWN", "reason": "worker could not read request", "backend_version": None}))


def analyze_affine_accesses(root: Path, path: str, function: str,
                            timeout_ms: int = 2_000) -> dict[str, Any]:
    """Return exact overlap/dependence relations for supported named-array accesses."""
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be an integer from 1 to {MAX_TIMEOUT_MS}")
    if not isinstance(function, str) or len(function) > 256 or not function.isidentifier() or keyword.iskeyword(function):
        raise ValueError("function must be an identifier of at most 256 characters")
    source = _source(Path(root), path)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    request = json.dumps({"source": source, "function": function}, ensure_ascii=True, separators=(",", ":"))
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "vkit.operations.affine_accesses", "--worker"],
            input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", timeout=timeout_ms / 1000, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        result = {"status": "UNKNOWN", "reason": f"analysis exceeded {timeout_ms} ms", "backend_version": None}
    except OSError:
        result = {"status": "UNAVAILABLE", "reason": "cannot start isolated ISL worker", "backend_version": None}
    else:
        try:
            parsed = json.loads(completed.stdout) if completed.returncode == 0 else None
            if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT or not isinstance(parsed, dict):
                raise ValueError("worker returned invalid or oversized evidence")
            if (parsed.get("status") not in {"ANALYZED", "UNSUPPORTED", "UNAVAILABLE", "UNKNOWN"}
                    or (parsed.get("status") == "ANALYZED"
                        and (parsed.get("backend_version") != BACKEND_VERSION
                             or not isinstance(parsed.get("relations"), list)))):
                raise ValueError("worker returned an invalid result contract")
            result = parsed
        except (UnicodeError, json.JSONDecodeError, ValueError):
            result = {"status": "UNKNOWN", "reason": "ISL worker exited without bounded evidence", "backend_version": None}
    return {"operation": "analyze_affine_accesses", "path": path, "function": function,
            "source_sha256": digest, "model_scope": MODEL_SCOPE, "backend": BACKEND,
            "timeout_ms": timeout_ms, **result}


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker_main()

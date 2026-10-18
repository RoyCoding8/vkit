"""Exact comparison of restricted integer generator iteration sets."""
from __future__ import annotations

import ast
import hashlib
import json
import keyword
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

BACKEND = "islpy"
BACKEND_VERSION = "2026.2.2"
MODEL_SCOPE = (
    "Set equality for all mathematical integer parameter values in the selected "
    "function AST; built-in range is fixed, module globals are ignored, and yield "
    "order and multiplicity are ignored."
)

Status = Literal["EQUIVALENT", "COUNTEREXAMPLE", "UNSUPPORTED", "UNAVAILABLE", "UNKNOWN"]

MAX_SOURCE_BYTES = 65_536
MAX_REQUEST_CHARS = 12 * MAX_SOURCE_BYTES + 1_024
MAX_AST_NODES = 2_000
MAX_AST_DEPTH = 64
MAX_EXPRESSION_DEPTH = 32
MAX_PARAMETERS = 16
MAX_COORDINATES = 8
MAX_LOOP_DEPTH = 8
MAX_BOOLEAN_PIECES = 128
MAX_SET_PIECES = 256
MAX_INTEGER_BITS = 256
MAX_WITNESS_BITS = 4_096
MAX_TIMEOUT_MS = 60_000


class _Unsupported(ValueError):
    """The source is outside the supported generator language."""


@dataclass(frozen=True)
class _Affine:
    constant: int = 0
    terms: tuple[tuple[str, int], ...] = ()

    @classmethod
    def build(cls, constant: int, terms: dict[str, int] | None = None) -> _Affine:
        terms = terms or {}
        if abs(constant).bit_length() > MAX_INTEGER_BITS:
            raise _Unsupported("integer coefficient exceeds the bit limit")
        normalized = tuple(sorted((name, value) for name, value in terms.items() if value))
        if any(abs(value).bit_length() > MAX_INTEGER_BITS for _, value in normalized):
            raise _Unsupported("integer coefficient exceeds the bit limit")
        return cls(constant, normalized)

    @classmethod
    def variable(cls, name: str) -> _Affine:
        return cls(0, ((name, 1),))

    def add(self, other: _Affine) -> _Affine:
        terms = dict(self.terms)
        for name, coefficient in other.terms:
            terms[name] = terms.get(name, 0) + coefficient
        return self.build(self.constant + other.constant, terms)

    def scale(self, coefficient: int) -> _Affine:
        return self.build(
            self.constant * coefficient,
            {name: value * coefficient for name, value in self.terms},
        )

    def subtract(self, other: _Affine) -> _Affine:
        return self.add(other.scale(-1))

    def text(self) -> str:
        parts: list[str] = []
        for name, coefficient in self.terms:
            magnitude = abs(coefficient)
            term = name if magnitude == 1 else f"{magnitude}*{name}"
            parts.append(("-" if coefficient < 0 else "+") + term)
        if self.constant:
            parts.append(("-" if self.constant < 0 else "+") + str(abs(self.constant)))
        if not parts:
            return "0"
        first = parts[0]
        result = first[1:] if first.startswith("+") else first
        for part in parts[1:]:
            result += (" - " if part.startswith("-") else " + ") + part[1:]
        return result


@dataclass(frozen=True)
class _Piece:
    variables: tuple[str, ...]
    constraints: tuple[str, ...]
    coordinates: tuple[_Affine, ...]


@dataclass(frozen=True)
class _Generator:
    parameters: tuple[str, ...]
    coordinate_count: int
    pieces: tuple[_Piece, ...]


@dataclass(frozen=True)
class _Path:
    variables: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()


def _unsupported(node: ast.AST, message: str) -> _Unsupported:
    line = getattr(node, "lineno", None)
    location = f" at line {line}" if isinstance(line, int) else ""
    return _Unsupported(f"{message}{location}")


def _tree_size(root: ast.AST) -> tuple[int, int]:
    count = 0
    maximum_depth = 0
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        maximum_depth = max(maximum_depth, depth)
        if count > MAX_AST_NODES or maximum_depth > MAX_AST_DEPTH:
            raise _unsupported(node, "source exceeds the syntax-tree limit")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return count, maximum_depth


def _parse_module(source: str) -> ast.Module:
    try:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise _Unsupported(f"source exceeds {MAX_SOURCE_BYTES} bytes")
        module = ast.parse(source)
    except UnicodeEncodeError as exc:
        raise _Unsupported("source must contain Unicode scalar values") from exc
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise _Unsupported("source is not valid, bounded Python syntax") from exc
    return module


def _find_function(module: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef):
        raise _Unsupported(f"expected one top-level synchronous function named {name!r}")
    return matches[0]


def _parameters(function: ast.FunctionDef) -> tuple[str, ...]:
    args = function.args
    if (function.decorator_list or args.posonlyargs or args.vararg or args.kwonlyargs
            or args.kwarg or args.defaults or args.kw_defaults or function.returns is not None
            or getattr(function, "type_params", ())):
        raise _unsupported(function, "decorators, defaults, return annotations, and non-plain parameters are unsupported")
    names: list[str] = []
    for argument in args.args:
        if argument.annotation is None or not isinstance(argument.annotation, ast.Name) or argument.annotation.id != "int":
            raise _unsupported(argument, "each parameter must have the exact int annotation")
        if argument.arg == "range":
            raise _unsupported(argument, "a parameter cannot shadow the built-in range name")
        names.append(argument.arg)
    if len(names) > MAX_PARAMETERS:
        raise _unsupported(function, "function exceeds the parameter limit")
    if len(set(names)) != len(names):
        raise _unsupported(function, "parameter names must be unique")
    return tuple(names)


def _integer_constant(node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        value = node.value
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _integer_constant(node.operand)
        if isinstance(node.op, ast.USub):
            value = -value
    else:
        raise _unsupported(node, "expected an integer literal")
    if abs(value).bit_length() > MAX_INTEGER_BITS:
        raise _unsupported(node, "integer literal exceeds the bit limit")
    return value


def _affine(node: ast.AST, environment: dict[str, _Affine], depth: int = 0) -> _Affine:
    if depth > MAX_EXPRESSION_DEPTH:
        raise _unsupported(node, "affine expression exceeds the depth limit")
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return _Affine.build(node.value)
    if isinstance(node, ast.Name):
        if node.id not in environment:
            raise _unsupported(node, f"name {node.id!r} is outside its parameter or loop scope")
        return environment[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _affine(node.operand, environment, depth + 1)
        return value if isinstance(node.op, ast.UAdd) else value.scale(-1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _affine(node.left, environment, depth + 1)
        right = _affine(node.right, environment, depth + 1)
        if isinstance(node.op, ast.Add):
            return left.add(right)
        if isinstance(node.op, ast.Sub):
            return left.subtract(right)
        if not left.terms:
            return right.scale(left.constant)
        if not right.terms:
            return left.scale(right.constant)
        raise _unsupported(node, "multiplication requires an integer constant operand")
    raise _unsupported(node, "expected an integer affine expression")


def _comparison_cases(left: ast.AST, operator: ast.cmpop, right: ast.AST,
                      environment: dict[str, _Affine], negated: bool) -> tuple[tuple[str, ...], ...]:
    left_affine = _affine(left, environment)
    right_affine = _affine(right, environment)
    difference = left_affine.subtract(right_affine).text()
    operators: dict[type[ast.cmpop], str] = {
        ast.Eq: "=", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
        ast.Gt: ">", ast.GtE: ">=",
    }
    op = operators.get(type(operator))
    if op is None:
        raise _unsupported(operator, "only affine integer comparisons are supported")
    if negated:
        op = {"=": "!=", "!=": "=", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}[op]
    if op == "!=":
        return ((f"{difference} < 0",), (f"{difference} > 0",))
    return ((f"{difference} {op} 0",),)


def _dnf(node: ast.AST, environment: dict[str, _Affine], negated: bool = False,
         depth: int = 0) -> tuple[tuple[str, ...], ...]:
    if depth > MAX_EXPRESSION_DEPTH:
        raise _unsupported(node, "boolean expression exceeds the depth limit")
    if isinstance(node, ast.Constant) and type(node.value) is bool:
        return (((),) if node.value != negated else ())
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _dnf(node.operand, environment, not negated, depth + 1)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        is_and = isinstance(node.op, ast.And) != negated
        children = [_dnf(value, environment, negated, depth + 1) for value in node.values]
        if not is_and:
            joined = tuple(piece for child in children for piece in child)
            if len(joined) > MAX_BOOLEAN_PIECES:
                raise _unsupported(node, "boolean expression expands beyond the piece limit")
            return joined
        result: tuple[tuple[str, ...], ...] = ((),)
        for child in children:
            result = _dnf_product(result, child, node)
        return result
    if isinstance(node, ast.Compare):
        pairs = list(zip([node.left, *node.comparators[:-1]], node.ops, node.comparators))
        alternatives = [
            _comparison_cases(left, operator, right, environment, negated)
            for left, operator, right in pairs
        ]
        if negated:
            joined = tuple(piece for group in alternatives for piece in group)
            if len(joined) > MAX_BOOLEAN_PIECES:
                raise _unsupported(node, "boolean expression expands beyond the piece limit")
            return joined
        result: tuple[tuple[str, ...], ...] = ((),)
        for group in alternatives:
            result = _dnf_product(result, group, node)
        return result
    raise _unsupported(node, "if conditions must be boolean combinations of affine comparisons")


def _dnf_product(left: tuple[tuple[str, ...], ...], right: tuple[tuple[str, ...], ...],
                 node: ast.AST) -> tuple[tuple[str, ...], ...]:
    if len(left) * len(right) > MAX_BOOLEAN_PIECES:
        raise _unsupported(node, "boolean expression expands beyond the piece limit")
    return tuple(a + b for a in left for b in right)


def _range(node: ast.AST, environment: dict[str, _Affine],
           loop_symbol: str) -> tuple[_Affine, tuple[str, ...]]:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "range" or node.keywords:
        raise _unsupported(node, "for loops must iterate over the built-in range with positional arguments")
    if len(node.args) not in (1, 2, 3):
        raise _unsupported(node, "range must have one, two, or three arguments")
    values = [_affine(argument, environment) for argument in node.args]
    if len(values) == 1:
        start, stop = _Affine(), values[0]
        step_node = ast.Constant(value=1)
    else:
        start, stop = values[:2]
        step_node = node.args[2] if len(node.args) == 3 else ast.Constant(value=1)
    step = _integer_constant(step_node)
    if step == 0:
        raise _unsupported(step_node, "range step cannot be zero")
    index = start.add(_Affine.variable(loop_symbol).scale(step))
    bound = f"{index.text()} {'<' if step > 0 else '>'} {stop.text()}"
    return index, (f"{loop_symbol} >= 0", bound)


def _lower_generator(source: str, function_name: str) -> _Generator:
    if not isinstance(function_name, str) or not function_name.isidentifier() or keyword.iskeyword(function_name):
        raise _Unsupported("function must be a Python identifier")
    module = _parse_module(source)
    function = _find_function(module, function_name)
    _tree_size(function)
    parameters = _parameters(function)
    environment = {
        name: _Affine.variable(f"p{index}")
        for index, name in enumerate(parameters)
    }
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body.pop(0)

    coordinate_count: int | None = None
    pieces: list[_Piece] = []
    loop_names: set[str] = set()
    next_loop = 0

    def visit(statements: list[ast.stmt], paths: tuple[_Path, ...],
              scope: dict[str, _Affine], loop_depth: int) -> None:
        nonlocal coordinate_count, next_loop
        for statement in statements:
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.For):
                if statement.orelse or not isinstance(statement.target, ast.Name):
                    raise _unsupported(statement, "loops need one name target and cannot have an else clause")
                target = statement.target.id
                if target == "range" or target in parameters or target in loop_names:
                    raise _unsupported(statement.target, "loop target reuses a parameter, range, or loop name")
                if loop_depth >= MAX_LOOP_DEPTH:
                    raise _unsupported(statement, "loop nesting exceeds the depth limit")
                symbol = f"q{next_loop}"
                next_loop += 1
                loop_names.add(target)
                index, constraints = _range(statement.iter, scope, symbol)
                nested_scope = dict(scope)
                nested_scope[target] = index
                nested_paths = tuple(
                    _Path(path.variables + (symbol,), path.constraints + constraints)
                    for path in paths
                )
                if len(nested_paths) > MAX_SET_PIECES:
                    raise _unsupported(statement, "source expands beyond the set-piece limit")
                visit(statement.body, nested_paths, nested_scope, loop_depth + 1)
                continue
            if isinstance(statement, ast.If):
                true_dnf = _dnf(statement.test, scope)
                false_dnf = _dnf(statement.test, scope, negated=True)
                true_paths = _extend_paths(paths, true_dnf, statement)
                false_paths = _extend_paths(paths, false_dnf, statement)
                visit(statement.body, true_paths, scope, loop_depth)
                visit(statement.orelse, false_paths, scope, loop_depth)
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield):
                value = statement.value.value
                if not isinstance(value, ast.Tuple) or not value.elts:
                    raise _unsupported(statement, "each yield must contain a non-empty coordinate tuple")
                coordinates = tuple(_affine(item, scope) for item in value.elts)
                if len(coordinates) > MAX_COORDINATES:
                    raise _unsupported(statement, "yield tuple exceeds the coordinate limit")
                if coordinate_count is None:
                    coordinate_count = len(coordinates)
                elif coordinate_count != len(coordinates):
                    raise _unsupported(statement, "all yields must have one fixed coordinate count")
                pieces.extend(_Piece(path.variables, path.constraints, coordinates) for path in paths)
                if len(pieces) > MAX_SET_PIECES:
                    raise _unsupported(statement, "source expands beyond the set-piece limit")
                continue
            raise _unsupported(statement, f"unsupported statement {type(statement).__name__}")

    visit(body, (_Path(),), environment, 0)
    if coordinate_count is None:
        raise _unsupported(function, "function must contain a coordinate-tuple yield")
    return _Generator(parameters, coordinate_count, tuple(pieces))


def _extend_paths(paths: tuple[_Path, ...], disjunction: tuple[tuple[str, ...], ...],
                  node: ast.AST) -> tuple[_Path, ...]:
    if len(paths) * len(disjunction) > MAX_SET_PIECES:
        raise _unsupported(node, "source expands beyond the set-piece limit")
    return tuple(
        _Path(path.variables, path.constraints + constraints)
        for path in paths for constraints in disjunction
    )


def _compile_pair(old_source: str, function_name: str, replacement: str) -> tuple[_Generator, _Generator]:
    old = _lower_generator(old_source, function_name)
    new = _lower_generator(replacement, function_name)
    if old.parameters != new.parameters:
        raise _Unsupported("old and replacement functions must have identical ordered parameters")
    if old.coordinate_count != new.coordinate_count:
        raise _Unsupported("old and replacement yields must have identical tuple dimensions")
    return old, new


def _set_text(generator: _Generator, piece: _Piece) -> str:
    dimensions = [f"p{index}" for index in range(len(generator.parameters))]
    dimensions.extend(f"c{index}" for index in range(generator.coordinate_count))
    constraints = list(piece.constraints)
    constraints.extend(
        f"c{index} = {coordinate.text()}"
        for index, coordinate in enumerate(piece.coordinates)
    )
    body = " and ".join(constraints)
    if piece.variables:
        quantified = ", ".join(piece.variables)
        body = f"exists ({quantified}: {body})"
    return "{ [" + ", ".join(dimensions) + "] : " + body + " }"


def _witness(point: Any, isl: Any, generator: _Generator) -> dict[str, Any]:
    values = []
    for index in range(len(generator.parameters) + generator.coordinate_count):
        value = point.get_coordinate_val(isl.dim_type.set, index).to_python()
        if type(value) is not int:
            raise ArithmeticError("backend produced a non-integer witness")
        if abs(value).bit_length() > MAX_WITNESS_BITS:
            raise OverflowError("backend witness exceeds the output bit limit")
        values.append(value)
    parameter_values = dict(zip(generator.parameters, values[:len(generator.parameters)]))
    return {"parameters": parameter_values, "point": values[len(generator.parameters):]}


def _worker_compare(old_source: str, function_name: str, replacement: str) -> dict[str, Any]:
    try:
        old, new = _compile_pair(old_source, function_name, replacement)
    except _Unsupported as exc:
        return {"status": "UNSUPPORTED", "reason": str(exc), "backend_version": None}
    if sys.platform == "win32":
        return {
            "status": "UNAVAILABLE",
            "reason": "islpy has no supported native backend on Windows",
            "backend_version": None,
        }
    try:
        import importlib.metadata

        try:
            found_version = importlib.metadata.version(BACKEND)
        except importlib.metadata.PackageNotFoundError:
            found_version = None
        if found_version != BACKEND_VERSION:
            return {
                "status": "UNAVAILABLE",
                "reason": f"requires islpy {BACKEND_VERSION}; found {found_version or 'not installed'}",
                "backend_version": found_version,
            }
        import islpy as isl
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": f"islpy {BACKEND_VERSION} is not installed", "backend_version": None}

    try:
        context = isl.DEFAULT_CONTEXT

        def build(generator: _Generator) -> Any:
            dimensions = [f"p{index}" for index in range(len(generator.parameters))]
            dimensions.extend(f"c{index}" for index in range(generator.coordinate_count))
            if not generator.pieces:
                return isl.Set.read_from_str(context, "{ [" + ", ".join(dimensions) + "] : 1 = 0 }")
            result = isl.Set.read_from_str(context, _set_text(generator, generator.pieces[0]))
            for piece in generator.pieces[1:]:
                result = result.union(isl.Set.read_from_str(context, _set_text(generator, piece)))
            return result

        old_set = build(old)
        new_set = build(new)
        old_difference = old_set.subtract(new_set)
        new_difference = new_set.subtract(old_set)
        old_witness = None if old_difference.is_empty() else _witness(old_difference.sample_point(), isl, old)
        new_witness = None if new_difference.is_empty() else _witness(new_difference.sample_point(), isl, new)
        return {
            "status": "COUNTEREXAMPLE" if old_witness is not None or new_witness is not None else "EQUIVALENT",
            "old_only": old_witness,
            "new_only": new_witness,
            "reason": None,
            "backend_version": found_version,
        }
    except (OverflowError, ArithmeticError) as exc:
        return {"status": "UNKNOWN", "reason": str(exc), "backend_version": found_version}
    except Exception:
        return {"status": "UNKNOWN", "reason": "islpy could not complete the exact set comparison", "backend_version": found_version}


def _worker_main() -> int:
    try:
        request = sys.stdin.read(MAX_REQUEST_CHARS + 1)
        if len(request) > MAX_REQUEST_CHARS:
            response = {"status": "UNSUPPORTED", "reason": "worker request exceeds the source limit", "backend_version": None}
        else:
            payload = json.loads(request)
            if (not isinstance(payload, dict)
                    or not all(isinstance(payload.get(key), str)
                               for key in ("old_source", "function", "replacement"))):
                response = {"status": "UNKNOWN", "reason": "worker request has an invalid shape", "backend_version": None}
            else:
                response = _worker_compare(payload["old_source"], payload["function"], payload["replacement"])
    except Exception:
        response = {"status": "UNKNOWN", "reason": "isolated worker could not read the request", "backend_version": None}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 0


def _unavailable(reason: str, status: Status = "UNAVAILABLE") -> dict[str, Any]:
    return {
        "status": status,
        "old_only": None,
        "new_only": None,
        "reason": reason,
        "backend": BACKEND,
        "backend_version": None,
        "model_scope": MODEL_SCOPE,
    }


def compare_iteration_sets(root: Path, path: str, function: str, replacement: str,
                           timeout_ms: int = 2_000) -> dict[str, Any]:
    """Compare a repository generator with replacement over all integer parameters."""
    if not isinstance(replacement, str):
        raise ValueError("replacement must be a string")
    try:
        replacement_bytes = len(replacement.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("replacement must contain Unicode scalar values") from exc
    if replacement_bytes > MAX_SOURCE_BYTES:
        raise ValueError(f"replacement must be at most {MAX_SOURCE_BYTES} bytes")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be an integer from 1 to {MAX_TIMEOUT_MS}")
    if not isinstance(function, str) or len(function) > 256 or not function.isidentifier() or keyword.iskeyword(function):
        raise ValueError("function must be a Python identifier of at most 256 characters")

    from . import _source

    old_source = _source(Path(root), path)
    old_digest = hashlib.sha256(old_source.encode("utf-8")).hexdigest()
    replacement_digest = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
    request = json.dumps(
        {"old_source": old_source, "function": function, "replacement": replacement},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
            input=request,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_ms / 1_000,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result = _unavailable(f"computation exceeded {timeout_ms} ms", "UNKNOWN")
    except OSError:
        result = _unavailable("cannot start the isolated islpy worker")
    else:
        try:
            payload = json.loads(completed.stdout) if completed.returncode == 0 else None
        except (json.JSONDecodeError, TypeError):
            payload = None
        status = payload.get("status") if isinstance(payload, dict) else None
        old_only = payload.get("old_only") if isinstance(payload, dict) else None
        new_only = payload.get("new_only") if isinstance(payload, dict) else None
        version = payload.get("backend_version") if isinstance(payload, dict) else None
        def valid_witness(value: object) -> bool:
            return (
                isinstance(value, dict)
                and isinstance(value.get("parameters"), dict)
                and all(type(item) is int for item in value["parameters"].values())
                and isinstance(value.get("point"), list)
                and all(type(item) is int for item in value["point"])
            )

        positive_result_invalid = (
            status in {"EQUIVALENT", "COUNTEREXAMPLE"}
            and (
                version != BACKEND_VERSION
                or (status == "EQUIVALENT" and (old_only is not None or new_only is not None))
                or (status == "COUNTEREXAMPLE"
                    and not (valid_witness(old_only) or valid_witness(new_only)))
            )
        )
        if status not in {"EQUIVALENT", "COUNTEREXAMPLE", "UNSUPPORTED", "UNAVAILABLE", "UNKNOWN"} or positive_result_invalid:
            result = _unavailable("isolated islpy worker exited without a result")
        else:
            result = {
                "status": status,
                "old_only": old_only,
                "new_only": new_only,
                "reason": payload.get("reason"),
                "backend": BACKEND,
                "backend_version": version,
                "model_scope": MODEL_SCOPE,
            }
    return {
        "operation": "compare_iteration_sets",
        "path": path,
        "function": function,
        "old_source_sha256": old_digest,
        "replacement_sha256": replacement_digest,
        **result,
    }


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker_main())

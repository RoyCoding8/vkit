"""Parse the supported pure Python subset into a shared immutable IR."""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from typing import Any, Literal

Sort = Literal["int", "bool"]

MODEL_SCOPE = (
    "Mathematical integers and booleans. Parameters range over their annotated sorts. "
    "Annotations define the modeled domain; caller values are not checked."
)


@dataclass(frozen=True)
class Term:
    """One typed expression node shared by proof and simplification."""

    op: str
    args: tuple[Term, ...] = ()
    value: int | bool | str | None = None
    sort: Sort = "int"


@dataclass(frozen=True)
class Parameter:
    name: str
    sort: Sort


@dataclass(frozen=True)
class FunctionModel:
    name: str
    parameters: tuple[Parameter, ...]
    return_sort: Sort
    body: Term
    source_sha256: str


@dataclass(frozen=True)
class ResourceLimits:
    timeout_ms: int = 2_000
    max_source_bytes: int = 100_000
    max_ast_nodes: int = 10_000

    def __post_init__(self) -> None:
        bounds = ((self.timeout_ms, 1, 60_000),
                  (self.max_source_bytes, 1, 1_000_000),
                  (self.max_ast_nodes, 1, 100_000))
        if any(type(value) is not int or not low <= value <= high for value, low, high in bounds):
            raise ValueError("resource limits are outside the supported bounds")


@dataclass(frozen=True)
class ParseFailure:
    reason: str
    status: Literal["UNSUPPORTED", "UNAVAILABLE"] = "UNSUPPORTED"

    def to_json(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


class UnsupportedSource(ValueError):
    pass


def _annotation(node: ast.expr | None) -> Sort:
    if isinstance(node, ast.Name) and node.id in ("int", "bool"):
        return node.id  # type: ignore[return-value]
    raise UnsupportedSource("parameters and return values need explicit int or bool annotations")


def _term(op: str, args: tuple[Term, ...], sort: Sort, value: int | bool | str | None = None) -> Term:
    return Term(op, args, value, sort)


def _parse_expr(node: ast.expr, names: dict[str, Sort]) -> Term:
    if isinstance(node, ast.Constant):
        if type(node.value) is int:
            return _term("int_const", (), "int", node.value)
        if type(node.value) is bool:
            return _term("bool_const", (), "bool", node.value)
        raise UnsupportedSource("only integer and boolean literals are supported")
    if isinstance(node, ast.Name):
        sort = names.get(node.id)
        if sort is None:
            raise UnsupportedSource(f"name {node.id!r} is not a parameter or local assignment")
        return _term("var", (), sort, node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _parse_expr(node.left, names)
        right = _parse_expr(node.right, names)
        if left.sort != "int" or right.sort != "int":
            raise UnsupportedSource("arithmetic operators require int operands")
        op = "add" if isinstance(node.op, ast.Add) else "sub" if isinstance(node.op, ast.Sub) else "mul"
        return _term(op, (left, right), "int")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        operand = _parse_expr(node.operand, names)
        if isinstance(node.op, ast.Not):
            if operand.sort != "bool":
                raise UnsupportedSource("not requires a bool operand")
            return _term("not", (operand,), "bool")
        if operand.sort != "int":
            raise UnsupportedSource("unary arithmetic requires an int operand")
        return operand if isinstance(node.op, ast.UAdd) else _term("sub", (_term("int_const", (), "int", 0), operand), "int")
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = tuple(_parse_expr(value, names) for value in node.values)
        if any(value.sort != "bool" for value in values):
            raise UnsupportedSource("and/or are supported only when every operand is bool")
        return _term("and" if isinstance(node.op, ast.And) else "or", values, "bool")
    if isinstance(node, ast.Compare):
        left = _parse_expr(node.left, names)
        comparisons: list[Term] = []
        operators: dict[type[ast.cmpop], str] = {
            ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le",
            ast.Gt: "gt", ast.GtE: "ge",
        }
        for operator, comparator in zip(node.ops, node.comparators):
            right = _parse_expr(comparator, names)
            if left.sort != right.sort:
                raise UnsupportedSource("comparison operands need the same sort")
            op = operators.get(type(operator))
            if op is None:
                raise UnsupportedSource("comparison operator is unsupported")
            if op in ("lt", "le", "gt", "ge") and left.sort != "int":
                raise UnsupportedSource("ordering comparisons require int operands")
            comparisons.append(_term(op, (left, right), "bool"))
            left = right
        return comparisons[0] if len(comparisons) == 1 else _term("and", tuple(comparisons), "bool")
    if isinstance(node, ast.IfExp):
        test = _parse_expr(node.test, names)
        yes = _parse_expr(node.body, names)
        no = _parse_expr(node.orelse, names)
        if test.sort != "bool" or yes.sort != no.sort:
            raise UnsupportedSource("conditional expressions need a bool test and matching result sorts")
        return _term("ite", (test, yes, no), yes.sort)
    raise UnsupportedSource(f"expression {type(node).__name__} is unsupported")


def _substitute(term: Term, name: str, value: Term, limit: int) -> Term:
    value_size = _term_size(value, limit)
    produced = 0

    def visit(current: Term) -> Term:
        nonlocal produced
        if current.op == "var" and current.value == name:
            if current.sort != value.sort:
                raise UnsupportedSource(f"assignment to {name!r} changes its sort")
            produced += value_size
            if produced > limit:
                raise UnsupportedSource("lowered expression exceeds the AST node limit")
            return value
        produced += 1
        if produced > limit:
            raise UnsupportedSource("lowered expression exceeds the AST node limit")
        if not current.args:
            return current
        args = tuple(visit(arg) for arg in current.args)
        return current if args == current.args else _term(current.op, args, current.sort, current.value)

    return visit(term)


def _term_size(term: Term, stop_after: int) -> int:
    count = 0
    stack = [term]
    while stack and count <= stop_after:
        current = stack.pop()
        count += 1
        stack.extend(current.args)
    return count


def _lower_block(statements: list[ast.stmt], continuation: Term | None,
                 names: dict[str, Sort], limits: ResourceLimits) -> Term | None:
    result = continuation
    for statement in reversed(statements):
        if isinstance(statement, ast.Return):
            if statement.value is None:
                raise UnsupportedSource("bare return is unsupported")
            result = _parse_expr(statement.value, names)
        elif isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise UnsupportedSource("assignments must target one local name")
            target = statement.targets[0].id
            if target not in names:
                raise UnsupportedSource(f"assignment target {target!r} has no supported sort")
            value = _parse_expr(statement.value, names)
            if value.sort != names[target]:
                raise UnsupportedSource(f"assignment to {target!r} changes its sort")
            if result is not None:
                result = _substitute(result, target, value, limits.max_ast_nodes)
        elif isinstance(statement, ast.If):
            test = _parse_expr(statement.test, names)
            if test.sort != "bool":
                raise UnsupportedSource("if conditions must be bool")
            yes = _lower_block(statement.body, result, names, limits)
            no = _lower_block(statement.orelse, result, names, limits)
            if yes is None or no is None:
                result = None
            elif yes.sort != no.sort:
                raise UnsupportedSource("conditional branches return different sorts")
            else:
                result = _term("ite", (test, yes, no), yes.sort)
        else:
            raise UnsupportedSource(f"statement {type(statement).__name__} is unsupported")
        if result is not None and _term_size(result, limits.max_ast_nodes) > limits.max_ast_nodes:
            raise UnsupportedSource("lowered expression exceeds the AST node limit")
    return result


def _infer_expr_sort(node: ast.expr, names: dict[str, Sort]) -> Sort:
    if isinstance(node, ast.Constant) and type(node.value) in (int, bool):
        return "int" if type(node.value) is int else "bool"
    if isinstance(node, ast.Name):
        sort = names.get(node.id)
        if sort is not None:
            return sort
        raise UnsupportedSource(f"sort for local {node.id!r} is not known yet")
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left, right = _infer_expr_sort(node.left, names), _infer_expr_sort(node.right, names)
        if left == right == "int":
            return "int"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        if _infer_expr_sort(node.operand, names) == "int":
            return "int"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        if _infer_expr_sort(node.operand, names) == "bool":
            return "bool"
    if isinstance(node, ast.Compare):
        return "bool"
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        if all(_infer_expr_sort(value, names) == "bool" for value in node.values):
            return "bool"
    if isinstance(node, ast.IfExp):
        yes, no = _infer_expr_sort(node.body, names), _infer_expr_sort(node.orelse, names)
        if yes == no and _infer_expr_sort(node.test, names) == "bool":
            return yes
    raise UnsupportedSource("cannot infer local assignment sort from the supported subset")


def _variables(term: Term) -> set[str]:
    result = set()
    stack = [term]
    while stack:
        current = stack.pop()
        if current.op == "var" and isinstance(current.value, str):
            result.add(current.value)
        stack.extend(current.args)
    return result


def parse_function(source: str, function: str, limits: ResourceLimits | None = None) -> FunctionModel | ParseFailure:
    """Parse one top-level function without importing or executing its source."""
    limits = limits or ResourceLimits()
    try:
        if not isinstance(source, str) or len(source.encode("utf-8")) > limits.max_source_bytes:
            raise UnsupportedSource("source exceeds the configured byte limit")
        tree = ast.parse(source)
        if sum(1 for _ in ast.walk(tree)) > limits.max_ast_nodes:
            raise UnsupportedSource("source exceeds the configured AST node limit")
        matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == function]
        if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef):
            raise UnsupportedSource(f"top-level synchronous function {function!r} was not found")
        node = matches[0]
        if node.decorator_list or getattr(node, "type_params", ()):
            raise UnsupportedSource("decorators and generic functions are unsupported")
        args = node.args
        if (args.posonlyargs or args.vararg or args.kwonlyargs or args.kwarg or args.defaults
                or any(value is not None for value in args.kw_defaults)):
            raise UnsupportedSource("defaults and non-positional parameters are unsupported")
        parameters = tuple(Parameter(arg.arg, _annotation(arg.annotation)) for arg in args.args)
        return_sort = _annotation(node.returns)
        names: dict[str, Sort] = {parameter.name: parameter.sort for parameter in parameters}
        assignments: list[ast.Assign] = []
        for statement in ast.walk(node):
            if isinstance(statement, ast.AnnAssign):
                raise UnsupportedSource("annotated assignments are unsupported")
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    raise UnsupportedSource("assignments must target one local name")
                target = statement.targets[0].id
                if target in names:
                    raise UnsupportedSource("parameter reassignment is unsupported")
                assignments.append(statement)
        for _ in range(len(assignments) + 1):
            changed = False
            for statement in assignments:
                target = statement.targets[0].id
                try:
                    inferred = _infer_expr_sort(statement.value, names)
                except UnsupportedSource:
                    continue
                previous = names.get(target)
                if previous is not None and previous != inferred:
                    raise UnsupportedSource(f"assignment to {target!r} changes its sort")
                if previous is None:
                    names[target] = inferred
                    changed = True
            if not changed:
                break
        if any(statement.targets[0].id not in names for statement in assignments):
            raise UnsupportedSource("cannot infer a local variable sort")
        body = _lower_block(node.body, None, names, limits)
        if body is None:
            raise UnsupportedSource("every control-flow path must return a value")
        if body.sort != return_sort:
            raise UnsupportedSource("function body does not match its annotated return sort")
        parameter_names = {parameter.name for parameter in parameters}
        if any(name not in parameter_names for name in _variables(body)):
            raise UnsupportedSource("a local is not assigned on every path where it is read")
        return FunctionModel(function, parameters, return_sort, body,
                             hashlib.sha256(source.encode("utf-8")).hexdigest())
    except (SyntaxError, UnicodeError, UnsupportedSource, RecursionError, ValueError) as exc:
        return ParseFailure(str(exc) or type(exc).__name__)

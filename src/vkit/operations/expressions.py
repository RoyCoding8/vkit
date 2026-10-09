"""Parse the supported pure Python subset into a shared immutable IR."""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from typing import Literal

Sort = Literal["int", "bool"]

MODEL_SCOPE = (
    "Mathematical integers and booleans. Parameters range over their annotated sorts. "
    "Annotations define the modeled domain; caller values are not checked. The selected "
    "top-level function AST is modeled; module-level statements are ignored and not executed."
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


@dataclass
class _Path:
    guard: Term
    environment: dict[str, Term]
    parameters: frozenset[str]
    result: Term | None = None


class _Budget:
    def __init__(self, limit: int, initial: int) -> None:
        self.limit = limit
        self.used = initial
        if initial > limit:
            raise UnsupportedSource("source exceeds the configured AST node limit")

    def charge(self, amount: int = 1) -> None:
        self.used += amount
        if self.used > self.limit:
            raise UnsupportedSource("frontend expansion exceeds the AST node limit")


class UnsupportedSource(ValueError):
    pass


class _TopLevelBindings(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(alias.asname or alias.name for alias in node.names)


def _annotation(node: ast.expr | None) -> Sort:
    if isinstance(node, ast.Name):
        if node.id == "int":
            return "int"
        if node.id == "bool":
            return "bool"
    raise UnsupportedSource("parameters and return values need explicit int or bool annotations")


def _term(op: str, args: tuple[Term, ...], sort: Sort, budget: _Budget,
          value: int | bool | str | None = None) -> Term:
    budget.charge()
    return Term(op, args, value, sort)


_EXPRESSION_NODES = (ast.Constant, ast.Name, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp)
_OPERATOR_NODES = (
    ast.Load, ast.Add, ast.Sub, ast.Mult, ast.USub, ast.UAdd, ast.Not,
    ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def _validate_expr_shape(expression: ast.expr) -> None:
    for node in ast.walk(expression):
        if not isinstance(node, _EXPRESSION_NODES + _OPERATOR_NODES):
            raise UnsupportedSource(f"expression {type(node).__name__} is unsupported")
        if isinstance(node, ast.Constant) and type(node.value) not in (int, bool):
            raise UnsupportedSource("only integer and boolean literals are supported")


def _validate_block_shape(statements: list[ast.stmt]) -> None:
    for statement in statements:
        if isinstance(statement, ast.Return):
            if statement.value is None:
                raise UnsupportedSource("bare return is unsupported")
            _validate_expr_shape(statement.value)
        elif isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise UnsupportedSource("assignments must target one local name")
            _validate_expr_shape(statement.value)
        elif isinstance(statement, ast.If):
            _validate_expr_shape(statement.test)
            _validate_block_shape(statement.body)
            _validate_block_shape(statement.orelse)
        else:
            raise UnsupportedSource(f"statement {type(statement).__name__} is unsupported")

def _parse_expr(node: ast.expr, environment: dict[str, Term], budget: _Budget) -> Term:
    budget.charge()
    if isinstance(node, ast.Constant):
        if type(node.value) is int:
            return _term("int_const", (), "int", budget, node.value)
        if type(node.value) is bool:
            return _term("bool_const", (), "bool", budget, node.value)
        raise UnsupportedSource("only integer and boolean literals are supported")
    if isinstance(node, ast.Name):
        value = environment.get(node.id)
        if value is None:
            raise UnsupportedSource(f"name {node.id!r} is read before assignment")
        return value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _parse_expr(node.left, environment, budget)
        right = _parse_expr(node.right, environment, budget)
        if left.sort != "int" or right.sort != "int":
            raise UnsupportedSource("arithmetic operators require int operands")
        op = "add" if isinstance(node.op, ast.Add) else "sub" if isinstance(node.op, ast.Sub) else "mul"
        return _term(op, (left, right), "int", budget)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        operand = _parse_expr(node.operand, environment, budget)
        if isinstance(node.op, ast.Not):
            if operand.sort != "bool":
                raise UnsupportedSource("not requires a bool operand")
            return _term("not", (operand,), "bool", budget)
        if operand.sort != "int":
            raise UnsupportedSource("unary arithmetic requires an int operand")
        return operand if isinstance(node.op, ast.UAdd) else _term(
            "sub", (_term("int_const", (), "int", budget, 0), operand), "int", budget)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = tuple(_parse_expr(value, environment, budget) for value in node.values)
        if any(value.sort != "bool" for value in values):
            raise UnsupportedSource("and/or are supported only when every operand is bool")
        return _term("and" if isinstance(node.op, ast.And) else "or", values, "bool", budget)
    if isinstance(node, ast.Compare):
        left = _parse_expr(node.left, environment, budget)
        comparisons: list[Term] = []
        operators: dict[type[ast.cmpop], str] = {
            ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le",
            ast.Gt: "gt", ast.GtE: "ge",
        }
        for operator, comparator in zip(node.ops, node.comparators):
            right = _parse_expr(comparator, environment, budget)
            if left.sort != right.sort:
                raise UnsupportedSource("comparison operands need the same sort")
            op = operators.get(type(operator))
            if op is None:
                raise UnsupportedSource("comparison operator is unsupported")
            if op in ("lt", "le", "gt", "ge") and left.sort != "int":
                raise UnsupportedSource("ordering comparisons require int operands")
            comparisons.append(_term(op, (left, right), "bool", budget))
            left = right
        return comparisons[0] if len(comparisons) == 1 else _term("and", tuple(comparisons), "bool", budget)
    if isinstance(node, ast.IfExp):
        test = _parse_expr(node.test, environment, budget)
        yes = _parse_expr(node.body, environment, budget)
        no = _parse_expr(node.orelse, environment, budget)
        if test.sort != "bool" or yes.sort != no.sort:
            raise UnsupportedSource("conditional expressions need a bool test and matching result sorts")
        return _term("ite", (test, yes, no), yes.sort, budget)
    raise UnsupportedSource(f"expression {type(node).__name__} is unsupported")


def _term_size(term: Term, stop_after: int) -> int:
    count = 0
    stack = [term]
    while stack and count <= stop_after:
        current = stack.pop()
        count += 1
        stack.extend(current.args)
    return count


def _path_guard(path: _Path, condition: Term, positive: bool, budget: _Budget) -> Term:
    test = condition if positive else _term("not", (condition,), "bool", budget)
    if path.guard.op == "bool_const" and path.guard.value is True:
        return test
    return _term("and", (path.guard, test), "bool", budget)


def _run_block(statements: list[ast.stmt], paths: list[_Path], budget: _Budget) -> list[_Path]:
    for statement in statements:
        budget.charge(len(paths))
        active = [path for path in paths if path.result is None]
        if not active:
            break
        if isinstance(statement, ast.Return):
            if statement.value is None:
                raise UnsupportedSource("bare return is unsupported")
            for path in active:
                path.result = _parse_expr(statement.value, path.environment, budget)
        elif isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise UnsupportedSource("assignments must target one local name")
            target = statement.targets[0].id
            if any(target in path.parameters for path in active):
                raise UnsupportedSource("parameter reassignment is unsupported")
            for path in active:
                path.environment[target] = _parse_expr(statement.value, path.environment, budget)
        elif isinstance(statement, ast.If):
            completed = [path for path in paths if path.result is not None]
            yes_paths: list[_Path] = []
            no_paths: list[_Path] = []
            for path in active:
                test = _parse_expr(statement.test, path.environment, budget)
                if test.sort != "bool":
                    raise UnsupportedSource("if conditions must be bool")
                budget.charge(2 * len(path.environment) + 2)
                yes_guard = _path_guard(path, test, True, budget)
                no_guard = _path_guard(path, test, False, budget)
                yes_environment = path.environment.copy()
                no_environment = path.environment.copy()
                yes_paths.append(_Path(yes_guard, yes_environment, path.parameters))
                no_paths.append(_Path(no_guard, no_environment, path.parameters))
            yes_paths = _run_block(statement.body, yes_paths, budget)
            no_paths = _run_block(statement.orelse, no_paths, budget)
            paths = completed + yes_paths + no_paths
            if len(paths) > budget.limit:
                raise UnsupportedSource("control-flow path expansion exceeds the AST node limit")
        else:
            raise UnsupportedSource(f"statement {type(statement).__name__} is unsupported")
    return paths


def _module_rebinds(tree: ast.Module, selected: ast.FunctionDef, name: str) -> bool:
    bindings = _TopLevelBindings()
    for statement in tree.body:
        if statement is not selected:
            bindings.visit(statement)
    return name in bindings.names


def parse_function(source: str, function: str, limits: ResourceLimits | None = None) -> FunctionModel | ParseFailure:
    """Parse one top-level function without importing or executing its source."""
    limits = limits or ResourceLimits()
    try:
        if not isinstance(source, str) or len(source.encode("utf-8")) > limits.max_source_bytes:
            raise UnsupportedSource("source exceeds the configured byte limit")
        tree = ast.parse(source)
        node_count = sum(1 for _ in ast.walk(tree))
        budget = _Budget(limits.max_ast_nodes, node_count)
        compile(tree, "<vkit-source>", "exec", dont_inherit=True)
        matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == function]
        if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef):
            raise UnsupportedSource(f"top-level synchronous function {function!r} was not found exactly once")
        node = matches[0]
        if _module_rebinds(tree, node, function):
            raise UnsupportedSource("module-level code rebinds the selected function name")
        if node.decorator_list or getattr(node, "type_params", ()):
            raise UnsupportedSource("decorators and generic functions are unsupported")
        args = node.args
        if (args.posonlyargs or args.vararg or args.kwonlyargs or args.kwarg or args.defaults
                or any(value is not None for value in args.kw_defaults)):
            raise UnsupportedSource("defaults and non-positional parameters are unsupported")
        parameters = tuple(Parameter(arg.arg, _annotation(arg.annotation)) for arg in args.args)
        return_sort = _annotation(node.returns)
        _validate_block_shape(node.body)
        environment = {
            parameter.name: _term("var", (), parameter.sort, budget, parameter.name)
            for parameter in parameters
        }
        parameter_names = frozenset(parameter.name for parameter in parameters)
        initial_guard = _term("bool_const", (), "bool", budget, True)
        initial_path = _Path(initial_guard, environment, parameter_names)
        paths = _run_block(node.body, [initial_path], budget)
        if any(path.result is None for path in paths):
            raise UnsupportedSource("every control-flow path must return a value")
        results = [path.result for path in paths]
        if any(result.sort != return_sort for result in results if result is not None):
            raise UnsupportedSource("function body does not match its annotated return sort")
        body = results[-1]
        assert body is not None
        for path in reversed(paths[:-1]):
            assert path.result is not None
            if path.result.sort != body.sort:
                raise UnsupportedSource("conditional branches return different sorts")
            body = _term("ite", (path.guard, path.result, body), body.sort, budget)
        if _term_size(body, limits.max_ast_nodes) > limits.max_ast_nodes:
            raise UnsupportedSource("lowered expression exceeds the AST node limit")
        return FunctionModel(function, parameters, return_sort, body,
                             hashlib.sha256(source.encode("utf-8")).hexdigest())
    except (SyntaxError, UnicodeError, UnsupportedSource, RecursionError, ValueError) as exc:
        return ParseFailure(str(exc) or type(exc).__name__)

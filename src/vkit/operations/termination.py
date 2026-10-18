"""Synthesize and independently check linear rankings for integer loop models."""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _source
from ._data import encode_json
from .expressions import Term, UnsupportedSource, _Budget, _module_rebinds, _parse_expr, _term_size
from .rewrites import _evaluate, _to_solver

MAX_OUTPUT = 80_000
MAX_REQUEST = 500_000
MAX_CANDIDATES = 48
COEFFICIENT_LIMIT = 16
OFFSET_LIMIT = 2**32
MODEL_SCOPE = (
    "One captured Python while loop over mathematical integer parameters with total piecewise-affine "
    "updates. TERMINATES proves all guarded transitions admit the returned nonnegative strictly "
    "decreasing integer ranking. NONTERMINATING exhibits one replayed guarded fixed point. "
    "No claim about machine overflow, resource exhaustion, effects or application termination."
)


@dataclass(frozen=True)
class _Loop:
    variables: tuple[str, ...]
    guard: Term
    updates: tuple[Term, ...]


def _depends(term: Term) -> bool:
    return term.op == "var" or any(_depends(arg) for arg in term.args)


def _linear(term: Term) -> None:
    if term.op == "mul" and all(_depends(arg) for arg in term.args):
        raise UnsupportedSource("variable products are outside the affine loop profile")
    if term.op == "int_const" and int(term.value).bit_length() > 256:
        raise UnsupportedSource("integer constants exceed 256 bits")
    for arg in term.args:
        _linear(arg)


def _parse(source: str, function: str) -> _Loop:
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    budget = _Budget(1_000, len(nodes))
    stack = [(tree, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > 64:
            raise UnsupportedSource("source AST exceeds depth 64")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(current))
    compile(tree, "<vkit-loop>", "exec", dont_inherit=True)
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function]
    if len(matches) != 1:
        raise UnsupportedSource("selected synchronous function must occur exactly once")
    node = matches[0]
    args = node.args
    if (node.decorator_list or getattr(node, "type_params", ()) or _module_rebinds(tree, node, function)
            or args.posonlyargs or args.vararg or args.kwonlyargs or args.kwarg or args.defaults):
        raise UnsupportedSource("decorators, rebinding, defaults and non-positional parameters are unsupported")
    if not 1 <= len(args.args) <= 4 or any(
            not isinstance(arg.annotation, ast.Name) or arg.annotation.id != "int" for arg in args.args):
        raise UnsupportedSource("one to four explicitly annotated integer parameters are required")
    if not isinstance(node.returns, ast.Name) or node.returns.id != "int":
        raise UnsupportedSource("an integer return annotation is required")
    body = node.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if (len(body) != 2 or not isinstance(body[0], ast.While) or body[0].orelse
            or not isinstance(body[1], ast.Return) or body[1].value is None):
        raise UnsupportedSource("function must contain one while loop followed by one integer return")
    names = tuple(arg.arg for arg in args.args)
    environment = {name: Term("var", value=name) for name in names}
    guard = _parse_expr(body[0].test, environment, budget)
    if guard.sort != "bool":
        raise UnsupportedSource("loop guard must be boolean")
    for statement in body[0].body:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AugAssign) and isinstance(statement.op, (ast.Add, ast.Sub, ast.Mult)):
            target = statement.target
            value = ast.BinOp(left=ast.Name(id=getattr(target, "id", ""), ctx=ast.Load()),
                              op=statement.op, right=statement.value)
        else:
            raise UnsupportedSource("loop body supports only straight assignments and pass")
        if not isinstance(target, ast.Name) or target.id not in environment:
            raise UnsupportedSource("assignments must update existing integer parameters")
        result = _parse_expr(value, environment, budget)
        if result.sort != "int" or _term_size(result, 1_000) > 1_000:
            raise UnsupportedSource("update is not a bounded integer expression")
        _linear(result)
        environment[target.id] = result
    returned = _parse_expr(body[1].value, environment, budget)
    if returned.sort != "int":
        raise UnsupportedSource("return must be a total integer expression")
    _linear(guard)
    _linear(returned)
    return _Loop(names, guard, tuple(environment[name] for name in names))


def _check(source: str, function: str, timeout_ms: int) -> dict[str, Any]:
    try:
        loop = _parse(source, function)
    except (ValueError, SyntaxError, RecursionError) as exc:
        return {"status": "UNSUPPORTED", "reason": str(exc)}
    try:
        import cvc5
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": "cvc5 1.4.2 is required"}
    if getattr(cvc5, "__version__", None) != "1.4.2":
        return {"status": "UNAVAILABLE", "reason": "cvc5 1.4.2 is required"}
    deadline = time.monotonic() + timeout_ms / 1_000
    kind = cvc5.Kind

    def solver():
        result = cvc5.Solver()
        result.setLogic("QF_LIA")
        result.setOption("produce-models", "true")
        result.setOption("incremental", "true")
        return result

    def query(engine):
        remaining = int((deadline - time.monotonic()) * 1_000)
        if remaining <= 0:
            raise TimeoutError("ranking search exceeded its deadline")
        engine.setOption("tlimit-per", str(remaining))
        return engine.checkSat()

    def add(engine, terms):
        return terms[0] if len(terms) == 1 else engine.mkTerm(kind.ADD, *terms)

    check = solver()
    variables = {name: check.mkConst(check.getIntegerSort(), f"s{index}")
                 for index, name in enumerate(loop.variables)}
    updates = [_to_solver(term, variables, check, cvc5) for term in loop.updates]
    check.assertFormula(_to_solver(loop.guard, variables, check, cvc5))
    check.push()
    for name, update in zip(loop.variables, updates):
        check.assertFormula(check.mkTerm(kind.EQUAL, variables[name], update))
    answer = query(check)
    if answer.isSat():
        state = {name: int(check.getValue(value).getIntegerValue()) for name, value in variables.items()}
        replay = {name: _evaluate(term, state) for name, term in zip(loop.variables, loop.updates)}
        if not _evaluate(loop.guard, state) or replay != state:
            return {"status": "UNKNOWN", "reason": "fixed point failed independent IR replay"}
        return {"status": "NONTERMINATING", "state": state, "next_state": replay, "replayed": True}
    check.pop()
    propose = solver()
    coefficients = [propose.mkConst(propose.getIntegerSort(), f"c{index}")
                    for index in range(len(loop.variables) + 1)]
    for index, coefficient in enumerate(coefficients):
        limit = OFFSET_LIMIT if index == 0 else COEFFICIENT_LIMIT
        propose.assertFormula(propose.mkTerm(kind.GEQ, coefficient, propose.mkInteger(str(-limit))))
        propose.assertFormula(propose.mkTerm(kind.LEQ, coefficient, propose.mkInteger(str(limit))))

    def ranking(engine, coeffs, vals):
        return add(engine, [coeffs[0], *[engine.mkTerm(kind.MULT, coefficient, value)
                                       for coefficient, value in zip(coeffs[1:], vals)]])

    for iteration in range(MAX_CANDIDATES):
        answer = query(propose)
        if not answer.isSat():
            return {"status": "UNKNOWN", "reason": "bounded linear ranking synthesis found no candidate"}
        candidate = [int(propose.getValue(term).getIntegerValue()) for term in coefficients]
        literals = [check.mkInteger(str(value)) for value in candidate]
        before = ranking(check, literals, list(variables.values()))
        after = ranking(check, literals, updates)
        check.push()
        check.assertFormula(check.mkTerm(kind.OR, check.mkTerm(kind.LT, before, check.mkInteger(0)),
                                        check.mkTerm(kind.GEQ, after, before)))
        answer = query(check)
        if answer.isUnsat():
            return {"status": "TERMINATES", "ranking": {"offset": candidate[0],
                    "coefficients": dict(zip(loop.variables, candidate[1:]))},
                    "iteration_bound": "0 if initial guard is false, otherwise ranking(initial_state) + 1",
                    "candidates_checked": iteration + 1, "backend": {"name": "cvc5", "version": "1.4.2"}}
        if not answer.isSat():
            return {"status": "UNKNOWN", "reason": str(answer)}
        state = {name: int(check.getValue(value).getIntegerValue()) for name, value in variables.items()}
        replay = [_evaluate(term, state) for term in loop.updates]
        current_rank = candidate[0] + sum(c * state[name] for c, name in zip(candidate[1:], loop.variables))
        next_rank = candidate[0] + sum(c * value for c, value in zip(candidate[1:], replay))
        if not _evaluate(loop.guard, state) or (current_rank >= 0 and next_rank < current_rank):
            return {"status": "UNKNOWN", "reason": "ranking witness failed independent IR replay"}
        check.pop()
        current = ranking(propose, coefficients, [propose.mkInteger(str(state[name])) for name in loop.variables])
        following = ranking(propose, coefficients, [propose.mkInteger(str(value)) for value in replay])
        propose.assertFormula(propose.mkTerm(kind.GEQ, current, propose.mkInteger(0)))
        propose.assertFormula(propose.mkTerm(kind.LT, following, current))
    return {"status": "UNKNOWN", "reason": "linear ranking candidate limit reached"}


def _worker() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("loop request exceeds byte limit")
        request = json.loads(raw)
        if (not isinstance(request, dict) or set(request) != {"source", "function", "timeout_ms"}
                or not isinstance(request["source"], str) or len(request["source"].encode("utf-8")) > 65_536
                or not isinstance(request["function"], str) or not request["function"].isidentifier()
                or type(request["timeout_ms"]) is not int or not 1 <= request["timeout_ms"] <= 60_000):
            raise ValueError("invalid loop request")
        result = _check(**request)
        print(encode_json(result, max_bytes=MAX_OUTPUT))
    except Exception as exc:
        print(encode_json({"status": "UNKNOWN", "reason": f"loop analysis did not complete: {type(exc).__name__}"},
                          max_bytes=MAX_OUTPUT))


def analyze_loop_termination(root: Path, path: str, function: str, timeout_seconds: int = 10) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be from 1 to 60")
    evidence = {"operation": "analyze_loop_termination", "path": path, "function": function,
                "timeout_seconds": timeout_seconds, "model_scope": MODEL_SCOPE}
    try:
        source = _source(root, path)
        if not isinstance(function, str) or not function.isidentifier():
            raise ValueError("function must be an identifier")
        evidence["source_sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    except (OSError, ValueError) as exc:
        return {**evidence, "status": "UNSUPPORTED", "reason": str(exc)}
    from ..nowindow import hidden_window

    try:
        worker = subprocess.run([sys.executable, "-I", "-m", "vkit.operations.termination"],
                                input=encode_json({"source": source, "function": function,
                                                   "timeout_ms": timeout_seconds * 1_000}, max_bytes=MAX_REQUEST),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                timeout=timeout_seconds, **hidden_window())
        if worker.returncode or len(worker.stdout.encode("utf-8")) > MAX_OUTPUT:
            raise ValueError("loop worker did not return bounded evidence")
        result = json.loads(worker.stdout)
        if not isinstance(result, dict):
            raise ValueError("loop worker returned a non-object")
        return {**evidence, **result}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**evidence, "status": "UNKNOWN", "reason": str(exc)}


if __name__ == "__main__":
    _worker()

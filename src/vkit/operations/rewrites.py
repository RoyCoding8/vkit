"""Whole-function equivalence checks over the expression frontend's shared IR."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .expressions import (
    MODEL_SCOPE,
    FunctionModel,
    ParseFailure,
    ResourceLimits,
    Term,
    parse_function,
)

Status = Literal["PROVED", "COUNTEREXAMPLE", "UNKNOWN", "UNSUPPORTED", "UNAVAILABLE"]


@dataclass(frozen=True)
class ProofResult:
    status: Status
    model_scope: str = MODEL_SCOPE
    backend: str = "cvc5"
    backend_version: str | None = None
    source_sha256: str | None = None
    counterexample: dict[str, int | bool] | None = None
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model_scope": self.model_scope,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "source_sha256": self.source_sha256,
            "counterexample": self.counterexample,
            "reason": self.reason,
        }


def _same_signature(left: FunctionModel, right: FunctionModel) -> bool:
    return (left.name == right.name and left.return_sort == right.return_sort
            and left.parameters == right.parameters)


def _to_solver(term: Term, variables: dict[str, Any], solver: Any, cvc5: Any) -> Any:
    kind = cvc5.Kind
    if term.op == "int_const":
        return solver.mkInteger(str(term.value))
    if term.op == "bool_const":
        return solver.mkBoolean(bool(term.value))
    if term.op == "var":
        return variables[str(term.value)]
    args = tuple(_to_solver(arg, variables, solver, cvc5) for arg in term.args)
    if term.op in ("add", "sub", "mul"):
        op = {"add": kind.ADD, "sub": kind.SUB, "mul": kind.MULT}[term.op]
        return solver.mkTerm(op, *args)
    if term.op == "eq":
        return solver.mkTerm(kind.EQUAL, *args)
    if term.op == "ne":
        return solver.mkTerm(kind.NOT, solver.mkTerm(kind.EQUAL, *args))
    if term.op in ("lt", "le", "gt", "ge"):
        op = {"lt": kind.LT, "le": kind.LEQ, "gt": kind.GT, "ge": kind.GEQ}[term.op]
        return solver.mkTerm(op, *args)
    if term.op == "and":
        return solver.mkTerm(kind.AND, *args)
    if term.op == "or":
        return solver.mkTerm(kind.OR, *args)
    if term.op == "not":
        return solver.mkTerm(kind.NOT, *args)
    if term.op == "ite":
        return solver.mkTerm(kind.ITE, *args)
    raise ValueError(f"unknown IR operation {term.op!r}")


def _evaluate(term: Term, values: dict[str, int | bool]) -> int | bool:
    if term.op in ("int_const", "bool_const"):
        return term.value  # type: ignore[return-value]
    if term.op == "var":
        return values[str(term.value)]
    args = tuple(_evaluate(arg, values) for arg in term.args)
    if term.op == "add":
        return int(args[0]) + int(args[1])
    if term.op == "sub":
        return int(args[0]) - int(args[1])
    if term.op == "mul":
        return int(args[0]) * int(args[1])
    if term.op == "eq":
        return args[0] == args[1]
    if term.op == "ne":
        return args[0] != args[1]
    if term.op == "lt":
        return int(args[0]) < int(args[1])
    if term.op == "le":
        return int(args[0]) <= int(args[1])
    if term.op == "gt":
        return int(args[0]) > int(args[1])
    if term.op == "ge":
        return int(args[0]) >= int(args[1])
    if term.op == "and":
        return all(bool(value) for value in args)
    if term.op == "or":
        return any(bool(value) for value in args)
    if term.op == "not":
        return not bool(args[0])
    if term.op == "ite":
        return args[1] if bool(args[0]) else args[2]
    raise ValueError(f"unknown IR operation {term.op!r}")


def prove_equivalent(original: FunctionModel, replacement: FunctionModel,
                     limits: ResourceLimits | None = None) -> ProofResult:
    """Ask cvc5 whether any parameter tuple gives different return values."""
    limits = limits or ResourceLimits()
    if not _same_signature(original, replacement):
        return ProofResult("UNSUPPORTED", source_sha256=original.source_sha256,
                           reason="replacement must have the same function name and signature")
    try:
        import cvc5
    except ImportError as exc:
        return ProofResult("UNAVAILABLE", source_sha256=original.source_sha256,
                           reason=f"cvc5 is unavailable: {exc}")
    version = getattr(cvc5, "__version__", "unknown")
    try:
        solver = cvc5.Solver()
        solver.setLogic("ALL")
        solver.setOption("produce-models", "true")
        solver.setOption("tlimit-per", str(limits.timeout_ms))
        sorts = {"int": solver.getIntegerSort(), "bool": solver.getBooleanSort()}
        variables = {
            parameter.name: solver.mkConst(sorts[parameter.sort], f"arg_{index}_{parameter.name}")
            for index, parameter in enumerate(original.parameters)
        }
        left = _to_solver(original.body, variables, solver, cvc5)
        right = _to_solver(replacement.body, variables, solver, cvc5)
        different = solver.mkTerm(cvc5.Kind.NOT, solver.mkTerm(cvc5.Kind.EQUAL, left, right))
        solver.assertFormula(different)
        result = solver.checkSat()
        if result.isUnsat():
            return ProofResult("PROVED", backend_version=version, source_sha256=original.source_sha256)
        if result.isUnknown():
            return ProofResult("UNKNOWN", backend_version=version, source_sha256=original.source_sha256,
                               reason=str(result))
        counterexample: dict[str, int | bool] = {}
        for parameter in original.parameters:
            value = solver.getValue(variables[parameter.name])
            counterexample[parameter.name] = (
                bool(value.getBooleanValue()) if parameter.sort == "bool"
                else int(value.getIntegerValue())
            )
        if _evaluate(original.body, counterexample) == _evaluate(replacement.body, counterexample):
            return ProofResult("UNKNOWN", backend_version=version, source_sha256=original.source_sha256,
                               reason="cvc5 model did not reproduce the counterexample in the IR evaluator")
        return ProofResult("COUNTEREXAMPLE", backend_version=version,
                           source_sha256=original.source_sha256, counterexample=counterexample)
    except Exception as exc:
        return ProofResult("UNKNOWN", backend_version=version, source_sha256=original.source_sha256,
                           reason=f"cvc5 could not decide this query: {type(exc).__name__}: {exc}")


def check_function_rewrite(source: str, function: str, replacement: str,
                           limits: ResourceLimits | None = None) -> ProofResult:
    """Parse both function sources and check their complete annotated domains."""
    limits = limits or ResourceLimits()
    original = parse_function(source, function, limits)
    if isinstance(original, ParseFailure):
        return ProofResult(original.status, source_sha256=None, reason=original.reason)
    revised = parse_function(replacement, function, limits)
    if isinstance(revised, ParseFailure):
        return ProofResult(revised.status, source_sha256=original.source_sha256, reason=revised.reason)
    return prove_equivalent(original, revised, limits)

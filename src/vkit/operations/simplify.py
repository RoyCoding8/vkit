"""Bounded e-graph simplification for the supported pure Python subset."""

from __future__ import annotations

import hashlib
import importlib.metadata
from dataclasses import dataclass
from typing import Any

from .expressions import (
    MODEL_SCOPE,
    FunctionModel,
    ParseFailure,
    ResourceLimits,
    Term,
    parse_function,
)
from .rewrites import ProofResult, Status, check_function_rewrite

try:
    from egglog import (
        BigInt,
        BigIntLike,
        Bool,
        BoolLike,
        EGraph,
        Expr,
        expr_parts,
        i64Like,
        rewrite,
        ruleset,
        vars_,
    )
except ImportError:
    _EGGLOG_IMPORT_ERROR: str | None = "egglog is not installed"
else:
    _EGGLOG_IMPORT_ERROR = None


MAX_EGRAPH_ITERATIONS = 4


@dataclass(frozen=True, slots=True)
class SimplifyResult:
    status: Status
    replacement: str | None
    source_sha256: str
    model_scope: str
    simplifier_backend: str
    simplifier_version: str | None
    proof_backend: str
    proof_version: str | None
    reason: str | None = None
    proof: ProofResult | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "replacement": self.replacement,
            "source_sha256": self.source_sha256,
            "model_scope": self.model_scope,
            "simplifier_backend": self.simplifier_backend,
            "simplifier_version": self.simplifier_version,
            "proof_backend": self.proof_backend,
            "proof_version": self.proof_version,
            "reason": self.reason,
            "proof": self.proof.to_json() if self.proof is not None else None,
        }


if _EGGLOG_IMPORT_ERROR is None:

    class _IntExpr(Expr):
        @classmethod
        def literal(cls, value: BigIntLike) -> _IntExpr: ...

        @classmethod
        def variable(cls, index: i64Like) -> _IntExpr: ...

        def add(self, other: _IntExpr) -> _IntExpr: ...

        def sub(self, other: _IntExpr) -> _IntExpr: ...

        def mul(self, other: _IntExpr) -> _IntExpr: ...

        def eq_int(self, other: _IntExpr) -> _BoolExpr: ...

        def ne_int(self, other: _IntExpr) -> _BoolExpr: ...

        def lt(self, other: _IntExpr) -> _BoolExpr: ...

        def le(self, other: _IntExpr) -> _BoolExpr: ...

        def gt(self, other: _IntExpr) -> _BoolExpr: ...

        def ge(self, other: _IntExpr) -> _BoolExpr: ...

        def choose(
            self, condition: _BoolExpr, otherwise: _IntExpr
        ) -> _IntExpr: ...


    class _BoolExpr(Expr):
        @classmethod
        def literal(cls, value: BoolLike) -> _BoolExpr: ...

        @classmethod
        def variable(cls, index: i64Like) -> _BoolExpr: ...

        def and_(self, other: _BoolExpr) -> _BoolExpr: ...

        def or_(self, other: _BoolExpr) -> _BoolExpr: ...

        def not_(self) -> _BoolExpr: ...

        def eq_bool(self, other: _BoolExpr) -> _BoolExpr: ...

        def ne_bool(self, other: _BoolExpr) -> _BoolExpr: ...

        def choose(
            self, condition: _BoolExpr, otherwise: _BoolExpr
        ) -> _BoolExpr: ...


    _ZERO = _IntExpr.literal(BigInt.from_string("0"))
    _ONE = _IntExpr.literal(BigInt.from_string("1"))
    _TRUE = _BoolExpr.literal(Bool(True))
    _FALSE = _BoolExpr.literal(Bool(False))
    _IX, _IY, _IZ = vars_("ix iy iz", _IntExpr)
    _BX, _BY = vars_("bx by", _BoolExpr)
    _IA, _IB = vars_("ia ib", BigInt)
    _RULES = ruleset(
        rewrite(_IX.mul(_IY).add(_IX.mul(_IZ))).to(_IX.mul(_IY.add(_IZ))),
        rewrite(_IY.mul(_IX).add(_IZ.mul(_IX))).to(_IY.add(_IZ).mul(_IX)),
        rewrite(_IX.add(_ZERO)).to(_IX),
        rewrite(_ZERO.add(_IX)).to(_IX),
        rewrite(_IX.sub(_ZERO)).to(_IX),
        rewrite(_IX.sub(_IX)).to(_ZERO),
        rewrite(_IX.mul(_ONE)).to(_IX),
        rewrite(_ONE.mul(_IX)).to(_IX),
        rewrite(_IX.mul(_ZERO)).to(_ZERO),
        rewrite(_ZERO.mul(_IX)).to(_ZERO),
        rewrite(_IntExpr.literal(_IA).add(_IntExpr.literal(_IB))).to(
            _IntExpr.literal(_IA + _IB)
        ),
        rewrite(_IntExpr.literal(_IA).sub(_IntExpr.literal(_IB))).to(
            _IntExpr.literal(_IA - _IB)
        ),
        rewrite(_IntExpr.literal(_IA).mul(_IntExpr.literal(_IB))).to(
            _IntExpr.literal(_IA * _IB)
        ),
        rewrite(_IntExpr.literal(_IA).eq_int(_IntExpr.literal(_IB))).to(
            _BoolExpr.literal(_IA.bool_eq(_IB))
        ),
        rewrite(_IntExpr.literal(_IA).ne_int(_IntExpr.literal(_IB))).to(
            _BoolExpr.literal(~_IA.bool_eq(_IB))
        ),
        rewrite(_IntExpr.literal(_IA).lt(_IntExpr.literal(_IB))).to(
            _BoolExpr.literal(_IA.bool_lt(_IB))
        ),
        rewrite(_IntExpr.literal(_IA).le(_IntExpr.literal(_IB))).to(
            _BoolExpr.literal(_IA.bool_le(_IB))
        ),
        rewrite(_IntExpr.literal(_IA).gt(_IntExpr.literal(_IB))).to(
            _BoolExpr.literal(_IA.bool_gt(_IB))
        ),
        rewrite(_IntExpr.literal(_IA).ge(_IntExpr.literal(_IB))).to(
            _BoolExpr.literal(_IA.bool_ge(_IB))
        ),
        rewrite(_IX.eq_int(_IX)).to(_TRUE),
        rewrite(_IX.ne_int(_IX)).to(_FALSE),
        rewrite(_IX.lt(_IX)).to(_FALSE),
        rewrite(_IX.le(_IX)).to(_TRUE),
        rewrite(_IX.gt(_IX)).to(_FALSE),
        rewrite(_IX.ge(_IX)).to(_TRUE),
        rewrite(_BX.and_(_TRUE)).to(_BX),
        rewrite(_TRUE.and_(_BX)).to(_BX),
        rewrite(_BX.and_(_FALSE)).to(_FALSE),
        rewrite(_FALSE.and_(_BX)).to(_FALSE),
        rewrite(_BX.or_(_FALSE)).to(_BX),
        rewrite(_FALSE.or_(_BX)).to(_BX),
        rewrite(_BX.or_(_TRUE)).to(_TRUE),
        rewrite(_TRUE.or_(_BX)).to(_TRUE),
        rewrite(_BX.and_(_BX)).to(_BX),
        rewrite(_BX.or_(_BX)).to(_BX),
        rewrite(_TRUE.not_()).to(_FALSE),
        rewrite(_FALSE.not_()).to(_TRUE),
        rewrite(_BX.not_().not_()).to(_BX),
        rewrite(_BX.eq_bool(_BX)).to(_TRUE),
        rewrite(_BX.ne_bool(_BX)).to(_FALSE),
        rewrite(_IX.choose(_TRUE, _IY)).to(_IX),
        rewrite(_IX.choose(_FALSE, _IY)).to(_IY),
        rewrite(_IX.choose(_BX, _IX)).to(_IX),
        rewrite(_BX.choose(_TRUE, _BY)).to(_BX),
        rewrite(_BX.choose(_FALSE, _BY)).to(_BY),
        rewrite(_BX.choose(_BY, _BX)).to(_BX),
    )


def simplify_function(
    source: str,
    function: str,
    *,
    limits: ResourceLimits | None = None,
) -> SimplifyResult:
    """Suggest and independently prove a smaller replacement function."""
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    parsed = parse_function(source, function, limits)
    if isinstance(parsed, ParseFailure):
        return _result(parsed.status, None, source_sha256, reason=parsed.reason)

    if _EGGLOG_IMPORT_ERROR is not None:
        return _result("UNAVAILABLE", None, source_sha256, reason=_EGGLOG_IMPORT_ERROR)

    try:
        candidate = _extract_candidate(parsed)
    except (ValueError, TypeError, KeyError) as error:
        return _result("UNSUPPORTED", None, source_sha256, reason=str(error))
    except Exception as error:
        return _result(
            "UNKNOWN",
            None,
            source_sha256,
            reason=f"Egglog search failed: {type(error).__name__}.",
        )
    replacement = _render_function(parsed, candidate)
    proof = check_function_rewrite(source, function, replacement, limits=limits)
    return _result(
        proof.status,
        replacement,
        parsed.source_sha256,
        reason=proof.reason,
        proof=proof,
    )


def _result(
    status: Status,
    replacement: str | None,
    source_sha256: str,
    *,
    reason: str | None = None,
    proof: ProofResult | None = None,
) -> SimplifyResult:
    return SimplifyResult(
        status=status,
        replacement=replacement,
        source_sha256=source_sha256,
        model_scope=MODEL_SCOPE,
        simplifier_backend="egglog",
        simplifier_version=_version("egglog"),
        proof_backend="cvc5",
        proof_version=proof.backend_version if proof is not None else _version("cvc5"),
        reason=reason,
        proof=proof,
    )


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _term_size(term: Term) -> int:
    return 1 + sum(_term_size(child) for child in term.args)


def _extract_candidate(model: FunctionModel) -> Term:
    indices: dict[str, int] = {
        parameter.name: index for index, parameter in enumerate(model.parameters)
    }
    names = {index: name for name, index in indices.items()}
    root = _to_egglog(model.body, indices)
    graph = EGraph()
    graph.register(root)
    graph.run(_RULES * MAX_EGRAPH_ITERATIONS)
    extracted, cost = graph.extract(
        root, include_cost=True, cost_model=_ast_node_cost
    )
    candidate = _from_egglog_decl(expr_parts(extracted), names)
    if cost != _term_size(candidate):
        raise RuntimeError("Egglog extraction cost does not match AST node count.")
    return candidate


def _to_egglog(term: Term, indices: dict[str, int]) -> Any:
    args = [_to_egglog(argument, indices) for argument in term.args]
    op = term.op
    if op == "int_const":
        return _IntExpr.literal(BigInt.from_string(str(term.value)))
    if op == "bool_const":
        return _BoolExpr.literal(Bool(bool(term.value)))
    if op == "var":
        if not isinstance(term.value, str):
            raise ValueError("A variable term has no name.")
        index = indices[term.value]
        if term.sort == "int":
            return _IntExpr.variable(index)
        return _BoolExpr.variable(index)
    if op == "add":
        return args[0].add(args[1])
    if op == "sub":
        return args[0].sub(args[1])
    if op == "mul":
        return args[0].mul(args[1])
    if op == "eq":
        return args[0].eq_int(args[1]) if term.args[0].sort == "int" else args[0].eq_bool(args[1])
    if op == "ne":
        return args[0].ne_int(args[1]) if term.args[0].sort == "int" else args[0].ne_bool(args[1])
    if op in ("lt", "le", "gt", "ge"):
        return getattr(args[0], op)(args[1])
    if op == "and":
        result = args[0]
        for argument in args[1:]:
            result = result.and_(argument)
        return result
    if op == "or":
        result = args[0]
        for argument in args[1:]:
            result = result.or_(argument)
        return result
    if op == "not":
        return args[0].not_()
    if op == "ite":
        return args[1].choose(args[0], args[2])
    raise ValueError(f"Unsupported expression operation {op!r}.")


def _ast_node_cost(egraph: Any, expression: Any, children_costs: list[int]) -> int:
    type_name = expr_parts(expression).tp.ident.name
    if type_name in {"_IntExpr", "_BoolExpr"}:
        return 1 + sum(children_costs)
    return 0


def _from_egglog_decl(declaration: Any, names: dict[int, str]) -> Term:
    call = declaration.expr
    if not hasattr(call, "callable"):
        raise ValueError("Egglog extracted a non-constructor expression.")
    reference = call.callable
    method = getattr(reference, "method_name", None)
    arguments = call.args
    type_name = declaration.tp.ident.name

    if method == "literal" and type_name == "_IntExpr":
        string_call = arguments[0].expr
        string_value = string_call.args[0].expr.value
        return Term("int_const", value=int(string_value), sort="int")
    if method == "literal" and type_name == "_BoolExpr":
        value = arguments[0].expr.value
        return Term("bool_const", value=bool(value), sort="bool")
    if method == "variable":
        index = arguments[0].expr.value
        if index not in names:
            raise ValueError("Egglog extracted an unknown variable.")
        return Term("var", value=names[index], sort="int" if type_name == "_IntExpr" else "bool")

    op_map = {
        "add": "add",
        "sub": "sub",
        "mul": "mul",
        "eq_int": "eq",
        "ne_int": "ne",
        "lt": "lt",
        "le": "le",
        "gt": "gt",
        "ge": "ge",
        "eq_bool": "eq",
        "ne_bool": "ne",
        "and_": "and",
        "or_": "or",
        "not_": "not",
        "choose": "ite",
    }
    if method not in op_map:
        raise ValueError(f"Egglog extracted an unknown constructor {method!r}.")
    children = tuple(_from_egglog_decl(argument, names) for argument in arguments)
    if method == "choose":
        children = (children[1], children[0], children[2])
    return Term(op_map[method], children, sort="int" if type_name == "_IntExpr" else "bool")


def _render_function(model: FunctionModel, body: Term) -> str:
    parameters = ", ".join(
        f"{parameter.name}: {parameter.sort}"
        for parameter in model.parameters
    )
    return f"def {model.name}({parameters}) -> {model.return_sort}:\n    return {_render_term(body)}\n"


def _render_term(term: Term) -> str:
    if term.op == "var":
        return str(term.value)
    if term.op == "int_const":
        return str(term.value)
    if term.op == "bool_const":
        return "True" if term.value else "False"
    if term.op in {"add", "sub", "mul", "eq", "ne", "lt", "le", "gt", "ge", "and", "or"}:
        operator = {
            "add": "+",
            "sub": "-",
            "mul": "*",
            "eq": "==",
            "ne": "!=",
            "lt": "<",
            "le": "<=",
            "gt": ">",
            "ge": ">=",
            "and": "and",
            "or": "or",
        }[term.op]
        left, right = (_render_term(child) for child in term.args)
        return f"({left} {operator} {right})"
    if term.op == "not":
        return f"(not {_render_term(term.args[0])})"
    if term.op == "ite":
        condition, then, otherwise = term.args
        return f"({_render_term(then)} if {_render_term(condition)} else {_render_term(otherwise)})"
    raise ValueError(f"Unsupported expression operation {term.op!r}.")

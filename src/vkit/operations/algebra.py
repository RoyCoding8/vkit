"""Exact rational-function comparison with a separate coefficient checker."""
from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ._data import encode_json, read_json

BACKEND_VERSION = "1.14.0"
MAX_BYTES = 40_000
MAX_OUTPUT = 250_000
MAX_TERMS = 1_024
MAX_BITS = 2_048
MODEL_SCOPE = (
    "Identity in the rational-function field Q(x1,...,xn), using exact rational coefficients. "
    "Expressions are compared where all original division denominators are nonzero. "
    "An identity does not assert equality of the expressions' domains at removed poles. "
    "No floating-point, transcendental, branch, or program-runtime semantics are modeled."
)


class _Limit(ValueError):
    pass


def _validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"version", "variables", "before", "after"}:
        raise ValueError("bundle needs exactly version, variables, before and after")
    variables = raw["variables"]
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("version must be 1")
    if (not isinstance(variables, list) or not 1 <= len(variables) <= 4
            or any(not isinstance(v, str) or not v.isascii() or not v.isidentifier()
                   or len(v) > 32 for v in variables) or len(set(variables)) != len(variables)):
        raise ValueError("variables must contain 1 to 4 distinct ASCII identifiers")
    count = 0

    def visit(node: Any, depth: int = 0) -> None:
        nonlocal count
        count += 1
        if count > 128 or depth > 24:
            raise ValueError("expression exceeds 128 nodes or depth 24")
        if not isinstance(node, dict) or len(node) != 1:
            raise ValueError("each expression node must have one operator")
        op, args = next(iter(node.items()))
        if op == "var":
            if not isinstance(args, str) or args not in variables:
                raise ValueError("undeclared variable")
        elif op == "rational":
            if (not isinstance(args, list) or len(args) != 2
                    or any(type(v) is not int or v.bit_length() > 128 for v in args)
                    or args[1] == 0):
                raise ValueError("rational needs two bounded integers and a nonzero denominator")
        elif op in {"add", "sub", "mul", "div"}:
            if not isinstance(args, list) or len(args) != 2:
                raise ValueError(f"{op} needs two expressions")
            for child in args:
                visit(child, depth + 1)
        elif op == "pow":
            if (not isinstance(args, list) or len(args) != 2
                    or type(args[1]) is not int or not 0 <= args[1] <= 8):
                raise ValueError("pow needs an expression and an integer exponent from 0 to 8")
            visit(args[0], depth + 1)
        else:
            raise ValueError(f"unsupported expression operator {op!r}")

    visit(raw["before"])
    visit(raw["after"])
    return raw


def _calculate(bundle: dict[str, Any]) -> dict[str, Any]:
    import sympy as sp

    variables = bundle["variables"]
    symbols = tuple(sp.Symbol(name) for name in variables)
    zero = (0,) * len(variables)
    one = {zero: Fraction(1)}
    work = 0
    guards: list[dict[tuple[int, ...], Fraction]] = []

    def checked(poly):
        clean = {m: c for m, c in poly.items() if c}
        if len(clean) > MAX_TERMS or any(sum(m) > 64 or c.numerator.bit_length() > MAX_BITS
                                       or c.denominator.bit_length() > MAX_BITS for m, c in clean.items()):
            raise _Limit("polynomial exceeds term, degree or coefficient limits")
        return clean

    def add(a, b, sign=1):
        result = dict(a)
        for monomial, coefficient in b.items():
            result[monomial] = result.get(monomial, Fraction()) + sign * coefficient
        return checked(result)

    def mul(a, b):
        nonlocal work
        work += len(a) * len(b)
        if work > 250_000:
            raise _Limit("coefficient multiplication budget exhausted")
        result = {}
        for ma, ca in a.items():
            for mb, cb in b.items():
                monomial = tuple(x + y for x, y in zip(ma, mb))
                result[monomial] = result.get(monomial, Fraction()) + ca * cb
        return checked(result)

    def parse(node):
        op, args = next(iter(node.items()))
        if op == "rational":
            value = Fraction(*args)
            return ({zero: value} if value else {}, one, sp.Rational(*args))
        if op == "var":
            index = variables.index(args)
            monomial = tuple(int(i == index) for i in range(len(variables)))
            return {monomial: Fraction(1)}, one, symbols[index]
        an, ad, av = parse(args[0])
        if op == "pow":
            n, d = one, one
            for _ in range(args[1]):
                n, d = mul(n, an), mul(d, ad)
            return n, d, av ** args[1]
        bn, bd, bv = parse(args[1])
        if op in {"add", "sub"}:
            sign = 1 if op == "add" else -1
            return add(mul(an, bd), mul(bn, ad), sign), mul(ad, bd), av + sign * bv
        if op == "mul":
            return mul(an, bn), mul(ad, bd), av * bv
        if not bn:
            raise ValueError("division by an identically zero rational expression")
        guards.append(bn)
        return mul(an, bd), mul(ad, bn), av / bv

    an, ad, av = parse(bundle["before"])
    bn, bd, bv = parse(bundle["after"])
    difference = add(mul(an, bd), mul(bn, ad), -1)

    def to_sympy(poly):
        return sum((sp.Rational(c.numerator, c.denominator)
                    * sp.prod(s ** exponent for s, exponent in zip(symbols, m))
                    for m, c in poly.items()), sp.Integer(0))

    def coefficients(expr):
        poly = sp.Poly(expr, *symbols, domain=sp.QQ)
        return {tuple(m): Fraction(int(c.p), int(c.q)) for m, c in poly.terms() if c}

    numerator, denominator = sp.fraction(sp.cancel(av - bv))
    backend_n, backend_d = coefficients(numerator), coefficients(denominator)
    original_d = mul(ad, bd)
    if mul(difference, backend_d) != mul(backend_n, original_d):
        return {"status": "UNKNOWN", "reason": "backend normal form failed exact coefficient verification"}

    def serialized(poly):
        return [{"powers": list(m), "coefficient": [c.numerator, c.denominator]}
                for m, c in sorted(poly.items())]

    return {
        "status": "IDENTICAL" if not difference else "NOT_IDENTICAL",
        "normal_difference": {"numerator": serialized(backend_n), "denominator": serialized(backend_d)},
        "cross_product": serialized(difference),
        "nonzero_guards": [serialized(g) for g in guards],
        "verification": "independent exact sparse coefficient cross multiplication",
        "coefficient_products": work,
    }


def _worker() -> None:
    try:
        bundle = _validate(json.loads(sys.stdin.read(MAX_BYTES + 1)))
        result = _calculate(bundle)
    except _Limit as exc:
        result = {"status": "UNKNOWN", "reason": str(exc)}
    except (ValueError, RecursionError) as exc:
        result = {"status": "UNSUPPORTED", "reason": str(exc)}
    try:
        print(encode_json(result, max_bytes=MAX_OUTPUT))
    except ValueError:
        print('{"status":"UNKNOWN","reason":"evidence exceeded output limit"}')


def compare_rational_expressions(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be from 1 to 60")
    result: dict[str, Any] = {"status": "UNSUPPORTED", "model_scope": MODEL_SCOPE}
    try:
        raw, digest = read_json(root, path, max_bytes=MAX_BYTES, reject_symlinks=True)
        bundle = _validate(raw)
        result["input_sha256"] = digest
    except (ValueError, RecursionError) as exc:
        return {**result, "reason": str(exc)}
    try:
        installed = version("sympy")
    except PackageNotFoundError:
        installed = None
    result["backend"] = {"name": "sympy", "version": installed}
    if installed != BACKEND_VERSION:
        return {**result, "status": "UNAVAILABLE", "reason": "pinned SymPy 1.14.0 is required"}
    from ..nowindow import hidden_window

    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "vkit.operations.algebra"],
            input=encode_json(bundle, max_bytes=MAX_BYTES), text=True, encoding="utf-8",
            capture_output=True, timeout=timeout_seconds, **hidden_window(),
        )
        if completed.returncode or len(completed.stdout.encode("utf-8")) > MAX_OUTPUT:
            return {**result, "status": "UNKNOWN", "reason": "algebra worker did not produce bounded evidence"}
        output = json.loads(completed.stdout)
        if not isinstance(output, dict) or output.get("status") not in {"IDENTICAL", "NOT_IDENTICAL", "UNSUPPORTED", "UNKNOWN"}:
            raise ValueError("invalid algebra worker result")
        return {**result, **output}
    except subprocess.TimeoutExpired:
        return {**result, "status": "UNKNOWN", "reason": "algebra deadline exceeded"}
    except (OSError, ValueError) as exc:
        return {**result, "status": "UNKNOWN", "reason": str(exc)}


if __name__ == "__main__":
    _worker()

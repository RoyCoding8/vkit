"""Deductive scalar C contracts with an owned ACSL/WP proof harness."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ..proc import run
from ._data import encode_json, read_json
from ._runtime import pinned_binary
from .c_safety import (
    _UnsupportedSource, _parser, _process_env, _read_source, _scalar_type,
    _source_path, _validate_ast,
)
from .expressions import Term, UnsupportedSource, _Budget, _parse_expr, _validate_expr_shape
from .rewrites import _evaluate, _to_solver
from .synthesis import _load_cvc5

_MAX_OUTPUT = 262_144
_SCOPE = (
    "Frama-C 33.0 WP/Qed; C11 x86_64, 32-bit int and unsigned int, scalar _Bool. "
    "The supplied preconditions, postconditions and loop invariants describe the claim. "
    "All generated obligations, runtime guards and termination goals must be discharged. "
    "No application requirement, pointer, heap, concurrency or external proof-kernel claim."
)
_INAPPLICABLE_WARNINGS = {
    "[wp] Warning: Skipped RTE guards: unaligned pointers (\\aligned not supported)",
    "[wp] Warning: Skipped RTE guards: invalid function pointer calls (\\valid_function not supported)",
}


def _expression(text: Any, environment: dict[str, Term], budget: _Budget, sort: str) -> Term:
    if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 2048:
        raise ValueError("contract expressions must contain 1..2048 UTF-8 bytes")
    try:
        expression = ast.parse(text, mode="eval").body
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("contract expression is outside the supported syntax") from exc
    pending = [(expression, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 256 or depth > 32:
            raise ValueError("contract expression exceeds its syntax limit")
        if isinstance(node, ast.Constant) and type(node.value) is int and node.value.bit_length() > 256:
            raise ValueError("contract integer constants must fit 256 bits")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    _validate_expr_shape(expression)
    term = _parse_expr(expression, environment, budget)
    if term.sort != sort:
        raise ValueError(f"this contract expression must have {sort} sort")
    return term


def _render(term: Term) -> str:
    if term.op == "int_const":
        return str(term.value)
    if term.op == "bool_const":
        return "\\true" if term.value else "\\false"
    if term.op == "var":
        name = "\\result" if term.value == "result" else str(term.value)
        return f"({name} != 0)" if term.sort == "bool" else name
    args = [_render(arg) for arg in term.args]
    if term.op == "not":
        return f"(!{args[0]})"
    if term.op == "ite":
        return f"({args[0]} ? {args[1]} : {args[2]})"
    operator = {"add": "+", "sub": "-", "mul": "*", "eq": "==", "ne": "!=",
                "lt": "<", "le": "<=", "gt": ">", "ge": ">=", "and": "&&", "or": "||"}[term.op]
    return "(" + f" {operator} ".join(args) + ")"


def _clauses(value: Any, environment: dict[str, Term], budget: _Budget) -> list[Term]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("each contract clause list must contain 1..8 expressions")
    return [_expression(item, environment, budget, "bool") for item in value]


def _capture(root: Path, document: Any) -> tuple[bytes, str, list[Term], dict[str, tuple[str, ...]], set[str]]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "source", "function", "requires", "ensures", "loops",
    } or type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("expected the exact C contract schema_version 1 fields")
    function = document["function"]
    if not isinstance(function, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", function):
        raise ValueError("function must be a supported C identifier")
    source = _read_source(_source_path(root, document["source"]))
    parser = _parser()
    if parser is None:
        raise ImportError("requires pycparser 3.0")
    c_ast, c_generator, c_parser = parser
    try:
        tree = c_parser.CParser().parse(source.decode("utf-8").replace("\r\n", "\n"))
    except (c_parser.ParseError, RecursionError) as exc:
        raise ValueError("source must contain comment-free, preprocessed scalar C") from exc
    _validate_ast(c_ast, tree, function)
    definition = tree.ext[0]
    return_type = _scalar_type(c_ast, definition.decl.type.type)
    parameters = [item for item in definition.decl.type.args.params if isinstance(item, c_ast.Decl)]
    if len(parameters) > 16:
        raise ValueError("at most 16 scalar parameters are supported")
    types = {item.name: _scalar_type(c_ast, item.type) for item in parameters}
    if len(types) != len(parameters):
        raise ValueError("duplicate parameter names are unsupported")
    items = definition.body.block_items or []
    if not items or not isinstance(items[-1], c_ast.Return) or items[-1].expr is None:
        raise ValueError("the function must end in an explicit scalar return")
    locals_: dict[str, tuple[str, ...]] = {}
    for item in items:
        if not isinstance(item, c_ast.Decl):
            break
        if item.name in types or item.name in locals_:
            raise ValueError("shadowed or duplicate declarations are unsupported")
        locals_[item.name] = _scalar_type(c_ast, item.type)
    leading = {id(item) for item in items[:len(locals_)]}
    parameter_ids = {id(item) for item in parameters}
    loops: list[Any] = []
    pending = [definition]
    nodes = 0
    while pending:
        node = pending.pop()
        nodes += 1
        if nodes > 2048:
            raise ValueError("C contract source is limited to 2048 AST nodes")
        if isinstance(node, (c_ast.ArrayDecl, c_ast.ArrayRef, c_ast.InitList)):
            raise ValueError("only scalar C is supported")
        if isinstance(node, c_ast.Decl) and node is not definition.decl and id(node) not in parameter_ids \
                and id(node) not in leading:
            raise ValueError("local declarations must precede all function statements")
        if isinstance(node, c_ast.ID) and (node.name == "result" or node.name.startswith("vkit_")):
            raise ValueError("result and vkit_ identifiers are reserved")
        if isinstance(node, c_ast.Decl) and (node.name == "result" or (node.name or "").startswith("vkit_")):
            raise ValueError("result and vkit_ identifiers are reserved")
        target = node.lvalue if isinstance(node, c_ast.Assignment) else (
            node.expr if isinstance(node, c_ast.UnaryOp) and node.op in {"++", "--", "p++", "p--"} else None
        )
        if isinstance(target, c_ast.ID) and target.name in types:
            raise ValueError("contract parameters are immutable in this profile")
        if isinstance(node, (c_ast.While, c_ast.For, c_ast.DoWhile)):
            loops.append(node)
        pending.extend(child for _, child in reversed(node.children()))
    entries = document["loops"]
    if not isinstance(entries, list) or len(entries) != len(loops) or len(loops) > 8:
        raise ValueError("provide exactly one contract per loop in AST preorder, at most eight loops")
    budget = _Budget(4000, 0)
    environment = {name: Term("var", value=name, sort="bool" if kind == ("_Bool",) else "int")
                   for name, kind in types.items()}
    requires = _clauses(document["requires"], environment, budget)
    ensures = _clauses(document["ensures"], {
        **environment, "result": Term("var", value="result", sort="bool" if return_type == ("_Bool",) else "int"),
    }, budget)
    loop_env = {**environment, **{
        name: Term("var", value=name, sort="bool" if kind == ("_Bool",) else "int")
        for name, kind in locals_.items()
    }}
    annotations: dict[int, str] = {}
    expected = {f"{function}_ensures_vkit_post_{index}" for index in range(len(ensures))}
    for index, (loop, entry) in enumerate(zip(loops, entries)):
        if not isinstance(entry, dict) or set(entry) != {"invariants", "variant"}:
            raise ValueError("loop contracts have exactly invariants and variant fields")
        invariants = _clauses(entry["invariants"], loop_env, budget)
        variant = _expression(entry["variant"], loop_env, budget, "int")
        clauses = []
        for offset, term in enumerate(invariants):
            label = f"vkit_inv_{index}_{offset}"
            clauses.append(f"loop invariant {label}: {_render(term)};")
            expected.add(f"{function}_loop_invariant_{label}")
        assigned = ', '.join(locals_) if locals_ else '\\nothing'
        clauses += [f"loop assigns {assigned};",
                    f"loop variant {_render(variant)};"]
        annotations[id(loop)] = "/*@ " + "\n".join(clauses) + " */\n"

    class Generator(c_generator.CGenerator):
        def visit_While(self, node):
            return annotations[id(node)] + super().visit_While(node)

        def visit_For(self, node):
            return annotations[id(node)] + super().visit_For(node)

        def visit_DoWhile(self, node):
            return annotations[id(node)] + super().visit_DoWhile(node)

    contract = [f"requires {_render(term)};" for term in requires]
    contract += ["terminates \\true;", "assigns \\nothing;"]
    contract += [f"ensures vkit_post_{index}: {_render(term)};" for index, term in enumerate(ensures)]
    generated = ("/*@ " + "\n".join(contract) + " */\n" + Generator().visit(tree)).encode("utf-8")
    if len(generated) > 65536:
        raise ValueError("generated contract exceeds 65536 bytes")
    return generated, hashlib.sha256(source).hexdigest(), requires, types, expected


def _admissible(requires: list[Term], types: dict[str, tuple[str, ...]]) -> tuple[str, Any]:
    cvc5, error = _load_cvc5()
    if cvc5 is None:
        return "UNAVAILABLE", error
    solver = cvc5.Solver()
    solver.setLogic("ALL")
    solver.setOption("produce-models", "true")
    solver.setOption("tlimit-per", "1000")
    variables = {name: solver.mkConst(solver.getBooleanSort() if kind == ("_Bool",)
                                     else solver.getIntegerSort(), name) for name, kind in types.items()}
    for name, kind in types.items():
        if kind == ("_Bool",):
            continue
        low, high = (0, 2**32 - 1) if kind == ("unsigned", "int") else (-2**31, 2**31 - 1)
        solver.assertFormula(solver.mkTerm(cvc5.Kind.GEQ, variables[name], solver.mkInteger(str(low))))
        solver.assertFormula(solver.mkTerm(cvc5.Kind.LEQ, variables[name], solver.mkInteger(str(high))))
    for term in requires:
        solver.assertFormula(_to_solver(term, variables, solver, cvc5))
    result = solver.checkSat()
    if result.isUnsat():
        return "INFEASIBLE_PRECONDITION", "no typed input satisfies the preconditions"
    if not result.isSat():
        return "UNKNOWN", "precondition feasibility was not decided"
    values = {name: solver.getValue(value).getBooleanValue() if types[name] == ("_Bool",)
              else int(solver.getValue(value).getIntegerValue()) for name, value in variables.items()}
    if not all(_evaluate(term, values) is True for term in requires):
        return "UNKNOWN", "the feasibility witness did not replay"
    return "SAT", values


def _completion(raw: bytes, log: str, function: str, expected: set[str], source: Path) -> tuple[bool, list[dict[str, Any]]]:
    try:
        entries = json.loads(raw)
    except (ValueError, RecursionError):
        return False, []
    if not isinstance(entries, list) or not entries or len(entries) > 512:
        return False, []
    goals: set[str] = set()
    properties: set[str] = set()
    obligations = []
    all_valid = True
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("function") != function or entry.get("smoke") is not False \
                or entry.get("file") != str(source):
            return False, obligations
        goal, property_ = entry.get("goal"), entry.get("property")
        if not isinstance(goal, str) or goal in goals or not isinstance(property_, str):
            return False, obligations
        provers = entry.get("provers")
        if not isinstance(provers, list) or any(not isinstance(item, dict) or item.get("prover") != "qed"
                                                for item in provers):
            return False, obligations
        valid = (entry.get("passed") is True and entry.get("verdict") == "valid" and entry.get("proved") == 1
                 and len(provers) == 1 and provers[0].get("success") == 1)
        all_valid = all_valid and valid
        goals.add(goal)
        properties.add(property_)
        obligations.append({"goal": goal, "property": property_, "status": "VALID" if valid else "UNPROVED",
                            "prover": "qed"})
    footer = re.findall(r"\[wp\] Proved goals:\s+(\d+)\s*/\s*(\d+)", log)
    if len(footer) != 1 or int(footer[0][0]) != int(footer[0][1]) or int(footer[0][0]) < len(entries):
        return False, obligations
    if any(("Warning:" in line and line not in _INAPPLICABLE_WARNINGS)
           or re.search(r"\b(?:Error|Failure|Abort|Timeout)\b", line) for line in log.splitlines()):
        return False, obligations
    for property_ in expected:
        if property_ not in properties:
            return False, obligations
        if "_loop_invariant_" in property_ and not all(
            f"typed_{property_}_{suffix}" in goals for suffix in ("established", "preserved")
        ):
            return False, obligations
    if any("_loop_invariant_" in property_ for property_ in expected) and not all(
        f"typed_{function}_loop_variant_{suffix}" in goals for suffix in ("positive", "decrease")
    ):
        return False, obligations
    return all_valid, obligations


def prove_c_contract(root: Path, path: str, timeout_seconds: int = 20) -> dict[str, Any]:
    """Prove the captured scalar contract only when every owned proof obligation completes."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be in 1..60")
    document, digest = read_json(root, path, max_bytes=65536, reject_symlinks=True)
    base = {"operation": "prove_c_contract", "path": path, "model_sha256": digest,
            "backend": "frama-c/33.0/wp-qed", "model_scope": _SCOPE}
    deadline = time.monotonic() + timeout_seconds
    try:
        snapshot, source_digest, requires, types, expected = _capture(root, document)
    except ImportError as exc:
        return {**base, "status": "UNAVAILABLE", "detail": str(exc)}
    except (ValueError, _UnsupportedSource, UnsupportedSource, RecursionError) as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    base.update(source_sha256=source_digest, snapshot_sha256=hashlib.sha256(snapshot).hexdigest())
    try:
        status, witness = _admissible(requires, types)
    except (RuntimeError, ValueError, OverflowError) as exc:
        return {**base, "status": "UNKNOWN", "detail": f"precondition solver failed: {exc}"}
    if status != "SAT":
        return {**base, "status": status, "detail": witness}
    base["admissible_input"] = witness
    runtime = pinned_binary("VKIT_FRAMAC_BIN", "VKIT_FRAMAC_SHA256")
    if runtime is None:
        return {**base, "status": "UNAVAILABLE", "detail": "requires an operator-pinned Frama-C 33.0 runtime"}
    binary, sha = runtime
    base["backend_sha256"] = sha
    with tempfile.TemporaryDirectory(prefix="vkit-c-contract-") as temporary:
        workdir = Path(temporary)
        (workdir / "candidate.i").write_bytes(snapshot)
        stdout, stderr, report = workdir / "stdout.log", workdir / "stderr.log", workdir / "proof.json"

        def output_bytes():
            size = 0
            for file in (stdout, stderr, report):
                try:
                    size += file.stat().st_size
                except FileNotFoundError:
                    pass
            return size

        def bounded_run(arguments):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            return run([str(binary), *arguments], cwd=workdir, env=_process_env(workdir),
                       stdout_path=stdout, stderr_path=stderr, timeout_seconds=remaining,
                       should_cancel=lambda: output_bytes() > _MAX_OUTPUT)

        version = bounded_run(["-version"])
        if version is None or version.timed_out or version.cancelled:
            return {**base, "status": "UNKNOWN", "detail": "runtime identity check exceeded its budget"}
        if version.code != 0 or stdout.read_bytes().strip() != b"33.0 (Arsenic)" or stderr.read_bytes():
            return {**base, "status": "UNAVAILABLE", "detail": "runtime did not report Frama-C 33.0 (Arsenic)"}
        result = bounded_run(["-wp", "-wp-prover", "qed", "-wp-rte", "-wp-cache", "none",
                              "-machdep", "x86_64", "-wp-report-json", "proof.json", "candidate.i"])
        if result is None or result.timed_out or result.cancelled:
            return {**base, "status": "UNKNOWN", "detail": "proof exceeded its time or output budget"}
        if result.launch_error:
            return {**base, "status": "UNAVAILABLE", "detail": "Frama-C could not be started"}
        if output_bytes() > _MAX_OUTPUT:
            return {**base, "status": "UNKNOWN", "detail": "proof output exceeded its byte limit"}
        log = (stdout.read_bytes() + stderr.read_bytes()).decode("utf-8", errors="replace")
        complete, obligations = _completion(report.read_bytes() if report.is_file() else b"", log,
                                             document["function"], expected, workdir / "candidate.i")
    response = {**base, "status": "PROVED" if result.code == 0 and complete else "UNKNOWN",
            "obligations": obligations, "snapshot_source": snapshot.decode("utf-8"),
            "detail": "all owned obligations discharged" if result.code == 0 and complete
            else "the owned proof campaign did not discharge every required obligation",
            "diagnostics": log}
    try:
        encode_json(response, max_bytes=_MAX_OUTPUT)
    except ValueError:
        return {**base, "status": "UNKNOWN", "detail": "complete proof evidence exceeded the response byte limit"}
    return response

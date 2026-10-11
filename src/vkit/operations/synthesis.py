"""Synthesize bounded expression candidates and prove them over the parsed model."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = "cvc5"
BACKEND_VERSION = "1.4.2"
PROFILE = "linear-int-v1"
MAX_SOURCE_BYTES = 65_536
MAX_AST_NODES = 1_000
MAX_CANDIDATE_NODES = 1_000
MAX_TIMEOUT_MS = 60_000
MAX_WORKER_INPUT_BYTES = 70_000
MAX_WORKER_OUTPUT_BYTES = 200_000

GRAMMAR = {
    "profile": PROFILE,
    "parameters": "zero or more int-annotated positional parameters",
    "return": "int",
    "expression": "E ::= parameter | -1 | 0 | 1 | E + E | E - E",
    "semantics": "unbounded mathematical integers",
}


class _UnsupportedModel(Exception):
    pass


def _load_cvc5() -> tuple[Any | None, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version(BACKEND)
    except PackageNotFoundError:
        return None, f"{BACKEND} {BACKEND_VERSION} is not installed"
    if installed != BACKEND_VERSION:
        return None, f"requires {BACKEND} {BACKEND_VERSION}; found {installed}"
    try:
        import cvc5
    except ImportError as exc:
        return None, f"{BACKEND} {BACKEND_VERSION} cannot be imported: {exc}"
    return cvc5, None


def _children(term: Any) -> tuple[Any, ...]:
    # The pinned Python binding exposes children through Term iteration.
    return tuple(term)


def _decode_solution(solution: Any, parameters: tuple[str, ...], cvc5: Any) -> Any:
    from .expressions import Term

    kind = cvc5.Kind
    if parameters:
        if solution.getKind() != kind.LAMBDA:
            raise _UnsupportedModel("cvc5 returned a solution without the expected lambda")
        parts = _children(solution)
        if len(parts) != 2 or parts[0].getKind() != kind.VARIABLE_LIST:
            raise _UnsupportedModel("cvc5 returned an unexpected function solution shape")
        bound = _children(parts[0])
        if tuple(item.getSymbol() for item in bound) != parameters:
            raise _UnsupportedModel("cvc5 solution binders do not match the reference parameters")
        body = parts[1]
    else:
        if solution.getKind() == kind.LAMBDA:
            raise _UnsupportedModel("cvc5 returned a lambda for a parameterless function")
        body = solution

    parameter_names = frozenset(parameters)

    def decode(node: Any) -> Any:
        node_kind = node.getKind()
        if node_kind == kind.CONST_INTEGER and node.isIntegerValue():
            return Term("int_const", value=int(node.getIntegerValue()), sort="int")
        if node_kind == kind.VARIABLE:
            name = node.getSymbol()
            if name not in parameter_names:
                raise _UnsupportedModel("cvc5 solution refers to a variable outside its binders")
            return Term("var", value=name, sort="int")
        children = _children(node)
        if node_kind == kind.ADD and len(children) == 2:
            return Term("add", tuple(decode(child) for child in children), sort="int")
        if node_kind == kind.SUB and len(children) == 2:
            return Term("sub", tuple(decode(child) for child in children), sort="int")
        raise _UnsupportedModel(f"cvc5 returned a node outside the declared grammar: {node_kind}")

    return decode(body)


def _check_grammar(term: Any, parameters: frozenset[str]) -> None:
    from .expressions import Term

    pending = [term]
    nodes = 0
    while pending:
        node = pending.pop()
        nodes += 1
        if nodes > MAX_CANDIDATE_NODES:
            raise _UnsupportedModel("cvc5 candidate exceeds the expression-node limit")
        if not isinstance(node, Term):
            raise _UnsupportedModel("decoded solution is not an expression term")
        if node.op == "var":
            if node.sort != "int" or node.value not in parameters or node.args:
                raise _UnsupportedModel("candidate variable is outside the declared grammar")
        elif node.op == "int_const":
            if node.sort != "int" or node.value not in (-1, 0, 1) or node.args:
                raise _UnsupportedModel("candidate constant is outside the declared grammar")
        elif node.op in ("add", "sub") and node.sort == "int" and len(node.args) == 2:
            pending.extend(node.args)
        else:
            raise _UnsupportedModel(f"candidate operation {node.op!r} is outside the declared grammar")


def _synthesize(source: str, function: str, timeout_ms: int) -> dict[str, Any]:
    from .expressions import MODEL_SCOPE, ParseFailure, ResourceLimits, parse_function

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    model = parse_function(
        source, function,
        ResourceLimits(timeout_ms=timeout_ms, max_source_bytes=MAX_SOURCE_BYTES,
                       max_ast_nodes=MAX_AST_NODES),
    )
    base: dict[str, Any] = {
        "profile": PROFILE,
        "grammar": GRAMMAR,
        "model_scope": MODEL_SCOPE,
        "backend": BACKEND,
        "backend_version": None,
        "source_sha256": digest,
        "candidate": None,
        "proof": None,
        "minimality": "not_claimed",
    }
    if isinstance(model, ParseFailure):
        return {**base, "status": "UNSUPPORTED", "reason": model.reason}
    if model.return_sort != "int" or any(parameter.sort != "int" for parameter in model.parameters):
        return {**base, "status": "UNSUPPORTED",
                "reason": "linear-int-v1 requires only int parameters and an int return"}

    cvc5, unavailable = _load_cvc5()
    if unavailable:
        return {**base, "status": "UNAVAILABLE", "reason": unavailable}

    deadline = time.monotonic() + timeout_ms / 1000
    version = BACKEND_VERSION
    try:
        solver = cvc5.Solver()
        solver.setLogic("ALL")
        solver.setOption("sygus", "true")
        solver.setOption("tlimit-per", str(timeout_ms))
        integer = solver.getIntegerSort()
        variables = {
            parameter.name: solver.declareSygusVar(parameter.name, integer)
            for parameter in model.parameters
        }
        ordered_variables = [variables[parameter.name] for parameter in model.parameters]
        start = solver.mkVar(integer, "Start")
        grammar = solver.mkGrammar(ordered_variables, [start])
        grammar.addRules(start, [
            *ordered_variables,
            solver.mkInteger(-1), solver.mkInteger(0), solver.mkInteger(1),
            solver.mkTerm(cvc5.Kind.ADD, start, start),
            solver.mkTerm(cvc5.Kind.SUB, start, start),
        ])
        candidate_fun = solver.synthFun("candidate", ordered_variables, integer, grammar)
        from .rewrites import _to_solver, check_function_rewrite

        reference = _to_solver(model.body, variables, solver, cvc5)
        application = (
            solver.mkTerm(cvc5.Kind.APPLY_UF, candidate_fun, *ordered_variables)
            if ordered_variables else candidate_fun
        )
        solver.addSygusConstraint(solver.mkTerm(cvc5.Kind.EQUAL, application, reference))
        result = solver.checkSynth()
        if result.hasNoSolution():
            return {**base, "backend_version": version, "status": "NO_SOLUTION",
                    "reason": "cvc5 determined no solution exists in the declared grammar"}
        if result.isUnknown() or not result.hasSolution():
            return {**base, "backend_version": version, "status": "UNKNOWN",
                    "reason": f"cvc5 did not solve the synthesis conjecture: {result}"}

        solution = solver.getSynthSolution(candidate_fun)
        candidate_term = _decode_solution(
            solution, tuple(parameter.name for parameter in model.parameters), cvc5,
        )
        _check_grammar(candidate_term, frozenset(parameter.name for parameter in model.parameters))
        from .simplify import _render_function

        replacement = _render_function(model, candidate_term)
        if len(replacement.encode("utf-8")) > MAX_SOURCE_BYTES:
            return {**base, "backend_version": version, "status": "UNKNOWN",
                    "reason": "candidate exceeds the source-size limit"}
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms < 1:
            return {**base, "backend_version": version, "status": "UNKNOWN",
                    "reason": "synthesis deadline expired before independent equivalence proof"}
        proof = check_function_rewrite(
            source, function, replacement,
            ResourceLimits(timeout_ms=remaining_ms, max_source_bytes=MAX_SOURCE_BYTES,
                           max_ast_nodes=MAX_AST_NODES),
        )
        proof_json = proof.to_json()
        if proof.status != "PROVED":
            return {**base, "backend_version": version, "status": "UNKNOWN",
                    "proof": proof_json,
                    "reason": f"candidate failed independent proof with status {proof.status}"}
        return {**base, "backend_version": version, "status": "SYNTHESIZED",
                "candidate": replacement, "proof": proof_json}
    except _UnsupportedModel as exc:
        return {**base, "backend_version": version, "status": "UNKNOWN", "reason": str(exc)}
    except Exception as exc:
        return {**base, "backend_version": version, "status": "UNKNOWN",
                "reason": f"synthesis or proof backend could not decide: {type(exc).__name__}: {exc}"}


def _worker() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            raise ValueError("worker request exceeds the input limit")
        request = json.loads(raw)
        if (not isinstance(request, dict) or set(request) != {"source", "function", "timeout_ms"}
                or not isinstance(request["source"], str) or not isinstance(request["function"], str)
                or type(request["timeout_ms"]) is not int
                or not 1 <= request["timeout_ms"] <= MAX_TIMEOUT_MS):
            raise ValueError("worker request has invalid fields")
        result = _synthesize(request["source"], request["function"], request["timeout_ms"])
    except Exception as exc:
        result = {"status": "UNSUPPORTED", "reason": f"invalid synthesis request: {exc}"}
    output = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    if len(output.encode("utf-8")) > MAX_WORKER_OUTPUT_BYTES:
        output = json.dumps({"status": "UNKNOWN", "reason": "worker result exceeds the output limit"})
    sys.stdout.write(output)
    return 0


def synthesize_function(root: Path, path: str, function: str, profile: str = PROFILE,
                        timeout_ms: int = 2_000) -> dict[str, Any]:
    """Synthesize a grammar-bounded function and independently prove equivalence."""
    from . import _source
    from .expressions import MODEL_SCOPE

    if profile != PROFILE:
        raise ValueError(f"unsupported synthesis profile {profile!r}; expected {PROFILE!r}")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be between 1 and {MAX_TIMEOUT_MS}")
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty repository-relative path")
    if not isinstance(function, str) or not function:
        raise ValueError("function must be a non-empty function name")
    source = _source(Path(root), path)
    request = json.dumps(
        {"source": source, "function": function, "timeout_ms": timeout_ms},
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(request.encode("utf-8")) > MAX_WORKER_INPUT_BYTES:
        raise ValueError(f"serialized synthesis request must be at most {MAX_WORKER_INPUT_BYTES} bytes")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c",
             "from vkit.operations.synthesis import _worker; raise SystemExit(_worker())"],
            input=request, capture_output=True, encoding="utf-8", timeout=timeout_ms / 1000,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result = {"status": "UNKNOWN", "reason": "isolated synthesis worker exceeded its deadline"}
    except OSError as exc:
        result = {"status": "UNAVAILABLE", "reason": f"cannot start isolated synthesis worker: {exc}"}
    else:
        if completed.returncode != 0:
            result = {"status": "UNAVAILABLE", "reason": "isolated synthesis worker exited without a result"}
        else:
            try:
                result = json.loads(completed.stdout)
                if not isinstance(result, dict) or result.get("status") not in (
                    "SYNTHESIZED", "NO_SOLUTION", "UNSUPPORTED", "UNKNOWN", "UNAVAILABLE",
                ):
                    raise ValueError("invalid worker result")
            except (ValueError, json.JSONDecodeError):
                result = {"status": "UNAVAILABLE", "reason": "isolated synthesis worker returned invalid JSON"}
    return {
        "operation": "synthesize_function", "path": path, "function": function,
        "profile": PROFILE, "grammar": GRAMMAR, "model_scope": MODEL_SCOPE,
        "backend": BACKEND, "backend_version": None,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "candidate": None, "proof": None, "minimality": "not_claimed",
        **result,
    }


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker())

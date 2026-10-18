"""Bound exact-operation rounding error with the pinned Gappa engine."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .. import proc
from ._data import read_json
from ._runtime import pinned_binary

MAX_MODEL_BYTES = 65_536
MAX_SCRIPT_BYTES = 65_536
MAX_OUTPUT_BYTES = 256_000
MAX_TIMEOUT_SECONDS = 60
MAX_NODES = 128
MAX_VARIABLES = 8
MAX_DEPTH = 32
MAX_INTEGER_BITS = 2_048
GAPPA_VERSION = "1.8.3"
GAPPA_SOURCE_SHA256 = "9b3e14e95a5b6aa0ee909b1d1e5c1fac04042024f29b66377cfb9e1923b25f5c"
GAPPA_BIN_ENV = "VKIT_GAPPA_BIN"
GAPPA_SHA_ENV = "VKIT_GAPPA_SHA256"
MODEL_SCOPE = (
    "The supplied finite arithmetic DAG over exact rational input intervals. Every add, sub, "
    "mul, div, and explicit round node uses IEEE-754 binary32 or binary64 round-to-nearest, "
    "ties-to-even, with gradual subnormals. The proof excludes overflow and division by zero. "
    "It does not establish source/compiler correspondence, FMA contraction, alternate rounding "
    "modes, flush-to-zero behavior, NaN/infinity behavior, or runtime performance."
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,47}\Z")
_FORMATS = {
    "binary32": ("ieee_32", "16777215b104", 24),
    "binary64": ("ieee_64", "9007199254740991b971", 53),
}
_OPS = {"add", "sub", "mul", "div"}


@dataclass(frozen=True)
class _Variable:
    name: str
    lower: Fraction
    upper: Fraction


@dataclass(frozen=True)
class _Node:
    name: str
    op: str
    args: tuple[str, ...] = ()
    value: Fraction | None = None
    variable: str | None = None
    depth: int = 0


@dataclass(frozen=True)
class _Model:
    variables: tuple[_Variable, ...]
    nodes: tuple[_Node, ...]
    output: str
    budget: Fraction
    request: dict[str, Any]


def analyze_roundoff(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Prove an absolute round-off budget or find a feasible uniform format in a finite grid.

    A failed Gappa obligation is inconclusive; it is never reported as a numerical
    counterexample. Results apply only to the closed model in the supplied JSON file.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    base: dict[str, Any] = {
        "operation": "analyze_roundoff",
        "model_scope": MODEL_SCOPE,
        "engine": f"Gappa {GAPPA_VERSION}",
        "engine_source_sha256": GAPPA_SOURCE_SHA256,
        "proof_trust": "Gappa trusted-engine result; no Rocq/Coq kernel check was performed",
    }
    try:
        raw, input_sha = read_json(root, path, max_bytes=MAX_MODEL_BYTES, reject_symlinks=True)
        model = _parse_model(raw)
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        return {**base, "path": path, "status": "UNSUPPORTED", "detail": str(exc)}

    common = {**base, "path": path, "input_sha256": input_sha,
              "error_budget": _fraction_json(model.budget)}
    runtime = pinned_binary(GAPPA_BIN_ENV, GAPPA_SHA_ENV)
    if runtime is None:
        return {**common, "status": "UNAVAILABLE",
                "detail": f"configure {GAPPA_BIN_ENV} and {GAPPA_SHA_ENV} for the pinned Gappa binary"}
    binary, binary_sha = runtime
    common["engine_binary_sha256"] = binary_sha
    deadline = time.monotonic() + timeout_seconds

    with tempfile.TemporaryDirectory(prefix="vkit-roundoff-") as temporary:
        directory = Path(temporary)
        version_run = proc.run(
            [str(binary), "--version"], cwd=directory,
            stdout_path=directory / "version.out", stderr_path=directory / "version.err",
            timeout_seconds=max(0.05, deadline - time.monotonic()),
        )
        if version_run.timed_out:
            return {**common, "status": "UNKNOWN", "detail": "Gappa version check timed out"}
        if version_run.launch_error:
            return {**common, "status": "UNAVAILABLE", "detail": "the configured Gappa binary could not start"}
        version_text = _read_output(directory / "version.out")
        version_err = _read_output(directory / "version.err")
        if version_run.code != 0 or version_text != f"Gappa {GAPPA_VERSION}\n" or version_err:
            return {**common, "status": "UNAVAILABLE",
                    "detail": "the configured binary is not the pinned Gappa release"}

        attempts: list[dict[str, Any]] = []
        formats = _formats_for(model.request)
        for format_name in formats:
            script = _script(model, format_name)
            if len(script.encode("ascii")) > MAX_SCRIPT_BYTES:
                attempts.append({"format": format_name, "status": "UNKNOWN",
                                 "detail": "generated Gappa obligation exceeds the byte limit"})
                continue
            obligation_sha = hashlib.sha256(script.encode("ascii")).hexdigest()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append({"format": format_name, "status": "UNKNOWN",
                                 "detail": "analysis exceeded the time budget"})
                break
            source = directory / f"roundoff-{format_name}.g"
            source.write_text(script, encoding="ascii", newline="\n")
            result = proc.run(
                [str(binary), "-Bnull", str(source)], cwd=directory,
                stdout_path=directory / "gappa.out", stderr_path=directory / "gappa.err",
                timeout_seconds=remaining,
            )
            stdout = _read_output(directory / "gappa.out")
            stderr = _read_output(directory / "gappa.err")
            item = {"format": format_name, "obligation_sha256": obligation_sha,
                    "obligation": script}
            if result.timed_out or time.monotonic() > deadline:
                attempts.append({**item, "status": "UNKNOWN", "detail": "Gappa proof timed out"})
                break
            if result.launch_error:
                return {**common, "status": "UNAVAILABLE", "attempts": attempts,
                        "detail": "the pinned Gappa binary could not start"}
            if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES or len(stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
                attempts.append({**item, "status": "UNKNOWN", "detail": "Gappa output exceeded the byte limit"})
                continue
            if result.code == 0 and not stdout and not stderr:
                attempts.append({**item, "status": "PROVED_BY_GAPPA"})
                if model.request["kind"] == "bound":
                    return {**common, "status": "PROVED_BY_GAPPA", "format": format_name,
                            "absolute_error_bound": _fraction_json(model.budget),
                            "obligation_sha256": obligation_sha, "obligation": script}
                return {**common, "status": "CERTIFIED_FEASIBLE_FORMAT", "format": format_name,
                        "minimality_claim": None, "attempts": attempts}
            attempts.append({**item, "status": "UNKNOWN",
                             "detail": "Gappa did not prove every generated obligation",
                             "engine_exit_code": result.code})

        return {**common, "status": "UNKNOWN", "attempts": attempts,
                "detail": "no requested format had every round-off obligation proved"}


def _parse_model(raw: Any) -> _Model:
    if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "request", "variables", "nodes", "output", "error_budget"}:
        raise ValueError("model needs exactly schema_version, request, variables, nodes, output and error_budget")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    request = raw["request"]
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    if request.get("kind") == "bound":
        if set(request) != {"kind", "format"} or request["format"] not in _FORMATS:
            raise ValueError("bound request needs a supported binary32 or binary64 format")
    elif request.get("kind") == "uniform_precision":
        if (set(request) != {"kind", "formats"} or not isinstance(request["formats"], list)
                or not 1 <= len(request["formats"]) <= len(_FORMATS)
                or any(item not in _FORMATS for item in request["formats"])
                or len(set(request["formats"])) != len(request["formats"])):
            raise ValueError("uniform_precision request needs a nonempty list of distinct supported formats")
    else:
        raise ValueError("request.kind must be bound or uniform_precision")

    raw_variables = raw["variables"]
    if (not isinstance(raw_variables, list) or len(raw_variables) > MAX_VARIABLES):
        raise ValueError(f"variables must be a list of at most {MAX_VARIABLES} entries")
    variables: list[_Variable] = []
    variable_names: set[str] = set()
    for item in raw_variables:
        if not isinstance(item, dict) or set(item) != {"name", "lower", "upper"}:
            raise ValueError("each variable needs exactly name, lower and upper")
        name = item["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in variable_names:
            raise ValueError("variable names must be distinct ASCII identifiers")
        lower, upper = _rational(item["lower"], "lower endpoint"), _rational(item["upper"], "upper endpoint")
        if lower > upper:
            raise ValueError(f"variable {name!r} has an inverted interval")
        variables.append(_Variable(name, lower, upper))
        variable_names.add(name)

    raw_nodes = raw["nodes"]
    if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= MAX_NODES:
        raise ValueError(f"nodes must be a list with 1 to {MAX_NODES} entries")
    nodes: list[_Node] = []
    by_name: dict[str, _Node] = {}
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise ValueError("each node must be an object")
        name = item.get("id")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in by_name:
            raise ValueError("node ids must be distinct ASCII identifiers")
        op = item.get("op")
        if op == "var":
            if set(item) != {"id", "op", "name"} or item["name"] not in variable_names:
                raise ValueError("var node must refer to a declared variable")
            node = _Node(name, op, variable=item["name"])
        elif op == "rational":
            if set(item) != {"id", "op", "value"}:
                raise ValueError("rational node needs exactly id, op and value")
            node = _Node(name, op, value=_rational(item["value"], "rational constant"))
        elif op == "round":
            if set(item) != {"id", "op", "arg"}:
                raise ValueError("round node needs exactly id, op and arg")
            args = _references([item["arg"]], by_name)
            node = _Node(name, op, args=args, depth=1 + by_name[args[0]].depth)
        elif op in _OPS:
            if set(item) != {"id", "op", "args"} or not isinstance(item["args"], list) or len(item["args"]) != 2:
                raise ValueError(f"{op} node needs exactly two arguments")
            args = _references(item["args"], by_name)
            node = _Node(name, op, args=args, depth=1 + max(by_name[arg].depth for arg in args))
        else:
            raise ValueError(f"unsupported node operator {op!r}")
        if node.depth > MAX_DEPTH:
            raise ValueError(f"node graph exceeds depth {MAX_DEPTH}")
        nodes.append(node)
        by_name[name] = node

    output = raw["output"]
    if not isinstance(output, str) or output not in by_name:
        raise ValueError("output must identify a declared node")
    needed: set[str] = set()
    pending = [output]
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending.extend(by_name[name].args)
    if needed != set(by_name):
        raise ValueError("every node must contribute to the declared output")
    budget = _rational(raw["error_budget"], "error_budget", nonnegative=True)
    return _Model(tuple(variables), tuple(nodes), output, budget, request)


def _references(values: list[Any], previous: dict[str, _Node]) -> tuple[str, ...]:
    if any(not isinstance(value, str) or value not in previous for value in values):
        raise ValueError("node arguments must refer only to earlier nodes")
    return tuple(values)


def _rational(value: Any, label: str, *, nonnegative: bool = False) -> Fraction:
    if (not isinstance(value, list) or len(value) != 2
            or any(type(part) is not int or part.bit_length() > MAX_INTEGER_BITS for part in value)
            or value[1] == 0):
        raise ValueError(f"{label} must be a pair of bounded integers with nonzero denominator")
    result = Fraction(value[0], value[1])
    if nonnegative and result < 0:
        raise ValueError("error_budget must be nonnegative")
    return result


def _formats_for(request: dict[str, Any]) -> tuple[str, ...]:
    if request["kind"] == "bound":
        return (request["format"],)
    return tuple(sorted(request["formats"], key=lambda name: _FORMATS[name][2]))


def _script(model: _Model, format_name: str) -> str:
    float_format, maximum, _ = _FORMATS[format_name]
    lines = [f"@rnd = float<{float_format}, ne>;"]
    input_names = {item.name: f"v{index}" for index, item in enumerate(model.variables)}
    rounded: dict[str, str] = {}
    exact: dict[str, str] = {}
    goals: list[str] = []

    for index, node in enumerate(model.nodes):
        machine_name, real_name = f"f{index}", f"r{index}"
        rounded[node.name], exact[node.name] = machine_name, real_name
        if node.op == "var":
            lines.append(f"{machine_name} = {input_names[node.variable]};")
            lines.append(f"{real_name} = {input_names[node.variable]};")
        elif node.op == "rational":
            literal = _literal(node.value)
            lines.append(f"{machine_name} = {literal};")
            lines.append(f"{real_name} = {literal};")
        elif node.op == "round":
            source = rounded[node.args[0]]
            raw_name = f"raw{index}"
            lines.append(f"{raw_name} = {source};")
            lines.append(f"{machine_name} = rnd({source});")
            lines.append(f"{real_name} = {exact[node.args[0]]};")
            goals.append(f"{raw_name} in [-{maximum}, {maximum}]")
        else:
            left, right = (rounded[arg] for arg in node.args)
            exact_left, exact_right = (exact[arg] for arg in node.args)
            operator = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[node.op]
            raw_name = f"raw{index}"
            lines.append(f"{raw_name} = {left} {operator} {right};")
            lines.append(f"{machine_name} rnd= {left} {operator} {right};")
            lines.append(f"{real_name} = {exact_left} {operator} {exact_right};")
            goals.append(f"{raw_name} in [-{maximum}, {maximum}]")
            if node.op == "div":
                goals.append(f"not {right} in [0, 0]")
                goals.append(f"not {exact_right} in [0, 0]")

    assumptions: list[str] = []
    for index, variable in enumerate(model.variables):
        machine_name = input_names[variable.name]
        assumptions.extend(_interval_predicates(machine_name, variable.lower, variable.upper))
    if not assumptions:
        assumptions.append("1 = 1")
    error = f"({rounded[model.output]} - {exact[model.output]})"
    budget_n, budget_d = model.budget.numerator, model.budget.denominator
    goals.extend([f"({error}) * {budget_d} <= {budget_n}",
                  f"-(({error}) * {budget_d}) <= {budget_n}"])
    lines.extend(["{", "  (" + " /\\ ".join(assumptions) + ") ->",
                  "  (" + " /\\ ".join(goals) + ")", "}"])
    return "\n".join(lines) + "\n"


def _interval_predicates(name: str, lower: Fraction, upper: Fraction) -> list[str]:
    low_n, low_d = lower.numerator, lower.denominator
    high_n, high_d = upper.numerator, upper.denominator
    return [f"{name} * {low_d} >= {low_n}", f"{name} * {high_d} <= {high_n}"]


def _literal(value: Fraction | None) -> str:
    assert value is not None
    if value.denominator == 1:
        return str(value.numerator)
    return f"({value.numerator} / {value.denominator})"


def _fraction_json(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


def _read_output(path: Path) -> str:
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_OUTPUT_BYTES + 1)
        if len(raw) > MAX_OUTPUT_BYTES:
            return "\0OUTPUT_TOO_LARGE"
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return "\0OUTPUT_UNREADABLE"

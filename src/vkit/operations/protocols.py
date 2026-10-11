"""Exhaustively check a closed finite transition system with the pinned TLC runner."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..verifiers.obligation import PropertyObligation
from ..verifiers.spec import (
    DomainBounds,
    FingerprintSpec,
    ModuleRef,
    TlcCheck,
    ToolchainRef,
)
from ..verifiers.tlc_adapter import AdapterResult, TlcRun, _read, interpret
from ..verifiers.tlc_runner import _digest
from ._data import read_json

MAX_MODEL_BYTES = 65_536
MAX_VARIABLES = 12
MAX_DOMAIN_VALUES = 16
MAX_TRANSITIONS = 32
MAX_INVARIANTS = 32
MAX_EXPRESSION_NODES = 256
MAX_EXPRESSION_DEPTH = 32
MAX_MODEL_STATES = 1_000_000
MAX_TIMEOUT_SECONDS = 60
MIN_INTEGER = -(2**63)
MAX_INTEGER = 2**63 - 1
TLC_VERSION = "2.19"
TLC_JAR_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_STRING = re.compile(r"[A-Za-z0-9_.:/-]{1,64}\Z")
_VALUE = re.compile(r'(-?[0-9]+|TRUE|FALSE|"[A-Za-z0-9_.:/-]{1,64}")\Z')
_PROTOCOL = "VKIT_TLA2TOOLS_JAR"


@dataclass(frozen=True)
class _Expr:
    op: str
    kind: str
    value: Any = None
    args: tuple[_Expr, ...] = ()


@dataclass(frozen=True)
class _Variable:
    name: str
    index: int
    kind: str
    domain: tuple[Any, ...]
    initial: tuple[Any, ...]
    durability: str
    restart: Any = None


@dataclass(frozen=True)
class _Transition:
    name: str
    guard: _Expr
    updates: tuple[tuple[int, _Expr], ...]


@dataclass(frozen=True)
class _Invariant:
    name: str
    expression: _Expr
    generated: str


@dataclass(frozen=True)
class _Model:
    variables: tuple[_Variable, ...]
    transitions: tuple[_Transition, ...]
    invariants: tuple[_Invariant, ...]
    crash_recovery: bool
    check_deadlock: bool
    digest: str


class _ExpressionBudget:
    def __init__(self) -> None:
        self.nodes = 0


def check_protocol_model(root: Path, path: str,
                         timeout_seconds: int = 10) -> dict[str, Any]:
    """Check safety and optional progress properties in a finite, user-declared model.

    This operation establishes properties only about the supplied transition system. It does
    not execute, inspect, or establish behavior of an application implementation.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    raw, digest = read_json(root, path, max_bytes=MAX_MODEL_BYTES, reject_symlinks=True)
    model = _parse_model(raw, digest)
    base: dict[str, Any] = {
        "operation": "check_protocol_model",
        "path": path,
        "model_sha256": digest,
        "semantics": "closed finite transition system",
        "claim_scope": "supplied model only",
        "engine": f"TLC {TLC_VERSION}",
        "engine_sha256": TLC_JAR_SHA256,
        "deadlock_checked": model.check_deadlock,
        "crash_recovery_modeled": model.crash_recovery,
    }
    jar_text = os.environ.get(_PROTOCOL, "")
    if not jar_text:
        return {**base, "status": "UNAVAILABLE",
                "detail": f"set {_PROTOCOL} to the pinned TLC {TLC_VERSION} jar"}
    jar = Path(jar_text).expanduser()
    try:
        jar = jar.resolve(strict=True)
        actual_digest = _digest(jar)
    except (OSError, RuntimeError, ValueError):
        return {**base, "status": "UNAVAILABLE", "detail": "the configured TLC jar is unreadable"}
    if not jar.is_file() or actual_digest != TLC_JAR_SHA256:
        return {**base, "status": "UNAVAILABLE",
                "detail": "the configured TLC jar does not match the pinned SHA-256"}

    with tempfile.TemporaryDirectory(prefix="vkit-protocol-") as temporary:
        directory = Path(temporary)
        (directory / "Protocol.tla").write_text(_module_text(model), encoding="utf-8")
        (directory / "Protocol.cfg").write_text(_config_text(model), encoding="utf-8")
        report_path = directory / "tlc-report.json"
        argv = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "verifiers" / "tlc_runner.py"),
            "--java", "java",
            "--jar", str(jar),
            "--jar-sha256", TLC_JAR_SHA256,
            "--expected-version", TLC_VERSION,
            "--model", "Protocol",
            "--model-path", "Protocol.tla",
            "--config", "Protocol.cfg",
            "--root", str(directory),
            "--constants-from-config", "true",
            "--checksum-states", "false",
            "--workers", "1",
            "--metadir", str(directory / "tlc-states"),
            "--report", str(report_path),
        ]
        try:
            process = _run_tree(argv, timeout_seconds)
        except subprocess.TimeoutExpired:
            return {**base, "status": "UNKNOWN",
                    "detail": f"TLC did not finish within {timeout_seconds} seconds"}
        except OSError:
            return {**base, "status": "UNAVAILABLE",
                    "detail": "the pinned TLC runner could not be started"}
        if not report_path.is_file():
            return {**base, "status": "UNKNOWN",
                    "detail": "the TLC runner returned no validated report",
                    "runner_exit_code": process.returncode}
        report = report_path.read_bytes()
        check = _check_spec(directory, model)
        result = interpret(report, check)
        if not isinstance(result, AdapterResult):
            initial_failure = _initial_counterexample(report, result.detail, model, check)
            if initial_failure is not None:
                failed_name, trace = initial_failure
                if _replay_counterexample(model, trace, failed_name):
                    return {**base, "status": "COUNTEREXAMPLE",
                            "kind": _failure_kind(model, failed_name),
                            "property": _property_label(model, failed_name),
                            "trace": trace, "states": 1, "depth": 1}
            return {**base, "status": "UNKNOWN", "detail": result.detail,
                    "runner_exit_code": process.returncode}
        if result.counterexamples:
            failed, rendered_trace = result.counterexamples[0]
            trace = _parse_trace(rendered_trace, model)
            if trace is None or not _replay_counterexample(model, trace, failed.property_name):
                return {**base, "status": "UNKNOWN",
                        "detail": "TLC's counterexample failed independent replay",
                        "property": failed.property_name}
            kind = _failure_kind(model, failed.property_name)
            return {**base, "status": "COUNTEREXAMPLE", "kind": kind,
                    "property": _property_label(model, failed.property_name),
                    "trace": trace,
                    "states": result.exploration.distinct,
                    "depth": result.exploration.depth}
        expected = {item.property_name for item in check.properties}
        observed = {item.property_name for item in result.observations}
        if expected != observed or not result.exploration.exhaustive:
            return {**base, "status": "UNKNOWN",
                    "detail": "TLC did not establish every requested property exhaustively"}
        return {**base, "status": "EXHAUSTIVE",
                "properties": [_property_label(model, name) for name in sorted(expected)],
                "states": result.exploration.distinct,
                "generated": result.exploration.generated,
                "depth": result.exploration.depth}


def _parse_model(raw: Any, digest: str) -> _Model:
    document = _fields(raw, {"schema_version", "variables", "transitions", "invariants",
                             "crash_recovery", "check_deadlock"}, "model")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("model.schema_version must be 1")
    if type(document["crash_recovery"]) is not bool or type(document["check_deadlock"]) is not bool:
        raise ValueError("model.crash_recovery and model.check_deadlock must be booleans")
    raw_variables = document["variables"]
    if not isinstance(raw_variables, dict) or not 1 <= len(raw_variables) <= MAX_VARIABLES:
        raise ValueError(f"model.variables must contain 1 to {MAX_VARIABLES} variables")
    variables: list[_Variable] = []
    name_to_index: dict[str, int] = {}
    state_count = 1
    for index, (name, raw_variable) in enumerate(raw_variables.items()):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"variable name {name!r} must be an ASCII identifier")
        label = f"model.variables[{name!r}]"
        variable = _fields(raw_variable, {"domain", "initial", "durability"}, label,
                           optional={"restart"})
        durability = variable["durability"]
        if durability not in ("durable", "volatile"):
            raise ValueError(f"{label}.durability must be 'durable' or 'volatile'")
        expected = {"domain", "initial", "durability"} | ({"restart"} if durability == "volatile" else set())
        if set(variable) != expected:
            raise ValueError(f"{label} fields must be {sorted(expected)}")
        domain = _scalar_list(variable["domain"], f"{label}.domain")
        initial = _scalar_list(variable["initial"], f"{label}.initial")
        if len(domain) > MAX_DOMAIN_VALUES or not initial:
            raise ValueError(f"{label}.domain and initial must be nonempty, with at most {MAX_DOMAIN_VALUES} domain values")
        kind = _scalar_kind(domain[0])
        if any(_scalar_kind(value) != kind for value in domain + initial):
            raise ValueError(f"{label}.domain and initial must use one scalar type")
        if not _subset(initial, domain):
            raise ValueError(f"{label}.initial must be a subset of {label}.domain")
        restart = variable.get("restart")
        if durability == "volatile":
            if _scalar_kind(restart) != kind or not _contains(domain, restart):
                raise ValueError(f"{label}.restart must belong to {label}.domain")
        state_count *= len(domain)
        name_to_index[name] = index
        variables.append(_Variable(name, index, kind, domain, initial, durability, restart))
    if document["crash_recovery"]:
        state_count *= 2
    if state_count > MAX_MODEL_STATES:
        raise ValueError(f"model state-domain product exceeds {MAX_MODEL_STATES}")

    raw_transitions = document["transitions"]
    if not isinstance(raw_transitions, list) or len(raw_transitions) > MAX_TRANSITIONS:
        raise ValueError(f"model.transitions must be an array with at most {MAX_TRANSITIONS} entries")
    transitions: list[_Transition] = []
    transition_names: set[str] = set()
    for index, raw_transition in enumerate(raw_transitions):
        label = f"model.transitions[{index}]"
        item = _fields(raw_transition, {"name", "guard", "updates"}, label)
        name = item["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in transition_names:
            raise ValueError(f"{label}.name must be a unique ASCII identifier")
        transition_names.add(name)
        budget = _ExpressionBudget()
        guard = _expression(item["guard"], name_to_index, variables, budget, 0)
        if guard.kind != "bool":
            raise ValueError(f"{label}.guard must be boolean")
        raw_updates = item["updates"]
        if not isinstance(raw_updates, dict) or not raw_updates:
            raise ValueError(f"{label}.updates must be a nonempty object")
        updates: list[tuple[int, _Expr]] = []
        for variable_name, raw_expression in raw_updates.items():
            if variable_name not in name_to_index:
                raise ValueError(f"{label}.updates names unknown variable {variable_name!r}")
            variable_index = name_to_index[variable_name]
            expression = _expression(raw_expression, name_to_index, variables, budget, 0)
            if expression.kind != variables[variable_index].kind:
                raise ValueError(f"{label}.updates[{variable_name!r}] has the wrong scalar type")
            updates.append((variable_index, expression))
        transitions.append(_Transition(name, guard, tuple(sorted(updates))))

    raw_invariants = document["invariants"]
    if not isinstance(raw_invariants, list) or len(raw_invariants) > MAX_INVARIANTS:
        raise ValueError(f"model.invariants must be an array with at most {MAX_INVARIANTS} entries")
    invariants: list[_Invariant] = []
    invariant_names: set[str] = set()
    for index, raw_invariant in enumerate(raw_invariants):
        label = f"model.invariants[{index}]"
        item = _fields(raw_invariant, {"name", "expression"}, label)
        name = item["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in invariant_names:
            raise ValueError(f"{label}.name must be a unique ASCII identifier")
        invariant_names.add(name)
        expression = _expression(item["expression"], name_to_index, variables,
                                 _ExpressionBudget(), 0)
        if expression.kind != "bool":
            raise ValueError(f"{label}.expression must be boolean")
        invariants.append(_Invariant(name, expression, f"Inv_{index}"))
    return _Model(tuple(variables), tuple(transitions), tuple(invariants),
                  document["crash_recovery"], document["check_deadlock"], digest)


def _expression(raw: Any, variables_by_name: dict[str, int], variables: list[_Variable],
                budget: _ExpressionBudget, depth: int) -> _Expr:
    budget.nodes += 1
    if budget.nodes > MAX_EXPRESSION_NODES or depth > MAX_EXPRESSION_DEPTH:
        raise ValueError("expression exceeds its node or nesting limit")
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError("each expression must be a one-key literal, variable, or operator object")
    op, value = next(iter(raw.items()))
    if op == "var":
        if not isinstance(value, str) or value not in variables_by_name:
            raise ValueError(f"expression names unknown variable {value!r}")
        index = variables_by_name[value]
        return _Expr("var", variables[index].kind, index)
    if op in ("int", "bool", "string"):
        kind = {"int": "int", "bool": "bool", "string": "string"}[op]
        if _scalar_kind(value) != kind:
            raise ValueError(f"{op} expression has the wrong JSON scalar type")
        return _Expr(op, kind, value)
    if op not in {"not", "add", "sub", "mul", "eq", "ne", "lt", "le", "gt",
                  "ge", "and", "or"}:
        raise ValueError(f"expression operator {op!r} is unsupported")
    if op == "not":
        argument = _expression(value, variables_by_name, variables, budget, depth + 1)
        if argument.kind != "bool":
            raise ValueError("not requires a boolean operand")
        return _Expr(op, "bool", args=(argument,))
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{op} requires an array of two expressions")
    left = _expression(value[0], variables_by_name, variables, budget, depth + 1)
    right = _expression(value[1], variables_by_name, variables, budget, depth + 1)
    if op in {"add", "sub", "mul"}:
        if left.kind != "int" or right.kind != "int":
            raise ValueError(f"{op} requires integer operands")
        kind = "int"
    elif op in {"and", "or"}:
        if left.kind != "bool" or right.kind != "bool":
            raise ValueError(f"{op} requires boolean operands")
        kind = "bool"
    elif op in {"lt", "le", "gt", "ge"}:
        if left.kind != "int" or right.kind != "int":
            raise ValueError(f"{op} requires integer operands")
        kind = "bool"
    else:
        if left.kind != right.kind:
            raise ValueError(f"{op} requires operands of the same type")
        kind = "bool"
    return _Expr(op, kind, args=(left, right))


def _fields(value: Any, required: set[str], label: str,
            optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    allowed = required | (optional or set())
    missing = required - set(value)
    extra = set(value) - allowed
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unknown " + ", ".join(sorted(extra)))
        raise ValueError(f"{label} has " + " and ".join(detail))
    return value


def _scalar_kind(value: Any) -> str:
    if type(value) is int:
        if not MIN_INTEGER <= value <= MAX_INTEGER:
            raise ValueError("finite integer values must fit signed 64-bit range")
        return "int"
    if type(value) is bool:
        return "bool"
    if isinstance(value, str) and _STRING.fullmatch(value):
        return "string"
    raise ValueError("finite values must be integers, booleans, or short safe ASCII strings")


def _scalar_list(value: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a nonempty array")
    result = tuple(value)
    if len({_scalar_key(item) for item in result}) != len(result):
        raise ValueError(f"{label} must not contain duplicate values")
    for item in result:
        _scalar_kind(item)
    return result


def _scalar_key(value: Any) -> tuple[type, Any]:
    return type(value), value


def _contains(values: tuple[Any, ...], value: Any) -> bool:
    return _scalar_key(value) in {_scalar_key(item) for item in values}


def _subset(values: tuple[Any, ...], domain: tuple[Any, ...]) -> bool:
    domain_keys = {_scalar_key(item) for item in domain}
    return all(_scalar_key(item) in domain_keys for item in values)


def _module_text(model: _Model) -> str:
    names = [f"v{variable.index}" for variable in model.variables]
    if model.crash_recovery:
        names.append("running")
    variable_tuple = ", ".join(names)
    lines = ["---- MODULE Protocol ----", "EXTENDS Integers", "", f"VARIABLES {variable_tuple}",
             f"vars == <<{variable_tuple}>>", ""]
    for variable in model.variables:
        name = f"v{variable.index}"
        values = ", ".join(_tlc_value(value) for value in variable.domain)
        initial = ", ".join(_tlc_value(value) for value in variable.initial)
        lines.extend((f"Domain_{name} == {{{values}}}",
                      f"Init_{name} == {name} \\in {{{initial}}}"))
    domain_parts = [f"v{v.index} \\in Domain_v{v.index}" for v in model.variables]
    lines.extend(("", "DomainOK == " + _and(domain_parts), "", "Init == " +
                  _and([f"Init_v{v.index}" for v in model.variables] +
                       (["running = TRUE"] if model.crash_recovery else []))))
    for index, transition in enumerate(model.transitions):
        updates = dict(transition.updates)
        parts = ["running = TRUE"] if model.crash_recovery else []
        parts.append(_emit(transition.guard))
        for variable in model.variables:
            expression = updates.get(variable.index)
            if expression is None:
                parts.append(f"UNCHANGED v{variable.index}")
            else:
                parts.append(f"v{variable.index}' = {_emit(expression)}")
        if model.crash_recovery:
            parts.append("UNCHANGED running")
        lines.extend(("", f"Step{index} == " + _and(parts)))
    if model.crash_recovery:
        preserve = [f"UNCHANGED v{v.index}" for v in model.variables]
        restart_values = [
            f"UNCHANGED v{v.index}" if v.durability == "durable"
            else f"v{v.index}' = {_tlc_value(v.restart)}"
            for v in model.variables
        ]
        lines.extend(("", "Crash == " + _and(["running = TRUE", "running' = FALSE"] + preserve),
                      "", "Restart == " + _and(["running = FALSE", "running' = TRUE"] + restart_values)))
    user_steps = [f"Step{index}" for index in range(len(model.transitions))]
    enabled_action = " \\/ ".join(user_steps) if user_steps else "FALSE"
    if model.crash_recovery:
        next_action = " \\/ ".join(user_steps + ["Crash", "Restart"])
        progress = f"(running = FALSE) \\/ ENABLED ({enabled_action})"
    else:
        next_action = enabled_action
        progress = f"ENABLED ({enabled_action})"
    lines.extend(("", f"UserNext == {enabled_action}", f"Next == {next_action}", ""))
    for index, invariant in enumerate(model.invariants):
        lines.append(f"{invariant.generated} == {_emit(invariant.expression)}")
    lines.extend(("", "Inv_Domain == DomainOK"))
    if model.check_deadlock:
        lines.extend(("", f"Inv_Deadlock == {progress}"))
    lines.append("====")
    return "\n".join(lines) + "\n"


def _config_text(model: _Model) -> str:
    properties = ["Inv_Domain"] + [invariant.generated for invariant in model.invariants]
    if model.check_deadlock:
        properties.append("Inv_Deadlock")
    return "\n".join(("INIT Init", "NEXT Next", "CHECK_DEADLOCK FALSE", "INVARIANTS",
                       *(f"  {name}" for name in properties), ""))


def _and(parts: list[str]) -> str:
    if not parts:
        return "TRUE"
    return " /\\ ".join(f"({part})" for part in parts)


def _emit(expression: _Expr) -> str:
    if expression.op == "var":
        return f"v{expression.value}"
    if expression.op in {"int", "bool", "string"}:
        return _tlc_value(expression.value)
    if expression.op == "not":
        return f"~({_emit(expression.args[0])})"
    left, right = (_emit(argument) for argument in expression.args)
    operators = {"add": "+", "sub": "-", "mul": "*", "eq": "=", "ne": "/=",
                 "lt": "<", "le": "<=", "gt": ">", "ge": ">=", "and": "/\\", "or": "\\/"}
    return f"({left} {operators[expression.op]} {right})"


def _tlc_value(value: Any) -> str:
    if type(value) is bool:
        return "TRUE" if value else "FALSE"
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and _STRING.fullmatch(value):
        return f'"{value}"'
    raise ValueError("unsupported finite scalar")


def _check_spec(directory: Path, model: _Model) -> TlcCheck:
    properties = [PropertyObligation("Inv_Domain")]
    properties.extend(PropertyObligation(item.generated) for item in model.invariants)
    if model.check_deadlock:
        properties.append(PropertyObligation("Inv_Deadlock"))
    return TlcCheck(
        id="protocol-model",
        cwd=directory,
        timeout_seconds=MAX_TIMEOUT_SECONDS,
        artifact_name="tlc-report.json",
        model=ModuleRef("Protocol", "Protocol.tla"),
        config="Protocol.cfg",
        properties=tuple(properties),
        bounds=DomainBounds(),
        fingerprint=FingerprintSpec(True, False, 1),
        toolchain=ToolchainRef("tlc", TLC_VERSION, jar_sha256=TLC_JAR_SHA256),
    )


def _run_tree(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    options: dict[str, Any] = {
        "args": argv,
        "cwd": Path(argv[argv.index("--root") + 1]),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(**options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, check=False)
            except OSError:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _parse_trace(rendered: str, model: _Model) -> list[dict[str, Any]] | None:
    states: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in rendered.splitlines():
        if re.match(r"^State [0-9]+:", line):
            if current is not None:
                states.append(current)
            current = {}
            continue
        match = re.match(r"^(?:/\\ )?(v[0-9]+|running) = (.*?)\s*$", line)
        if match and current is not None:
            value = _parse_tlc_value(match.group(2))
            if value is None:
                return None
            current[match.group(1)] = value
    if current is not None:
        states.append(current)
    expected = {f"v{v.index}" for v in model.variables}
    if model.crash_recovery:
        expected.add("running")
    if len(states) < 1 or any(set(state) != expected for state in states):
        return None
    result = []
    for state in states:
        result.append({
            "variables": {variable.name: state[f"v{variable.index}"] for variable in model.variables},
            **({"process_running": state["running"]} if model.crash_recovery else {}),
        })
    return result


def _initial_counterexample(raw: bytes, detail: str, model: _Model,
                            check: TlcCheck) -> tuple[str, list[dict[str, Any]]] | None:
    if not detail.startswith("results_absent:"):
        return None
    report = _read(raw)
    if not isinstance(report, TlcRun):
        return None
    match = re.search(
        r"Error: Invariant ([A-Za-z_][A-Za-z0-9_]*) is violated by the initial state:",
        report.stdout,
    )
    if match is None or match.group(1) not in {item.property_name for item in check.properties}:
        return None
    lines = report.stdout.splitlines()
    start = next((index for index, line in enumerate(lines) if "violated by the initial state:" in line), None)
    end = next((index for index, line in enumerate(lines[start + 1:], start + 1)
                if line.startswith("Finished in ")), None) if start is not None else None
    if start is None or end is None:
        return None
    rendered = "State 1: <Initial predicate>\n" + "\n".join(lines[start + 1:end])
    trace = _parse_trace(rendered, model)
    if trace is None or len(trace) != 1:
        return None
    return match.group(1), trace


def _parse_tlc_value(text: str) -> Any:
    if not _VALUE.fullmatch(text):
        return None
    if text == "TRUE":
        return True
    if text == "FALSE":
        return False
    if text.startswith('"'):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        return None


def _replay_counterexample(model: _Model, trace: list[dict[str, Any]], failed: str) -> bool:
    first = trace[0]["variables"]
    if any(not _contains(variable.initial, first[variable.name])
           for variable in model.variables):
        return False
    if model.crash_recovery and trace[0].get("process_running") is not True:
        return False
    for before_state, after_state in zip(trace, trace[1:]):
        before = before_state["variables"]
        after = after_state["variables"]
        old = [before[variable.name] for variable in model.variables]
        running = before_state.get("process_running", True)
        successors: list[tuple[list[Any], bool]] = []
        if model.crash_recovery and running:
            successors.append((old.copy(), False))
        if model.crash_recovery and not running:
            restarted = [
                old[variable.index] if variable.durability == "durable" else variable.restart
                for variable in model.variables
            ]
            successors.append((restarted, True))
        if running:
            for transition in model.transitions:
                if _eval(transition.guard, old):
                    updated = old.copy()
                    for index, expression in transition.updates:
                        updated[index] = _eval(expression, old)
                    successors.append((updated, running))
        next_values = [after[variable.name] for variable in model.variables]
        if not any(values == next_values and is_running == after_state.get("process_running", True)
                   for values, is_running in successors):
            return False
    final_record = trace[-1]
    final_map = final_record["variables"]
    final = [final_map[variable.name] for variable in model.variables]
    if failed == "Inv_Domain":
        return any(not _contains(variable.domain, final[variable.index])
                   for variable in model.variables)
    if failed == "Inv_Deadlock":
        if model.crash_recovery and not final_record["process_running"]:
            return False
        return not any(_eval(transition.guard, final) for transition in model.transitions)
    invariant = next((item for item in model.invariants if item.generated == failed), None)
    return invariant is not None and not _eval(invariant.expression, final)


def _eval(expression: _Expr, state: list[Any]) -> Any:
    if expression.op == "var":
        return state[expression.value]
    if expression.op in {"int", "bool", "string"}:
        return expression.value
    args = [_eval(argument, state) for argument in expression.args]
    if expression.op == "not":
        return not args[0]
    operators = {
        "add": lambda: args[0] + args[1], "sub": lambda: args[0] - args[1],
        "mul": lambda: args[0] * args[1], "eq": lambda: args[0] == args[1],
        "ne": lambda: args[0] != args[1], "lt": lambda: args[0] < args[1],
        "le": lambda: args[0] <= args[1], "gt": lambda: args[0] > args[1],
        "ge": lambda: args[0] >= args[1], "and": lambda: args[0] and args[1],
        "or": lambda: args[0] or args[1],
    }
    return operators[expression.op]()


def _failure_kind(model: _Model, failed: str) -> str:
    if failed == "Inv_Domain":
        return "DOMAIN_ESCAPE"
    if failed == "Inv_Deadlock":
        return "DEADLOCK"
    return "INVARIANT_VIOLATION"


def _property_label(model: _Model, name: str) -> str:
    if name == "Inv_Domain":
        return "declared variable domains"
    if name == "Inv_Deadlock":
        return "enabled protocol transition"
    invariant = next((item.name for item in model.invariants if item.generated == name), name)
    return invariant

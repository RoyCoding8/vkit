"""Check reachability of one captured scalar C if-arm and replay any witness."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..proc import run as run_process
from . import c_safety
from ._runtime import pinned_binary

_CBMC_ENV = "VKIT_CBMC_BIN"
_CBMC_SHA_ENV = "VKIT_CBMC_SHA256"
_GCC_ENV = "VKIT_C_BRANCH_GCC_BIN"
_GCC_SHA_ENV = "VKIT_C_BRANCH_GCC_SHA256"
_CBMC_VERSION = "6.11.0"
_MAX_TIMEOUT_SECONDS = 60
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_CAPTURE_BYTES = _MAX_OUTPUT_BYTES
_IDENTIFIER = c_safety._IDENTIFIER
_BRANCH_ID = re.compile(r"if@[1-9][0-9]*:[1-9][0-9]*:(?:then|else)\Z")


def _walk(node: Any):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(child for _, child in reversed(current.children()))


def _branch_id(node: Any, arm: str) -> str:
    return f"if@{node.coord.line}:{node.coord.column}:{arm}"


def _function_and_branches(c_ast: Any, tree: Any, function: str) -> tuple[Any, dict[str, tuple[Any, str]]]:
    definition = tree.ext[0]
    if definition.decl.name != function:
        raise c_safety._UnsupportedSource("the requested function must be the sole function definition")
    branches: dict[str, tuple[Any, str]] = {}
    for node in _walk(definition.body):
        if not isinstance(node, c_ast.If):
            continue
        if node.coord is None:
            raise c_safety._UnsupportedSource("if statements must have source coordinates")
        for arm in ("then", "else"):
            if arm == "else" and node.iffalse is None:
                continue
            identifier = _branch_id(node, arm)
            if identifier in branches:
                raise c_safety._UnsupportedSource("if branch source coordinates are ambiguous")
            branches[identifier] = (node, arm)
    return definition, branches


def _branch_inventory(source: bytes, function: str, parser: Any) -> list[dict[str, Any]]:
    c_ast, _, c_parser = parser
    try:
        tree = c_parser.CParser().parse(source.decode("utf-8").replace("\r\n", "\n"), filename="candidate.c")
    except (c_parser.ParseError, RecursionError) as exc:
        raise c_safety._UnsupportedSource("source is not valid in the supported C subset") from exc
    c_safety._validate_ast(c_ast, tree, function)
    if any(isinstance(node, (c_ast.ID, c_ast.Decl)) and node.name == "__vkit_branch_hit"
           for node in _walk(tree)):
        raise c_safety._UnsupportedSource("source uses a reserved branch-instrumentation identifier")
    _, branches = _function_and_branches(c_ast, tree, function)
    return [
        {"branch_id": identifier, "line": node.coord.line, "column": node.coord.column, "arm": arm}
        for identifier, (node, arm) in branches.items()
    ]


def _literal_value(value: int | bool, type_name: str) -> str:
    if type_name == "_Bool":
        if type(value) is not bool:
            raise ValueError("CBMC returned an invalid _Bool input")
        return "1" if value else "0"
    if type_name == "unsigned int":
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("CBMC returned an out-of-range unsigned int")
        return f"{value}U"
    if not -(1 << 31) <= value < (1 << 31):
        raise ValueError("CBMC returned an out-of-range int")
    return "(-2147483647 - 1)" if value == -(1 << 31) else str(value)


def _trace_inputs(trace: Any, function: str, parameters: list[tuple[str, str]]) -> dict[str, int | bool] | None:
    if not isinstance(trace, list):
        return None
    values: dict[str, int | bool] = {}
    parameter_types = dict(parameters)
    pending: list[tuple[str, Any]] = []
    for step in trace:
        if not isinstance(step, dict):
            continue
        location = step.get("sourceLocation")
        if step.get("stepType") == "assignment" and step.get("hidden") is True \
                and isinstance(location, dict) and location.get("function") == function \
                and isinstance(step.get("lhs"), str):
            name = step["lhs"].rsplit("::", 1)[-1].rsplit(".", 1)[-1]
            if name in parameter_types:
                pending.append((name, step.get("value")))
            continue
        if step.get("stepType") != "input":
            continue
        location = step.get("sourceLocation")
        if not isinstance(location, dict) or location.get("function") != function:
            continue
        if not pending:
            return None
        name, trace_value = pending.pop()
        type_name = parameter_types[name]
        if not isinstance(trace_value, dict):
            return None
        trace_type = str(trace_value.get("type", "")).lower()
        width = trace_value.get("width")
        if type_name == "_Bool":
            if trace_type != "_bool" or width != 8:
                return None
        elif type_name == "unsigned int":
            if "unsigned int" not in trace_type or width != 32:
                return None
        elif trace_type != "signed int" or width != 32:
            return None
        raw = trace_value.get("data")
        if type(raw) is int:
            value = raw
        elif isinstance(raw, str) and type_name == "unsigned int" \
                and re.fullmatch(r"[0-9]+[uU]?", raw):
            value = int(raw.rstrip("uU"))
        elif isinstance(raw, str) and type_name == "int" and re.fullmatch(r"-?[0-9]+", raw):
            value = int(raw)
        elif type_name == "_Bool" and isinstance(raw, str) and raw.lower() in {"true", "false"}:
            value = raw.lower() == "true"
        else:
            return None
        if name in values and values[name] != value:
            return None
        values[name] = value
    return values if set(values) == set(parameter_types) else None


def _run(argv: list[str], workdir: Path, deadline: float, prefix: str,
         env: dict[str, str]) -> tuple[Any, bytes, bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, b"", b""
    stdout_path = workdir / f"{prefix}.stdout"
    stderr_path = workdir / f"{prefix}.stderr"
    result = run_process(
        argv, cwd=workdir, env=env, stdout_path=stdout_path, stderr_path=stderr_path,
        timeout_seconds=remaining, max_output_bytes=_CAPTURE_BYTES,
    )
    try:
        stdout = stdout_path.read_bytes()
        stderr = stderr_path.read_bytes()
    except OSError:
        stdout, stderr = b"", b""
    return result, stdout, stderr


def _parameters(c_ast: Any, definition: Any) -> list[tuple[str, str]]:
    declarations = definition.decl.type.args.params
    if len(declarations) == 1 and isinstance(declarations[0], c_ast.Typename):
        return []
    result = []
    for declaration in declarations:
        names = tuple(declaration.type.type.names)
        type_name = "_Bool" if names == ("_Bool",) else (
            "unsigned int" if names == ("unsigned", "int") else "int"
        )
        result.append((declaration.name, type_name))
    return result


def _instrument(source: bytes, function: str, selected_id: str, parser: Any,
                mode: str, arguments: list[str] | None = None) -> tuple[bytes, list[tuple[str, str]]]:
    c_ast, c_generator, c_parser = parser
    try:
        tree = c_parser.CParser().parse(source.decode("utf-8").replace("\r\n", "\n"), filename="candidate.c")
    except (c_parser.ParseError, RecursionError) as exc:
        raise c_safety._UnsupportedSource("source is not valid in the supported C subset") from exc
    c_safety._validate_ast(c_ast, tree, function)
    definition, branches = _function_and_branches(c_ast, tree, function)
    branch = branches.get(selected_id)
    if branch is None:
        raise c_safety._UnsupportedSource("branch_id does not identify an if-arm in the requested function")
    node, arm = branch
    parameters = _parameters(c_ast, definition)
    if mode == "cbmc":
        helper_text = f'void __vkit_branch_hit(void) {{ __CPROVER_assert(0, "VKIT_BRANCH_ID:{selected_id}"); }}'
    else:
        helper_text = f'void __vkit_branch_hit(void) {{ puts("VKIT_BRANCH_ID:{selected_id}"); fflush(stdout); _Exit(0); }}'
        main_text = f"int main(void) {{ (void){function}({', '.join(arguments or [])}); return 70; }}"
        tree.ext.append(c_parser.CParser().parse(main_text).ext[0])
    helper = c_parser.CParser().parse(helper_text).ext[0]
    hit = c_parser.CParser().parse("void __vkit_insert(void) { __vkit_branch_hit(); }").ext[0]
    statement = hit.body.block_items[0]
    arm_node = node.iftrue if arm == "then" else node.iffalse
    if not isinstance(arm_node, c_ast.Compound):
        arm_node = c_ast.Compound(block_items=[arm_node])
        if arm == "then":
            node.iftrue = arm_node
        else:
            node.iffalse = arm_node
    arm_node.block_items.insert(0, statement)
    tree.ext.insert(0, helper)
    rendered = c_generator.CGenerator().visit(tree).encode("utf-8")
    if mode == "native":
        rendered = (
            b"#include <stdio.h>\n#include <stdlib.h>\n"
            b"_Static_assert(sizeof(int) == 4, \"int ABI\");\n"
            b"_Static_assert(sizeof(unsigned int) == 4, \"unsigned int ABI\");\n"
            b"_Static_assert(sizeof(_Bool) == 1, \"_Bool ABI\");\n"
            b"_Static_assert(sizeof(long) == 8, \"long ABI\");\n"
            b"_Static_assert(sizeof(void *) == 8, \"pointer ABI\");\n" + rendered
        )
    if len(rendered) > c_safety._MAX_SOURCE_BYTES:
        raise c_safety._UnsupportedSource("instrumented source exceeds the supported size limit")
    return rendered, parameters


def inspect_c_branch_reachability(
    root: Path,
    path: str,
    function: str,
    branch_id: str,
    unwind: int = 16,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Determine whether a selected if-arm is reachable in the bounded scalar C model."""
    if not isinstance(function, str) or not _IDENTIFIER.fullmatch(function) or "__CPROVER" in function:
        raise ValueError("function must be a C identifier")
    if not isinstance(branch_id, str) or not _BRANCH_ID.fullmatch(branch_id):
        raise ValueError("branch_id must use the generated if@line:column:then|else form")
    if type(unwind) is not int or not 1 <= unwind <= 256:
        raise ValueError("unwind must be an integer in [1, 256]")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {_MAX_TIMEOUT_SECONDS}]")

    base = {
        "operation": "inspect_c_branch_reachability", "path": path, "function": function,
        "branch_id": branch_id,
        "model_scope": f"{c_safety._MODEL}; standalone scalar function; bounded loops with unwinding assertions; selected if-arm reachability; native GCC UBSan replay for hits",
        "backend": f"cbmc/{_CBMC_VERSION}", "unwind": unwind,
    }
    try:
        target = c_safety._source_path(Path(root), path)
        source = c_safety._read_source(target)
    except ValueError as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    base["source_sha256"] = hashlib.sha256(source).hexdigest()
    parser = c_safety._parser()
    if parser is None:
        return {**base, "status": "UNAVAILABLE", "detail": "the pinned pycparser 3.0 dependency is unavailable"}
    deadline = time.monotonic() + timeout_seconds
    try:
        branches = _branch_inventory(source, function, parser)
        base["branches"] = branches
        if not any(item["branch_id"] == branch_id for item in branches):
            return {**base, "status": "UNSUPPORTED", "detail": "branch_id does not identify an if-arm in the requested function"}
        analyzed_source, parameters = _instrument(source, function, branch_id, parser, "cbmc")
    except c_safety._UnsupportedSource as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    if function == "main":
        return {**base, "status": "UNSUPPORTED", "detail": "main cannot be the analyzed function"}
    if time.monotonic() >= deadline:
        return {**base, "status": "UNKNOWN", "detail": "source validation exceeded the time budget"}

    cbmc_runtime = pinned_binary(_CBMC_ENV, _CBMC_SHA_ENV)
    gcc_runtime = pinned_binary(_GCC_ENV, _GCC_SHA_ENV)
    if cbmc_runtime is None or gcc_runtime is None:
        return {**base, "status": "UNAVAILABLE", "detail": "pinned CBMC 6.11.0 and Linux GCC runtimes are required"}
    cbmc, cbmc_sha256 = cbmc_runtime
    gcc, gcc_sha256 = gcc_runtime
    if sys.platform != "linux":
        return {**base, "status": "UNAVAILABLE", "cbmc_sha256": cbmc_sha256, "gcc_sha256": gcc_sha256,
                "detail": "native replay requires the modeled Linux x86_64 ABI"}

    try:
        with tempfile.TemporaryDirectory(prefix="vkit-c-branch-") as temporary:
            workdir = Path(temporary)
            model_path = workdir / "candidate.c"
            model_path.write_bytes(analyzed_source)
            model_sha256 = hashlib.sha256(analyzed_source).hexdigest()
            env = c_safety._process_env(workdir)
            command_dir = workdir / "bin"
            command_dir.mkdir()
            (command_dir / "gcc").symlink_to(gcc)
            env["PATH"] = os.pathsep.join((str(command_dir), str(gcc.parent)))
            version_result, version_out, version_err = _run([str(cbmc), "--version"], workdir, deadline, "cbmc-version", env)
            version_text = (version_out + version_err).decode("utf-8", errors="replace")
            if version_result is None or version_result.timed_out or version_result.output_limited:
                return {**base, "status": "UNKNOWN", "detail": "CBMC version check exceeded the time budget"}
            if version_result.code != 0 or not version_text.startswith(_CBMC_VERSION + " "):
                return {**base, "status": "UNAVAILABLE", "detail": "the configured binary did not report CBMC 6.11.0"}
            gcc_version, gcc_out, gcc_err = _run([str(gcc), "--version"], workdir, deadline, "gcc-version", env)
            machine, machine_out, machine_err = _run([str(gcc), "-dumpmachine"], workdir, deadline, "gcc-machine", env)
            if gcc_version is None or machine is None or gcc_version.timed_out or machine.timed_out \
                    or gcc_version.output_limited or machine.output_limited:
                return {**base, "status": "UNKNOWN", "detail": "GCC identity check exceeded the time budget"}
            if gcc_version.code != 0 or machine.code != 0 or machine_out.strip() != b"x86_64-linux-gnu" \
                    or b"gcc" not in (gcc_out + gcc_err).lower():
                return {**base, "status": "UNAVAILABLE", "detail": "the configured compiler is not native x86_64 Linux GCC"}
            command = [
                str(cbmc), "--json-ui", "--trace", "--c11", "--arch", "x86_64", "--os", "linux",
                "--LP64", "--little-endian", "--no-standard-checks", "--no-library",
                "--bounds-check", "--pointer-check", "--div-by-zero-check", "--signed-overflow-check",
                "--undefined-shift-check", "--unwinding-assertions", "--unwind", str(unwind),
                "--function", function, str(model_path),
            ]
            result, output, errors = _run(command, workdir, deadline, "cbmc", env)
            if result is None or result.timed_out or result.cancelled or result.output_limited:
                return {**base, "status": "UNKNOWN", "cbmc_sha256": cbmc_sha256, "gcc_sha256": gcc_sha256,
                        "model_sha256": model_sha256, "detail": "CBMC exceeded its time or output budget"}
            if result.launch_error:
                return {**base, "status": "UNAVAILABLE", "detail": "the pinned CBMC binary could not be started"}
            raw_sha256 = hashlib.sha256(len(output).to_bytes(8, "big") + output + errors).hexdigest()
            if len(output) >= _CAPTURE_BYTES or len(output) + len(errors) > _MAX_OUTPUT_BYTES:
                return {**base, "status": "UNKNOWN", "detail": "CBMC output exceeded its parsing limit"}
            try:
                document = json.loads(output.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                return {**base, "status": "UNKNOWN", "detail": "CBMC returned malformed or deeply nested JSON"}
            if not isinstance(document, list):
                return {**base, "status": "UNKNOWN", "detail": "CBMC returned malformed JSON output"}
            records = [record for record in document if isinstance(record, dict)]
            if any(record.get("messageType") == "ERROR" for record in records):
                return {**base, "status": "UNKNOWN", "detail": "CBMC reported an analysis error"}
            properties = []
            for record in records:
                listed = record.get("result")
                if listed is None:
                    continue
                if not isinstance(listed, list) or any(not isinstance(prop, dict) for prop in listed):
                    return {**base, "status": "UNKNOWN", "detail": "CBMC returned malformed property data"}
                properties.extend(listed)
            targets = [prop for prop in properties if prop.get("description") == f"VKIT_BRANCH_ID:{branch_id}"]
            cprover_statuses = [record["cProverStatus"] for record in records if "cProverStatus" in record]
            if len(cprover_statuses) != 1 or cprover_statuses[0] not in {"success", "failure"}:
                return {**base, "status": "UNKNOWN", "detail": "CBMC did not return one unambiguous completion status"}
            cprover = cprover_statuses[0]
            if len(targets) != 1:
                return {**base, "status": "UNKNOWN", "cbmc_sha256": cbmc_sha256, "gcc_sha256": gcc_sha256,
                        "model_sha256": model_sha256, "backend_output_sha256": raw_sha256,
                        "detail": "CBMC did not return exactly one selected branch property"}
            target_property = targets[0]
            if target_property.get("status") == "FAILURE" and cprover == "failure" and result.code == 10:
                trace = target_property.get("trace")
                if not isinstance(trace, list) or not any(
                    isinstance(step, dict) and step.get("stepType") == "failure" for step in trace
                ):
                    return {**base, "status": "UNKNOWN", "detail": "the selected property failure had no failure trace"}
                values = _trace_inputs(trace, function, parameters)
                if values is None:
                    return {**base, "status": "UNKNOWN", "detail": "CBMC trace did not provide every typed scalar input"}
                arguments = [_literal_value(values[name], type_name) for name, type_name in parameters]
                native_source, _ = _instrument(source, function, branch_id, parser, "native", arguments)
                native_path = workdir / "replay.c"
                binary_path = workdir / "replay"
                native_path.write_bytes(native_source)
                compile_result, compile_out, compile_err = _run([
                    str(gcc), "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=undefined", "-fno-sanitize-recover=all", str(native_path), "-o", str(binary_path),
                ], workdir, deadline, "compile", env)
                if compile_result is None or compile_result.timed_out or compile_result.output_limited:
                    return {**base, "status": "UNKNOWN", "detail": "native replay compilation exceeded its budget"}
                if compile_result.code != 0:
                    return {**base, "status": "UNKNOWN", "detail": "native replay did not compile cleanly"}
                replay, replay_out, replay_err = _run([str(binary_path)], workdir, deadline, "replay", env)
                marker = f"VKIT_BRANCH_ID:{branch_id}\n".encode("ascii")
                if replay is None or replay.timed_out or replay.output_limited or replay.code != 0 \
                        or replay_out != marker or replay_err:
                    return {**base, "status": "UNKNOWN", "detail": "native UBSan replay did not confirm the exact branch hit"}
                replay_sha256 = hashlib.sha256(native_source).hexdigest()
                return {
                    **base, "status": "REACHABLE", "cbmc_sha256": cbmc_sha256, "gcc_sha256": gcc_sha256,
                    "model_sha256": model_sha256, "backend_output_sha256": raw_sha256,
                    "replay_sha256": replay_sha256,
                    "witness": {"inputs": values, "input_types": dict(parameters), "observation": marker.decode("ascii").rstrip()},
                }
            if target_property.get("status") == "SUCCESS" and cprover == "success" and result.code == 0 \
                    and properties and all(prop.get("status") == "SUCCESS" for prop in properties):
                return {
                    **base, "status": "UNREACHABLE", "cbmc_sha256": cbmc_sha256, "gcc_sha256": gcc_sha256,
                    "model_sha256": model_sha256, "backend_output_sha256": raw_sha256,
                    "detail": "the selected property and all safety and unwinding checks completed successfully",
                }
            return {**base, "status": "UNKNOWN", "cbmc_sha256": cbmc_sha256, "gcc_sha256": gcc_sha256,
                    "model_sha256": model_sha256, "backend_output_sha256": raw_sha256,
                    "detail": "the selected property or a required safety or unwinding check was incomplete"}
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError) as exc:
        return {**base, "status": "UNKNOWN", "detail": f"analysis could not be completed: {type(exc).__name__}"}

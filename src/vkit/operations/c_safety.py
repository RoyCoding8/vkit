"""Bounded safety checks for a deliberately small, standalone C subset."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PureWindowsPath
from typing import Any

_BACKEND_ENV = "VKIT_CBMC_BIN"
_BACKEND_SHA_ENV = "VKIT_CBMC_SHA256"
_BACKEND_VERSION = "6.11.0"
_PARSER_VERSION = "3.0"
_MAX_TIMEOUT_SECONDS = 60
_MAX_SOURCE_BYTES = 65_536
_MAX_AST_NODES = 8_192
_MAX_AST_DEPTH = 128
_MAX_PARAMETERS = 32
_MAX_LOCALS = 64
_MAX_ARRAYS = 16
_MAX_ARRAY_ELEMENTS = 1_024
_MAX_ARRAY_LENGTH = 256
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MODEL = "C11 x86_64 Linux LP64 little-endian, 32-bit int"
_CHECK_SCOPE = "bounds, pointer, division-by-zero, signed-overflow, undefined-shift"
_SAFETY_RULES = {
    "array bounds": "bounds-check",
    "division-by-zero": "div-by-zero-check",
    "division by zero": "div-by-zero-check",
    "undefined-shift": "undefined-shift-check",
    "undefined shift": "undefined-shift-check",
    "overflow": "signed-overflow-check",
    "pointer dereference": "pointer-check",
    "pointer": "pointer-check",
}
_SELECTED_CHECKS = [
    "bounds-check", "pointer-check", "div-by-zero-check", "signed-overflow-check",
    "undefined-shift-check", "unwinding-assertions",
]


class _UnsupportedSource(Exception):
    pass


def _source_path(root: Path, path: str) -> Path:
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("path must be a nonempty repository-relative C source path")
    relative = Path(path)
    windows = PureWindowsPath(path)
    if relative.is_absolute() or windows.is_absolute() or windows.drive \
            or ".." in relative.parts or ".." in windows.parts:
        raise ValueError("path must stay inside the repository")
    try:
        base = Path(root).resolve(strict=True)
        target = base / relative
        current = base
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("source path must not contain symlinks")
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve C source path: {exc}") from exc
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise ValueError("path must identify a regular source file inside the repository")
    return resolved


def _read_source(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            data = source.read(_MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read C source: {exc}") from exc
    if len(data) > _MAX_SOURCE_BYTES:
        raise ValueError(f"C source must be at most {_MAX_SOURCE_BYTES} bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("C source must be UTF-8") from exc
    return data


def _parser():
    try:
        if version("pycparser") != _PARSER_VERSION:
            return None
        from pycparser import c_ast, c_generator, c_parser
    except (ImportError, PackageNotFoundError):
        return None
    return c_ast, c_generator, c_parser


def _scalar_type(c_ast: Any, node: Any, *, allow_void: bool = False) -> tuple[str, ...]:
    if not isinstance(node, c_ast.TypeDecl) or node.quals or not isinstance(node.type, c_ast.IdentifierType):
        raise _UnsupportedSource("only unqualified scalar integer types are supported")
    names = tuple(node.type.names)
    allowed = {("int",), ("signed", "int"), ("unsigned", "int"), ("_Bool",)}
    if allow_void:
        allowed.add(("void",))
    if names not in allowed:
        raise _UnsupportedSource("only int, unsigned int, and _Bool scalar types are supported")
    return names


def _validate_ast(c_ast: Any, tree: Any, function: str) -> None:
    allowed = {
        c_ast.ArrayDecl, c_ast.ArrayRef, c_ast.Assignment, c_ast.BinaryOp,
        c_ast.Break, c_ast.Case, c_ast.Cast, c_ast.Compound, c_ast.Constant,
        c_ast.Continue, c_ast.Decl, c_ast.DeclList, c_ast.Default, c_ast.DoWhile,
        c_ast.EmptyStatement, c_ast.ExprList, c_ast.For, c_ast.FuncDecl,
        c_ast.FuncDef, c_ast.ID, c_ast.IdentifierType, c_ast.If, c_ast.InitList,
        c_ast.ParamList, c_ast.Return, c_ast.Switch, c_ast.TernaryOp, c_ast.TypeDecl,
        c_ast.Typename, c_ast.UnaryOp, c_ast.While,
    }
    if len(tree.ext) != 1 or not isinstance(tree.ext[0], c_ast.FuncDef):
        raise _UnsupportedSource("source must contain exactly one function definition and no globals")
    definition = tree.ext[0]
    declaration = definition.decl
    if declaration.name != function:
        raise _UnsupportedSource("the requested function must be the sole function definition")
    if declaration.storage or declaration.funcspec or declaration.quals or declaration.align:
        raise _UnsupportedSource("function storage classes, qualifiers, and attributes are unsupported")
    function_type = declaration.type
    if not isinstance(function_type, c_ast.FuncDecl):
        raise _UnsupportedSource("function declaration is unsupported")
    _scalar_type(c_ast, function_type.type, allow_void=True)

    params = function_type.args
    if params is None or not isinstance(params, c_ast.ParamList):
        raise _UnsupportedSource("function parameters must have an explicit prototype")
    parameter_declarations = params.params
    void_parameter = None
    if len(parameter_declarations) == 1 and isinstance(parameter_declarations[0], c_ast.Typename):
        candidate_type = parameter_declarations[0].type
        if isinstance(candidate_type, c_ast.TypeDecl) and isinstance(candidate_type.type, c_ast.IdentifierType) \
                and tuple(candidate_type.type.names) == ("void",) and not candidate_type.quals:
            void_parameter = candidate_type
    if void_parameter is not None:
        parameter_declarations = []
    if len(parameter_declarations) > _MAX_PARAMETERS:
        raise _UnsupportedSource(f"functions may have at most {_MAX_PARAMETERS} parameters")
    for parameter in parameter_declarations:
        if not isinstance(parameter, c_ast.Decl) or not parameter.name:
            raise _UnsupportedSource("parameters must be named scalar values")
        if parameter.storage or parameter.funcspec or parameter.quals or parameter.align:
            raise _UnsupportedSource("parameter qualifiers and storage classes are unsupported")
        _scalar_type(c_ast, parameter.type)

    node_count = 0
    void_type_ids = {id(function_type.type)}
    if void_parameter is not None:
        void_type_ids.add(id(void_parameter))
    stack = [(definition, 1, None, "")]
    array_names: set[str] = set()
    identifier_uses: list[tuple[Any, Any, str]] = []
    declarations = 0
    array_count = 0
    array_elements = 0
    while stack:
        node, depth, parent, slot = stack.pop()
        node_count += 1
        if node_count > _MAX_AST_NODES or depth > _MAX_AST_DEPTH:
            raise _UnsupportedSource("source syntax exceeds the supported size or nesting limit")
        if type(node) not in allowed:
            raise _UnsupportedSource(f"unsupported C syntax: {type(node).__name__}")
        if isinstance(node, c_ast.ID) and "__CPROVER" in node.name:
            raise _UnsupportedSource("CBMC intrinsic identifiers are unsupported")
        if isinstance(node, c_ast.ID):
            identifier_uses.append((node, parent, slot))
        mutation = isinstance(node, c_ast.Assignment) or (
            isinstance(node, c_ast.UnaryOp) and node.op in {"++", "--", "p++", "p--"}
        )
        statement = isinstance(parent, c_ast.Compound) or (
            isinstance(parent, c_ast.For) and slot in {"init", "next", "stmt"}
        ) or (
            isinstance(parent, (c_ast.If, c_ast.While, c_ast.DoWhile))
            and slot in {"iftrue", "iffalse", "stmt"}
        ) or isinstance(parent, (c_ast.Case, c_ast.Default))
        if mutation and not statement:
            raise _UnsupportedSource("assignments and increments must be standalone statements or for-loop updates")
        if isinstance(node, c_ast.TypeDecl):
            _scalar_type(c_ast, node, allow_void=id(node) in void_type_ids)
        if isinstance(node, c_ast.Decl):
            if node.name and "__CPROVER" in node.name:
                raise _UnsupportedSource("CBMC intrinsic identifiers are unsupported")
            declarations += 1
            if declarations > _MAX_LOCALS + len(parameter_declarations) + 1:
                raise _UnsupportedSource("source declares too many local variables")
            if node is not declaration and node not in parameter_declarations:
                if node.storage or node.funcspec or node.quals or node.align or node.bitsize:
                    raise _UnsupportedSource("local storage classes, qualifiers, and bitfields are unsupported")
                if node.init is None:
                    raise _UnsupportedSource("local variables and arrays must have explicit initializers")
                initializer_nodes = [node.init]
                while initializer_nodes:
                    initializer = initializer_nodes.pop()
                    if isinstance(initializer, c_ast.ID) and initializer.name == node.name:
                        raise _UnsupportedSource("local initializers must not reference the variable being declared")
                    initializer_nodes.extend(child for _, child in initializer.children())
                if isinstance(node.type, c_ast.ArrayDecl):
                    array_names.add(node.name)
                    array_count += 1
                    if array_count > _MAX_ARRAYS or node.type.dim is None \
                            or not isinstance(node.type.dim, c_ast.Constant) \
                            or node.type.dim.type != "int" \
                            or not re.fullmatch(r"[0-9]+", node.type.dim.value):
                        raise _UnsupportedSource("only fixed one-dimensional local arrays are supported")
                    length = int(node.type.dim.value)
                    _scalar_type(c_ast, node.type.type)
                    if not 1 <= length <= _MAX_ARRAY_LENGTH:
                        raise _UnsupportedSource(f"local arrays must have between 1 and {_MAX_ARRAY_LENGTH} elements")
                    array_elements += length
                    if array_elements > _MAX_ARRAY_ELEMENTS:
                        raise _UnsupportedSource("local array storage exceeds the supported limit")
                else:
                    _scalar_type(c_ast, node.type)
        if isinstance(node, (c_ast.Assignment,)) and node.op not in {
            "=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|=",
        }:
            raise _UnsupportedSource("assignment operator is unsupported")
        if isinstance(node, c_ast.BinaryOp) and node.op not in {
            "+", "-", "*", "/", "%", "<<", ">>", "&", "|", "^", "&&", "||",
            "<", "<=", ">", ">=", "==", "!=",
        }:
            raise _UnsupportedSource("binary operator is unsupported")
        if isinstance(node, c_ast.UnaryOp) and node.op not in {
            "+", "-", "!", "~", "++", "--", "p++", "p--", "sizeof",
        }:
            raise _UnsupportedSource("pointer or unsupported unary operator is unsupported")
        if isinstance(node, c_ast.Constant):
            if node.type not in {"int", "unsigned int", "char"}:
                raise _UnsupportedSource("only int, unsigned int, and character constants are supported")
            if len(node.value) > 128:
                raise _UnsupportedSource("integer constants exceed the supported size limit")
        stack.extend((child, depth + 1, node, name) for name, child in node.children())
    for node, parent, slot in identifier_uses:
        if node.name in array_names and not (
            isinstance(parent, c_ast.ArrayRef) and slot == "name"
            or isinstance(parent, c_ast.UnaryOp) and parent.op == "sizeof"
        ):
            raise _UnsupportedSource("local arrays may only be indexed directly or used with sizeof")


def _validate_and_generate(source: bytes, function: str, parser: Any) -> bytes:
    c_ast, c_generator, c_parser = parser
    try:
        text = source.decode("utf-8")
        tree = c_parser.CParser().parse(text, filename="candidate.c")
    except (c_parser.ParseError, RecursionError) as exc:
        raise _UnsupportedSource("source is not valid in the supported C subset") from exc
    _validate_ast(c_ast, tree, function)
    try:
        generated = c_generator.CGenerator().visit(tree).encode("utf-8")
    except (RecursionError, ValueError) as exc:
        raise _UnsupportedSource("source could not be serialized within the supported limits") from exc
    if len(generated) > _MAX_SOURCE_BYTES:
        raise _UnsupportedSource("normalized C source exceeds the supported size limit")
    return generated


def _runtime() -> tuple[Path, str] | None:
    raw_path = os.environ.get(_BACKEND_ENV)
    expected = os.environ.get(_BACKEND_SHA_ENV, "").lower()
    if not raw_path or not _SHA256.fullmatch(expected):
        return None
    try:
        binary = Path(raw_path).expanduser().resolve()
        if not binary.is_file():
            return None
        with binary.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None
    return (binary, digest) if digest == expected else None


def _process_env(workdir: Path) -> dict[str, str]:
    env = {"PATH": "", "HOME": str(workdir), "TMPDIR": str(workdir), "LC_ALL": "C", "LANG": "C"}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE"):
            if value := os.environ.get(key):
                env[key] = value
        env["TEMP"] = str(workdir)
        env["TMP"] = str(workdir)
    return env


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _decode_json(raw: bytes) -> Any:
    if len(raw) > _MAX_OUTPUT_BYTES:
        raise ValueError("CBMC output exceeds the supported limit")
    return json.loads(raw.decode("utf-8"))


def _normalize_trace(value: Any, source_path: str) -> Any:
    if isinstance(value, list):
        return [_normalize_trace(item, source_path) for item in value]
    if isinstance(value, dict):
        normalized = {key: _normalize_trace(item, source_path) for key, item in value.items()}
        location = normalized.get("sourceLocation")
        if isinstance(location, dict) and PureWindowsPath(str(location.get("file", ""))).name == "candidate.i":
            location["file"] = source_path
        return normalized
    return value


def _property_record(item: dict[str, Any], source_path: str) -> dict[str, Any]:
    location = item.get("sourceLocation")
    location = location if isinstance(location, dict) else {}
    trace = item.get("trace")
    return {
        "ruleid": item.get("propertyId") or item.get("property"),
        "status": item.get("status"),
        "file": source_path,
        "line": int(location["line"]) if str(location.get("line", "")).isdigit() else None,
        "line_basis": "normalized_snapshot",
        "message": item.get("description") or item.get("reason"),
        "property_class": location.get("propertyClass"),
        "trace": _normalize_trace(trace, source_path) if isinstance(trace, list) else None,
    }


def _is_unwinding(record: dict[str, Any]) -> bool:
    message = str(record.get("message") or "").lower()
    ruleid = str(record.get("ruleid") or "").lower()
    return "unwinding assertion" in message or "unwinding assertion" in ruleid


def _has_failure_trace(record: dict[str, Any]) -> bool:
    trace = record.get("trace")
    return isinstance(trace, list) and any(
        isinstance(step, dict) and step.get("stepType") == "failure" for step in trace
    )


def _classify(document: Any, source_path: str) -> tuple[str, list[dict[str, Any]], str | None]:
    if not isinstance(document, list):
        return "UNKNOWN", [], "CBMC returned malformed JSON output"
    properties = [
        _property_record(prop, source_path)
        for record in document if isinstance(record, dict)
        for prop in record.get("result", []) if isinstance(prop, dict)
    ]
    messages = [record for record in document if isinstance(record, dict)]
    errors = [str(record.get("messageText", "")) for record in messages if record.get("messageType") == "ERROR"]
    if errors:
        source_errors = (
            "parse", "typecheck", "type-check", "conversion error", "failed to find symbol",
            "type mismatch", "undeclared", "unknown type", "incomplete type",
        )
        if any(any(marker in message.lower() for marker in source_errors) for message in errors):
            return "UNSUPPORTED", properties, "; ".join(errors[:4])
        return "UNKNOWN", properties, "; ".join(errors[:4])
    cprover = next((record.get("cProverStatus") for record in messages if "cProverStatus" in record), None)
    if any(prop.get("status") in {"UNKNOWN", "NOT_CHECKED"} for prop in properties):
        return "UNKNOWN", properties, "one or more safety checks were unfinished"
    failures = [prop for prop in properties if prop.get("status") == "FAILURE"]
    if any(_is_unwinding(prop) for prop in failures):
        return "UNKNOWN", properties, "the requested loop unwind was insufficient"
    if failures:
        verified = [
            prop for prop in failures
            if prop.get("property_class") in _SAFETY_RULES
            and _has_failure_trace(prop)
        ]
        if verified and cprover == "failure":
            return "COUNTEREXAMPLE", properties, None
        return "UNKNOWN", properties, "CBMC reported a failure without a recognized safety trace"
    if cprover == "success" and all(prop.get("status") == "SUCCESS" for prop in properties):
        return "SAFE", properties, None
    return "UNKNOWN", properties, "CBMC did not complete every selected safety check"


def check_c_safety(
    root: Path,
    path: str,
    function: str,
    unwind: int = 16,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Check one standalone C function with fixed, bounded CBMC safety properties."""
    if not isinstance(function, str) or not _IDENTIFIER.fullmatch(function) or "__CPROVER" in function:
        raise ValueError("function must be a C identifier")
    if type(unwind) is not int or not 1 <= unwind <= 256:
        raise ValueError("unwind must be an integer in [1, 256]")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {_MAX_TIMEOUT_SECONDS}]")

    target = _source_path(root, path)
    source = _read_source(target)
    source_sha256 = hashlib.sha256(source).hexdigest()
    model_scope = (
        f"{_MODEL}; unconstrained scalar parameters; local declarations require explicit initializers; "
        f"uninitialized reads are not checked; checks: {_CHECK_SCOPE}; "
        f"loops bounded to {unwind} iterations with unwinding assertions"
    )
    base: dict[str, Any] = {
        "operation": "check_c_safety",
        "path": path,
        "function": function,
        "model_scope": model_scope,
        "selected_checks": _SELECTED_CHECKS.copy(),
        "source_sha256": source_sha256,
        "backend": f"cbmc/{_BACKEND_VERSION}",
    }
    parser = _parser()
    if parser is None:
        return {**base, "status": "UNAVAILABLE", "backend_sha256": None,
                "detail": "the pinned pycparser 3.0 dependency is unavailable"}
    deadline = time.monotonic() + timeout_seconds
    try:
        generated = _validate_and_generate(source, function, parser)
    except _UnsupportedSource as exc:
        return {**base, "status": "UNSUPPORTED", "backend_sha256": None, "detail": str(exc)}
    if _remaining(deadline) <= 0:
        return {**base, "status": "UNKNOWN", "backend_sha256": None,
                "detail": "source validation exceeded the time budget"}

    runtime = _runtime()
    if runtime is None:
        return {**base, "status": "UNAVAILABLE", "backend_sha256": None,
                "detail": "the configured CBMC binary is unavailable or failed its SHA-256 check"}
    binary, backend_sha256 = runtime
    try:
        with tempfile.TemporaryDirectory(prefix="vkit-c-safety-") as temporary:
            workdir = Path(temporary)
            snapshot = workdir / "candidate.i"
            snapshot.write_bytes(generated)
            snapshot_sha256 = hashlib.sha256(generated).hexdigest()
            env = _process_env(workdir)
            remaining = _remaining(deadline)
            if remaining <= 0:
                return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                        "snapshot_sha256": snapshot_sha256, "detail": "analysis exceeded the time budget"}
            version_result = subprocess.run(
                [str(binary), "--version"], cwd=workdir, env=env,
                stdin=subprocess.DEVNULL, capture_output=True, timeout=remaining, check=False,
            )
            version_text = (version_result.stdout + version_result.stderr).decode("utf-8", errors="replace")
            if version_result.returncode != 0 or not version_text.startswith(_BACKEND_VERSION + " "):
                return {**base, "status": "UNAVAILABLE", "backend_sha256": backend_sha256,
                        "snapshot_sha256": snapshot_sha256,
                        "detail": "the configured binary did not report CBMC 6.11.0"}
            remaining = _remaining(deadline)
            if remaining <= 0:
                return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                        "snapshot_sha256": snapshot_sha256, "detail": "analysis exceeded the time budget"}
            command = [
                str(binary), "--json-ui", "--trace", "--c11", "--arch", "x86_64", "--os", "linux",
                "--LP64", "--little-endian", "--no-library", "--no-standard-checks",
                "--bounds-check", "--pointer-check", "--div-by-zero-check", "--signed-overflow-check",
                "--undefined-shift-check", "--unwinding-assertions", "--unwind", str(unwind),
                "--function", function, str(snapshot),
            ]
            result = subprocess.run(
                command, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=remaining, check=False,
            )
    except subprocess.TimeoutExpired:
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "detail": "the C safety analysis exceeded its time budget"}
    except OSError:
        return {**base, "status": "UNAVAILABLE", "backend_sha256": backend_sha256,
                "detail": "the configured CBMC binary could not be started"}
    try:
        document = _decode_json(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "snapshot_sha256": snapshot_sha256, "detail": "CBMC returned malformed or oversized output"}
    try:
        status, properties, detail = _classify(document, path)
    except RecursionError:
        return {**base, "status": "UNKNOWN", "backend_sha256": backend_sha256,
                "snapshot_sha256": snapshot_sha256, "detail": "CBMC output nesting exceeds parser limits"}
    if (status == "SAFE" and result.returncode != 0) \
            or (status == "COUNTEREXAMPLE" and result.returncode != 10):
        status, detail = "UNKNOWN", "CBMC exit status did not match its reported properties"
    result_record = {
        **base,
        "status": status,
        "backend_sha256": backend_sha256,
        "snapshot_sha256": snapshot_sha256,
        "diagnostics": properties,
        "detail": detail,
    }
    if status in {"COUNTEREXAMPLE", "UNKNOWN"} and properties:
        result_record["snapshot_source"] = generated.decode("utf-8")
    return result_record

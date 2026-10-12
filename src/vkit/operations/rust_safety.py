"""Check one closed primitive Rust function with the pinned Kani verifier."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .. import proc
from ._runtime import pinned_binary

KANI_VERSION = "0.68.0"
CBMC_VERSION = "6.11.0"
RUST_TOOLCHAIN = "nightly-2026-08-21"
RUSTC_VERSION = "rustc 1.100.0-nightly (8925ea358 2026-08-20)"
TREE_SITTER_VERSION = "0.25.2"
TREE_SITTER_RUST_VERSION = "0.24.2"
KANI_BUNDLE_SHA256 = "32e2b484d73ede0bbf64a2cf0879c4259422497d8aec4ee67d448de9ae7843d3"
KANI_BIN_ENV = "VKIT_KANI_BIN"
KANI_SHA_ENV = "VKIT_KANI_SHA256"
KANI_HOME_ENV = "VKIT_KANI_HOME"
RUSTUP_HOME_ENV = "VKIT_RUSTUP_HOME"
MAX_SOURCE_BYTES = 65_536
MAX_GENERATED_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 60
MAX_UNWIND = 32
MAX_NODES = 2_048
MAX_DEPTH = 64
MAX_PARAMETERS = 16
MAX_LOCALS = 64
MAX_LOOP_DEPTH = 8
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_DECIMAL = re.compile(r"([0-9][0-9_]*)?(u8|u16|u32|u64|i8|i16|i32|i64)?\Z")
_INT_TYPES = {f"{sign}{bits}": (int(bits), sign == "i")
              for sign in ("u", "i") for bits in (8, 16, 32, 64)}
_BINARY_OPS = {"+", "-", "*", "/", "%", "<<", ">>", "&", "|", "^",
               "&&", "||", "==", "!=", "<", "<=", ">", ">="}
_ARITHMETIC = {"+", "-", "*", "/", "%"}
_ASSIGNMENT_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "|=", "^="}
_CHECK_KINDS = {
    "attempt to add with overflow": "overflow",
    "attempt to subtract with overflow": "overflow",
    "attempt to multiply with overflow": "overflow",
    "attempt to negate with overflow": "overflow",
    "attempt to divide by zero": "division-by-zero",
    "attempt to divide with overflow": "overflow",
    "attempt to calculate the remainder with a divisor of zero": "division-by-zero",
    "attempt to calculate the remainder with overflow": "overflow",
    "attempt to shift left with overflow": "invalid-shift",
    "attempt to shift right with overflow": "invalid-shift",
}


class _Unsupported(Exception):
    pass


class _ReplayFailure(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


@dataclass(frozen=True)
class _Expr:
    kind: str
    type_name: str
    value: Any = None
    children: tuple[_Expr, ...] = ()


@dataclass(frozen=True)
class _Stmt:
    kind: str
    name: str = ""
    type_name: str = ""
    mutable: bool = False
    op: str = ""
    expr: _Expr | None = None
    body: tuple[_Stmt, ...] = ()
    otherwise: tuple[_Stmt, ...] = ()
    condition: _Expr | None = None


@dataclass(frozen=True)
class _Function:
    name: str
    parameters: tuple[tuple[str, str], ...]
    return_type: str
    body: tuple[_Stmt, ...]


def check_rust_safety(root: Path, path: str, function: str,
                      unwind: int = 8, timeout_seconds: int = 60) -> dict[str, Any]:
    """Check the fixed integer safety properties of one standalone primitive function."""
    if not isinstance(function, str) or not _IDENTIFIER.fullmatch(function):
        raise ValueError("function must be a Rust identifier")
    if type(unwind) is not int or not 1 <= unwind <= MAX_UNWIND:
        raise ValueError(f"unwind must be an integer in [1, {MAX_UNWIND}]")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")

    base: dict[str, Any] = {
        "operation": "check_rust_safety",
        "path": path,
        "function": function,
        "engine": {
            "name": "Kani",
            "version": KANI_VERSION,
            "cbmc_version": CBMC_VERSION,
            "rust_toolchain": RUST_TOOLCHAIN,
            "target": "x86_64-unknown-linux-gnu",
            "release_bundle_sha256": KANI_BUNDLE_SHA256,
        },
        "model_scope": (
            "One standalone function lowered to the closed typed primitive subset; full domains of "
            "u8/u16/u32/u64, i8/i16/i32/i64, and bool parameters; initialized scalar locals; "
            "fixed Rust overflow, division, remainder, shift, and panic checks; while loops use "
            f"unwind {unwind} with Kani unwinding assertions enabled; no pointers, heap, unsafe, "
            "macros, attributes, calls, external state, or concurrency"
        ),
        "selected_checks": ["integer overflow", "division by zero", "invalid shift", "panic", "unwinding assertions"],
    }
    try:
        source_path = _source_path(root, path)
        source = _read_source(source_path)
        source_sha = hashlib.sha256(source).hexdigest()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    base["source_sha256"] = source_sha

    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        return {**base, "status": "UNAVAILABLE", "detail": "the pinned Kani runtime is supported here only on Linux x86_64"}
    parser = _parser()
    if parser is None:
        return {**base, "status": "UNAVAILABLE",
                "detail": f"tree-sitter {TREE_SITTER_VERSION} and tree-sitter-rust {TREE_SITTER_RUST_VERSION} are required"}
    try:
        model = _parse_function(source, function, parser)
        generated = _generate(model)
    except _Unsupported as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    except (RecursionError, ValueError, OverflowError):
        return {**base, "status": "UNSUPPORTED", "detail": "Rust source exceeds the supported syntax limits"}
    if len(generated) > MAX_GENERATED_BYTES:
        return {**base, "status": "UNSUPPORTED", "detail": "generated proof input exceeds its byte limit"}
    generated_sha = hashlib.sha256(generated).hexdigest()
    base["generated_source_sha256"] = generated_sha

    runtime = pinned_binary(KANI_BIN_ENV, KANI_SHA_ENV)
    if runtime is None:
        return {**base, "status": "UNAVAILABLE",
                "detail": f"configure {KANI_BIN_ENV} and {KANI_SHA_ENV} for the pinned Kani executable"}
    kani_bin, binary_sha = runtime
    base["engine"]["binary_sha256"] = binary_sha
    home_value = os.environ.get(KANI_HOME_ENV)
    rustup_value = os.environ.get(RUSTUP_HOME_ENV)
    if not home_value or not rustup_value:
        return {**base, "status": "UNAVAILABLE",
                "detail": f"configure {KANI_HOME_ENV} and {RUSTUP_HOME_ENV} for the pinned Kani runtime"}
    kani_home = Path(home_value).expanduser().resolve()
    rustup_home = Path(rustup_value).expanduser().resolve()
    bundle_name = f"kani-{KANI_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
    bundle_cache = kani_home.parent / "bundle-cache" / bundle_name
    bundle_sha = _sha256_file(bundle_cache)
    toolchain_root = rustup_home / "toolchains" / "nightly-2026-08-21-x86_64-unknown-linux-gnu"
    if bundle_sha != KANI_BUNDLE_SHA256 or not (kani_home / f"kani-{KANI_VERSION}" / "bin" / "cbmc").is_file():
        return {**base, "status": "UNAVAILABLE", "detail": "the installed Kani bundle is missing or failed its release SHA-256 check"}
    rustc_path = toolchain_root / "bin" / "rustc"
    if not rustc_path.is_file():
        return {**base, "status": "UNAVAILABLE", "detail": "the pinned Rust toolchain is not installed"}

    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="vkit-rust-safety-") as temporary:
        workdir = Path(temporary)
        generated_path = workdir / "model.rs"
        result_path = workdir / "kani-results.json"
        generated_path.write_bytes(generated)
        (workdir / "home").mkdir()
        (workdir / "cargo-home").mkdir()
        (workdir / "cargo-home" / "bin").mkdir()
        rustup_binary = kani_bin.parent / "rustup"
        if not rustup_binary.is_file():
            return {**base, "status": "UNAVAILABLE", "detail": "the isolated runtime is missing its pinned rustup executable"}
        shutil.copy2(rustup_binary, workdir / "cargo-home" / "bin" / "rustup")
        env = {
            "PATH": os.pathsep.join((str(workdir / "cargo-home" / "bin"), str(rustc_path.parent),
                                      str(kani_bin.parent), "/usr/bin", "/bin")),
            "HOME": str(workdir / "home"),
            "CARGO_HOME": str(workdir / "cargo-home"),
            "CARGO_TARGET_DIR": str(workdir / "target"),
            "TMPDIR": str(workdir),
            "RUSTUP_HOME": str(rustup_home),
            "RUSTUP_TOOLCHAIN": RUST_TOOLCHAIN,
        }

        private_kani_home = workdir / "kani-home"
        private_kani_home.mkdir()
        try:
            os.symlink(kani_home / f"kani-{KANI_VERSION}",
                       private_kani_home / f"kani-{KANI_VERSION}", target_is_directory=True)
            # Kani removes its setup archive after use. A private symlink keeps
            # that unlink scoped to this request while preserving the shared cache.
            os.symlink(bundle_cache, private_kani_home / bundle_name)
        except OSError:
            return {**base, "status": "UNAVAILABLE", "detail": "the verified Kani bundle cannot be isolated for this request"}
        env["KANI_HOME"] = str(private_kani_home)
        captured_source_path = workdir / "captured.rs"
        captured_source_path.write_bytes(source)
        remaining = _remaining(deadline)
        if remaining <= 0:
            return {**base, "status": "UNKNOWN", "detail": "analysis exceeded its time budget before Kani version check"}
        version_run = proc.run(
            [str(kani_bin), "--version"], cwd=workdir,
            stdout_path=workdir / "version.out", stderr_path=workdir / "version.err",
            timeout_seconds=remaining, env=env,
            should_cancel=lambda: _output_over_limit(workdir / "version.out", workdir / "version.err"),
        )
        version_out = _read_output(workdir / "version.out")
        if version_run.timed_out or version_run.cancelled:
            return {**base, "status": "UNKNOWN", "detail": "Kani version check exceeded its time or output budget"}
        if version_run.launch_error:
            return {**base, "status": "UNAVAILABLE", "detail": "the pinned Kani executable could not start"}
        version_lines = version_out.splitlines() if version_out is not None else []
        if version_run.code != 0 or version_lines[-2:] != [
                f"Kani Rust Verifier {KANI_VERSION} (standalone)", f"CBMC {CBMC_VERSION}"]:
            return {**base, "status": "UNAVAILABLE", "detail": "the configured executable is not Kani 0.68.0 with CBMC 6.11.0"}
        remaining = _remaining(deadline)
        if remaining <= 0:
            return {**base, "status": "UNKNOWN", "detail": "analysis exceeded its time budget before Rust version check"}
        rustc_run = proc.run(
            [str(rustc_path), "--version"], cwd=workdir,
            stdout_path=workdir / "rustc.out", stderr_path=workdir / "rustc.err",
            timeout_seconds=_remaining(deadline), env=env,
            should_cancel=lambda: _output_over_limit(workdir / "rustc.out", workdir / "rustc.err"),
        )
        rustc_out = _read_output(workdir / "rustc.out")
        rustc_err = _read_output(workdir / "rustc.err")
        if rustc_run.timed_out or rustc_run.cancelled:
            return {**base, "status": "UNKNOWN", "detail": "Rust toolchain check exceeded its time or output budget"}
        if rustc_run.launch_error or rustc_run.code != 0 or rustc_out is None or rustc_err is None \
                or rustc_out.strip() != RUSTC_VERSION or rustc_err:
            return {**base, "status": "UNAVAILABLE", "detail": "the configured rustup home lacks the pinned Rust compiler"}
        compile_run = proc.run(
            [str(rustc_path), "--crate-name", "__vkit_captured", "--crate-type", "lib",
             "--emit", "metadata", "--edition", "2021", "--cap-lints", "allow",
             "-o", str(workdir / "captured.rmeta"), str(captured_source_path)],
            cwd=workdir, env=env,
            stdout_path=workdir / "compile.out", stderr_path=workdir / "compile.err",
            timeout_seconds=_remaining(deadline),
            should_cancel=lambda: _output_over_limit(workdir / "compile.out", workdir / "compile.err"),
        )
        if compile_run.timed_out or compile_run.cancelled:
            return {**base, "status": "UNKNOWN", "detail": "captured Rust source compilation exceeded its time or output budget"}
        if compile_run.launch_error:
            return {**base, "status": "UNAVAILABLE", "detail": "the pinned Rust compiler could not start"}
        if compile_run.code != 0:
            return {**base, "status": "UNSUPPORTED", "detail": "captured source does not compile as a standalone Rust 2021 library"}
        remaining = _remaining(deadline)
        if remaining <= 0:
            return {**base, "status": "UNKNOWN", "detail": "analysis exceeded its time budget before Kani ran"}
        run = proc.run(
            [str(kani_bin), str(generated_path), "--default-unwind", str(unwind),
             "-Z", "unstable-options", "--export-json", str(result_path),
             "-Z", "concrete-playback", "--concrete-playback=print"],
            cwd=workdir, env=env,
            stdout_path=workdir / "kani.out", stderr_path=workdir / "kani.err",
            timeout_seconds=remaining,
            should_cancel=lambda: _output_over_limit(workdir / "kani.out", workdir / "kani.err", result_path),
        )
        if run.launch_error:
            return {**base, "status": "UNAVAILABLE", "detail": "the pinned Kani executable could not start"}
        if run.timed_out or run.cancelled:
            return {**base, "status": "UNKNOWN", "detail": "Kani proof exceeded its time budget"}
        stdout = _read_output(workdir / "kani.out")
        stderr = _read_output(workdir / "kani.err")
        if stdout is None or stderr is None or _file_size(result_path) is None \
                or _file_size(result_path) > MAX_OUTPUT_BYTES:
            return {**base, "status": "UNKNOWN", "detail": "Kani output exceeded its byte limit"}
        try:
            result_doc = json.loads(result_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            checks = _parse_result(result_doc, function)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            return {**base, "status": "UNKNOWN", "detail": "Kani returned malformed or unsupported structured results"}
        base["checks"] = checks
        if any(item["status"] == "Undetermined"
               or (item["category"] == "unwind" and item["status"] != "Success") for item in checks):
            return {**base, "status": "UNKNOWN", "detail": "Kani reported an unfinished property or incomplete loop unwinding"}
        failures = [item for item in checks if item["status"] == "Failure"]
        if not failures:
            if run.code == 0 and all(item["status"] == "Success" for item in checks):
                return {**base, "status": "SAFE", "detail": None}
            return {**base, "status": "UNKNOWN", "detail": "Kani did not complete every fixed property"}
        if run.code != 1:
            return {**base, "status": "UNKNOWN", "detail": "Kani exit status did not match its reported properties"}
        replay = _replay_failure(stdout, model, unwind, failures)
        if replay is None:
            return {**base, "status": "UNKNOWN", "detail": "Kani failure did not have a concrete witness independently reproduced by the typed IR"}
        return {**base, "status": "COUNTEREXAMPLE", "counterexample": replay, "detail": None}


def _source_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\0" in relative:
        raise ValueError("path must be a nonempty repository-relative Rust source path")
    candidate = Path(relative)
    windows = PureWindowsPath(relative)
    if candidate.is_absolute() or windows.is_absolute() or windows.drive \
            or ".." in candidate.parts or ".." in windows.parts:
        raise ValueError("path must stay inside the repository")
    base = Path(root).resolve(strict=True)
    target = base / candidate
    current = base
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Rust source path must not contain symlinks")
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise ValueError("path must identify a regular Rust source file inside the repository")
    return resolved


def _read_source(path: Path) -> bytes:
    with path.open("rb") as source:
        raw = source.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"Rust source must be at most {MAX_SOURCE_BYTES} bytes")
    raw.decode("utf-8")
    return raw


def _parser() -> Any | None:
    try:
        from importlib.metadata import version
        import tree_sitter
        import tree_sitter_rust
        if version("tree-sitter") != TREE_SITTER_VERSION or version("tree-sitter-rust") != TREE_SITTER_RUST_VERSION:
            return None
        return tree_sitter.Parser(tree_sitter.Language(tree_sitter_rust.language()))
    except (ImportError, ModuleNotFoundError, ValueError):
        return None


def _parse_function(source: bytes, function: str, parser: Any) -> _Function:
    tree = parser.parse(source)
    if tree.root_node.has_error:
        raise _Unsupported("Rust source has a parse error")
    _bounded_tree(tree.root_node)
    top = [node for node in tree.root_node.named_children if node.type not in {"line_comment", "block_comment"}]
    if len(top) != 1 or top[0].type != "function_item":
        raise _Unsupported("source must contain exactly one standalone function and no other items")
    item = top[0]
    if item.child_by_field_name("name") is None or _text(source, item.child_by_field_name("name")) != function:
        raise _Unsupported("the requested function must be the sole function definition")
    for child in item.children:
        if child.type in {"attribute_item", "visibility_modifier", "function_modifiers", "generic_parameters", "where_clause"}:
            raise _Unsupported("function attributes, visibility, generics, and modifiers are unsupported")
    params_node = item.child_by_field_name("parameters")
    if params_node is None:
        raise _Unsupported("function parameters must use a simple explicit signature")
    parameters = []
    env: dict[str, tuple[str, bool]] = {}
    for param in params_node.named_children:
        if param.type != "parameter":
            raise _Unsupported("variadic and receiver parameters are unsupported")
        pattern = param.child_by_field_name("pattern")
        type_node = param.child_by_field_name("type")
        if pattern is None or pattern.type != "identifier" or type_node is None:
            raise _Unsupported("parameters must be named scalar values with explicit types")
        name = _text(source, pattern)
        if name in env or name.startswith("__vkit_"):
            raise _Unsupported("parameter names must be unique and may not use the reserved __vkit_ prefix")
        type_name = _parse_type(source, type_node)
        env[name] = (type_name, False)
        parameters.append((name, type_name))
        if len(parameters) > MAX_PARAMETERS:
            raise _Unsupported(f"functions may have at most {MAX_PARAMETERS} scalar parameters")
    return_node = item.child_by_field_name("return_type")
    return_type = "unit" if return_node is None else _parse_type(source, return_node)
    body_node = item.child_by_field_name("body")
    if body_node is None:
        raise _Unsupported("function body is required")
    body, _ = _parse_block(source, body_node, env, return_type, 0, 0)
    if _local_count(body) > MAX_LOCALS:
        raise _Unsupported(f"functions may declare at most {MAX_LOCALS} scalar locals")
    return _Function(function, tuple(parameters), return_type, body)


def _bounded_tree(root: Any) -> None:
    stack = [(root, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise _Unsupported("Rust source exceeds the syntax-tree size or depth limit")
        stack.extend((child, depth + 1) for child in node.children)


def _local_count(statements: tuple[_Stmt, ...]) -> int:
    count = 0
    for statement in statements:
        count += statement.kind == "let"
        count += _local_count(statement.body)
        count += _local_count(statement.otherwise)
    return count


def _parse_type(source: bytes, node: Any) -> str:
    if node.type == "primitive_type":
        value = _text(source, node)
        if value in _INT_TYPES or value == "bool":
            return value
    if node.type == "unit_type":
        return "unit"
    raise _Unsupported("only u8/u16/u32/u64, i8/i16/i32/i64, bool, and unit types are supported")


def _parse_block(source: bytes, node: Any, incoming: dict[str, tuple[str, bool]],
                 return_type: str, loop_depth: int, statement_count: int,
                 *, allow_value_tail: bool = True) -> tuple[tuple[_Stmt, ...], dict[str, tuple[str, bool]]]:
    env = incoming.copy()
    output: list[_Stmt] = []
    children = [child for child in node.named_children if child.type not in {"line_comment", "block_comment"}]
    for index, child in enumerate(children):
        statement_count += 1
        if statement_count > MAX_NODES:
            raise _Unsupported("Rust source exceeds the statement limit")
        if child.type == "let_declaration":
            pattern = child.child_by_field_name("pattern")
            type_node = child.child_by_field_name("type")
            value_node = child.child_by_field_name("value")
            if pattern is None or pattern.type != "identifier" or type_node is None or value_node is None:
                raise _Unsupported("locals must be initialized named scalars with explicit types")
            name = _text(source, pattern)
            if name in env or name.startswith("__vkit_"):
                raise _Unsupported("local names must be unique and may not use the reserved __vkit_ prefix")
            type_name = _parse_type(source, type_node)
            mutable = any(c.type == "mutable_specifier" for c in child.children)
            expr = _parse_expr(source, value_node, env, type_name, loop_depth)
            env[name] = (type_name, mutable)
            output.append(_Stmt("let", name=name, type_name=type_name, mutable=mutable, expr=expr))
            continue
        if child.type == "expression_statement":
            parts = child.named_children
            if len(parts) != 1:
                raise _Unsupported("expression statements must contain exactly one expression")
            expr_node = parts[0]
            has_semicolon = any(child.type == ";" for child in child.children)
            if index == len(children) - 1 and allow_value_tail and not has_semicolon \
                    and expr_node.type == "if_expression":
                parsed, _ = _parse_control(source, expr_node, env, return_type, loop_depth,
                                           statement_count, branch_tail=True)
                output.extend(parsed)
                continue
            if index == len(children) - 1 and allow_value_tail and not has_semicolon \
                    and expr_node.type in {"while_expression", "loop_expression"}:
                if return_type != "unit":
                    raise _Unsupported("a loop tail expression is supported only for unit-returning functions")
                parsed, _ = _parse_control(source, expr_node, env, return_type, loop_depth, statement_count)
                output.extend(parsed)
                continue
            stmt, env = _parse_statement_expr(source, expr_node, env, return_type, loop_depth, statement_count)
            output.extend(stmt)
            continue
        if child.type in {"return_expression", "break_expression", "continue_expression"}:
            stmt, env = _parse_control(source, child, env, return_type, loop_depth, statement_count)
            output.extend(stmt)
            continue
        if index == len(children) - 1:
            # Rust's block tail expression is a function return in this closed subset.
            if child.type == "if_expression" and allow_value_tail:
                parsed, _ = _parse_control(source, child, env, return_type, loop_depth,
                                           statement_count, branch_tail=True)
                output.extend(parsed)
                continue
            if child.type in {"while_expression", "loop_expression"} and allow_value_tail:
                if return_type != "unit":
                    raise _Unsupported("a loop tail expression is supported only for unit-returning functions")
                parsed, _ = _parse_control(source, child, env, return_type, loop_depth, statement_count)
                output.extend(parsed)
                continue
            if not allow_value_tail:
                raise _Unsupported("value-tail expressions in control-flow bodies are unsupported")
            expr = _parse_expr(source, child, env, return_type, loop_depth)
            output.append(_Stmt("return", expr=expr))
            continue
        raise _Unsupported(f"unsupported Rust statement: {child.type}")
    return tuple(output), env


def _parse_statement_expr(source: bytes, node: Any, env: dict[str, tuple[str, bool]],
                          return_type: str, loop_depth: int,
                          statement_count: int) -> tuple[tuple[_Stmt, ...], dict[str, tuple[str, bool]]]:
    if node.type in {"if_expression", "while_expression", "loop_expression"}:
        return _parse_control(source, node, env, return_type, loop_depth, statement_count)
    if node.type in {"assignment_expression", "compound_assignment_expr"}:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or left.type != "identifier" or right is None:
            raise _Unsupported("assignments must target a named scalar")
        name = _text(source, left)
        if name not in env or not env[name][1]:
            raise _Unsupported("assignments require a mutable local variable")
        op = _operator(node, source)
        if op not in _ASSIGNMENT_OPS or (node.type == "assignment_expression" and op != "=") \
                or (node.type == "compound_assignment_expr" and op == "="):
            raise _Unsupported("assignment operator is unsupported")
        expr = _parse_expr(source, right, env, env[name][0], loop_depth)
        return (_Stmt("assign", name=name, type_name=env[name][0], op=op, expr=expr),), env
    if node.type == "return_expression":
        return _parse_control(source, node, env, return_type, loop_depth, statement_count)
    if node.type in {"break_expression", "continue_expression"}:
        return _parse_control(source, node, env, return_type, loop_depth, statement_count)
    expr = _parse_expr(source, node, env, None, loop_depth)
    if expr.type_name != "unit":
        raise _Unsupported("standalone value expressions are unsupported; use them in an assignment or return")
    return (_Stmt("eval", expr=expr),), env


def _parse_control(source: bytes, node: Any, env: dict[str, tuple[str, bool]],
                   return_type: str, loop_depth: int,
                   statement_count: int, *,
                   branch_tail: bool = False) -> tuple[tuple[_Stmt, ...], dict[str, tuple[str, bool]]]:
    if node.type == "return_expression":
        value_node = node.child_by_field_name("value")
        if value_node is None:
            if return_type != "unit":
                raise _Unsupported("return without a value requires a unit return type")
            return (_Stmt("return"),), env
        if return_type == "unit":
            raise _Unsupported("a unit function cannot return a value")
        expr = _parse_expr(source, value_node, env, return_type, loop_depth)
        return (_Stmt("return", expr=expr),), env
    if node.type in {"break_expression", "continue_expression"}:
        if loop_depth == 0:
            raise _Unsupported("break and continue are supported only inside a loop")
        if node.named_children:
            raise _Unsupported("break values and labeled loop control are unsupported")
        return (_Stmt("break" if node.type == "break_expression" else "continue"),), env
    if node.type == "if_expression":
        condition_node = node.child_by_field_name("condition")
        consequence = node.child_by_field_name("consequence")
        alternative = node.child_by_field_name("alternative")
        if condition_node is None or consequence is None or consequence.type != "block":
            raise _Unsupported("if statements require a boolean condition and a block body")
        condition = _parse_expr(source, condition_node, env, "bool", loop_depth)
        body, _ = _parse_block(source, consequence, env, return_type, loop_depth, statement_count,
                               allow_value_tail=branch_tail)
        other: tuple[_Stmt, ...] = ()
        if alternative is not None:
            if alternative.type == "else_clause":
                clauses = alternative.named_children
                if len(clauses) != 1:
                    raise _Unsupported("else branches must contain one block or nested if")
                alternative = clauses[0]
            if alternative.type == "block":
                other, _ = _parse_block(source, alternative, env, return_type, loop_depth, statement_count,
                                        allow_value_tail=branch_tail)
            elif alternative.type == "if_expression":
                parsed, _ = _parse_control(source, alternative, env, return_type, loop_depth, statement_count,
                                           branch_tail=branch_tail)
                other = parsed
            else:
                raise _Unsupported("else branches must be blocks or nested if statements")
        elif branch_tail and return_type != "unit":
            raise _Unsupported("a value-returning tail if expression requires an else branch")
        return (_Stmt("if", condition=condition, body=body, otherwise=other),), env
    if node.type == "while_expression":
        if loop_depth >= MAX_LOOP_DEPTH:
            raise _Unsupported("loop nesting exceeds its limit")
        condition_node = node.child_by_field_name("condition")
        body_node = node.child_by_field_name("body")
        if any(child.type == "label" for child in node.named_children):
            raise _Unsupported("labeled loops are unsupported")
        if condition_node is None or body_node is None or body_node.type != "block":
            raise _Unsupported("while loops require a boolean condition and a block body")
        condition = _parse_expr(source, condition_node, env, "bool", loop_depth)
        body, _ = _parse_block(source, body_node, env, return_type, loop_depth + 1, statement_count,
                               allow_value_tail=False)
        return (_Stmt("while", condition=condition, body=body),), env
    if node.type == "loop_expression":
        if loop_depth >= MAX_LOOP_DEPTH:
            raise _Unsupported("loop nesting exceeds its limit")
        body_node = node.child_by_field_name("body")
        if any(child.type == "label" for child in node.named_children):
            raise _Unsupported("labeled loops are unsupported")
        if body_node is None:
            raise _Unsupported("loop body is required")
        body, _ = _parse_block(source, body_node, env, return_type, loop_depth + 1, statement_count,
                               allow_value_tail=False)
        return (_Stmt("loop", body=body),), env
    raise _Unsupported(f"unsupported Rust control statement: {node.type}")


def _parse_expr(source: bytes, node: Any, env: dict[str, tuple[str, bool]],
                expected: str | None, loop_depth: int) -> _Expr:
    kind = node.type
    if kind == "parenthesized_expression":
        parts = node.named_children
        if len(parts) != 1:
            raise _Unsupported("parenthesized expressions must contain one expression")
        return _parse_expr(source, parts[0], env, expected, loop_depth)
    if kind == "identifier":
        name = _text(source, node)
        if name not in env:
            raise _Unsupported(f"identifier {name!r} is not a visible scalar variable")
        type_name = env[name][0]
        _expect_type(type_name, expected)
        return _Expr("var", type_name, name)
    if kind == "boolean_literal":
        value = _text(source, node) == "true"
        _expect_type("bool", expected)
        return _Expr("bool", "bool", value)
    if kind == "integer_literal":
        raw = _text(source, node)
        match = _DECIMAL.fullmatch(raw)
        if not match or not match.group(1):
            raise _Unsupported("only decimal integer literals with optional explicit primitive suffixes are supported")
        digits = int(match.group(1).replace("_", ""))
        suffix = match.group(2)
        type_name = suffix or expected
        if type_name not in _INT_TYPES:
            raise _Unsupported("an unsuffixed integer literal needs an explicit scalar type context")
        _expect_type(type_name, expected)
        bits, signed = _INT_TYPES[type_name]
        maximum = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
        if digits > maximum:
            raise _Unsupported("integer literal is outside its declared primitive type")
        return _Expr("int", type_name, digits)
    if kind == "unary_expression":
        op = _operator(node, source)
        operand = node.named_children[-1]
        if op == "!":
            operand_type = _known_type(source, operand, env) or expected
            if operand_type not in {*_INT_TYPES, "bool"}:
                raise _Unsupported("boolean or integer not requires an explicit primitive operand type")
            value = _parse_expr(source, operand, env, operand_type, loop_depth)
            return _Expr("unary", operand_type, op, (value,))
        if op == "-":
            if operand.type == "integer_literal":
                raw = _text(source, operand)
                match = _DECIMAL.fullmatch(raw)
                if not match or not match.group(1):
                    raise _Unsupported("only decimal integer literals are supported")
                digits = int(match.group(1).replace("_", ""))
                type_name = match.group(2) or expected
                if type_name not in _INT_TYPES:
                    raise _Unsupported("a negative integer literal needs an explicit signed integer type")
                _expect_type(type_name, expected)
                bits, signed = _INT_TYPES[type_name]
                if signed and digits == 1 << (bits - 1):
                    return _Expr("int", type_name, -digits)
            value = _parse_expr(source, operand, env, expected, loop_depth)
            if value.type_name not in _INT_TYPES:
                raise _Unsupported("unary minus requires a primitive integer")
            return _Expr("unary", value.type_name, op, (value,))
        raise _Unsupported("only unary minus and boolean/integer not are supported")
    if kind == "binary_expression":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            raise _Unsupported("malformed binary expression")
        op = _operator(node, source)
        if op not in _BINARY_OPS:
            raise _Unsupported("binary operator is unsupported")
        if op in {"&&", "||"}:
            lhs = _parse_expr(source, left, env, "bool", loop_depth)
            rhs = _parse_expr(source, right, env, "bool", loop_depth)
            return _Expr("binary", "bool", op, (lhs, rhs))
        left_hint = _known_type(source, left, env)
        right_hint = _known_type(source, right, env)
        if op in {"<<", ">>"}:
            lhs_type = left_hint or expected
            if lhs_type not in _INT_TYPES:
                raise _Unsupported("shift left operand requires an explicit primitive integer type")
            lhs = _parse_expr(source, left, env, lhs_type, loop_depth)
            rhs_type = right_hint
            if rhs_type is None:
                # Rust's fallback type for an unconstrained integer shift count is i32.
                rhs_type = "i32"
            if rhs_type not in _INT_TYPES:
                raise _Unsupported("shift count must be a primitive integer")
            rhs = _parse_expr(source, right, env, rhs_type, loop_depth)
            literal_shift = _literal_value(rhs)
            if literal_shift is not None and (literal_shift < 0 or literal_shift >= _INT_TYPES[lhs_type][0]):
                raise _Unsupported("a literal shift count is outside the Rust primitive width")
            return _Expr("binary", lhs_type, op, (lhs, rhs))
        operand_type = left_hint or right_hint or (expected if op in _ARITHMETIC else None)
        if operand_type is None:
            raise _Unsupported("binary expression needs a primitive integer type context")
        lhs = _parse_expr(source, left, env, operand_type, loop_depth)
        rhs = _parse_expr(source, right, env, operand_type, loop_depth)
        if lhs.type_name != rhs.type_name:
            raise _Unsupported("binary operands must have the same primitive type")
        if op in {"==", "!=", "<", "<=", ">", ">="}:
            _expect_type("bool", expected)
            if op not in {"==", "!="} and lhs.type_name == "bool":
                raise _Unsupported("ordered comparison is only supported for integers")
            return _Expr("binary", "bool", op, (lhs, rhs))
        if lhs.type_name not in _INT_TYPES or op not in _BINARY_OPS:
            raise _Unsupported("arithmetic operators require primitive integers")
        _expect_type(lhs.type_name, expected)
        return _Expr("binary", lhs.type_name, op, (lhs, rhs))
    raise _Unsupported(f"unsupported Rust expression: {kind}")


def _known_type(source: bytes, node: Any, env: dict[str, tuple[str, bool]]) -> str | None:
    if node.type == "identifier":
        found = env.get(_text(source, node))
        return found[0] if found else None
    if node.type == "boolean_literal":
        return "bool"
    if node.type == "integer_literal":
        match = _DECIMAL.fullmatch(_text(source, node))
        return match.group(2) if match else None
    if node.type == "parenthesized_expression" and len(node.named_children) == 1:
        return _known_type(source, node.named_children[0], env)
    if node.type == "unary_expression" and node.named_children:
        return _known_type(source, node.named_children[-1], env)
    return None


def _literal_value(expression: _Expr) -> int | None:
    if expression.kind == "int":
        return expression.value
    if expression.kind == "unary" and expression.value == "-" \
            and expression.children[0].kind == "int":
        return -expression.children[0].value
    return None


def _expect_type(found: str, expected: str | None) -> None:
    if expected is not None and found != expected:
        raise _Unsupported(f"expected {expected}, found {found}")


def _operator(node: Any, source: bytes) -> str:
    for child in node.children:
        if not child.is_named and child.type.strip() in _BINARY_OPS | _ASSIGNMENT_OPS | {"!", "-", "~"}:
            return child.type.strip()
    raise _Unsupported(f"unsupported operator in {node.type}")


def _text(source: bytes, node: Any) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8")


def _generate(function: _Function) -> bytes:
    lines = ["mod __vkit_model {", _function_source(function, 1), "}", "#[kani::proof]",
             "fn __vkit_harness() {"]
    for index, (_, type_name) in enumerate(function.parameters):
        lines.append(f"    let __vkit_arg_{index}: {type_name} = kani::any();")
    arguments = ", ".join(f"__vkit_arg_{index}" for index in range(len(function.parameters)))
    lines.append(f"    let _ = __vkit_model::{function.name}({arguments});")
    lines.extend(["}", ""])
    return "\n".join(lines).encode("utf-8")


def _function_source(function: _Function, indent: int) -> str:
    pad = "    " * indent
    parameters = ", ".join(f"{name}: {type_name}" for name, type_name in function.parameters)
    return_type = "" if function.return_type == "unit" else f" -> {function.return_type}"
    lines = [f"{pad}pub(super) fn {function.name}({parameters}){return_type} {{"]
    lines.extend(_statement_source(statement, indent + 1) for statement in function.body)
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def _statement_source(statement: _Stmt, indent: int) -> str:
    pad = "    " * indent
    if statement.kind == "let":
        mut = "mut " if statement.mutable else ""
        return f"{pad}let {mut}{statement.name}: {statement.type_name} = {_expr_source(statement.expr)};"
    if statement.kind == "assign":
        return f"{pad}{statement.name} {statement.op} {_expr_source(statement.expr)};"
    if statement.kind == "eval":
        return f"{pad}{_expr_source(statement.expr)};"
    if statement.kind == "return":
        return f"{pad}return;" if statement.expr is None else f"{pad}return {_expr_source(statement.expr)};"
    if statement.kind == "break":
        return f"{pad}break;"
    if statement.kind == "continue":
        return f"{pad}continue;"
    if statement.kind in {"if", "while"}:
        keyword = "if" if statement.kind == "if" else "while"
        lines = [f"{pad}{keyword} {_expr_source(statement.condition)} {{"]
        lines.extend(_statement_source(item, indent + 1) for item in statement.body)
        if statement.otherwise:
            lines.append(f"{pad}}} else {{")
            lines.extend(_statement_source(item, indent + 1) for item in statement.otherwise)
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if statement.kind == "loop":
        lines = [f"{pad}loop {{"]
        lines.extend(_statement_source(item, indent + 1) for item in statement.body)
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    raise ValueError("unknown typed statement")


def _expr_source(expression: _Expr | None) -> str:
    if expression is None:
        raise ValueError("missing typed expression")
    if expression.kind == "var":
        return str(expression.value)
    if expression.kind == "bool":
        return "true" if expression.value else "false"
    if expression.kind == "int":
        if expression.value < 0:
            return f"{expression.type_name}::MIN"
        return f"{expression.value}{expression.type_name}"
    if expression.kind == "unary":
        return f"({expression.value}{_expr_source(expression.children[0])})"
    if expression.kind == "binary":
        return f"({_expr_source(expression.children[0])} {expression.value} {_expr_source(expression.children[1])})"
    raise ValueError("unknown typed expression")


def _parse_result(document: Any, function: str) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("metadata"), dict) \
            or document["metadata"].get("kani_version") != KANI_VERSION:
        raise ValueError("wrong Kani result version")
    if document.get("metadata", {}).get("target") != "x86_64-unknown-linux-gnu":
        raise ValueError("wrong Kani result target")
    tools = document.get("tools")
    if not isinstance(tools, dict) or tools.get("cbmc") != f"{CBMC_VERSION} (cbmc-{CBMC_VERSION})" \
            or tools.get("rustc") != RUSTC_VERSION:
        raise ValueError("wrong Kani result tools")
    verification = document.get("verification_results")
    if not isinstance(verification, dict):
        raise ValueError("Kani omitted verification results")
    results = verification.get("results")
    summary = verification.get("summary")
    if not isinstance(summary, dict) or summary.get("status") != "completed" \
            or summary.get("total_harnesses") != 1 or summary.get("executed") != 1 \
            or not isinstance(results, list) or len(results) != 1:
        raise ValueError("Kani did not complete exactly the generated harness")
    harness = results[0]
    if not isinstance(harness, dict) or harness.get("harness_id") != "__vkit_harness":
        raise ValueError("unexpected Kani harness result")
    checks = harness.get("checks")
    if not isinstance(checks, list):
        raise ValueError("Kani returned malformed safety checks")
    output = []
    for check in checks:
        if not isinstance(check, dict) or check.get("function") != f"__vkit_model::{function}":
            raise ValueError("Kani returned a check outside the selected function")
        status = check.get("status")
        category = check.get("category")
        description = check.get("description")
        if status not in {"Success", "Failure", "Undetermined"} or category not in {"assertion", "unwind"} \
                or not isinstance(description, str) or len(description) > 256:
            raise ValueError("Kani returned an unsupported check record")
        output.append({"description": description, "status": status, "category": category})
    failed = sum(item["status"] == "Failure" for item in output)
    if harness.get("status") != ("Failure" if failed else "Success") \
            or summary.get("successful") != (0 if failed else 1) \
            or summary.get("failed") != (1 if failed else 0):
        raise ValueError("Kani harness summary did not match its checks")
    return output


def _replay_failure(stdout: str, function: _Function, unwind: int,
                    failures: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(failures) != 1 or failures[0]["category"] != "assertion":
        return None
    description = failures[0]["description"]
    expected_kind = _CHECK_KINDS.get(description)
    if expected_kind is None:
        return None
    try:
        inputs = _playback_inputs(stdout, function)
        _interpret(function, inputs, unwind)
    except _ReplayFailure as exc:
        if exc.kind != expected_kind:
            return None
        return {"check": description,
                "inputs": [{"name": name, "type": type_name, "value": value}
                           for (name, type_name), value in zip(function.parameters, inputs)]}
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None
    return None


def _playback_inputs(stdout: str, function: _Function) -> list[Any]:
    marker = f"Concrete playback unit test for `__vkit_harness`:\n"
    if stdout.count(marker) != 1:
        raise ValueError("expected exactly one concrete playback")
    block = stdout.split(marker, 1)[1]
    declaration = re.search(r"let concrete_vals: Vec<Vec<u8>> = vec!\[(.*?)\];\s*kani::concrete_playback_run\(concrete_vals, __vkit_harness\);",
                            block, flags=re.DOTALL)
    if declaration is None:
        raise ValueError("concrete playback format changed")
    body = re.sub(r"(?m)^\s*//[^\n]*(?:\n|$)", "", declaration.group(1))
    vectors = re.findall(r"vec!\[([0-9,\s]*)\]\s*,", body)
    residual = re.sub(r"vec!\[[0-9,\s]*\]\s*,", "", body)
    if re.sub(r"[\s,]", "", residual) or len(vectors) != len(function.parameters):
        raise ValueError("concrete input vectors do not match the function signature")
    values = []
    for vector, (_, type_name) in zip(vectors, function.parameters):
        octets = [int(value) for value in vector.split(",") if value.strip()]
        if type_name == "bool":
            if octets not in ([0], [1]):
                raise ValueError("invalid concrete bool encoding")
            values.append(bool(octets[0]))
            continue
        bits, signed = _INT_TYPES[type_name]
        if len(octets) != bits // 8 or any(value < 0 or value > 255 for value in octets):
            raise ValueError("concrete integer encoding has the wrong width")
        value = int.from_bytes(bytes(octets), "little", signed=False)
        if signed and value >= 1 << (bits - 1):
            value -= 1 << bits
        values.append(value)
    return values


def _interpret(function: _Function, values: list[Any], unwind: int) -> Any:
    if len(values) != len(function.parameters):
        raise ValueError("wrong input count")
    env = {name: value for (name, _), value in zip(function.parameters, values)}
    try:
        _execute(function.body, env, unwind, 0)
    except _Return as result:
        return result.value
    return None


def _execute(statements: tuple[_Stmt, ...], env: dict[str, Any], unwind: int, loop_depth: int) -> None:
    for statement in statements:
        if statement.kind == "let":
            env[statement.name] = _evaluate(statement.expr, env)
        elif statement.kind == "assign":
            left = env[statement.name]
            right = _evaluate(statement.expr, env)
            op = statement.op[:-1] if statement.op != "=" else "="
            env[statement.name] = right if op == "=" else _binary(op, left, right, statement.type_name)
        elif statement.kind == "eval":
            _evaluate(statement.expr, env)
        elif statement.kind == "return":
            raise _Return(None if statement.expr is None else _evaluate(statement.expr, env))
        elif statement.kind == "break":
            raise _Break()
        elif statement.kind == "continue":
            raise _Continue()
        elif statement.kind == "if":
            _execute(statement.body if _evaluate(statement.condition, env) else statement.otherwise,
                     env, unwind, loop_depth)
        elif statement.kind in {"while", "loop"}:
            if loop_depth >= MAX_LOOP_DEPTH:
                raise ValueError("replay exceeded the selected loop unwind")
            for _ in range(unwind):
                if statement.kind == "while" and not _evaluate(statement.condition, env):
                    break
                try:
                    _execute(statement.body, env, unwind, loop_depth + 1)
                except _Continue:
                    continue
                except _Break:
                    break
            else:
                if statement.kind == "loop" or _evaluate(statement.condition, env):
                    raise ValueError("replay reached the unwind boundary")
        else:
            raise ValueError("unsupported typed statement")


def _evaluate(expression: _Expr | None, env: dict[str, Any]) -> Any:
    if expression is None:
        raise ValueError("missing expression")
    if expression.kind == "var":
        return env[str(expression.value)]
    if expression.kind in {"int", "bool"}:
        return expression.value
    if expression.kind == "unary":
        value = _evaluate(expression.children[0], env)
        if expression.value == "!":
            if expression.type_name == "bool":
                return not value
            bits, signed = _INT_TYPES[expression.type_name]
            return _signed_value((~_unsigned(value, bits)) & ((1 << bits) - 1), bits, signed)
        if expression.value == "-":
            return _checked(-value, expression.type_name, "overflow")
        raise ValueError("unknown unary operator")
    if expression.kind == "binary":
        left = _evaluate(expression.children[0], env)
        if expression.value == "&&" and not left:
            return False
        if expression.value == "||" and left:
            return True
        right = _evaluate(expression.children[1], env)
        return _binary(expression.value, left, right, expression.type_name)
    raise ValueError("unknown expression kind")


def _binary(op: str, left: Any, right: Any, type_name: str) -> Any:
    if op == "&&":
        return bool(left and right)
    if op == "||":
        return bool(left or right)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if type_name not in _INT_TYPES:
        raise ValueError("integer operation has a non-integer type")
    if op in {"/", "%"}:
        if right == 0:
            raise _ReplayFailure("division-by-zero")
        bits, signed = _INT_TYPES[type_name]
        minimum = -(1 << (bits - 1)) if signed else 0
        if signed and left == minimum and right == -1:
            raise _ReplayFailure("overflow")
        quotient = abs(left) // abs(right)
        if (left < 0) != (right < 0):
            quotient = -quotient
        return quotient if op == "/" else left - quotient * right
    if op in {"<<", ">>"}:
        bits, signed = _INT_TYPES[type_name]
        if right < 0 or right >= bits:
            raise _ReplayFailure("invalid-shift")
        if op == ">>":
            return left >> right if signed else _unsigned(left, bits) >> right
        shifted = _unsigned(left, bits) << right
        return _signed_value(shifted & ((1 << bits) - 1), bits, signed)
    if op == "&":
        bits, signed = _INT_TYPES[type_name]
        return _signed_value(_unsigned(left, bits) & _unsigned(right, bits), bits, signed)
    if op == "|":
        bits, signed = _INT_TYPES[type_name]
        return _signed_value(_unsigned(left, bits) | _unsigned(right, bits), bits, signed)
    if op == "^":
        bits, signed = _INT_TYPES[type_name]
        return _signed_value(_unsigned(left, bits) ^ _unsigned(right, bits), bits, signed)
    result = {"+": lambda: left + right,
              "-": lambda: left - right,
              "*": lambda: left * right}.get(op)
    if result is None:
        raise ValueError("unknown integer operator")
    return _checked(result(), type_name, "overflow")


def _checked(value: int, type_name: str, kind: str) -> int:
    bits, signed = _INT_TYPES[type_name]
    lower = -(1 << (bits - 1)) if signed else 0
    upper = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    if value < lower or value > upper:
        raise _ReplayFailure(kind)
    return value


def _unsigned(value: int, bits: int) -> int:
    return value & ((1 << bits) - 1)


def _signed_value(value: int, bits: int, signed: bool) -> int:
    if signed and value >= 1 << (bits - 1):
        return value - (1 << bits)
    return value


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _output_over_limit(*paths: Path) -> bool:
    total = 0
    for path in paths:
        size = _file_size(path)
        if size is None:
            if path.exists():
                return True
        else:
            total += size
    return total > MAX_OUTPUT_BYTES


def _sha256_file(path: Path) -> str | None:
    try:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError:
        return None


def _read_output(path: Path) -> str | None:
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_OUTPUT_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_OUTPUT_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paired_profiles import U32Binding
from . import proc


class UnsupportedRustSource(ValueError):
    """The captured Rust source is outside the admitted scalar profile."""


_PROFILE_VERSION = "rust-u32-pure-v1"
_SOURCE_LIMIT = 64 * 1024
_NODE_LIMIT = 3000
_DEPTH_LIMIT = 64
_EXPECTED_CORE_VERSION = "0.26.0"
_EXPECTED_GRAMMAR_VERSION = "0.24.2"
_EXPECTED_GRAMMAR_ABI = 15
_PARSER_TIMEOUT_SECONDS = 10
_PARSER_MAX_OUTPUT = 64 * 1024
_PARSER_CHILD = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from vkit.paired_profiles import parse_binding
from vkit.rust_source import _admit_rust_source, UnsupportedRustSource
try:
    profile = _admit_rust_source(Path('source.rs').read_bytes(), parse_binding(json.loads(sys.argv[2])))
    print(json.dumps({'profile': profile.to_json()}))
except UnsupportedRustSource as exc:
    print(json.dumps({'unsupported': str(exc)}))
"""
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DECIMAL_U32 = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_U32_MAX = (1 << 32) - 1
_COMMENTS = frozenset({"line_comment", "block_comment"})
_NODE_TYPES = frozenset(
    {
        "source_file",
        "function_item",
        "visibility_modifier",
        "identifier",
        "parameters",
        "parameter",
        "primitive_type",
        "block",
        "let_declaration",
        "expression_statement",
        "if_expression",
        "else_clause",
        "binary_expression",
        "unary_expression",
        "return_expression",
        "integer_literal",
        "boolean_literal",
        *_COMMENTS,
    }
)
_TOKEN_TYPES = frozenset(
    {
        "pub",
        "fn",
        "(",
        ")",
        ":",
        "->",
        "{",
        "}",
        "let",
        "=",
        ";",
        "if",
        "else",
        "return",
        "+",
        "-",
        "*",
        "==",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "&&",
        "||",
        "!",
    }
)
_BINARY_OPERATORS = frozenset(
    {"+", "-", "*", "==", "!=", "<", "<=", ">", ">=", "&&", "||"}
)


@dataclass(frozen=True, slots=True)
class RustSourceProfile:
    """Admission identity for one captured function and one pinned parser build."""

    source_sha256: str
    _binding: U32Binding = field(repr=False)
    _parser_identity: tuple[tuple[str, str], ...] = field(repr=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "restricted_rust_u32_source",
            "profile_version": _PROFILE_VERSION,
            "source_sha256": self.source_sha256,
            "binding": {
                "language": self._binding.language,
                "source": self._binding.source,
                "symbol": self._binding.symbol,
            },
            "parser_identity": dict(self._parser_identity),
        }


def _sha256_file(path: str | Path, label: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise UnsupportedRustSource(f"cannot identify {label}: {exc}") from exc


def _parser_runtime() -> tuple[Any, Any, Any, tuple[tuple[str, str], ...]]:
    try:
        core = importlib.import_module("tree_sitter")
        grammar = importlib.import_module("tree_sitter_rust")
        core_binding = importlib.import_module("tree_sitter._binding")
        grammar_binding = importlib.import_module("tree_sitter_rust._binding")
        from tree_sitter import Language as TreeSitterLanguage
        from tree_sitter import Parser
    except (ImportError, ModuleNotFoundError) as exc:
        raise UnsupportedRustSource(
            "Tree-sitter Rust parser is unavailable; install the pinned rust extra"
        ) from exc

    try:
        core_version = importlib.metadata.version("tree-sitter")
        grammar_version = importlib.metadata.version("tree-sitter-rust")
        language = TreeSitterLanguage(grammar.language())
        abi_version = language.abi_version
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        raise UnsupportedRustSource(
            f"Tree-sitter parser API is incompatible: {exc}"
        ) from exc

    if core_version != _EXPECTED_CORE_VERSION:
        raise UnsupportedRustSource(
            f"unsupported tree-sitter version: expected {_EXPECTED_CORE_VERSION}, got {core_version}"
        )
    if grammar_version != _EXPECTED_GRAMMAR_VERSION:
        raise UnsupportedRustSource(
            f"unsupported tree-sitter-rust version: expected {_EXPECTED_GRAMMAR_VERSION}, got {grammar_version}"
        )
    if abi_version != _EXPECTED_GRAMMAR_ABI:
        raise UnsupportedRustSource(
            f"unsupported Rust grammar ABI: expected {_EXPECTED_GRAMMAR_ABI}, got {abi_version}"
        )

    identity = (
        ("tree_sitter_core_version", core_version),
        ("tree_sitter_grammar_version", grammar_version),
        ("tree_sitter_runtime_abi_max", str(core.LANGUAGE_VERSION)),
        ("tree_sitter_runtime_abi_min", str(core.MIN_COMPATIBLE_LANGUAGE_VERSION)),
        ("tree_sitter_grammar_abi", str(abi_version)),
        ("python_implementation", sys.implementation.name),
        ("python_version", ".".join(map(str, sys.version_info[:3]))),
        (
            "tree_sitter_python_sha256",
            _sha256_file(core.__file__, "Tree-sitter Python module"),
        ),
        (
            "tree_sitter_native_sha256",
            _sha256_file(core_binding.__file__, "Tree-sitter native module"),
        ),
        (
            "tree_sitter_rust_python_sha256",
            _sha256_file(grammar.__file__, "Rust grammar Python module"),
        ),
        (
            "tree_sitter_rust_native_sha256",
            _sha256_file(grammar_binding.__file__, "Rust grammar native module"),
        ),
        (
            "profile_policy_code_sha256",
            _sha256_file(__file__, "Rust source profile code"),
        ),
    )
    return TreeSitterLanguage, Parser, language, identity


def _significant_children(node: Any) -> list[Any]:
    return [child for child in node.children if child.type not in _COMMENTS]


def _node_text(node: Any) -> str:
    try:
        return node.text.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedRustSource("Rust source contains invalid UTF-8") from exc


def _check_tree(tree: Any) -> Any:
    root = tree.root_node
    if root.has_error:
        raise UnsupportedRustSource("Rust source has a syntax error or missing syntax")
    stack = [(root, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > _NODE_LIMIT:
            raise UnsupportedRustSource("Rust source exceeds the AST node limit")
        if depth > _DEPTH_LIMIT:
            raise UnsupportedRustSource("Rust source exceeds the AST depth limit")
        if node.is_error or node.is_missing:
            raise UnsupportedRustSource("Rust source has an error or missing AST node")
        if node.type == "attribute_item":
            raise UnsupportedRustSource("Rust attributes are not admitted")
        if node.type in _COMMENTS:
            text = _node_text(node)
            if text.startswith(("///", "//!")) and not text.startswith("////"):
                raise UnsupportedRustSource(
                    "Rust documentation comments are not admitted"
                )
            if text.startswith(("/**", "/*!")) and not text.startswith("/***"):
                raise UnsupportedRustSource(
                    "Rust documentation comments are not admitted"
                )
            continue
        elif node.is_named:
            if node.type not in _NODE_TYPES:
                raise UnsupportedRustSource(
                    f"unsupported Rust syntax node: {node.type}"
                )
        elif node.type not in _TOKEN_TYPES:
            raise UnsupportedRustSource(f"unsupported Rust syntax token: {node.type}")
        stack.extend((child, depth + 1) for child in reversed(node.children))
    return root


def _require_children(node: Any, expected: tuple[str, ...], label: str) -> list[Any]:
    children = _significant_children(node)
    actual = tuple(child.type for child in children)
    if actual != expected:
        raise UnsupportedRustSource(
            f"unsupported {label} shape: expected {expected}, got {actual}"
        )
    return children


def _primitive(node: Any, allowed: frozenset[str], label: str) -> str:
    if node.type != "primitive_type":
        raise UnsupportedRustSource(f"{label} must use a primitive type")
    value = _node_text(node)
    if value not in allowed:
        raise UnsupportedRustSource(f"{label} type is not admitted: {value}")
    return value


def _identifier(node: Any, label: str) -> str:
    if node.type != "identifier":
        raise UnsupportedRustSource(f"{label} must be a plain ASCII identifier")
    name = _node_text(node)
    if _IDENTIFIER.fullmatch(name) is None:
        raise UnsupportedRustSource(f"{label} must be a plain ASCII identifier")
    return name


def _function(root: Any, binding: U32Binding) -> tuple[Any, str]:
    top = _significant_children(root)
    if len(top) != 1 or top[0].type != "function_item":
        raise UnsupportedRustSource(
            "Rust source must contain exactly one function item"
        )
    function = top[0]
    children = _require_children(
        function,
        (
            "visibility_modifier",
            "fn",
            "identifier",
            "parameters",
            "->",
            "primitive_type",
            "block",
        ),
        "function item",
    )
    visibility, _, name_node, parameters, _, return_type, body = children
    visibility_children = _require_children(visibility, ("pub",), "visibility")
    if len(visibility_children) != 1:
        raise UnsupportedRustSource("function must have plain public visibility")
    name = _identifier(name_node, "function name")
    if name != binding.symbol:
        raise UnsupportedRustSource(
            "Rust function name differs from the declared binding"
        )
    _primitive(return_type, frozenset({"u32"}), "function return")
    params = _require_children(parameters, ("(", "parameter", ")"), "parameter list")
    if len(params) != 3:
        raise UnsupportedRustSource("Rust function must have exactly one parameter")
    param_children = _require_children(
        params[1], ("identifier", ":", "primitive_type"), "parameter"
    )
    parameter_name = _identifier(param_children[0], "parameter name")
    if parameter_name == name:
        raise UnsupportedRustSource("parameter may not shadow the function name")
    _primitive(param_children[2], frozenset({"u32"}), "function parameter")
    return body, parameter_name


def _literal_type(node: Any) -> str:
    value = _node_text(node)
    if node.type == "integer_literal":
        if _DECIMAL_U32.fullmatch(value) is None or int(value) > _U32_MAX:
            raise UnsupportedRustSource(
                "only unsuffixed decimal u32 literals are admitted"
            )
        return "u32"
    if node.type == "boolean_literal" and value in {"true", "false"}:
        return "bool"
    raise UnsupportedRustSource(f"unsupported Rust literal: {node.type}")


def _operator(node: Any, allowed: frozenset[str]) -> str:
    value = _node_text(node)
    if not node.is_named and value in allowed:
        return value
    raise UnsupportedRustSource(f"unsupported Rust operator: {value}")


def _expression(node: Any, scope: set[str]) -> None:
    kind = node.type
    if kind == "identifier":
        name = _identifier(node, "local reference")
        if name not in scope:
            raise UnsupportedRustSource(f"unbound identifier in Rust source: {name}")
        return
    if kind in {"integer_literal", "boolean_literal"}:
        _literal_type(node)
        return
    if kind in {"binary_expression", "unary_expression"}:
        children = _significant_children(node)
        if kind == "binary_expression":
            if len(children) != 3:
                raise UnsupportedRustSource("unsupported binary expression shape")
            _expression(children[0], scope)
            _operator(children[1], _BINARY_OPERATORS)
            _expression(children[2], scope)
            return
        if len(children) != 2 or _operator(children[0], frozenset({"!"})) != "!":
            raise UnsupportedRustSource("only logical negation is admitted")
        _expression(children[1], scope)
        return
    if kind == "return_expression":
        children = _significant_children(node)
        if len(children) not in {1, 2} or children[0].type != "return":
            raise UnsupportedRustSource("unsupported return expression")
        if len(children) == 2:
            _expression(children[1], scope)
        return
    if kind == "if_expression":
        children = _significant_children(node)
        if len(children) not in {3, 4} or children[0].type != "if":
            raise UnsupportedRustSource("unsupported if-expression shape")
        _expression(children[1], scope)
        if children[2].type != "block":
            raise UnsupportedRustSource("if consequence must be a block")
        _block(children[2], scope)
        if len(children) == 4:
            clause = _significant_children(children[3])
            if len(clause) != 2 or clause[0].type != "else":
                raise UnsupportedRustSource("unsupported else clause")
            if clause[1].type == "block":
                _block(clause[1], scope)
            elif clause[1].type == "if_expression":
                _expression(clause[1], scope)
            else:
                raise UnsupportedRustSource(
                    "else branch must be a block or if expression"
                )
        return
    if kind == "block":
        _block(node, scope)
        return
    raise UnsupportedRustSource(f"unsupported Rust expression: {kind}")


def _let_binding(node: Any, scope: set[str]) -> None:
    children = _significant_children(node)
    expected = ("let", "identifier", ":", "primitive_type", "=", ";")
    if (
        len(children) != 7
        or tuple(child.type for child in children[:5]) != expected[:5]
    ):
        raise UnsupportedRustSource("unsupported local declaration shape")
    if children[-1].type != expected[-1]:
        raise UnsupportedRustSource("local declaration must end with a semicolon")
    name = _identifier(children[1], "local name")
    if name in scope:
        raise UnsupportedRustSource("local shadowing is not admitted")
    _primitive(children[3], frozenset({"u32", "bool"}), "local")
    _expression(children[5], scope)
    scope.add(name)


def _block(node: Any, outer_scope: set[str]) -> None:
    children = _significant_children(node)
    if len(children) < 2 or children[0].type != "{" or children[-1].type != "}":
        raise UnsupportedRustSource("unsupported Rust block shape")
    scope = set(outer_scope)
    body = children[1:-1]
    for index, child in enumerate(body):
        final = index == len(body) - 1
        if child.type == "let_declaration":
            _let_binding(child, scope)
        elif child.type == "expression_statement":
            statement = _significant_children(child)
            if not statement or statement[0].type not in {
                "if_expression",
                "return_expression",
            }:
                raise UnsupportedRustSource(
                    "only if and return expression statements are admitted"
                )
            if len(statement) > 2 or (len(statement) == 2 and statement[1].type != ";"):
                raise UnsupportedRustSource("unsupported expression statement")
            _expression(statement[0], scope)
        elif child.type in {
            "identifier",
            "integer_literal",
            "boolean_literal",
            "binary_expression",
            "unary_expression",
            "if_expression",
            "block",
            "return_expression",
        }:
            if not final:
                raise UnsupportedRustSource(
                    "a block expression must be the final block item"
                )
            _expression(child, scope)
        else:
            raise UnsupportedRustSource(f"unsupported Rust block item: {child.type}")


def _admit_rust_source(source: bytes, binding: U32Binding) -> RustSourceProfile:
    """Admit exact captured bytes into the closed single-function Rust source profile."""
    if type(source) is not bytes:
        raise TypeError("Rust source must be exact bytes")
    if len(source) > _SOURCE_LIMIT:
        raise UnsupportedRustSource("Rust source exceeds the 64 KiB limit")
    if not isinstance(binding, U32Binding) or binding.language != "rust":
        raise UnsupportedRustSource(
            "Rust source requires a parsed Rust u32-function binding"
        )
    if _IDENTIFIER.fullmatch(binding.symbol) is None:
        raise UnsupportedRustSource("binding symbol must be a plain ASCII identifier")
    try:
        source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedRustSource("Rust source must be valid UTF-8") from exc

    _, Parser, language, identity = _parser_runtime()
    parser = Parser(language)
    tree = parser.parse(source)
    if tree is None:
        raise UnsupportedRustSource("Rust parser timed out")
    root = _check_tree(tree)
    body, parameter_name = _function(root, binding)
    _block(body, {parameter_name})

    return RustSourceProfile(
        hashlib.sha256(source).hexdigest(),
        binding,
        (*identity, ("source_profile_version", _PROFILE_VERSION)),
    )


def admit_rust_source(source: bytes, binding: U32Binding) -> RustSourceProfile:
    if type(source) is not bytes:
        raise TypeError("Rust source must be exact bytes")
    if len(source) > _SOURCE_LIMIT:
        raise UnsupportedRustSource("Rust source exceeds the 64 KiB limit")
    if not isinstance(binding, U32Binding) or binding.language != "rust":
        raise UnsupportedRustSource("Rust source requires a parsed Rust u32-function binding")
    declaration = {"schema_version": 1, "kind": "u32_function", "language": "rust",
                   "source": binding.source, "symbol": binding.symbol}
    with tempfile.TemporaryDirectory(prefix="vkit-rust-source-") as temporary:
        directory = Path(temporary)
        (directory / "source.rs").write_bytes(source)
        stdout, stderr = directory / "stdout.json", directory / "stderr.log"
        environment = {key: os.environ[key] for key in (
            "PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
        ) if key in os.environ}
        result = proc.run(
            [sys.executable, "-I", "-c", _PARSER_CHILD, str(Path(__file__).resolve().parents[1]),
             json.dumps(declaration)], cwd=directory, stdout_path=stdout, stderr_path=stderr,
            timeout_seconds=_PARSER_TIMEOUT_SECONDS, env=environment, max_output_bytes=_PARSER_MAX_OUTPUT,
        )
        if (result.code != 0 or result.timed_out or result.cancelled or result.output_limited
                or result.launch_error):
            raise UnsupportedRustSource(f"Rust parser process did not complete: {result}")
        try:
            raw = json.loads(stdout.read_bytes())
            if isinstance(raw, dict) and set(raw) == {"unsupported"} and isinstance(raw["unsupported"], str):
                raise UnsupportedRustSource(raw["unsupported"])
            if not isinstance(raw, dict) or set(raw) != {"profile"}:
                raise ValueError("unexpected parser response")
            profile = raw["profile"]
            identity = profile["parser_identity"]
            if not isinstance(identity, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in identity.items()
            ):
                raise ValueError("invalid parser identity")
            admitted = RustSourceProfile(profile["source_sha256"], binding, tuple(sorted(identity.items())))
            if admitted.to_json() != profile or admitted.source_sha256 != hashlib.sha256(source).hexdigest():
                raise ValueError("parser response does not bind the supplied source")
            return admitted
        except UnsupportedRustSource:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise UnsupportedRustSource(f"Rust parser process returned malformed evidence: {exc}") from exc

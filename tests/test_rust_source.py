from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from vkit.paired_profiles import U32Binding
from vkit.rust_source import (
    RustSourceProfile,
    UnsupportedRustSource,
    admit_rust_source,
)


def _binding(symbol: str = "convert") -> U32Binding:
    return U32Binding("rust", "src/convert.rs", symbol)


def _admit(source: str, symbol: str = "convert") -> RustSourceProfile:
    return admit_rust_source(source.encode("utf-8"), _binding(symbol))


@pytest.mark.parametrize(
    "source",
    [
        "pub fn convert(value: u32) -> u32 { value }\n",
        (
            "// cfg!(kani) and #[cfg(kani)] are only comment text\n"
            "pub fn convert(value: u32) -> u32 {\n"
            "    let incremented: u32 = value + 1;\n"
            "    if incremented > value { return incremented; } else { value }\n"
            "}\n"
        ),
        (
            "pub fn convert(value: u32) -> u32 {\n"
            "    let enabled: bool = value == 0;\n"
            "    if enabled { 1 } else { 0 }\n"
            "}\n"
        ),
        "pub fn convert(value: u32) -> u32 { let other: u32 = value; other }",
    ],
)
def test_admits_small_scalar_functions_and_comments(source):
    profile = _admit(source)

    assert profile.source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert profile.to_json()["source_sha256"] == profile.source_sha256


def test_profile_records_complete_parser_identity_and_is_frozen():
    source = b"pub fn convert(value: u32) -> u32 { value }"
    profile = admit_rust_source(source, _binding())
    data = profile.to_json()
    identity = data["parser_identity"]

    assert data["binding"] == {
        "language": "rust",
        "source": "src/convert.rs",
        "symbol": "convert",
    }
    assert identity["tree_sitter_core_version"] == "0.26.0"
    assert identity["tree_sitter_grammar_version"] == "0.24.2"
    assert identity["tree_sitter_grammar_abi"] == "15"
    assert identity["source_profile_version"] == "rust-u32-pure-v1"
    for key in (
        "tree_sitter_python_sha256",
        "tree_sitter_native_sha256",
        "tree_sitter_rust_python_sha256",
        "tree_sitter_rust_native_sha256",
        "profile_policy_code_sha256",
    ):
        assert len(identity[key]) == 64
        int(identity[key], 16)
    with pytest.raises(FrozenInstanceError):
        profile.source_sha256 = "0" * 64
    identity["tree_sitter_core_version"] = "changed"
    assert profile.to_json()["parser_identity"]["tree_sitter_core_version"] == "0.26.0"


@pytest.mark.parametrize(
    "source",
    [
        "#[inline]\npub fn convert(value: u32) -> u32 { value }",
        "pub fn convert(value: u32) -> u32 { #[cfg(kani)] value }",
        "pub unsafe fn convert(value: u32) -> u32 { value }",
        "pub async fn convert(value: u32) -> u32 { value }",
        "pub const fn convert(value: u32) -> u32 { value }",
        'pub extern "C" fn convert(value: u32) -> u32 { value }',
        "pub fn convert<T>(value: u32) -> u32 { value }",
        "pub fn convert(value: u64) -> u32 { value as u32 }",
        "pub fn convert(value: u32) -> u64 { value as u64 }",
        "pub fn convert(value: u32) -> u32 { value as u32 }",
        "pub fn convert(value: u32) -> u32 { value + 1u32 }",
        "pub fn convert(value: u32) -> u32 { value + 0xff }",
        "pub fn convert(value: u32) -> u32 { value + 1_000 }",
        "pub fn convert(value: u32) -> u32 { value + 4294967296 }",
        "pub fn convert(value: u32) -> u32 { value / 2 }",
        "pub fn convert(value: u32) -> u32 { value & 1 }",
        "pub fn convert(value: u32) -> u32 { value() }",
        "pub fn convert(value: u32) -> u32 { value.wrapping_add(1) }",
        "pub fn convert(value: u32) -> u32 { other }",
        "pub fn convert(value: u32) -> u32 { let value: u32 = 1; value }",
        "pub fn convert(value: u32) -> u32 { while value > 0 { value } value }",
        "pub fn convert(value: u32) -> u32 { loop { value } }",
        "pub fn convert(value: u32) -> u32 { let inner: u32 = { fn nested() {} 1 }; inner }",
        "pub fn convert(value: u32) -> u32 { cfg!(kani); value }",
        "use std::cmp; pub fn convert(value: u32) -> u32 { value }",
        "static LIMIT: u32 = 1; pub fn convert(value: u32) -> u32 { value }",
        "mod helper {} pub fn convert(value: u32) -> u32 { value }",
        "pub fn convert(value: u32, other: u32) -> u32 { value }",
        "pub fn another(value: u32) -> u32 { value }",
        "pub fn convert(value: bool) -> u32 { 0 }",
        "fn convert(value: u32) -> u32 { value }",
        "pub fn convert(value: u32) -> u32 { value + }",
        "pub fn convert(value: u32) -> u32 { value",
        "pub fn convert(value: u32) -> u32 { value; value }",
    ],
)
def test_rejects_syntax_outside_the_closed_profile(source):
    with pytest.raises(UnsupportedRustSource):
        _admit(source)


def test_rejects_multiple_items_and_extra_file_content():
    with pytest.raises(UnsupportedRustSource):
        _admit("pub fn convert(value: u32) -> u32 { value }\npub fn other() {}")


def test_admission_checks_syntax_and_scope_but_leaves_typechecking_to_rustc():
    profile = _admit("pub fn convert(value: u32) -> u32 { if value { 1 } else { 0 } }")

    assert (
        profile.source_sha256
        == hashlib.sha256(
            b"pub fn convert(value: u32) -> u32 { if value { 1 } else { 0 } }"
        ).hexdigest()
    )


def test_rejects_non_bytes_and_invalid_utf8_at_the_api_boundary():
    with pytest.raises(TypeError):
        admit_rust_source("pub fn convert(value: u32) -> u32 { value }", _binding())
    with pytest.raises(UnsupportedRustSource):
        admit_rust_source(b"\xff", _binding())


def test_rejects_oversized_source_before_parsing():
    with pytest.raises(UnsupportedRustSource, match="64 KiB"):
        admit_rust_source(b" " * (64 * 1024 + 1), _binding())


def test_rejects_ast_depth_above_the_profile_limit():
    source = "pub fn convert(value: u32) -> u32 {" + "{" * 70 + "value" + "}" * 70 + "}"

    with pytest.raises(UnsupportedRustSource, match="AST depth"):
        _admit(source)


def test_rejects_ast_node_count_above_the_profile_limit():
    locals_source = "".join(f"let local_{index}: u32 = 0;" for index in range(500))
    source = f"pub fn convert(value: u32) -> u32 {{{locals_source}value}}"

    with pytest.raises(UnsupportedRustSource, match="AST node"):
        _admit(source)


@pytest.mark.parametrize(("child", "reason"), [
    ("import os; os._exit(23)", "code=23"),
    ("import time; time.sleep(30)", "timed_out=True"),
    ("print('x' * 1000000)", "output_limited=True"),
    ("print('{}')", "malformed evidence"),
])
def test_parser_process_faults_never_admit_source(monkeypatch, child, reason):
    from vkit import rust_source

    monkeypatch.setattr(rust_source, "_PARSER_CHILD", child)
    monkeypatch.setattr(rust_source, "_PARSER_TIMEOUT_SECONDS", 1)
    with pytest.raises(UnsupportedRustSource, match=reason):
        _admit("pub fn convert(value: u32) -> u32 { value }")

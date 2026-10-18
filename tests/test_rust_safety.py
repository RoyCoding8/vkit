from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path

import pytest

from vkit.operations.rust_safety import (
    KANI_BIN_ENV,
    KANI_BUNDLE_SHA256,
    KANI_HOME_ENV,
    KANI_SHA_ENV,
    RUSTUP_HOME_ENV,
    _CHECK_KINDS,
    _Unsupported,
    _generate,
    _interpret,
    _parse_function,
    _parser,
    check_rust_safety,
)


def _write(root: Path, name: str, source: str) -> str:
    (root / name).write_text(source, encoding="utf-8")
    return name


def test_generated_function_is_a_closed_canonical_translation() -> None:
    source = b"fn bump(value: u8) -> u8 { let mut next: u8 = value; if value < 255 { next += 1; } next }"
    model = _parse_function(source, "bump", _parser())
    generated = _generate(model).decode()

    assert "pub(super) fn bump(value: u8) -> u8" in generated
    assert "next += 1u8;" in generated
    assert "let __vkit_arg_0: u8 = kani::any();" in generated
    assert source.decode() not in generated
    assert "unsafe" not in generated


@pytest.mark.parametrize("source", [
    "#[kani::proof] fn candidate(value: u8) -> u8 { value }",
    "fn candidate(value: u8) -> u8 { unsafe { value } }",
    "fn candidate(value: u8) -> u8 { helper(value) }",
    "fn candidate(value: u8) -> u8 { todo!() }",
    "fn candidate(value: u8) -> u8 { value } fn helper(value: u8) -> u8 { value }",
    "fn candidate(value: u8) -> u8 { value << 8 }",
    "fn candidate(value: u8) -> u8 { if value == 255 { value + 1 } else { value }; value }",
    "fn candidate(flag: bool) -> u8 { if flag { 1u8 } else { 2u8 }; }",
    "fn candidate(value: u8) -> u8 { loop { break value + 1; } }",
    "fn candidate(value: u8) -> u8 { 'outer: loop { loop { break 'outer; } } }",
])
def test_source_outside_the_closed_language_is_rejected(source: str) -> None:
    with pytest.raises(_Unsupported):
        _parse_function(source.encode(), "candidate", _parser())


def test_replay_uses_rust_signed_minimum_and_checked_negation() -> None:
    model = _parse_function(b"fn negate(value: i8) -> i8 { -value }", "negate", _parser())
    assert _generate(_parse_function(b"fn minimum() -> i8 { -128 }", "minimum", _parser())) \
        .count(b"i8::MIN") == 1

    with pytest.raises(Exception) as failure:
        _interpret(model, [-128], 8)

    assert getattr(failure.value, "kind", None) == "overflow"
    assert _CHECK_KINDS["attempt to shift left with overflow"] == "invalid-shift"


def test_path_escape_is_rejected_before_backend_use(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.rs"
    outside.write_text("fn safe() {}", encoding="utf-8")

    result = check_rust_safety(tmp_path, "../outside.rs", "safe")

    assert result["status"] == "UNSUPPORTED"
    assert "repository" in result["detail"]


def test_missing_or_mismatched_kani_runtime_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    source = _write(tmp_path, "safe.rs", "fn safe(value: u8) -> u8 { value }")
    for name in (KANI_BIN_ENV, KANI_SHA_ENV, KANI_HOME_ENV, RUSTUP_HOME_ENV):
        monkeypatch.delenv(name, raising=False)

    missing = check_rust_safety(tmp_path, source, "safe")

    assert missing["status"] == "UNAVAILABLE"
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        return
    fake = tmp_path / "kani"
    fake.write_bytes(b"not Kani")
    monkeypatch.setenv(KANI_BIN_ENV, str(fake))
    monkeypatch.setenv(KANI_SHA_ENV, "0" * 64)
    monkeypatch.setenv(KANI_HOME_ENV, str(tmp_path))
    monkeypatch.setenv(RUSTUP_HOME_ENV, str(tmp_path))

    mismatch = check_rust_safety(tmp_path, source, "safe")

    assert mismatch["status"] == "UNAVAILABLE"


def _configured_kani(monkeypatch) -> None:
    binary = os.environ.get(KANI_BIN_ENV)
    expected = os.environ.get(KANI_SHA_ENV)
    home = os.environ.get(KANI_HOME_ENV)
    rustup = os.environ.get(RUSTUP_HOME_ENV)
    if not any((binary, expected, home, rustup)):
        if os.environ.get("VKIT_KANI_REQUIRED") == "1":
            pytest.fail("the real-engine job requires the pinned Kani runtime variables")
        pytest.skip("the pinned Kani 0.68.0 runtime is not configured")
    if not all((binary, expected, home, rustup)):
        pytest.fail("the configured Kani runtime requires executable, digest, Kani home, and rustup home")
    path = Path(binary).resolve()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected.lower():
        pytest.fail("the configured Kani executable does not match its SHA-256")
    bundle = Path(home).parent / "bundle-cache" / "kani-0.68.0-x86_64-unknown-linux-gnu.tar.gz"
    if not bundle.is_file() or hashlib.sha256(bundle.read_bytes()).hexdigest() != KANI_BUNDLE_SHA256:
        pytest.fail("the configured Kani release bundle does not match its official SHA-256")
    monkeypatch.setenv(KANI_BIN_ENV, str(path))
    monkeypatch.setenv(KANI_SHA_ENV, expected.lower())


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"},
                    reason="the pinned Kani runtime is available only on Linux x86_64")
def test_real_kani_proves_safe_code_and_replays_overflow_and_shift(tmp_path: Path, monkeypatch) -> None:
    _configured_kani(monkeypatch)
    safe_path = _write(tmp_path, "safe.rs",
                       "fn bump(value: u8) -> u8 { if value < 255 { value + 1 } else { value } }")
    bounded_loop_path = _write(tmp_path, "bounded-loop.rs",
                               "fn finish(value: u8) -> u8 { let mut next: u8 = value; while next < 2 { next += 1; } next }")
    bool_path = _write(tmp_path, "boolean.rs", "fn invert(flag: bool) -> bool { !flag }")
    minimum_path = _write(tmp_path, "minimum.rs", "fn minimum() -> i8 { -128 }")
    type_error_path = _write(tmp_path, "type-error.rs",
                             "fn bad(value: u8) -> u8 { let flag: bool = value; if flag { 1 } else { 0 } }")
    overflow_path = _write(tmp_path, "overflow.rs", "fn bump(value: u8) -> u8 { value + 1 }")
    shift_path = _write(tmp_path, "shift.rs",
                        "fn shift(value: u8, amount: u32) -> u8 { value << amount }")

    safe = check_rust_safety(tmp_path, safe_path, "bump")
    bounded_loop = check_rust_safety(tmp_path, bounded_loop_path, "finish", unwind=3)
    boolean = check_rust_safety(tmp_path, bool_path, "invert")
    minimum = check_rust_safety(tmp_path, minimum_path, "minimum")
    type_error = check_rust_safety(tmp_path, type_error_path, "bad")
    overflow = check_rust_safety(tmp_path, overflow_path, "bump")
    shift = check_rust_safety(tmp_path, shift_path, "shift")

    assert safe["status"] == "SAFE"
    assert bounded_loop["status"] == "SAFE"
    assert any(item["category"] == "unwind" and item["status"] == "Success"
               for item in bounded_loop["checks"])
    assert boolean["status"] == minimum["status"] == "SAFE"
    assert type_error["status"] == "UNSUPPORTED"
    assert safe["source_sha256"] == hashlib.sha256((tmp_path / safe_path).read_bytes()).hexdigest()
    assert safe["engine"]["release_bundle_sha256"] == KANI_BUNDLE_SHA256
    assert overflow["status"] == "COUNTEREXAMPLE"
    assert overflow["counterexample"]["inputs"] == [
        {"name": "value", "type": "u8", "value": 255},
    ]
    assert shift["status"] == "COUNTEREXAMPLE"
    assert shift["counterexample"]["check"] == "attempt to shift left with overflow"
    assert shift["counterexample"]["inputs"][1]["value"] >= 8


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"},
                    reason="the pinned Kani runtime is available only on Linux x86_64")
def test_real_kani_does_not_call_an_insufficient_loop_unwind_safe(tmp_path: Path, monkeypatch) -> None:
    _configured_kani(monkeypatch)
    path = _write(tmp_path, "loop.rs",
                  "fn finish(value: u8) -> u8 { let mut next: u8 = value; while next < 5 { next += 1; } next }")

    result = check_rust_safety(tmp_path, path, "finish", unwind=2, timeout_seconds=60)

    assert result["status"] == "UNKNOWN"
    assert any(item["category"] == "unwind" for item in result["checks"])

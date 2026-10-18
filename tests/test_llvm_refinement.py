from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from vkit.operations.llvm_refinement import (
    ALIVE2_BIN_ENV,
    ALIVE2_SHA_ENV,
    VALIDATOR_BIN_ENV,
    VALIDATOR_SHA_ENV,
    _capture,
    _classify_alive2,
    check_llvm_refinement,
)

IDENTITY = """define i8 @f(i8 %x) {
entry:
  %y = add i8 %x, 0
  ret i8 %y
}
"""
RETURN_INPUT = """define i8 @f(i8 %x) {
entry:
  ret i8 %x
}
"""
ADD_ONE = """define i8 @f(i8 %x) {
entry:
  %one = add i8 %x, 1
  ret i8 %one
}
"""


def _write_pair(root: Path, before: str = IDENTITY, after: str = RETURN_INPUT) -> tuple[str, str]:
    (root / "before.ll").write_text(before, encoding="utf-8", newline="\n")
    (root / "after.ll").write_text(after, encoding="utf-8", newline="\n")
    return "before.ll", "after.ll"


def test_capture_uses_exact_repo_bytes_and_hash(tmp_path: Path) -> None:
    before_path, _ = _write_pair(tmp_path)
    raw, digest = _capture(tmp_path, before_path)
    assert raw == IDENTITY.encode()
    assert len(digest) == 64


@pytest.mark.parametrize("path", ["../outside.ll", "D:/outside.ll", "", "folder/./before.ll"])
def test_capture_rejects_non_repo_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        _capture(tmp_path, path)


def test_missing_pins_report_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_pair(tmp_path)
    for key in (ALIVE2_BIN_ENV, ALIVE2_SHA_ENV, VALIDATOR_BIN_ENV, VALIDATOR_SHA_ENV):
        monkeypatch.delenv(key, raising=False)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "UNAVAILABLE"


def test_result_classifier_requires_exact_complete_counts() -> None:
    success = (
        "Transformation seems to be correct!\n\nSummary:\n"
        "  1 correct transformations\n  0 incorrect transformations\n"
        "  0 failed-to-prove transformations\n  0 Alive2 errors\n"
    )
    assert _classify_alive2(success) == (
        "REFINES", "Summary:\n  1 correct transformations\n  0 incorrect transformations\n"
        "  0 failed-to-prove transformations\n  0 Alive2 errors\n",
    )
    assert _classify_alive2(success.replace("0 Alive2 errors", "1 Alive2 errors")) is None
    assert _classify_alive2(success + "warning\n") is None
    assert _classify_alive2("Transformation doesn't verify!\n") is None


def _real_engine_configured() -> bool:
    return all(os.environ.get(key) for key in (
        ALIVE2_BIN_ENV, ALIVE2_SHA_ENV, VALIDATOR_BIN_ENV, VALIDATOR_SHA_ENV,
    ))


@pytest.fixture
def real_engine() -> None:
    if _real_engine_configured():
        return
    if os.environ.get("VKIT_ALIVE2_REQUIRED") == "1":
        pytest.fail("VKIT_ALIVE2_REQUIRED=1 but pinned Alive2 and validator binaries are not configured")
    pytest.skip("pinned Alive2 20.0 runs in Linux engine CI")


def test_real_alive2_proves_directional_integer_refinement(tmp_path: Path, real_engine: None) -> None:
    _write_pair(tmp_path)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "REFINES"
    assert result["direction"] == "after refines before"
    assert result["validation"]["before"]["instructions"] == 2
    assert result["alive2_binary_sha256"] == os.environ[ALIVE2_SHA_ENV]


def test_real_alive2_returns_directional_mismatch_witness(tmp_path: Path, real_engine: None) -> None:
    _write_pair(tmp_path, before=ADD_ONE, after=RETURN_INPUT)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "COUNTEREXAMPLE"
    assert "ERROR: Value mismatch" in result["engine_witness"]
    assert re.search(r"i8 %x = #x[0-9a-f]{2} \([0-9]+\)", result["engine_witness"])
    assert result["proof_trust"] == "Alive2 trusted-engine result; no independent counterexample replay"


def test_real_alive2_models_nsw_poison_in_refinement_direction(tmp_path: Path, real_engine: None) -> None:
    signed_overflow_source = """define i8 @f(i8 %x) {
entry:
  %y = add nsw i8 %x, 1
  ret i8 %y
}
"""
    wrapping_target = """define i8 @f(i8 %x) {
entry:
  %y = add i8 %x, 1
  ret i8 %y
}
"""
    _write_pair(tmp_path, before=signed_overflow_source, after=wrapping_target)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "REFINES"
    assert result["direction"] == "after refines before"


def test_real_llvm_validator_rejects_looping_cfg(tmp_path: Path, real_engine: None) -> None:
    looping = """define i8 @f(i8 %x) {
entry:
  br label %loop
loop:
  br label %loop
}
"""
    _write_pair(tmp_path, before=looping)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "UNSUPPORTED"
    assert "acyclic" in result["detail"]


def test_real_llvm_validator_rejects_poison_and_calls(tmp_path: Path, real_engine: None) -> None:
    poison = """define i8 @f(i8 %x) {
entry:
  ret i8 poison
}
"""
    _write_pair(tmp_path, before=poison)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "UNSUPPORTED"


def test_real_llvm_validator_rejects_nneg_cast_flag(tmp_path: Path, real_engine: None) -> None:
    flagged_cast = """define i16 @f(i8 %x) {
entry:
  %wide = zext nneg i8 %x to i16
  ret i16 %wide
}
"""
    _write_pair(tmp_path, before=flagged_cast)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "UNSUPPORTED"
    assert "unsupported instruction" in result["detail"]


def test_real_llvm_validator_rejects_non_dominating_ssa(tmp_path: Path, real_engine: None) -> None:
    invalid_ssa = """define i8 @f(i1 %condition, i8 %x) {
entry:
  br i1 %condition, label %left, label %right
left:
  %value = add i8 %x, 1
  br label %join
right:
  br label %join
join:
  ret i8 %value
}
"""
    _write_pair(tmp_path, before=invalid_ssa)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "UNSUPPORTED"
    assert "LLVM verifier" in result["detail"]


def test_real_llvm_validator_rejects_invalid_phi_predecessors(tmp_path: Path, real_engine: None) -> None:
    invalid_phi = """define i8 @f(i1 %condition, i8 %x) {
entry:
  br i1 %condition, label %left, label %right
left:
  br label %join
right:
  br label %join
join:
  %value = phi i8 [ %x, %entry ], [ %x, %right ]
  ret i8 %value
}
"""
    _write_pair(tmp_path, before=invalid_phi)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "UNSUPPORTED"
    assert "LLVM verifier" in result["detail"]

    call = """declare i8 @g(i8)
define i8 @f(i8 %x) {
entry:
  %y = call i8 @g(i8 %x)
  ret i8 %y
}
"""
    _write_pair(tmp_path, before=call)
    result = check_llvm_refinement(tmp_path, "before.ll", "after.ll", "f")
    assert result["status"] == "UNSUPPORTED"

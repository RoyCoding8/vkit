from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from vkit.operations.c_branches import inspect_c_branch_reachability


@pytest.fixture
def configured_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "linux":
        pytest.skip("native branch replay requires Linux")
    names = (
        "VKIT_CBMC_BIN", "VKIT_CBMC_SHA256",
        "VKIT_C_BRANCH_GCC_BIN", "VKIT_C_BRANCH_GCC_SHA256",
    )
    configured = [os.environ.get(name) for name in names]
    if not any(configured):
        if sys.platform == "linux" and os.environ.get("VKIT_C_BRANCH_REQUIRED") == "1":
            pytest.fail("Linux CI requires pinned CBMC and GCC replay runtimes")
        pytest.skip("pinned CBMC and Linux GCC replay runtimes are not configured")
    if not all(configured):
        pytest.fail("both pinned runtimes require paths and SHA-256 values")
    for path_name, sha_name in ((names[0], names[1]), (names[2], names[3])):
        path = Path(os.environ[path_name]).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != os.environ[sha_name].lower():
            pytest.fail(f"{path_name} does not match its configured SHA-256")
        monkeypatch.setenv(path_name, str(path))
        monkeypatch.setenv(sha_name, digest)


def _source(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8", newline="\n")


def test_reachable_branch_requires_exact_native_ubsan_replay(tmp_path, configured_backends):
    _source(tmp_path, "choose.c", "int choose(int value) { if (value == 7) return 42; return 0; }\n")

    result = inspect_c_branch_reachability(tmp_path, "choose.c", "choose", "if@1:25:then")

    assert result["status"] == "REACHABLE", result
    assert result["branches"] == [{
        "branch_id": "if@1:25:then", "line": 1, "column": 25, "arm": "then",
    }]
    assert result["witness"] == {
        "inputs": {"value": 7}, "input_types": {"value": "int"},
        "observation": "VKIT_BRANCH_ID:if@1:25:then",
    }
    assert len(result["backend_output_sha256"]) == 64
    assert len(result["replay_sha256"]) == 64


def test_witness_preserves_unsigned_and_boolean_scalar_types(tmp_path, configured_backends):
    source = (
        "int choose(int value, unsigned int mode, _Bool enabled) { "
        "if (value == 7 && mode == 1U && enabled) return 42; return 0; }\n"
    )
    _source(tmp_path, "typed.c", source)

    result = inspect_c_branch_reachability(
        tmp_path, "typed.c", "choose", f"if@1:{source.index('if') + 1}:then",
    )

    assert result["status"] == "REACHABLE", result
    assert result["witness"]["inputs"] == {"value": 7, "mode": 1, "enabled": True}
    assert result["witness"]["input_types"] == {
        "value": "int", "mode": "unsigned int", "enabled": "_Bool",
    }


def test_else_arm_is_replayed_as_the_selected_branch(tmp_path, configured_backends):
    source = "int choose(int value) { if (value) return 1; else return 0; }\n"
    _source(tmp_path, "else.c", source)

    result = inspect_c_branch_reachability(
        tmp_path, "else.c", "choose", f"if@1:{source.index('if') + 1}:else",
    )

    assert result["status"] == "REACHABLE", result
    assert result["witness"]["inputs"] == {"value": 0}
    assert result["witness"]["observation"].endswith(":else")


def test_undefined_behavior_before_branch_makes_replay_unknown(tmp_path, configured_backends):
    source = "int choose(int value) { int inverse = 1 / value; if (value == 0) return inverse; return 0; }\n"
    _source(tmp_path, "undefined.c", source)

    result = inspect_c_branch_reachability(
        tmp_path, "undefined.c", "choose", f"if@1:{source.index('if') + 1}:then",
    )

    assert result["status"] == "UNKNOWN", result
    assert "native UBSan replay" in result["detail"]


def test_unreachable_branch_requires_all_safety_and_unwind_properties(tmp_path, configured_backends):
    source = "int impossible(int value) { if (value > 0 && value < 0) return 1; return 0; }\n"
    _source(tmp_path, "impossible.c", source)
    branch_column = source.index("if") + 1

    result = inspect_c_branch_reachability(
        tmp_path, "impossible.c", "impossible", f"if@1:{branch_column}:then",
    )

    assert result["status"] == "UNREACHABLE", result
    assert "all safety and unwinding checks completed" in result["detail"]


def test_insufficient_loop_unwind_is_unknown(tmp_path, configured_backends):
    source = (
        "int loop(int limit) {\n"
        "    int index = 0;\n"
        "    while (index < limit) { ++index; }\n"
        "    if (index < 0 && index > 0) return 1;\n"
        "    return 0;\n"
        "}\n"
    )
    _source(tmp_path, "loop.c", source)

    result = inspect_c_branch_reachability(tmp_path, "loop.c", "loop", "if@4:5:then", unwind=2)

    assert result["status"] == "UNKNOWN", result
    assert "incomplete" in result["detail"]


def test_unknown_branch_id_returns_inventory_without_running_backend(tmp_path):
    _source(tmp_path, "candidate.c", "int candidate(int value) { if (value) return 1; return 0; }\n")

    result = inspect_c_branch_reachability(tmp_path, "candidate.c", "candidate", "if@1:25:else")

    assert result["status"] == "UNSUPPORTED"
    assert result["branches"] == [{
        "branch_id": "if@1:28:then", "line": 1, "column": 28, "arm": "then",
    }]
    assert "does not identify" in result["detail"]

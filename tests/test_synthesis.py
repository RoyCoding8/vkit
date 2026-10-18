from __future__ import annotations

import hashlib

import pytest

from vkit.operations.rewrites import check_function_rewrite
from vkit.operations.synthesis import PROFILE, synthesize_function


@pytest.fixture(autouse=True)
def optional_backend() -> None:
    pytest.importorskip("cvc5", reason="optional cvc5 backend is not installed")


def _run(tmp_path, source: str, function: str = "f", **kwargs):
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    return synthesize_function(tmp_path, "sample.py", function, **kwargs)


def test_synthesizes_constant_with_complete_function_and_proof(tmp_path) -> None:
    source = "def f() -> int:\n    return 1\n"

    result = _run(tmp_path, source)

    assert result["status"] == "SYNTHESIZED"
    assert result["profile"] == PROFILE
    assert result["candidate"] == "def f() -> int:\n    return 1\n"
    captured = (tmp_path / "sample.py").read_bytes()
    assert result["source_sha256"] == hashlib.sha256(captured).hexdigest()
    assert result["proof"]["status"] == "PROVED"
    assert result["proof"]["backend"] == "cvc5"
    assert "Mathematical integers" in result["model_scope"]
    assert result["minimality"] == "not_claimed"


def test_synthesizes_linear_function_and_independent_checker_reproves_it(tmp_path) -> None:
    source = "def f(x: int, y: int) -> int:\n    return x + y\n"

    result = _run(tmp_path, source)

    assert result["status"] == "SYNTHESIZED"
    assert result["candidate"].startswith("def f(x: int, y: int) -> int:\n    return ")
    assert result["proof"]["status"] == "PROVED"
    assert check_function_rewrite(source, "f", result["candidate"]).status == "PROVED"


def test_int_profile_rejects_boolean_parameter_or_return(tmp_path) -> None:
    result = _run(tmp_path, "def f(enabled: bool) -> int:\n    return 1 if enabled else 0\n")

    assert result["status"] == "UNSUPPORTED"
    assert result["candidate"] is None
    assert "only int" in result["reason"]


def test_grammar_limited_reference_is_not_misreported_as_synthesized(tmp_path) -> None:
    result = _run(tmp_path, "def f(x: int) -> int:\n    return x * x\n")

    assert result["status"] in {"NO_SOLUTION", "UNKNOWN"}
    assert result["candidate"] is None
    assert result["grammar"]["expression"] == "E ::= parameter | -1 | 0 | 1 | E + E | E - E"


def test_source_global_reference_is_rejected_without_execution(tmp_path) -> None:
    source = (
        "raise RuntimeError('module source must not run')\n"
        "offset = 3\n"
        "def f(x: int) -> int:\n"
        "    return x + offset\n"
    )

    result = _run(tmp_path, source)

    assert result["status"] == "UNSUPPORTED"
    assert result["candidate"] is None
    assert "offset" in result["reason"]


def test_hard_deadline_covers_isolated_worker_startup_and_proof(tmp_path) -> None:
    result = _run(tmp_path, "def f(x: int) -> int:\n    return x + 1\n", timeout_ms=1)

    assert result["status"] == "UNKNOWN"
    assert result["candidate"] is None
    assert "deadline" in result["reason"]


def test_invalid_profile_and_timeout_are_rejected_at_boundary(tmp_path) -> None:
    (tmp_path / "sample.py").write_text("def f(x: int) -> int:\n    return x\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported synthesis profile"):
        synthesize_function(tmp_path, "sample.py", "f", profile="piecewise-int")
    with pytest.raises(ValueError, match="timeout_ms"):
        synthesize_function(tmp_path, "sample.py", "f", timeout_ms=0)

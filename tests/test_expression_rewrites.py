from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.operations.expressions import ParseFailure, ResourceLimits, parse_function  # noqa: E402
from vkit.operations.rewrites import check_function_rewrite  # noqa: E402



def test_whole_function_branch_assignment_is_proved_by_cvc5() -> None:
    original = """
def score(x: int, enabled: bool) -> int:
    y = x + 3
    if enabled:
        z = y * 2
        return z
    else:
        return (x + 3) * 2
"""
    replacement = """
def score(x: int, enabled: bool) -> int:
    return (x + 3) * 2
"""

    result = check_function_rewrite(original, "score", replacement)

    assert result.status == "PROVED"
    assert result.backend == "cvc5"
    assert result.backend_version == "1.4.2"
    assert "Mathematical integers and booleans" in result.model_scope
    assert result.counterexample is None



def test_counterexample_returns_the_exact_large_integer() -> None:
    large = 10**90 + 123456789
    original = f"""
def accepts(x: int) -> bool:
    return x == {large}
"""
    replacement = """
def accepts(x: int) -> bool:
    return False
"""

    result = check_function_rewrite(original, "accepts", replacement)

    assert result.status == "COUNTEREXAMPLE"
    assert result.counterexample == {"x": large}
    assert type(result.counterexample["x"]) is int



def test_branch_assignments_lower_to_one_pure_expression() -> None:
    original = """
def choose(x: int, flag: bool) -> int:
    if flag:
        value = x + 1
    else:
        value = x - 1
    return value
"""
    replacement = """
def choose(x: int, flag: bool) -> int:
    return x + 1 if flag else x - 1
"""

    parsed = parse_function(original, "choose")
    assert not isinstance(parsed, ParseFailure)
    assert parsed.body.op == "ite"
    assert check_function_rewrite(original, "choose", replacement).status == "PROVED"


def test_unused_read_before_assignment_is_rejected() -> None:
    source = """
def f(x: int) -> int:
    unused = later + 1
    later = x
    return x
"""
    replacement = "def f(x: int) -> int:\n    return x\n"

    result = check_function_rewrite(source, "f", replacement)

    assert result.status == "UNSUPPORTED"
    assert "later" in result.reason
    assert "read before assignment" in result.reason



def test_read_before_assignment_in_a_branch_is_rejected_even_when_unused() -> None:
    source = """
def f(x: int, enabled: bool) -> int:
    if enabled:
        unused = later + 1
    later = x
    return x
"""
    replacement = "def f(x: int, enabled: bool) -> int:\n    return x\n"

    result = check_function_rewrite(source, "f", replacement)

    assert result.status == "UNSUPPORTED"
    assert "later" in result.reason



def test_local_assigned_on_only_one_branch_cannot_be_read_after_join() -> None:
    source = """
def f(x: int, enabled: bool) -> int:
    if enabled:
        value = x
    return value
"""

    parsed = parse_function(source, "f")

    assert isinstance(parsed, ParseFailure)
    assert "value" in parsed.reason
    assert "read before assignment" in parsed.reason



def test_repeated_assignments_resolve_in_source_order() -> None:
    source = """
def f(x: int) -> int:
    value = x
    value = value + 1
    value = value * 2
    return value
"""
    replacement = "def f(x: int) -> int:\n    return (x + 1) * 2\n"

    result = check_function_rewrite(source, "f", replacement)

    assert result.status == "PROVED"



def test_module_level_rebinding_of_selected_function_is_unsupported() -> None:
    source = "def f(x: int) -> int:\n    return x\nf = other\n"

    parsed = parse_function(source, "f")

    assert isinstance(parsed, ParseFailure)
    assert "rebinds" in parsed.reason


def test_frontend_bounds_path_expansion() -> None:
    branches = "\n".join(
        "    if enabled:\n        value = value + 1" for _ in range(12)
    )
    source = f"def f(x: int, enabled: bool) -> int:\n    value = x\n{branches}\n    return value\n"

    parsed = parse_function(source, "f", ResourceLimits(max_ast_nodes=200))

    assert isinstance(parsed, ParseFailure)
    assert "AST node limit" in parsed.reason


def test_boolean_branch_and_chained_comparison_are_proved() -> None:
    original = """
def inside(x: int, enabled: bool) -> bool:
    if enabled:
        return 0 <= x < 10
    return False
"""
    replacement = """
def inside(x: int, enabled: bool) -> bool:
    return enabled and x >= 0 and x < 10
"""

    assert check_function_rewrite(original, "inside", replacement).status == "PROVED"



def test_unsupported_python_is_rejected_without_running_source() -> None:
    source = """
raise RuntimeError('source must never execute')
def unsafe(x: int) -> int:
    return helper(x)
"""
    replacement = """
def unsafe(x: int) -> int:
    return x
"""

    result = check_function_rewrite(source, "unsafe", replacement)

    assert result.status == "UNSUPPORTED"
    assert "Call" in result.reason



def test_effects_division_and_defaults_are_unsupported() -> None:
    cases = (
        ("def f(x: int) -> int:\n    print(x)\n    return x\n", "Expr"),
        ("def f(x: int) -> int:\n    return x // 2\n", "FloorDiv"),
        ("def f(x: int = 1) -> int:\n    return x\n", "defaults"),
    )
    for source, reason in cases:
        parsed = parse_function(source, "f")
        assert isinstance(parsed, ParseFailure)
        assert parsed.status == "UNSUPPORTED"
        assert reason.lower() in parsed.reason.lower()
        assert parsed.to_json()["status"] == "UNSUPPORTED"



def test_unsupported_syntax_after_return_is_still_rejected() -> None:
    source = "def f(x: int) -> int:\n    return x\n    print(x)\n"

    parsed = parse_function(source, "f")

    assert isinstance(parsed, ParseFailure)
    assert "Expr" in parsed.reason


def test_replacement_must_keep_the_same_annotated_signature() -> None:
    source = "def f(x: int) -> int:\n    return x\n"
    replacement = "def f(x: bool) -> int:\n    return 1\n"

    result = check_function_rewrite(source, "f", replacement)

    assert result.status == "UNSUPPORTED"
    assert "same function name and signature" in result.reason



def test_resource_limits_reject_oversized_source_at_the_frontend() -> None:
    limits = ResourceLimits(max_source_bytes=16)

    parsed = parse_function("def f(x: int) -> int:\n    return x\n", "f", limits)

    assert isinstance(parsed, ParseFailure)
    assert "byte limit" in parsed.reason

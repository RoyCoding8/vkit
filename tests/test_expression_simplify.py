from __future__ import annotations

import pytest

from vkit.operations.simplify import simplify_function


@pytest.fixture(autouse=True)
def optional_backends() -> None:
    pytest.importorskip("egglog")
    pytest.importorskip("cvc5")


def test_simplifies_and_cvc5_proves_whole_replacement() -> None:
    source = "def score(x: int) -> int:\n    return x + 0\n"
    result = simplify_function(source, "score")

    assert result.status == "PROVED"
    assert result.replacement == "def score(x: int) -> int:\n    return x\n"
    assert result.proof.status == "PROVED"
    assert result.simplifier_backend == "egglog"
    assert result.simplifier_version == "13.2.0"
    assert result.proof_backend == "cvc5"
    assert result.proof_version is not None
    assert result.source_sha256


def test_arbitrary_precision_literals_survive_extraction() -> None:
    source = (
        "def offset(x: int) -> int:\n"
        "    return (x + 123456789012345678901234567890) + 0\n"
    )
    result = simplify_function(source, "offset")

    assert result.status == "PROVED"
    assert result.replacement == (
        "def offset(x: int) -> int:\n"
        "    return (x + 123456789012345678901234567890)\n"
    )


def test_constant_folding_uses_exact_integer_arithmetic() -> None:
    source = (
        "def large() -> int:\n"
        "    return 9223372036854775808 + 9223372036854775808\n"
    )
    result = simplify_function(source, "large")

    assert result.status == "PROVED"
    assert result.replacement == "def large() -> int:\n    return 18446744073709551616\n"


def test_simplifies_boolean_and_conditional_terms() -> None:
    source = (
        "def choose(flag: bool) -> bool:\n"
        "    return (flag and True and True) if True else False\n"
    )
    result = simplify_function(source, "choose")

    assert result.status == "PROVED"
    assert result.replacement == "def choose(flag: bool) -> bool:\n    return flag\n"


def test_unsupported_function_is_not_executed() -> None:
    source = "def unsafe(x: int) -> int:\n    return int(str(x))\n"
    result = simplify_function(source, "unsafe")

    assert result.status == "UNSUPPORTED"
    assert result.replacement is None
    assert result.source_sha256


def test_unbound_unused_assignment_is_rejected() -> None:
    source = "def broken() -> int:\n    unused = missing\n    return 1\n"

    result = simplify_function(source, "broken")

    assert result.status == "UNSUPPORTED"
    assert result.replacement is None

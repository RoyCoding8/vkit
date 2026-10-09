from __future__ import annotations

import hashlib
import sys

import pytest

from vkit.operations.affine import _lower_generator, compare_iteration_sets


def _compare(tmp_path, old: str, new: str) -> dict:
    source = tmp_path / "module.py"
    source.write_text(old, encoding="utf-8")
    return compare_iteration_sets(tmp_path, source.name, "points", new, timeout_ms=10_000)


def _assert_status(result: dict, expected: str) -> None:
    if sys.platform == "win32":
        assert result["status"] == "UNAVAILABLE"
        assert result["backend"] == "islpy"
        assert result["backend_version"] is None
    else:
        assert result["status"] == expected
        if expected in {"EQUIVALENT", "COUNTEREXAMPLE"}:
            assert result["backend_version"] == "2026.2.2"


def test_lowering_uses_actual_range_values_for_strided_and_descending_loops() -> None:
    strided = _lower_generator(
        "def points():\n    for i in range(5, 9, 2):\n        yield (i,)\n",
        "points",
    )
    piece = strided.pieces[0]
    assert piece.constraints == ("q0 >= 0", "2*q0 + 5 < 9")
    assert piece.coordinates[0].text() == "2*q0 + 5"

    descending = _lower_generator(
        "def points(n: int):\n    for i in range(n - 1, -1, -1):\n        yield (i,)\n",
        "points",
    )
    piece = descending.pieces[0]
    assert piece.constraints == ("q0 >= 0", "p0 - q0 - 1 > -1")
    assert piece.coordinates[0].text() == "p0 - q0 - 1"


def test_nested_bounds_use_the_outer_loop_value() -> None:
    lowered = _lower_generator(
        "def points():\n    for i in range(5, 9, 2):\n        for j in range(i):\n            yield (i, j)\n",
        "points",
    )
    piece = lowered.pieces[0]
    assert piece.variables == ("q0", "q1")
    assert piece.constraints == ("q0 >= 0", "2*q0 + 5 < 9", "q1 >= 0", "q1 < 2*q0 + 5")
    assert tuple(value.text() for value in piece.coordinates) == ("2*q0 + 5", "q1")


def test_parser_does_not_execute_module_or_function_source() -> None:
    lowered = _lower_generator(
        "raise RuntimeError('module code must not run')\n"
        "def points(n: int):\n"
        "    yield (n,)\n",
        "points",
    )
    assert lowered.parameters == ("n",)
    assert lowered.pieces[0].coordinates[0].text() == "p0"


@pytest.mark.parametrize(
    "source",
    [
        "@decorate\ndef points(n: int):\n    yield (n,)\n",
        "def points(n: int = 1):\n    yield (n,)\n",
        "def points(n: int):\n    n = 2\n    yield (n,)\n",
        "def points(n: int):\n    yield (float(n),)\n",
        "def points(n: int):\n    yield (True,)\n",
        "def points(n: int):\n    for i in range(n):\n        break\n        yield (i,)\n",
        "def points(n: int):\n    for i in range(n):\n        continue\n        yield (i,)\n",
        "def points(n: int):\n    return\n    yield (n,)\n",
        "def points(n: int):\n    for n in range(n):\n        yield (n,)\n",
        "def points(n: int):\n    for i in range(n):\n        pass\n    yield (i,)\n",
        "def points(n: int):\n    for i in range(n):\n        for i in range(n):\n            yield (i,)\n",
        "def points(n: int):\n    for i in range(n):\n        yield from ((i,),)\n",
        "def points(n: int):\n    for i in range(n, 0, 0):\n        yield (i,)\n",
        "def points(n: int):\n    for i in values:\n        yield (i,)\n",
        "def points(n: int) -> object:\n    yield (n,)\n",
    ],
)
def test_rejects_source_outside_the_generator_contract(source: str) -> None:
    with pytest.raises(ValueError):
        _lower_generator(source, "points")


def test_rejects_large_boolean_expansion() -> None:
    conditions = " or ".join(f"n == {value}" for value in range(129))
    source = f"def points(n: int):\n    if {conditions}:\n        yield (n,)\n"
    with pytest.raises(ValueError, match="piece limit"):
        _lower_generator(source, "points")


def test_rejects_loop_index_after_its_lexical_loop() -> None:
    source = "def points(n: int):\n    for i in range(n):\n        pass\n    yield (i,)\n"
    with pytest.raises(ValueError, match="outside its parameter or loop scope"):
        _lower_generator(source, "points")


def test_reordered_triangular_loops_match_for_finite_integer_parameters(tmp_path) -> None:
    old = """def points(n: int):
    for i in range(n):
        for j in range(i):
            yield (i, j)
"""
    new = """def points(n: int):
    for j in range(n):
        for i in range(j + 1, n):
            yield (i, j)
"""

    def old_points(n: int) -> set[tuple[int, int]]:
        return {(i, j) for i in range(n) for j in range(i)}

    def new_points(n: int) -> set[tuple[int, int]]:
        return {(i, j) for j in range(n) for i in range(j + 1, n)}

    assert all(old_points(n) == new_points(n) for n in range(-3, 9))
    result = _compare(tmp_path, old, new)
    _assert_status(result, "EQUIVALENT")


def test_nested_strides_and_outer_bound_are_compared_exactly(tmp_path) -> None:
    old = """def points(n: int):
    for i in range(0, n, 2):
        for j in range(i, n, 2):
            yield (i, j)
"""
    new = """def points(n: int):
    for j in range(0, n, 2):
        for i in range(0, j + 1, 2):
            yield (i, j)
"""

    def old_points(n: int) -> set[tuple[int, int]]:
        return {(i, j) for i in range(0, n, 2) for j in range(i, n, 2)}

    def new_points(n: int) -> set[tuple[int, int]]:
        return {(i, j) for j in range(0, n, 2) for i in range(0, j + 1, 2)}

    assert all(old_points(n) == new_points(n) for n in range(-3, 12))
    result = _compare(tmp_path, old, new)
    _assert_status(result, "EQUIVALENT")


def test_negative_stride_endpoint_matches_positive_stride(tmp_path) -> None:
    old = """def points():
    for i in range(9, -1, -2):
        yield (i,)
"""
    new = """def points():
    for i in range(1, 10, 2):
        yield (i,)
"""
    assert set(range(9, -1, -2)) == set(range(1, 10, 2))
    result = _compare(tmp_path, old, new)
    _assert_status(result, "EQUIVALENT")


def test_boolean_negation_and_or_lower_to_the_same_set(tmp_path) -> None:
    old = """def points(n: int):
    for i in range(-2, 3):
        if not (i < n and n <= 2):
            yield (i,)
"""
    new = """def points(n: int):
    for i in range(-2, 3):
        if i >= n or n > 2:
            yield (i,)
"""

    def old_points(n: int) -> set[tuple[int]]:
        return {(i,) for i in range(-2, 3) if not (i < n and n <= 2)}

    def new_points(n: int) -> set[tuple[int]]:
        return {(i,) for i in range(-2, 3) if i >= n or n > 2}

    assert all(old_points(n) == new_points(n) for n in range(-4, 5))
    result = _compare(tmp_path, old, new)
    _assert_status(result, "EQUIVALENT")


def test_counterexample_can_require_an_unbounded_negative_parameter(tmp_path) -> None:
    threshold = -10**40
    old = f"""def points(n: int):
    if n < {threshold}:
        yield (0,)
"""
    new = """def points(n: int):
    for i in range(0):
        yield (0,)
"""
    result = _compare(tmp_path, old, new)
    _assert_status(result, "COUNTEREXAMPLE")
    if sys.platform != "win32":
        assert result["old_only"]["parameters"]["n"] < threshold
        assert result["old_only"]["point"] == [0]
        assert result["new_only"] is None


def test_order_and_duplicate_yields_are_outside_set_semantics(tmp_path) -> None:
    old = """def points():
    yield (0,)
    yield (1,)
"""
    new = """def points():
    yield (1,)
    yield (0,)
    yield (1,)
"""
    result = _compare(tmp_path, old, new)
    _assert_status(result, "EQUIVALENT")


def test_both_directional_witnesses_are_returned(tmp_path) -> None:
    old = "def points():\n    yield (0,)\n"
    new = "def points():\n    yield (1,)\n"
    result = _compare(tmp_path, old, new)
    _assert_status(result, "COUNTEREXAMPLE")
    if sys.platform != "win32":
        assert result["old_only"] == {"parameters": {}, "point": [0]}
        assert result["new_only"] == {"parameters": {}, "point": [1]}


def test_result_binds_both_captured_sources_with_sha256(tmp_path) -> None:
    old = "def points(n: int):\n    yield (n,)\n"
    new = "def points(n: int):\n    yield (n + 1,)\n"
    result = _compare(tmp_path, old, new)
    captured = (tmp_path / "module.py").read_bytes().decode("utf-8")
    assert result["old_source_sha256"] == hashlib.sha256(captured.encode("utf-8")).hexdigest()
    assert result["replacement_sha256"] == hashlib.sha256(new.encode("utf-8")).hexdigest()
    assert "selected function AST" in result["model_scope"]
    assert "module globals are ignored" in result["model_scope"]


def test_supported_generator_reports_unavailable_on_windows(tmp_path) -> None:
    if sys.platform != "win32":
        pytest.skip("the Windows backend availability contract is platform-specific")
    source = "def points(n: int):\n    yield (n,)\n"
    result = _compare(tmp_path, source, source)
    assert result["status"] == "UNAVAILABLE"
    assert "Windows" in result["reason"]

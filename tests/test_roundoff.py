from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

import pytest

from vkit.operations.roundoff import (
    GAPPA_BIN_ENV,
    GAPPA_SHA_ENV,
    _parse_model,
    _script,
    analyze_roundoff,
)


def _model(*, request=None, budget=(3, 1 << 26)):
    return {
        "schema_version": 1,
        "request": request or {"kind": "bound", "format": "binary32"},
        "variables": [{"name": "x", "lower": [0, 1], "upper": [1, 1]}],
        "nodes": [
            {"id": "x", "op": "var", "name": "x"},
            {"id": "one", "op": "rational", "value": [1, 1]},
            {"id": "one_minus_x", "op": "sub", "args": ["one", "x"]},
            {"id": "out", "op": "mul", "args": ["x", "one_minus_x"]},
        ],
        "output": "out",
        "error_budget": list(budget),
    }


def _write_model(root: Path, model: dict) -> str:
    path = root / "roundoff.json"
    path.write_text(json.dumps(model), encoding="utf-8")
    return path.name


def test_rational_endpoints_and_format_are_lowered_exactly() -> None:
    model = _model()
    model["variables"][0]["lower"] = [-1, 3]
    model["variables"][0]["upper"] = [2, 3]
    parsed = _parse_model(model)
    script = _script(parsed, "binary32")
    assert "v0 * 3 >= -1" in script
    assert "v0 * 3 <= 2" in script
    assert "@rnd = float<ieee_32, ne>;" in script
    assert "f2 rnd= f1 - f0;" in script
    assert "raw2 in [-16777215b104, 16777215b104]" in script
    assert "* 67108864 <= 3" in script


def test_division_obligations_cover_rounded_and_exact_denominators() -> None:
    model = {
        "schema_version": 1,
        "request": {"kind": "bound", "format": "binary64"},
        "variables": [{"name": "x", "lower": [1, 1], "upper": [2, 1]},
                      {"name": "y", "lower": [2, 1], "upper": [3, 1]}],
        "nodes": [{"id": "x", "op": "var", "name": "x"},
                  {"id": "y", "op": "var", "name": "y"},
                  {"id": "out", "op": "div", "args": ["x", "y"]}],
        "output": "out",
        "error_budget": [1, 1],
    }
    script = _script(_parse_model(model), "binary64")
    assert "@rnd = float<ieee_64, ne>;" in script
    assert "not f1 in [0, 0]" in script
    assert "not r1 in [0, 0]" in script
    assert "raw2 in [-9007199254740991b971, 9007199254740991b971]" in script


@pytest.mark.parametrize("change", [
    lambda m: m.update(schema_version=True),
    lambda m: m.update(error_budget=[1, 0]),
    lambda m: m["variables"][0].update(lower=[2, 1]),
    lambda m: m["nodes"].append({"id": "hidden", "op": "rational", "value": [0, 1]}),
    lambda m: m["nodes"][2].update(args=["out", "x"]),
    lambda m: m.update(request={"kind": "bound", "format": "binary16"}),
])
def test_malformed_or_unsupported_models_are_rejected(change) -> None:
    model = _model()
    change(model)
    with pytest.raises((TypeError, ValueError)):
        _parse_model(model)


def test_missing_engine_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GAPPA_BIN_ENV, raising=False)
    monkeypatch.delenv(GAPPA_SHA_ENV, raising=False)
    result = analyze_roundoff(tmp_path, _write_model(tmp_path, _model()))
    assert result["status"] == "UNAVAILABLE"


def test_real_gappa_proves_absolute_bound_and_selects_uniform_format(
        tmp_path: Path) -> None:
    if not os.environ.get(GAPPA_BIN_ENV) or not os.environ.get(GAPPA_SHA_ENV):
        pytest.skip("Gappa 1.8.3 is configured only in the real-engine CI job")
    path = _write_model(tmp_path, _model(
        request={"kind": "uniform_precision", "formats": ["binary64", "binary32"]}))
    result = analyze_roundoff(tmp_path, path)
    assert result["status"] == "CERTIFIED_FEASIBLE_FORMAT"
    assert result["format"] == "binary32"
    assert result["minimality_claim"] is None
    assert result["attempts"][0]["status"] == "PROVED_BY_GAPPA"


def test_real_gappa_does_not_turn_failed_goal_into_counterexample(
        tmp_path: Path) -> None:
    if not os.environ.get(GAPPA_BIN_ENV) or not os.environ.get(GAPPA_SHA_ENV):
        pytest.skip("Gappa 1.8.3 is configured only in the real-engine CI job")
    model = _model(budget=(1, 1 << 40))
    result = analyze_roundoff(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "UNKNOWN"
    assert "counterexample" not in result["status"].lower()
    assert result["attempts"][0]["engine_exit_code"] != 0


def test_real_gappa_proves_tie_to_even_explicit_conversion(tmp_path: Path) -> None:
    if not os.environ.get(GAPPA_BIN_ENV) or not os.environ.get(GAPPA_SHA_ENV):
        pytest.skip("Gappa 1.8.3 is configured only in the real-engine CI job")
    tie = Fraction((1 << 24) + 1, 1 << 24)
    model = {
        "schema_version": 1,
        "request": {"kind": "bound", "format": "binary32"},
        "variables": [{"name": "x", "lower": [tie.numerator, tie.denominator],
                       "upper": [tie.numerator, tie.denominator]}],
        "nodes": [{"id": "x", "op": "var", "name": "x"},
                  {"id": "rounded", "op": "round", "arg": "x"}],
        "output": "rounded",
        "error_budget": [1, 1 << 24],
    }
    result = analyze_roundoff(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "PROVED_BY_GAPPA"
    assert result["absolute_error_bound"] == [1, 1 << 24]


def test_real_gappa_proves_gradual_smallest_subnormal_rounding(tmp_path: Path) -> None:
    if not os.environ.get(GAPPA_BIN_ENV) or not os.environ.get(GAPPA_SHA_ENV):
        pytest.skip("Gappa 1.8.3 is configured only in the real-engine CI job")
    smallest = Fraction(1, 1 << 149)
    model = {
        "schema_version": 1,
        "request": {"kind": "bound", "format": "binary32"},
        "variables": [{"name": "x", "lower": [smallest.numerator, smallest.denominator],
                       "upper": [smallest.numerator, smallest.denominator]}],
        "nodes": [{"id": "x", "op": "var", "name": "x"},
                  {"id": "rounded", "op": "round", "arg": "x"}],
        "output": "rounded",
        "error_budget": [0, 1],
    }
    result = analyze_roundoff(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "PROVED_BY_GAPPA"
    assert result["absolute_error_bound"] == [0, 1]


def test_real_gappa_proves_binary64_model(tmp_path: Path) -> None:
    if not os.environ.get(GAPPA_BIN_ENV) or not os.environ.get(GAPPA_SHA_ENV):
        pytest.skip("Gappa 1.8.3 is configured only in the real-engine CI job")
    model = {
        "schema_version": 1,
        "request": {"kind": "bound", "format": "binary64"},
        "variables": [{"name": "x", "lower": [1, 1], "upper": [1, 1]}],
        "nodes": [{"id": "x", "op": "var", "name": "x"},
                  {"id": "rounded", "op": "round", "arg": "x"}],
        "output": "rounded",
        "error_budget": [0, 1],
    }
    result = analyze_roundoff(tmp_path, _write_model(tmp_path, model))
    assert result["status"] == "PROVED_BY_GAPPA"
    assert result["format"] == "binary64"


def test_real_gappa_keeps_overflow_and_zero_divisor_inconclusive(tmp_path: Path) -> None:
    if not os.environ.get(GAPPA_BIN_ENV) or not os.environ.get(GAPPA_SHA_ENV):
        pytest.skip("Gappa 1.8.3 is configured only in the real-engine CI job")
    overflow = {
        "schema_version": 1,
        "request": {"kind": "bound", "format": "binary32"},
        "variables": [{"name": "x", "lower": [1 << 128, 1], "upper": [1 << 128, 1]}],
        "nodes": [{"id": "x", "op": "var", "name": "x"},
                  {"id": "rounded", "op": "round", "arg": "x"}],
        "output": "rounded",
        "error_budget": [1, 1],
    }
    overflow_result = analyze_roundoff(tmp_path, _write_model(tmp_path, overflow))
    assert overflow_result["status"] == "UNKNOWN"

    division = {
        "schema_version": 1,
        "request": {"kind": "bound", "format": "binary32"},
        "variables": [{"name": "one", "lower": [1, 1], "upper": [1, 1]},
                      {"name": "y", "lower": [-1, 1], "upper": [1, 1]}],
        "nodes": [{"id": "one", "op": "var", "name": "one"},
                  {"id": "y", "op": "var", "name": "y"},
                  {"id": "out", "op": "div", "args": ["one", "y"]}],
        "output": "out",
        "error_budget": [1, 1],
    }
    division_result = analyze_roundoff(tmp_path, _write_model(tmp_path, division))
    assert division_result["status"] == "UNKNOWN"

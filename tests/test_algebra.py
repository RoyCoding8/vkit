from __future__ import annotations

import hashlib
import json

import pytest

from vkit.operations.algebra import compare_rational_expressions


pytest.importorskip("sympy")


def var(name="x"):
    return {"var": name}


def const(n, d=1):
    return {"rational": [n, d]}


def run(tmp_path, before, after, **kwargs):
    path = tmp_path / "algebra.json"
    path.write_text(json.dumps({"version": 1, "variables": ["x", "y"],
                                "before": before, "after": after}), encoding="utf-8")
    return compare_rational_expressions(tmp_path, path.name, **kwargs)


def test_exact_rational_coefficients_and_capture(tmp_path):
    result = run(tmp_path, {"mul": [const(1, 3), {"add": [var(), var()]}]},
                 {"mul": [const(2, 3), var()]})
    assert result["status"] == "IDENTICAL"
    assert result["cross_product"] == []
    assert result["input_sha256"] == hashlib.sha256((tmp_path / "algebra.json").read_bytes()).hexdigest()
    assert result["backend"] == {"name": "sympy", "version": "1.14.0"}


def test_cancelled_poles_are_reported_without_domain_claim(tmp_path):
    result = run(tmp_path, {"div": [{"sub": [{"pow": [var(), 2]}, const(1)]},
                                  {"sub": [var(), const(1)]}]}, {"add": [var(), const(1)]})
    assert result["status"] == "IDENTICAL"
    assert result["nonzero_guards"] == [[
        {"powers": [0, 0], "coefficient": [-1, 1]},
        {"powers": [1, 0], "coefficient": [1, 1]}]]
    assert "does not assert equality" in result["model_scope"]


def test_nonidentity_has_exact_coefficient_evidence(tmp_path):
    result = run(tmp_path, {"pow": [{"add": [var(), var("y")]}, 2]},
                 {"add": [{"pow": [var(), 2]}, {"pow": [var("y"), 2]}]})
    assert result["status"] == "NOT_IDENTICAL"
    assert result["cross_product"] == [{"powers": [1, 1], "coefficient": [2, 1]}]


@pytest.mark.parametrize("bad", [
    {"div": [const(1), {"sub": [var(), var()]}]},
    {"rational": [1, 0]}, {"rational": [True, 1]},
    {"sin": var()}, {"var": "undeclared"}, {"pow": [var(), -1]},
])
def test_unsupported_algebra_fails_closed(tmp_path, bad):
    assert run(tmp_path, bad, const(0))["status"] == "UNSUPPORTED"


def test_timeout_is_unknown(tmp_path, monkeypatch):
    import subprocess

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr("vkit.operations.algebra.subprocess.run", timeout)
    result = run(tmp_path, var(), var())
    assert result["status"] == "UNKNOWN"
    assert "deadline" in result["reason"]


def test_missing_pin_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("vkit.operations.algebra.version", lambda _: "other")
    assert run(tmp_path, var(), var())["status"] == "UNAVAILABLE"


def test_expression_swell_stops_without_identity_claim(tmp_path):
    large = {"pow": [{"pow": [{"pow": [var(), 8]}, 8]}, 8]}
    assert run(tmp_path, large, large)["status"] == "UNKNOWN"

from __future__ import annotations

import hashlib

import pytest

from helpers import DRIVER, McpClient, scenario_check, vkit

SOURCE = "def total(x: int, y: int) -> int:\n    extra = x + 0\n    return extra + y\n"
EQUIVALENT = "def total(x: int, y: int) -> int:\n    return y + x\n"
WRONG = "def total(x: int, y: int) -> int:\n    return x - y\n"


def example_project(make_project, source=SOURCE):
    return make_project({"maths.py": source, "driver.py": DRIVER + 'report([("ok", True, "ok")])\n'},
                        [scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["maths.py", "driver.py"])])


def test_rewrite_cli_proves_or_returns_a_concrete_counterexample(make_project, tmp_path):
    pytest.importorskip("cvc5")
    project = example_project(make_project)
    replacement = tmp_path / "replacement.py"
    replacement.write_text(EQUIVALENT, encoding="utf-8")
    code, body = vkit(project, "compute", "check-rewrite", "--path", "maths.py", "--function", "total",
                      "--replacement-file", str(replacement))
    assert (code, body["status"], body["backend"]) == (0, "PROVED", "cvc5")
    assert body["source_sha256"] == hashlib.sha256((project / "maths.py").read_bytes()).hexdigest()
    replacement.write_text(WRONG, encoding="utf-8")
    code, body = vkit(project, "compute", "check-rewrite", "--path", "maths.py", "--function", "total",
                      "--replacement-file", str(replacement))
    values = body["counterexample"]
    assert code == 1 and body["status"] == "COUNTEREXAMPLE"
    assert values["x"] + values["y"] != values["x"] - values["y"]
    assert (project / "maths.py").read_text() == SOURCE


def test_mcp_computations_preserve_gate_and_refuse_escape_and_untrusted_options(make_project, tmp_path):
    pytest.importorskip("cvc5")
    pytest.importorskip("egglog")
    project = example_project(make_project)
    outside = tmp_path / "outside.py"
    outside.write_text(SOURCE, encoding="utf-8")
    client = McpClient(project)
    try:
        before, _ = client.call("gate")
        result, error = client.call("check_rewrite", path="maths.py", function="total", replacement=EQUIVALENT)
        assert not error and result["status"] == "PROVED"
        result, error = client.call("simplify_function", path="maths.py", function="total")
        assert not error and result["status"] == "PROVED"
        assert "replacement" in result
        after, _ = client.call("gate")
        assert after == before
        for args in ({"path": str(outside), "function": "total", "replacement": EQUIVALENT},
                     {"path": "../outside.py", "function": "total", "replacement": EQUIVALENT},
                     {"path": "maths.py", "function": "total", "replacement": EQUIVALENT, "rules": []},
                     {"path": "maths.py", "function": "total", "replacement": EQUIVALENT, "timeout_ms": 0},
                     {"path": "maths.py", "function": "total"}):
            result, error = client.call("check_rewrite", **args)
            assert error and "error" in result
        assert (project / "maths.py").read_text() == SOURCE
    finally:
        client.close()


def test_unsupported_source_has_no_proof(make_project, tmp_path):
    pytest.importorskip("cvc5")
    source = "def total(x: int) -> int:\n    return abs(x)\n"
    project = example_project(make_project, source)
    replacement = tmp_path / "replacement.py"
    replacement.write_text(source, encoding="utf-8")
    code, body = vkit(project, "compute", "check-rewrite", "--path", "maths.py", "--function", "total",
                      "--replacement-file", str(replacement))
    assert (code, body["status"]) == (2, "UNSUPPORTED")


def test_simplification_cli_returns_a_verified_replacement(make_project):
    pytest.importorskip("cvc5")
    pytest.importorskip("egglog")
    project = example_project(make_project)

    code, body = vkit(project, "compute", "simplify-function", "--path", "maths.py", "--function", "total")

    assert (code, body["status"]) == (0, "PROVED")
    assert body["replacement"] == "def total(x: int, y: int) -> int:\n    return (x + y)\n"
    assert body["proof"]["status"] == "PROVED"
    assert (project / "maths.py").read_text() == SOURCE

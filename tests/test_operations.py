from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

from helpers import DRIVER, McpClient, pytest_kind_check, scenario_check, vkit

SOURCE = "def total(x: int, y: int) -> int:\n    extra = x + 0\n    return extra + y\n"
EQUIVALENT = "def total(x: int, y: int) -> int:\n    return y + x\n"
WRONG = "def total(x: int, y: int) -> int:\n    return x - y\n"


def example_project(make_project, source=SOURCE):
    return make_project({"maths.py": source, "driver.py": DRIVER + 'report([("ok", True, "ok")])\n'},
                        [scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["maths.py", "driver.py"])])


def test_matchsets_cli_returns_shortest_directional_witnesses(make_project):
    pytest.importorskip("greenery")
    project = example_project(make_project)
    code, body = vkit(project, "compute", "compare-matchsets", "--old-pattern", "a*", "--new-pattern", "a+",
                      "--alphabet", "a")
    assert (code, body["status"], body["old_only"], body["new_only"]) == (1, "COUNTEREXAMPLE", "", None)
    code, body = vkit(project, "compute", "compare-matchsets", "--old-pattern", "a|b", "--new-pattern", "[ab]",
                      "--alphabet", "ab")
    assert (code, body["status"], body["old_only"], body["new_only"]) == (0, "EQUIVALENT", None, None)


def test_matchsets_mcp_preserves_gate_and_refuses_untrusted_options(make_project):
    pytest.importorskip("greenery")
    project = example_project(make_project)
    client = McpClient(project)
    try:
        before, _ = client.call("gate")
        result, error = client.call("compare_matchsets", old_pattern="a|bc", new_pattern="a|bd", alphabet="abcd")
        assert not error
        assert (result["status"], result["old_only"], result["new_only"]) == ("COUNTEREXAMPLE", "bc", "bd")
        after, _ = client.call("gate")
        assert after == before
        for args in ({"old_pattern": "a", "new_pattern": "a"},
                     {"old_pattern": "a", "new_pattern": "a", "alphabet": "a", "timeout_ms": 0},
                     {"old_pattern": "a", "new_pattern": "a", "alphabet": "a", "rules": []},
                     {"old_pattern": "a", "new_pattern": "a", "alphabet": "a", "dialect": "python"}):
            result, error = client.call("compare_matchsets", **args)
            assert error and "error" in result
        assert (project / "maths.py").read_text() == SOURCE
    finally:
        client.close()


def test_matchsets_worker_ignores_candidate_imports(tmp_path, monkeypatch):
    pytest.importorskip("greenery")
    from vkit.operations.matchsets import compare_matchsets

    (tmp_path / "greenery.py").write_text(
        "open('candidate-loaded', 'w').write('loaded')\nraise RuntimeError('candidate backend')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    result = compare_matchsets("a|b", "[ab]", "ab")
    assert result.status == "EQUIVALENT"
    assert not (tmp_path / "candidate-loaded").exists()


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


def test_reduction_transports_report_missing_engine_and_refuse_unknown_runs(make_project, monkeypatch):
    monkeypatch.delenv("VKIT_PERSES_JAR", raising=False)
    monkeypatch.delenv("VKIT_PERSES_SHA256", raising=False)
    project = make_project({"maths.py": SOURCE, "driver.py": DRIVER + 'report([("bug", False, "observed")])\n'},
                           [scenario_check("failure", "driver.py", scenarios=["bug"],
                                           inputs=["maths.py", "driver.py"])])
    code, run = vkit(project, "check", "run", "--check", "failure")
    assert code == 1
    run_id = run["runs"][0]["run_id"]

    code, body = vkit(project, "compute", "reduce-failure", "--run-id", run_id, "--path", "maths.py")

    assert (code, body["status"]) == (5, "UNAVAILABLE")
    client = McpClient(project)
    try:
        body, error = client.call("reduce_failure", run_id=run_id, path="maths.py")
        assert not error and body["status"] == "UNAVAILABLE"
        body, error = client.call("reduce_failure", run_id="missing", path="maths.py")
        assert error and "run is missing" in body["error"]
    finally:
        client.close()
    assert (project / "maths.py").read_text() == SOURCE


@pytest.mark.skipif(os.name == "nt", reason="Perses requires a POSIX runtime")
def test_real_pytest_failure_reduction_through_cli(make_project, tmp_path):
    if not os.environ.get("VKIT_PERSES_JAR") or not os.environ.get("VKIT_PERSES_SHA256"):
        pytest.skip("pinned Perses runtime is not configured")
    source = "VALUE = 0\n\ndef unused():\n    return 42\n"
    test = "from pkg.subject import VALUE\n\ndef test_value():\n    assert VALUE == 1\n"
    test_id = "test_subject.py::test_value"
    project = make_project({"pkg/subject.py": source, "test_subject.py": test, "pytest.ini": "[pytest]\n"},
                           [pytest_kind_check("failure", [test_id], ["pkg/subject.py", "test_subject.py", "pytest.ini"])])
    code, run = vkit(project, "check", "run", "--check", "failure")
    assert code == 1
    record = run["runs"][0]

    code, body = vkit(project, "compute", "reduce-failure", "--run-id", record["run_id"],
                      "--path", "pkg/subject.py", "--timeout-seconds", "120")

    assert (code, body["status"]) == (0, "REDUCED")
    assert body["reduced_source"] == "VALUE = 0\n"
    assert body["failure_id"] == test_id
    assert body["failure_observation"] == record["outcome"]["scenarios"][0]["observation"]
    assert (project / "pkg" / "subject.py").read_text() == source
    replay = tmp_path / "replay"
    (replay / "pkg").mkdir(parents=True)
    (replay / "pkg" / "subject.py").write_text(body["reduced_source"], encoding="utf-8")
    (replay / "test_subject.py").write_text(test, encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_id],
                            cwd=replay, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "assert 0 == 1" in result.stdout

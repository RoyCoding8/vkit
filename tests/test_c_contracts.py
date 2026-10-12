from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from vkit.operations.c_contracts import prove_c_contract


def _model(root, source="int count(int n) { int i=0; while(i<n) { i++; } return i; }", **changes):
    (root / "counter.c").write_text(source, encoding="utf-8")
    model = {"schema_version": 1, "source": "counter.c", "function": "count",
             "requires": ["0 <= n <= 1000000"], "ensures": ["result == n"],
             "loops": [{"invariants": ["0 <= i <= n"], "variant": "n-i"}]}
    model.update(changes)
    (root / "contract.json").write_text(json.dumps(model), encoding="utf-8")


@pytest.fixture
def frama():
    raw = os.environ.get("VKIT_FRAMAC_BIN")
    sha = os.environ.get("VKIT_FRAMAC_SHA256")
    if not raw and not sha:
        if os.environ.get("VKIT_REQUIRE_FRAMAC_ENGINE") == "1":
            pytest.fail("the required Frama-C runtime is not configured")
        pytest.skip("the pinned Frama-C runtime is not configured")
    assert raw and sha
    assert hashlib.sha256(Path(raw).read_bytes()).hexdigest() == sha


def test_million_iteration_loop_is_proved_without_unwinding(tmp_path, frama, monkeypatch):
    _model(tmp_path)
    monkeypatch.setenv("PATH", "")
    result = prove_c_contract(tmp_path, "contract.json")
    assert result["status"] == "PROVED", result
    assert result["admissible_input"]["n"] in range(1000001)
    goals = {item["goal"] for item in result["obligations"]}
    assert "typed_count_ensures_vkit_post_0" in goals
    assert "typed_count_loop_invariant_vkit_inv_0_0_established" in goals
    assert "typed_count_loop_invariant_vkit_inv_0_0_preserved" in goals
    assert "typed_count_loop_variant_positive" in goals
    assert "typed_count_loop_variant_decrease" in goals
    assert "typed_count_assert_rte_signed_overflow" in goals
    assert "--unwind" not in result["snapshot_source"]


@pytest.mark.parametrize("changes", [
    {"ensures": ["result == n+1"]},
    {"loops": [{"invariants": ["i == 0"], "variant": "n-i"}]},
    {"loops": [{"invariants": ["0 <= i <= n"], "variant": "n"}]},
    {"requires": ["n >= 0"], "loops": [{"invariants": ["i >= 0"], "variant": "n-i"}]},
])
def test_unproved_postcondition_invariant_variant_or_overflow_is_unknown(tmp_path, frama, changes):
    _model(tmp_path, **changes)
    result = prove_c_contract(tmp_path, "contract.json")
    assert result["status"] == "UNKNOWN", result


def test_false_precondition_is_not_a_vacuous_proof(tmp_path):
    _model(tmp_path, requires=["n > 2147483647"])
    result = prove_c_contract(tmp_path, "contract.json")
    assert result["status"] == "INFEASIBLE_PRECONDITION", result


def test_unsigned_precondition_arithmetic_is_mathematical(tmp_path, frama):
    _model(tmp_path, source="unsigned int count(unsigned int n) { return n; }",
           requires=["n == 4294967295 and n+1 > 0"], ensures=["result == 4294967295"], loops=[])
    result = prove_c_contract(tmp_path, "contract.json")
    assert result["status"] == "PROVED", result
    assert result["admissible_input"] == {"n": 4294967295}
    _model(tmp_path, source="unsigned int count(unsigned int n) { return 0; }",
           requires=["n == 4294967295 and n+1 > 0"], ensures=["result == 1"], loops=[])
    result = prove_c_contract(tmp_path, "contract.json")
    assert result["status"] == "UNKNOWN", result
    assert any(item["status"] == "UNPROVED" for item in result["obligations"])


def test_boolean_and_signed_boundary_contracts(tmp_path, frama):
    _model(tmp_path, source="_Bool count(_Bool flag) { return !flag; }", requires=["True"],
           ensures=["result == (not flag)"], loops=[])
    result = prove_c_contract(tmp_path, "contract.json")
    assert result["status"] == "PROVED", result
    _model(tmp_path, source="int count(int n) { return n; }", requires=["n == -2147483648"],
           ensures=["result == -2147483648"], loops=[])
    result = prove_c_contract(tmp_path, "contract.json")
    assert result["status"] == "PROVED", result


@pytest.mark.parametrize("changes", [
    {"requires": ["__import__('os').system('echo bad')"]},
    {"requires": ["n.foo == 0"]},
    {"requires": ["i == 0"]},
    {"ensures": ["result == n; axiom injected: False"]},
    {"loops": []},
    {"loops": [{"invariants": ["i >= 0"], "variant": "n-i", "axioms": ["False"]}]},
    {"requires": ["n"]},
    {"schema_version": True},
])
def test_contract_input_cannot_inject_proof_logic(tmp_path, changes):
    _model(tmp_path, **changes)
    assert prove_c_contract(tmp_path, "contract.json")["status"] == "UNSUPPORTED"


@pytest.mark.parametrize("source", [
    "int count(int n) { n++; return n; }",
    "int count(int n) { int result=0; return result; }",
    "int count(int n) { int a[1]={0}; return a[0]; }",
    "int count(int n) { while(n<0) { int i=0; } return n; }",
    "int count(int n) { /*@ assert \\false; */ return n; }",
    "int count(int n) { return external(n); }",
    "#define count ignored\nint count(int n) { return n; }",
])
def test_source_harness_overrides_and_unsupported_effects_are_rejected(tmp_path, source):
    _model(tmp_path, source=source)
    assert prove_c_contract(tmp_path, "contract.json")["status"] == "UNSUPPORTED"


def test_runtime_missing_or_mismatched_is_unavailable(tmp_path, monkeypatch):
    _model(tmp_path)
    monkeypatch.delenv("VKIT_FRAMAC_BIN", raising=False)
    monkeypatch.delenv("VKIT_FRAMAC_SHA256", raising=False)
    assert prove_c_contract(tmp_path, "contract.json")["status"] == "UNAVAILABLE"


def test_expression_size_is_bounded_before_solver(tmp_path):
    _model(tmp_path, requires=["1" * 1000 + " == n"])
    assert prove_c_contract(tmp_path, "contract.json")["status"] == "UNSUPPORTED"


def test_contract_cli_mcp_and_help(make_project, frama):
    from helpers import McpClient, vkit

    project = make_project({}, [], accept=False)
    _model(project)
    code, body = vkit(project, "compute", "prove-c-contract", "--path", "contract.json")
    assert (code, body["status"]) == (0, "PROVED"), body
    client = McpClient(project)
    try:
        body, error = client.call("prove_c_contract", path="contract.json")
        assert not error and body["status"] == "PROVED", body
        help_, error = client.call("help", query="prove_c_contract")
        assert not error and "prove_c_contract" in json.dumps(help_)
    finally:
        client.close()

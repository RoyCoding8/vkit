from __future__ import annotations

import shutil
from dataclasses import replace

import pytest
from test_formal_policy import component

from vkit.formal import ledger
from vkit.formal.policy import FormalComponent


def accepted_component():
    entry = component()
    package = ledger.load_package()
    entry["package"]["sha256"] = package.identity.sha256
    return FormalComponent.from_json(entry)


def test_owned_package_binds_the_complete_required_surface():
    policy = accepted_component()
    assert ledger.verify_package(policy).identity == policy.package
    assert ledger.obligation_ids(policy) == (
        "allocation:ledger_map_accounting", "allocation:new", "allocation:reserve",
        "allocation:release", "allocation:reserve_batch", "allocation:apply_command",
        "allocation:run_commands",
    )
    expected = ledger.render_expected(policy)
    assert "const N: usize = 7;" in expected
    assert "candidate_api::api::reserve(input, id, owner, amount)" in expected
    assert "ledger::ledger_api::reserve_result_view(input.view(), id, amount)" in expected
    assert "pub fn expected_run_commands<const M: usize>" in expected
    assert "__VKIT_" not in expected


def test_changing_an_api_binding_cannot_replace_its_expected_statement():
    policy = accepted_component()
    other = replace(policy, bindings=replace(policy.bindings, reserve="replacement::reserve"))
    assert ledger.render_expected(other).replace("candidate_api::replacement::reserve(",
                                                "candidate_api::api::reserve(") == ledger.render_expected(policy)
    assert other.digest != policy.digest


def test_custom_representation_keeps_owned_invariants_and_operation_contracts():
    native = accepted_component()
    columns = replace(native, representation="columns::Columns")
    expected = ledger.render_expected(columns)
    assert "candidate_api::columns::Columns<N>" in expected
    assert "ledger::ledger_api::valid_view::<N>(input.view())" in expected
    assert "ledger::ledger_api::valid_view::<N>(out.0.view())" in expected
    assert "input.valid()" not in expected
    assert expected.replace("candidate_api::columns::Columns<N>", "ledger::ledger_api::Ledger<N>") == ledger.render_expected(native)
    assert columns.digest != native.digest


def test_package_identity_binds_raw_model_and_template_bytes(tmp_path, monkeypatch):
    original = ledger.load_package()
    shutil.copytree(original.root, tmp_path / "package")
    policy = accepted_component()
    monkeypatch.setattr(ledger, "_ROOT", tmp_path / "package")
    assert ledger.load_package().identity == original.identity
    path = tmp_path / "package" / "ledger.rs"
    path.write_bytes(path.read_bytes() + b"\r\n")
    assert ledger.load_package().identity != original.identity
    with pytest.raises(ValueError, match="installed ledger package"):
        ledger.verify_package(policy)

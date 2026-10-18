from __future__ import annotations

import pytest

from vkit.operations.contracts import compare_contracts


@pytest.mark.parametrize("profile,category", [
    ("protobuf-file", "FILE"), ("protobuf-package", "PACKAGE"),
    ("protobuf-wire-json", "WIRE_JSON"), ("protobuf-wire", "WIRE"),
])
def test_protobuf_profile_preserves_fixed_category_and_result(monkeypatch, tmp_path, profile, category):
    from vkit.operations import protobuf

    calls = []

    def compare(root, old, new, selected, seconds):
        calls.append((root, old, new, selected, seconds))
        return {"status": "BREAKING", "diagnostics": [{"rule_id": "FIELD_SAME_TYPE"}]}

    monkeypatch.setattr(protobuf, "check_proto_compatibility", compare)
    assert compare_contracts(tmp_path, "old", "new", profile, 5) == {
        "operation": "compare_contracts", "profile": profile,
        "status": "BREAKING", "diagnostics": [{"rule_id": "FIELD_SAME_TYPE"}]}
    assert calls == [(tmp_path, "old", "new", category, 5)]


def test_contract_profile_cannot_select_custom_rules(tmp_path):
    with pytest.raises(ValueError, match="profile must be one of"):
        compare_contracts(tmp_path, "old", "new", "ignore-breaking")

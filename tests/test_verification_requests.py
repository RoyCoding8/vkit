from __future__ import annotations

from copy import deepcopy

import pytest

from vkit.requests import PackageVerificationRequest
from vkit.schemas import SchemaValidationError


def request():
    return {"schema_version": 2,
            "claim": {"kind": "package", "component": "allocation",
                      "policy_sha256": "a" * 64, "package_sha256": "b" * 64},
            "candidate": {"kind": "git_commit", "oid": "c" * 40}}


def test_request_retains_exact_claim_and_source_but_excludes_scheduling_from_identity():
    raw = request()
    parsed = PackageVerificationRequest.from_json({**raw, "wait_seconds": 60, "reuse": True})
    assert parsed.semantic_json() == raw
    assert parsed.to_json() == {**raw, "wait_seconds": 60, "reuse": True}
    assert parsed.claim.component == "allocation"
    assert parsed.candidate.oid == "c" * 40


@pytest.mark.parametrize("field,value", [
    ("schema_version", 1), ("schema_version", True),
    ("wait_seconds", 61), ("wait_seconds", True), ("reuse", 1),
    ("authority", {}), ("checker", "custom"), ("goal", {"kind": "implements"}),
    ("candidate", {"kind": "working_tree", "target_commit": "a" * 40}),
    ("candidate", {"kind": "git_commit", "oid": "HEAD"}),
    ("candidate", {"kind": "git_commit", "oid": "c" * 40 + "\n"}),
    ("candidate", {"kind": "git_commit", "oid": "c" * 41}),
    ("candidate", {"kind": "git_commit", "oid": "c" * 64 + "\n"}),
])
def test_request_rejects_unknown_fields_nonexact_sources_and_invalid_scheduling(field, value):
    with pytest.raises(SchemaValidationError):
        PackageVerificationRequest.from_json({**request(), field: value})


@pytest.mark.parametrize("field,value", [
    ("kind", "finite_contract"), ("component", "allocation\n"),
    ("policy_sha256", "a" * 64 + "\n"), ("policy_sha256", "A" * 64),
    ("package_sha256", "b" * 63), ("obligations", []),
])
def test_claim_cannot_change_kind_or_supply_a_partial_identity(field, value):
    raw = deepcopy(request())
    raw["claim"][field] = value
    with pytest.raises(SchemaValidationError):
        PackageVerificationRequest.from_json(raw)


@pytest.mark.parametrize("field", ["component", "policy_sha256", "package_sha256"])
def test_claim_requires_every_identity_pin(field):
    raw = request()
    del raw["claim"][field]
    with pytest.raises(SchemaValidationError):
        PackageVerificationRequest.from_json(raw)


def test_old_implements_wire_is_not_a_package_claim():
    with pytest.raises(SchemaValidationError):
        PackageVerificationRequest.from_json({"component": "allocation",
            "goal": {"kind": "implements"}, "candidate": {"kind": "working_tree"}})

from __future__ import annotations

from copy import deepcopy

import pytest

from vkit.formal.policy import VerificationRequest
from vkit.manifest import ManifestError, parse_manifest_data
from vkit.paths import Project
from vkit.schemas import SchemaValidationError


def component():
    return {"id": "allocation", "package": {"id": "vkit.resource-ledger", "version": "0.1.0",
                                            "sha256": "a" * 64},
            "contract": "ledger.source.v1", "source": {"entry": "src/allocation.rs"},
            "profile": "verus-linux-x86_64-v1", "parameters": {"max_records": 7},
            "bindings": {"new": "api::new", "reserve": "api::reserve", "release": "api::release",
                         "reserve_batch": "api::reserve_batch", "apply_command": "api::apply_command",
                         "run_commands": "api::run_commands"}, "required": True}


def parse(tmp_path, entry):
    return parse_manifest_data({"schema_version": 3, "checks": [], "formal": {"components": [entry]}},
                               project=Project(tmp_path, tmp_path / ".git"), origin="test manifest")


def test_manifest_resolves_a_complete_owned_component(tmp_path):
    original = component()
    manifest = parse(tmp_path, original)
    actual = manifest.require_component("allocation")
    assert actual.to_json() == original
    assert actual.max_records == 7
    assert actual.bindings.run_commands == "api::run_commands"
    assert not manifest.checks


@pytest.mark.parametrize("field,value", [
    ("contract", "ledger.total.v1"), ("profile", "custom-verus"),
    ("bindings", {"reserve": "reserve"}),
    ("source", {"entry": "../outside.rs"}), ("source", {"entry": "src/./file.rs"}),
    ("source", {"entry": "C:/outside.rs"}), ("source", {"entry": "src\\file.rs"}),
    ("parameters", {"max_records": 0}), ("parameters", {"max_records": True}),
])
def test_manifest_rejects_weakened_or_unsupported_component_policy(tmp_path, field, value):
    entry = component()
    entry[field] = value
    with pytest.raises(ManifestError):
        parse(tmp_path, entry)


@pytest.mark.parametrize("binding", ["crate::reserve", "unsafe::reserve", "_", "reserve(); assume(false)"])
def test_binding_paths_cannot_inject_rust_or_replace_the_crate_prefix(tmp_path, binding):
    entry = component()
    entry["bindings"]["reserve"] = binding
    with pytest.raises(ManifestError):
        parse(tmp_path, entry)


def test_representation_is_accepted_policy_and_round_trips(tmp_path):
    entry = component()
    entry["representation"] = "columns::Columns"
    parsed = parse(tmp_path, entry).require_component("allocation")
    assert parsed.representation == "columns::Columns"
    assert parsed.to_json() == entry


@pytest.mark.parametrize("representation", ["unsafe::State", "_", "State<u32>", "State; assume(false)"])
def test_representation_cannot_inject_rust_or_set_a_concrete_size(tmp_path, representation):
    entry = component()
    entry["representation"] = representation
    with pytest.raises(ManifestError):
        parse(tmp_path, entry)


def test_duplicate_components_and_unknown_obligations_are_rejected(tmp_path):
    entry = component()
    raw = {"schema_version": 3, "checks": [], "formal": {"components": [entry, deepcopy(entry)]}}
    with pytest.raises(ManifestError, match="duplicate formal component"):
        parse_manifest_data(raw, project=Project(tmp_path, tmp_path / ".git"), origin="duplicate")
    entry["obligations"] = ["only-the-easy-one"]
    with pytest.raises(ManifestError):
        parse(tmp_path, entry)


def test_wait_and_reuse_are_excluded_from_verification_request_semantics():
    raw = {"component": "allocation", "goal": {"kind": "implements"},
           "candidate": {"kind": "git_commit", "oid": "a" * 40, "target_commit": "b" * 40}}
    first = VerificationRequest.from_json(raw)
    second = VerificationRequest.from_json({**raw, "wait_seconds": 60, "reuse": True})
    assert first.semantic_json() == second.semantic_json() == raw
    assert (first.wait_seconds, second.wait_seconds) == (0, 60)
    assert (first.reuse, second.reuse) == (False, True)


@pytest.mark.parametrize("change", [
    {"candidate": {"kind": "working_tree", "target_commit": "a" * 40}},
    {"candidate": {"kind": "git_commit", "oid": "HEAD"}},
    {"goal": {"kind": "change"}}, {"goal": {"kind": "implements", "obligations": []}},
    {"wait_seconds": 61}, {"checker": "my-own-checker"}, {"authority": "protected"},
])
def test_request_cannot_override_the_accepted_claim_or_authority(change):
    raw = {"component": "allocation", "goal": {"kind": "implements"}, "candidate": {"kind": "working_tree"}}
    with pytest.raises(SchemaValidationError):
        VerificationRequest.from_json({**raw, **change})

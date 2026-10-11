from __future__ import annotations

from collections.abc import Callable
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from vkit.operations import policies
from vkit.operations.policies import _CedarText, _WitnessError, compare_authorization_policies

_ENGINE_CONFIGURED = all(os.environ.get(name) for name in (
    "VKIT_CEDAR_PATH", "VKIT_CEDAR_SHA256", "VKIT_CVC5_PATH", "VKIT_CVC5_SHA256"))
_ENGINE_REQUIRED = os.environ.get("VKIT_REQUIRE_POLICY_ENGINE") == "1"
_ENGINE_SKIP = pytest.mark.skipif(not _ENGINE_CONFIGURED and not _ENGINE_REQUIRED,
                                  reason="requires release-pinned Cedar and cvc5; CI enables VKIT_REQUIRE_POLICY_ENGINE")


def _bundle(*, before: list[str], after: list[str]) -> dict[str, object]:
    return {
        "version": 1,
        "schema": (
            "entity User = { department: String };\n"
            "entity Document = { classification: String };\n"
            'action "view" appliesTo { principal: [User], resource: [Document], context: {} };\n'
        ),
        "action": 'Action::"view"',
        "principal_type": "User",
        "resource_type": "Document",
        "before": before,
        "after": after,
    }


def _write_bundle(root: Path, bundle: dict[str, object]) -> str:
    (root / "policies.json").write_text(json.dumps(bundle), encoding="utf-8")
    return "policies.json"


def test_witness_parser_converts_attributes_and_entity_parents() -> None:
    output = '''✗ Policy sets are equivalent: DOES NOT HOLD
  Counterexample found:
principal: User::"alice", action: Action::"view", resource: Document::"doc"
context: {region: "west"}
entities: [
  Group::"staff",
  User::"alice" in [Group::"staff"] {
    department: "eng",
    active: true,
    level: 3,
    flags: {"a", "b"},
  },
]'''

    from vkit.operations.policies import _parse_witness

    request, entities = _parse_witness(output)
    assert request == {
        "principal": 'User::"alice"',
        "action": 'Action::"view"',
        "resource": 'Document::"doc"',
        "context": {"region": "west"},
    }
    assert entities == [
        {"uid": {"type": "Group", "id": "staff"}, "attrs": {}, "parents": []},
        {"uid": {"type": "User", "id": "alice"},
         "attrs": {"department": "eng", "active": True, "level": 3, "flags": ["a", "b"]},
         "parents": [{"type": "Group", "id": "staff"}]},
    ]
    with pytest.raises(_WitnessError, match="trailing output"):
        policies._parse_witness(output + "\nextra output")


def test_witness_parser_rejects_unknown_value_syntax() -> None:
    with pytest.raises(_WitnessError, match="unsupported Cedar value"):
        _CedarText('ip("127.0.0.1")').value()


def test_bundle_rejects_unknown_fields_and_unsupported_action(tmp_path: Path) -> None:
    bundle = _bundle(before=['permit(principal, action, resource);'], after=['permit(principal, action, resource);'])
    bundle["extra"] = "ignored is unsafe"
    _write_bundle(tmp_path, bundle)
    with pytest.raises(ValueError, match="exactly"):
        compare_authorization_policies(tmp_path, "policies.json")

    del bundle["extra"]
    bundle["action"] = 'Ns::Action::"view"'
    _write_bundle(tmp_path, bundle)
    with pytest.raises(ValueError, match="Action UID"):
        compare_authorization_policies(tmp_path, "policies.json")


def test_missing_engine_pins_are_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VKIT_CEDAR_PATH", raising=False)
    monkeypatch.delenv("VKIT_CEDAR_SHA256", raising=False)
    monkeypatch.delenv("VKIT_CVC5_PATH", raising=False)
    monkeypatch.delenv("VKIT_CVC5_SHA256", raising=False)
    path = _write_bundle(tmp_path, _bundle(before=['permit(principal, action, resource);'],
                                          after=['permit(principal, action, resource);']))
    result = compare_authorization_policies(tmp_path, path)
    assert result["status"] == "UNAVAILABLE"


def test_version_probe_timeout_returns_unknown_with_shared_deadline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_bundle(tmp_path, _bundle(before=[], after=[]))
    monkeypatch.setattr(policies, "pinned_binary", lambda *_: (Path("engine"), "0" * 64))

    def timed_out(command: list[str], *_: object, **__: object) -> tuple[int, bytes]:
        raise subprocess.TimeoutExpired(command, 0.01)

    monkeypatch.setattr(policies, "_run", timed_out)
    result = compare_authorization_policies(tmp_path, path, timeout_seconds=1)
    assert result["status"] == "UNKNOWN"
    assert "time budget" in result["detail"]


def test_request_outside_declared_model_slice_is_rejected(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path, _bundle(before=[], after=[]))
    bundle = policies._load_bundle(tmp_path, path)
    request = {"principal": 'OtherUser::"alice"', "action": 'Action::"view"',
               "resource": 'Document::"doc"'}
    assert not policies._request_matches_scope(request, bundle)


def test_engine_output_limit_is_enforced_before_reading_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def oversized_run(_argv: object, *, stdout_path: Path, stderr_path: Path,
                      should_cancel: Callable[[], bool], **_: object) -> object:
        stdout_path.write_bytes(b"x" * (policies.MAX_OUTPUT_BYTES + 1))
        stderr_path.write_bytes(b"")
        assert should_cancel()
        return policies.proc.Exit(None, cancelled=True)

    monkeypatch.setattr(policies.proc, "run", oversized_run)
    with pytest.raises(_WitnessError, match="output exceeded"):
        policies._run(Path("engine"), [], tmp_path, {}, time.monotonic() + 1)


@_ENGINE_SKIP
def test_real_symcc_counterexample_is_replayed_by_cedar(tmp_path: Path) -> None:
    before = ['permit(principal, action == Action::"view", resource) when { principal.department == "eng" };']
    after = ['permit(principal, action == Action::"view", resource) when { principal.department == "sales" };']
    path = _write_bundle(tmp_path, _bundle(before=before, after=after))
    result = compare_authorization_policies(tmp_path, path, timeout_seconds=30)
    assert result["status"] == "COUNTEREXAMPLE"
    witness = result["witness"]
    assert isinstance(witness, dict)
    assert witness["before_decision"] != witness["after_decision"]
    assert witness["before_decision"] == "ALLOW"
    assert witness["after_decision"] == "DENY"
    entities = witness["entities"]
    principal = next(entity for entity in entities if entity["uid"]["type"] == "User"
                     and entity["uid"]["id"] == witness["request"]["principal"].split('"')[1])
    assert principal["attrs"]["department"] == "eng"


@_ENGINE_SKIP
def test_real_symcc_equivalence_uses_captured_policy_sets(tmp_path: Path) -> None:
    policy = ['permit(principal, action == Action::"view", resource) when { principal.department == "eng" };']
    path = _write_bundle(tmp_path, _bundle(before=policy, after=policy))
    result = compare_authorization_policies(tmp_path, path, timeout_seconds=30)
    assert result["status"] == "EQUIVALENT"
    assert result["witness"] is None


@_ENGINE_SKIP
def test_real_cedar_rejects_invalid_policy_model_as_unsupported(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path, _bundle(before=["this is not Cedar"],
                                          after=['permit(principal, action == Action::"view", resource);']))
    result = compare_authorization_policies(tmp_path, path, timeout_seconds=30)
    assert result["status"] == "UNSUPPORTED"


@_ENGINE_SKIP
def test_real_symcc_compares_empty_default_deny_policy_set(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path, _bundle(before=[], after=['permit(principal, action == Action::"view", resource);']))
    result = compare_authorization_policies(tmp_path, path, timeout_seconds=30)
    assert result["status"] == "COUNTEREXAMPLE", result
    witness = result["witness"]
    assert witness["before_decision"] == "DENY"
    assert witness["after_decision"] == "ALLOW"

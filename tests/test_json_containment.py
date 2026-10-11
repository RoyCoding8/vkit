from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft4Validator

from vkit.operations import json_containment
from vkit.operations.json_containment import compare_json_containment


def _write_schema(root: Path, name: str, schema: object) -> str:
    (root / name).write_text(json.dumps(schema), encoding="utf-8")
    return name


def _compare(root: Path, old: object, new: object) -> dict[str, object]:
    old_path = _write_schema(root, "old.json", old)
    new_path = _write_schema(root, "new.json", new)
    return compare_json_containment(root, old_path, new_path)


def test_nested_object_and_array_containment_uses_old_subset_new(tmp_path: Path) -> None:
    old = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id"],
        "additionalProperties": False,
    }
    new = {
        "type": "object",
        "properties": {
            "id": {"type": "number"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id"],
        "additionalProperties": False,
    }

    result = _compare(tmp_path, old, new)

    assert result["status"] == "COMPATIBLE"
    assert result["relation"] == "old_subset_new"
    assert result["backend_answer"] is True
    assert result["witness"] is None
    assert result["witness_status"] == "not_generated"
    assert result["backend_version"] == "0.0.8"


def test_homogeneous_array_integer_values_are_contained_in_number_values(tmp_path: Path) -> None:
    result = _compare(
        tmp_path,
        {"type": "array", "items": {"type": "integer"}},
        {"type": "array", "items": {"type": "number"}},
    )

    assert result["status"] == "COMPATIBLE"
    assert result["backend_answer"] is True


def test_required_property_direction_returns_negative_without_witness(tmp_path: Path) -> None:
    old = {"type": "object", "properties": {"id": {"type": "string"}}}
    new = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}

    result = _compare(tmp_path, old, new)

    assert result["status"] == "BREAKING"
    assert result["backend_answer"] is False
    assert result["relation"] == "old_subset_new"
    assert result["witness"] is None
    assert result["witness_status"] == "not_generated"


def test_additional_properties_direction_is_preserved(tmp_path: Path) -> None:
    open_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    closed_schema = {**open_schema, "additionalProperties": False}

    open_to_closed = _compare(tmp_path, open_schema, closed_schema)
    assert open_to_closed["status"] == "BREAKING"
    assert open_to_closed["backend_answer"] is False

    closed_to_open = _compare(tmp_path, closed_schema, open_schema)
    assert closed_to_open["status"] == "COMPATIBLE"
    assert closed_to_open["backend_answer"] is True


def test_nested_optional_property_schema_is_still_compared(tmp_path: Path) -> None:
    old = {"type": "object", "properties": {"payload": {"type": "string"}}}
    new = {"type": "object", "properties": {"payload": {"type": "integer"}}}

    result = _compare(tmp_path, old, new)

    assert result["status"] == "BREAKING"
    assert result["backend_answer"] is False


def test_empty_schema_is_universal_and_equal_empty_schemas_are_compatible(tmp_path: Path) -> None:
    narrower = _compare(tmp_path, {}, {"type": "string"})
    assert narrower["status"] == "BREAKING"
    assert narrower["backend_answer"] is False

    equal = _compare(tmp_path, {}, {})
    assert equal["status"] == "COMPATIBLE"
    assert equal["backend_answer"] is True


@pytest.mark.parametrize("schema", [
    {"type": "string", "not": {"enum": [""]}, "pattern": ".+"},
    {"type": "object", "properties": {"x": {"type": "string", "format": "email"}}},
    {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "string"},
    {"type": "object", "properties": {"x": {"type": "string"}}, "$ref": "https://example.com/schema"},
    {"type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "integer"}},
     "required": ["missing"]},
    {"type": "array", "items": [{"type": "string"}]},
    {"type": "number", "minimum": 0},
    {"type": ["integer", "string"]},
    {"enum": ["x"]},
    {"type": "string", "x-custom": True},
    {"type": "object", "properties": {"x": {"$schema": "http://json-schema.org/draft-04/schema#"}}},
])
def test_rejects_unmodeled_constructs_before_backend_call(tmp_path: Path, monkeypatch, schema) -> None:
    old_path = _write_schema(tmp_path, "old.json", schema)
    new_path = _write_schema(tmp_path, "new.json", {"type": "string"})

    def unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("unsupported schemas must be rejected before backend execution")

    monkeypatch.setattr(json_containment.subprocess, "run", unexpected_call)
    result = compare_json_containment(tmp_path, old_path, new_path)

    assert result["status"] == "UNSUPPORTED"
    assert result["backend_answer"] is None


def test_real_issue_36_pair_is_rejected_before_backend_call(tmp_path: Path, monkeypatch) -> None:
    old = {"type": "string", "not": {"enum": [""]}}
    new = {"type": "string", "pattern": ".+"}
    old_path = _write_schema(tmp_path, "old.json", old)
    new_path = _write_schema(tmp_path, "new.json", new)
    witness = "\n"
    assert Draft4Validator(old).is_valid(witness)
    assert not Draft4Validator(new).is_valid(witness)

    def unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("issue #36 keywords must be rejected before backend execution")

    monkeypatch.setattr(json_containment.subprocess, "run", unexpected_call)
    result = compare_json_containment(tmp_path, old_path, new_path)

    assert result["status"] == "UNSUPPORTED"


def test_backend_none_preserves_undecided_result(tmp_path: Path) -> None:
    result = _compare(tmp_path, {"type": "number"}, {"type": "integer"})

    assert result["status"] == "UNKNOWN"
    assert result["backend_answer"] is None
    assert result["witness"] is None


def test_capture_rejects_symlinked_schema_path(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"type":"string"}', encoding="utf-8")
    link = tmp_path / "old.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    new_path = _write_schema(tmp_path, "new.json", {"type": "string"})

    result = compare_json_containment(tmp_path, "old.json", new_path)

    assert result["status"] == "UNSUPPORTED"
    assert result["old_sha256"] is None


def test_schema_byte_depth_and_node_limits_are_enforced(tmp_path: Path) -> None:
    oversized_path = tmp_path / "oversized.json"
    oversized_path.write_bytes(b" " * (json_containment.MAX_SCHEMA_BYTES + 1))
    new_path = _write_schema(tmp_path, "new.json", {"type": "string"})
    oversized = compare_json_containment(tmp_path, oversized_path.name, new_path)
    assert oversized["status"] == "UNSUPPORTED"

    deep: object = {"type": "string"}
    for _ in range(json_containment.MAX_SCHEMA_DEPTH):
        deep = {"type": "object", "properties": {"x": deep}}
    deep_path = _write_schema(tmp_path, "deep.json", deep)
    assert compare_json_containment(tmp_path, deep_path, new_path)["status"] == "UNSUPPORTED"

    many_properties = {f"p{index}": {"type": "string"}
                       for index in range(json_containment.MAX_SCHEMA_NODES)}
    many_path = _write_schema(tmp_path, "many.json", {"type": "object", "properties": many_properties})
    assert compare_json_containment(tmp_path, many_path, new_path)["status"] == "UNSUPPORTED"


def test_hard_deadline_returns_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_path = _write_schema(tmp_path, "old.json", {"type": "string"})
    new_path = _write_schema(tmp_path, "new.json", {"type": "string"})

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("worker", 1)

    monkeypatch.setattr(json_containment.subprocess, "run", timed_out)
    result = compare_json_containment(tmp_path, old_path, new_path)

    assert result["status"] == "UNKNOWN"
    assert result["witness"] is None


def test_malformed_worker_output_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_path = _write_schema(tmp_path, "old.json", {"type": "string"})
    new_path = _write_schema(tmp_path, "new.json", {"type": "string"})

    def malformed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess("worker", 0, stdout="not json", stderr="")

    monkeypatch.setattr(json_containment.subprocess, "run", malformed)
    result = compare_json_containment(tmp_path, old_path, new_path)

    assert result["status"] == "UNAVAILABLE"
    assert result["backend_answer"] is None


def test_version_drift_is_reported_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_containment, "version", lambda package: "wrong")

    installed, reason = json_containment._runtime_versions()

    assert installed == {"jsonsubschema": "wrong"}
    assert reason == "requires jsonsubschema 0.0.8; found wrong"


def test_positive_backend_answers_match_a_bounded_draft4_instance_corpus() -> None:
    jsonsubschema = pytest.importorskip("jsonsubschema")
    schemas = [
        {},
        {"type": "string"},
        {"type": "integer"},
        {"type": "number"},
        {"type": "object"},
        {"type": "object", "properties": {"x": {"type": "string"}}},
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        {"type": "object", "properties": {"x": {"type": "integer"}}},
        {"type": "array", "items": {"type": "integer"}},
        {"type": "array", "items": {"type": "number"}},
    ]
    scalar_values = [None, False, True, -1, 0, 1, 1.5, "", "x", "\n"]
    instances: list[object] = [*scalar_values, [], {}, *([value] for value in scalar_values)]
    instances.extend({"x": value} for value in scalar_values)
    instances.extend({"y": value} for value in scalar_values)
    instances.extend([{"x": {"y": "v"}}, {"x": [1]}, [{"x": "v"}], [1, "v"]])

    for old in schemas:
        old_validator = Draft4Validator(old)
        for new in schemas:
            answer = jsonsubschema.isSubschema(old, new)
            if answer is True:
                new_validator = Draft4Validator(new)
                for instance in instances:
                    if old_validator.is_valid(instance):
                        assert new_validator.is_valid(instance), (old, new, instance)

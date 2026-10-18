from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vkit.operations import openapi
from vkit.operations.openapi import OASDIFF_VERSION, check_openapi_compatibility


def test_backend_output_limit_combines_stdout_and_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(openapi, "MAX_BACKEND_OUTPUT_BYTES", 100_000)
    script = "import os; os.write(1, b'o' * 60000); os.write(2, b'e' * 60000)"
    with pytest.raises(openapi._BackendOutputExceeded):
        openapi._run(Path(sys.executable), ["-c", script], tmp_path,
                     dict(os.environ), time.monotonic() + 10)


def test_backend_preserves_output_within_combined_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(openapi, "MAX_BACKEND_OUTPUT_BYTES", 100_000)
    script = "import os; os.write(1, b'o' * 20000); os.write(2, b'e' * 30000)"
    result = openapi._run(Path(sys.executable), ["-c", script], tmp_path,
                          dict(os.environ), time.monotonic() + 10)
    assert result.returncode == 0
    assert result.stdout == b'o' * 20000
    assert result.stderr == b'e' * 30000


@pytest.fixture
def configured_oasdiff(monkeypatch: pytest.MonkeyPatch) -> Path:
    raw_path = os.environ.get("VKIT_OASDIFF_BIN")
    expected = os.environ.get("VKIT_OASDIFF_SHA256")
    if not raw_path or not expected:
        if raw_path or expected:
            pytest.fail("the configured oasdiff helper requires both its path and SHA-256")
        pytest.skip(f"a pinned oasdiff {OASDIFF_VERSION} binary is not configured")
    binary = Path(raw_path).resolve()
    if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != expected.lower():
        pytest.fail("the configured oasdiff helper does not match its SHA-256")
    monkeypatch.setenv("VKIT_OASDIFF_BIN", str(binary))
    monkeypatch.setenv("VKIT_OASDIFF_SHA256", expected.lower())
    return binary


def _write(root: Path, relative: str, document: dict) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _write_text(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _spec(schema: dict, *, request: bool = True) -> dict:
    operation = {
        "operationId": "createItem",
        "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": schema}}}},
    }
    if request:
        operation["requestBody"] = {"required": True, "content": {
            "application/json": {"schema": {"type": "object", "properties": {
                "name": {"type": "string"}}}}}}
    return {"openapi": "3.0.3", "info": {"title": "Items", "version": "1"},
            "paths": {"/items": {"post": operation}}}


def _pair(root: Path, old: dict, new: dict) -> None:
    _write(root, "old/openapi.json", old)
    _write(root, "new/openapi.json", new)


def test_required_request_field_is_breaking_with_method_and_path(
        tmp_path, configured_oasdiff, monkeypatch):
    old = _spec({"type": "string"})
    new = _spec({"type": "string"})
    new["paths"]["/items"]["post"]["requestBody"]["content"]["application/json"][
        "schema"]["required"] = ["name"]
    _pair(tmp_path, old, new)
    untrusted_config = tmp_path / "untrusted-oasdiff.yaml"
    untrusted_config.write_text("breaking: [invalid\n", encoding="utf-8")
    monkeypatch.setenv("OASDIFF_CONFIG", str(untrusted_config))

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "BREAKING"
    finding = next(item for item in result["diagnostics"]
                   if item["rule_id"] == "request-property-became-required")
    assert (finding["operation"], finding["path"], finding["severity"]) == (
        "POST", "/items", "error")


@pytest.mark.parametrize("version", ["3.0.3", "3.1.2"])
def test_nullable_response_change_is_reported_by_oasdiff(tmp_path, configured_oasdiff, version):
    old = _spec({"type": "string"})
    new = _spec({"type": "string", "nullable": True} if version == "3.0.3"
                else {"type": ["string", "null"]})
    old["openapi"] = new["openapi"] = version
    _pair(tmp_path, old, new)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "BREAKING"
    assert any(item["severity"] in {"error", "warning"}
               for item in result["diagnostics"] if item.get("path") == "/items")


def test_optional_response_property_addition_is_compatible(tmp_path, configured_oasdiff):
    old = _spec({"type": "object", "properties": {"id": {"type": "string"}}})
    new = _spec({"type": "object", "properties": {
        "id": {"type": "string"}, "label": {"type": "string"}}})
    new["info"]["version"] = "2"
    _pair(tmp_path, old, new)
    before = (tmp_path / "old/openapi.json").read_bytes()
    after = (tmp_path / "new/openapi.json").read_bytes()

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "COMPATIBLE"
    assert result["diagnostics"] == []
    assert (tmp_path / "old/openapi.json").read_bytes() == before
    assert (tmp_path / "new/openapi.json").read_bytes() == after
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == [
        "new", "new/openapi.json", "old", "old/openapi.json"]


def test_external_refs_and_allof_composition_are_compared_old_to_new(tmp_path, configured_oasdiff):
    base = {"openapi": "3.0.3", "info": {"title": "Items", "version": "1"},
            "paths": {"/items": {"post": {"operationId": "createItem",
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"allOf": [{"$ref": "./components.yaml#/components/schemas/Item"},
                                           {"type": "object", "properties": {
                                               "label": {"type": "string"}}}]}}}},
                "responses": {"200": {"description": "ok"}}}}}}
    _write(tmp_path, "old/openapi.json", base)
    _write(tmp_path, "new/openapi.json", base)
    _write_text(tmp_path, "old/components.yaml", """components:
  schemas:
    Item:
      type: object
      properties:
        id:
          type: string
""")
    _write_text(tmp_path, "new/components.yaml", """components:
  schemas:
    Item:
      type: object
      properties:
        id:
          type: string
      required: [id]
""")

    forward = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")
    reverse = check_openapi_compatibility(tmp_path, "new/openapi.json", "old/openapi.json")

    assert forward["status"] == "BREAKING"
    assert any(item["path"] == "/items" for item in forward["diagnostics"] if "path" in item)
    assert reverse["status"] == "COMPATIBLE"
    assert forward["old_snapshot_sha256"] != forward["new_snapshot_sha256"]
    assert forward["old_snapshot_file_count"] == forward["new_snapshot_file_count"] == 2


def test_remote_ref_is_unsupported_even_when_nested_in_uncompared_content(tmp_path):
    old = _spec({"type": "string"})
    old["components"] = {"examples": {"unused": {"$ref": "https://example.invalid/x.yaml"}}}
    _write(tmp_path, "old/openapi.json", old)
    _write(tmp_path, "new/openapi.json", _spec({"type": "string"}))

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert "remote $refs" in result["detail"]
    assert len(result["old_source_sha256"]) == 64


@pytest.mark.parametrize(("keyword", "reference"), [
    ("$dynamicRef", "#/$defs/Item"),
    ("$dynamicRef", "https://example.invalid/schema.json#/$defs/Item"),
    ("$dynamicRef", "file:///outside/schema.json#/Item"),
    ("$recursiveRef", "#"),
])
def test_dynamic_and_recursive_refs_are_unsupported(tmp_path, keyword, reference):
    old = _spec({"type": "string"})
    old["components"] = {"schemas": {"Unused": {keyword: reference}}}
    _write(tmp_path, "old/openapi.json", old)
    _write(tmp_path, "new/openapi.json", _spec({"type": "string"}))

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert "$dynamicRef" in result["detail"]
    assert "$recursiveRef" in result["detail"]


def test_json_schema_base_uri_is_unsupported(tmp_path):
    old = _spec({"type": "string"})
    old["components"] = {"schemas": {"Unused": {
        "$id": "https://example.invalid/base.json", "$ref": "schema.json"}}}
    _write(tmp_path, "old/openapi.json", old)
    _write(tmp_path, "new/openapi.json", _spec({"type": "string"}))

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert "$id base URI" in result["detail"]


@pytest.mark.parametrize("document", [
    '{"openapi":"3.0.3","openapi":"3.1.0"}',
    '{"openapi":"3.0.3",',
])
def test_duplicate_or_malformed_json_is_unsupported(tmp_path, document):
    (tmp_path / "old.json").write_text(document, encoding="utf-8")

    result = check_openapi_compatibility(tmp_path, "old.json", "old.json")

    assert result["status"] == "UNSUPPORTED"
    assert result["diagnostics"] == []


def test_duplicate_yaml_keys_are_unsupported(tmp_path):
    (tmp_path / "old.yaml").write_text(
        'openapi: "3.0.3"\nopenapi: "3.1.0"\n', encoding="utf-8")

    result = check_openapi_compatibility(tmp_path, "old.yaml", "old.yaml")

    assert result["status"] == "UNSUPPORTED"
    assert "unambiguous YAML" in result["detail"]


def test_recursive_yaml_alias_is_unsupported_before_backend_execution(tmp_path):
    (tmp_path / "openapi.yaml").write_text(
        'openapi: "3.0.3"\ninfo: {title: test, version: "1"}\npaths: {}\n'
        'x-recursive: &loop {self: *loop}\n', encoding="utf-8")

    result = check_openapi_compatibility(tmp_path, "openapi.yaml", "openapi.yaml")

    assert result["status"] == "UNSUPPORTED"
    assert "recursive YAML aliases" in result["detail"]


def test_yaml_merge_keys_are_rejected_before_alias_expansion(tmp_path):
    (tmp_path / "openapi.yaml").write_text(
        'openapi: "3.0.3"\nx-base: &base {title: test, version: "1"}\n'
        'info: {<<: *base}\npaths: {}\n', encoding="utf-8")

    result = check_openapi_compatibility(tmp_path, "openapi.yaml", "openapi.yaml")

    assert result["status"] == "UNSUPPORTED"
    assert "YAML merge keys" in result["detail"]


def test_missing_and_mismatched_runtime_are_unavailable(tmp_path, monkeypatch):
    _pair(tmp_path, _spec({"type": "string"}), _spec({"type": "string"}))
    monkeypatch.delenv("VKIT_OASDIFF_BIN", raising=False)
    monkeypatch.delenv("VKIT_OASDIFF_SHA256", raising=False)

    missing = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")
    fake = tmp_path / "oasdiff.exe"
    fake.write_bytes(b"not oasdiff")
    monkeypatch.setenv("VKIT_OASDIFF_BIN", str(fake))
    monkeypatch.setenv("VKIT_OASDIFF_SHA256", "0" * 64)
    mismatched = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert missing["status"] == mismatched["status"] == "UNAVAILABLE"


def test_malformed_backend_output_is_unknown(tmp_path, configured_oasdiff, monkeypatch):
    _pair(tmp_path, _spec({"type": "string"}), _spec({"type": "string"}))
    original = openapi._run

    def malformed(binary, args, cwd, env, deadline):
        if args[0] == "breaking":
            return subprocess.CompletedProcess(args, 0, b"{bad", b"")
        return original(binary, args, cwd, env, deadline)

    monkeypatch.setattr(openapi, "_run", malformed)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNKNOWN"
    assert "malformed" in result["detail"]


def test_backend_timeout_is_unknown(tmp_path, configured_oasdiff, monkeypatch):
    _pair(tmp_path, _spec({"type": "string"}), _spec({"type": "string"}))

    def timeout(binary, args, cwd, env, deadline):
        raise subprocess.TimeoutExpired(args, deadline)

    monkeypatch.setattr(openapi, "_run", timeout)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNKNOWN"
    assert "time budget" in result["detail"]


def test_bad_refs_and_resource_limits_are_unsupported(tmp_path):
    spec = _spec({"type": "string"})
    spec["components"] = {"schemas": {"Unused": {"$ref": "../../outside.yaml"}}}
    _write(tmp_path, "old/openapi.json", spec)
    _write(tmp_path, "new/openapi.json", _spec({"type": "string"}))
    escaping = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    large = tmp_path / "large.json"
    large.write_bytes(b" " * (openapi.MAX_OPENAPI_FILE_BYTES + 1))
    oversized = check_openapi_compatibility(tmp_path, "large.json", "large.json")

    assert escaping["status"] == "UNSUPPORTED"
    assert oversized["status"] == "UNSUPPORTED"


def test_percent_encoded_backslash_cannot_escape_on_windows(tmp_path):
    old = _spec({"type": "string"})
    old["components"] = {"schemas": {"Unused": {
        "$ref": "%2e%2e%5c%2e%2e%5coutside.yaml#/Item"}}}
    _write(tmp_path, "old/openapi.json", old)
    _write(tmp_path, "new/openapi.json", _spec({"type": "string"}))

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"


def test_symlinked_input_is_unsupported(tmp_path, monkeypatch):
    source = _write(tmp_path, "openapi.json", _spec({"type": "string"}))
    linked = tmp_path / "linked.json"
    linked.write_bytes(source.read_bytes())
    original_is_symlink = Path.is_symlink

    def is_symlink(path: Path) -> bool:
        return path == linked or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)

    result = check_openapi_compatibility(tmp_path, "linked.json", "openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert "symlinks" in result["detail"]


def test_invalid_openapi_document_never_returns_compatible(tmp_path, configured_oasdiff):
    old = _spec({"type": "string"})
    new = _spec({"type": "bogus"})
    _pair(tmp_path, old, new)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert any(item["rule_id"] == "schema-type-unsupported" for item in result["diagnostics"])


@pytest.mark.parametrize("version", ["2.0.0", "3.2.0", "4.0.0", "nonsense", ["3.0.3"]])
def test_unsupported_document_versions_are_rejected(tmp_path, version):
    spec = _spec({"type": "string"})
    spec["openapi"] = version
    _pair(tmp_path, spec, spec)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert "supported OpenAPI" in result["detail"]


@pytest.mark.parametrize("keyword", ["$schema", "jsonSchemaDialect"])
def test_custom_schema_dialect_is_rejected(tmp_path, keyword):
    spec = _spec({"type": "string", keyword: "https://example.invalid/custom-dialect"})
    _pair(tmp_path, spec, spec)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert "custom JSON Schema dialects" in result["detail"]


def test_ignored_reusable_path_items_are_rejected(tmp_path):
    spec = _spec({"type": "string"})
    spec["components"] = {"pathItems": {"Unused": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    _pair(tmp_path, spec, spec)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert "components.pathItems" in result["detail"]


def test_validation_warning_cannot_become_compatible(tmp_path, configured_oasdiff):
    spec = _spec({"type": "string", "const": "fixed"})
    _pair(tmp_path, spec, spec)

    result = check_openapi_compatibility(tmp_path, "old/openapi.json", "new/openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert any(item["severity"] == "warning" for item in result["diagnostics"])

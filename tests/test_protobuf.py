from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from vkit.operations import protobuf
from vkit.operations.protobuf import BUF_VERSION, check_proto_compatibility


@pytest.fixture
def configured_buf(monkeypatch: pytest.MonkeyPatch) -> Path:
    raw_path = os.environ.get("VKIT_BUF_BIN")
    expected = os.environ.get("VKIT_BUF_SHA256")
    if not raw_path or not expected:
        if raw_path or expected:
            pytest.fail("the configured Buf helper requires both its path and SHA-256")
        pytest.skip("a pinned Buf 1.73.0 binary is not configured")
    binary = Path(raw_path).resolve()
    if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != expected.lower():
        pytest.fail("the configured Buf helper does not match its SHA-256")
    monkeypatch.setenv("VKIT_BUF_BIN", str(binary))
    monkeypatch.setenv("VKIT_BUF_SHA256", expected.lower())
    return binary


def _tree(root: Path, name: str, schema: str) -> Path:
    path = root / name
    path.mkdir()
    (path / "service.proto").write_text(schema, encoding="utf-8")
    return path


_BASE = '''syntax = "proto3";
package demo.v1;
message Item {
  int32 id = 1;
  string name = 2;
}
'''


def test_addition_is_compatible_in_every_supported_category(tmp_path, configured_buf):
    old = _tree(tmp_path, "old", _BASE)
    new = _tree(tmp_path, "new", _BASE.replace("  string name = 2;", "  string name = 2;\n  bool active = 3;"))

    for category in ("FILE", "PACKAGE", "WIRE_JSON", "WIRE"):
        result = check_proto_compatibility(tmp_path, "old", "new", category)
        assert result["status"] == "COMPATIBLE"
        assert result["category"] == category
        assert result["old_proto_file_count"] == result["new_proto_file_count"] == 1
        assert result["old_proto_sha256"] != result["new_proto_sha256"]
        assert result["backend"] == f"buf/{BUF_VERSION}"
        assert result["backend_sha256"] == os.environ["VKIT_BUF_SHA256"]
        assert result["diagnostics"] == []
    assert old.exists() and new.exists()


def test_field_type_change_is_breaking_with_machine_readable_diagnostic(tmp_path, configured_buf):
    _tree(tmp_path, "old", _BASE)
    _tree(tmp_path, "new", _BASE.replace("int32 id", "string id"))

    result = check_proto_compatibility(tmp_path, "old", "new", "WIRE_JSON")

    assert result["status"] == "BREAKING"
    assert result["diagnostics"] == [{
        "rule_id": "FIELD_WIRE_JSON_COMPATIBLE_TYPE",
        "file": "service.proto",
        "line": 4,
        "message": ('Field "1" with name "id" on message "Item" changed type from "int32" to '
                    '"string". See https://developers.google.com/protocol-buffers/docs/proto3#updating '
                    'for wire compatibility rules and https://developers.google.com/protocol-buffers/docs/'
                    'proto3#json for JSON compatibility rules.'),
    }]


@pytest.mark.parametrize(("category", "expected"), [
    ("FILE", "BREAKING"), ("PACKAGE", "BREAKING"),
    ("WIRE_JSON", "COMPATIBLE"), ("WIRE", "COMPATIBLE"),
])
def test_field_removal_depends_on_selected_buf_category(tmp_path, configured_buf, category, expected):
    _tree(tmp_path, "old", _BASE)
    _tree(tmp_path, "new", _BASE.replace("  string name = 2;\n", '  reserved 2;\n  reserved "name";\n'))

    result = check_proto_compatibility(tmp_path, "old", "new", category)

    assert result["status"] == expected
    if expected == "BREAKING":
        assert result["diagnostics"][0]["rule_id"] == "FIELD_NO_DELETE"
        assert result["diagnostics"][0]["file"] == "service.proto"
        assert result["diagnostics"][0]["line"] == 3


def test_project_buf_config_and_ignore_files_cannot_change_the_fixed_ruleset(tmp_path, configured_buf):
    _tree(tmp_path, "old", _BASE)
    _tree(tmp_path, "new", _BASE.replace("int32 id", "string id"))
    (tmp_path / "buf.yaml").write_text(
        "version: v2\nbreaking:\n  use: [WIRE]\n  ignore: [service.proto]\nplugins:\n  - plugin: evil\n",
        encoding="utf-8",
    )
    (tmp_path / ".bufignore").write_text("service.proto\n", encoding="utf-8")
    (tmp_path / "old" / ".bufignore").write_text("service.proto\n", encoding="utf-8")
    (tmp_path / "new" / ".bufignore").write_text("service.proto\n", encoding="utf-8")

    result = check_proto_compatibility(tmp_path, "old", "new", "WIRE_JSON")

    assert result["status"] == "BREAKING"
    assert result["diagnostics"][0]["rule_id"] == "FIELD_WIRE_JSON_COMPATIBLE_TYPE"


@pytest.mark.parametrize("malformed_tree", ["old", "new"])
def test_malformed_source_is_unsupported_with_source_digests(tmp_path, configured_buf, malformed_tree):
    for name in ("old", "new"):
        schema = "syntax = \"proto3\";\nmessage {\n" if name == malformed_tree else _BASE
        _tree(tmp_path, name, schema)

    result = check_proto_compatibility(tmp_path, "old", "new")

    assert result["status"] == "UNSUPPORTED"
    assert len(result["old_proto_sha256"]) == len(result["new_proto_sha256"]) == 64
    assert result["diagnostics"] == []


def test_builtin_google_well_known_type_compiles_without_a_project_dependency(tmp_path, configured_buf):
    schema = '''syntax = "proto3";
package demo.v1;
import "google/protobuf/timestamp.proto";
message Event { google.protobuf.Timestamp created_at = 1; }
'''
    _tree(tmp_path, "old", schema)
    _tree(tmp_path, "new", schema)

    result = check_proto_compatibility(tmp_path, "old", "new")

    assert result["status"] == "COMPATIBLE"


def test_missing_external_import_is_unsupported(tmp_path, configured_buf):
    schema = _BASE.replace('package demo.v1;\n', 'package demo.v1;\nimport "buf.build/acme/types/v1/type.proto";\n')
    _tree(tmp_path, "old", schema)
    _tree(tmp_path, "new", schema)

    result = check_proto_compatibility(tmp_path, "old", "new")

    assert result["status"] == "UNSUPPORTED"


def test_invalid_category_and_path_escape_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="category"):
        check_proto_compatibility(tmp_path, "old", "new", "custom")
    with pytest.raises(ValueError, match="repository-relative"):
        check_proto_compatibility(tmp_path, "../outside", "new")


def test_symlinked_source_path_is_rejected(tmp_path, monkeypatch):
    _tree(tmp_path, "old", _BASE)
    linked = tmp_path / "linked"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == linked or original(path))

    with pytest.raises(ValueError, match="symlinks"):
        check_proto_compatibility(tmp_path, "old", "linked")


def test_unavailable_binary_and_process_timeout_have_distinct_statuses(tmp_path, monkeypatch):
    _tree(tmp_path, "old", _BASE)
    _tree(tmp_path, "new", _BASE)
    monkeypatch.delenv("VKIT_BUF_BIN", raising=False)
    monkeypatch.delenv("VKIT_BUF_SHA256", raising=False)

    absent = check_proto_compatibility(tmp_path, "old", "new")

    fake = tmp_path / "buf.exe"
    fake.write_bytes(b"not Buf")
    monkeypatch.setenv("VKIT_BUF_BIN", str(fake))
    monkeypatch.setenv("VKIT_BUF_SHA256", "0" * 64)
    mismatched = check_proto_compatibility(tmp_path, "old", "new")
    monkeypatch.setenv("VKIT_BUF_SHA256", hashlib.sha256(fake.read_bytes()).hexdigest())
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(protobuf.subprocess, "run", timeout)

    timed_out = check_proto_compatibility(tmp_path, "old", "new")

    assert absent["status"] == mismatched["status"] == "UNAVAILABLE"
    assert timed_out["status"] == "UNKNOWN"

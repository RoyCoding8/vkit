from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from vkit.operations import openapi, parser_consistency
from vkit.operations.openapi import OASDIFF_VERSION


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


def _write_text(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _entry(root: Path, component: str = "./components.yaml#/components/schemas/Probe") -> None:
    _write_text(root, "openapi.yaml", f'''openapi: "3.0.3"
info: {{title: Probe, version: "1"}}
paths: {{}}
components:
  schemas:
    Probe:
      $ref: {component}
''')


def _scalar_closure(root: Path, value: str) -> None:
    _write_text(root, "components.yaml", f'''components:
  schemas:
    Probe:
      type: object
      x-vkit-probe: {value}
''')
    _entry(root)


def test_native_flatten_compares_every_captured_local_reference_file(
        tmp_path, configured_oasdiff):
    _scalar_closure(tmp_path, "1e3")

    result = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")

    assert result["status"] == "DISAGREEMENT"
    assert result["file"] == "components.yaml"
    assert result["file_count"] == 2
    assert result["fresh_process_replay_count"] == 1
    difference = result["differences"][0]
    assert difference["pointer"] == "/components/schemas/Probe/x-vkit-probe"
    assert difference["original"] == {"type": "integer", "value": "1000"}
    assert difference["local_json_control"]["type"] == "string"
    assert difference["local_json_control"]["preview"] == "1e3"
    for field in ("backend_sha256", "file_source_sha256", "local_json_sha256",
                  "original_output_sha256", "local_json_output_sha256"):
        assert len(result[field]) == 64


def test_yaml_on_scalar_preserves_backend_parser_difference(tmp_path, configured_oasdiff):
    _scalar_closure(tmp_path, "on")

    result = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")

    assert result["status"] == "DISAGREEMENT"
    assert result["file"] == "components.yaml"
    assert result["differences"][0]["pointer"] == "/components/schemas/Probe/x-vkit-probe"
    assert result["differences"][0]["local_json_control"]["type"] == "boolean"


def test_non_json_yaml_date_is_explicitly_unsupported(tmp_path):
    _scalar_closure(tmp_path, "2026-10-12")

    result = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")

    assert result["status"] == "UNSUPPORTED"
    assert result["unsupported_file"] == "components.yaml"
    assert result["unsupported_pointer"] == "/components/schemas/Probe/x-vkit-probe"
    assert result["unsupported_type"] == "date"
    assert "non-JSON date" in result["detail"]


def test_local_refs_and_yaml_aliases_are_captured_and_flattened(
        tmp_path, configured_oasdiff):
    _write_text(tmp_path, "components.yaml", '''components:
  schemas:
    Base: &probe
      type: object
      properties:
        label: {type: string}
    Probe: *probe
''')
    _entry(tmp_path, "./components.yaml#/components/schemas/Probe")

    result = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")

    assert result["status"] == "CONSISTENT_IN_FLATTENED_MODEL"
    assert result["files_checked"] == 2
    assert [item["path"] for item in result["files"]] == ["components.yaml", "openapi.yaml"]
    for item in result["files"]:
        assert item["original_backend_status"] == item["local_json_backend_status"] == "PARSED"
        assert len(item["original_output_sha256"]) == 64
        assert len(item["local_json_output_sha256"]) == 64


def test_external_reference_is_rejected_by_capture(tmp_path):
    _write_text(tmp_path, "openapi.yaml", '''openapi: "3.0.3"
info: {title: Probe, version: "1"}
paths: {}
components:
  schemas:
    Probe:
      $ref: https://example.invalid/schema.yaml#/Probe
''')

    result = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")

    assert result["status"] == "UNSUPPORTED"
    assert "remote $refs" in result["detail"]


def test_timeout_and_backend_output_limits_return_unknown(
        tmp_path, configured_oasdiff, monkeypatch):
    _scalar_closure(tmp_path, "1e3")
    original_run = openapi._run

    def timeout(binary, args, cwd, env, deadline):
        if args[0] == "flatten":
            raise subprocess.TimeoutExpired(args, 1)
        return original_run(binary, args, cwd, env, deadline)

    monkeypatch.setattr(openapi, "_run", timeout)
    timed_out = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")
    assert timed_out["status"] == "UNKNOWN"
    assert "time budget" in timed_out["detail"]

    def oversized(binary, args, cwd, env, deadline):
        if args[0] == "flatten":
            raise openapi._BackendOutputExceeded
        return original_run(binary, args, cwd, env, deadline)

    monkeypatch.setattr(openapi, "_run", oversized)
    output_limited = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")
    assert output_limited["status"] == "UNKNOWN"
    assert "output exceeded" in output_limited["detail"]


def test_nondeterministic_fresh_process_replay_is_unknown(
        tmp_path, configured_oasdiff, monkeypatch):
    _scalar_closure(tmp_path, "1e3")
    flatten = parser_consistency._flatten
    calls = 0

    def inconsistent_replay(*args, **kwargs):
        nonlocal calls
        observation = flatten(*args, **kwargs)
        calls += 1
        if calls == 3:
            observation = {**observation, "value": {"changed": True}}
        return observation

    monkeypatch.setattr(parser_consistency, "_flatten", inconsistent_replay)

    result = parser_consistency.inspect_openapi_parser_consistency(tmp_path, "openapi.yaml")

    assert result["status"] == "UNKNOWN"
    assert "fresh-process replay" in result["detail"]


@pytest.mark.parametrize("timeout_seconds", [0, 61, True, 1.0])
def test_timeout_must_be_a_bounded_integer(tmp_path, timeout_seconds):
    with pytest.raises(ValueError, match="timeout_seconds"):
        parser_consistency.inspect_openapi_parser_consistency(
            tmp_path, "openapi.yaml", timeout_seconds=timeout_seconds)

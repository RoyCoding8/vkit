from __future__ import annotations

import json
import hashlib
import math
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import McpClient, NO_WINDOW
from test_paired import _repository
from vkit.change_methods import parse_kani_toolchain
from vkit.kani_toolchain import KaniToolchain
from vkit.paired_profiles import FilePin
from vkit.query import validate_wait_seconds


def _request_file(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "change-request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return path


def _cli(
    root: Path,
    request_path: Path,
    trusted_target_oid: str,
    *options: str,
    as_json: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "vkit.cli",
            "change",
            "certify",
            "--project",
            str(root),
            "--request",
            str(request_path),
            "--trusted-target",
            trusted_target_oid,
            *options,
            *(["--json"] if as_json else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        **NO_WINDOW,
    )


def _python_repository(tmp_path: Path, delta: int = 0):
    return _repository(
        tmp_path,
        language="python",
        candidate_delta=delta,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )


def test_public_toolchain_parser_rejects_open_or_derived_values(tmp_path: Path) -> None:
    pin = KaniToolchain(
        kani_root=tmp_path / "kani",
        rustup_home=tmp_path / "rustup",
        cargo_home=tmp_path / "cargo",
        kani_manifest_sha256="a" * 64,
        rust_manifest_sha256="b" * 64,
        rustup_sha256="c" * 64,
        host_libraries=(FilePin(str(tmp_path / "libc.so"), "d" * 64),),
    )
    assert parse_kani_toolchain(pin.to_json()) == pin

    altered = pin.to_json()
    altered["environment"]["PATH"] += ";candidate"
    with pytest.raises(ValueError):
        parse_kani_toolchain(altered)


def test_cli_submits_healthy_finite_shadow_run(tmp_path: Path) -> None:
    root, target_oid, request = _python_repository(tmp_path)
    request_path = _request_file(tmp_path, request)

    result = _cli(root, request_path, target_oid, "--wait", "60")

    payload = json.loads(result.stdout)
    shadow = payload["summary"]["change_certification"]
    assert (result.returncode, payload["summary"]["outcome"]["result"]) == (0, "PASS")
    assert shadow["status"] == "observed_conformance"
    assert shadow["requirement"]["source"] == "requirements.md#conversion"
    assert shadow["intent_sha256"] == request["intent_sha256"]
    assert [(item["method"], item["status"]) for item in shadow["methods"]] == [
        ("finite_paired", "observed_conformance")
    ]
    assert set(shadow["methods"][0]["report"]) >= {"path", "sha256"}
    artifact = shadow["methods"][0]["report"]
    assert artifact["path"] == "change-output/01-finite_paired/change-analysis.json"
    retained = root / ".git/vkit/runs" / payload["summary"]["run_id"] / artifact["path"]
    assert hashlib.sha256(retained.read_bytes()).hexdigest() == artifact["sha256"]
    assert shadow["source_proof"] is False
    assert shadow["approval_authority"] is False
    assert "method_plan" not in shadow
    assert "input_tokens" not in json.dumps(shadow)


def test_cli_human_output_shows_shadow_status_and_requirement(tmp_path: Path) -> None:
    root, target, request = _python_repository(tmp_path)
    result = _cli(root, _request_file(tmp_path, request), target, "--wait", "60", as_json=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "shadow analysis: observed_conformance (empirical, no approval authority)" in result.stdout
    assert "requirement: requirements.md#conversion" in result.stdout


def test_cli_reports_unguarded_behavior_as_violation(tmp_path: Path) -> None:
    root, target_oid, request = _python_repository(tmp_path, delta=1)
    request_path = _request_file(tmp_path, request)

    result = _cli(root, request_path, target_oid, "--wait", "60")

    payload = json.loads(result.stdout)
    shadow = payload["summary"]["change_certification"]
    assert (result.returncode, payload["summary"]["outcome"]["result"]) == (1, "FAIL")
    assert shadow["status"] == "observed_violation"
    assert shadow["methods"][0]["status"] == "observed_violation"
    assert shadow["source_proof"] is False
    assert payload["summary"]["next_action"] == "inspect_artifacts"


def test_cli_rejects_bad_target_and_explicit_kani_does_not_fall_back(
    tmp_path: Path,
) -> None:
    root, target_oid, request = _python_repository(tmp_path)
    request_path = _request_file(tmp_path, request)

    invalid = _cli(root, request_path, "not-a-commit", "--wait", "60")
    assert invalid.returncode == 2
    assert "error" in json.loads(invalid.stdout)

    unsupported = _cli(
        root,
        request_path,
        target_oid,
        "--method",
        "rust_kani",
        "--wait",
        "60",
    )
    payload = json.loads(unsupported.stdout)
    shadow = payload["summary"]["change_certification"]
    assert (unsupported.returncode, payload["summary"]["outcome"]["result"]) == (
        3,
        "BLOCKED",
    )
    assert shadow["status"] == "unsupported"
    assert shadow["methods"] == []
    assert "method_plan" not in shadow


def test_mcp_discovers_and_runs_finite_shadow_job(tmp_path: Path) -> None:
    root, target_oid, request = _python_repository(tmp_path)
    client = McpClient(root, exposure="named")
    try:
        tools = client.request("tools/list", {})["tools"]
        definition = next(tool for tool in tools if tool["name"] == "change_certify")
        properties = definition["inputSchema"]["properties"]
        assert set(definition["inputSchema"]["required"]) == {
            "request",
            "trusted_target_oid",
        }
        assert properties["request"]["additionalProperties"] is False
        assert properties["method"]["enum"] == ["auto", "finite_paired", "rust_kani"]
        assert properties["kani_toolchain"]["additionalProperties"] is False

        body, error = client.call(
            "change_certify",
            request=request,
            trusted_target_oid=target_oid,
            method="finite_paired",
            wait_seconds=60,
        )
        assert not error
        run_id = body["summary"]["run_id"]
        assert (
            body["summary"]["change_certification"]["status"] == "observed_conformance"
        )

        waited, error = client.call("run_wait", run_id=run_id, wait_seconds=60)
        assert not error
        fetched, error = client.call("run_get", run_id=run_id)
        assert not error
        assert waited["summary"]["run_id"] == fetched["summary"]["run_id"] == run_id
        assert fetched["summary"]["change_certification"]["source_proof"] is False

        body, error = client.call(
            "change_certify",
            request=request,
            trusted_target_oid="not-a-commit",
        )
        assert error
        assert "does not match" in body["error"]

        _, error = client.call(
            "change_certify",
            request=request,
            trusted_target_oid=target_oid,
            command="candidate-controlled",
        )
        assert error
    finally:
        client.close()


@pytest.mark.parametrize("value", [True, -0.1, 60.1, math.nan, math.inf, -math.inf])
def test_wait_seconds_rejects_nonfinite_booleans_and_out_of_range(
    value: object,
) -> None:
    with pytest.raises(ValueError):
        validate_wait_seconds(value)


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (10, 10.0), (60.0, 60.0)])
def test_wait_seconds_accepts_closed_range(value: object, expected: float) -> None:
    assert validate_wait_seconds(value) == expected

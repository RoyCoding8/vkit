from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from test_rust_relational import _fixture
from vkit import kani_execution as execution
from vkit.evidence import sha256
from vkit.kani_toolchain import KaniToolchain, KaniToolchainError
from vkit.paths import open_project
from vkit.proc import Exit
from vkit.rust_relational import prepare_rust_relational


@pytest.fixture(scope="module")
def program(tmp_path_factory):
    root, target, request = _fixture(tmp_path_factory.mktemp("kani-program"))
    return prepare_rust_relational(open_project(root), request, trusted_target_oid=target)


@pytest.fixture
def host(tmp_path, monkeypatch, program):
    pin = KaniToolchain(tmp_path / "kani", tmp_path / "rustup", tmp_path / "cargo",
                        "a" * 64, "b" * 64, "c" * 64)
    monkeypatch.setattr(execution.platform, "system", lambda: "Linux")
    monkeypatch.setattr(execution.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(execution, "validate_kani_toolchain", lambda *a, **k: (("kani.jsonl", b"{}\n"),))
    profiles = {profile.source_sha256: profile for profile in (program.base_profile, program.candidate_profile)}
    monkeypatch.setattr(execution, "admit_rust_source", lambda raw, binding: profiles[hashlib.sha256(raw).hexdigest()])
    return pin


def _process(monkeypatch, *, result=Exit(0), raw=b"{}", mutate=lambda cwd: None):
    calls = []
    original = execution.proc.run

    def run(argv, **kwargs):
        if len(argv) < 2 or argv[1] != "harness.rs":
            return original(argv, **kwargs)
        calls.append((argv, kwargs))
        kwargs["stdout_path"].write_bytes(b"constructed process output\n")
        kwargs["stderr_path"].write_bytes(b"")
        if raw is not None:
            (kwargs["cwd"] / "kani-results.json").write_bytes(raw)
        mutate(kwargs["cwd"])
        return result

    monkeypatch.setattr(execution.proc, "run", run)
    return calls


def _report(result):
    assert sha256(result.report_path) == result.report_sha256
    report = json.loads(result.report_path.read_bytes())
    assert report["outcome"] == {"kind": result.kind, "reason": result.reason}
    assert report["source_proof"] is False
    assert report["approval_authority"] is False
    for ref in report["artifacts"]:
        assert sha256(result.report_path.parent / ref["path"]) == ref["sha256"]
    return report


def test_closed_invocation_retains_captures_and_process_boundary(program, host, tmp_path, monkeypatch):
    calls = _process(monkeypatch)
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "run")
    assert result.kind == "unknown"
    report = _report(result)
    assert report["executed"] is True
    assert report["toolchain_validated_before"] is True
    assert report["toolchain_validated_after"] is True
    assert report["staged_executable_modes_checked"] is False
    argv, options = calls[0]
    assert argv == [str(host.driver), "harness.rs", "--harness", "vkit_relational",
                    "-Z", "unstable-options", "--export-json", "kani-results.json"]
    assert options["env"] == host.environment()
    assert options["max_output_bytes"] == 1048576
    assert options["timeout_seconds"] == 90
    assert (options["cwd"] / "harness.rs").read_bytes() == program.harness
    assert (options["cwd"] / "candidate/candidate.rs").read_bytes() == next(
        file.content for file in program.candidate.files if file.path == "candidate.rs")
    assert {"kani_execution.py", "kani_toolchain.py", "kani_result.py"} <= set(
        json.loads((result.report_path.parent / "implementation.json").read_bytes()))


@pytest.mark.parametrize(("name", "exit_code", "kind", "reason"), [
    ("preservation", 0, "verified", "all_qualified_properties_succeeded"),
    ("regression", 1, "proof_rejected", "named_relational_policy_assertion_failed"),
    ("outside-model", 1, "unknown", "observation_model_failed"),
])
def test_retained_exports_flow_through_constructed_process(
    program, host, tmp_path, monkeypatch, name, exit_code, kind, reason,
):
    raw = (Path(__file__).parent / "fixtures/kani-results" / name / "kani-results.json").read_bytes()
    _process(monkeypatch, result=Exit(exit_code), raw=raw)
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "run")
    assert (result.kind, result.reason) == (kind, reason)
    _report(result)


@pytest.mark.parametrize(("exit_result", "kind", "reason"), [
    (Exit(0, timed_out=True), "unknown", "process_timeout"),
    (Exit(0, cancelled=True), "unknown", "cancelled"),
    (Exit(0, output_limited=True), "unknown", "process_output_limit"),
    (Exit(None, launch_error="constructed launch failure"), "infrastructure_failure", "process_launch_failed"),
    (Exit(23), "unknown", "unexpected_process_exit"),
])
def test_process_failure_precedes_partial_export(program, host, tmp_path, monkeypatch, exit_result, kind, reason):
    _process(monkeypatch, result=exit_result)
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "run")
    assert (result.kind, result.reason) == (kind, reason)
    assert _report(result)["toolchain_validated_after"] is False


@pytest.mark.parametrize("mutation", ["source", "harness", "retained", "extra_source"])
def test_process_cannot_change_staged_or_retained_inputs(program, host, tmp_path, monkeypatch, mutation):
    def mutate(cwd):
        path = {"source": cwd / "candidate/candidate.rs", "harness": cwd / "harness.rs",
                "retained": cwd.parent / "program.json", "extra_source": cwd / "base/new.rs"}[mutation]
        path.write_bytes(b"changed")

    _process(monkeypatch, mutate=mutate)
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "run")
    assert result.kind == "infrastructure_failure"
    assert result.reason in {"execution_artifact_changed", "retained_artifact_changed"}


def test_post_execution_toolchain_failure_cannot_verify(program, host, tmp_path, monkeypatch):
    _process(monkeypatch)
    calls = []

    def validate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KaniToolchainError("pin_mismatch", "constructed changed installation")
        return (("kani.jsonl", b"{}\n"),)

    monkeypatch.setattr(execution, "validate_kani_toolchain", validate)
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "run")
    assert (result.kind, result.reason) == ("infrastructure_failure", "toolchain_pin_mismatch")
    _report(result)


@pytest.mark.parametrize("raw", [None, b"x" * (2 * 1024 * 1024 + 1)], ids=["missing", "oversized"])
def test_missing_or_oversized_export_cannot_verify(program, host, tmp_path, monkeypatch, raw):
    _process(monkeypatch, raw=raw)
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "run")
    assert (result.kind, result.reason) == (
        ("infrastructure_failure", "execution_artifact_unavailable") if raw is None
        else ("unknown", "result_size_limit"))
    _report(result)


def test_source_admission_identity_change_blocks_launch(program, host, tmp_path, monkeypatch):
    calls = _process(monkeypatch)
    changed = replace(program, base_profile=program.candidate_profile)
    result = execution.run_kani_relational(changed, toolchain=host, output_dir=tmp_path / "run")
    assert (result.kind, result.reason) == ("infrastructure_failure", "source_admission_identity_changed")
    assert calls == []


def test_unsupported_platform_and_cancellation_do_not_launch(program, host, tmp_path, monkeypatch):
    calls = _process(monkeypatch)
    monkeypatch.setattr(execution.platform, "system", lambda: "Windows")
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "unsupported")
    assert (result.kind, result.reason) == ("unknown", "unsupported_platform")
    monkeypatch.setattr(execution.platform, "system", lambda: "Linux")
    result = execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "cancel",
                                          should_cancel=lambda: True)
    assert (result.kind, result.reason) == ("unknown", "cancelled")
    assert calls == []


@pytest.mark.parametrize("timeout", [True, 0, -1, 301, float("inf"), float("nan")])
def test_invalid_deadline_rejected_before_creating_directory(program, host, tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        execution.run_kani_relational(program, toolchain=host, output_dir=tmp_path / "run", timeout_seconds=timeout)
    assert not (tmp_path / "run").exists()


def test_existing_output_directory_is_never_reused(program, host, tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    marker = output / "report.json"
    marker.write_bytes(b"previous run")
    with pytest.raises(FileExistsError):
        execution.run_kani_relational(program, toolchain=host, output_dir=output)
    assert marker.read_bytes() == b"previous run"

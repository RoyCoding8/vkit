from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import NO_WINDOW, ROOT


MANIFEST = ROOT / "examples/change-certification/manifest.json"
RUNNER = ROOT / "scripts/check_change_corpus.py"


def run_corpus(output_dir: Path, manifest: Path | None = None, env: dict[str, str] | None = None):
    command = [sys.executable, str(RUNNER), "--output-dir", str(output_dir)]
    if manifest is not None:
        command += ["--manifest", str(manifest)]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env=env, **NO_WINDOW)


@pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc is required for native fixture execution")
def test_runner_compiles_and_checks_the_frozen_runtime_observations(tmp_path: Path):
    output = tmp_path / "run"
    result = run_corpus(output)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS cases=12 runtime=8 policy_only=4 observations=19 native_tests=8 scope=finite_literal_rows" in result.stdout
    assert result.stdout.count("POLICY-ONLY ") == 4
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert (report["status"], report["runtime_observation_count"], report["native_test_count"]) == ("PASS", 19, 8)
    assert len(report["runtime_cases"]) == 8
    assert all(case["kind"] == "runtime" for case in report["runtime_cases"])
    assert [case["id"] for case in report["policy_only_cases"]] == ["SC-04", "PA-04", "RS-03", "RS-04"]
    assert all(case["kind"] == "policy_only" for case in report["policy_only_cases"])
    assert report["finite_scope"].startswith("Finite literal Rust fixtures.")
    provenance = report["provenance"]
    assert provenance["manifest_sha256"] == hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    assert provenance["rustc_version"].startswith("rustc ")
    assert set(provenance["fixture_source_sha256"]) == set(provenance["native_output_logs"])
    for source, digest in provenance["fixture_source_sha256"].items():
        fixture = ROOT / "examples/change-certification/fixtures" / source
        assert digest == hashlib.sha256(fixture.read_bytes()).hexdigest()
        log = output / provenance["native_output_logs"][source]
        assert "test result: ok." in log.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc is required for native fixture execution")
def test_runner_rejects_an_invalidated_frozen_observation(tmp_path: Path):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    case = next(case for case in manifest["cases"] if case["id"] == "SC-03")
    row = next(row for row in case["observations"] if row["input"] == "x=10")
    row["after"] = "10"
    altered_manifest = tmp_path / "manifest.json"
    altered_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    output = tmp_path / "run"
    result = run_corpus(output, altered_manifest)
    assert result.returncode == 1
    assert "observations differ for scalar.rs" in result.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert "missing=" in report["error"]
    assert report["provenance"]["manifest_sha256"] == hashlib.sha256(altered_manifest.read_bytes()).hexdigest()


def test_runner_reports_missing_rustc_as_blocked(tmp_path: Path):
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    environment = os.environ.copy()
    environment["PATH"] = str(empty_path)
    output = tmp_path / "run"
    result = run_corpus(output, env=environment)
    assert result.returncode == 2
    assert result.stdout == "BLOCKED: rustc is unavailable; native fixture evidence was not run.\n"
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "BLOCKED"
    assert report["reason"] == "rustc is unavailable"

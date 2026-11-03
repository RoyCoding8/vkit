from __future__ import annotations

import hashlib
import json
from threading import Event
from types import SimpleNamespace

import pytest

from test_change_methods import _captured_pair, _toolchain
from vkit import change_service
from vkit.change_methods import ChangeMethodPlan, FiniteMethod, KaniMethod
from vkit.kani_execution import KaniExecution
from vkit.paired import _json_bytes


def test_real_finite_refutation_stops_later_method_and_survives_cancellation(tmp_path, monkeypatch):
    from test_paired import _repository
    from vkit.intent import _prepare_change_request
    from vkit.paths import open_project

    root, target, request = _repository(
        tmp_path, language="python", candidate_delta=2,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    bound, base, candidate = _prepare_change_request(open_project(root), request, trusted_target_oid=target)
    cancelled = Event()
    native = change_service._run_prepared_change_analysis

    def finite(*args, **kwargs):
        result = native(*args, **kwargs)
        cancelled.set()
        return result

    monkeypatch.setattr(change_service, "_run_prepared_change_analysis", finite)
    monkeypatch.setattr(change_service, "_prepare_captured_rust_relational",
                        lambda *args: pytest.fail("later method must not run after refutation"))
    report = change_service.run_change_methods(
        bound, base, candidate, ChangeMethodPlan((FiniteMethod(), KaniMethod(_toolchain(tmp_path)))),
        output_dir=tmp_path / "methods", should_cancel=cancelled.is_set,
    )
    assert report["status"] == "observed_violation"
    assert report["stopped"] == "checked_refutation"
    assert [r["method"] for r in report["methods"]] == ["finite_paired"]
    assert report["source_proof"] is False and report["approval_authority"] is False


@pytest.mark.parametrize("finite_status,kani_status,expected", [
    ("unknown", "verified", "unknown"),
    ("observed_conformance", "verified", "model_verified"),
    ("unknown", "proof_rejected", "proof_rejected"),
    ("observed_conformance", "unknown", "unknown"),
])
def test_constructed_adapter_results_preserve_unknown_and_rejection(
    tmp_path, monkeypatch, finite_status, kani_status, expected,
):
    bound, base, candidate = _captured_pair(tmp_path, "python")

    def finite(*args, output_dir, **kwargs):
        output_dir.mkdir()
        report = {"status": finite_status, "scope": "constructed_adapter_result"}
        (output_dir / "change-analysis.json").write_bytes(_json_bytes(report))
        return SimpleNamespace(report=report, assessment=SimpleNamespace(
            status=finite_status, coverage_complete=finite_status == "observed_conformance",
        ))

    def kani(*args, output_dir, **kwargs):
        output_dir.mkdir()
        path = output_dir / "report.json"
        path.write_bytes(_json_bytes({"status": kani_status, "scope": "constructed_adapter_result"}))
        return KaniExecution(kani_status, "constructed_result", path, hashlib.sha256(path.read_bytes()).hexdigest())

    monkeypatch.setattr(change_service, "_run_prepared_change_analysis", finite)
    monkeypatch.setattr(change_service, "_prepare_captured_rust_relational", lambda *args: object())
    monkeypatch.setattr(change_service, "run_kani_relational", kani)
    directory = tmp_path / "methods"
    report = change_service.run_change_methods(
        bound, base, candidate, ChangeMethodPlan((FiniteMethod(), KaniMethod(_toolchain(tmp_path)))),
        output_dir=directory,
    )
    assert report["status"] == expected
    assert report["source_proof"] is False and report["approval_authority"] is False
    assert report["native_compiler_correspondence"] is False
    assert [r["status"] for r in report["methods"]] == [finite_status, kani_status]
    assert json.loads((directory / "change-certification.json").read_text()) == report
    for result in report["methods"]:
        artifact = result["report"]
        assert hashlib.sha256((directory / artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]

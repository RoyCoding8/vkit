import json

import pytest

from vkit.outcome import Blocked, BlockedReason
from vkit.verifiers.dispatch import outcome_from_reading, read_build
from vkit.verifiers.spec import BuildCheck


@pytest.fixture
def check(tmp_path):
    return BuildCheck(
        id="wheel", cwd=tmp_path, timeout_seconds=10, artifact_name="build.json",
        uv_executable="uv", uv_version="1", uv_sha256="0" * 64,
        python_version="3.11", python_sha256="1" * 64,
        build_constraints="constraints.txt", wheelhouse="wheels", runtime_requirements="runtime.lock",
        imports=("example",), entry_points=(),
    )


@pytest.fixture
def report():
    wheel = {"filename": "example.whl", "bytes": 1, "sha256": "2" * 64}
    return {
        "schema_version": 1, "profile": "offline-wheel-build-install-v1", "status": "PASS",
        "toolchain": {"uv_version": "1", "uv_sha256": "0" * 64,
                      "python_version": "3.11", "python_sha256": "1" * 64},
        "same_wheel_bytes": True,
        "builds": [
            {"index": index, "exit_code": 0, "stdout_bytes": 0, "stdout_sha256": "3" * 64,
             "stderr_bytes": 0, "stderr_sha256": "3" * 64, "wheel": dict(wheel)}
            for index in (1, 2)
        ],
        "assertions": [
            {"id": "builds:wheel-bytes-identical", "result": "PASS", "expected": True, "observed": True},
            {"id": "builds:wheel-filename-identical", "result": "PASS",
             "expected": "example.whl", "observed": "example.whl"},
            {"id": "install-1:imports", "result": "PASS", "expected": ["example"], "observed": ["example"]},
            {"id": "install-2:imports", "result": "PASS", "expected": ["example"], "observed": ["example"]},
        ],
    }


@pytest.mark.parametrize(("path", "value", "detail"), [
    (("toolchain", "uv_version"), "2", "build report toolchain does not match its approved profile"),
    (("assertions", 1, "id"), "builds:wheel-bytes-identical", "build report has duplicate or missing assertion ids"),
    (("assertions", 1, "id"), "unapproved", "build report assertions do not match the approved install smoke profile"),
    (("builds", 0, "exit_code"), 1, "completed build report does not contain two successful wheel builds"),
    (("builds", 1, "index"), 1, "build report does not identify both independent builds"),
    (("same_wheel_bytes",), False, "build report byte-equality assertion disagrees with wheel digests"),
    (("assertions", 0, "observed"), False, "build report byte-equality assertion is inconsistent"),
    (("assertions", 1, "expected"), "other.whl", "build report filename assertion is inconsistent"),
    (("status",), "FAIL", "build report status disagrees with its assertions"),
])
def test_build_report_refuses_inconsistent_evidence(check, report, path, value, detail):
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    reading = read_build(check, json.dumps(report).encode())

    assert outcome_from_reading(reading) == Blocked(BlockedReason.ARTIFACT_MALFORMED, detail)


@pytest.mark.parametrize("failed", [False, True])
def test_build_report_distinguishes_completed_success_and_failure(check, report, failed):
    if failed:
        report["status"] = "FAIL"
        report["assertions"][2]["result"] = "FAIL"
        report["assertions"][2]["observed"] = []

    outcome = outcome_from_reading(read_build(check, json.dumps(report).encode())).to_json()

    assert outcome["result"] == ("FAIL" if failed else "PASS")
    assert [item["result"] for item in outcome["scenarios"]] == (
        ["PASS", "PASS", "FAIL", "PASS"] if failed else ["PASS"] * 4)


def test_build_report_preserves_known_blocked_reason(check, report):
    report["status"] = "BLOCKED"
    report["problem"] = {"reason": "timeout", "detail": "build deadline"}

    assert outcome_from_reading(read_build(check, json.dumps(report).encode())) == Blocked(
        BlockedReason.TIMEOUT, "build deadline")

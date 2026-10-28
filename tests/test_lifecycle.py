from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_lifecycle", Path(__file__).resolve().parents[1] / "scripts/check_lifecycle.py"
)
assert spec and spec.loader
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)
capture_runtime_observations = lifecycle.capture_runtime_observations


def test_runtime_observations_cover_contention_concurrent_attempts_and_recovery(tmp_path):
    observed = capture_runtime_observations(tmp_path / "runtime")
    attempts = observed["attempts"]

    assert observed["concurrent_same_job"]["shared_job_id"] == observed["job_id"]
    assert len(set(observed["concurrent_same_job"]["run_ids"])) == 2
    raced = observed["cancel_completion_race"]["outcome"]
    assert raced["result"] in {"PASS", "BLOCKED"}
    if raced["result"] == "BLOCKED":
        assert raced["reason"] == "cancelled"
    assert len({attempt["run_id"] for attempt in attempts}) == 3
    assert [attempt["state"] for attempt in attempts] == ["done", "interrupted", "done"]
    assert attempts[0]["job_id"] == observed["job_id"]
    assert attempts[1].get("outcome") is None
    assert attempts[2]["outcome"]["result"] == "PASS"
    assert observed["published_before_worker_loss"] == observed["published_after_worker_loss"]
    assert observed["published_before_worker_loss"]["run_id"] == attempts[0]["run_id"]
    assert observed["final_published"]["run_id"] == attempts[2]["run_id"]

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_lifecycle", Path(__file__).resolve().parents[1] / "scripts/check_lifecycle.py"
)
assert spec and spec.loader
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)
capture_runtime_trace = lifecycle.capture_runtime_trace


def test_runtime_trace_uses_cancel_worker_loss_recovery_and_retry(tmp_path):
    events, identity, attempts = capture_runtime_trace(tmp_path / "runtime")

    assert events == [
        "submit", "start", "cancel", "complete_blocked", "retry", "start",
        "worker_lost", "recover", "retry", "start", "complete_pass",
    ]
    assert identity[0] == attempts[0]["job_id"]
    assert len({attempt["run_id"] for attempt in attempts}) == 3
    assert [attempt["state"] for attempt in attempts] == ["done", "interrupted", "done"]
    assert attempts[0]["outcome"]["reason"] == "cancelled"
    assert attempts[1].get("outcome") is None
    assert attempts[2]["outcome"]["result"] == "PASS"

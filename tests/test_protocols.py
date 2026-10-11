from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from helpers import McpClient, vkit

from vkit.operations import protocols
from vkit.operations.protocols import check_protocol_model


def test_ci_requires_configured_protocol_engine():
    if os.environ.get("VKIT_REQUIRE_PROTOCOL_ENGINE"):
        assert _engine_available(), "CI must configure the real pinned TLC engine"


@pytest.mark.skipif(not bool(os.environ.get("VKIT_TLA2TOOLS_JAR")), reason="pinned TLC engine is not configured")
def test_protocol_operation_cli_and_mcp(make_project):
    model = {"schema_version": 1,
             "variables": {"x": {"domain": [0], "initial": [0], "durability": "durable"}},
             "transitions": [], "invariants": [], "crash_recovery": False, "check_deadlock": False}
    project = make_project({"model.json": json.dumps(model)}, [], accept=False)
    code, body = vkit(project, "compute", "check-protocol-model", "--path", "model.json")
    assert body["status"] == "EXHAUSTIVE", body.get("detail")
    assert (code, body["status"], body["states"]) == (0, "EXHAUSTIVE", 1)
    client = McpClient(project)
    try:
        body, error = client.call("check_protocol_model", path="model.json")
        assert not error and body["status"] == "EXHAUSTIVE" and body["reference_saturation"] == "exact"
    finally:
        client.close()


def _write_model(tmp_path: Path, model: dict) -> tuple[Path, str]:
    (tmp_path / "model.json").write_text(json.dumps(model), encoding="utf-8")
    return tmp_path, "model.json"


def _engine_available() -> bool:
    return bool(os.environ.get("VKIT_TLA2TOOLS_JAR"))


@pytest.mark.skipif(not _engine_available(), reason="pinned TLC engine is not configured")
def test_tlc_checks_simultaneous_updates_in_the_finite_model(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {
            "x": {"domain": [0, 1], "initial": [0], "durability": "durable"},
            "y": {"domain": [0, 1], "initial": [0], "durability": "durable"},
        },
        "transitions": [{
            "name": "copy_prestate",
            "guard": {"eq": [{"var": "x"}, {"int": 0}]},
            "updates": {"x": {"int": 1}, "y": {"var": "x"}},
        }],
        "invariants": [{"name": "y_stays_zero", "expression": {"eq": [{"var": "y"}, {"int": 0}]}}],
        "crash_recovery": False,
        "check_deadlock": False,
    })

    result = check_protocol_model(root, path)

    assert result["status"] == "EXHAUSTIVE"
    assert result["states"] == 2
    assert result["properties"] == ["y_stays_zero", "declared variable domains"]


@pytest.mark.skipif(not _engine_available(), reason="pinned TLC engine is not configured")
def test_tlc_reports_and_replays_domain_escape(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"counter": {"domain": [0, 1], "initial": [1], "durability": "durable"}},
        "transitions": [{
            "name": "overflow",
            "guard": {"bool": True},
            "updates": {"counter": {"add": [{"var": "counter"}, {"int": 1}]}},
        }],
        "invariants": [],
        "crash_recovery": False,
        "check_deadlock": False,
    })

    result = check_protocol_model(root, path)

    assert result["status"] == "COUNTEREXAMPLE"
    assert result["kind"] == "DOMAIN_ESCAPE"
    assert result["trace"] == [
        {"variables": {"counter": 1}},
        {"variables": {"counter": 2}},
    ]


@pytest.mark.skipif(not _engine_available(), reason="pinned TLC engine is not configured")
def test_restart_resets_volatile_and_preserves_durable_state(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {
            "committed": {"domain": [0, 1], "initial": [1], "durability": "durable"},
            "cache": {"domain": [0, 1], "initial": [1], "durability": "volatile", "restart": 0},
        },
        "transitions": [{
            "name": "resume_work",
            "guard": {"not": {"eq": [{"var": "cache"}, {"int": 0}]}},
            "updates": {"cache": {"int": 1}},
        }],
        "invariants": [{
            "name": "restart_resets_cache",
            "expression": {"eq": [{"var": "cache"}, {"int": 1}]},
        }],
        "crash_recovery": True,
        "check_deadlock": False,
    })

    result = check_protocol_model(root, path)

    assert result["status"] == "COUNTEREXAMPLE"
    assert result["kind"] == "INVARIANT_VIOLATION"
    assert result["trace"] == [
        {"variables": {"committed": 1, "cache": 1}, "process_running": True},
        {"variables": {"committed": 1, "cache": 1}, "process_running": False},
        {"variables": {"committed": 1, "cache": 0}, "process_running": True},
    ]


@pytest.mark.skipif(not _engine_available(), reason="pinned TLC engine is not configured")
def test_deadlock_is_a_counterexample_even_when_crash_recovery_is_available(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"done": {"domain": [False, True], "initial": [True], "durability": "durable"}},
        "transitions": [],
        "invariants": [],
        "crash_recovery": True,
        "check_deadlock": True,
    })

    result = check_protocol_model(root, path)

    assert result["status"] == "COUNTEREXAMPLE"
    assert result["kind"] == "DEADLOCK"
    assert result["trace"] == [{
        "variables": {"done": True},
        "process_running": True,
    }]


def test_rejects_engine_language_and_unknown_fields(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"x": {"domain": [0], "initial": [0], "durability": "durable"}},
        "transitions": [{
            "name": "bad",
            "guard": {"tla": "x' = 1"},
            "updates": {"x": {"int": 1}},
        }],
        "invariants": [],
        "crash_recovery": False,
        "check_deadlock": False,
    })

    result = check_protocol_model(root, path)
    assert result["status"] == "UNSUPPORTED"
    assert "unsupported" in result["detail"]


def test_saturation_stops_when_exact_state_budget_is_exceeded(monkeypatch) -> None:
    model = protocols._parse_model({
        "schema_version": 1,
        "variables": {"x": {"domain": [0, 1], "initial": [0], "durability": "durable"}},
        "transitions": [{
            "name": "advance",
            "guard": {"eq": [{"var": "x"}, {"int": 0}]},
            "updates": {"x": {"int": 1}},
        }],
        "invariants": [],
        "crash_recovery": False,
        "check_deadlock": False,
    }, "0" * 64)
    monkeypatch.setattr(protocols, "MAX_REACHABLE_STATES", 1)

    result = protocols._saturate(model, protocols.time.monotonic() + 2)

    assert result == {
        "status": "UNKNOWN",
        "detail": "exact saturation exceeds 1 reachable states",
    }


def test_invalid_json_value_shape_returns_unsupported(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"x": {"domain": [{"not": "a scalar"}], "initial": [0], "durability": "durable"}},
        "transitions": [],
        "invariants": [],
        "crash_recovery": False,
        "check_deadlock": False,
    })

    result = check_protocol_model(root, path)

    assert result["status"] == "UNSUPPORTED"
    assert "finite values" in result["detail"]


def test_boolean_and_integer_domains_are_not_interchangeable(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"x": {"domain": [True], "initial": [1], "durability": "durable"}},
        "transitions": [],
        "invariants": [],
        "crash_recovery": False,
        "check_deadlock": False,
    })

    result = check_protocol_model(root, path)

    assert result["status"] == "UNSUPPORTED"
    assert "one scalar type" in result["detail"]


def test_missing_pinned_engine_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"x": {"domain": [0], "initial": [0], "durability": "durable"}},
        "transitions": [],
        "invariants": [],
        "crash_recovery": False,
        "check_deadlock": False,
    })
    monkeypatch.delenv("VKIT_TLA2TOOLS_JAR", raising=False)

    result = check_protocol_model(root, path)

    assert result["status"] == "UNAVAILABLE"


@pytest.mark.skipif(not _engine_available(), reason="pinned TLC engine is not configured")
def test_initial_invariant_failure_with_multiple_initial_states_is_replayed(tmp_path: Path) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"x": {"domain": [0, 1], "initial": [0, 1], "durability": "durable"}},
        "transitions": [],
        "invariants": [{"name": "x_zero", "expression": {"eq": [{"var": "x"}, {"int": 0}]}}],
        "crash_recovery": False,
        "check_deadlock": False,
    })

    result = check_protocol_model(root, path)

    assert result["status"] == "COUNTEREXAMPLE"
    assert result["kind"] == "INVARIANT_VIOLATION"
    assert result["trace"] == [{"variables": {"x": 1}}]


@pytest.mark.skipif(not _engine_available(), reason="pinned TLC engine is not configured")
def test_tlc_timeout_is_unknown(tmp_path: Path, monkeypatch) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"x": {"domain": [0], "initial": [0], "durability": "durable"}},
        "transitions": [],
        "invariants": [],
        "crash_recovery": False,
        "check_deadlock": False,
    })

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr(protocols, "_run_tree", timeout)
    result = check_protocol_model(root, path)

    assert result["status"] == "UNKNOWN"
    assert "did not finish" in result["detail"]


@pytest.mark.skipif(not _engine_available(), reason="pinned TLC engine is not configured")
def test_failed_reference_trace_replay_is_unknown(tmp_path: Path, monkeypatch) -> None:
    root, path = _write_model(tmp_path, {
        "schema_version": 1,
        "variables": {"x": {"domain": [0], "initial": [0], "durability": "durable"}},
        "transitions": [],
        "invariants": [{"name": "never", "expression": {"bool": False}}],
        "crash_recovery": False,
        "check_deadlock": False,
    })
    monkeypatch.setattr(protocols, "_replay_counterexample", lambda *args: False)

    result = check_protocol_model(root, path)

    assert result["status"] == "UNKNOWN"
    assert "failed independent replay" in result["detail"]

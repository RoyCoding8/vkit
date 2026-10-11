from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vkit.operations.protocols import check_protocol_model


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

    with pytest.raises(ValueError, match="unsupported"):
        check_protocol_model(root, path)

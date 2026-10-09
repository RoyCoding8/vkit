from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkit.operations.history import check_history


def _write_trace(root: Path, operations: list[dict], *, name: str = "history.json",
                 extra: dict | None = None) -> Path:
    trace = {"schema_version": 1, "operations": operations}
    if extra:
        trace.update(extra)
    path = root / name
    path.write_text(json.dumps(trace), encoding="utf-8")
    return path


def _op(operation_id: str, call: int, returned: int, action: str,
        value: int | None = None, output: int | None = None, client_id: int = 0) -> dict:
    input_ = {"op": action}
    if action in {"write", "enqueue"}:
        input_["value"] = value
    return {"id": operation_id, "client_id": client_id, "call": call, "return": returned,
            "input": input_, "output": output}


@pytest.fixture
def configured_helper(monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = os.environ.get("VKIT_HISTORY_BIN")
    expected = os.environ.get("VKIT_HISTORY_SHA256")
    if not binary or not expected:
        pytest.skip("a pinned Porcupine helper is not configured")
    path = Path(binary).resolve()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected.lower():
        pytest.skip("the configured Porcupine helper does not match its SHA-256")
    monkeypatch.setenv("VKIT_HISTORY_BIN", str(path))
    monkeypatch.setenv("VKIT_HISTORY_SHA256", expected.lower())
    return path


def test_register_history_returns_a_complete_replayable_linearization(tmp_path, configured_helper):
    source = tmp_path / "subject.py"
    source.write_text("VALUE = 7\n", encoding="utf-8")
    trace = _write_trace(tmp_path, [
        _op("write", 1, 2, "write", value=7),
        _op("read", 3, 4, "read", output=7, client_id=1),
    ])

    result = check_history(tmp_path, trace.name, "register")

    assert result["status"] == "LINEARIZABLE"
    assert result["linearization"] == ["write", "read"]
    assert result["backend"] == "porcupine/1.3.1"
    assert source.read_text(encoding="utf-8") == "VALUE = 7\n"


def test_register_rejects_a_real_time_old_read(tmp_path, configured_helper):
    trace = _write_trace(tmp_path, [
        _op("write", 1, 2, "write", value=7),
        _op("old-read", 3, 4, "read", output=0),
    ])

    result = check_history(tmp_path, trace.name, "register")

    assert result["status"] == "NOT_LINEARIZABLE"
    assert result["linearization"] is None


def test_overlapping_register_operations_can_linearize_read_before_write(tmp_path, configured_helper):
    trace = _write_trace(tmp_path, [
        _op("write", 1, 4, "write", value=7),
        _op("overlap-read", 2, 3, "read", output=0, client_id=1),
    ])

    result = check_history(tmp_path, trace.name, "register")

    assert result["status"] == "LINEARIZABLE"
    assert result["linearization"] == ["overlap-read", "write"]


def test_equal_timestamp_endpoints_are_concurrent_under_porcupine_semantics(tmp_path, configured_helper):
    trace = _write_trace(tmp_path, [
        _op("write", 1, 2, "write", value=7),
        _op("same-boundary-read", 2, 3, "read", output=0, client_id=1),
    ])

    result = check_history(tmp_path, trace.name, "register")

    assert result["status"] == "LINEARIZABLE"
    assert result["linearization"] == ["same-boundary-read", "write"]


def test_queue_rejects_fifo_violation_and_accepts_empty_dequeue(tmp_path, configured_helper):
    trace = _write_trace(tmp_path, [
        _op("enqueue-1", 1, 2, "enqueue", value=1),
        _op("enqueue-2", 3, 4, "enqueue", value=2),
        _op("dequeue", 5, 6, "dequeue", output=2),
    ], name="fifo.json")
    empty = _write_trace(tmp_path, [_op("empty-dequeue", 1, 2, "dequeue", output=None)], name="empty.json")

    fifo_result = check_history(tmp_path, trace.name, "queue")
    empty_result = check_history(tmp_path, empty.name, "queue")

    assert fifo_result["status"] == "NOT_LINEARIZABLE"
    assert empty_result["status"] == "LINEARIZABLE"
    assert empty_result["linearization"] == ["empty-dequeue"]


def test_empty_history_is_vacuously_linearizable(tmp_path, configured_helper):
    trace = _write_trace(tmp_path, [])

    result = check_history(tmp_path, trace.name, "register")

    assert result["status"] == "LINEARIZABLE"
    assert result["linearization"] == []


def test_invalid_history_is_rejected_before_backend_use(tmp_path, monkeypatch):
    trace = _write_trace(tmp_path, [_op("pending", 1, 2, "read", output=0)], extra={"commands": []})
    monkeypatch.delenv("VKIT_HISTORY_BIN", raising=False)
    monkeypatch.delenv("VKIT_HISTORY_SHA256", raising=False)

    with pytest.raises(ValueError, match="unknown commands"):
        check_history(tmp_path, trace.name, "register")


def test_pending_operation_is_rejected_instead_of_dropped(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text(
        '{"schema_version":1,"operations":[{"id":"pending","client_id":0,"call":1,'
        '"input":{"op":"read"},"output":0}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing return"):
        check_history(tmp_path, path.name, "register")


def test_missing_or_mismatched_backend_is_unavailable(tmp_path, monkeypatch):
    trace = _write_trace(tmp_path, [_op("read", 1, 2, "read", output=0)])
    monkeypatch.delenv("VKIT_HISTORY_BIN", raising=False)
    monkeypatch.delenv("VKIT_HISTORY_SHA256", raising=False)

    missing = check_history(tmp_path, trace.name, "register")

    fake = tmp_path / "helper.exe"
    fake.write_bytes(b"not the configured helper")
    monkeypatch.setenv("VKIT_HISTORY_BIN", str(fake))
    monkeypatch.setenv("VKIT_HISTORY_SHA256", "0" * 64)
    mismatched = check_history(tmp_path, trace.name, "register")

    assert missing["status"] == mismatched["status"] == "UNAVAILABLE"


def test_partial_or_malformed_helper_witness_never_proves_history(tmp_path, monkeypatch):
    trace = _write_trace(tmp_path, [
        _op("first", 1, 2, "write", value=7),
        _op("second", 3, 4, "read", output=7),
    ])
    binary = tmp_path / "verified-helper"
    binary.write_bytes(b"trusted by test seam")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setenv("VKIT_HISTORY_BIN", str(binary))
    monkeypatch.setenv("VKIT_HISTORY_SHA256", digest)
    monkeypatch.setattr("vkit.operations.history.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout=b'{"protocol_version":1,"backend_version":"1.3.1","status":"OK","linearization":["first"]}',
        stderr=b"",
    ))

    result = check_history(tmp_path, trace.name, "register")

    assert result["status"] == "UNKNOWN"
    assert result["detail"] == "the helper did not return a complete independently replayable witness"


def test_trace_file_escape_and_integer_overflow_are_rejected(tmp_path):
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="inside the repository"):
        check_history(tmp_path, "../outside.json", "register")

    trace = _write_trace(tmp_path, [_op("too-large", 1, 2, "write", value=2**63)])
    with pytest.raises(ValueError, match="input.value"):
        check_history(tmp_path, trace.name, "register")

    too_many = _write_trace(tmp_path, [
        _op(str(index), 2 * index + 1, 2 * index + 2, "read", output=0)
        for index in range(1_001)
    ])
    with pytest.raises(ValueError, match="at most 1000"):
        check_history(tmp_path, too_many.name, "register")

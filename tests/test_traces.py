from __future__ import annotations

import hashlib
import importlib.metadata
import os

import pytest
from helpers import McpClient, vkit

from vkit.operations.traces import (
    MAX_TRACE_BYTES,
    _analyze,
    _processor_binary,
    explain_trace_interval,
)


if os.environ.get("VKIT_REQUIRE_TRACE_ENGINE"):
    import perfetto
else:
    pytest.importorskip("perfetto", reason="optional Perfetto engine is not installed")


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _integer(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _string(field: int, value: str) -> bytes:
    return _bytes(field, value.encode("utf-8"))


def _sched_switch(timestamp_ns: int, prev_comm: str, prev_pid: int, prev_state: int,
                  next_comm: str, next_pid: int) -> bytes:
    switch = (_string(1, prev_comm) + _integer(2, prev_pid) + _integer(3, 120)
              + _integer(4, prev_state) + _string(5, next_comm)
              + _integer(6, next_pid) + _integer(7, 120))
    event = _integer(1, timestamp_ns) + _integer(2, 0) + _bytes(4, switch)
    return _bytes(2, event)


def _process(pid: int, tid: int, name: str) -> bytes:
    process = _integer(1, pid) + _string(3, f"process-{pid}")
    thread = _integer(1, tid) + _integer(2, pid) + _string(3, name)
    return _bytes(2, _bytes(1, process) + _bytes(2, thread))


def _scheduler_trace(*, lost_events: bool = False, late_thread: bool = False,
                     duplicate_tid: bool = False, offset_ns: int = 0) -> bytes:
    # Minimal public Perfetto Trace/TracePacket/FtraceEventBundle schema:
    # sched_switch at 1, 5, 8, and 10 seconds; worker TID 100 sleeps from 5 to 8.
    packets = bytearray()
    if duplicate_tid:
        packets += _bytes(1, _process(10, 100, "worker-a"))
        packets += _bytes(1, _process(20, 100, "worker-b"))
    bundle = _integer(1, 0)
    if lost_events:
        bundle += _integer(3, 1)
    if late_thread:
        bundle += _sched_switch(offset_ns + 1_000_000_000, "idle", 0, 0, "other", 200)
        bundle += _sched_switch(offset_ns + 2_000_000_000, "other", 200, 0, "worker", 100)
    else:
        bundle += _sched_switch(offset_ns + 1_000_000_000, "idle", 0, 0, "worker", 100)
    bundle += _sched_switch(offset_ns + 5_000_000_000, "worker", 100, 1, "idle", 0)
    bundle += _sched_switch(offset_ns + 8_000_000_000, "idle", 0, 0, "worker", 100)
    bundle += _sched_switch(offset_ns + 10_000_000_000, "worker", 100, 0, "idle", 0)
    packet = _bytes(1, bundle)  # TracePacket.ftrace_events
    packets += _bytes(1, packet)
    return bytes(packets)      # Trace.packet (repeated)


def _require_preinstalled_engine() -> None:
    binary, _, reason = _processor_binary()
    if binary is None:
        assert not os.environ.get("VKIT_REQUIRE_TRACE_ENGINE"), reason
        pytest.skip(reason or "pinned Trace Processor is not preinstalled")


def test_trace_operation_cli_and_mcp(make_project):
    _require_preinstalled_engine()
    project = make_project({}, [], accept=False)
    (project / "sched.trace").write_bytes(_scheduler_trace())
    code, body = vkit(project, "compute", "explain-trace-interval", "--path", "sched.trace",
                      "--start-ns", "1500000000", "--end-ns", "9500000000", "--tid", "100")
    assert (code, body["status"], body["durations_ns"]["running"]) == (0, "MEASURED", 5000000000)
    client = McpClient(project)
    try:
        body, error = client.call("explain_trace_interval", path="sched.trace", start_ns=1500000000,
                                 end_ns=9500000000, tid=100)
        assert not error and body["status"] == "MEASURED"
    finally:
        client.close()


def test_explain_trace_interval_uses_real_perfetto_scheduler_rows(tmp_path) -> None:
    _require_preinstalled_engine()
    _, expected_sha, _ = _processor_binary()
    (tmp_path / "sched.perfetto-trace").write_bytes(_scheduler_trace())

    result = explain_trace_interval(tmp_path, "sched.perfetto-trace", 1_500_000_000,
                                    9_500_000_000, 100)

    assert result["status"] == "MEASURED"
    assert result["processor_package"] == "0.58.2"
    assert result["processor_sha256"] == expected_sha
    assert result["trace_sha256"]
    assert result["thread"]["tid"] == 100
    assert result["clock_basis"].startswith("nanoseconds on Trace Processor's normalized")
    assert result["trace_bounds_ns"] == {"start": 1_000_000_000, "end": 10_000_000_000}
    assert result["segments"][0]["cpu"] == 0
    assert result["events_truncated"] is False
    assert result["durations_ns"] == {
        "running": 5_000_000_000,
        "runnable": 0,
        "blocked_sleep_or_uninterruptible": 3_000_000_000,
        "other_states": 0,
        "covered": 8_000_000_000,
        "unobserved": 0,
        "overlap": 0,
    }
    assert [segment["state"] for segment in result["segments"]] == ["Running", "S", "Running"]
    assert [(s["start_ns"], s["end_ns"], s["duration_ns"]) for s in result["segments"]] == [
        (1_500_000_000, 5_000_000_000, 3_500_000_000),
        (5_000_000_000, 8_000_000_000, 3_000_000_000),
        (8_000_000_000, 9_500_000_000, 1_500_000_000),
    ]
    assert result["data_loss"] == []


def test_explain_trace_interval_marks_perfetto_ftrace_loss_incomplete(tmp_path) -> None:
    _require_preinstalled_engine()
    (tmp_path / "lost.perfetto-trace").write_bytes(_scheduler_trace(lost_events=True))

    result = explain_trace_interval(tmp_path, "lost.perfetto-trace", 1_500_000_000,
                                    9_500_000_000, 100)

    assert result["status"] == "INCOMPLETE"
    assert result["durations_ns"]["covered"] == 8_000_000_000
    assert result["data_loss"] == [{"name": "ftrace_cpu_has_data_loss", "value": 1, "source": "trace"}]


def test_explain_trace_interval_rejects_path_escape_and_invalid_interval(tmp_path) -> None:
    with pytest.raises(ValueError, match="segments"):
        explain_trace_interval(tmp_path, "../outside.trace", 0, 1, 1)
    with pytest.raises(ValueError, match="interval must be non-empty"):
        explain_trace_interval(tmp_path, "trace", 2, 1, 1)


def test_explain_trace_interval_rejects_ambiguous_tid_without_upid(tmp_path) -> None:
    _require_preinstalled_engine()
    (tmp_path / "ambiguous.trace").write_bytes(_scheduler_trace(duplicate_tid=True))

    result = explain_trace_interval(tmp_path, "ambiguous.trace", 1_500_000_000,
                                    9_500_000_000, 100)

    assert result["status"] == "UNSUPPORTED"
    assert result["reason"] == "thread ID is ambiguous; supply trace-local upid"
    assert len({candidate["upid"] for candidate in result["candidates"]}) == 2


def test_explain_trace_interval_marks_missing_state_coverage_incomplete(tmp_path) -> None:
    _require_preinstalled_engine()
    (tmp_path / "gap.trace").write_bytes(_scheduler_trace(late_thread=True))

    result = explain_trace_interval(tmp_path, "gap.trace", 1_000_000_000,
                                    4_000_000_000, 100)

    assert result["status"] == "INCOMPLETE"
    assert result["durations_ns"]["unobserved"] == 1_000_000_000
    assert result["durations_ns"]["covered"] == 2_000_000_000


def test_explain_trace_interval_rejects_trace_bounds_and_malformed_bytes(tmp_path) -> None:
    _require_preinstalled_engine()
    (tmp_path / "outside.trace").write_bytes(_scheduler_trace())
    outside = explain_trace_interval(tmp_path, "outside.trace", 0, 1_000_000_000, 100)
    assert outside["status"] == "UNSUPPORTED"
    assert outside["reason"] == "requested interval is outside trace bounds"
    assert outside["trace_bounds_ns"] == {"start": 1_000_000_000, "end": 10_000_000_000}

    (tmp_path / "malformed.trace").write_bytes(b"this is not a Perfetto trace")
    malformed = explain_trace_interval(tmp_path, "malformed.trace", 0, 1, 100)
    assert malformed["status"] in {"UNSUPPORTED", "UNKNOWN"}
    assert malformed["status"] != "MEASURED"


def test_explain_trace_interval_rejects_capture_over_limit(tmp_path) -> None:
    oversized = tmp_path / "oversized.trace"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_TRACE_BYTES + 1)
    with pytest.raises(ValueError, match="at most"):
        explain_trace_interval(tmp_path, "oversized.trace", 0, 1, 100)


def test_trace_analysis_rejects_bounds_outside_exact_integer_arithmetic(tmp_path) -> None:
    _require_preinstalled_engine()
    offset = 1 << 61
    raw = _scheduler_trace(offset_ns=offset)
    trace = tmp_path / "wide-timestamps.trace"
    trace.write_bytes(raw)

    result = _analyze({
        "trace_path": str(trace),
        "trace_sha256": hashlib.sha256(raw).hexdigest(),
        "start_ns": offset + 1_500_000_000,
        "end_ns": offset + 9_500_000_000,
        "tid": 100,
        "upid": None,
    })

    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "trace bounds exceed the exact scheduler arithmetic range"


@pytest.mark.parametrize("installed_version", [None, "0.58.1"])
def test_trace_analysis_refuses_absent_or_mismatched_pinned_processor(
    tmp_path, monkeypatch, installed_version
) -> None:
    trace = tmp_path / "captured.trace"
    raw = _scheduler_trace()
    trace.write_bytes(raw)

    def reported_version(package: str) -> str:
        assert package == "perfetto"
        if installed_version is None:
            raise importlib.metadata.PackageNotFoundError(package)
        return installed_version

    monkeypatch.setattr("vkit.operations.traces.importlib.metadata.version", reported_version)

    result = _analyze({
        "trace_path": str(trace),
        "trace_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "start_ns": 1_500_000_000,
        "end_ns": 9_500_000_000,
        "tid": 100,
        "upid": None,
    })

    assert result["status"] == "UNAVAILABLE"
    if installed_version is None:
        assert result["reason"] == "optional perfetto package is not installed"
    else:
        assert result["reason"] == f"perfetto package must be exactly 0.58.2, found {installed_version}"

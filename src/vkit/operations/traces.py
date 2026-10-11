"""Bounded scheduling facts from a captured Perfetto trace."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..nowindow import hidden_window
from ._data import encode_json

PERFETTO_VERSION = "0.58.2"
MAX_TRACE_BYTES = 32 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 120
MAX_INTERVAL_NS = 3_600_000_000_000
MAX_SQL_INT = (1 << 63) - 1
MAX_EXACT_TS = 1 << 61
MAX_WORKER_INPUT_BYTES = 4_096
MAX_WORKER_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_EVENT_SAMPLES = 100
MAX_LOSS_ROWS = 128
_STATUSES = {"MEASURED", "INCOMPLETE", "UNSUPPORTED", "UNAVAILABLE", "UNKNOWN"}


def _result(status: str, trace_sha256: str | None, **details: Any) -> dict[str, Any]:
    return {"status": status, "trace_sha256": trace_sha256, **details}


def _read_trace(root: Path, path: str) -> bytes:
    if (not isinstance(path, str) or not path or Path(path).is_absolute()
            or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive):
        raise ValueError("path must be a non-empty repository-relative path")
    parts = path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must not contain empty, '.' or '..' segments")
    try:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("root must identify a directory")
        target = resolved_root.joinpath(*parts)
        current = resolved_root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("trace path must not contain symlinks")
        target = target.resolve(strict=True)
        if not target.is_relative_to(resolved_root) or not target.is_file():
            raise ValueError("path must identify a regular file inside root")
        with target.open("rb") as handle:
            raw = handle.read(MAX_TRACE_BYTES + 1)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot capture trace {path!r}: {exc}") from exc
    if len(raw) > MAX_TRACE_BYTES:
        raise ValueError(f"trace must be at most {MAX_TRACE_BYTES} bytes")
    if not raw:
        raise ValueError("trace must not be empty")
    return raw


def _processor_binary() -> tuple[Path | None, str | None, str | None]:
    """Find only the package-pinned, already-cached shell; this never downloads."""
    try:
        installed = importlib.metadata.version("perfetto")
    except importlib.metadata.PackageNotFoundError:
        return None, None, "optional perfetto package is not installed"
    if installed != PERFETTO_VERSION:
        return None, None, f"perfetto package must be exactly {PERFETTO_VERSION}, found {installed}"
    try:
        from perfetto.prebuilts.manifests.trace_processor_shell import TRACE_PROCESSOR_SHELL_MANIFEST
    except ImportError as exc:
        return None, None, f"pinned Trace Processor manifest is unavailable: {exc}"

    entry = next((item for item in TRACE_PROCESSOR_SHELL_MANIFEST
                  if item.get("platform") == sys.platform
                  and platform.machine().lower() in item.get("machine", [])), None)
    if entry is None:
        return None, None, f"Perfetto has no pinned Trace Processor for {sys.platform}-{platform.machine()}"
    expected_hash = entry.get("sha256")
    expected_size = entry.get("file_size")
    file_name = entry.get("file_name")
    if (not isinstance(expected_hash, str) or len(expected_hash) != 64
            or type(expected_size) is not int or not isinstance(file_name, str)):
        return None, None, "pinned Trace Processor manifest entry is invalid"
    stem, suffix = os.path.splitext(file_name)
    binary = (Path.home() / ".local" / "share" / "perfetto" / "prebuilts"
              / f"{stem}-{expected_hash[:16]}{suffix}")
    try:
        if binary.is_symlink() or not binary.is_file() or binary.stat().st_size != expected_size:
            return None, None, "pinned Trace Processor binary is not preinstalled in the Perfetto cache"
        digest = hashlib.sha256()
        with binary.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        return None, None, f"cannot verify pinned Trace Processor binary: {exc}"
    if digest.hexdigest() != expected_hash:
        return None, None, "cached Trace Processor binary does not match the pinned SHA-256"
    return binary, expected_hash, None


def _rows(processor: Any, sql: str) -> list[dict[str, Any]]:
    return [dict(row.__dict__) for row in processor.query(sql)]


def _analyze(request: dict[str, Any]) -> dict[str, Any]:
    binary, binary_sha256, unavailable = _processor_binary()
    trace_sha256 = request["trace_sha256"]
    if binary is None:
        return _result("UNAVAILABLE", trace_sha256, reason=unavailable,
                       processor_package=PERFETTO_VERSION)
    from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig

    start_ns, end_ns = request["start_ns"], request["end_ns"]
    tid, upid = request["tid"], request["upid"]
    try:
        trace_file = Path(request["trace_path"])
        if trace_file.is_symlink() or not trace_file.is_file() or trace_file.stat().st_size > MAX_TRACE_BYTES:
            return _result("UNKNOWN", trace_sha256, reason="private trace snapshot is not a bounded regular file")
        with trace_file.open("rb") as handle:
            actual = hashlib.sha256(handle.read(MAX_TRACE_BYTES + 1)).hexdigest()
        if actual != trace_sha256:
            return _result("UNKNOWN", trace_sha256, reason="private trace snapshot changed before analysis")
        with TraceProcessor(
            trace=request["trace_path"],
            config=TraceProcessorConfig(bin_path=str(binary), load_timeout=20),
        ) as processor:
            bounds = _rows(processor, "SELECT start_ts, end_ts FROM trace_bounds LIMIT 1")
            if not bounds or bounds[0]["start_ts"] is None or bounds[0]["end_ts"] is None:
                return _result("UNSUPPORTED", trace_sha256, reason="trace has no bounded timestamp interval")
            raw_trace_start, raw_trace_end = bounds[0]["start_ts"], bounds[0]["end_ts"]
            if (type(raw_trace_start) is not int or type(raw_trace_end) is not int
                    or not -MAX_EXACT_TS <= raw_trace_start <= MAX_EXACT_TS
                    or not -MAX_EXACT_TS <= raw_trace_end <= MAX_EXACT_TS
                    or raw_trace_end <= raw_trace_start):
                return _result("UNKNOWN", trace_sha256,
                               reason="trace bounds exceed the exact scheduler arithmetic range")
            trace_start, trace_end = raw_trace_start, raw_trace_end
            if start_ns < trace_start or end_ns > trace_end:
                return _result("UNSUPPORTED", trace_sha256, reason="requested interval is outside trace bounds",
                               trace_bounds_ns={"start": trace_start, "end": trace_end})

            thread_sql = ("SELECT utid, tid, upid, substr(name, 1, 256) AS name FROM thread "
                          f"WHERE tid = {tid}")
            if upid is not None:
                thread_sql += f" AND upid = {upid}"
            threads = _rows(processor, thread_sql + " ORDER BY utid LIMIT 3")
            if not threads:
                return _result("UNSUPPORTED", trace_sha256, reason="trace contains no matching thread",
                               processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)
            if len(threads) > 1:
                return _result("UNSUPPORTED", trace_sha256, reason="thread ID is ambiguous; supply trace-local upid",
                               candidates=threads, processor_package=PERFETTO_VERSION,
                               processor_sha256=binary_sha256)
            thread = threads[0]
            utid = int(thread["utid"])
            unsafe_states = _rows(processor,
                "SELECT COUNT(*) AS row_count FROM thread_state "
                f"WHERE utid = {utid} AND (ts < {-MAX_EXACT_TS} OR ts > {MAX_EXACT_TS} "
                f"OR dur < {-MAX_EXACT_TS} OR dur > {MAX_EXACT_TS} OR (dur < 0 AND dur != -1))")
            if (len(unsafe_states) != 1 or type(unsafe_states[0]["row_count"]) is not int
                    or unsafe_states[0]["row_count"] != 0):
                return _result("UNKNOWN", trace_sha256,
                               reason="thread states exceed the exact scheduler arithmetic range",
                               processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)
            state_filter = (f"utid = {utid} AND ts < {end_ns} "
                            f"AND (dur < 0 OR ts + dur > {start_ns})")
            state_totals = _rows(processor,
                "SELECT state, COUNT(*) AS row_count, "
                f"SUM(MIN({end_ns}, CASE WHEN dur < 0 THEN {trace_end} ELSE ts + dur END) "
                f"- MAX({start_ns}, ts)) AS duration_ns FROM thread_state WHERE {state_filter} "
                "GROUP BY state ORDER BY state")
            coverage_sql = (
                "WITH clipped AS ("
                f"SELECT MAX(ts, {start_ns}) AS s, "
                f"MIN({end_ns}, CASE WHEN dur < 0 THEN {trace_end} ELSE ts + dur END) AS e "
                f"FROM thread_state WHERE {state_filter}), "
                "ordered AS (SELECT s, e, MAX(e) OVER (ORDER BY s, e "
                "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_end FROM clipped) "
                "SELECT COALESCE(SUM(CASE WHEN s > COALESCE(previous_end, "
                f"{start_ns}) THEN s - COALESCE(previous_end, {start_ns}) ELSE 0 END), 0) "
                f"+ {end_ns} - COALESCE(MAX(e), {start_ns}) AS unobserved_ns, "
                "COALESCE(SUM(CASE WHEN s < COALESCE(previous_end, "
                f"{start_ns}) THEN MIN(e, previous_end) - s ELSE 0 END), 0) AS overlap_ns FROM ordered"
            )
            coverage = _rows(processor, coverage_sql)[0]
            sample_rows = _rows(processor,
                "SELECT MAX(ts, " + str(start_ns) + ") AS start_ns, "
                f"MIN({end_ns}, CASE WHEN dur < 0 THEN {trace_end} ELSE ts + dur END) AS end_ns, "
                "state, cpu, io_wait, substr(blocked_function, 1, 64) AS blocked_function, waker_utid "
                f"FROM thread_state WHERE {state_filter} ORDER BY ts, dur, state LIMIT {MAX_EVENT_SAMPLES + 1}")
            losses = _rows(processor, "SELECT name, value, source FROM stats "
                            "WHERE severity = 'data_loss' AND value != 0 ORDER BY name "
                            f"LIMIT {MAX_LOSS_ROWS + 1}")
    except Exception as exc:
        return _result("UNKNOWN", trace_sha256,
                       reason=f"pinned Trace Processor could not analyze the trace: {type(exc).__name__}: {exc}",
                       processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)

    if len(losses) > MAX_LOSS_ROWS:
        return _result("INCOMPLETE", trace_sha256, reason="trace result exceeded a fixed row limit",
                       thread=thread, interval_ns={"start": start_ns, "end": end_ns},
                       processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)
    if not state_totals:
        return _result("UNSUPPORTED", trace_sha256, reason="trace has no scheduler states for the selected thread",
                       thread=thread, interval_ns={"start": start_ns, "end": end_ns},
                       processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)

    interval_ns = end_ns - start_ns
    if (set(coverage) != {"unobserved_ns", "overlap_ns"}
            or type(coverage["unobserved_ns"]) is not int
            or type(coverage["overlap_ns"]) is not int
            or any(type(row["row_count"]) is not int or type(row["duration_ns"]) is not int
                   or row["row_count"] < 0 or row["duration_ns"] < 0 for row in state_totals)):
        return _result("UNKNOWN", trace_sha256, reason="Trace Processor returned non-integer scheduler aggregates",
                       processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)
    unobserved_ns = coverage["unobserved_ns"]
    overlap_ns = coverage["overlap_ns"]
    if (unobserved_ns < 0 or unobserved_ns > interval_ns or overlap_ns < 0
            or sum(row["duration_ns"] for row in state_totals)
            != interval_ns - unobserved_ns + overlap_ns):
        return _result("UNKNOWN", trace_sha256, reason="scheduler aggregates violate interval arithmetic",
                       processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)
    covered_ns = interval_ns - unobserved_ns
    totals = {str(row["state"]): row["duration_ns"] for row in state_totals}
    running_ns = totals.get("Running", 0)
    runnable_ns = totals.get("R", 0)
    blocked_ns = totals.get("S", 0) + totals.get("D", 0)
    other_ns = sum(duration for state, duration in totals.items()
                   if state not in {"Running", "R", "S", "D"})
    if any(type(row["start_ns"]) is not int or type(row["end_ns"]) is not int
           or row["end_ns"] <= row["start_ns"] for row in sample_rows):
        return _result("UNKNOWN", trace_sha256, reason="Trace Processor returned impossible scheduler segments",
                       processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256)
    segments = [{"start_ns": row["start_ns"], "end_ns": row["end_ns"],
                 "duration_ns": row["end_ns"] - row["start_ns"],
                 "state": row["state"], "cpu": row["cpu"], "io_wait": row["io_wait"],
                 "blocked_function": row["blocked_function"], "waker_utid": row["waker_utid"]}
                for row in sample_rows[:MAX_EVENT_SAMPLES]]
    complete = not losses and unobserved_ns == 0 and overlap_ns == 0
    status = "MEASURED" if complete else "INCOMPLETE"
    return _result(
        status, trace_sha256,
        reason=None if complete else "observed trace contains data loss, uncovered time, or overlapping states",
        processor_package=PERFETTO_VERSION, processor_sha256=binary_sha256,
        clock_basis="nanoseconds on Trace Processor's normalized trace timeline; cross-source clock synchronization is not independently proven",
        interval_ns={"start": start_ns, "end": end_ns, "duration": interval_ns},
        trace_bounds_ns={"start": trace_start, "end": trace_end},
        thread={**thread, "utid": utid},
        durations_ns={"running": running_ns, "runnable": runnable_ns, "blocked_sleep_or_uninterruptible": blocked_ns,
                      "other_states": other_ns, "covered": covered_ns, "unobserved": unobserved_ns,
                      "overlap": overlap_ns},
        state_totals=[{"state": row["state"], "row_count": int(row["row_count"]),
                       "duration_ns": int(row["duration_ns"])} for row in state_totals],
        state_row_count=sum(int(row["row_count"]) for row in state_totals),
        segments=segments, events_truncated=sum(int(row["row_count"]) for row in state_totals) > MAX_EVENT_SAMPLES,
        data_loss=losses,
    )


def _worker() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            raise ValueError("worker request exceeds its byte limit")
        request = json.loads(raw.decode("utf-8"))
        expected = {"trace_path", "trace_sha256", "start_ns", "end_ns", "tid", "upid"}
        if (not isinstance(request, dict) or set(request) != expected
                or not isinstance(request["trace_path"], str) or not Path(request["trace_path"]).is_absolute()
                or not isinstance(request["trace_sha256"], str) or len(request["trace_sha256"]) != 64
                or any(type(request[key]) is not int for key in ("start_ns", "end_ns", "tid"))
                or (request["upid"] is not None and type(request["upid"]) is not int)):
            raise ValueError("worker request has invalid fields")
        if (any(character not in "0123456789abcdef" for character in request["trace_sha256"])
                or not -MAX_EXACT_TS <= request["start_ns"] <= MAX_EXACT_TS
                or not -MAX_EXACT_TS <= request["end_ns"] <= MAX_EXACT_TS
                or request["end_ns"] <= request["start_ns"]
                or request["end_ns"] - request["start_ns"] > MAX_INTERVAL_NS
                or not 0 <= request["tid"] <= MAX_SQL_INT
                or (request["upid"] is not None and not 0 <= request["upid"] <= MAX_SQL_INT)):
            raise ValueError("worker request values exceed supported bounds")
        result = _analyze(request)
    except Exception as exc:
        result = _result("UNKNOWN", None, reason=f"isolated trace worker could not complete: {type(exc).__name__}")
    try:
        output = encode_json(result, max_bytes=MAX_WORKER_OUTPUT_BYTES)
    except (TypeError, ValueError, RecursionError):
        output = encode_json(_result("UNKNOWN", None, reason="worker result exceeded its JSON byte limit"),
                             max_bytes=MAX_WORKER_OUTPUT_BYTES)
    sys.stdout.write(output)
    return 0


def _terminate_worker(process: subprocess.Popen[bytes], job: Any) -> None:
    if os.name == "nt":
        if job is not None:
            import win32job
            win32job.TerminateJobObject(job, 1)
        else:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _close_job(job: Any) -> None:
    if job is not None:
        import win32api
        win32api.CloseHandle(job)


def _start_worker() -> tuple[subprocess.Popen[bytes], Any]:
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "vkit.operations.traces", "--worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        **({"start_new_session": True} if os.name != "nt" else hidden_window()),
    )
    if os.name != "nt":
        return process, None
    job = None
    try:
        import win32api
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        win32job.AssignProcessToJobObject(job, int(process._handle))
    except Exception as exc:
        process.kill()
        process.wait(timeout=5)
        if job is not None:
            import win32api
            win32api.CloseHandle(job)
        raise OSError(f"cannot establish a kill-on-close worker job: {exc}") from exc
    return process, job


def explain_trace_interval(root: Path, path: str, start_ns: int, end_ns: int, tid: int, *,
                           upid: int | None = None, timeout_seconds: int = 30) -> dict[str, Any]:
    """Report bounded scheduling state facts for one thread and trace-clock interval."""
    if (type(start_ns) is not int or type(end_ns) is not int
            or not -MAX_EXACT_TS <= start_ns <= MAX_EXACT_TS
            or not -MAX_EXACT_TS <= end_ns <= MAX_EXACT_TS
        or end_ns <= start_ns
            or end_ns - start_ns > MAX_INTERVAL_NS):
        raise ValueError("interval must be non-empty, at most one hour, and inside the exact nanosecond range")
    if type(tid) is not int or not 0 <= tid <= MAX_SQL_INT:
        raise ValueError("tid must be a non-negative integer")
    if upid is not None and (type(upid) is not int or not 0 <= upid <= MAX_SQL_INT):
        raise ValueError("upid must be a non-negative integer or None")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}")

    started = time.monotonic()
    raw_trace = _read_trace(root, path)
    trace_sha256 = hashlib.sha256(raw_trace).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="vkit-trace-") as directory:
            trace_path = Path(directory) / "captured.perfetto-trace"
            trace_path.write_bytes(raw_trace)
            request = encode_json({"trace_path": str(trace_path.resolve()), "trace_sha256": trace_sha256,
                                   "start_ns": start_ns, "end_ns": end_ns, "tid": tid, "upid": upid},
                                  max_bytes=MAX_WORKER_INPUT_BYTES)
            process, job = _start_worker()
            try:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    _terminate_worker(process, job)
                    process.wait(timeout=5)
                    return _result("UNKNOWN", trace_sha256, reason="trace worker exceeded its wall deadline")
                try:
                    stdout, _ = process.communicate(request.encode("ascii"), timeout=remaining)
                except subprocess.TimeoutExpired:
                    _terminate_worker(process, job)
                    process.wait(timeout=5)
                    return _result("UNKNOWN", trace_sha256, reason="Trace Processor exceeded its wall deadline")
            finally:
                _close_job(job)
            if process.returncode != 0 or len(stdout) > MAX_WORKER_OUTPUT_BYTES:
                return _result("UNKNOWN", trace_sha256, reason="isolated trace worker returned no bounded result")
            try:
                result = json.loads(stdout.decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _result("UNKNOWN", trace_sha256, reason="isolated trace worker returned invalid JSON")
            if not isinstance(result, dict) or result.get("status") not in _STATUSES:
                return _result("UNKNOWN", trace_sha256, reason="isolated trace worker returned an invalid result")
            return {"operation": "explain_trace_interval", "path": path, "tid": tid,
                    "upid": upid, **result}
    except (OSError, subprocess.SubprocessError) as exc:
        return _result("UNAVAILABLE", trace_sha256, reason=f"cannot start bounded trace worker: {exc}")


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker())

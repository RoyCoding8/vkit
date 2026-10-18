from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

from vkit import proc


def _run(tmp_path: Path, source: str, *, limit: int | None = None, timeout: float = 5) -> proc.Exit:
    return proc.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        timeout_seconds=timeout,
        max_output_bytes=limit,
    )


def test_capped_capture_stops_a_fast_combined_burst(tmp_path):
    result = _run(
        tmp_path,
        "import os; os.write(1, b'O' * 1000000); os.write(2, b'E' * 1000000)",
        limit=4096,
    )

    assert result.output_limited is True
    assert result.timed_out is False
    stdout = (tmp_path / "stdout.log").read_bytes()
    stderr = (tmp_path / "stderr.log").read_bytes()
    assert len(stdout) + len(stderr) == 4096
    assert set(stdout) <= {ord("O")}
    assert set(stderr) <= {ord("E")}


def test_capped_capture_kills_a_live_process_writing_both_streams(tmp_path):
    source = (
        "import os, threading\n"
        "barrier = threading.Barrier(2)\n"
        "def write(fd, value):\n"
        "    while True:\n"
        "        os.write(fd, value * 1024)\n"
        "        barrier.wait()\n"
        "threads = [threading.Thread(target=write, args=(1, b'O')), "
        "threading.Thread(target=write, args=(2, b'E'))]\n"
        "[thread.start() for thread in threads]\n"
        "[thread.join() for thread in threads]\n"
    )
    started = time.monotonic()

    result = _run(tmp_path, source, limit=4096)

    stdout = (tmp_path / "stdout.log").read_bytes()
    stderr = (tmp_path / "stderr.log").read_bytes()
    assert result.output_limited is True
    assert result.code is not None
    assert time.monotonic() - started < 5
    assert len(stdout) + len(stderr) == 4096
    assert stdout or stderr


def test_default_capture_keeps_existing_unbounded_file_path(tmp_path):
    result = _run(tmp_path, "import os; os.write(1, b'out'); os.write(2, b'err')")

    assert result.code == 0
    assert result.output_limited is False
    assert (tmp_path / "stdout.log").read_bytes() == b"out"
    assert (tmp_path / "stderr.log").read_bytes() == b"err"


def test_capped_capture_preserves_timeout_classification(tmp_path):
    result = _run(tmp_path, "import os, time; os.write(1, b'started'); time.sleep(5)", limit=16, timeout=0.2)

    assert result.timed_out is True
    assert result.output_limited is False
    assert (tmp_path / "stdout.log").read_bytes() == b"started"


@pytest.mark.parametrize("limit", [None, 4096])
def test_file_input_preserves_bytes_and_closes_at_eof(tmp_path, limit):
    request = tmp_path / "request"
    request.write_bytes(b"request\x00\xff\n")
    result = proc.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        cwd=tmp_path, stdin_path=request,
        stdout_path=tmp_path / "stdout.log", stderr_path=tmp_path / "stderr.log",
        timeout_seconds=5, max_output_bytes=limit,
    )

    assert result == proc.Exit(0)
    assert (tmp_path / "stdout.log").read_bytes() == b"request\x00\xff\n"
    assert (tmp_path / "stderr.log").read_bytes() == b""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cooperative interrupt")
@pytest.mark.parametrize("ignore_interrupt", [False, True])
def test_graceful_cancel_also_stops_a_parent_that_ignores_interrupt(tmp_path, ignore_interrupt):
    ready = tmp_path / "ready"
    source = (
        "import signal, socket, time\n"
        "from pathlib import Path\n"
        + ("signal.signal(signal.SIGINT, signal.SIG_IGN)\n" if ignore_interrupt else "")
        + "server = socket.socket()\nserver.bind(('127.0.0.1', 0))\nserver.listen()\n"
        + "Path('ready').write_text(str(server.getsockname()[1]))\n"
        + "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = proc.run(
        [sys.executable, "-c", source], cwd=tmp_path,
        stdout_path=tmp_path / "stdout", stderr_path=tmp_path / "stderr",
        timeout_seconds=10, should_cancel=ready.exists, graceful_cancel=True,
    )
    assert result == proc.Exit(None, cancelled=True)
    with socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", int(ready.read_text()))) != 0
    assert time.monotonic() - started < 8
    if not ignore_interrupt:
        assert "KeyboardInterrupt" in (tmp_path / "stderr").read_text()

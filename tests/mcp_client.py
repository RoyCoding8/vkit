"""A JSON-RPC client that speaks the MCP wire format by hand.

Deliberately not the SDK's own client. The point of the protocol suite is to
prove the server answers frames a foreign implementation wrote, so the frames
here are assembled and read as bytes, and a malformed one can be sent on
purpose. A client built from the same SDK as the server would agree with it
about a changed contract; this one cannot.

Nothing in this module imports `vkit` or `mcp`.
"""
from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A version the server is required to accept. 2025-06-18 is in the handshake
#: ladder of the pinned SDK, and picking a mid-range one means a server that
#: only spoke the newest would have to negotiate down rather than pass by luck.
CLIENT_PROTOCOL = "2025-06-18"

REPO_ROOT = Path(__file__).resolve().parents[1]


class ProtocolError(RuntimeError):
    """The server did not answer in a way a client can act on."""


class ServerDied(ProtocolError):
    """The subprocess stopped before answering."""


@dataclass
class Exchange:
    """One frame written and the frame that came back, kept for a transcript."""

    method: str
    sent: str
    received: str | None = None

    def json(self) -> dict[str, Any]:
        if self.received is None:
            raise ProtocolError(f"{self.method} was never answered")
        return json.loads(self.received)


class StdioClient:
    """A connected `vkit mcp serve` subprocess and a running id sequence."""

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)
        self.proc: subprocess.Popen | None = None
        self.exchanges: list[Exchange] = []
        self.stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._stdout: io.TextIOWrapper | None = None
        self._stderr: io.TextIOWrapper | None = None
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._next_id = 0
        self._pending: Exchange | None = None
        #: Set by `kill`, so a fixture can tell "died on its own" from "asked to".
        self.killed = False

    @property
    def stderr_lines(self) -> list[str]:
        """What the server logged. stderr is never protocol traffic."""
        return self._stderr_lines

    # -- lifecycle -----------------------------------------------------------

    def start(self, *, handshake: bool = True) -> "StdioClient":
        """Spawn the shipped command, and complete the handshake by default."""
        env = dict(os.environ)
        # This checkout's own src first, so the subprocess runs the code under
        # test rather than whichever install happens to lead on the path.
        env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")])
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vkit.cli", "mcp", "serve", "--project", str(self.repo)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Binary pipes, and only the reader side is wrapped. In text mode a
            # child writing one protocol frame to a pipe nobody is reading blocks
            # in the child's own line buffering, so a frame can be stuck behind a
            # write that never finishes.
            bufsize=0, env=env, cwd=str(REPO_ROOT),
        )
        self._stdout = io.TextIOWrapper(self.proc.stdout, encoding="utf-8", errors="replace")
        self._stderr = io.TextIOWrapper(self.proc.stderr, encoding="utf-8", errors="replace")

        # One thread per stream, and no other reader anywhere. A second reader
        # racing the first for the same pipe loses frames silently, which shows up
        # as a test that hangs rather than as a failure.
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        if handshake:
            self.initialize()
        return self

    def _pump_stdout(self) -> None:
        assert self._stdout is not None
        for line in self._stdout:
            self.stdout_lines.append(line.rstrip("\r\n"))
            self._queue.put(line)
        self._queue.put(None)

    def _pump_stderr(self) -> None:
        assert self._stderr is not None
        for line in self._stderr:
            self._stderr_lines.append(line.rstrip("\r\n"))

    def __enter__(self) -> "StdioClient":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> int | None:
        """Disconnect and wait. Returns the server's exit code.

        Idempotent, because a test may want to assert the code and still let the
        context manager clean up afterwards. The read end is closed before the
        wait so the server's own end is not held open by this process; on Windows
        a child that would not exit is killed rather than left running, because a
        leaked `mcp serve` still holds the bound project open.
        """
        if self.proc is None:
            return None
        try:
            if self.proc.stdin is not None and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            code = self.proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.killed = True
            self.proc.kill()
            code = self.proc.wait(timeout=120)
        finally:
            for stream in (self._stdout, self._stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
        return code

    def kill(self) -> None:
        """Disconnect hard, as a host that lost the process would."""
        if self.proc is not None:
            self.killed = True
            self.proc.kill()
            self.proc.wait(timeout=120)

    def assert_alive(self) -> None:
        if self.proc is None:
            raise ServerDied("no server process was started")
        if self.proc.poll() is not None:
            raise ServerDied(
                f"the server exited with {self.proc.returncode} when it should have kept "
                f"serving; stderr: {' | '.join(self.stderr_lines[-8:])}"
            )

    # -- the wire ------------------------------------------------------------

    def send_raw(self, text: str) -> None:
        """Write one line to the server's stdin, verbatim and unvalidated."""
        if self.proc is None or self.proc.stdin is None:
            raise ServerDied("no server process is connected")
        self.proc.stdin.write((text + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """A notification, which the protocol says is never answered."""
        self.send_raw(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def recv(self, expect_id: int | None, method: str, timeout: float = 300.0) -> dict[str, Any]:
        """Read one frame, or raise.

        Anything on stdout is protocol traffic, so a line that is not JSON is a
        failure rather than something to skip past: a client that tolerates one
        is a client whose stream a banner can corrupt.
        """
        if self.proc is None:
            raise ServerDied("no server process is connected")
        deadline = time.monotonic() + timeout
        while True:
            self.assert_alive()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(
                    f"no frame within {timeout:g}s for {method}; "
                    f"stderr: {' | '.join(self._stderr_lines[-5:])}"
                )
            try:
                line = self._queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise ServerDied(
                    f"the server closed stdout before answering {method}; "
                    f"stderr: {' | '.join(self._stderr_lines[-5:])}"
                )
            line = line.rstrip("\r\n")
            if not line:
                continue
            if self._pending is not None:
                self._pending.received = line
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProtocolError(
                    "stdout carried a line that is not a protocol frame, which would "
                    f"corrupt the stream for any client: {line[:200]!r} ({exc})"
                ) from exc
            if expect_id is None or frame.get("id") == expect_id:
                return frame

    def request(self, method: str, params: dict[str, Any], timeout: float = 300.0) -> dict[str, Any]:
        """One request, and the frame that answers it."""
        if self.proc is None:
            raise ServerDied("no server process is connected")
        self._next_id += 1
        want = self._next_id
        text = json.dumps({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        exchange = Exchange(method=method, sent=text)
        self.exchanges.append(exchange)
        self._pending = exchange
        self.send_raw(text)
        frame = self.recv(want, method, timeout=timeout)
        self._pending = None
        return frame

    def request_error(
        self, method: str, params: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        """Ask for something outside the contract and require a JSON-RPC error."""
        reply = self.request(method, params, timeout=timeout)
        if "error" not in reply:
            raise ProtocolError(
                f"{method} answered with a result where a protocol error was required: "
                f"{json.dumps(reply)[:300]}"
            )
        return reply["error"]

    # -- MCP -----------------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        reply = self.request("initialize", {
            "protocolVersion": CLIENT_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "vkit-protocol-suite", "version": "1.0.0"},
        })
        self.notify("notifications/initialized", {})
        return reply["result"]

    def handshake(self) -> dict[str, Any]:
        """The `initialize` result, read back from the recorded frame."""
        return self.exchanges[0].json()["result"]

    def list_tools(self) -> list[dict[str, Any]]:
        return self.request("tools/list", {})["result"]["tools"]

    def call(
        self, name: str, arguments: dict[str, Any] | None = None, timeout: float = 300.0
    ) -> dict[str, Any]:
        """A tool call's whole result, `isError` and all."""
        return self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
        )["result"]

    def call_body(
        self, name: str, arguments: dict[str, Any] | None = None, timeout: float = 300.0
    ) -> dict[str, Any]:
        """The JSON the tool's text content carries, which is where a verdict is."""
        return json.loads(self.call(name, arguments, timeout=timeout)["content"][0]["text"])

    def transcript(self) -> str:
        return "\n".join(
            f"{e.method}\n  -> {e.sent}\n  <- {e.received if e.received else '(no reply)'}"
            for e in self.exchanges
        )

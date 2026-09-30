"""Loopback HTTP and routing. No business logic.

The only thing this module decides is which address the socket is opened on, and
that decision is a refusal rather than a warning. A loopback server with no
authentication is only safe while it genuinely cannot be reached from another
machine, and a warning leaves the operator to notice it.

`HTTPServer.__init__` opens and binds the socket, so the check runs in
`bind_and_activate` and the socket is never bound to a routable address. It is
not a keyword argument and not a hook called after binding; by the time
`bind_and_activate` returns False, `socket.bind` was never reached.

Binding is not authentication, and stating that is the reason this module
exists. The loopback bind says no other machine can reach this socket. It does
not say who is talking to it, and a page the operator has open in a browser on
this machine is talking to it. Who the caller is, and whether they may change
anything, is decided once in `api.admit`, before a route is chosen. This module
hands `admit` what arrived, writes back what it said, and returns from the
request method the moment it refuses.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import api
from .api import Request, Session
from .operations import Context
from .plan import LOOPBACK_HOST

STATIC_DIR = Path(__file__).resolve().parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class BindRefused(Exception):
    """A non-loopback address was asked for, and refused at the socket."""


class ConsoleServer(ThreadingHTTPServer):
    """A threading HTTP server that will only be loopback.

    `allow_reuse_address` is left at its default so a second console on the same
    port fails loudly instead of silently taking the address.
    """

    daemon_threads = True

    def __init__(self, address: tuple[str, int], context: Context) -> None:
        self.context = context
        super().__init__(address, ConsoleHandler)
        # Minted after the bind, because the token is bound to the port the
        # console actually answers on. A port asked for as 0 is the OS's to
        # choose, and `server_address` is the only place that choice is visible.
        self.session = Session.mint(port=self.server_address[1])

    def bind_and_activate(self) -> bool:
        host = self.server_address[0]
        if host != LOOPBACK_HOST:
            raise BindRefused(
                f"refusing to bind {host!r}: this console serves 127.0.0.1 only. It has "
                f"no authentication, so it is safe only while no other machine can reach it."
            )
        return super().bind_and_activate()


class ConsoleHandler(BaseHTTPRequestHandler):
    """Serve the page, and route `/api/<name>` through one gate.

    The gate is `api.admit`. It raises for every refusal there is, so the two
    `except` clauses below are the whole of this module's policy and no code
    after them is reachable by a request the gate turned away.
    """

    server_version = "vkit-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - see do_GET
        self._handle("POST")

    # ------------------------------------------------------------- plumbing

    def _handle(self, method: str) -> None:
        from urllib.parse import parse_qsl, urlsplit

        parts = urlsplit(self.path)
        request = Request(
            method=method,
            route=parts.path[len("/api/"):] if parts.path.startswith("/api/") else None,
            query=dict(parse_qsl(parts.query, keep_blank_values=False)),
            host=self.headers.get("Host", ""),
            origin=self.headers.get("Origin"),
            token=self.headers.get(api.TOKEN_HEADER),
            length=self.headers.get("Content-Length"),
            chunked="chunked" in (self.headers.get("Transfer-Encoding") or "").lower(),
        )
        try:
            query = api.admit(request, self.server.session, self._body)  # type: ignore[attr-defined]
        except (api.Rejected, api.BadRequest) as refused:
            self._refuse(refused)
            return
        if query is None:
            self._serve_static(parts.path)
            return
        self._run(request.route, query)

    def _body(self, length: int) -> bytes:
        """The declared body. `admit` calls this only after bounding the length."""
        return self.rfile.read(length) if length else b""

    def _run(self, route: str, query: Any) -> None:
        context: Context = self.server.context  # type: ignore[attr-defined]
        try:
            payload = api.dispatch(context, route, query)
        except Exception as exc:  # noqa: BLE001 - one response shape for every failure
            status, document = api.error_of(exc)
            self._json(status, document)
            return
        self._json(200, payload)

    def _refuse(self, refused: Exception) -> None:
        """Answer a request the gate turned away, and end the connection.

        A refused request had no body read, and the peer's bytes are still in
        flight. Answering as though the exchange finished would leave the next
        bytes on this socket parsed as the next request, which is how one
        oversized POST turned into a second response. Closing instead gives the
        peer the one response and no chance to send another request on a body
        this server never accepted.
        """
        self.close_connection = True
        self._json(*api.error_of(refused))

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        # A static server that will read any file the process can read is a file
        # server. Containment is checked after resolution because a prefix test
        # loses to `..`.
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self._json(404, {"error": f"no such path: {path}"})
            return
        body = target.read_bytes()
        if target.suffix == ".html":
            body = body.replace(
                b"__VKIT_TOKEN__", self.server.session.token.encode("ascii")  # type: ignore[attr-defined]
            )
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, document: Any) -> None:
        body = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """One line per request on stderr, and never a 500 stack trace to a browser.

        The console is a local tool and its operator is the one reading stderr,
        so the standard access log is kept. A traceback is not written to the
        response body: the browser gets the message and the detail stays here.
        """
        import sys

        sys.stderr.write("console %s - %s\n" % (self.address_string(), fmt % args))


def serve(context: Context, port: int = 8765, host: str = LOOPBACK_HOST) -> ConsoleServer:
    """Bind and return a server. Nothing is served until the caller serves it.

    Raises BindRefused for any host but 127.0.0.1, before the socket exists.
    """
    if host != LOOPBACK_HOST:
        raise BindRefused(
            f"refusing to bind {host!r}: this console serves 127.0.0.1 only. It has no "
            f"authentication, so it is safe only while no other machine can reach it."
        )
    return ConsoleServer((host, port), context)


def start_in_thread(context: Context, port: int = 0) -> tuple[ConsoleServer, threading.Thread]:
    """Bind on `port` (0 asks the OS for a free one) and serve on a daemon thread.

    Port 0 is what a test uses, so a suite cannot collide with a console the
    operator is already running.
    """
    server = serve(context, port=port)
    thread = threading.Thread(target=server.serve_forever, name="vkit-console", daemon=True)
    thread.start()
    return server, thread

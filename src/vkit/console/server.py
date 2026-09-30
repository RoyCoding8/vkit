"""Loopback HTTP and routing. No business logic.

The only thing this module decides is which address the socket is opened on, and
that decision is a refusal rather than a warning. A loopback server with no
authentication is only safe while it genuinely cannot be reached from another
machine, and a warning leaves the operator to notice it.

`HTTPServer.__init__` opens and binds the socket, so the check runs in
`bind_and_activate` and the socket is never bound to a routable address. It is
not a keyword argument and not a hook called after binding; by the time
`bind_and_activate` returns False, `socket.bind` was never reached.

There is no authentication because there is no remote peer. Do not describe this
as a network service.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import api, operations
from .operations import Context
from .plan import LOOPBACK_HOST

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Bodies are not needed by any route, so a body above this is refused rather
#: than read. Every argument travels in the query string, which keeps a refused
#: mutation reproducible from a URL.
MAX_BODY_BYTES = 64 * 1024

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

    def bind_and_activate(self) -> bool:
        host = self.server_address[0]
        if host != LOOPBACK_HOST:
            raise BindRefused(
                f"refusing to bind {host!r}: this console serves 127.0.0.1 only. It has "
                f"no authentication, so it is safe only while no other machine can reach it."
            )
        return super().bind_and_activate()


class ConsoleHandler(BaseHTTPRequestHandler):
    """Serve the static page, and route `/api/<name>` to one call.

    Reads a route, calls `api.dispatch`, writes the result. It makes no decision
    about what is permitted: `api.ROUTES` is the whole surface, and a path this
    handler does not recognise is a 404 rather than a fallback.
    """

    server_version = "vkit-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        route, params, query = self._split(self.path)
        if route is None:
            self._serve_static()
            return
        self._answer(route, params, query)

    def do_POST(self) -> None:  # noqa: N802 - see do_GET
        route, params, query = self._split(self.path)
        if route is None:
            self._fail(404, {"error": f"no such path: {self.path}"})
            return
        self._answer(route, params, query, body=self._read_body())

    # ------------------------------------------------------------- plumbing

    def _split(self, target: str) -> tuple[str | None, dict[str, str], dict[str, str]]:
        from urllib.parse import urlsplit

        parts = urlsplit(target)
        if not parts.path.startswith("/api/"):
            return None, {}, {}
        route = parts.path[len("/api/"):]
        query = api.parse_query(parts.query)
        return route, {"route": route}, query

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._fail(413, {"error": f"body above {MAX_BODY_BYTES} bytes; send arguments in the query string"})
            return {}
        return api.parse_body(self.rfile.read(length) if length else b"")

    def _answer(
        self, route: str, params: dict[str, str], query: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> None:
        context: Context = self.server.context  # type: ignore[attr-defined]
        try:
            if body:
                query = {**query, **{k: v for k, v in body.items() if isinstance(v, str)}}
            payload = api.dispatch(context, route, query)
        except Exception as exc:  # noqa: BLE001 - one response shape for every failure
            status, document = api.error_of(exc)
            self._fail(status, document)
            return
        self._json(200, payload)

    def _serve_static(self) -> None:
        from urllib.parse import urlsplit

        path = urlsplit(self.path).path
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        # A static server that will read any file the process can read is a file
        # server. Containment is checked after resolution because a prefix test
        # loses to `..`.
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self._fail(404, {"error": f"no such path: {self.path}"})
            return
        content_type = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
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

    def _fail(self, status: int, document: Any) -> None:
        self._json(status, document)

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

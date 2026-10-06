"""A read-only local web view of the same answers the CLI and MCP tools give."""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import query
from .paths import Project


def _page() -> bytes:
    return resources.files("vkit").joinpath("console.html").read_bytes()


def _handler(project: Project, allowed_hosts: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                                                        "script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(value).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._json({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
                return
            url = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    self._send(HTTPStatus.OK, _page(), "text/html; charset=utf-8")
                elif url.path == "/api/status":
                    self._json(query.status(query.open_context(project.root)))
                elif url.path == "/api/runs":
                    self._json(query.runs(query.open_context(project.root), int(params.get("limit", 30))))
                elif url.path == "/api/run":
                    view = query.run_view(query.open_context(project.root), params.get("id", ""),
                                          log=params.get("log", "stdout"), offset=int(params.get("offset", 0)))
                    self._json(view if view is not None else {"error": "no such run"},
                               HTTPStatus.OK if view is not None else HTTPStatus.NOT_FOUND)
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    return Handler


def bind(project: Project, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(project, set()))
    bound = server.server_address[1]
    server.RequestHandlerClass = _handler(project, {f"127.0.0.1:{bound}", f"localhost:{bound}"})
    return server


def serve(project: Project, *, port: int, open_browser: bool) -> int:
    server = bind(project, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(url, flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("console stopped", file=sys.stderr)
    finally:
        server.server_close()
    return 0

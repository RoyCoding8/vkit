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

from . import __version__, query
from .paths import Project


_ASSETS = {
    "/": ("console.html", "text/html; charset=utf-8"),
    "/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
    "/assets/material.js": ("console-assets/material.js", "text/javascript; charset=utf-8"),
    "/assets/theme.css": ("console-assets/theme.css", "text/css; charset=utf-8"),
    "/assets/icons.svg": ("console-assets/icons.svg", "image/svg+xml"),
    "/assets/logo.svg": ("console-assets/logo.svg", "image/svg+xml"),
    "/assets/roboto.woff2": ("console-assets/roboto.woff2", "font/woff2"),
}


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
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                        "script-src 'self'; object-src 'none'; base-uri 'none'")
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
                if url.path in _ASSETS:
                    name, content_type = _ASSETS[url.path]
                    self._send(HTTPStatus.OK, resources.files("vkit").joinpath(name).read_bytes(), content_type)
                elif url.path == "/api/status":
                    self._json(query.status(query.open_context(project.root)))
                elif url.path == "/api/runs":
                    self._json(query.runs(query.open_context(project.root), int(params.get("limit", 30))))
                elif url.path == "/api/run":
                    view = query.run_view(query.open_context(project.root), params.get("id", ""),
                                          log=params.get("log", "stdout"), offset=int(params.get("offset", 0)))
                    self._json(view if view is not None else {"error": "no such run"},
                               HTTPStatus.OK if view is not None else HTTPStatus.NOT_FOUND)
                elif url.path == "/api/config":
                    ctx = query.open_context(project.root)
                    self._json({"version": __version__, "doctor": query.doctor(ctx),
                                "description": ctx.manifest.description if ctx.manifest else "",
                                "definitions": ctx.manifest.entries if ctx.manifest else {}})
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

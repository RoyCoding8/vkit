"""The local web console for one repository."""
from __future__ import annotations

import json
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, query
from .console_settings import (DEFAULT_SETTINGS, SETTINGS_SCHEMA, ConsoleSettingsFileError, load_settings,
                               parse_settings, save_settings, settings_path)
from .mcp import BY_NAME, Server, tool_definitions
from .operations.catalog import OPERATIONS
from .paths import Project

_MAX_BODY_BYTES = 1_048_576
_INVALID_BODY = object()
_CONSOLE_TOOLS = (*OPERATIONS, BY_NAME["check_run"], BY_NAME["run_cancel"])
_CALLABLE_TOOLS = frozenset(tool["name"] for tool in tool_definitions(_CONSOLE_TOOLS))
_OPERATION_DESCRIPTORS = [
    {"name": tool["name"], "description": tool["description"], "input_schema": tool["inputSchema"],
     "read_only": tool["annotations"]["readOnlyHint"]}
    for tool in tool_definitions(OPERATIONS)
]


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
    allowed_hosts = frozenset(allowed_hosts)
    csrf_token = secrets.token_urlsafe(32)
    dispatcher = Server(project.root)

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
                                                        "script-src 'self'; object-src 'none'; base-uri 'none'; "
                                                        "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(value).encode("utf-8"), "application/json")

        def _host_allowed(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _post_authorized(self) -> bool:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            token = self.headers.get("X-Vkit-Token", "")
            return (host in allowed_hosts and origin == f"http://{host}" and
                    secrets.compare_digest(token.encode("utf-8"), csrf_token.encode("ascii")))

        def _post_json(self) -> Any:
            if self.headers.get_content_type() != "application/json":
                self._json({"error": "Content-Type must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return _INVALID_BODY
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                self._json({"error": "Content-Length is required"}, HTTPStatus.LENGTH_REQUIRED)
                return _INVALID_BODY
            try:
                size = int(content_length)
            except ValueError:
                self._json({"error": "invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
                return _INVALID_BODY
            if size < 0:
                self._json({"error": "invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
                return _INVALID_BODY
            if size > _MAX_BODY_BYTES:
                self._json({"error": "request body is too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return _INVALID_BODY
            if size == 0:
                self._json({"error": "request body is required"}, HTTPStatus.BAD_REQUEST)
                return _INVALID_BODY
            raw = self.rfile.read(size)
            if len(raw) != size:
                self._json({"error": "incomplete request body"}, HTTPStatus.BAD_REQUEST)
                return _INVALID_BODY
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json({"error": f"invalid JSON body: {exc}"}, HTTPStatus.BAD_REQUEST)
                return _INVALID_BODY

        def do_GET(self) -> None:
            if not self._host_allowed():
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
                    settings = load_settings(project)
                    self._json({"version": __version__, "doctor": query.doctor(ctx),
                                "description": ctx.manifest.description if ctx.manifest else "",
                                "definitions": ctx.manifest.entries if ctx.manifest else {},
                                "settings": settings.as_json(), "settings_path": str(settings_path(project)),
                                "settings_defaults": DEFAULT_SETTINGS.as_json(),
                                "settings_schema": SETTINGS_SCHEMA, "csrf_token": csrf_token,
                                "operations": _OPERATION_DESCRIPTORS})
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except ConsoleSettingsFileError as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            if not self._host_allowed():
                self._json({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
                return
            if not self._post_authorized():
                self._json({"error": "same-origin request with a valid console token required"},
                           HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            if path not in ("/api/settings", "/api/call"):
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            payload = self._post_json()
            if payload is _INVALID_BODY:
                return
            if path == "/api/settings":
                try:
                    settings = parse_settings(payload)
                    save_settings(project, settings)
                except ValueError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                except OSError as exc:
                    self._json({"error": f"cannot save console settings: {exc}"},
                               HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._json({"settings": settings.as_json(), "settings_path": str(settings_path(project))})
                return

            if not isinstance(payload, dict) or set(payload) != {"name", "arguments"}:
                self._json({"error": "request must contain exactly name and arguments"}, HTTPStatus.BAD_REQUEST)
                return
            name, arguments = payload["name"], payload["arguments"]
            if not isinstance(name, str) or name not in _CALLABLE_TOOLS:
                self._json({"error": "unknown console operation"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(arguments, dict):
                self._json({"error": "arguments must be an object"}, HTTPStatus.BAD_REQUEST)
                return
            result, is_error = dispatcher.call(name, arguments)
            self._json({"result": result, "is_error": is_error})

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

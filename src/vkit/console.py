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
from socketserver import TCPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, query
from .settings import (DEFAULT_SETTINGS, SETTINGS_SCHEMA, SettingsFileError, load_settings,
                       parse_settings, save_settings, settings_path, launcher_settings_path,
                       load_launcher_settings, parse_launcher_settings, save_launcher_settings)
from .mcp import BY_NAME, Server
from .categories import CATEGORIES
from .paths import Project

_MAX_BODY_BYTES = 1_048_576
_INVALID_BODY = object()


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

        def _discard_request_body(self) -> bool:
            if self.headers.get("Transfer-Encoding"):
                return False
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return True
            try:
                remaining = int(raw_length)
            except ValueError:
                return False
            if remaining < 0 or remaining > _MAX_BODY_BYTES:
                return False
            timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(1.0)
                while remaining:
                    chunk = self.rfile.read(min(remaining, 64 * 1024))
                    if not chunk:
                        return False
                    remaining -= len(chunk)
                return True
            except (TimeoutError, OSError):
                return False
            finally:
                self.connection.settimeout(timeout)

        def _reject_post(self, error: str, status: HTTPStatus) -> None:
            if not self._discard_request_body():
                self.close_connection = True
            self._json({"error": error}, status)

        def _post_json(self) -> Any:
            if self.headers.get_content_type() != "application/json":
                self._reject_post("Content-Type must be application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return _INVALID_BODY
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                self._reject_post("Content-Length is required", HTTPStatus.LENGTH_REQUIRED)
                return _INVALID_BODY
            try:
                size = int(content_length)
            except ValueError:
                self._reject_post("invalid Content-Length", HTTPStatus.BAD_REQUEST)
                return _INVALID_BODY
            if size < 0:
                self._reject_post("invalid Content-Length", HTTPStatus.BAD_REQUEST)
                return _INVALID_BODY
            if size > _MAX_BODY_BYTES:
                self._reject_post("request body is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return _INVALID_BODY
            if size == 0:
                self._reject_post("request body is required", HTTPStatus.BAD_REQUEST)
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
                                "categories": [{"id": key, "label": label} for key, label in CATEGORIES.items()],
                                "launcher_settings": load_launcher_settings().as_json(),
                                "launcher_settings_path": str(launcher_settings_path()),
                                "mcp_config": {"mcpServers": {"vkit": {
                                    "command": sys.executable,
                                    "args": ["-m", "vkit.cli", "mcp", "serve", "--project", str(project.root),
                                             "--exposure", settings.mcp_exposure, "--categories",
                                             *(["all"] if settings.mcp_categories is None else settings.mcp_categories)],
                                }}},
                                "managed_tools": [{"name": tool.name, "description": tool.description,
                                                   "category": tool.category}
                                                  for tool in BY_NAME.values() if tool.name != "help"]})
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except SettingsFileError as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            if not self._host_allowed():
                self._reject_post("unexpected Host header", HTTPStatus.FORBIDDEN)
                return
            if not self._post_authorized():
                self._reject_post("same-origin request with a valid console token required",
                                  HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            if path not in ("/api/settings", "/api/launcher-settings", "/api/call"):
                self._reject_post("not found", HTTPStatus.NOT_FOUND)
                return
            payload = self._post_json()
            if payload is _INVALID_BODY:
                return
            if path in ("/api/settings", "/api/launcher-settings"):
                launcher = path == "/api/launcher-settings"
                try:
                    if launcher:
                        settings = parse_launcher_settings(payload)
                        save_launcher_settings(settings)
                    else:
                        settings = parse_settings(payload)
                        save_settings(project, settings)
                except ValueError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                except OSError as exc:
                    self._json({"error": f"cannot save settings: {exc}"},
                               HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._json({"settings": settings.as_json(),
                            "settings_path": str(launcher_settings_path() if launcher else settings_path(project))})
                return

            if not isinstance(payload, dict) or set(payload) != {"name", "arguments"}:
                self._json({"error": "request must contain exactly name and arguments"}, HTTPStatus.BAD_REQUEST)
                return
            name, arguments = payload["name"], payload["arguments"]
            if not isinstance(name, str) or name not in BY_NAME:
                self._json({"error": "unknown console tool"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(arguments, dict):
                self._json({"error": "arguments must be an object"}, HTTPStatus.BAD_REQUEST)
                return
            result, is_error = dispatcher.call(name, arguments)
            self._json({"result": result, "is_error": is_error})

    return Handler


class _LoopbackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def bind(project: Project, port: int) -> ThreadingHTTPServer:
    server = _LoopbackServer(("127.0.0.1", port), _handler(project, set()))
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

"""Find and freshly replay stateful API counterexamples against a loopback service."""

from __future__ import annotations

import contextlib
import hashlib
import io
import ipaddress
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ..proc import run as run_process
from . import openapi

SCHEMATHESIS_VERSION = "4.30.0"
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 300
MAX_EXAMPLES = 100
MAX_STEPS = 12
MAX_OPENAPI_FILES = 128
MAX_OPENAPI_TOTAL_BYTES = 2_097_152
MAX_REPORT_BYTES = 262_144
MAX_BODY_BYTES = 1_048_576
MAX_TOTAL_REQUESTS = 2_000
MAX_STEP_BYTES = 32_768
REQUEST_TIMEOUT_SECONDS = 3
_BASE_ENV = "VKIT_API_SEQUENCE_BASE_URL"
_RESET_ENV = "VKIT_API_SEQUENCE_RESET_URL"
_MODEL_SCOPE = (
    "Schemathesis 4.30.0 bounded stateful OpenAPI checks against one operator-configured "
    "loopback HTTP service, with exact fresh replay of a detected counterexample; a clean "
    "sampled campaign is not proof of universal API correctness."
)


class _Unsupported(ValueError):
    pass


class _EngineUnavailable(RuntimeError):
    pass


class _WorkerFailure(BaseException):
    pass


class _ServiceFailure(BaseException):
    pass


class _LimitExceeded(BaseException):
    pass


@dataclass(frozen=True)
class _Endpoint:
    url: str
    origin: tuple[str, str, int]


@dataclass(frozen=True)
class _Source:
    path: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class _Request:
    root: Path
    schema: str
    sources: tuple[_Source, ...]
    base: _Endpoint
    reset: _Endpoint
    max_examples: int
    max_steps: int
    timeout_seconds: int
    deadline: float


def _endpoint(raw: str | None, name: str) -> _Endpoint:
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise _Unsupported(f"{name} must be a configured loopback HTTP URL")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise _Unsupported(f"{name} is not a valid loopback HTTP URL") from None
    if (
        parsed.scheme != "http"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.path
        or "\x00" in raw
        or "%" in parsed.netloc
    ):
        raise _Unsupported(
            f"{name} must be a plain loopback HTTP URL without credentials, query, or fragment"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise _Unsupported(f"{name} must use a literal loopback IP address") from None
    if not address.is_loopback:
        raise _Unsupported(f"{name} must use a literal loopback IP address")
    effective_port = 80 if port is None else port
    if not 1 <= effective_port <= 65535:
        raise _Unsupported(f"{name} must use a valid TCP port")
    if parsed.path and not parsed.path.startswith("/"):
        raise _Unsupported(f"{name} has an invalid URL path")
    normalized_host = address.compressed.lower()
    normalized_url = (
        f"http://[{normalized_host}]:{effective_port}{parsed.path or '/'}"
        if address.version == 6
        else f"http://{normalized_host}:{effective_port}{parsed.path or '/'}"
    )
    return _Endpoint(normalized_url, ("http", normalized_host, effective_port))


def _validate_schema(document: Any, *, entry: bool) -> None:
    if not isinstance(document, dict):
        raise _Unsupported("OpenAPI documents must be objects")
    operation_ids: list[str] = []
    links: list[str] = []
    stack = [document]
    visited: set[int] = set()
    while stack:
        value = stack.pop()
        if not isinstance(value, (dict, list)):
            continue
        identity = id(value)
        if identity in visited:
            continue
        if isinstance(value, dict):
            servers = value.get("servers")
            singular_server = value.get("server")
            has_server_declaration = (
                isinstance(servers, list)
                and any(
                    isinstance(server, dict) and isinstance(server.get("url"), str)
                    for server in servers
                )
            ) or (
                isinstance(singular_server, dict)
                and isinstance(singular_server.get("url"), str)
            )
            if has_server_declaration:
                raise _Unsupported(
                    "OpenAPI server declarations are unsupported; the configured loopback URL owns routing"
                )
            if "callbacks" in value and "responses" in value:
                raise _Unsupported("OpenAPI callbacks are unsupported")
            if value.get("in") in ("header", "cookie") and "name" in value:
                raise _Unsupported(
                    "OpenAPI header and cookie parameters are unsupported"
                )
            if value.get("security") and ("openapi" in value or "responses" in value):
                raise _Unsupported("OpenAPI security requirements are unsupported")
            if "openapi" in value and "webhooks" in value:
                raise _Unsupported("OpenAPI webhooks are unsupported")
            if "links" in value:
                link_objects = value["links"]
                if not isinstance(link_objects, dict):
                    link_objects = {}
                for link in link_objects.values():
                    if not isinstance(link, dict):
                        continue
                    if "operationRef" in link or "server" in link:
                        raise _Unsupported(
                            "external operation and server links are unsupported"
                        )
                    operation_id = link.get("operationId")
                    if operation_id is not None:
                        if not isinstance(operation_id, str) or not operation_id:
                            raise _Unsupported(
                                "stateful links must use a non-empty local operationId"
                            )
                        links.append(operation_id)
            examples = value.get("examples")
            if isinstance(examples, dict) and any(
                isinstance(example, dict) and "externalValue" in example
                for example in examples.values()
            ):
                raise _Unsupported("external examples are unsupported")
            stack.extend(value.values())
        else:
            stack.extend(value)
        visited.add(identity)
    if not entry:
        return
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise _Unsupported("the OpenAPI entry must declare a paths object")
    for path in paths:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or any(ord(char) < 0x20 for char in path)
        ):
            raise _Unsupported("OpenAPI paths must be local absolute paths")
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if not isinstance(method, str):
                continue
            if method.lower() not in {
                "get",
                "put",
                "post",
                "delete",
                "options",
                "head",
                "patch",
                "trace",
            }:
                continue
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if operation_id is not None:
                if not isinstance(operation_id, str) or not operation_id:
                    raise _Unsupported(
                        "OpenAPI operationId values must be non-empty strings"
                    )
                operation_ids.append(operation_id)
    if len(operation_ids) != len(set(operation_ids)):
        raise _Unsupported(
            "OpenAPI operationId values must be unique for stateful links"
        )
    if any(target not in set(operation_ids) for target in links):
        raise _Unsupported("every stateful link must name a local operationId")
    components = document.get("components")
    if isinstance(components, dict) and components.get("securitySchemes"):
        raise _Unsupported("OpenAPI security schemes are unsupported")


def _capture(
    root: Path, schema_path: str, deadline: float
) -> tuple[str, tuple[_Source, ...]]:
    try:
        _entry_path, entry = openapi._source_file(root, schema_path)
        pending = [entry]
        sources: dict[str, _Source] = {}
        total = 0
        while pending:
            openapi._check_deadline(deadline)
            relative = pending.pop()
            if relative in sources:
                continue
            if len(relative) > 512:
                raise _Unsupported(
                    "OpenAPI source paths exceed the 512 character report limit"
                )
            path = openapi._checked_relative_file(root, relative)
            raw = openapi._read_source(path, deadline)
            total += len(raw)
            if total > MAX_OPENAPI_TOTAL_BYTES:
                raise _Unsupported(
                    "OpenAPI reference closure exceeds the 2 MiB total byte limit"
                )
            if len(sources) >= MAX_OPENAPI_FILES:
                raise _Unsupported(
                    "OpenAPI reference closure exceeds the 128 file limit"
                )
            document = openapi._parse_document(relative, raw, relative == entry)
            _validate_schema(document, entry=relative == entry)
            sources[relative] = _Source(relative, raw, hashlib.sha256(raw).hexdigest())
            for reference in openapi._references(document, deadline):
                target = openapi._reference_target(root, relative, reference)
                if target is not None and target not in sources:
                    pending.append(target)
    except openapi._UnsupportedDocument as exc:
        raise _Unsupported(str(exc)) from None
    except openapi._ParserUnavailable as exc:
        raise _EngineUnavailable(str(exc)) from None
    if entry not in sources:
        raise _Unsupported("the OpenAPI entry file could not be captured")
    return entry, tuple(sources[path] for path in sorted(sources))


def _assert_fresh(request: _Request) -> None:
    if (
        _endpoint(os.environ.get(_BASE_ENV), _BASE_ENV) != request.base
        or _endpoint(os.environ.get(_RESET_ENV), _RESET_ENV) != request.reset
    ):
        raise _WorkerFailure(
            "operator API service configuration changed during the campaign"
        )
    for source in request.sources:
        try:
            current = openapi._checked_relative_file(request.root, source.path)
            raw = openapi._read_source(current, request.deadline)
        except (
            OSError,
            openapi._UnsupportedDocument,
            openapi._DeadlineExceeded,
        ) as exc:
            raise _WorkerFailure(
                "an OpenAPI source changed or became unreadable during the campaign"
            ) from exc
        if hashlib.sha256(raw).hexdigest() != source.sha256:
            raise _WorkerFailure("an OpenAPI source changed during the campaign")


def _validate_engine() -> None:
    try:
        import hypothesis
        import schemathesis
    except ImportError as exc:
        raise _EngineUnavailable(
            "the pinned Schemathesis engine is not installed"
        ) from exc
    if schemathesis.__version__ != SCHEMATHESIS_VERSION:
        raise _EngineUnavailable(f"Schemathesis {SCHEMATHESIS_VERSION} is required")
    if not getattr(hypothesis, "__version__", None):
        raise _EngineUnavailable("Hypothesis version could not be identified")


def _environment(work: Path, base: _Endpoint, reset: _Endpoint) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(work),
            "USERPROFILE": str(work),
            "XDG_CONFIG_HOME": str(work / "config"),
            _BASE_ENV: base.url,
            _RESET_ENV: reset.url,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONWARNINGS": "ignore",
        }
    )
    return env


def _write_snapshot(sources: tuple[_Source, ...], entry: str, destination: Path) -> str:
    for source in sources:
        path = destination.joinpath(*PurePosixPath(source.path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.content)
    return str(destination.joinpath(*PurePosixPath(entry).parts))


def _read_bounded(path: Path) -> bytes:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > MAX_REPORT_BYTES
        ):
            raise _WorkerFailure(
                "the API sequence worker report is missing or exceeds 256 KiB"
            )
        with path.open("rb") as stream:
            data = stream.read(MAX_REPORT_BYTES + 1)
    except OSError as exc:
        raise _WorkerFailure(
            "the API sequence worker did not produce a report"
        ) from exc
    if len(data) > MAX_REPORT_BYTES:
        raise _WorkerFailure("the API sequence worker report exceeds 256 KiB")
    return data


def _prepare(
    root: Path,
    schema_path: Any,
    max_examples: Any,
    max_steps: Any,
    timeout_seconds: Any,
) -> _Request:
    if type(max_examples) is not int or not 1 <= max_examples <= MAX_EXAMPLES:
        raise ValueError(f"max_examples must be an integer in [1, {MAX_EXAMPLES}]")
    if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
        raise ValueError(f"max_steps must be an integer in [1, {MAX_STEPS}]")
    if (
        type(timeout_seconds) is not int
        or not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"timeout_seconds must be an integer in [{MIN_TIMEOUT_SECONDS}, {MAX_TIMEOUT_SECONDS}]"
        )
    try:
        project_root = root.resolve(strict=True)
    except OSError:
        raise _Unsupported("project root must be a readable directory") from None
    if not project_root.is_dir():
        raise _Unsupported("project root must be a readable directory")
    base = _endpoint(os.environ.get(_BASE_ENV), _BASE_ENV)
    reset = _endpoint(os.environ.get(_RESET_ENV), _RESET_ENV)
    if base.origin != reset.origin:
        raise _Unsupported("API and reset URLs must have the same loopback origin")
    deadline = time.monotonic() + timeout_seconds
    if not isinstance(schema_path, str) or len(schema_path) > 1024:
        raise _Unsupported("schema_path must name a repository-relative OpenAPI file")
    schema, sources = _capture(project_root, schema_path, deadline)
    return _Request(
        project_root,
        schema,
        sources,
        base,
        reset,
        max_examples,
        max_steps,
        timeout_seconds,
        deadline,
    )


def _worker_payload(request: _Request, schema_file: str) -> dict[str, Any]:
    return {
        "schema": schema_file,
        "base_url": request.base.url,
        "reset_url": request.reset.url,
        "max_examples": request.max_examples,
        "max_steps": request.max_steps,
        "timeout_seconds": request.timeout_seconds,
    }


def _run_worker(request: _Request) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="vkit-api-sequences-") as raw:
        workspace = Path(raw)
        snapshot = workspace / "schema"
        snapshot.mkdir()
        schema_file = _write_snapshot(request.sources, request.schema, snapshot)
        payload_path = workspace / "request.json"
        payload_path.write_text(
            json.dumps(_worker_payload(request, schema_file)), encoding="utf-8"
        )
        stdout_path = workspace / "worker.json"
        stderr_path = workspace / "worker.log"
        remaining = request.deadline - time.monotonic()
        if remaining <= 0:
            raise _WorkerFailure("API sequence campaign exceeded its wall deadline")
        env = _environment(workspace, request.base, request.reset)
        argv = [
            sys.executable,
            "-I",
            "-m",
            "vkit.operations.api_sequences",
            "--worker",
            str(payload_path),
        ]
        execution = run_process(
            argv,
            cwd=workspace,
            env=env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=remaining,
        )
        if execution.launch_error:
            raise _EngineUnavailable("the bounded API sequence worker could not start")
        if execution.cancelled:
            raise _WorkerFailure("API sequence campaign was cancelled")
        if execution.timed_out:
            raise _WorkerFailure("API sequence campaign exceeded its wall deadline")
        if execution.code != 0:
            raise _WorkerFailure("API sequence worker exited without a trusted result")
        raw_report = _read_bounded(stdout_path)
        try:
            result = json.loads(raw_report.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _WorkerFailure(
                "API sequence worker returned malformed JSON"
            ) from None
        if not isinstance(result, dict) or result.get("status") not in {
            "BUG_REPRODUCED",
            "NO_VIOLATION",
            "INCONCLUSIVE",
            "UNSUPPORTED",
            "UNAVAILABLE",
        }:
            raise _WorkerFailure("API sequence worker returned an unrecognized result")
        return result


def check_api_sequences(
    root: Path,
    schema_path: str,
    *,
    max_examples: int = 20,
    max_steps: int = 6,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Run a bounded stateful campaign and fresh-replay any detected API failure."""
    result: dict[str, Any] = {
        "operation": "check_api_sequences",
        "status": "UNSUPPORTED",
        "schema_path": schema_path[:1024] if isinstance(schema_path, str) else "",
        "engine": {"name": "Schemathesis", "version": SCHEMATHESIS_VERSION},
        "campaign": {
            "max_examples": max_examples if type(max_examples) is int else None,
            "max_steps": max_steps if type(max_steps) is int else None,
            "timeout_seconds": timeout_seconds
            if type(timeout_seconds) is int
            else None,
        },
        "scope": _MODEL_SCOPE,
        "counterexample": None,
        "schema_files": [],
        "detail": None,
    }
    try:
        request = _prepare(root, schema_path, max_examples, max_steps, timeout_seconds)
        _validate_engine()
    except _EngineUnavailable as exc:
        result.update({"status": "UNAVAILABLE", "detail": str(exc)})
        return result
    except (OSError, ValueError, TypeError, openapi._DeadlineExceeded) as exc:
        result["detail"] = str(exc)
        return result
    result["schema_files"] = [
        {"path": source.path, "bytes": len(source.content), "sha256": source.sha256}
        for source in request.sources
    ]
    result["service_origin"] = (
        f"{request.base.origin[0]}://{request.base.origin[1]}:{request.base.origin[2]}"
    )
    try:
        _assert_fresh(request)
        worker = _run_worker(request)
        _assert_fresh(request)
        result.update(worker)
        result["operation"] = "check_api_sequences"
        result["schema_path"] = request.schema
        if isinstance(result.get("campaign"), dict):
            result["campaign"].setdefault("timeout_seconds", request.timeout_seconds)
        result["engine"] = {"name": "Schemathesis", "version": SCHEMATHESIS_VERSION}
        result["scope"] = _MODEL_SCOPE
        encoded = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_REPORT_BYTES:
            result.update(
                {
                    "status": "INCONCLUSIVE",
                    "counterexample": None,
                    "schema_files": [],
                    "detail": "API sequence report exceeds 256 KiB",
                }
            )
        return result
    except (_WorkerFailure, OSError, ValueError, TypeError) as exc:
        result.update({"status": "INCONCLUSIVE", "detail": str(exc)})
        return result


def _worker_session_class(
    origin: tuple[str, str, int], max_bytes: int, request_budget: dict[str, int]
):
    import requests

    class BoundedSession(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            self.trust_env = False

        def send(self, request, **kwargs):
            try:
                parsed = urlsplit(request.url)
                address = ipaddress.ip_address(parsed.hostname or "")
                port = parsed.port or 80
                actual_origin = (parsed.scheme, address.compressed.lower(), port)
            except (ValueError, AttributeError):
                raise _ServiceFailure(
                    "generated API request left the configured loopback origin"
                ) from None
            if actual_origin != origin or not address.is_loopback:
                raise _ServiceFailure(
                    "generated API request left the configured loopback origin"
                )
            body = request.body
            if body is not None:
                if isinstance(body, str):
                    body = body.encode("utf-8")
                if not isinstance(body, bytes) or len(body) > max_bytes:
                    raise _LimitExceeded(
                        "generated API request body exceeds 1 MiB or is not a bounded byte body"
                    )
            if request_budget["count"] >= MAX_TOTAL_REQUESTS:
                raise _LimitExceeded(
                    "API sequence worker exceeds 2,000 total HTTP requests"
                )
            request_budget["count"] += 1
            kwargs["allow_redirects"] = False
            kwargs["stream"] = True
            response = super().send(request, **kwargs)
            content = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise _LimitExceeded("API response body exceeds 1 MiB")
                response._content = bytes(content)
                response._content_consumed = True
                return response
            except BaseException:
                response.close()
                raise

    return BoundedSession


def _failure_ids(error: BaseException) -> tuple[str, ...]:
    failures: list[str] = []
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        elif current.__class__.__name__ not in {"FailureGroup"}:
            failures.append(
                f"{current.__class__.__module__}.{current.__class__.__qualname__}"
            )
    return tuple(sorted(set(failures)))


def _failure_of(
    error: BaseException, failure_type: type[BaseException]
) -> BaseException | None:
    pending = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, failure_type):
            return current
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
    return None


def _case_data(case: Any) -> dict[str, Any]:
    def plain(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, dict):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        raise _LimitExceeded(
            "generated API case contains a value outside the bounded JSON scope"
        )

    data = {
        "method": str(case.operation.method).upper(),
        "operation_id": case.operation.definition.raw.get("operationId"),
        "path": case.path,
        "path_parameters": plain(case.path_parameters or {}),
        "query": plain(case.query or {}),
    }
    if case.body.__class__.__name__ != "NotSet":
        data["body"] = plain(case.body)
    try:
        encoded = json.dumps(
            data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _LimitExceeded(
            "generated API case cannot be represented as bounded JSON"
        ) from None
    if len(encoded) > MAX_STEP_BYTES:
        raise _LimitExceeded(
            "one generated API case exceeds the 32 KiB counterexample limit"
        )
    return data


def _reset(
    reset_url: str,
    origin: tuple[str, str, int],
    requests_module: Any,
    request_budget: dict[str, int],
) -> None:
    session = _worker_session_class(origin, MAX_BODY_BYTES, request_budget)()
    try:
        response = session.post(
            reset_url,
            data=b"",
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
        if not 200 <= response.status_code < 300:
            raise _ServiceFailure(
                "same-origin reset did not return a 2xx response without redirect"
            )
        response.close()
    except (_LimitExceeded, _ServiceFailure):
        raise
    except requests_module.RequestException:
        raise _ServiceFailure("same-origin reset request failed") from None
    finally:
        session.close()


def _worker_run(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        _validate_engine()
    except _EngineUnavailable as exc:
        return {"status": "UNAVAILABLE", "detail": str(exc)}
    import requests
    import schemathesis
    from hypothesis import HealthCheck, settings
    from schemathesis.config import (
        GenerationConfig,
        ProjectConfig,
        ProjectsConfig,
        SchemathesisConfig,
    )
    from schemathesis.core.failures import FailureGroup
    from schemathesis.generation import GenerationMode

    base = _endpoint(payload.get("base_url"), _BASE_ENV)
    reset = _endpoint(payload.get("reset_url"), _RESET_ENV)
    if base.origin != reset.origin:
        return {
            "status": "UNSUPPORTED",
            "detail": "API and reset URLs must have the same loopback origin",
        }
    max_examples = payload.get("max_examples")
    max_steps = payload.get("max_steps")
    if (
        type(max_examples) is not int
        or not 1 <= max_examples <= MAX_EXAMPLES
        or type(max_steps) is not int
        or not 1 <= max_steps <= MAX_STEPS
    ):
        return {
            "status": "UNSUPPORTED",
            "detail": "worker limits are outside their supported range",
        }
    config = SchemathesisConfig(
        projects=ProjectsConfig(
            default=ProjectConfig(
                base_url=base.url,
                generation=GenerationConfig(
                    modes=[GenerationMode.POSITIVE],
                    deterministic=True,
                    allow_extra_parameters=False,
                    with_security_parameters=False,
                    database="none",
                ),
                request_timeout=REQUEST_TIMEOUT_SECONDS,
                max_redirects=0,
                request_retries=0,
                proxy=None,
                headers={},
                auth=None,
            )
        )
    )
    try:
        schema = schemathesis.openapi.from_path(payload["schema"], config=config)
        machine_base = schema.as_state_machine()
    except Exception as exc:  # noqa: BLE001 - engine parser failures have version-specific types.
        return {
            "status": "UNSUPPORTED",
            "detail": f"Schemathesis could not load the captured OpenAPI schema ({type(exc).__name__})",
        }
    if (
        getattr(schema.statistic, "operations", None) is not None
        and schema.statistic.operations.total == 0
    ):
        return {
            "status": "UNSUPPORTED",
            "detail": "OpenAPI schema declares no executable operations",
        }

    counts = {"sequences": 0, "steps": 0, "resets": 0}
    request_budget = {"count": 0}
    latest: dict[str, Any] = {"steps": None, "failure_ids": None}
    origin = base.origin

    class Campaign(machine_base):
        def setup(self) -> None:
            super().setup()
            if not getattr(self, "_replay", False):
                _reset(reset.url, origin, requests, request_budget)
                counts["resets"] += 1
            self.session = _worker_session_class(
                origin, MAX_BODY_BYTES, request_budget
            )()
            self.sequence: list[dict[str, Any]] = []
            self.current_input = None
            counts["sequences"] += 1

        def teardown(self) -> None:
            session = getattr(self, "session", None)
            if session is not None:
                session.close()
            super().teardown()

        def get_call_kwargs(self, case):
            return {
                "base_url": base.url,
                "session": self.session,
                "timeout": REQUEST_TIMEOUT_SECONDS,
                "allow_redirects": False,
            }

        def step(self, input):
            self.current_input = input
            try:
                return super().step(input)
            except FailureGroup as exc:
                latest["steps"] = list(self.sequence)
                latest["failure_ids"] = _failure_ids(exc)
                raise

        def after_call(self, response, case):
            super().after_call(response, case)
            if self.current_input is None:
                raise _WorkerFailure(
                    "Schemathesis did not provide a replayable step input"
                )
            row = {"case": _case_data(case), "status_code": response.status_code}
            self.sequence.append({"input": self.current_input, "report": row})
            counts["steps"] += 1

    run_settings = settings(
        max_examples=max_examples,
        stateful_step_count=max_steps,
        deadline=None,
        derandomize=True,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            Campaign.run(settings=run_settings)
    except (
        Exception,  # noqa: BLE001 - engine failures become INCONCLUSIVE results.
        BaseExceptionGroup,
        _WorkerFailure,
        _ServiceFailure,
        _LimitExceeded,
    ) as exc:
        failure = _failure_of(exc, FailureGroup)
        if failure is None:
            cause = exc
            while cause is not None:
                if isinstance(cause, (_ServiceFailure, _LimitExceeded, _WorkerFailure)):
                    return {"status": "INCONCLUSIVE", "detail": str(cause)}
                cause = cause.__cause__ or cause.__context__
            return {
                "status": "INCONCLUSIVE",
                "detail": f"stateful campaign did not complete ({type(exc).__name__})",
            }
        sequence = latest["steps"]
        expected_failures = latest["failure_ids"]
        state_machine_setups_before_failure = counts["sequences"]
        steps_before_failure = counts["steps"]
        resets_before_replay = counts["resets"]
        if not sequence or not expected_failures:
            return {
                "status": "INCONCLUSIVE",
                "detail": "engine reported a failure without a replayable sequence",
            }
        try:
            replay = Campaign()
            replay._replay = True
            replay_ids: tuple[str, ...] = ()
            try:
                for row in sequence:
                    step_input = row["input"]
                    if step_input.transition is not None:
                        replay.recorder.record_case(
                            parent_id=step_input.transition.parent_id,
                            transition=step_input.transition,
                            case=step_input.case,
                            is_transition_applied=step_input.is_applied,
                        )
                    else:
                        replay.recorder.record_case(
                            parent_id=None,
                            case=step_input.case,
                            transition=None,
                            is_transition_applied=False,
                        )
                    replay.control.record_step(step_input, replay.recorder)
                    replay.step(step_input)
                replay.teardown()
            except FailureGroup as replay_error:
                replay_ids = _failure_ids(replay_error)
                replay.teardown()
            except (
                Exception,  # noqa: BLE001 - worker and engine failures cannot prove replay.
                BaseExceptionGroup,
                _WorkerFailure,
                _ServiceFailure,
                _LimitExceeded,
            ) as replay_error:
                replay.teardown()
                cause = replay_error
                while cause is not None:
                    if isinstance(
                        cause, (_ServiceFailure, _LimitExceeded, _WorkerFailure)
                    ):
                        return {"status": "INCONCLUSIVE", "detail": str(cause)}
                    cause = cause.__cause__ or cause.__context__
                return {
                    "status": "INCONCLUSIVE",
                    "detail": "captured sequence could not be replayed",
                }
        except (
            Exception,  # noqa: BLE001 - replay setup errors are inconclusive.
            BaseExceptionGroup,
            _WorkerFailure,
            _ServiceFailure,
            _LimitExceeded,
        ) as exc:
            return {
                "status": "INCONCLUSIVE",
                "detail": f"fresh replay could not start ({type(exc).__name__})",
            }
        if replay_ids != expected_failures:
            return {
                "status": "INCONCLUSIVE",
                "detail": "fresh replay did not reproduce the same engine failure",
            }
        output_steps = [
            {**row["report"], "operation_id": row["report"]["case"].get("operation_id")}
            for row in sequence
        ]
        serialized = json.dumps(
            output_steps, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(serialized) > MAX_REPORT_BYTES - 4096:
            return {
                "status": "INCONCLUSIVE",
                "detail": "minimized counterexample exceeds the 256 KiB report limit",
            }
        return {
            "status": "BUG_REPRODUCED",
            "counterexample": {
                "failure_ids": list(expected_failures),
                "replayed_failure_ids": list(replay_ids),
                "steps": output_steps,
                "sha256": hashlib.sha256(serialized).hexdigest(),
            },
            "campaign": {
                "state_machine_setups_before_failure": state_machine_setups_before_failure,
                "steps_before_failure": steps_before_failure,
                "max_examples": max_examples,
                "max_steps": max_steps,
                "resets_before_replay": resets_before_replay,
                "replay_resets": counts["resets"] - resets_before_replay,
                "total_requests": request_budget["count"],
                "max_total_requests": MAX_TOTAL_REQUESTS,
            },
        }
    if counts["sequences"] == 0 or counts["steps"] == 0:
        return {
            "status": "INCONCLUSIVE",
            "detail": "stateful engine did not complete every requested sequence",
            "campaign": {
                "requested_examples": max_examples,
                "state_machine_setups": counts["sequences"],
                "steps": counts["steps"],
                "max_examples": max_examples,
                "max_steps": max_steps,
                "total_requests": request_budget["count"],
                "max_total_requests": MAX_TOTAL_REQUESTS,
            },
        }
    return {
        "status": "NO_VIOLATION",
        "campaign": {
            "requested_examples": max_examples,
            "state_machine_setups": counts["sequences"],
            "steps": counts["steps"],
            "max_examples": max_examples,
            "max_steps": max_steps,
            "total_requests": request_budget["count"],
            "max_total_requests": MAX_TOTAL_REQUESTS,
        },
        "detail": "all requested sampled stateful sequences completed without a replayable failure",
    }


def _worker_main(payload_path: Path) -> int:
    try:
        if (
            payload_path.is_symlink()
            or not payload_path.is_file()
            or payload_path.stat().st_size > 16_384
        ):
            raise _WorkerFailure("worker request is missing or too large")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise _WorkerFailure("worker request is malformed")
        result = _worker_run(payload)
    except (Exception, _WorkerFailure, _ServiceFailure, _LimitExceeded) as exc:  # noqa: BLE001 - always return a bounded worker report.
        result = {
            "status": "INCONCLUSIVE",
            "detail": f"worker failed safely ({type(exc).__name__})",
        }
    raw = json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raw = json.dumps(
            {"status": "INCONCLUSIVE", "detail": "worker report exceeds 256 KiB"},
            separators=(",", ":"),
        ).encode("utf-8")
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    raise SystemExit(_worker_main(Path(sys.argv[2])))

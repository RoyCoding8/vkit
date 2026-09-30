"""Request and response shapes. Validation happens here and nowhere else.

This is the boundary the plan names: everything arriving from a browser is
untrusted text until it has been through this module, and everything leaving is
a JSON document with a known shape. `server.py` below knows routing and nothing
else, and `operations.py` above knows nothing about either.

Two things are worth naming.

**Refusals keep the core's wording.** `error_of` passes the reason through
unchanged. The plan requires the console to show the operation's own message
rather than a friendlier invention, and a paraphrase is exactly the failure
that requirement exists to catch.

**The manifest has no route.** Not one handler below reads a path under
`verification/` or `schemas/`, and there is no request shape that could name
one. A test walks this file's AST and asserts it, so the guarantee is checked
against the source rather than against a comment describing the source.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from . import operations
from .plan import (
    DEFAULT_RUN_LIMIT,
    MAX_LOG_BYTES,
    NotImplementedInBuild,
    Refused,
    WRITABLE_NAMES,
    under_protected_path,
)
from .operations import Context

#: Everything a caller can ask for. Read-only views plus the six operations.
#: Nothing here takes a path from the request, so nothing here can be pointed at
#: the repository's committed policy.
ROUTES: dict[str, Callable[[Context, Mapping[str, Any]], Any]] = {
    "project": lambda ctx, q: operations.project_view(ctx),
    "readiness": lambda ctx, q: operations.readiness_view(ctx),
    "checks": lambda ctx, q: operations.checks_view(ctx),
    "runs": lambda ctx, q: operations.runs_view(ctx, limit=_int_param(q, "limit", DEFAULT_RUN_LIMIT)),
    "run": lambda ctx, q: operations.run_detail_view(ctx, _text_param(q, "run_id")),
    "log": lambda ctx, q: operations.log_tail(
        ctx,
        _text_param(q, "run_id"),
        _text_param(q, "stream", "stdout"),
        max_bytes=_int_param(q, "max_bytes", MAX_LOG_BYTES),
    ),
    "recovery": lambda ctx, q: operations.recovery_view(ctx),
    "operations": lambda ctx, q: {"operations": operations.writable_surface()},
    "plan": lambda ctx, q: operations.plan_change_set(ctx, _text_param(q, "operation")).to_json(),
    "run_check": lambda ctx, q: operations.run_check(ctx, _text_param(q, "check_id")),
    "cancel_run": lambda ctx, q: operations.cancel_check_run(ctx, _text_param(q, "run_id")),
    # Invoke an operation by name. Every name on the writable surface is
    # reachable here and nowhere else, and the four the core does not have
    # refuse with 501 rather than being absent from the list.
    "apply": lambda ctx, q: _apply(ctx, q),
}

#: The only request parameters any route reads. None of them is a path, so no
#: request can name a file the console would then act on.
PARAM_NAMES: frozenset[str] = frozenset({
    "limit", "run_id", "check_id", "stream", "max_bytes", "operation", "scope", "accepted",
})


def _apply(context: Context, query: Mapping[str, Any]) -> dict[str, Any]:
    """Call one operation by name, and nothing else.

    The lookup is through `operations.OPERATIONS`, whose keys are asserted equal
    to the writable list in a test, so this cannot reach a function that is not
    on the surface. Arguments are coerced to the types the operation declares, so
    `accepted=true` arrives as a boolean rather than the string "true", which is
    truthy for `accepted=false` and would enroll a repository nobody accepted.
    """
    name = _text_param(query, "operation")
    handler = operations.OPERATIONS.get(name)
    if handler is None:
        raise BadRequest(
            f"unknown operation {name!r}; the writable surface is: "
            f"{', '.join(sorted(operations.OPERATIONS))}"
        )
    arguments: dict[str, Any] = {}
    if "scope" in query:
        arguments["scope"] = _text_param(query, "scope")
    if "accepted" in query:
        arguments["accepted"] = _bool_param(query, "accepted")
    result = handler(context, **arguments)
    return {"operation": name, "result": result}


class BadRequest(Exception):
    """The request was malformed. Never about whether the operation is permitted."""


def _bool_param(query: Mapping[str, Any], name: str) -> bool:
    """A boolean from a query string or a JSON body.

    Only the two literals the page sends are accepted. `bool("false")` is True,
    so anything that stringifies into truthiness would let a caller asking for
    "false" get "true"; that is the whole reason this is a fixed table.
    """
    raw = query.get(name)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0", ""):
            return False
    raise BadRequest(f"{name} must be true or false, got {raw!r}")


def _text_param(query: Mapping[str, Any], name: str, default: str | None = None) -> str:
    raw = query.get(name, default)
    if raw is None:
        raise BadRequest(f"missing required parameter {name!r}")
    if not isinstance(raw, str) or not raw.strip():
        raise BadRequest(f"{name} must be a nonempty string, got {raw!r}")
    if under_protected_path(raw):
        raise BadRequest(
            f"{name} must not name a repository policy path; the console does not "
            f"read or write anything under verification/ or schemas/"
        )
    return raw


def _int_param(query: Mapping[str, Any], name: str, default: int) -> int:
    raw = query.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise BadRequest(f"{name} must be an integer, got a boolean")
    if isinstance(raw, str):
        try:
            return int(raw, 10)
        except ValueError:
            raise BadRequest(f"{name} must be an integer, got {raw!r}") from None
    if isinstance(raw, int):
        return raw
    raise BadRequest(f"{name} must be an integer, got {type(raw).__name__}")


def dispatch(context: Context, route: str, query: Mapping[str, Any]) -> dict[str, Any]:
    """Run one named route. Raises BadRequest, Refused, or NotImplementedInBuild."""
    handler = ROUTES.get(route)
    if handler is None:
        raise BadRequest(
            f"unknown route {route!r}; the console answers {', '.join(sorted(ROUTES))}"
        )
    return handler(context, query)


def error_of(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """The status and body for an exception. The message is never reworded.

    Three statuses because the caller must be able to tell them apart: 400 is a
    malformed request, 409 is the core declining, and 501 is an operation this
    build does not have. Collapsing 409 into 400 would tell an operator their
    typo and the core's refusal were the same event.
    """
    if isinstance(exc, BadRequest):
        return 400, {"error": str(exc)}
    if isinstance(exc, NotImplementedInBuild):
        return 501, {"error": str(exc), "operation": exc.operation,
                     "implemented": False, "writable_surface": list(WRITABLE_NAMES)}
    if isinstance(exc, Refused):
        return 409, {"error": exc.reason, "refused": True}
    return 500, {"error": str(exc)}


def parse_query(raw: str) -> dict[str, str]:
    """Parse a query string, tolerating a malformed one by yielding nothing.

    A browser is the only caller and it will not send a broken query, so
    refusing the whole request here would be a guard against nothing. The typed
    validators in `_int_param` and `_text_param` are where a bad value is caught.
    """
    from urllib.parse import parse_qsl

    return {key: value for key, value in parse_qsl(raw, keep_blank_values=False)}


def parse_body(raw: bytes) -> dict[str, Any]:
    """Decode a JSON object body, or refuse.

    A body is optional for the read routes and optional for the write ones,
    which also take their arguments from the query so that a refused mutation is
    reproducible from the URL in a test. A body value is passed through with its
    JSON type intact, because `accepted=false` has to stay false: a body coerced
    to strings would make it truthy and enroll a repository nobody accepted.
    """
    if not raw:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequest(f"body is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise BadRequest(f"body must be a JSON object, got {type(document).__name__}")
    return document

"""The request gate. Validation happens here and nowhere else.

Everything arriving from a browser is untrusted text until it has been through
`admit`, and every refusal happens there, before a route is chosen. `server.py`
below knows the socket and nothing else; `operations.py` above knows nothing
about either, and is never handed the token.

**The route tables are split by what they do.** `READ_ROUTES` answers GET and
reads. `MUTATIONS` answers POST and needs the session token. A name in one table
is absent from the other, so no read route can be reached by a method that
writes and no mutation can be reached by one that does not.

**Refusals keep the core's wording.** `error_of` passes the reason through
unchanged, so a refusal the core raised reaches the operator as the core wrote
it.

**The manifest has no route.** Not one handler below reads a path under
`verification/` or `schemas/`, and there is no request shape that could name
one. This module opens no file at all: every route reads from `operations`,
which holds the project's resolved paths. `test_this_module_names_no_policy_path`
walks this file's AST and asserts it, so the guarantee is checked against the
source rather than against a comment describing the source.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import operations
from .plan import (
    CONFIG_STAGES,
    DEFAULT_RUN_LIMIT,
    MAX_LOG_BYTES,
    NotImplementedInBuild,
    Refused,
    WRITABLE_NAMES,
    under_protected_path,
)
from .operations import Context, ConfigurationRefused

#: The only host names that name this machine. The socket is bound to
#: `LOOPBACK_HOST`, so these resolve here, and a request naming anything else is
#: a name the console did not serve.
LOOPBACK_NAMES: tuple[str, ...] = ("127.0.0.1", "localhost")

#: Bodies are not needed by any route, so a body above this is refused rather
#: than read. Every argument travels in the query string.
MAX_BODY_BYTES = 64 * 1024

#: The header the page carries its session token in.
TOKEN_HEADER = "X-Vkit-Token"

#: Everything a caller can read. Nothing here writes.
READ_ROUTES: dict[str, Callable[[Context, Mapping[str, Any]], Any]] = {
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
}

#: Everything a caller can change. Each name is on the writable surface, and
#: each is reachable here and nowhere else.
MUTATIONS: dict[str, Callable[[Context, Mapping[str, Any]], Any]] = {
    "run_check": lambda ctx, q: operations.run_check(ctx, _text_param(q, "check_id")),
    "cancel_run": lambda ctx, q: operations.cancel_check_run(ctx, _text_param(q, "run_id")),
    # Invoke an operation by name. Every name on the writable surface is
    # reachable here and nowhere else. All six are implemented in this build,
    # so no name currently reaches the 501 path; an operation marked
    # `implemented=False` would refuse with 501 rather than being absent from
    # the list.
    "apply": lambda ctx, q: _apply(ctx, q),
}

#: The only request parameters any route reads. None of them is a path, so no
#: request can name a file the console would then act on.
#:
#: `stage` is the one addition checkpoint 12.3 made. It is a closed vocabulary of
#: four words checked in `_stage_param`, it is read by no read route, and it
#: cannot carry a path: a value outside `CONFIG_STAGES` is refused by name, so
#: the new parameter cannot become a way to name a file. Every other name is
#: declared here rather than at each call site so that a name added to a route
#: without being added to this set fails a test instead of quietly widening what a
#: request can name.
PARAM_NAMES: frozenset[str] = frozenset({
    "limit", "run_id", "check_id", "stream", "max_bytes", "operation", "scope", "accepted",
    "stage",
})


@dataclass(frozen=True)
class Session:
    """What makes this console instance this console: its port, and its token.

    The token is minted once, here, and is the only value any mutation accepts.
    Nothing outside this module reads it, so no operation can be reached with a
    token it was handed.
    """
    port: int
    token: str

    @classmethod
    def mint(cls, port: int) -> "Session":
        return cls(port=port, token=secrets.token_urlsafe(32))

    def authorities(self) -> tuple[str, ...]:
        """The `Host` values that name this console."""
        return tuple(f"{name}:{self.port}" for name in LOOPBACK_NAMES)

    def origins(self) -> tuple[str, ...]:
        return tuple(f"http://{authority}" for authority in self.authorities())


@dataclass(frozen=True)
class Request:
    """What arrived, before any of it is believed.

    `route` is `None` for a path the console does not route, which the server
    serves as a page. `length` and `chunked` are the raw framing headers, kept
    unparsed so this module decides what they mean.
    """
    method: str
    route: str | None
    query: Mapping[str, str]
    host: str
    origin: str | None
    token: str | None
    length: str | None
    chunked: bool


class Rejected(Exception):
    """This boundary will not admit the request. `status` is the answer."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class BadRequest(Exception):
    """The request was malformed. Never about whether the operation is permitted."""


def admit(
    request: Request, session: Session, read_body: Callable[[int], bytes],
) -> Mapping[str, Any] | None:
    """The one gate. Returns what `dispatch` should run, or `None` for a page.

    Every refusal below happens before a handler is chosen, so a refused request
    cannot reach an operation however it was addressed. `read_body` is called
    only once every check that could refuse has passed, and it is handed a
    length already proved to be at most `MAX_BODY_BYTES`, so a request refused
    for any reason above has had no body read at all.
    """
    if request.host not in session.authorities():
        raise Rejected(
            403, f"refusing Host {request.host!r}; this console answers "
                 f"{', '.join(session.authorities())} and no other address"
        )
    if request.origin is not None and request.origin not in session.origins():
        raise Rejected(
            403, f"refusing Origin {request.origin!r}; this console answers its own page "
                 f"({', '.join(session.origins())}) and nothing else"
        )
    if request.route is None:
        return None
    if request.method == "GET":
        if request.route in MUTATIONS:
            raise Rejected(
                405, f"{request.route} changes state and is answered by POST only; "
                     f"the routes GET answers are {', '.join(sorted(READ_ROUTES))}"
            )
        if request.route not in READ_ROUTES:
            raise BadRequest(f"unknown route {request.route!r}")
        return request.query

    if request.route not in MUTATIONS:
        raise Rejected(
            405, f"{request.route} reads and is answered by GET; POST reaches only "
                 f"{', '.join(sorted(MUTATIONS))}"
        )
    if not secrets.compare_digest((request.token or "").encode(), session.token.encode()):
        raise Rejected(
            403, "a mutation needs this console page's session token; "
                 "load the page this console served and use its buttons"
        )
    if request.chunked:
        raise Rejected(400, "send a Content-Length; this console does not read chunked bodies")
    if request.length and not request.length.isdigit():
        raise Rejected(400, f"Content-Length is not a length: {request.length!r}")
    length = int(request.length or 0)
    if length > MAX_BODY_BYTES:
        raise Rejected(
            413, f"body above {MAX_BODY_BYTES} bytes; send arguments in the query string"
        )
    return {**request.query, **parse_body(read_body(length))}


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
    if "stage" in query:
        # Only the configuration operation reads it, and the operation refuses
        # the name it was not asked for. Passing it to every operation would make
        # `stage` a parameter each handler has to remember to ignore.
        if name != "save_project_config":
            raise BadRequest(
                f"stage applies only to save_project_config, not to {name!r}"
            )
        arguments["stage"] = _stage_param(query, "stage")
    # The proposed document and the digest it was edited against are body
    # values, not query parameters: both are too large for a URL, and the digest
    # is compared rather than read. Neither is a path, so neither reaches
    # `under_protected_path`, and the operation validates the document's own
    # structure before it writes anything.
    for body_name in ("document", "expected_digest", "revision"):
        if body_name in query:
            arguments[body_name] = query[body_name]
    if "document" in arguments:
        arguments["document"] = _document_param(arguments["document"])
    if "expected_digest" in arguments:
        arguments["expected_digest"] = _digest_param(
            arguments["expected_digest"], "expected_digest"
        )
    if "revision" in arguments:
        arguments["revision"] = _digest_param(arguments["revision"], "revision")
    if "path" in query:
        raise BadRequest(
            "no operation takes a path. The configuration save writes the two "
            "fixed project configuration files named in the writable surface and "
            "takes no path, so there is nothing here to redirect"
        )
    result = handler(context, **arguments)
    return {"operation": name, "result": result}


def _stage_param(query: Mapping[str, Any], name: str) -> str:
    """A stage from the closed four-word vocabulary, or a refusal quoting it.

    Checked here rather than at the operation because this is where untrusted
    text becomes a typed value: the operation receives a `str` it may trust.
    `under_protected_path` runs first, so a caller cannot smuggle a path through
    this parameter even as far as the refusal message.
    """
    raw = query.get(name)
    if not isinstance(raw, str) or raw not in CONFIG_STAGES:
        raise BadRequest(
            f"{name} must be one of {', '.join(CONFIG_STAGES)}, got {raw!r}"
        )
    return raw


def _document_param(raw: Any) -> dict[str, Any]:
    """The proposed configuration document, as a JSON object.

    It must be an object because every field in it is a typed value the
    operation parses, and a list or a string would be a request shape with no
    validation path. It is deliberately NOT read by `_text_param`: that helper
    refuses a string naming a protected path, which is the right rule for an
    identifier and the wrong rule for a validated document whose contents are
    checked by the operation's own whole-document validation. Applying the
    identifier rule to the document would have required weakening it for every
    other route, which is the blanket relaxation this checkpoint refuses.
    """
    if not isinstance(raw, dict):
        raise BadRequest(
            f"document must be a JSON object, got {type(raw).__name__}"
        )
    return raw


def _digest_param(raw: Any, name: str) -> str:
    """A hex digest, of the length this console's own digests have.

    `expected_digest` is a full 64-character sha256 over the configuration and a
    `revision` is the first 32 characters of the sha256 over the same content, so
    one length cannot check both. Both are checked as hex of a length this module
    produces, and the operation compares against a real value either way: a caller
    that sent something else cannot make it match, so this is about a readable
    refusal rather than about safety.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise BadRequest(f"{name} must be a nonempty string, got {raw!r}")
    text = raw.strip()
    if len(text) not in (32, 64) or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise BadRequest(
            f"{name} must be a 64-character configuration digest or a 32-character "
            f"revision, got {raw!r}"
        )
    return text


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
    """Run one named route. The gate has already decided the method may reach it.

    Raises BadRequest, Rejected, or NotImplementedInBuild.
    """
    handler = READ_ROUTES.get(route) or MUTATIONS.get(route)
    if handler is None:
        raise BadRequest(
            f"unknown route {route!r}; the console answers {', '.join(sorted(READ_ROUTES | MUTATIONS))}"
        )
    return handler(context, query)


def error_of(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """The status and body for an exception. The message is never reworded.

    Five statuses because the caller must be able to tell them apart: 400 is a
    malformed request, 403 is this boundary refusing the caller, 405 is a method
    the route does not answer, 409 is the core declining, and 501 is an operation
    this build does not have. Collapsing 403 into 400 would tell an operator
    whose page was rejected the same thing as whose request had a typo.
    """
    if isinstance(exc, Rejected):
        return exc.status, {"error": exc.reason}
    if isinstance(exc, BadRequest):
        return 400, {"error": str(exc)}
    if isinstance(exc, NotImplementedInBuild):
        return 501, {"error": str(exc), "operation": exc.operation,
                     "implemented": False, "writable_surface": list(WRITABLE_NAMES)}
    # A configuration refusal is a 409 with the same shape as a core refusal, so
    # a caller sees one conflict status rather than a 500 that reads as a crash.
    # Its reason is carried verbatim for the same reason every other refusal is.
    if isinstance(exc, ConfigurationRefused):
        return 409, {"error": str(exc), "refused": True,
                     "configuration": True}
    if isinstance(exc, Refused):
        return 409, {"error": exc.reason, "refused": True}
    return 500, {"error": str(exc)}


def parse_body(raw: bytes) -> dict[str, Any]:
    """Decode a JSON object body, or refuse.

    A body value is passed through with its JSON type intact, because
    `accepted=false` has to stay false: a body coerced to strings would make it
    truthy and enroll a repository nobody accepted.
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

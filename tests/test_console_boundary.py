"""What the console refuses, and that a refusal reaches nothing.

**The seam is `api.dispatch`.** It is the last thing between a request and an
operation, so replacing it with a recorder settles both halves of every case
below at once: what the wire said, and that no operation was reached. A test
that only read the response would pass against the old server too, which
answered 413 and then ran `run_check` anyway.

**Real HTTP, real server, real socket.** The old defect was not visible in a
unit test of the parsing helpers. It needed the connection to stay open with an
unread body in it, so every case here goes over a socket and reads the socket
after the response, the way `review/probe_interface.py` does.

**No repository on disk.** `Context(None, None, None, None)` is deliberate: a
request the gate refuses never reaches a context, and the recorder means no
operation runs even when one is admitted. Building a throwaway Git repository
would test the fixture, not the boundary.

**Assert literals.** Every status and every substring below is written out. A
test asserting `status == api.error_of(exc)[0]` would agree with whatever the
code decided.
"""
from __future__ import annotations

import ast
import http.client
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from vkit.console import api, server
from vkit.console.operations import Context


@dataclass()
class Console:
    """A live console, its session token, and everything dispatched through it."""

    port: int
    token: str
    dispatched: list[dict[str, Any]] = field(default_factory=list)

    def headers(self, **overrides: str | None) -> dict[str, str]:
        """The headers a page on this console sends, with `overrides` applied.

        An override of `None` removes a header, so the cases that leave the token
        or the origin off are expressed here rather than by a second dict.
        """
        sent = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            api.TOKEN_HEADER: self.token,
        }
        for name, value in overrides.items():
            if value is None:
                sent.pop(name.replace("_", "-"), None)
            else:
                sent[name.replace("_", "-")] = value
        return sent


@pytest.fixture()
def console(monkeypatch: pytest.MonkeyPatch) -> Console:
    """A console whose dispatch records instead of running an operation."""
    live = Console(port=0, token="")
    monkeypatch.setattr(
        api, "dispatch",
        lambda _context, route, query: live.dispatched.append(
            {"route": route, "query": dict(query)}
        ) or {"witness": "dispatch reached, no operation ran"},
    )
    # No repository on disk: the recorder means nothing below this line runs an
    # operation, so the context is here to be passed along, not read.
    bound, thread = server.start_in_thread(Context(None, None, None, None), port=0)  # type: ignore[arg-type]
    live.port = bound.server_address[1]
    live.token = bound.session.token
    try:
        yield live
    finally:
        bound.shutdown()
        bound.server_close()
        thread.join(timeout=2)


def _wire(
    console: Console, method: str, path: str,
    *, headers: dict[str, str] | None = None, body: bytes | None = None,
    trailing: bool = False,
) -> tuple[int, dict[str, Any], list[str]]:
    """Send one request and read the connection afterwards.

    `trailing` collects every `HTTP/1.1` status line that arrives after the
    response, which is how a leftover request body announces itself as a second
    response the server never intended.
    """
    conn = http.client.HTTPConnection("127.0.0.1", console.port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        status = response.status
        document = json.loads(response.read().decode("utf-8"))
        return status, document, _trailing(conn) if trailing else []
    finally:
        conn.close()


def _trailing(conn: http.client.HTTPConnection) -> list[str]:
    conn.sock.settimeout(1)
    received = bytearray()
    try:
        while chunk := conn.sock.recv(4096):
            received.extend(chunk)
    except (TimeoutError, OSError):
        pass
    text = received.decode("iso-8859-1", errors="replace")
    return [line for line in text.splitlines() if line.startswith("HTTP/1.1 ")]


def _asset(console: Console, path: str) -> str:
    """One static file, as the server sent it."""
    conn = http.client.HTTPConnection("127.0.0.1", console.port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{console.port}"})
        response = conn.getresponse()
        assert response.status == 200, path
        return response.read().decode("utf-8")
    finally:
        conn.close()


# ------------------------------------------------------- who is asking at all


def test_the_console_serves_a_read_from_its_own_page(console: Console) -> None:
    """The control: the same wiring the refusals below break actually works."""
    status, document, _ = _wire(console, "GET", "/api/checks", headers=console.headers())
    assert status == 200
    assert document == {"witness": "dispatch reached, no operation ran"}
    assert console.dispatched == [{"route": "checks", "query": {}}]


def test_a_foreign_host_is_refused_before_a_route_is_chosen(console: Console) -> None:
    status, document, late = _wire(
        console, "GET", "/api/checks",
        headers=console.headers(Host="untrusted.example"), trailing=True,
    )
    assert status == 403
    assert document == {
        "error": f"refusing Host 'untrusted.example'; this console answers "
                 f"127.0.0.1:{console.port}, localhost:{console.port} and no other address"
    }
    assert console.dispatched == []
    assert late == []


def test_a_foreign_host_is_refused_before_the_page_is_served(console: Console) -> None:
    """Host is checked first, so a page request is refused the same way.

    `/` is not a route, and a gate that answered pages without asking who was
    asking would hand the session token to whoever sent `Host`.
    """
    status, document, _ = _wire(
        console, "GET", "/", headers=console.headers(Host="untrusted.example"),
    )
    assert status == 403
    assert "refusing Host" in document["error"]
    assert console.token not in str(document)


def test_a_foreign_origin_is_refused(console: Console) -> None:
    """A page on another origin may read the console, and must not.

    This is the DNS-rebinding shape: the name resolves here, but the browser
    that sent the request was rendered somewhere else, and a page there can read
    whatever the loopback socket will hand it.
    """
    status, document, _ = _wire(
        console, "GET", "/api/checks",
        headers=console.headers(Origin="https://untrusted.example"),
    )
    assert status == 403
    assert document == {
        "error": f"refusing Origin 'https://untrusted.example'; this console answers "
                 f"its own page (http://127.0.0.1:{console.port}, "
                 f"http://localhost:{console.port}) and nothing else"
    }
    assert console.dispatched == []


def test_a_local_origin_with_a_different_port_is_refused(console: Console) -> None:
    """Loopback is not enough; the origin has to be this console's own page."""
    status, document, _ = _wire(
        console, "GET", "/api/checks",
        headers=console.headers(Origin="http://127.0.0.1:9"),
    )
    assert status == 403
    assert document["error"] == (
        f"refusing Origin 'http://127.0.0.1:9'; this console answers its own page "
        f"(http://127.0.0.1:{console.port}, http://localhost:{console.port}) "
        f"and nothing else"
    )
    assert console.dispatched == []


def test_an_absent_origin_is_allowed(console: Console) -> None:
    """A browser sends `Origin` on a mutation, not on a same-origin navigation."""
    status, _document, _ = _wire(
        console, "GET", "/api/checks",
        headers={"Host": f"127.0.0.1:{console.port}"},
    )
    assert status == 200
    assert [call["route"] for call in console.dispatched] == ["checks"]


# ------------------------------------------------------------ GET reads only


def test_a_mutation_is_not_answered_by_get(console: Console) -> None:
    """`run_check` over GET is a 405 naming the method that answers it.

    Not a 404 and not a mutation: the route exists, GET simply does not do it.
    """
    status, document, late = _wire(
        console, "GET", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(), trailing=True,
    )
    assert status == 405
    assert document == {
        "error": "run_check changes state and is answered by POST only; the routes "
                 "GET answers are checks, log, operations, plan, project, readiness, "
                 "recovery, run, runs"
    }
    assert console.dispatched == []
    assert late == []


def test_no_mutation_is_answered_by_get(console: Console) -> None:
    """All three, because the guarantee is about the table and not one name."""
    for route in sorted(api.MUTATIONS):
        console.dispatched.clear()
        status, document, _ = _wire(
            console, "GET", f"/api/{route}", headers=console.headers(),
        )
        assert status == 405, route
        assert document["error"].startswith(f"{route} changes state"), route
        assert console.dispatched == [], route


def test_a_read_is_not_answered_by_post(console: Console) -> None:
    """The other half of the split, with the token present and still refused."""
    status, document, _ = _wire(
        console, "POST", "/api/checks", headers=console.headers(), body=b"{}",
    )
    assert status == 405
    assert document == {
        "error": "checks reads and is answered by GET; POST reaches only "
                 "apply, cancel_run, run_check"
    }
    assert console.dispatched == []


# ----------------------------------------------------------- the session token


def test_a_mutation_without_a_token_is_refused(console: Console) -> None:
    """The absence case, which is the one a cross-origin POST produces.

    A browser sends no `X-Vkit-Token` on a request it did not read this console's
    token from, so this is the shape a hostile page actually puts on the wire.
    """
    status, document, late = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(**{api.TOKEN_HEADER: None}), body=b"{}",
        trailing=True,
    )
    assert status == 403
    assert document == {
        "error": "a mutation needs this console page's session token; load the page "
                 "this console served and use its buttons"
    }
    assert console.dispatched == []
    assert late == []


@pytest.mark.parametrize(
    "forged",
    [
        pytest.param("", id="empty"),
        pytest.param("not-the-token", id="wrong-text"),
        pytest.param("a" * 43, id="right-shape-wrong-value"),
        pytest.param("../../etc/passwd", id="a-path"),
    ],
)
def test_a_forged_token_is_refused(console: Console, forged: str) -> None:
    status, document, _ = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(**{api.TOKEN_HEADER: forged}), body=b"{}",
    )
    assert status == 403
    assert document["error"].startswith("a mutation needs this console page's session token")
    assert console.dispatched == []


def test_another_console_s_token_is_refused_here(console: Console) -> None:
    """The token is bound to a session, not to the shape of a token.

    Two console instances on one machine both mint one, and each answers only
    its own. A token that would be correct for a neighbouring console is not
    correct here.
    """
    neighbour = api.Session.mint(port=console.port)
    assert neighbour.token != console.token
    status, _document, _ = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(**{api.TOKEN_HEADER: neighbour.token}), body=b"{}",
    )
    assert status == 403
    assert console.dispatched == []


def test_the_session_token_never_reaches_an_operation(console: Console) -> None:
    """The admitted query is the caller's arguments and nothing else.

    An operation that could read the token could compare it, log it, or hand it
    on. It is not in the arguments it is called with, and
    `test_no_operation_code_names_the_token` holds the line in the source.
    """
    status, _document, _ = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(), body=b"{}",
    )
    assert status == 200
    assert console.dispatched == [{"route": "run_check", "query": {"check_id": "totals-behavior"}}]
    assert console.token not in str(console.dispatched)


def test_no_operation_code_names_the_token() -> None:
    """No identifier named `token` anywhere in `operations.py`.

    Checked on the AST rather than by searching for the string, so a mention in
    a comment cannot satisfy it and a use disguised as `self.session_token`
    cannot slip past it either.
    """
    source = Path(api.__file__).with_name("operations.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.arg):
            named.add(node.arg)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            named.add(node.arg)
    assert "token" not in named


# --------------------------------------------------------------- the body cap


def test_a_body_above_the_limit_is_refused_and_nothing_dispatches(console: Console) -> None:
    """F14: the 413 is the end of the exchange, not a note before the work.

    The old server answered 413, read nothing, and ran `run_check` anyway from
    an empty body, leaving 65,537 bytes on the wire to be parsed as the next
    request. So this asserts all three: the status, the empty recorder, and no
    second response on the socket.
    """
    oversized = b"x" * (api.MAX_BODY_BYTES + 1)
    status, document, late = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(), body=oversized, trailing=True,
    )
    assert status == 413
    assert document == {
        "error": f"body above {api.MAX_BODY_BYTES} bytes; "
                 f"send arguments in the query string"
    }
    assert console.dispatched == []
    assert late == []


def test_a_body_at_the_limit_is_answered(console: Console) -> None:
    """The cap is where it says it is, not one byte below it."""
    document = b'{"run_id":"r1"'
    padded = document + b" " * (api.MAX_BODY_BYTES - len(document) - 1) + b"}"
    assert len(padded) == api.MAX_BODY_BYTES
    status, _document, _ = _wire(
        console, "POST", "/api/cancel_run", headers=console.headers(), body=padded,
    )
    assert status == 200
    assert console.dispatched == [{"route": "cancel_run", "query": {"run_id": "r1"}}]


def test_a_chunked_body_is_refused_rather_than_guessed_at(console: Console) -> None:
    """No chunked reader exists, so the gate refuses instead of inventing one."""
    status, document, late = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(Transfer_Encoding="chunked"),
        body=b"0\r\n\r\n", trailing=True,
    )
    assert status == 400
    assert document == {
        "error": "send a Content-Length; this console does not read chunked bodies"
    }
    assert console.dispatched == []
    assert late == []


def test_a_length_that_is_not_a_length_is_refused(console: Console) -> None:
    status, document, _ = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(Content_Length="many"), body=b"{}",
    )
    assert status == 400
    assert document == {"error": "Content-Length is not a length: 'many'"}
    assert console.dispatched == []


# ------------------------------------------------------------- a malformed body


@pytest.mark.parametrize(
    ("body", "error"),
    [
        pytest.param(b'{"check_id": ', "body is not valid JSON", id="truncated"),
        pytest.param(b"check_id=totals", "body is not valid JSON", id="not-json-at-all"),
        pytest.param(b"\xff\xfe", "body is not valid JSON", id="not-utf-8"),
        pytest.param(b"[1, 2]", "body must be a JSON object, got list", id="a-list"),
        pytest.param(b'"totals"', "body must be a JSON object, got str", id="a-string"),
    ],
)
def test_a_malformed_body_is_refused_before_the_operation(
    console: Console, body: bytes, error: str,
) -> None:
    """Decoding is part of admitting, so a body that will not decode stops here.

    The token is valid in every one of these: the refusal has to come from the
    body, not from the caller being turned away earlier for a different reason.
    """
    status, document, late = _wire(
        console, "POST", "/api/run_check?check_id=totals-behavior",
        headers=console.headers(), body=body, trailing=True,
    )
    assert status == 400
    assert document["error"].startswith(error)
    assert console.dispatched == []
    assert late == []


def test_an_empty_body_is_the_query(console: Console) -> None:
    """Arguments live in the query, so a refused mutation is reproducible from a URL."""
    status, _document, _ = _wire(
        console, "POST", "/api/apply?operation=repair",
        headers=console.headers(), body=b"",
    )
    assert status == 200
    assert console.dispatched == [{"route": "apply", "query": {"operation": "repair"}}]


# --------------------------------------------------- the page can really write


def test_the_page_carries_this_console_s_token(console: Console) -> None:
    """The only way a browser gets the token is by loading the page we served.

    Substitution happens at serve time, so the token in the HTML is the one this
    console accepts and a stale page from another console carries one it does
    not.
    """
    page = _asset(console, "/")
    assert f'<meta name="vkit-token" content="{console.token}">' in page
    assert "__VKIT_TOKEN__" not in page


def test_the_script_sends_the_token_the_page_carries(console: Console) -> None:
    """The two halves of the static change, checked together rather than alone.

    `index.html` names the meta tag and `app.js` reads it and sends the header
    the gate checks. Either file edited without the other leaves the operator
    with a console whose buttons cannot write, which no unit test of either file
    would catch.
    """
    page, script = _asset(console, "/"), _asset(console, "/app.js")
    assert f'<meta name="vkit-token" content="{console.token}">' in page
    assert 'meta[name="vkit-token"]' in script
    assert api.TOKEN_HEADER in script
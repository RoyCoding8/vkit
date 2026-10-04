"""The three things `vkit console` needs that no other module decides.

Which address the console ends up on, whether a browser is asked for it, and how
the process stops are all decided by the command that runs it. Binding and the
refusal to bind stay in `server.serve`, the session token stays in
`ConsoleServer`, and every request refusal stays in `api.admit`. Nothing here
reaches past those, and nothing here formats output or chooses an exit code:
the shell owns both, so one table of exit codes still governs every command.
"""
from __future__ import annotations

import errno
import sys
import threading
import webbrowser

from .operations import Context
from .plan import LOOPBACK_HOST, Refused
from .server import ConsoleServer, serve

POLL_SECONDS = 0.2

JOIN_TIMEOUT_SECONDS = 5.0

PORT_TAKEN = (errno.EADDRINUSE, errno.EACCES)


def bind(context: Context, port: int) -> ConsoleServer:
    """Open the loopback socket, or refuse in words the operator can act on.

    `ConsoleServer` sets `allow_reuse_address`, which is why a busy port reaches
    this function as a bare `OSError` from `socket.bind` rather than as a
    tidy exception. The refusal names the port, repeats the operating system's
    own reason, and names the flag that changes it, because the operator's next
    move is a different port and not a guess at what went wrong. A port is never
    chosen on the operator's behalf.
    """
    try:
        return serve(context, port=port)
    except OSError as exc:
        if exc.errno in PORT_TAKEN:
            raise Refused(
                f"could not bind 127.0.0.1:{port}: {exc.strerror or exc}. "
                f"Pass --port with another port to start a console."
            ) from exc
        raise Refused(f"could not bind 127.0.0.1:{port}: {exc}") from exc
    except (OverflowError, ValueError) as exc:
        raise Refused(f"could not bind 127.0.0.1:{port}: {exc}") from exc


def url_for(server: ConsoleServer) -> str:
    """The address the console answers on, read after the bind has happened.

    A port asked for as 0 belongs to the OS, and `server_address` is the only
    place that choice is visible. A URL built from the port that was asked for
    would name an address nothing is listening on.
    """
    return f"http://{LOOPBACK_HOST}:{server.server_address[1]}/"


def open_in_browser(url: str) -> None:
    """Hand the URL to the default browser, and never let that decide anything.

    `webbrowser.open` returns False when it has nothing to launch and raises when
    the platform helper is broken. Neither stops the console. The page is already
    serving and the URL is already on stdout, so the operator can open it by
    hand; what is lost is a convenience and that is worth one line on stderr.
    """
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - a broken browser is not a broken console
        print(f"could not open a browser ({exc}); open {url} yourself", file=sys.stderr)
        return
    if not opened:
        print(f"no browser was opened; open {url} yourself", file=sys.stderr)


def serve_until_stopped(server: ConsoleServer, stop: threading.Event) -> bool:
    """Serve until `stop` is set or the operator interrupts, and close the socket.

    The serving loop runs off the calling thread so that `shutdown()` has
    somewhere legal to be called from: `socketserver` documents it as illegal on
    the thread that is serving, and calling it there deadlocks. The command
    still blocks for the whole life of the console, which is what foreground
    means to someone at a terminal.

    Returns True when the console stopped because of Ctrl-C.

    Ctrl-C arrives as `KeyboardInterrupt` on the thread that is serving, which
    is why the loop below waits on an event rather than parking in a bare
    `serve_forever`: a `KeyboardInterrupt` raised inside `serve_forever` unwinds
    straight past this function's `finally`, leaving the socket open.
    """
    thread = threading.Thread(
        target=server.serve_forever,
        name="vkit-console",
        kwargs={"poll_interval": POLL_SECONDS},
    )
    thread.start()
    interrupted = False
    try:
        while not stop.wait(POLL_SECONDS):
            if not thread.is_alive():
                break
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        server.shutdown()
        thread.join(JOIN_TIMEOUT_SECONDS)
        server.server_close()
    return interrupted
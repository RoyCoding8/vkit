"""Every process this suite launches, launched from one place.

A console application launched from a test runner inherits a console, and on
Windows that opens a window over whatever the operator was using. The suite
launches `git` several times per fixture, one real MCP server per client, and
`pip` plus a fresh interpreter per packaging gate, so a run covers the screen
with flashing windows. That is a defect in the suite, not in any one test, so
nothing inside a test can be held responsible for it.

The keywords live here rather than at the call sites for two reasons. A
`creationflags=` copied to each call site is the same fragment repeated N times,
and the N+1th call site forgets it. And the flag belongs to whoever *creates* a
process: a child launched with `CREATE_NO_WINDOW` is given no console, so the
processes it launches in turn inherit nothing and stay invisible too. Fixing
the outermost launch hides the whole subtree. Fixing the innermost one hides a
single process and leaves the rest of the tree flashing.

`tests/test_subprocess_windows.py` walks the AST of every test module and fails
if one reaches for `subprocess.run` or its siblings directly, so the next call
site cannot be added without this file.

Off Windows `hidden_window()` returns an empty dict and the wrappers are
`subprocess.run` and `subprocess.Popen` with nothing added. `creationflags` and
`startupinfo` are the reason: `subprocess.STARTUPINFO` does not exist on POSIX
at all, so a call site that reached for it directly would not merely be
ineffective there.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any, Sequence

__all__ = ["hidden_window", "popen", "run"]


def hidden_window() -> dict[str, Any]:
    """`subprocess` keywords that keep a launched child off the operator's screen.

    Empty on every non-Windows host, so the wrappers below add nothing there.

    Both keywords are needed on Windows. The creation flag stops the console
    being allocated, and the startup info stops a host that honours
    `STARTUPINFO` from showing one anyway.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def run(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` with the child kept off the operator's screen."""
    return subprocess.run(args, **hidden_window(), **kwargs)


def popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen` with the child kept off the operator's screen."""
    return subprocess.Popen(*args, **hidden_window(), **kwargs)

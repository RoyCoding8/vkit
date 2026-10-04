"""Launch test subprocesses with hidden Windows consoles. Preserve caller flags and standard handles. Descendant launch sites must also select hidden launch options."""
from __future__ import annotations

import subprocess
import sys
from typing import Any, Sequence

__all__ = ["hidden_window", "popen", "run", "call", "check_call", "check_output"]


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
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _hidden_options(kwargs: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "win32":
        return kwargs
    flags = kwargs.get("creationflags", 0)
    if not flags & (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE):
        flags |= subprocess.CREATE_NO_WINDOW
    startup = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {**kwargs, "creationflags": flags, "startupinfo": startup}


def run(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` with the child kept off the operator's screen."""
    return subprocess.run(args, **_hidden_options(kwargs))


def popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen` with the child kept off the operator's screen."""
    return subprocess.Popen(*args, **_hidden_options(kwargs))


def call(*args: Any, **kwargs: Any) -> int:
    return subprocess.call(*args, **_hidden_options(kwargs))


def check_call(*args: Any, **kwargs: Any) -> int:
    return subprocess.check_call(*args, **_hidden_options(kwargs))


def check_output(*args: Any, **kwargs: Any) -> bytes | str:
    return subprocess.check_output(*args, **_hidden_options(kwargs))

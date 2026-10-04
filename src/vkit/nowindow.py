"""Windows launch options for console-free child processes. Each launch site must apply these options; a parent flag does not configure every descendant launch."""
from __future__ import annotations

import subprocess
import sys
from typing import Any

__all__ = ["NO_WINDOW", "hidden_window", "hide_startup", "is_windows"]

is_windows = sys.platform == "win32"

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0) if is_windows else 0


def hidden_window() -> dict[str, Any]:
    """`subprocess` keywords that keep a launched child off the operator's screen.

    Empty on every non-Windows host, so a call site spreads this unconditionally
    and pays nothing off Windows.

    Both keywords are needed on Windows, and they are not redundant. The creation
    flag stops the console being allocated at all. The startup info stops a host
    that honours `STARTUPINFO` from showing one anyway, and it is the half that
    also covers a child that allocates a console for its own descendants.
    """
    if not is_windows:
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": NO_WINDOW, "startupinfo": startup}


def hide_startup(startup: Any) -> None:
    """Add the show-window flags to a `STARTUPINFO` the caller already built.

    A `win32process.CreateProcess` call assembles its own STARTUPINFO because it
    has to hand over inheritable std handles, and this is that object's half of
    the decision. It sets the bits in place rather than replacing `dwFlags`,
    because the caller has usually already set `STARTF_USESTDHANDLES` and
    overwriting it would detach the child's output from the run's log files.

    `wShowWindow` is set explicitly rather than left at the struct's zero
    default. The default happens to be `SW_HIDE`, so leaving it would work on
    today's layout, and invisibility would then depend on an initialiser nobody
    wrote a test against.
    """
    if not is_windows:
        return
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE

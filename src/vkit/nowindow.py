"""Windows `subprocess` options that keep child processes from opening a console window. Each
launch site applies them itself.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

__all__ = ["hidden_window", "is_windows"]

is_windows = sys.platform == "win32"


def hidden_window() -> dict[str, Any]:
    """Keywords to spread into a launch; empty off Windows.

    Both are needed: the creation flag stops console allocation, and the startup
    info covers hosts that show a window anyway.
    """
    if not is_windows:
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}

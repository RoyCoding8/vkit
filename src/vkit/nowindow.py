"""Say, once, that a process this code launches should not appear on screen.

A console application launched from a process that has no console of its own is
given a new one, and on Windows that new console is a window over whatever the
operator was using. Nothing about the command is unusual: `git --version` and
`python -m pytest` are ordinary console programs. What is unusual is the parent.
A CLI started from a terminal already has a console and the child inherits it, so
nothing appears. The same CLI started by a double-click, by a GUI host, or by a
service has no console, and every child it launches opens a window. So a run of
a real check flashes across the screen, and the operator sees the tool fighting
itself.

**The flag belongs to whoever creates a process.** A child created with
`CREATE_NO_WINDOW` is given no console, so the processes it launches in turn
inherit nothing and stay invisible too. Fixing only the outermost launch hides
the whole subtree; fixing an inner one hides a single process and leaves the
rest flashing. That is why this is a module both the product and its test suite
can reach rather than a keyword copied to each call site, where the N+1th site
forgets it.

Two halves, because Windows has two ways to say it. `subprocess` takes the
keywords as a dict, which is `hidden_window()`. `win32process.CreateProcess`
takes a creation flag and a STARTUPINFO it builds itself, which is `NO_WINDOW`
and `hide_startup()`. Both express one decision, so a call site chooses by the
API it is calling rather than by remembering which words this launch needs.

`tests/test_subprocess_windows.py` walks the AST of `src/vkit/` and fails if a
module reaches for a launcher directly, so the next call site cannot be added
without this file. That check is why the two halves exist side by side instead
of one of them growing a second spelling.

Off Windows `hidden_window()` returns an empty dict, so the `subprocess` half
adds nothing there, and `NO_WINDOW` is 0. The Windows-only half is reached only
from code already gated on `IS_WINDOWS`, so it is never built on POSIX.
`subprocess.STARTUPINFO` does not exist on POSIX at all, which is why
`hidden_window()` checks the platform rather than building the struct
unconditionally and failing on the first call in a Linux CI run.

The test suite carries its own copy of `hidden_window()` in `tests/subproc.py`
rather than importing this one, and the duplication is deliberate. `conftest.py`
imports `subproc` before it points `sys.path` at the checkout under test, so a
`subproc` that imported `vkit` would bind the package to whichever tree the
interpreter found first, and a worktree worker would run the suite against the
main checkout's code. Measured, not assumed: with the import at that position,
`vkit.paths` resolves to the main `src` and the worktree's own copy is never
loaded. The second reason is that this file is the thing under test. A test that
asserts the keywords cannot be asserting them about the implementation it is
checking.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

__all__ = ["NO_WINDOW", "hidden_window", "hide_startup", "is_windows"]

#: Whether this host has the Windows process-creation behaviour at all. The
#: product gates its Windows paths on the same question in `procidentity`, and
#: two spellings of one fact is how they drift apart.
is_windows = sys.platform == "win32"

#: The `dwCreationFlags` bit that allocates no console for the child. 0 off
#: Windows, so OR-ing it into a flag word there changes nothing.
#:
#: Microsoft documents this as ignored when the same call also passes
#: `CREATE_NEW_CONSOLE` or `DETACHED_PROCESS`, which is why `supervisor.py`
#: passes it nowhere: that launch is already console-free by design and adding
#: the bit would assert something the kernel does not do. That note is from the
#: documentation; the runtime behaviour of the flag pair is not measured here.
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
    return {
        "creationflags": NO_WINDOW,
        "startupinfo": startup,
    }


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

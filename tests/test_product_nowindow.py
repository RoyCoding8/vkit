"""The product must not launch a process without saying to keep it off screen.

The test suite got this treatment already, in `test_subprocess_windows.py`. That
test found a real defect: running the suite covered the operator's screen with
console windows. The product had the same defect and nobody had looked, because
a helper nobody is required to use is a convention, and a convention nobody
checks stops holding at the next call site.

**The rule.** Every process `src/vkit/` creates is launched through
`vkit.nowindow`, which is the one place the Windows keywords are written. A
`subprocess` call spreads `hidden_window()`; a `win32process.CreateProcess` call
builds its STARTUPINFO and then calls `hide_startup()` on it.

**Why the two are separate functions.** They express one decision through two
APIs. `subprocess` takes a keywords dict, and `win32process.CreateProcess` takes
a creation flag plus a STARTUPINFO the caller assembles itself, because it has
to hand over inheritable std handles. Folding them into one function would mean
one of them takes arguments the other does not have, and every call site would
carry a branch saying which shape it needs. `procs.py` uses the raw API because
it is the containment path: the child is created suspended, assigned to a job
object, and only then resumed, and that ordering is not something
`subprocess.Popen` can express.

**Why this is a ratchet, not a flat ban.** Three sites are allowed to launch
directly, and each is allowed for a stated reason recorded in `ALLOWANCE` with a
count that must match exactly. Two are the POSIX halves of a module that
branches on `IS_WINDOWS`, and they use `start_new_session=True`; on POSIX there
is no console to suppress, so the keywords would be meaningless there. The
third is `supervisor.py`, which passes `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
because the supervisor has to survive its client exiting and must not be in the
client's console group. That launch is already console-free by design, and
Microsoft documents `CREATE_NO_WINDOW` as ignored when the same call also passes
`DETACHED_PROCESS`, so adding the bit would assert something the kernel does not
do. A call site that outgrows its reason must be raised here deliberately, in
writing, which is the point.

Both failure modes are new code reaching past the mechanism: a module not in the
table at all, or a module with more direct launches than its entry allows. A
module *dropping* below its allowance fails too, so an entry that no longer
matches reality is corrected rather than left as a stale number nobody trusts.

The walk reads calls, not text. Several tests here and in the policy suite write
source strings and then execute them, and a name inside a string is not a call.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from vkit import nowindow

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "vkit"

#: The `subprocess` entry points that create a process. `check_call` and `call`
#: route through `run` internally and would be equally fatal without the
#: keywords, so the list covers the whole surface rather than the wrappers
#: happen to use.
LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

#: The `os` entry points that create a process through a shell. None appear in
#: `src/` today; the list is here so adding one is caught rather than missed.
OS_LAUNCHERS = frozenset({
    "system", "popen", "execv", "execve", "execl", "execlp", "execle",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
    "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
})

#: The raw Win32 calls that create a process. A module reaching for one of these
#: is the `procs.py` shape, so it is counted and its reason recorded rather than
#: banned outright.
WIN32_CREATE = frozenset({"CreateProcess", "CreateProcessA", "CreateProcessW"})

#: The modules allowed to create a process without going through `nowindow`, and
#: exactly how many times each may. The count is compared with `==`, not `<=`, so
#: lowering an entry is part of using it up.
ALLOWANCE: dict[str, int] = {
    # Two POSIX halves of a module that branches on IS_WINDOWS. `start_new_session`
    # is the POSIX ownership mechanism and is unrelated to console visibility; on
    # POSIX there is no console to suppress.
    "procs.py": 2,
    # One function with two Popens, one per platform. The Windows one passes
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so the supervisor outlives its
    # client and is not in the client's console group, which is already
    # console-free; the POSIX one is `start_new_session`, its counterpart. Neither
    # wants CREATE_NO_WINDOW: Microsoft documents it as ignored alongside
    # DETACHED_PROCESS, so setting it would assert something the kernel does not
    # do. That reading is from the documentation, not measured here.
    "supervisor.py": 2,
}

#: The `nowindow` module itself, which is where the keywords are written.
EXEMPT = frozenset({"nowindow.py"})


def _imports(tree: ast.AST) -> dict[str, str]:
    """Local name -> the module it was bound to, for `import x` and `from x import y`.

    A module may write `import subprocess as sp` or `from subprocess import run`,
    and either spelling is the same defect as `subprocess.run`. Resolving the
    binding is what makes this check survive a rename instead of being satisfied
    by accident.
    """
    names: dict[str, str] = {}
    for child in ast.walk(tree):
        if isinstance(child, ast.Import):
            for alias in child.names:
                names[alias.asname or alias.name] = alias.name
        elif isinstance(child, ast.ImportFrom) and child.module:
            for alias in child.names:
                names[alias.asname or alias.name] = f"{child.module}.{alias.name}"
    return names


def _direct_launches(source: str, filename: str) -> list[tuple[int, str]]:
    """Every process-creating call that does not go through `nowindow`.

    A call routed through the helper is not a defect, so a call whose keywords
    are `hidden_window()` is not reported. The check is on the call, not on the
    file: a module may launch directly and spread the helper's dict, and a module
    may import the helper and spread it badly, and only the first is a
    concurrency problem with the next call site.
    """
    tree = ast.parse(source, filename=filename)
    bound = _imports(tree)
    found: list[tuple[int, str]] = []
    scopes = _function_scopes(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name):
            continue
        module = bound.get(owner.id, "")
        leaf = f"{module}.{node.func.attr}" if module else node.func.attr

        if module == "subprocess" and node.func.attr in LAUNCHERS:
            label = f"subprocess.{node.func.attr}"
        elif module == "os" and node.func.attr in OS_LAUNCHERS:
            label = f"os.{node.func.attr}"
        elif module.startswith("win32process") and node.func.attr in WIN32_CREATE:
            label = f"{owner.id}.{node.func.attr}"
        else:
            continue

        if _is_routed(node, bound, scopes.get(node.lineno, tree)):
            continue
        found.append((node.lineno, label))
    return sorted(found)


def _function_scopes(tree: ast.AST) -> dict[int, ast.AST]:
    """Map a statement's line to the innermost function containing it.

    The innermost is the one that matters, because it is the one that decides
    how a process it creates is launched. A module-level launch has no function
    and is looked at against the module itself.
    """
    scopes: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            lineno = getattr(child, "lineno", None)
            if lineno is None:
                continue
            if lineno not in scopes or node.lineno > scopes[lineno].lineno:
                scopes[lineno] = node
    return scopes


def _is_routed(node: ast.Call, bound: dict[str, str], scope: ast.AST) -> bool:
    """Whether this launch carries the decision `vkit.nowindow` makes.

    Two shapes reach the same decision, and they are visible differently.

    A `subprocess` call spreads `hidden_window()` into its own keywords, so the
    reference is inside the call and the call is self-evidently routed. A call
    that does not is a defect, and is a defect even in a function that routed a
    different call: the function decided once about a process it named, not about
    every process that later appears beside it.

    A `win32process.CreateProcess` call cannot carry the reference itself. The
    STARTUPINFO it is handed was built by the caller, so `hide_startup` ran on a
    statement above and the flag arrives as a bare name in the creation-flags
    word. So the argument passed where the STARTUPINFO goes is followed back to
    the call that was made on it. That is a name, not a type: a `hide_startup`
    applied to one STARTUPINFO says nothing about a second one built later in the
    same function, which is why the identity of the object is checked rather
    than the presence of any routed call nearby.

    A name is matched against the binding it resolves to, so an unrelated local
    called `hidden_window` is not mistaken for a route.
    """
    aliases = {
        name for name, module in bound.items() if module.split(".")[-1] == "nowindow"
    }
    routed_attrs = {"hidden_window", "NO_WINDOW", "hide_startup"}
    bare = {"hidden_window", "NO_WINDOW"}

    def names_in(tree: ast.AST) -> set[str]:
        return {
            child.id for child in ast.walk(tree)
            if isinstance(child, ast.Name) and child.id in bare
        }

    # The subprocess shape: the keywords are in the call.
    if names_in(node):
        return True
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr in bare
            and isinstance(child.value, ast.Name)
            and (child.value.id in aliases or child.value.id == "nowindow")
        ):
            return True

    # The Win32 shape: find the STARTUPINFO argument and its hide_startup call.
    startups = {arg.id for arg in node.args if isinstance(arg, ast.Name)}
    if not startups:
        return False
    for call in ast.walk(scope):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = None
        if isinstance(func, ast.Name) and func.id in routed_attrs and func.id in aliases:
            name = func.id
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in routed_attrs
            and isinstance(func.value, ast.Name)
            and (func.value.id in aliases or func.value.id == "nowindow")
        ):
            name = func.attr
        if name is None or not call.args:
            continue
        target = call.args[0]
        if isinstance(target, ast.Name) and target.id in startups:
            return True
    return False


def _source_modules() -> list[Path]:
    """Every module under `src/vkit/`, which is the shipped product.

    A directory that is not product code is skipped rather than walked, and
    `__pycache__` never appears, so the census cannot depend on which tests ran.
    """
    return sorted(
        path
        for path in SRC_DIR.rglob("*.py")
        if path.name not in EXEMPT
        and not any(
            part == "__pycache__" or part.startswith(".")
            for part in path.relative_to(SRC_DIR).parts
        )
    )


def _observed() -> dict[str, list[tuple[int, str]]]:
    return {
        path.name: _direct_launches(path.read_text(encoding="utf-8"), str(path))
        for path in _source_modules()
    }


def test_no_product_module_launches_a_new_process_directly() -> None:
    """Every new process launch under `src/vkit/` goes through `vkit.nowindow`.

    A `subprocess.run` that spreads `hidden_window()` is what this demands. On
    Windows each launch that does not is a console window over the operator's
    screen, and a run of a real check spawns `git` and an interpreter several
    times over.
    """
    observed = {name: sites for name, sites in _observed().items() if sites}

    unlisted = sorted(
        f"{name}:{lineno} {text}"
        for name, sites in observed.items() if name not in ALLOWANCE
        for lineno, text in sites
    )
    drifted = sorted(
        f"{name}: allowed {ALLOWANCE[name]}, found {len(observed[name])}"
        for name in ALLOWANCE
        if name in observed and len(observed[name]) != ALLOWANCE[name]
    )
    stale = sorted(
        f"{name}: allowed {ALLOWANCE[name]}, found {len(observed.get(name, []))}"
        for name in ALLOWANCE
        if name not in observed
    )

    assert not unlisted, (
        "these create a process without going through vkit.nowindow, so on Windows "
        "each one puts a console window on the operator's screen. Spread "
        f"nowindow.hidden_window() into the call:\n" + "\n".join(unlisted)
    )
    assert not drifted, (
        "these modules no longer match their recorded allowance. If the launch was "
        "routed through the helper, lower the number; if it is a new direct launch, "
        "record why here in writing:\n" + "\n".join(drifted)
    )
    assert not stale, (
        "these files no longer appear in the census, so their allowance should be "
        f"deleted:\n" + "\n".join(stale)
    )


def test_the_allowance_covers_every_known_exemption() -> None:
    """No module is silently exempt, and the table is not wider than the problem.

    Without this, an entry left behind after a launch was routed would sit here
    forever, and a ratchet nobody checks is a rubber stamp.
    """
    observed = {name: sites for name, sites in _observed().items() if sites}
    assert set(observed) == set(ALLOWANCE), (
        f"census and allowance disagree: only in the census "
        f"{sorted(set(observed) - set(ALLOWANCE))}, only in the allowance "
        f"{sorted(set(ALLOWANCE) - set(observed))}"
    )


def test_the_helper_carries_the_windows_keywords_here() -> None:
    """On this host the helper is what sets both keywords.

    Written to assert on whichever platform runs it rather than to skip, so the
    suite carries a positive claim about the behaviour this whole unit exists
    for. Off Windows there is nothing to claim, and the assertion is that the
    helper adds nothing, because `subprocess.STARTUPINFO` does not exist there
    and a helper that built one would raise on the first call in a CI run.
    """
    keywords = nowindow.hidden_window()
    if sys.platform == "win32":
        assert keywords["creationflags"] == subprocess.CREATE_NO_WINDOW
        startup = keywords["startupinfo"]
        assert startup.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert startup.wShowWindow == subprocess.SW_HIDE
    else:
        assert keywords == {}
        assert nowindow.NO_WINDOW == 0
        assert not hasattr(subprocess, "STARTUPINFO"), (
            "this host is POSIX and still exposes STARTUPINFO, so the assertion "
            "above is proving nothing about the POSIX shape"
        )


def test_hide_startup_keeps_the_handles_the_caller_set() -> None:
    """`hide_startup` adds its bits and does not overwrite the caller's.

    `procs.py` sets `STARTF_USESTDHANDLES` before calling this, because the
    child's output has to land in the run's log files. An implementation that
    assigned `dwFlags` instead of OR-ing would detach the child's stdout and the
    run would report an empty result while the check succeeded.
    """
    if sys.platform != "win32":
        pytest.skip("STARTUPINFO is a Windows structure; the POSIX half adds nothing")
    import win32con
    import win32process

    startup = win32process.STARTUPINFO()
    startup.dwFlags = win32con.STARTF_USESTDHANDLES
    before = startup.dwFlags
    nowindow.hide_startup(startup)
    assert startup.dwFlags & win32con.STARTF_USESTDHANDLES, (
        "hide_startup overwrote dwFlags, so a caller's std handles would be dropped"
    )
    assert startup.dwFlags & win32con.STARTF_USESHOWWINDOW
    assert startup.wShowWindow == win32con.SW_HIDE
    assert startup.dwFlags != before


def test_hide_startup_adds_nothing_off_windows() -> None:
    """Off Windows `hide_startup` is a no-op rather than an error.

    A test asserts this with a stand-in object, so the assertion is about this
    function's own behaviour and not about whether the host could run the
    Windows path at all.
    """
    if sys.platform == "win32":
        pytest.skip("this asserts the POSIX shape, which test above covers on Windows")

    class Stand:
        dwFlags = 0
        wShowWindow = 0

    stand = Stand()
    nowindow.hide_startup(stand)
    assert (stand.dwFlags, stand.wShowWindow) == (0, 0)

"""Require every test subprocess call to use the hidden-window helper. Source strings used as test fixtures are checked by their own execution tests."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import subproc

TESTS_DIR = Path(__file__).resolve().parent

#: The entry points that create a process. `check_call` and `call` go through
#: `run` internally and would be equally fatal without the keywords, so the list
#: covers the whole surface rather than the two wrappers that happen to be used.
LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

#: The wrapper module itself, and this file. Both are where the keywords are
#: written, so both are the one place a direct call is correct.
EXEMPT = frozenset({"subproc.py", "test_subprocess_windows.py"})

def _imports_subprocess(node: ast.AST) -> str | None:
    """The local name `subprocess` is bound to in this module, or None.

    A module may do `import subprocess as sp`, and a call written `sp.run(...)`
    is the same defect as `subprocess.run(...)`. Resolving the alias is what
    makes this check survive a rename.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                if alias.name == "subprocess":
                    return alias.asname or alias.name
    return None


def _direct_launches(source: str, filename: str) -> list[tuple[int, str]]:
    """Every direct `subprocess` launch in one module, as `(lineno, text)`."""
    tree = ast.parse(source, filename=filename)
    alias = _imports_subprocess(tree)
    if alias is None:
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in LAUNCHERS:
            continue
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id == alias:
            found.append((node.lineno, f"{alias}.{func.attr}"))
    return sorted(found)


def _test_modules() -> list[Path]:
    """Every module under `tests/`, which is where the suite's own code lives.

    A directory that is not test code is skipped rather than walked. `rglob`
    would otherwise descend into a scratch tree a test left behind and parse
    whatever it found, so the census could depend on which tests ran first.
    """
    return sorted(
        path
        for path in TESTS_DIR.rglob("*.py")
        if path.name not in EXEMPT
        and not any(
            part == "__pycache__" or part.startswith(".")
            for part in path.relative_to(TESTS_DIR).parts
        )
    )


def _observed() -> dict[str, list[tuple[int, str]]]:
    return {
        path.name: _direct_launches(path.read_text(encoding="utf-8"), str(path))
        for path in _test_modules()
    }


def test_no_test_module_launches_a_new_process_directly() -> None:
    """Every *new* process launch in `tests/` goes through `subproc`.

    The AST walk is what makes this worth having. Reading the text for
    `subprocess.run` cannot tell a call from the string a test asserts on, and
    the string cases are exactly the interesting ones: several tests here and in
    the policy suite write source that they then execute.
    """
    observed = {name: sites for name, sites in _observed().items() if sites}

    assert not observed, (
        "All test subprocesses must use subproc.run or subproc.popen: "
        + repr(observed)
    )


def test_standalone_verification_helpers_hide_their_children() -> None:
    root = TESTS_DIR.parent
    paths = [*root.joinpath("formal").glob("*.py"),
             root / "scripts/acceptance.py", root / "scripts/acceptance02.py"]
    missing = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess" and node.func.attr in LAUNCHERS):
                if not any(k.arg is None and isinstance(k.value, ast.Name)
                           and k.value.id == "_WINDOW_OPTIONS" for k in node.keywords):
                    missing.append((path.name, node.lineno))
    assert missing == []


def test_the_helper_actually_launches() -> None:
    """The wrappers are wired to `subprocess`, not a reimplementation of it.

    Without this the check above would still pass if `subproc.run` were a stub,
    and the suite would silently stop launching anything.
    """
    done = subproc.run([sys.executable, "-c", "print('launched')"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "launched"

    proc = subproc.popen([sys.executable, "-c", "pass"])
    assert proc.wait(timeout=60) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="asserts the non-Windows shape")
def test_the_helper_adds_nothing_off_windows() -> None:
    """Off Windows the helper passes no `creationflags` and no `startupinfo`.

    Not a style rule. `subprocess.STARTUPINFO` does not exist on POSIX, so a
    helper that built one unconditionally would raise `AttributeError` on the
    first call on Linux or macOS -- and only on the first call, which is the
    worst way to find out.
    """
    assert subproc.hidden_window() == {}
    assert not hasattr(subprocess, "STARTUPINFO"), (
        "this host is POSIX and still exposes STARTUPINFO; the skip above is "
        "misleading and the assertion below proves nothing"
    )
    done = subproc.run([sys.executable, "-c", "print('posix')"], capture_output=True, text=True)
    assert done.stdout.strip() == "posix"


def test_the_helper_carries_the_windows_keywords_here() -> None:
    """On Windows the helper is what sets both keywords.

    Run on this host, whatever it is, so the suite has a positive assertion for
    the behaviour the whole unit exists for rather than only a negative one.
    """
    if sys.platform != "win32":
        pytest.skip("the keywords are Windows-only; the POSIX shape is asserted above")
    keywords = subproc.hidden_window()
    assert keywords["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert keywords["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW

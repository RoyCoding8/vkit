"""The acceptance number has to mean the tree it was run in, and these tests are
the only reason anyone can believe it.

`scripts/acceptance.py` used to name its command as `sys.executable`'s sibling
`vkit.exe`. That is the virtualenv, and a virtualenv shared with another worker
holds an editable install pointing at somebody else's `src`, so every row could
pass against code this revision does not contain. Nothing about the output would
have said so: 22 of 22 rows either way.

The tests here cover the four ways that could still go wrong.

  * The command the rows run and the path their children are given both name
    this tree, asked of a real spawned child rather than assumed.
  * The script names the tree in its own output, so a green count says where it
    came from rather than asking the reader to trust it.
  * A broken tree produces a failing row, so "the number means this tree" does
    not degenerate into "the number is always 22".
  * The pre-fix resolution is run directly and shown landing on another tree.
    That measurement is what makes the three assertions above evidence rather
    than a description of intent.

The script itself refuses to report a count when its rows would import another
tree, and the last test covers that refusal. A reviewer running the script in
the wrong place is stopped rather than handed a green number.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "acceptance.py"
THIS_CLI = (REPO_ROOT / "src" / "vkit" / "cli.py").resolve()
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

sys.path.insert(0, str(SCRIPT.parent))

import acceptance  # noqa: E402


def run_script(script: Path, *, env: dict[str, str] | None = None,
               timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True,
        timeout=timeout, cwd=REPO_ROOT, env=env or {**os.environ},
    )


def declared_environment() -> dict[str, str]:
    """The environment the script builds for a row, read from the script itself.

    Asking the script rather than repeating its construction here is what keeps
    this file from asserting that its own idea of the environment is the script's.
    """
    return dict(acceptance.CHILD_ENV)


def test_the_rows_run_this_tree_not_the_installed_one() -> None:
    """The command is `-m` against this tree's `src`, not a neighbouring install.

    `-m` matters as much as the path. A console script beside the interpreter is
    a launcher for whatever `vkit` is installed there, so naming it would reopen
    the gap one layer up.
    """
    assert acceptance.COMMAND == [sys.executable, "-m", "vkit.cli"], (
        "the rows must run this tree's module by path, not a console script "
        "whose launcher belongs to whichever venv is on PATH"
    )
    assert Path(declared_environment()["PYTHONPATH"]).resolve() == (
        REPO_ROOT / "src"
    ).resolve(), (
        "a child's PYTHONPATH must name this checkout's src, or an editable "
        "install elsewhere wins the import"
    )


def test_a_row_would_import_this_tree() -> None:
    """The strongest available claim: spawn a row's environment and ask.

    The script's own guard compares this answer against `ROOT/src`, so asserting
    it here asserts the comparison the guard will make.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import vkit.cli; print(vkit.cli.__file__)"],
        capture_output=True, text=True, timeout=120, env=declared_environment(),
    )
    assert probe.returncode == 0, probe.stderr
    assert Path(probe.stdout.strip()).resolve() == THIS_CLI, (
        f"a row would import {probe.stdout.strip()}, not this tree's {THIS_CLI}"
    )


def test_the_script_names_the_tree_it_ran_in() -> None:
    """A reader has to be able to see which checkout produced the count."""
    done = run_script(SCRIPT)
    assert done.returncode == 0, done.stdout + done.stderr
    assert str(THIS_CLI) in done.stdout, (
        "the script must print the module its rows imported, so a green count "
        "names the tree it came from"
    )
    assert "22/22 acceptance rows pass" in done.stdout


def test_a_broken_tree_yields_a_failing_row() -> None:
    """The other direction. Without this, "the number means this tree" would be
    indistinguishable from "the number is always 22".

    The defect is introduced into a copy of the example the script walks, not
    into this repository, and the run is given the broken copy as its tree. The
    arithmetic the example is supposed to get right is the one that is wrong, so
    the row that watches for a defect has to fail rather than pass.
    """
    broken_root = REPO_ROOT.parent / f"vkit-acceptance-broken-{os.getpid()}"
    shutil.copytree(REPO_ROOT, broken_root, ignore=shutil.ignore_patterns(
        ".git", ".venv", "tmp", "formal", ".pytest_cache", "__pycache__",
    ))
    try:
        totals = broken_root / "examples" / "python-cli" / "src" / "totals.py"
        good = totals.read_text(encoding="utf-8")
        assert "running += amount" in good, "the example no longer has the line under test"
        totals.write_text(
            good.replace("running += amount", "running += amount + 1"), encoding="utf-8"
        )
        done = run_script(broken_root / "scripts" / "acceptance.py")
        assert done.returncode == 1, (
            f"the script exited {done.returncode} against a broken tree; "
            f"a count that cannot fall is a document"
        )
        assert "22/22 acceptance rows pass" not in done.stdout
        assert "Introduced arithmetic defect" in done.stdout
        assert "FAIL" in done.stdout
    finally:
        shutil.rmtree(broken_root, ignore_errors=True)


def test_the_reverted_resolution_reaches_another_tree() -> None:
    """The gap itself, measured rather than described.

    The pre-fix command was `Path(sys.executable).parent / "vkit.exe"`, which
    always names the virtualenv rather than the tree. Running exactly that, with
    this checkout's `src` taken out of the environment, shows what a row would
    have imported. This is what makes the fix's `COMMAND` assertion above
    evidence: the old expression demonstrably resolves somewhere else.
    """
    launcher = Path(sys.executable).parent / "vkit.exe"
    if not launcher.is_file():
        pytest.skip(f"no console script beside {sys.executable} to compare against")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    probe = subprocess.run(
        [str(launcher), "--help"], capture_output=True, text=True, timeout=180, env=env,
    )
    assert probe.returncode == 0, probe.stderr

    # The launcher is a shim; the tree it reaches is the one the shim's module
    # search puts first. Ask the interpreter that owns the venv.
    reached = subprocess.run(
        [sys.executable, "-c", "import vkit.cli; print(vkit.cli.__file__)"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert reached.returncode == 0, reached.stderr
    reached_path = Path(reached.stdout.strip()).resolve()

    if reached_path == THIS_CLI:
        pytest.skip(
            f"the virtualenv's vkit is installed from this tree ({THIS_CLI}), so "
            f"there is no second checkout for the old resolution to reach"
        )
    # And the fixed script still reports its own tree, which is the difference.
    fixed = run_script(SCRIPT)
    assert str(THIS_CLI) in fixed.stdout


def test_a_script_with_no_tree_of_its_own_refuses_rather_than_counting() -> None:
    """The guard, exercised.

    The command cannot be diverted from the environment any more, because the
    script builds the child's `PYTHONPATH` itself. The guard is the backstop for
    the case that is still reachable: the script sitting somewhere its own `src`
    is not present, so a child would silently import whatever else answers to
    `vkit`. Copying the script out of the repository reproduces exactly that, and
    the answer has to be a refusal with the two trees named.
    """
    elsewhere = REPO_ROOT.parent / f"vkit-detached-{os.getpid()}"
    (elsewhere / "scripts").mkdir(parents=True)
    try:
        detached = elsewhere / "scripts" / SCRIPT.name
        shutil.copy2(SCRIPT, detached)
        shutil.copytree(EXAMPLE, elsewhere / "examples" / "python-cli")

        done = run_script(detached)
        assert done.returncode == 4, (
            f"a script with no src tree of its own exited {done.returncode}. "
            f"It would have counted rows against somebody else's vkit."
        )
        assert "acceptance rows pass" not in done.stdout
        assert "not" in done.stderr, "the refusal has to say what it would not test"
        assert str(elsewhere / "src" / "vkit" / "cli.py") in done.stderr, (
            "the refusal has to name the tree it expected and the one it found"
        )
    finally:
        shutil.rmtree(elsewhere, ignore_errors=True)


def test_no_row_uses_a_command_resolved_outside_the_tree() -> None:
    """No second resolution path is hiding elsewhere in the script.

    Parsed rather than grepped, because the module docstring legitimately names
    the environment-relative resolution it is refusing. Only executable
    statements are evidence, so only they are read.
    """
    import ast

    calls = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
    ]
    for forbidden in ("shutil.which", "os.getenv", "VIRTUAL_ENV", "Scripts"):
        assert not [c for c in calls if forbidden in c], (
            f"acceptance.py calls {forbidden!r} somewhere; the command under test "
            f"has to come from this tree and nowhere else"
        )

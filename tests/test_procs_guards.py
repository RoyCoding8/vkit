"""The pre-flight refusals in `procs`, called the way a caller calls them.

Every other test of this module drives a real launch. These drive the three
inputs that must be refused before anything is created, because a refusal that
has no check behind it is the kind that quietly stops being one: nothing here
starts a process, so each test is fast, and each asserts the exception type, a
fragment of the message, and the side effect that must NOT have happened.

The `procs` under test is this checkout's, asserted at import for the same
reason `test_procs.py` does it: a gate that silently measures another owner's
code is a false pass.
"""
from __future__ import annotations
import subproc

import sys
from pathlib import Path

import pytest

_THIS_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()
sys.path.insert(0, str(_THIS_SRC))

import vkit.procs as _procs  # noqa: E402

if Path(_procs.__file__).resolve() != (_THIS_SRC / "vkit" / "procs.py"):
    raise AssertionError(
        f"vkit.procs resolved to {_procs.__file__}, not this worktree's "
        f"{_THIS_SRC / 'vkit' / 'procs.py'}. Refusing to run the gate against "
        "another owner's code."
    )

LaunchError = _procs.LaunchError
launch = _procs.launch
run_command = _procs.run_command


def _both_entry_points():
    """`run_command` and `launch` share one pre-flight, so both get checked.

    A guard written twice is a rule with two authorities and no way to tell
    which one is current. Parametrising both is what makes the second copy
    disappear if a future edit puts one back.
    """
    return pytest.mark.parametrize(
        "entry", [run_command, launch], ids=["run_command", "launch"]
    )


@_both_entry_points()
def test_empty_argv_is_refused(entry, tmp_path: Path) -> None:
    """Nothing to launch. No log files, because nothing was begun."""
    with pytest.raises(LaunchError) as caught:
        entry(
            [],
            cwd=tmp_path,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout_seconds=30.0,
        )

    assert "argv must name at least one executable" in str(caught.value)
    assert not (tmp_path / "out.log").exists(), (
        "a refusal that had already created the log directory created it before "
        "refusing; the pre-flight has to come first"
    )
    assert not (tmp_path / "err.log").exists()


@_both_entry_points()
def test_a_missing_working_directory_is_refused(entry, tmp_path: Path) -> None:
    """Reachable from a manifest.

    `manifest._resolve_cwd` resolves a repository-relative `cwd` and checks only
    that it stays inside the root, so a manifest can name a directory inside the
    repository that was never created. That parses cleanly and arrives here.
    """
    missing = tmp_path / "never-created"
    with pytest.raises(LaunchError) as caught:
        entry(
            [sys.executable, "-c", "pass"],
            cwd=missing,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout_seconds=30.0,
        )

    assert "working directory does not exist" in str(caught.value)
    assert str(missing) in str(caught.value), (
        "the refusal has to name the path; a caller reading only the message "
        "cannot tell which of several directories was rejected"
    )


@_both_entry_points()
def test_a_file_where_the_working_directory_belongs_is_refused(entry, tmp_path: Path) -> None:
    """`cwd` has to be a directory, and `is_dir` is what decides that.

    A regular file passes a naive truthiness check and fails at `CreateProcess`
    with an OSError that names neither this module's contract nor its own. The
    counterexample matters because the check a reader would assume is in place
    is `exists()`, and that one accepts a file.
    """
    not_a_directory = tmp_path / "a-file"
    not_a_directory.write_text("payload", encoding="utf-8")

    with pytest.raises(LaunchError, match="working directory does not exist"):
        entry(
            [sys.executable, "-c", "pass"],
            cwd=not_a_directory,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout_seconds=30.0,
        )


@pytest.mark.parametrize("timeout", [0.0, -1.0, -0.001])
@_both_entry_points()
def test_a_non_positive_timeout_is_refused(entry, timeout: float, tmp_path: Path) -> None:
    """Zero and negative are the half of the guard that has a producer.

    `schemas/manifest.v1.json` sets `exclusiveMinimum: 0` on `timeout_seconds`
    and `manifest.parse_manifest` re-checks `0 < timeout <= MAX_TIMEOUT_SECONDS`
    at `manifest.py:346`, so a manifest cannot carry one. The refusal still has
    to hold, because `launch` and `run_command` are called directly and a
    `JobLease` can be hand-built with `timeout_seconds=0.0`, which is the
    dataclass default at `procs.py:165`.
    """
    with pytest.raises(LaunchError) as caught:
        entry(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout_seconds=timeout,
        )

    assert "timeout must be a positive finite number of seconds" in str(caught.value)
    assert repr(timeout) in str(caught.value)


@_both_entry_points()
def test_an_infinite_timeout_is_refused(entry, tmp_path: Path) -> None:
    """Infinity has no producer anywhere in the repo.

    A grep for `math.inf` and `float("inf")` across every `.py` returns these
    two lines and no others. It is not reachable through a manifest either:
    `maximum: 86400` in the schema and `0 < timeout <= MAX_TIMEOUT_SECONDS` in
    `manifest.py:346` both refuse it first. What it would do if it got through
    is the reason the guard stays: on Windows `_wait_windows` computes
    `int(max(MIN_WAIT_SECONDS, timeout_seconds) * 1000)`, and an unbounded
    value passed to `WaitForSingleObject` is the INFINITE the constant's own
    comment names -- a wait with no deadline at all, which is not a long timeout.
    """
    with pytest.raises(LaunchError) as caught:
        entry(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout_seconds=float("inf"),
        )

    assert "timeout must be a positive finite number of seconds" in str(caught.value)
    assert "inf" in str(caught.value)


@_both_entry_points()
def test_a_valid_call_passes_the_preflight(entry, tmp_path: Path) -> None:
    """The control. Without it the five refusals above would also pass if the
    pre-flight refused everything, including a real interpreter that exits 0.

    This is the test that makes the others mean something: they are claims about
    which inputs are refused, and this one fixes the boundary between refused
    and accepted.
    """
    script = tmp_path / "ok.py"
    script.write_text("open('ran.txt', 'w').write('ran')\n", encoding="utf-8")

    if entry is launch:
        lease = launch(
            [sys.executable, str(script)],
            cwd=tmp_path,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout_seconds=30.0,
        )
        assert lease.pid is not None
        result = _procs.await_exit(lease)
        lease.close()
    else:
        result = run_command(
            [sys.executable, str(script)],
            cwd=tmp_path,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout_seconds=30.0,
        )

    assert result.launched is True, f"a valid call was refused: {result.detail}"
    assert result.exit_code == 0
    assert result.reason is None
    assert (tmp_path / "ran.txt").read_text(encoding="utf-8") == "ran"


def test_the_missing_cwd_refusal_is_reachable_through_a_manifest(tmp_path: Path) -> None:
    """Proves the guard above is not guarding nothing.

    A manifest is written by a person and is not required to be runnable. The
    other two refusals are unreachable through this path -- `command` is
    `minItems: 1` and `timeout_seconds` is `exclusiveMinimum: 0, maximum: 86400`
    in `schemas/manifest.v1.json` -- but `cwd` has neither a schema constraint
    nor an existence check in `_resolve_cwd`, which only refuses a path that
    escapes the root. A directory *inside* the root that was never created is a
    legal manifest and an illegal working directory, and the two facts have to
    be separated somewhere. This is that somewhere.
    """
    import json
    import subprocess

    from vkit.manifest import parse_manifest

    root = tmp_path / "repo"
    (root / "vkit").mkdir(parents=True)
    subproc.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    manifest_path = root / "vkit.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checks": [
                    {
                        "id": "demo",
                        "command": [sys.executable, "-c", "pass"],
                        "cwd": "never-created",
                        "timeout_seconds": 30,
                        "required_scenarios": ["s1"],
                        "artifact": "result.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    from vkit.paths import open_project

    project = open_project(root)
    manifest = parse_manifest(project, project.runs_root, manifest_path)

    assert manifest.checks["demo"].cwd == (root / "never-created")
    assert not (root / "never-created").exists(), (
        "the fixture stopped being the case it claims to be: the directory now "
        "exists, so this test no longer proves anything about a missing one"
    )

    with pytest.raises(LaunchError, match="working directory does not exist"):
        run_command(
            manifest.checks["demo"].argv,
            cwd=manifest.checks["demo"].cwd,
            stdout_path=root / "out.log",
            stderr_path=root / "err.log",
            timeout_seconds=manifest.checks["demo"].timeout_seconds,
        )

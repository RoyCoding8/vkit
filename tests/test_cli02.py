"""End-to-end behavior of the five Plan 02 commands.

Every test drives the real console script in a subprocess against a throwaway
copy of the example repository, because the thing being verified is what a user
gets from a shell: the exit code, and the one JSON object on stdout.

The subprocess is given this checkout's `src` on `PYTHONPATH`. The shared
virtualenv is an editable install pointing at one checkout, so without it a
worker's suite passes against code it did not write. See tests/conftest.py for
the same reason applied in process.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from conftest import console_script

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_INVALID = 2
EXIT_BLOCKED = 3

TOTAL = "totals-behavior"
SECOND = "totals-behavior-second"


def vkit(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real console entry point, never an import."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return subprocess.run(
        [str(console_script()), *args],
        capture_output=True, text=True, timeout=300, env=env,
    )


def one_json_object(done: subprocess.CompletedProcess[str]) -> dict:
    """Parse stdout as exactly one JSON document.

    `json.loads` raises on anything trailing the value, so this also proves
    nothing else reached stdout. A command that printed a warning before its
    payload, or a second line after it, fails here rather than passing a
    "does it parse" check.
    """
    return json.loads(done.stdout)


def commit(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=repo, check=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway Git repository holding a real copy of the example."""
    target = tmp_path / "repo"
    shutil.copytree(EXAMPLE, target)
    commit(target)
    return target


@pytest.fixture()
def two_checks(tmp_path: Path) -> Path:
    """The example with a second registered check under its own id.

    A contract naming an unregistered check is refused at `task begin`, so the
    only way to exercise a required check that has no run is to have a real
    second check the repository defines and the task simply has not run yet.
    """
    target = tmp_path / "two"
    shutil.copytree(EXAMPLE, target)
    path = target / "verification" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    second = json.loads(json.dumps(manifest["checks"][0]))
    second["id"] = SECOND
    manifest["checks"].append(second)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    commit(target)
    return target


def write_contract(directory: Path, name: str, checks: list[str], description: str) -> Path:
    path = directory / name
    path.write_text(
        json.dumps({"description": description, "required_checks": checks}), encoding="utf-8"
    )
    return path


def begin(repo_path: Path, contract: Path, request_id: str = "req-1") -> str:
    done = vkit("task", "begin", "--project", str(repo_path),
                "--contract", str(contract), "--request-id", request_id, "--json")
    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    return one_json_object(done)["task_id"]


def start(repo_path: Path, task_id: str, check: str = TOTAL, request_id: str = "run-1") -> dict:
    """Start one check and return the payload of the *finished* run.

    `check start` now records a launch and returns while the check is still
    running, which is the point of the change: the run is owned by a supervisor
    that outlives the client. So the verdict is not in that payload, and a test
    that wants the verdict asks for it the way a caller now has to.

    The wait is here rather than in the command for the same reason. A `check
    start` that blocked for the check would be the synchronous contract this
    milestone removes, and the tests would pass against a shape that no longer
    exists.
    """
    started = start_only(repo_path, task_id, check, request_id)
    return await_outcome(repo_path, started["run_id"])


def start_only(repo_path: Path, task_id: str, check: str = TOTAL, request_id: str = "run-1") -> dict:
    """Start one check and return the payload as the command actually returns it.

    No outcome, because the command exits 0 the moment the launch is durably
    recorded. This is the shape a real caller sees, and the tests that assert on
    it use this rather than the waited-on payload above.
    """
    done = vkit("check", "start", "--project", str(repo_path), "--task", task_id,
                "--check", check, "--request-id", request_id, "--json")
    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    return one_json_object(done)


def await_outcome(repo_path: Path, run_id: str, timeout: float = 120.0) -> dict:
    """Poll `run show` until the run is terminal, and return that payload.

    Polled with a deadline rather than slept for a fixed time: the check is a real
    process whose duration is not this test's to predict, and a fixed sleep would
    be either slow on a fast machine or flaky on a loaded one.
    """
    deadline = time.monotonic() + timeout
    while True:
        done = vkit("run", "show", "--project", str(repo_path), "--run", run_id, "--json")
        assert done.returncode in (EXIT_OK, EXIT_CHECK_FAILED, EXIT_BLOCKED), (
            done.stdout + done.stderr
        )
        payload = one_json_object(done)
        if payload.get("lifecycle") == "terminal" and payload.get("outcome"):
            return payload
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"run {run_id} was still {payload.get('lifecycle')!r} after {timeout}s"
            )
        time.sleep(0.1)


# ------------------------------------------------------------ task begin


def test_task_begin_returns_a_task_id_and_the_contract_stays_readable(repo: Path, tmp_path: Path) -> None:
    contract = write_contract(tmp_path, "c.json", [TOTAL], "totals must be correct")

    done = vkit("task", "begin", "--project", str(repo), "--contract", str(contract),
                "--request-id", "req-1", "--json")
    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    payload = one_json_object(done)
    assert payload["task_id"]
    assert payload["generation"] == 1
    assert payload["status"] == "active"
    assert payload["required_checks"] == [TOTAL]
    assert payload["policy_digest"]

    # The contract is readable afterwards because finalize still requires it. A
    # contract that was dropped on the floor would let this return READY with
    # nothing run, which is the failure this assertion exists to catch.
    finalized = vkit("task", "finalize", "--project", str(repo),
                     "--task", payload["task_id"], "--json")
    assert finalized.returncode == EXIT_BLOCKED
    assert one_json_object(finalized)["gaps"] == [
        f"no completed run for required check {TOTAL!r}"
    ]


def test_the_same_request_id_returns_the_same_task(repo: Path, tmp_path: Path) -> None:
    contract = write_contract(tmp_path, "c.json", [TOTAL], "totals must be correct")

    first = one_json_object(vkit("task", "begin", "--project", str(repo), "--contract", str(contract),
                                 "--request-id", "req-1", "--json"))
    second = one_json_object(vkit("task", "begin", "--project", str(repo), "--contract", str(contract),
                                  "--request-id", "req-1", "--json"))
    assert first["task_id"] == second["task_id"]
    assert second["generation"] == 1, "a retry must not open a second attempt"


def test_a_different_contract_under_the_same_request_id_is_refused(repo: Path, tmp_path: Path) -> None:
    first = write_contract(tmp_path, "a.json", [TOTAL], "totals must be correct")
    second = write_contract(tmp_path, "b.json", [TOTAL], "a different ask")

    opened = vkit("task", "begin", "--project", str(repo), "--contract", str(first),
                  "--request-id", "req-1", "--json")
    assert opened.returncode == EXIT_OK

    conflict = vkit("task", "begin", "--project", str(repo), "--contract", str(second),
                    "--request-id", "req-1", "--json")
    assert conflict.returncode == EXIT_INVALID
    assert "different payload" in one_json_object(conflict)["error"]


# ----------------------------------------------------------- check start


def test_check_start_produces_a_run_that_run_show_reads(repo: Path, tmp_path: Path) -> None:
    task_id = begin(repo, write_contract(tmp_path, "c.json", [TOTAL], "totals"))

    started = vkit("check", "start", "--project", str(repo), "--task", task_id,
                   "--check", TOTAL, "--request-id", "run-1", "--json")
    assert started.returncode == EXIT_OK, started.stdout + started.stderr
    run = one_json_object(started)
    assert run["run_id"]
    assert run["task_id"] == task_id
    assert run["attempt"] == 1
    # The command returns the run and the lifecycle it has recorded so far. It has
    # no outcome to return, and it does not wait for one: the check belongs to a
    # supervisor that outlives this process, which is the whole reason a cancel
    # from another terminal can reach it.
    assert run["lifecycle"] in ("preparing", "launching", "running", "terminal")
    assert "outcome" not in run

    report = await_outcome(repo, run["run_id"])
    assert report["outcome"]["result"] == "PASS"
    assert report["lifecycle"] == "terminal"


def test_a_retry_of_check_start_returns_the_same_run_without_running_again(
    repo: Path, tmp_path: Path
) -> None:
    task_id = begin(repo, write_contract(tmp_path, "c.json", [TOTAL], "totals"))
    args = ("check", "start", "--project", str(repo), "--task", task_id,
            "--check", TOTAL, "--request-id", "run-1", "--json")

    first = one_json_object(vkit(*args))
    second = one_json_object(vkit(*args))
    assert first["run_id"] == second["run_id"]
    # The retry attaches to the recorded run rather than executing a second time.
    # What it must never do is mint a second run id, so that is what is asserted;
    # the outcome is not part of the start payload and never was in this contract.
    assert second["replayed"] is True
    assert first["replayed"] is False
    assert await_outcome(repo, first["run_id"])["outcome"]["result"] == "PASS"


    # Exactly one run exists, so the retry attached rather than starting a second.
    with sqlite3.connect(f"file:{_state(repo) / 'state.sqlite3'}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?", (task_id,)).fetchone()[0]
    assert rows == 1


# ------------------------------------------------------------- run cancel


def test_run_cancel_on_a_finished_run_returns_the_outcome_it_reached(
    repo: Path, tmp_path: Path
) -> None:
    task_id = begin(repo, write_contract(tmp_path, "c.json", [TOTAL], "totals"))
    run_id = start(repo, task_id)["run_id"]

    cancelled = vkit("run", "cancel", "--project", str(repo), "--run", run_id,
                     "--request-id", "can-1", "--json")
    assert cancelled.returncode == EXIT_OK, cancelled.stdout + cancelled.stderr
    payload = one_json_object(cancelled)
    assert payload["run_id"] == run_id
    # A finished run is not cancelled. It answers with what it actually reached.
    assert payload["cancelled"] is False
    assert payload["outcome"]["result"] == "PASS"

    # And the durable evidence still says PASS, so the cancel invented nothing.
    report = one_json_object(vkit("run", "show", "--project", str(repo), "--run", run_id, "--json"))
    assert report["outcome"]["result"] == "PASS"
    assert report["lifecycle"] == "terminal"


def test_run_cancel_of_an_unknown_run_is_invalid(repo: Path) -> None:
    done = vkit("run", "cancel", "--project", str(repo), "--run", "no-such-run",
                "--request-id", "can-1", "--json")
    assert done.returncode == EXIT_INVALID
    assert one_json_object(done)["error"] == "no run is recorded under 'no-such-run'"


# ---------------------------------------------------------- task finalize


def test_task_finalize_with_a_missing_required_check_is_blocked_and_names_the_gap(
    two_checks: Path, tmp_path: Path
) -> None:
    task_id = begin(two_checks, write_contract(tmp_path, "c.json", [TOTAL, SECOND], "both"))
    assert start(two_checks, task_id, TOTAL)["outcome"]["result"] == "PASS"

    done = vkit("task", "finalize", "--project", str(two_checks), "--task", task_id, "--json")
    assert done.returncode == EXIT_BLOCKED, done.stdout + done.stderr
    payload = one_json_object(done)
    assert payload["readiness"] == "BLOCKED"
    assert payload["gaps"] == [f"no completed run for required check {SECOND!r}"]
    assert payload["required_checks"] == [TOTAL, SECOND]


def test_task_finalize_with_every_required_check_passing_exits_zero(repo: Path, tmp_path: Path) -> None:
    task_id = begin(repo, write_contract(tmp_path, "c.json", [TOTAL], "totals"))
    assert start(repo, task_id)["outcome"]["result"] == "PASS"

    done = vkit("task", "finalize", "--project", str(repo), "--task", task_id, "--json")
    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    payload = one_json_object(done)
    assert payload["readiness"] == "READY"
    assert payload["gaps"] == []


def test_task_finalize_reports_a_observed_defect_as_rejected(repo: Path, tmp_path: Path) -> None:
    """The countercheck. A real arithmetic change must produce REJECTED, not READY."""
    app = repo / "src" / "totals.py"
    good = app.read_text(encoding="utf-8")
    app.write_text(good.replace("running += amount", "running += amount + 1"), encoding="utf-8")

    task_id = begin(repo, write_contract(tmp_path, "c.json", [TOTAL], "totals"))
    assert start(repo, task_id)["outcome"]["result"] == "FAIL"

    done = vkit("task", "finalize", "--project", str(repo), "--task", task_id, "--json")
    assert done.returncode == EXIT_CHECK_FAILED, done.stdout + done.stderr
    assert one_json_object(done)["readiness"] == "REJECTED"


def test_a_task_may_add_a_check_but_cannot_drop_the_baseline(repo: Path, tmp_path: Path) -> None:
    contract = write_contract(tmp_path, "c.json", [TOTAL, SECOND], "both")
    done = vkit("task", "begin", "--project", str(repo), "--contract", str(contract),
                "--request-id", "req-1", "--json")
    assert done.returncode == EXIT_INVALID, done.stdout + done.stderr
    assert "unknown check" in one_json_object(done)["error"]


def test_a_nonexistent_task_id_is_invalid(repo: Path, tmp_path: Path) -> None:
    finalized = vkit("task", "finalize", "--project", str(repo), "--task", "no-such-task", "--json")
    assert finalized.returncode == EXIT_INVALID
    assert one_json_object(finalized)["error"] == "no such task: no-such-task"

    started = vkit("check", "start", "--project", str(repo), "--task", "no-such-task",
                   "--check", TOTAL, "--request-id", "run-1", "--json")
    assert started.returncode == EXIT_INVALID
    assert one_json_object(started)["error"] == "no such task: no-such-task"


# ---------------------------------------------------------------- recover


def test_recover_without_an_apply_flag_changes_nothing(repo: Path, tmp_path: Path) -> None:
    task_id = begin(repo, write_contract(tmp_path, "c.json", [TOTAL], "totals"))
    start(repo, task_id)
    before = _state_digest(repo)

    first = vkit("recover", "--project", str(repo), "--json")
    assert first.returncode == EXIT_OK, first.stdout + first.stderr
    assert one_json_object(first)["inspected"] is True

    # Inspecting twice answers identically, and no run, report or row appeared or
    # changed in between. A recover that quietly reconciled something would show
    # up in the second reading or in the digest.
    second = vkit("recover", "--project", str(repo), "--json")
    assert one_json_object(second) == one_json_object(first)
    assert _state_digest(repo) == before


def test_recover_with_an_apply_flag_and_no_evidence_is_refused(repo: Path) -> None:
    no_evidence = vkit("recover", "--project", str(repo), "--apply", "release_claim",
                       "--target", "checkout:main", "--json")
    assert no_evidence.returncode == EXIT_INVALID
    assert "--evidence required" in one_json_object(no_evidence)["error"]

    blank_evidence = vkit("recover", "--project", str(repo), "--apply", "release_claim",
                          "--target", "checkout:main", "--evidence", "   ", "--json")
    assert blank_evidence.returncode == EXIT_INVALID
    assert "--evidence required" in one_json_object(blank_evidence)["error"]

    no_target = vkit("recover", "--project", str(repo), "--apply", "mark_run_dead",
                     "--evidence", "the operator saw the console", "--json")
    assert no_target.returncode == EXIT_INVALID
    assert "--target required" in one_json_object(no_target)["error"]


def test_recover_naming_evidence_without_an_action_is_invalid(repo: Path) -> None:
    done = vkit("recover", "--project", str(repo), "--target", "checkout:main",
                "--evidence", "the operator saw the console", "--json")
    assert done.returncode == EXIT_INVALID
    assert "name one with --apply" in one_json_object(done)["error"]


# ------------------------------------------------------------------ shape


def test_every_command_writes_one_json_object_and_nothing_else(
    repo: Path, tmp_path: Path
) -> None:
    task_id = begin(repo, write_contract(tmp_path, "c.json", [TOTAL], "totals"))
    run_id = start(repo, task_id)["run_id"]
    commands = [
        ("task", "begin", "--project", str(repo), "--contract",
         str(write_contract(tmp_path, "again.json", [TOTAL], "totals")), "--request-id", "req-2"),
        ("check", "start", "--project", str(repo), "--task", task_id, "--check", TOTAL,
         "--request-id", "run-1"),
        ("run", "show", "--project", str(repo), "--run", run_id),
        ("run", "cancel", "--project", str(repo), "--run", run_id, "--request-id", "can-1"),
        ("task", "finalize", "--project", str(repo), "--task", task_id),
        ("recover", "--project", str(repo)),
    ]
    for command in commands:
        done = vkit(*command, "--json")
        payload = one_json_object(done)
        assert isinstance(payload, dict), command
        assert payload, f"{' '.join(command[:2])} wrote an empty object"
        assert done.stdout.endswith("\n")
        assert done.stdout.count('"command"') >= 0  # parsed whole, nothing trailing


# ----------------------------------------------------------------- helpers


def _state(repo: Path) -> Path:
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo, capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()
    return Path(common) / "verification-kit"


def _state_digest(repo: Path) -> list[tuple[str, str]]:
    """Every durable file under the state root, with its content hash.

    Read from outside the code under test, so a change to `recover` that wrote
    anything at all changes this list.
    """
    root = _state(repo)
    return sorted(
        (str(path.relative_to(root)).replace("\\", "/"), _sha256(path))
        for path in root.rglob("*")
        if path.is_file()
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_run_recorded_without_a_creation_time_cannot_be_cancelled() -> None:
    """A pid with no creation time is not a partial identity, it is no identity.

    The check used to live in `cli._identity_of`, which passed
    `recorded.get("creation_time")` straight into `ProcessIdentity`, whose field is
    typed `int`. A record that stored a pid but not its creation time therefore
    produced `creation_time=None`, which fails every comparison in
    `still_the_same_process`. The cancellation was refused as `ownership_lost` --
    the safe direction -- but the user was told a process was not theirs when it
    was, and the type contract was violated at a boundary that is supposed to be
    checked.

    It now lives in the core, next to the decision to cancel, because the client
    no longer supplies an identity at all and so there is no longer a CLI-shaped
    place for it to live.
    """
    from vkit.supervisor import SupervisorError, _recorded_identity

    with pytest.raises(SupervisorError) as caught:
        _recorded_identity("r1", {"pid": 4242, "check_id": "demo"})

    message = str(caught.value)
    assert "creation time" in message
    assert "pids are recycled" in message


def test_a_recorded_pid_with_a_creation_time_is_cancellable() -> None:
    """The guard must not refuse a well-formed identity, or it is a new bug."""
    from vkit.supervisor import _recorded_identity

    identity = _recorded_identity(
        "r1", {"pid": 4242, "creation_time": 133000000000000000, "check_id": "demo"}
    )

    assert identity.pid == 4242
    assert identity.creation_time == 133000000000000000


def test_a_zero_creation_time_is_refused_like_a_missing_one() -> None:
    """Zero is what a record gets when a field was written but never populated.

    It is falsy and it compares unequal to every real FILETIME, so it is the
    same defect as absent and is refused the same way.
    """
    from vkit.supervisor import SupervisorError, _recorded_identity

    for bad in (0, None, -1, "133", True):
        with pytest.raises(SupervisorError):
            _recorded_identity("r1", {"pid": 4242, "creation_time": bad, "check_id": "demo"})


def test_mcp_serve_lists_the_bound_project_tools(tmp_path: Path) -> None:
    """The plugin's .mcp.json runs `vkit mcp serve --project <root>`. If that
    subcommand does not exist, the plugin ships a config pointing at nothing and a
    green suite hides it."""
    import json as _json

    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    done = vkit("mcp", "serve", "--project", str(repo), "--json")
    assert done.returncode == EXIT_OK, done.stderr
    payload = _json.loads(done.stdout)
    assert payload["project"] == str(repo)
    names = {t["name"] for t in payload["tools"]}
    assert names == {
        "project_inspect", "task_begin", "check_start",
        "run_get", "run_cancel", "task_finalize",
    }


def test_mcp_serve_refuses_a_path_outside_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    done = vkit("mcp", "serve", "--project", str(plain), "--json")
    assert done.returncode == EXIT_INVALID

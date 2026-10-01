"""End-to-end behavior of the three Plan 01 commands.

Every test drives the installed console script in a real subprocess, because the
plan requires exercising "the actual installed command path rather than only
imported helper functions". A test that imports run_check and calls it proves the
core works; it does not prove a user can run vkit and get a trustworthy exit code.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import console_script

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

# The mechanism that contained the run, by the host it ran on. The value is the
# operating system's own vocabulary, not vkit's: a job object on Windows and a
# process group on POSIX are the two names run-report.v1.json admits, and which
# one is correct depends entirely on where the process was launched. Asserting
# one of them would pin the host rather than the record, and would fail on the
# other platform for a report that was right all along.
EXPECTED_OWNERSHIP = (
    "windows_job_object" if sys.platform == "win32" else "posix_process_group"
)

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INTERNAL = 4


def vkit(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the real console entry point, never an import.

    Located through the installation rather than beside `sys.executable`, because
    that is not where pip puts a script in every layout. See `console_script`.
    """
    return subprocess.run(
        [str(console_script()), *args],
        capture_output=True, text=True, timeout=180, cwd=cwd,
    )


@pytest.fixture()
def example_repo(tmp_path: Path) -> Path:
    """A throwaway Git repository holding a real copy of the example."""
    target = tmp_path / "späce repo"
    shutil.copytree(EXAMPLE, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True,
    )
    return target


def test_doctor_reports_ready_for_a_valid_project(example_repo: Path) -> None:
    done = vkit("doctor", "--project", str(example_repo), "--json")
    assert done.returncode == EXIT_OK, done.stderr
    payload = json.loads(done.stdout)
    assert payload["ok"] is True
    assert payload["state_writable"] is True
    assert "totals-behavior" in payload["checks"]


def test_doctor_human_and_json_agree(example_repo: Path) -> None:
    as_json = vkit("doctor", "--project", str(example_repo), "--json")
    human = vkit("doctor", "--project", str(example_repo))
    assert as_json.returncode == human.returncode
    payload = json.loads(as_json.stdout)
    assert payload["ok"] is True
    assert "totals-behavior" in human.stdout
    # A non-JSON run must not print a JSON document to stdout.
    with pytest.raises(json.JSONDecodeError):
        json.loads(human.stdout)


def test_check_run_passes_on_a_correct_application(example_repo: Path) -> None:
    done = vkit("check", "run", "--project", str(example_repo),
                "--check", "totals-behavior", "--json")
    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    payload = json.loads(done.stdout)
    assert payload["outcome"]["result"] == "PASS"
    scenarios = {s["id"] for s in payload["outcome"]["scenarios"]}
    assert scenarios == {
        "empty-cart", "single-positive", "several-positives",
        "mixed-sign", "negatives-only", "cancels-to-zero",
    }


def test_report_records_real_command_provenance(example_repo: Path) -> None:
    done = vkit("check", "run", "--project", str(example_repo),
                "--check", "totals-behavior", "--json")
    run_id = json.loads(done.stdout)["run_id"]
    shown = vkit("run", "show", "--project", str(example_repo),
                 "--run", run_id, "--json")
    report = json.loads(shown.stdout)
    assert report["command"]["argv"][0] == "python"
    assert report["command"]["argv"][1] == "verify_totals.py"
    assert report["source"]["head"]
    assert report["configuration_digest"]
    assert report["process"]["ownership"] == EXPECTED_OWNERSHIP


def test_introduced_defect_fails_with_expected_versus_actual(example_repo: Path) -> None:
    """The countercheck. A real arithmetic change must produce FAIL, not a stub."""
    app = example_repo / "src" / "totals.py"
    good = app.read_text(encoding="utf-8")
    app.write_text(good.replace("running += amount", "running += amount + 1"), encoding="utf-8")

    done = vkit("check", "run", "--project", str(example_repo),
                "--check", "totals-behavior", "--json")
    assert done.returncode == EXIT_CHECK_FAILED, done.stdout
    payload = json.loads(done.stdout)
    assert payload["outcome"]["result"] == "FAIL"
    failing = {s["id"] for s in payload["outcome"]["scenarios"] if s["result"] == "FAIL"}
    assert "mixed-sign" in failing
    observations = " ".join(s["observation"] for s in payload["outcome"]["scenarios"])
    assert "expected" in observations and "printed" in observations

    app.write_text(good, encoding="utf-8")
    again = vkit("check", "run", "--project", str(example_repo),
                 "--check", "totals-behavior", "--json")
    assert again.returncode == EXIT_OK, "restoring the source must restore PASS"


def test_unknown_check_is_rejected_before_launch(example_repo: Path) -> None:
    done = vkit("check", "run", "--project", str(example_repo),
                "--check", "no-such-check", "--json")
    assert done.returncode == EXIT_INVALID
    assert "unknown check" in json.loads(done.stdout)["error"]


def test_missing_tool_is_blocked_naming_the_prerequisite(tmp_path: Path) -> None:
    """A declared executable that is not on PATH must BLOCK, and must not claim
    that a subprocess ran."""
    repo = tmp_path / "repo"
    (repo / "verification").mkdir(parents=True)
    shutil.copy(EXAMPLE / "src" / "totals.py", repo / "totals.py")
    (repo / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "checks": [{
            "id": "needs-missing-tool",
            "command": ["definitely-not-installed-xyzzy", "--version"],
            "timeout_seconds": 10,
            "required_scenarios": ["s1"],
            "artifact": "result.json",
            "prerequisites": [{"name": "ghost", "executable": "definitely-not-installed-xyzzy"}],
        }],
    }), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    done = vkit("check", "run", "--project", str(repo), "--check", "needs-missing-tool", "--json")
    assert done.returncode == EXIT_BLOCKED
    payload = json.loads(done.stdout)
    assert payload["outcome"]["result"] == "BLOCKED"
    assert payload["outcome"]["reason"] == "prerequisite_missing"
    assert "ghost" in payload["outcome"]["detail"]
    assert payload["outcome"].get("process") is None


def test_run_show_on_an_unknown_run_is_invalid(example_repo: Path) -> None:
    done = vkit("run", "show", "--project", str(example_repo), "--run", "nope", "--json")
    assert done.returncode == EXIT_INVALID


def test_non_repository_project_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    done = vkit("doctor", "--project", str(plain), "--json")
    assert done.returncode == EXIT_INVALID
    assert "not inside a Git repository" in json.loads(done.stdout)["error"]


def test_state_lives_in_the_git_common_dir_not_the_worktree(example_repo: Path) -> None:
    """Evidence must survive the checkout being deleted, so state cannot live in it."""
    done = vkit("check", "run", "--project", str(example_repo),
                "--check", "totals-behavior", "--json")
    run_id = json.loads(done.stdout)["run_id"]
    report = json.loads(done.stdout)["report_path"]
    # Decode as UTF-8. The locale's ANSI code page mangles a non-ASCII path, so
    # reading git's output with text=True compares mojibake against mojibake and
    # fails for a path that is actually correct.
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=example_repo, capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert Path(common) in Path(report).parents
    assert Path(report).is_file()
    assert run_id


def test_non_ascii_repository_path_is_read_exactly(tmp_path: Path) -> None:
    """Regression: git emits UTF-8 paths, and decoding them with the locale's
    ANSI code page produced a different string, so the next process launch died
    with WinError 267. The acceptance table requires non-ASCII paths to work."""
    repo = tmp_path / "späce repo"
    shutil.copytree(EXAMPLE, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"],
                   cwd=repo, check=True)
    done = vkit("doctor", "--project", str(repo), "--json")
    # The point is that the path survives: before the fix this died with
    # WinError 267 and no JSON at all.
    assert done.returncode == EXIT_OK, done.stderr
    assert json.loads(done.stdout)["project"] == str(repo)


def test_a_manifest_naming_a_missing_binary_is_blocked_not_crashed(tmp_path: Path) -> None:
    """A typo in a manifest command is the most ordinary bad input there is.

    It used to exit 4 (internal error) and leave the run row stuck in 'running'
    with no result forever, because a refused launch produces a result whose pid
    is None and the report schema types pid as an integer.
    """
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE, repo)
    manifest = json.loads((repo / "verification" / "manifest.json").read_text(encoding="utf-8"))
    manifest["checks"][0]["command"] = ["no-such-binary-abc123", "--version"]
    manifest["checks"][0]["prerequisites"] = []
    (repo / "verification" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"],
                   cwd=repo, check=True)

    done = vkit("check", "run", "--project", str(repo), "--check", "totals-behavior", "--json")
    assert done.returncode == EXIT_BLOCKED, done.stdout
    outcome = json.loads(done.stdout)["outcome"]
    assert outcome["result"] == "BLOCKED"
    assert outcome["reason"] == "launch_failed"

    # And the run is terminal, not orphaned mid-flight.
    run_id = json.loads(done.stdout)["run_id"]
    shown = vkit("run", "show", "--project", str(repo), "--run", run_id, "--json")
    assert shown.returncode == EXIT_BLOCKED
    assert json.loads(shown.stdout)["lifecycle"] == "terminal"

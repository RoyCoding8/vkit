"""End-to-end behavior of the Node example.

The point of this file is that the feature map is not specific to one language,
so the check here drives a real Node CLI as a subprocess and reads a real
artifact back, exactly as a user or an agent would. Nothing imports the
arithmetic and asserts on it; every assertion is on what the process actually
printed or on the artifact a reader would read.

The defect test is the countercheck the plan requires. It injects a real
off-by-one into the leftover loop, proves the driver notices, and proves the
driver is not simply failing everything, because the scenarios with no
remainder must still pass under the same defect.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "node-cli"

# The literal line the defect rewrites, and the rewrite itself.
CORRECT_LOOP = "for (let handed = 0; handed < leftover; handed += 1) {"
DEFECT_LOOP = "for (let handed = 0; handed < leftover - 1; handed += 1) {"


def node_executable() -> str:
    """The real node, or a skip naming why it is not usable.

    The example is a Node application. Without node there is nothing to verify,
    and reporting that honestly beats a silent pass.
    """
    done = subprocess.run(
        ["node", "--version"], capture_output=True, text=True, timeout=30, check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"node is not on PATH: {done.stderr.strip() or 'node --version failed'}")
    return "node"


@pytest.fixture()
def example_repo(tmp_path: Path) -> Path:
    """A throwaway Git repository holding a real copy of the example.

    The path carries a space on purpose. A launch that breaks on a space is a
    launch that breaks for real users, and the plan asks for portability.
    """
    target = tmp_path / "späce repo"
    shutil.copytree(EXAMPLE, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True,
    )
    return target


def run_driver(node: str, repo: Path) -> dict:
    """Run the real driver as a subprocess and return the artifact it wrote.

    The driver is invoked the way the manifest invokes it, with the run
    directory supplied by the caller. The artifact is read back from disk rather
    than taken from any return value, because the artifact is the only channel a
    check has.
    """
    run_dir = repo / ".run" / "current"
    run_dir.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [node, "verify-split.js", "--app", "src/split-bill.js", "--out", str(run_dir / "result.json")],
        cwd=repo, capture_output=True, text=True, timeout=180, check=False,
    )
    assert done.returncode == 0, f"driver failed: {done.stderr}"
    artifact = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    return artifact


def results_by_id(artifact: dict) -> dict[str, str]:
    return {s["id"]: s["result"] for s in artifact["scenarios"]}


def test_a_correct_application_reports_every_scenario_pass(example_repo: Path) -> None:
    node = node_executable()
    artifact = run_driver(node, example_repo)
    results = results_by_id(artifact)
    assert results == {
        "even-two-way": "PASS",
        "even-three-way-weights": "PASS",
        "single-person": "PASS",
        "zero-total": "PASS",
        "leftover-cent-goes-to-first": "PASS",
        "tip-joins-the-split": "PASS",
        "three-way-leftover-two-cents": "PASS",
        "four-way-leftover-and-tip": "PASS",
    }


def test_the_shares_printed_sum_to_the_amount_owed(example_repo: Path) -> None:
    """The arithmetic invariant, checked against the real process output.

    This reads the shares the CLI printed and adds them up. It is the reason the
    example is money rather than text: an off-by-one in the remainder shows up
    here as shares that do not add up to the amount owed.
    """
    node = node_executable()
    done = subprocess.run(
        [node, "src/split-bill.js", "split", "999", "ann=3", "bob=3", "cyd=1"],
        cwd=example_repo, capture_output=True, text=True, timeout=60, check=True,
    )
    lines = done.stdout.strip().splitlines()
    owed = next(int(line.split()[1].replace(".", "")) for line in lines if line.startswith("owed "))
    shares = [
        int(line.split()[1].replace(".", "")) for line in lines if line.split()[0] in {"ann", "bob", "cyd"}
    ]
    assert shares == [428, 428, 143]
    assert sum(shares) == owed == 999


def test_introduced_defect_fails_with_expected_versus_actual(example_repo: Path) -> None:
    """The countercheck. A real money bug must produce FAIL, not a stub."""
    node = node_executable()
    app = example_repo / "src" / "split-bill.js"
    good = app.read_text(encoding="utf-8")
    assert CORRECT_LOOP in good
    app.write_text(good.replace(CORRECT_LOOP, DEFECT_LOOP), encoding="utf-8")

    artifact = run_driver(node, example_repo)
    results = results_by_id(artifact)
    failing = {sid for sid, result in results.items() if result == "FAIL"}
    assert "tip-joins-the-split" in failing
    assert "three-way-leftover-two-cents" in failing
    observation = next(
        s["observation"] for s in artifact["scenarios"] if s["id"] == "tip-joins-the-split"
    )
    assert "expected" in observation and "printed" in observation
    # The defect is a lost cent, so the expected block shows 6.67 and the printed
    # block shows 6.66. Both are present because the driver keeps the whole
    # comparison, not just a verdict.
    assert "ann 6.67" in observation
    assert "ann 6.66" in observation

    app.write_text(good, encoding="utf-8")
    again = run_driver(node, example_repo)
    assert set(results_by_id(again).values()) == {"PASS"}, "restoring must restore PASS"


def test_scenarios_without_a_remainder_still_pass_under_the_defect(example_repo: Path) -> None:
    """A driver where everything fails proves nothing.

    The defect only changes the answer when a leftover cent exists. The
    remainder-free scenarios must stay green under the same injected defect, so
    the FAIL results are attributable to the money bug and not to a driver that
    fails unconditionally.
    """
    node = node_executable()
    app = example_repo / "src" / "split-bill.js"
    good = app.read_text(encoding="utf-8")
    app.write_text(good.replace(CORRECT_LOOP, DEFECT_LOOP), encoding="utf-8")

    results = results_by_id(run_driver(node, example_repo))
    still_passing = {sid for sid, result in results.items() if result == "PASS"}
    assert "even-two-way" in still_passing
    assert "single-person" in still_passing
    assert "zero-total" in still_passing
    assert len(still_passing) >= 1

    app.write_text(good, encoding="utf-8")


def test_the_driver_has_no_flag_that_reports_a_result(example_repo: Path) -> None:
    """The driver cannot manufacture agreement.

    A driver that accepted a pass or fail flag would prove nothing when it
    agreed. This asserts the source contains no such flag, so the only way a
    scenario reports PASS is the process printing the expected split.
    """
    driver = (example_repo / "verify-split.js").read_text(encoding="utf-8")
    for forbidden in ("--pass", "--fail", "--result", "--expect", "--ok"):
        assert forbidden not in driver, f"driver must not accept {forbidden}"


def test_a_bad_weight_is_rejected_by_the_cli(example_repo: Path) -> None:
    """The CLI refuses a zero weight rather than dividing by it or ignoring it."""
    node = node_executable()
    done = subprocess.run(
        [node, "src/split-bill.js", "split", "1000", "ann=0"],
        cwd=example_repo, capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 2
    assert "positive" in done.stderr

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from helpers import DRIVER, NO_WINDOW, pytest_kind_check, scenario_check, vkit, vkit_argv

CALC = '''
def add(a, b):
    return a + b
'''
CALC_TESTS = '''
from calc import add

def test_adds_two_numbers():
    assert add(2, 3) == 5

def test_adds_negatives():
    assert add(-2, -3) == -5
'''


def calc_project(make_project, source: str = CALC):
    return make_project(
        {"calc.py": source, "test_calc.py": CALC_TESTS},
        [pytest_kind_check("calc-tests", ["test_calc.py::test_adds_two_numbers", "test_calc.py::test_adds_negatives"],
                      ["calc.py", "test_calc.py"])],
    )


def only_run(body):
    assert len(body["runs"]) == 1, body
    return body["runs"][0]


def test_a_pytest_check_passes_and_records_each_required_test(make_project):
    project = calc_project(make_project)
    code, body = vkit(project, "check", "run", "--check", "calc-tests")
    run = only_run(body)
    assert (code, run["outcome"]["result"], run["category"]) == (0, "PASS", "scenario")
    satisfied = sorted(item["obligation"]["obligation"] for item in run["obligations"]["satisfied"])
    assert satisfied == ["test_calc.py::test_adds_negatives", "test_calc.py::test_adds_two_numbers"]


def test_a_broken_function_fails_and_the_gate_rejects(make_project):
    project = calc_project(make_project, CALC.replace("a + b", "a - b"))
    code, body = vkit(project, "check", "run", "--check", "calc-tests")
    run = only_run(body)
    assert (code, run["outcome"]["result"]) == (1, "FAIL")
    failing = sorted(item["obligation"]["obligation"] for item in run["obligations"]["counterexamples"])
    assert failing == ["test_calc.py::test_adds_negatives", "test_calc.py::test_adds_two_numbers"]
    code, gate = vkit(project, "gate")
    assert (code, gate["verdict"], gate["checks"][0]["state"]) == (1, "REJECTED", "fresh_fail")


def test_a_required_test_that_does_not_exist_is_blocked_not_passed(make_project):
    project = make_project(
        {"calc.py": CALC, "test_calc.py": CALC_TESTS},
        [pytest_kind_check("calc-tests", ["test_calc.py::test_adds_two_numbers", "test_calc.py::test_missing"],
                      ["calc.py", "test_calc.py"])],
    )
    code, body = vkit(project, "check", "run", "--check", "calc-tests")
    run = only_run(body)
    assert (code, run["outcome"]["result"]) == (3, "BLOCKED")


SLOW_TREE = DRIVER + '''
import subprocess, time
marker = sys.argv[1] + ".grandchild"
subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(4); open({marker!r}, 'w').write('alive')"])
time.sleep(60)
report([("never", True, "unreachable")])
'''


def test_a_timeout_stops_the_check_and_everything_it_started(make_project):
    project = make_project({"slow.py": SLOW_TREE},
                           [scenario_check("slow", "slow.py", scenarios=["never"], inputs=["slow.py"], timeout=2)])
    started = time.monotonic()
    code, body = vkit(project, "check", "run", "--check", "slow")
    run = only_run(body)
    assert (code, run["outcome"]["result"], run["outcome"]["reason"]) == (3, "BLOCKED", "timeout")
    assert time.monotonic() - started < 30
    time.sleep(5)
    assert list((project / ".git" / "vkit" / "runs").rglob("*.grandchild")) == []


def _start_in_background(project: Path, check_id: str) -> subprocess.Popen:
    return subprocess.Popen(vkit_argv(project, "check", "run", "--check", check_id), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **NO_WINDOW)


def _wait_for_running(project: Path) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        code, body = vkit(project, "status")
        running = [r for c in body["checks"] for r in c["running"]]
        if running:
            return running[0]
        time.sleep(0.2)
    raise AssertionError("the run never appeared as running")


def test_run_cancel_stops_a_running_check(make_project):
    project = make_project({"slow.py": SLOW_TREE},
                           [scenario_check("slow", "slow.py", scenarios=["never"], inputs=["slow.py"], timeout=120)])
    runner = _start_in_background(project, "slow")
    run_id = _wait_for_running(project)
    code, body = vkit(project, "run", "cancel", "--run", run_id)
    assert (code, body["cancelled"]) == (0, True)
    stdout, _ = runner.communicate(timeout=30)
    outcome = json.loads(stdout)["runs"][0]["outcome"]
    assert (runner.returncode, outcome["result"], outcome["reason"]) == (3, "BLOCKED", "cancelled")


def test_a_run_whose_runner_died_reads_as_interrupted(make_project):
    project = make_project({"slow.py": SLOW_TREE},
                           [scenario_check("slow", "slow.py", scenarios=["never"], inputs=["slow.py"], timeout=120)])
    runner = _start_in_background(project, "slow")
    run_id = _wait_for_running(project)
    runner.kill()
    runner.wait(timeout=30)
    code, body = vkit(project, "run", "show", "--run", run_id)
    assert (code, body["state"]) == (3, "interrupted")


EDITS_ITS_INPUT = DRIVER + '''
with open("data.txt", "a", encoding="utf-8") as handle:
    handle.write("changed")
report([("reads-data", True, "read data.txt")])
'''


def test_an_input_edited_during_the_run_blocks_the_result(make_project):
    project = make_project({"edit.py": EDITS_ITS_INPUT, "data.txt": "original"},
                           [scenario_check("edits", "edit.py", scenarios=["reads-data"], inputs=["edit.py", "data.txt"])])
    code, body = vkit(project, "check", "run", "--check", "edits")
    outcome = only_run(body)["outcome"]
    assert (code, outcome["result"], outcome["reason"]) == (3, "BLOCKED", "source_changed")
    assert "data.txt" in outcome["detail"]


PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def test_accept_without_yes_is_refused_when_not_interactive(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])],
                           accept=False)
    code, body = vkit(project, "accept")
    assert code == 2 and "--yes" in body["error"]
    code, body = vkit(project, "check", "run", "--check", "ok")
    assert only_run(body)["outcome"]["reason"] == "not_approved"


def test_status_names_paths_no_check_reads(make_project):
    project = make_project({"ok.py": PASSES, "README.md": "hi"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    code, body = vkit(project, "status", "--path", "README.md", "--path", "ok.py")
    assert [c["id"] for c in body["checks"]] == ["ok"]
    assert body["unmapped_paths"] == ["README.md"]


def test_a_check_with_no_declared_inputs_goes_stale_on_any_edit(make_project):
    check = scenario_check("ok", "ok.py", scenarios=["ok"], inputs=[])
    project = make_project({"ok.py": PASSES, "README.md": "hi"}, [check])
    vkit(project, "check", "run", "--check", "ok")
    (project / "README.md").write_text("changed", encoding="utf-8")
    code, body = vkit(project, "status")
    assert (body["checks"][0]["state"], body["checks"][0]["inputs"]["scope"]) == ("stale", "whole tree (3 files)")

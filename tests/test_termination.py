from __future__ import annotations

import pytest

pytest.importorskip("cvc5")

from vkit.operations import termination
from vkit.operations.termination import analyze_loop_termination
from helpers import McpClient, vkit


def _run(project, code):
    (project / "loop.py").write_text(code, encoding="utf-8")
    return analyze_loop_termination(project, "loop.py", "f")


@pytest.mark.parametrize("guard,update", [
    ("x > 0", "x -= 1"),
    ("x < 10", "x += 2"),
    ("x >= 0", "x -= 2"),
    ("False", "pass"),
])
def test_integer_rankings_are_checked_over_every_guarded_state(tmp_path, guard, update):
    result = _run(tmp_path, f"def f(x: int) -> int:\n    while {guard}:\n        {update}\n    return x\n")
    assert result["status"] == "TERMINATES", result
    ranking = result["ranking"]
    for x in range(-20, 21):
        active = {"x > 0": x > 0, "x < 10": x < 10, "x >= 0": x >= 0, "False": False}[guard]
        if active:
            following = x + (2 if update == "x += 2" else -2 if update == "x -= 2" else -1)
            before = ranking["offset"] + ranking["coefficients"]["x"] * x
            after = ranking["offset"] + ranking["coefficients"]["x"] * following
            assert before >= 0 and after < before
    assert result["backend"] == {"name": "cvc5", "version": "1.4.2"}


def test_guarded_fixed_point_is_replayed(tmp_path):
    result = _run(tmp_path, "def f(x: int) -> int:\n    while x >= 0:\n        x = x\n    return x\n")
    assert result["status"] == "NONTERMINATING"
    assert result["state"]["x"] >= 0
    assert result["state"] == result["next_state"]
    assert result["replayed"] is True


def test_updates_use_python_sequential_assignment_semantics(tmp_path):
    result = _run(tmp_path, "def f(x: int, y: int) -> int:\n    while x > 0:\n        y = x - 1\n        x = y\n    return x\n")
    assert result["status"] == "TERMINATES", result
    source = "def f(x: int, y: int) -> int:\n    while x > 0:\n        y = x - 1\n        x = y\n    return x\n"
    loop = termination._parse(source, "f")
    assert [termination._evaluate(update, {"x": 3, "y": 100}) for update in loop.updates] == [2, 2]


def test_two_state_cycle_is_not_reported_as_termination(tmp_path):
    result = _run(tmp_path, "def f(x: int) -> int:\n    while x == 1 or x == -1:\n        x = -x\n    return x\n")
    assert result["status"] == "UNKNOWN"


@pytest.mark.parametrize("body", ["x = x * x", "print(x)", "break", "x = x // 2", "z = 0"])
def test_unmodeled_loop_semantics_are_rejected(tmp_path, body):
    result = _run(tmp_path, f"def f(x: int) -> int:\n    while x > 0:\n        {body}\n    return x\n")
    assert result["status"] == "UNSUPPORTED"


def test_failed_independent_fixed_point_replay_is_inconclusive(monkeypatch):
    original = termination._evaluate
    monkeypatch.setattr(termination, "_evaluate", lambda term, state: False if term.sort == "bool" else original(term, state))
    result = termination._check("def f(x: int) -> int:\n    while x >= 0:\n        pass\n    return x\n", "f", 2_000)
    assert result["status"] == "UNKNOWN"
    assert "replay" in result["reason"]


def test_loop_operation_cli_and_mcp(make_project):
    project = make_project({"loop.py": "def f(x: int) -> int:\n    while x > 0:\n        x -= 1\n    return x\n"}, [], accept=False)
    code, body = vkit(project, "compute", "analyze-loop-termination", "--path", "loop.py", "--function", "f")
    assert (code, body["status"]) == (0, "TERMINATES")
    client = McpClient(project)
    try:
        body, error = client.call("analyze_loop_termination", path="loop.py", function="f")
        assert not error and body["status"] == "TERMINATES"
    finally:
        client.close()

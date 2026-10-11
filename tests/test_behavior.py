from __future__ import annotations

import json
import subprocess
import sys

import pytest

from vkit.operations.behavior import find_behavioral_difference

pytest.importorskip("crosshair")


def run(tmp_path, before, after, **kwargs):
    path = tmp_path / "behavior.json"
    path.write_text(json.dumps({"version": 1, "function": "f", "before": before, "after": after}), encoding="utf-8")
    return find_behavioral_difference(tmp_path, path.name, **kwargs)


def test_list_behavior_difference_has_two_concrete_replays(tmp_path):
    result = run(tmp_path, "def f(xs: list[int]) -> int:\n    return sum(xs)\n",
                 "def f(xs: list[int]) -> int:\n    return len(xs)\n", timeout_seconds=20)
    assert result["status"] == "COUNTEREXAMPLE"
    xs = result["arguments"]["xs"]
    assert result["observations"]["before"]["return"] == sum(xs)
    assert result["observations"]["after"]["return"] == len(xs)
    assert sum(xs) != len(xs)
    assert result["replay_count"] == 2


def test_mutated_argument_difference_is_observed(tmp_path):
    result = run(tmp_path, "def f(xs: list[int]) -> int:\n    xs.append(1)\n    return 0\n",
                 "def f(xs: list[int]) -> int:\n    return 0\n", timeout_seconds=20)
    assert result["status"] == "COUNTEREXAMPLE"
    before, after = result["observations"]["before"], result["observations"]["after"]
    assert before["return"] == after["return"] == 0
    assert before["arguments_after"]["xs"] == after["arguments_after"]["xs"] + [1]


def test_exception_difference_is_observed(tmp_path):
    result = run(tmp_path, "def f(x: int) -> int:\n    return 1 // x\n",
                 "def f(x: int) -> int:\n    return 0\n", timeout_seconds=20)
    assert result["status"] == "COUNTEREXAMPLE"
    observations = result["observations"]
    assert observations["before"] != observations["after"]


def test_loop_and_string_search(tmp_path):
    result = run(tmp_path, "def f(s: str) -> int:\n    n = 0\n    for c in s:\n        n += 1\n    return n\n",
                 "def f(s: str) -> int:\n    return len(s) + 1\n", timeout_seconds=20)
    assert result["status"] == "COUNTEREXAMPLE"
    assert result["observations"]["after"]["return"] == result["observations"]["before"]["return"] + 1


def test_no_difference_never_proves_equivalence(tmp_path):
    source = "def f(x: int) -> int:\n    return x + 1\n"
    result = run(tmp_path, source, source, timeout_seconds=10)
    assert result["status"] == "UNKNOWN"
    assert "arguments" not in result


@pytest.mark.parametrize("source", [
    "import os\ndef f(x: int) -> int:\n    return x\n",
    "@print\ndef f(x: int) -> int:\n    return x\n",
    "def f(x: int = 1) -> int:\n    return x\n",
    "def f(x: int) -> int:\n    return __import__('os').system('echo unsafe')\n",
    "def f(x: int) -> int:\n    return x.__class__\n",
    "def f(x: int) -> int:\n    return open('unsafe', 'w')\n",
    "def f(x: int) -> int:\n    return offset + x\n",
    "def f(x: int) -> int:\n    global x\n    return x\n",
    "def f(x: int) -> int:\n    return f(x)\n",
    "def f(x: int) -> int:\n    return eval('x')\n",
])
def test_unsupported_execution_paths_are_rejected_before_worker(tmp_path, source, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("rejected source must never start a worker")

    monkeypatch.setattr("vkit.operations.behavior.subprocess.run", forbidden)
    assert run(tmp_path, source, "def f(x: int) -> int:\n    return x\n")["status"] == "UNSUPPORTED"


def test_annotation_mismatch_is_rejected(tmp_path):
    assert run(tmp_path, "def f(x: int) -> int:\n    return x\n",
               "def f(x: str) -> int:\n    return len(x)\n")["status"] == "UNSUPPORTED"


def test_non_cpython_runtime_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.implementation, "name", "other")
    source = "def f(x: int) -> int:\n    return x\n"
    assert run(tmp_path, source, source)["status"] == "UNAVAILABLE"


def test_invalid_worker_output_never_becomes_a_counterexample(tmp_path, monkeypatch):
    monkeypatch.setattr("vkit.operations.behavior.subprocess.run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0, stdout="[]"))
    source = "def f(x: int) -> int:\n    return x\n"
    assert run(tmp_path, source, source)["status"] == "UNKNOWN"


@pytest.mark.parametrize("payload", ["x" * 40_001, '{"mode":"unexpected"}',
                                      '{"mode":"search","bundle":{},"timeout_seconds":1,"extra":true}'],
                         ids=["oversized", "unknown-mode", "unknown-field"])
def test_direct_worker_rejects_oversize_or_unknown_requests(payload):
    completed = subprocess.run([sys.executable, "-I", "-m", "vkit.operations.behavior"],
                               input=payload, text=True, capture_output=True, timeout=10)
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["status"] == "UNKNOWN"

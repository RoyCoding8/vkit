from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
from helpers import NO_WINDOW
from vkit.formal import transitions
from scripts.check_transition_comparison import MARKER, check_rows, request_from_ids, resolve_source

ROOT = Path(__file__).resolve().parents[1]
compare_transition_request = transitions.compare_transition_request
SOURCE = Path(transitions.__file__).parents[2]


@pytest.mark.parametrize("reference,candidate", [(-1, 0), (192, 0), (0, -1), (0, 192)])
def test_bridge_rejects_ids_outside_the_declared_model_space(tmp_path, reference, candidate):
    row = (MARKER, reference, candidate, "equivalent", [False] * 6,
           [[0, 0]] * 6, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="invalid TLC pair row"):
        check_rows([row], tmp_path, SOURCE)


def test_an_invalid_explicit_source_cannot_fall_back_to_another_checkout(tmp_path):
    assert resolve_source(SOURCE) == SOURCE.resolve()
    with pytest.raises(RuntimeError, match="comparator is unavailable"):
        resolve_source(tmp_path / "missing")


def test_assumption_only_delta_has_the_shortest_reachable_probe() -> None:
    report = compare_transition_request(request_from_ids(50, 18))

    assert report["kind"] == "different"
    assert [(item["kind"], item["removed"], item["added"])
            for item in report["deltas"]] == [("assumptions", 1, 0)]
    assert report["deltas"][0]["witness"] == {
        "side": "reference",
        "initial_state": "s0",
        "path": [{"source": "s0", "action": "step", "input": "x", "target": "s1"}],
        "difference": ["s1", "step", "x"],
        "probe": {"state": "s1", "action": "step", "input": "x"},
    }


def test_reachable_transition_delta_has_the_shortest_path() -> None:
    report = compare_transition_request(request_from_ids(58, 54))

    assert report["kind"] == "different"
    assert [(item["kind"], item["removed"], item["added"])
            for item in report["deltas"]] == [("transitions", 1, 1)]
    assert report["deltas"][0]["witness"] == {
        "side": "candidate",
        "initial_state": "s0",
        "path": [
            {"source": "s0", "action": "step", "input": "x", "target": "s1"},
            {"source": "s1", "action": "step", "input": "x", "target": "s0"},
        ],
        "difference": ["s1", "step", "x", "s0"],
    }


def test_complete_tlc_pair_space_matches_the_public_comparator(tmp_path: Path) -> None:
    command = [sys.executable, str(ROOT / "scripts" / "check_transition_comparison.py"),
               "--output", str(tmp_path / "transition-tlc"), "--vkit-source", str(SOURCE)]
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                          timeout=1200, check=False, **NO_WINDOW)
    unavailable = (
        "Java is unavailable; set JAVA_HOME or pass --java",
        "TLC jar is unavailable; set VKIT_TLA2TOOLS_JAR or pass --jar",
    )
    if done.returncode and any(message in done.stderr for message in unavailable):
        pytest.skip("Java or the pinned TLC jar is not configured")
    assert done.returncode == 0, done.stdout + done.stderr
    summary = json.loads(done.stdout)
    report = json.loads(Path(summary["report"]).read_text(encoding="utf-8"))
    assert summary["pairs"] == 36_864
    assert summary["tlc_distinct_states"] == 36_864
    assert report["tlc"]["emitted_rows"] == 36_864
    assert report["bridge"]["pairs_compared"] == 36_864
    assert report["bridge"]["mismatches"] == 0
    controls = {item["mutant"]: item for item in report["bridge"]["negative_controls"]}
    assert controls["ignore_assumptions"]["observed_deltas"] == ["assumptions"]
    assert controls["ignore_assumptions"]["mutated_kind"] == "equivalent"
    assert controls["ignore_assumptions"]["caught_by_tlc_comparison"] is True
    assert controls["ignore_reachable_transitions"]["observed_deltas"] == ["transitions"]
    assert controls["ignore_reachable_transitions"]["mutated_kind"] == "equivalent"
    assert controls["ignore_reachable_transitions"]["caught_by_tlc_comparison"] is True
    assert report["limit"] == (
        "This cross-check covers the declared 192-model space. It is not a Python refinement proof, "
        "source proof, or claim about larger domains."
    )

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkit.verifiers.tlc_witness import (
    same_state_sequence,
    source_locations,
    trace_states,
    without_internal_state,
)
from helpers import NO_WINDOW


def _trace(values: list[int]) -> dict:
    states = [[index, {"x": value}] for index, value in enumerate(values, 1)]
    actions = [
        [states[index], {"name": "Next"}, states[index + 1]]
        for index in range(len(states) - 1)
    ]
    return {"vars": ["x"], "counterexample": {"state": states, "action": actions}}


def test_trace_shape_and_state_sequence_are_checked():
    valid = _trace([0, 1, 2])
    assert trace_states(valid) == (("x",), ((1, {"x": 0}), (2, {"x": 1}), (3, {"x": 2})))
    assert same_state_sequence(valid, _trace([0, 1, 2]))
    assert not same_state_sequence(valid, _trace([0, 9, 2]))
    assert not same_state_sequence(_trace([1]), _trace([True]))

    invalid = _trace([0, 1, 2])
    invalid["counterexample"]["state"][1][0] = 7
    assert trace_states(invalid) is None
    invalid["counterexample"]["state"][1][0] = True
    assert trace_states(invalid) is None


def test_internal_state_removal_reindexes_actions_and_locations_are_exact():
    trace = _trace([0, 0, 1])
    trace["counterexample"]["action"][0][1] = {
        "name": "Next", "location": {
            "module": "Witness", "beginLine": 7, "beginColumn": 1,
            "endLine": 7, "endColumn": 8,
        },
    }
    reduced = without_internal_state(trace, 2)
    assert reduced is not None
    assert [state[1]["x"] for state in reduced["counterexample"]["state"]] == [0, 1]
    assert [state[0] for state in reduced["counterexample"]["state"]] == [1, 2]
    assert trace_states(reduced) is not None
    locations, truncated = source_locations(
        trace, "Witness", "Witness.tla", "a" * 64,
    )
    assert not truncated
    assert locations == [{
        "path": "Witness.tla", "module": "Witness", "start_line": 7,
        "start_column": 1, "end_line": 7, "end_column": 8,
        "source_sha256": "a" * 64,
    }]
    assert source_locations(trace, "Other", "Witness.tla") == ([], False)
    many = _trace(list(range(66)))
    for line, action in enumerate(many["counterexample"]["action"], 1):
        action[1]["location"] = {
            "module": "Witness", "beginLine": line, "beginColumn": 1,
            "endLine": line, "endColumn": 2,
        }
    bounded, truncated = source_locations(many, "Witness", "Witness.tla")
    assert len(bounded) == 64 and truncated


def test_source_identity_detects_changed_or_removed_inputs(tmp_path):
    from vkit.verifiers.tlc_runner import _digest, _sources_unchanged

    model = tmp_path / "Model.tla"
    config = tmp_path / "Model.cfg"
    model.write_text("MODULE Model", encoding="utf-8")
    config.write_text("INVARIANT Safe", encoding="utf-8")
    model_digest, config_digest = _digest(model), _digest(config)
    assert _sources_unchanged(model, config, model_digest, config_digest)
    model.write_text("MODULE Changed", encoding="utf-8")
    assert not _sources_unchanged(model, config, model_digest, config_digest)
    model.unlink()
    assert not _sources_unchanged(model, config, model_digest, config_digest)


def _real_tlc(tmp_path: Path):
    java = shutil.which("java")
    jar_text = os.environ.get("VKIT_TLA2TOOLS_JAR", "").strip()
    if not java or not jar_text or not Path(jar_text).is_file():
        pytest.skip("real TLC evidence test requires Java and VKIT_TLA2TOOLS_JAR")
    jar = Path(jar_text).resolve()
    help_run = subprocess.run(
        [java, "-cp", str(jar), "tlc2.TLC", "-help"],
        capture_output=True, text=True, check=False,
        **NO_WINDOW,
    )
    help_text = help_run.stdout + help_run.stderr
    if "-dumpTrace" not in help_text or "-loadTrace" not in help_text:
        pytest.skip("configured TLC does not support native JSON trace replay")
    return java, jar


def test_native_tlc_replay_rejects_tampered_sequences_and_accepts_reduction(tmp_path):
    from vkit.verifiers.tlc_runner import _replay_candidate

    java, jar = _real_tlc(tmp_path)
    model = tmp_path / "Witness.tla"
    config = tmp_path / "Witness.cfg"
    model.write_text(
        "---- MODULE Witness ----\nEXTENDS Naturals\nVARIABLE x\n"
        "Init == x = 0\nNext == x' = x + 1\nInv == x < 2\n"
        "Spec == Init /\\ [][Next]_x\n====\n",
        encoding="utf-8",
    )
    config.write_text("INIT Init\nNEXT Next\nINVARIANT Inv\n", encoding="utf-8")
    original_path = tmp_path / "original.json"
    metadir = tmp_path / "meta-original"
    first = subprocess.run(
        [java, "-cp", str(jar), "tlc2.TLC", "-cleanup", "-workers", "1",
         "-metadir", str(metadir), "-dumpTrace", "json", str(original_path),
         "-config", str(config.with_suffix("")), "Witness"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        **NO_WINDOW,
    )
    assert first.returncode == 12, first.stdout + first.stderr
    assert "Error: Invariant Inv is violated." in first.stdout
    original = json.loads(original_path.read_text(encoding="utf-8"))
    assert [state[1]["x"] for state in original["counterexample"]["state"]] == [0, 1, 2]

    args = SimpleNamespace(java=java, workers=1, model="Witness", model_path="Witness.tla")
    output = tmp_path / "replayed.json"
    candidate = _trace([0, 1, 2])
    replayed, _ = _replay_candidate(
        args, jar, str(config.with_suffix("")), tmp_path, candidate, output,
        tmp_path / "meta-valid", "Inv", 1,
    )
    assert replayed is not None
    assert same_state_sequence(candidate, replayed)

    mutations = (
        _trace([0, 9, 2]),  # invalid transition, but the same final state violates Inv
        _trace([1, 2]),  # changed initial state
        _trace([0, 1, 1]),  # changed final state no longer violates Inv
        _trace([0, 9, 1, 2]),  # inserted unreachable state
        _trace([2, 1, 0]),  # reordered states
    )
    for number, tampered in enumerate(mutations):
        rejected, _ = _replay_candidate(
            args, jar, str(config.with_suffix("")), tmp_path, tampered, output,
            tmp_path / f"meta-tampered-{number}", "Inv", number + 2,
        )
        assert rejected is None, f"TLC accepted tampered sequence {number}"
    wrong_property, _ = _replay_candidate(
        args, jar, str(config.with_suffix("")), tmp_path, candidate, output,
        tmp_path / "meta-wrong-property", "OtherInvariant", 20,
    )
    assert wrong_property is None

    with_stutter = _trace([0, 0, 1, 2])
    reduced = without_internal_state(with_stutter, 2)
    assert reduced is not None
    accepted_reduction, _ = _replay_candidate(
        args, jar, str(config.with_suffix("")), tmp_path, reduced, output,
        tmp_path / "meta-reduced", "Inv", 10,
    )
    assert accepted_reduction is not None
    assert same_state_sequence(reduced, accepted_reduction)

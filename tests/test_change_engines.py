from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from helpers import NO_WINDOW, ROOT
SCRIPT = ROOT / "scripts/check_change_engines.py"
spec = importlib.util.spec_from_file_location("check_change_engines", SCRIPT)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
classify_kani = probe.classify_kani
classify_playback = probe.classify_playback
classify_verus = probe.classify_verus
make_playback_wrapper = probe.make_playback_wrapper
regression_playback = probe.regression_playback
run_command = probe.run_command

KANI_LOG_EXCERPTS = {
    "refactor": (0, "VERIFICATION:- SUCCESSFUL\nComplete - 1 successfully verified harnesses, 0 failures, 1 total."),
    "regression": (1, "VERIFICATION:- FAILED\nFailed Checks: assertion failed: scalar::cap_before(x) == scalar::cap_after(x)\n"
                      "Concrete playback unit test for `regression`:\n```\n"
                      "/// Test generated for harness `regression`\n/// Check for `assertion`: \"assertion failed: scalar::cap_before(x) == scalar::cap_after(x)\"\n\n"
                      "#[test]\nfn kani_concrete_playback_regression_2635896623606938374() {\n"
                      "    let concrete_vals: Vec<Vec<u8>> = vec![\n        // 10\n        vec![10, 0, 0, 0],\n    ];\n"
                      "    kani::concrete_playback_run(concrete_vals, regression);\n}\n```"),
    "exhausted": (1, "VERIFICATION:- FAILED\nFailed Checks: unwinding assertion loop 0\nexhausted.unwind.0"),
    "undefined_behavior": (1, "VERIFICATION:- FAILED\nCheck for `safety_check`: \"Offset result and original pointer must point to the same allocation\"\n"
                            "Concrete playback unit test for `undefined_behavior`"),
    "observation_mismatch": (1, "VERIFICATION:- FAILED\nFailed Checks: assertion failed: before == after"),
}
VERUS_LOG_EXCERPTS = {
    "positive": (0, "verification results:: 4 verified, 0 errors"),
    "negative": (1, "error: assertion failed\nverification results:: 3 verified, 1 errors"),
}
PLAYBACK_LOG_EXCERPT = """test kani_concrete_playback_regression_2635896623606938374 ... FAILED
test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 3 filtered out; finished in 0.02s
assertion `left == right` failed
left: 10
right: 9
"""


def run_probe(output_dir: Path, kani: Path, verus: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--kani", str(kani), "--verus", str(verus), "--output-dir", str(output_dir)],
        cwd=ROOT, capture_output=True, text=True, timeout=30, **NO_WINDOW,
    )


def test_recorded_engine_diagnostics_require_exit_codes_and_specific_markers():
    expected_kani = {
        "refactor": "proof_success",
        "regression": "seeded_semantic_failure",
        "exhausted": "unwind_exhaustion",
        "undefined_behavior": "ub_safety_failure",
        "observation_mismatch": "observation_mismatch",
    }
    for name, (code, output) in KANI_LOG_EXCERPTS.items():
        assert classify_kani(name, code, output) == expected_kani[name]
    assert classify_kani("refactor", None, "") == "timeout"
    assert classify_kani("regression", 0, KANI_LOG_EXCERPTS["regression"][1]) == "infrastructure_failure"
    assert classify_kani("regression", 1, "VERIFICATION:- FAILED") == "infrastructure_failure"
    for name, (code, output) in VERUS_LOG_EXCERPTS.items():
        expected = "proof_success" if name == "positive" else "seeded_semantic_failure"
        assert classify_verus(name, code, output) == expected
    assert classify_verus("positive", None, "") == "timeout"
    assert classify_verus("negative", 0, VERUS_LOG_EXCERPTS["negative"][1]) == "infrastructure_failure"
    assert classify_playback("kani_concrete_playback_regression_2635896623606938374", 101,
                             PLAYBACK_LOG_EXCERPT) == "native_regression_playback"
    assert classify_playback("kani_concrete_playback_regression_2635896623606938374", 0,
                             PLAYBACK_LOG_EXCERPT) == "infrastructure_failure"
    assert classify_playback("kani_concrete_playback_regression_2635896623606938374", None,
                             "") == "timeout"
    assert classify_playback("kani_concrete_playback_regression_2635896623606938374", 101,
                             PLAYBACK_LOG_EXCERPT.replace("test result:", "summary:")) == "infrastructure_failure"
    assert classify_playback("kani_concrete_playback_regression_2635896623606938374", 101,
                             PLAYBACK_LOG_EXCERPT.replace("1 failed;", "0 failed;")) == "infrastructure_failure"


def test_regression_parser_accepts_only_the_recorded_ten_witness():
    test_name, test_source = regression_playback(KANI_LOG_EXCERPTS["regression"][1])
    assert test_name == "kani_concrete_playback_regression_2635896623606938374"
    assert "vec![10, 0, 0, 0]" in test_source


def test_playback_wrapper_leaves_the_frozen_proof_snapshot_unchanged(tmp_path: Path):
    source = ROOT / "examples/change-certification/proofs/experiment-kani.rs"
    snapshot = tmp_path / "experiment-kani.rs"
    snapshot.write_bytes(source.read_bytes())
    original = snapshot.read_bytes()
    _, generated_test = regression_playback(KANI_LOG_EXCERPTS["regression"][1])
    playback = tmp_path / "playback.rs"

    digest = make_playback_wrapper(snapshot, playback, generated_test)

    assert snapshot.read_bytes() == original
    assert playback.read_bytes().startswith(original + b"\n\n")
    assert "kani_concrete_playback_regression_" in playback.read_text(encoding="utf-8")
    assert digest == hashlib.sha256(playback.read_bytes()).hexdigest()


def test_missing_tools_are_blocked_without_download_or_engine_execution(tmp_path: Path):
    output = tmp_path / "new-output"
    missing_kani = tmp_path / "missing-kani"
    missing_verus = tmp_path / "missing-verus"
    result = run_probe(output, missing_kani, missing_verus)
    assert result.returncode == 2
    assert "BLOCKED: missing executable(s): kani, verus" in result.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "BLOCKED"
    assert report["runs"] == []
    assert list(path.name for path in output.iterdir()) == ["report.json"]


def test_nonempty_output_directory_is_refused_without_mutation(tmp_path: Path):
    output = tmp_path / "existing-output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("existing result", encoding="utf-8")
    result = run_probe(output, tmp_path / "missing-kani", tmp_path / "missing-verus")
    assert result.returncode == 2
    assert "output path must be an empty directory" in result.stderr
    assert marker.read_text(encoding="utf-8") == "existing result"
    assert [path.name for path in output.iterdir()] == ["keep.txt"]


def test_command_timeout_uses_the_process_tree_runner(tmp_path: Path):
    record, output = run_command(
        "timeout-control",
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(5)"],
        tmp_path,
        0.3,
    )
    assert record["classification"] == "timeout"
    assert record["timed_out"] is True
    assert record["returncode"] is None
    assert "ready" in output
    assert (tmp_path / record["stdout_log"]).read_text(encoding="utf-8").strip() == "ready"

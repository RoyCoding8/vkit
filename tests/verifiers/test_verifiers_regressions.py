from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkit.core.outcome import Blocked, BlockedReason
from vkit.verifiers import (
    comparator_runner,
    lean_adapter,
    node_adapter,
    property_adapter,
    pytest_adapter,
    static_adapter,
    tlc_adapter,
    tlc_runner,
    tlc_witness,
)
from vkit.verifiers.obligation import PropertyObligation, TheoremObligation
from vkit.verifiers.spec import (
    DomainBounds,
    FingerprintSpec,
    LeanCheck,
    LeanProfile,
    ModuleRef,
    NodeTestCheck,
    PinnedRunner,
    TlcCheck,
    ToolchainRef,
)


def _tlc_check(tmp_path: Path, *, config: str = "Model.cfg", bounds: tuple[tuple[str, int], ...] = ()) -> TlcCheck:
    return TlcCheck(
        id="tlc",
        cwd=tmp_path,
        timeout_seconds=60,
        artifact_name="tlc.json",
        model=ModuleRef("Model", "Model.tla"),
        config=config,
        properties=(PropertyObligation("Inv", bounds),),
        bounds=DomainBounds(bounds),
        fingerprint=FingerprintSpec(True, False, 1),
        toolchain=ToolchainRef("tlc", version="2.18", jar_sha256="a" * 64),
    )


def test_tlc_missing_completion_banner_reports_banner_not_queue(tmp_path: Path):
    (tmp_path / "Model.cfg").write_text("INVARIANT Inv\n", encoding="utf-8")
    report = {
        "version": tlc_adapter.REPORT_VERSION,
        "complete": True,
        "tool": "tlc",
        "tool_version": "TLC2 Version 2.18",
        "model": "Model",
        "model_path": "Model.tla",
        "config": "Model.cfg",
        "jar_sha256": "a" * 64,
        "fingerprint": {"constants_from_config": True, "checksum_states": False, "workers": 1},
        "exit_code": 0,
        "stdout": "5 states generated, 2 distinct states found, 0 states left on queue.\n",
        "stderr": "",
        "trace_json_supported": False,
        "witness_validation": None,
    }
    outcome = tlc_adapter.interpret(json.dumps(report).encode("utf-8"), _tlc_check(tmp_path))
    assert isinstance(outcome, Blocked)
    assert outcome.reason == BlockedReason.INTERNAL_ERROR
    assert "completion banner" in outcome.detail
    assert "left 0 states" not in outcome.detail


def test_tlc_constants_parses_singular_inline_comments_and_no_trailing_newline():
    cfg = (
        "CONSTANT A = 1..3 \\* inline comment\n"
        "(* block comment *)\n"
        "CONSTANTS\n"
        "  B = {x, y}\n"
        "INVARIANT Safe\n"
        "CONSTANT C = 42"
    )
    assert tlc_adapter._constants(cfg) == {"A": 3, "B": 2, "C": 1}


def test_tlc_witness_distinguishes_int_and_bool_in_actions():
    states = [[1, {"x": 1}], [2, {"x": 2}]]
    bad_action = [[1, {"x": True}], {"name": "Next"}, [2, {"x": 2}]]
    doc = {"vars": ["x"], "counterexample": {"state": states, "action": [bad_action]}}
    assert tlc_witness.trace_states(doc) is None

    s1 = [1, {"x": 1}]
    s2 = [2, {"x": True}]
    s3 = [3, {"x": 2}]
    doc2 = {
        "vars": ["x"],
        "counterexample": {
            "state": [s1, s2, s3],
            "action": [
                [s1, {"name": "IntToBool"}, s2],
                [s2, {"name": "BoolToTwo"}, s3],
            ],
        },
    }
    reduced = tlc_witness.without_internal_state(doc2, 2)
    assert reduced is not None
    # s1 -> s3 did not exist in original actions (s2 had True, not 1), so fallback descriptor is used
    assert reduced["counterexample"]["action"][0][1] == {"name": "Next"}


def test_lean_unresolved_reports_missing_theorem_named_in_error_message(tmp_path: Path):
    source = tmp_path / "Challenge.lean"
    source.write_text("theorem t1 : True := trivial\n", encoding="utf-8")
    check = LeanCheck(
        id="lean",
        cwd=tmp_path,
        timeout_seconds=60,
        artifact_name="lean.json",
        challenge=ModuleRef("Challenge", "Challenge.lean"),
        theorems=(TheoremObligation("t1", "Challenge"), TheoremObligation("t2", "Challenge")),
        profile=LeanProfile.REVIEWED,
        permitted_axioms=(),
        toolchain=ToolchainRef("lean", version="4.10.0"),
    )
    audit = {
        "name": "audit",
        "exit_code": 1,
        "messages": [
            {"severity": "info", "kind": "", "data": "'Challenge.t1' does not depend on any axioms"},
            {"severity": "error", "kind": "", "data": "unknown identifier 't2'"},
        ],
    }
    assert lean_adapter._unresolved(audit, check) == ("t2",)


def test_lean_source_diagnostics_rejects_boolean_coordinates(tmp_path: Path):
    source = tmp_path / "Challenge.lean"
    source.write_text("theorem t1 : True := trivial\n", encoding="utf-8")
    digest = lean_adapter._digest(source)
    check = LeanCheck(
        id="lean",
        cwd=tmp_path,
        timeout_seconds=60,
        artifact_name="lean.json",
        challenge=ModuleRef("Challenge", "Challenge.lean"),
        theorems=(TheoremObligation("t1", "Challenge"),),
        profile=LeanProfile.REVIEWED,
        permitted_axioms=(),
        toolchain=ToolchainRef("lean", version="4.10.0"),
    )
    report = {
        "version": lean_adapter.REPORT_VERSION,
        "complete": True,
        "tool": "lean",
        "tool_version": "Lean (version 4.10.0)",
        "module": "Challenge",
        "profile": "reviewed_proof_sources",
        "source": str(source),
        "source_sha256": digest,
        "theorems": ["t1"],
        "steps": [
            {
                "name": "build:1",
                "exit_code": 1,
                "messages": [
                    {
                        "severity": "error",
                        "kind": "",
                        "data": "boom",
                        "file": str(source),
                        "line": True,
                        "column": False,
                    }
                ],
            },
            {"name": "audit", "exit_code": 0, "messages": []},
        ],
    }
    diagnostics = lean_adapter.source_diagnostics(
        json.dumps(report).encode("utf-8"),
        check,
        [{"path": "Challenge.lean", "sha256": digest}],
        snapshot_root=tmp_path,
    )
    assert diagnostics == []


def _node_check(tmp_path: Path, required: tuple[str, ...]) -> NodeTestCheck:
    return NodeTestCheck(
        id="node",
        cwd=tmp_path,
        timeout_seconds=60,
        artifact_name="tap.txt",
        required_tests=required,
        runner=PinnedRunner("node", ("--test",)),
        report_format="tap",
        expect_report_version=13,
    )


def test_node_adapter_handles_ellipsis_in_message_and_missing_yaml_block(tmp_path: Path):
    tap_with_ellipsis = (
        "TAP version 13\n"
        "not ok 1 - fails with dots\n"
        "  ---\n"
        "  duration_ms: 1.5\n"
        "  type: 'test'\n"
        "  location: 'a.test.js'\n"
        "  message: \"expected ... to match ...\"\n"
        "  code: ERR_ASSERTION\n"
        "  ...\n"
        "1..1\n"
    ).encode("utf-8")
    result = node_adapter.interpret(tap_with_ellipsis, _node_check(tmp_path, ("a.test.js::fails with dots",)))
    assert isinstance(result, node_adapter.AdapterResult)
    assert len(result.counterexamples) == 1
    assert result.counterexamples[0][1] == "expected ... to match ..."

    tap_missing_block = (
        "TAP version 13\n"
        "ok 1 - no block\n"
        "ok 2 - has block\n"
        "  ---\n"
        "  duration_ms: 1.0\n"
        "  type: 'test'\n"
        "  location: 'b.test.js'\n"
        "  ...\n"
        "1..2\n"
    ).encode("utf-8")
    blocked = node_adapter.interpret(tap_missing_block, _node_check(tmp_path, ("b.test.js::no block",)))
    assert isinstance(blocked, Blocked)
    assert blocked.reason == BlockedReason.ARTIFACT_MALFORMED
    assert "no field block" in blocked.detail


def test_generator_validators_reject_booleans():
    base = {
        "max_examples": 10,
        "stateful_step_count": 0,
        "deadline": 1.0,
        "suppress_health_check": [],
        "database": None,
    }
    assert pytest_adapter._is_generator(base)
    assert not pytest_adapter._is_generator(base | {"max_examples": True})
    assert not pytest_adapter._is_generator(base | {"stateful_step_count": False})
    assert not pytest_adapter._is_generator(base | {"deadline": True})

    cfg = SimpleNamespace(getoption=lambda *_args, **_kw: json.dumps({"max_examples": True}))
    with pytest.raises(property_adapter.SettingsRefused):
        property_adapter.pytest_configure(cfg)


@pytest.mark.parametrize("payload", [b'{"runs": null}', b'{"runs": [123]}', b'{"runs": [{"results": [1]}]}'])
def test_static_adapter_blocks_malformed_sarif_structures(payload: bytes):
    outcome = static_adapter.read(payload)
    assert isinstance(outcome, Blocked)
    assert outcome.reason == BlockedReason.ARTIFACT_MALFORMED


def test_comparator_sandbox_handles_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(comparator_runner.shutil, "which", lambda _name: "/usr/bin/landrun")

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["landrun", "--version"], timeout=30)

    monkeypatch.setattr(comparator_runner.subprocess, "run", raise_timeout)
    assert comparator_runner.sandbox() == "missing"


def test_tlc_replay_candidate_cleans_up_temp_input_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_run(*_args, **_kwargs):
        raise OSError("java missing")

    monkeypatch.setattr(tlc_runner, "_run", fail_run)
    args = SimpleNamespace(java="java", workers=1, model="Model")
    replay_output = tmp_path / "replay.json"
    candidate = {"vars": ["x"], "counterexample": {"state": [[1, {"x": 0}]], "action": []}}
    replayed, reason = tlc_runner._replay_candidate(
        args, tmp_path / "tla2tools.jar", "Model", tmp_path, candidate, replay_output,
        tmp_path / "meta", "Inv", 7,
    )
    assert replayed is None
    assert "java missing" in reason
    assert not (tmp_path / "tlc-replay-input-7.json").exists()

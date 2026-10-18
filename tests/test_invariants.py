from __future__ import annotations

import json
import os

import pytest

from helpers import McpClient, vkit

from vkit.operations import invariants
from vkit.operations.invariants import infer_trace_relations


def _bundle(tmp_path, *, profile="states"):
    bundle = {"version": 1, "profile": profile, "variables": {"x": "int", "y": "int"},
              "observations": [{"trace_id": "run", "point_id": "point", "step": i,
                                "values": {"x": i, "y": 2 * i + 3}} for i in range(20)]}
    (tmp_path / "trace.json").write_text(json.dumps(bundle), encoding="utf-8")
    return bundle


def test_ci_requires_configured_daikon_engine():
    if os.environ.get("VKIT_REQUIRE_DAIKON_ENGINE"):
        assert os.environ.get("VKIT_DAIKON_JAR")


@pytest.mark.skipif(not os.environ.get("VKIT_DAIKON_JAR"), reason="pinned Daikon engine is not configured")
def test_real_engine_discovers_affine_relation_and_checks_every_row(tmp_path):
    _bundle(tmp_path)
    result = infer_trace_relations(tmp_path, "trace.json")
    assert result["status"] == "ANALYZED_SAMPLES", result.get("reason")
    relation = next(row for row in result["candidates"] if row["expression"] == "2 * v0 - v1 + 3 == 0")
    assert (relation["status"], relation["samples"], relation["traces"]) == ("OBSERVED_IN_SAMPLES", 20, 1)
    assert result["counterobserved"] == []
    assert result["variable_mapping"] == {"v0": "x", "v1": "y"}


@pytest.mark.skipif(not os.environ.get("VKIT_DAIKON_JAR"), reason="pinned Daikon engine is not configured")
def test_trace_relation_operation_cli_and_mcp(make_project):
    project = make_project({}, [], accept=False)
    _bundle(project)
    code, body = vkit(project, "compute", "infer-trace-relations", "--path", "trace.json")
    assert (code, body["status"]) == (0, "ANALYZED_SAMPLES")
    client = McpClient(project)
    try:
        body, error = client.call("infer_trace_relations", path="trace.json")
        assert not error and body["status"] == "ANALYZED_SAMPLES"
    finally:
        client.close()


@pytest.mark.skipif(not os.environ.get("VKIT_DAIKON_JAR"), reason="pinned Daikon engine is not configured")
def test_consecutive_pairs_stay_inside_trace_and_point(tmp_path):
    bundle = _bundle(tmp_path, profile="consecutive")
    bundle["observations"][10]["trace_id"] = "other"
    (tmp_path / "trace.json").write_text(json.dumps(bundle), encoding="utf-8")
    result = infer_trace_relations(tmp_path, "trace.json")
    assert result["status"] == "ANALYZED_SAMPLES", result.get("reason")
    assert result["variable_mapping"] == {"b0": "before.x", "b1": "before.y", "a0": "after.x", "a1": "after.y"}
    assert all(row["samples"] == 18 for row in result["candidates"])


def test_wrong_scalar_type_is_rejected(tmp_path):
    bundle = _bundle(tmp_path)
    bundle["observations"][0]["values"]["x"] = True
    (tmp_path / "trace.json").write_text(json.dumps(bundle), encoding="utf-8")
    assert infer_trace_relations(tmp_path, "trace.json")["status"] == "UNSUPPORTED"


def test_reordered_observation_steps_are_rejected(tmp_path):
    bundle = _bundle(tmp_path)
    bundle["observations"].reverse()
    (tmp_path / "trace.json").write_text(json.dumps(bundle), encoding="utf-8")
    assert infer_trace_relations(tmp_path, "trace.json")["status"] == "UNSUPPORTED"


def test_missing_engine_is_unavailable(tmp_path, monkeypatch):
    _bundle(tmp_path)
    monkeypatch.delenv("VKIT_DAIKON_JAR", raising=False)
    assert infer_trace_relations(tmp_path, "trace.json")["status"] == "UNAVAILABLE"


@pytest.mark.skipif(not os.environ.get("VKIT_DAIKON_JAR"), reason="pinned Daikon engine is not configured")
def test_engine_candidate_disagreement_keeps_a_contradicting_row(tmp_path, monkeypatch):
    _bundle(tmp_path)
    monkeypatch.setattr(invariants, "_candidates", lambda *_: iter([("point", "v0 == 999")]))
    result = infer_trace_relations(tmp_path, "trace.json")
    assert result["candidates"] == []
    assert result["counterobserved"][0]["counterexample"] == {
        "row": 0, "trace_id": "run", "values": {"v0": 0, "v1": 3}}


@pytest.mark.parametrize("expression", ["__import__('os')", "v0 / 0 == 0", "v0 ** 2 == 0", "unknown == 1"])
def test_untrusted_engine_expression_language_is_never_executed(expression):
    with pytest.raises((ValueError, SyntaxError)):
        invariants._expression(expression, {"v0"})

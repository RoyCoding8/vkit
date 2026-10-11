from __future__ import annotations

import json

import pytest

pytest.importorskip("cvc5")

from helpers import McpClient, vkit
from vkit.operations import semantic_merge
from vkit.operations.semantic_merge import check_semantic_merge


def _inputs(project, bodies, *, annotations=None):
    for role, body in zip(("base", "left", "right", "merged"), bodies):
        signature = (annotations or {}).get(role, "x: int")
        (project / f"{role}.py").write_text(f"def f({signature}) -> int:\n    return {body}\n", encoding="utf-8")
    bundle = {"version": 1, "function": "f", **{role: f"{role}.py" for role in ("base", "left", "right", "merged")}}
    (project / "merge.json").write_text(json.dumps(bundle), encoding="utf-8")


def test_disjoint_input_changes_are_preserved(tmp_path):
    _inputs(tmp_path, ("x", "1 if x == 0 else x", "-1 if x == 2 else x",
                       "1 if x == 0 else (-1 if x == 2 else x)"))
    result = check_semantic_merge(tmp_path, "merge.json")
    assert result["status"] == "PROVED"
    assert result["function"] == "f" and result["timeout_seconds"] == 10
    assert set(result["sources"]) == {"base", "left", "right", "merged"}


@pytest.mark.parametrize("bodies,expected,returns", [
    (("x", "x + 1", "x", "x"), ["left_change_lost"], {"base": 0, "left": 1, "right": 0, "merged": 0}),
    (("x", "x", "x + 1", "x"), ["right_change_lost"], {"base": 0, "left": 0, "right": 1, "merged": 0}),
    (("x", "x", "x", "x + 1"), ["unchanged_input_modified"], {"base": 0, "left": 0, "right": 0, "merged": 1}),
])
def test_each_preservation_obligation_has_replayed_evidence(tmp_path, bodies, expected, returns):
    _inputs(tmp_path, bodies)
    result = check_semantic_merge(tmp_path, "merge.json")
    assert result["status"] == "COUNTEREXAMPLE"
    assert result["violations"] == expected
    offset = result["arguments"]["x"]
    assert result["returns"] == {role: value + offset for role, value in returns.items()}
    assert result["replayed"] is True


def test_identical_overlapping_changes_coalesce(tmp_path):
    _inputs(tmp_path, ("x", "x + 1", "x + 1", "x + 1"))
    assert check_semantic_merge(tmp_path, "merge.json")["status"] == "PROVED"


def test_conflicting_overlapping_changes_cannot_be_hidden(tmp_path):
    _inputs(tmp_path, ("x", "x + 1", "x + 2", "x + 1"))
    result = check_semantic_merge(tmp_path, "merge.json")
    assert result["status"] == "COUNTEREXAMPLE"
    assert result["violations"] == ["right_change_lost"]


def test_signature_mismatch_is_unsupported(tmp_path):
    _inputs(tmp_path, ("x", "x", "x", "x"), annotations={"right": "x: bool"})
    assert check_semantic_merge(tmp_path, "merge.json")["status"] == "UNSUPPORTED"


def test_unsupported_effects_are_never_executed(tmp_path):
    _inputs(tmp_path, ("x", "open('marker', 'w').write('bad')", "x", "x"))
    assert check_semantic_merge(tmp_path, "merge.json")["status"] == "UNSUPPORTED"
    assert not (tmp_path / "marker").exists()


def test_boolean_input_model_and_constant_functions(tmp_path):
    _inputs(tmp_path, ("0", "1 if x else 0", "0 if x else 2", "1 if x else 2"),
            annotations={role: "x: bool" for role in ("base", "left", "right", "merged")})
    assert check_semantic_merge(tmp_path, "merge.json")["status"] == "PROVED"
    for role, value in (("base", 0), ("left", 1), ("right", 0), ("merged", 1)):
        (tmp_path / f"{role}.py").write_text(f"def f() -> int:\n    return {value}\n")
    assert check_semantic_merge(tmp_path, "merge.json")["status"] == "PROVED"


def test_failed_independent_replay_never_reports_a_conflict(monkeypatch):
    sources = {role: f"def f(x: int) -> int:\n    return {body}\n" for role, body in
               zip(("base", "left", "right", "merged"), ("x", "x+1", "x", "x"))}
    monkeypatch.setattr(semantic_merge, "_evaluate", lambda *_: 0)
    result = semantic_merge._check(sources, "f", 2_000)
    assert result["status"] == "UNKNOWN"
    assert "failed independent IR replay" in result["reason"]


def test_mismatched_backend_is_unavailable(monkeypatch):
    import cvc5

    monkeypatch.setattr(cvc5, "__version__", "unsupported")
    sources = {role: "def f(x: int) -> int:\n    return x\n" for role in ("base", "left", "right", "merged")}
    assert semantic_merge._check(sources, "f", 2_000)["status"] == "UNAVAILABLE"


def test_cli_and_mcp_use_the_same_merge_contract(make_project):
    project = make_project({}, [], accept=False)
    _inputs(project, ("x", "x + 1", "x", "x + 1"))
    code, body = vkit(project, "compute", "check-semantic-merge", "--path", "merge.json")
    assert (code, body["status"]) == (0, "PROVED")
    client = McpClient(project)
    try:
        body, error = client.call("check_semantic_merge", path="merge.json")
        assert not error and body["status"] == "PROVED"
    finally:
        client.close()

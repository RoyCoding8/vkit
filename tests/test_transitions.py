from __future__ import annotations

from copy import deepcopy
import json

import pytest

from helpers import DRIVER, McpClient, scenario_check, vkit
from vkit.cli import main
from vkit.formal.transitions import compare_transition_request
from vkit.paths import open_project
from vkit.schemas import SchemaValidationError
from vkit.store import Store


def model():
    states = ["idle", "working", "done", "unreachable"]
    return {
        "states": states, "actions": ["step"], "inputs": ["go", "wait"],
        "observations": ["zero", "one", "two"], "initial_states": ["idle"],
        "assumptions": [{"state": state, "action": "step", "input": value}
                        for state in states for value in ["go", "wait"]],
        "rules": [
            {"source": "idle", "action": "step", "inputs": ["go"], "targets": ["working"]},
            {"source": "working", "action": "step", "inputs": ["go"], "targets": ["done"]},
        ],
        "state_observations": [{"state": state, "value": value}
                               for state, value in zip(states, ["zero", "one", "two", "zero"])],
    }


def request(reference=None, candidate=None, **extra):
    return {"schema_version": 1, "reference": reference or model(),
            "candidate": candidate or model(), **extra}


def test_reordered_models_have_canonical_identities_and_complete_equivalence():
    candidate = model()
    for value in candidate.values():
        value.reverse()
    report = compare_transition_request(request(candidate=candidate))

    assert report["kind"] == "equivalent"
    assert report["complete"] is True
    assert report["reference_sha256"] == report["candidate_sha256"]
    assert report["counts"]["candidate"] == {
        "declared_states": 4, "declared_contexts": 8, "expanded_rule_edges": 2,
        "reachable_states": 3, "effective_edges": 2, "examined_contexts": 8,
    }
    assert (report["source_proof"], report["approval_authority"]) == (False, False)


def test_changed_reachable_transition_has_a_shortest_concrete_witness():
    candidate = model()
    candidate["rules"][1]["targets"] = ["idle"]
    report = compare_transition_request(request(candidate=candidate))
    transition = next(delta for delta in report["deltas"] if delta["kind"] == "transitions")

    assert (report["kind"], report["complete"]) == ("different", True)
    assert transition == {
        "kind": "transitions", "removed": 1, "added": 1,
        "witness": {
            "side": "candidate", "initial_state": "idle",
            "path": [
                {"source": "idle", "action": "step", "input": "go", "target": "working"},
                {"source": "working", "action": "step", "input": "go", "target": "idle"},
            ],
            "difference": ["working", "step", "go", "idle"],
        },
    }
    assert report["counts"]["candidate"]["reachable_states"] == 2


def test_unreachable_or_assumption_masked_rules_do_not_change_reachable_behavior():
    candidate = model()
    candidate["rules"].append({"source": "unreachable", "action": "step",
                               "inputs": ["go"], "targets": ["idle"]})
    assert compare_transition_request(request(candidate=candidate))["kind"] == "equivalent"

    reference = model()
    reference["assumptions"] = [row for row in reference["assumptions"] if row["input"] != "wait"]
    candidate = deepcopy(reference)
    candidate["rules"][0]["inputs"].append("wait")
    assert compare_transition_request(request(reference, candidate))["kind"] == "equivalent"


@pytest.mark.parametrize("change, expected", [
    ("initial", "initial_states"), ("assumption", "assumptions"),
    ("guard", "guards"), ("observation", "observations"),
])
def test_each_reachable_semantic_change_is_identified(change, expected):
    candidate = model()
    if change == "initial":
        candidate["initial_states"] = ["working"]
    elif change == "assumption":
        candidate["assumptions"] = [row for row in candidate["assumptions"]
                                    if (row["state"], row["input"]) != ("idle", "wait")]
    elif change == "guard":
        candidate["rules"][0]["inputs"].append("wait")
    else:
        candidate["state_observations"][1]["value"] = "zero"
    report = compare_transition_request(request(candidate=candidate))

    assert (report["kind"], report["complete"]) == ("different", True)
    assert expected in {delta["kind"] for delta in report["deltas"]}


def test_budget_exhaustion_and_domain_mismatch_cannot_report_equivalence():
    report = compare_transition_request(request(max_contexts=1))
    assert (report["kind"], report["complete"], report["reason"]) == (
        "unknown", False, "context_budget_exceeded",
    )
    candidate = model()
    candidate["observations"].append("other")
    report = compare_transition_request(request(candidate=candidate))
    assert (report["kind"], report["complete"], report["different_domains"]) == (
        "incomparable", False, ["observations"],
    )


def test_nondeterministic_rules_use_the_shortest_path_from_all_initial_states():
    reference = model()
    reference["initial_states"] = ["idle", "working"]
    candidate = deepcopy(reference)
    candidate["rules"][1]["targets"].append("idle")
    report = compare_transition_request(request(reference, candidate))
    witness = next(delta["witness"] for delta in report["deltas"] if delta["kind"] == "transitions")
    assert witness["initial_state"] == "working"
    assert witness["path"] == [
        {"source": "working", "action": "step", "input": "go", "target": "idle"},
    ]


def test_expanded_edge_budget_does_not_allow_an_incomplete_pass():
    states = [f"s{number}" for number in range(16)]
    actions = [f"a{number}" for number in range(16)]
    inputs = [f"i{number}" for number in range(16)]
    large = {
        "states": states, "actions": actions, "inputs": inputs, "observations": ["zero"],
        "initial_states": ["s0"], "assumptions": [],
        "rules": [{"source": state, "action": action, "inputs": inputs, "targets": states}
                  for state in states for action in actions],
        "state_observations": [{"state": state, "value": "zero"} for state in states],
    }
    report = compare_transition_request(request(large, deepcopy(large)))
    assert (report["kind"], report["complete"], report["reason"]) == (
        "unknown", False, "expanded_edge_budget_exceeded",
    )


@pytest.mark.parametrize("change", ["unknown_field", "dangling_target", "bad_assumption",
                                   "duplicate_state", "duplicate_observation", "duplicate_rule",
                                   "empty_initial", "noninteger_version"])
def test_invalid_models_are_rejected_at_the_boundary(change):
    raw = request()
    candidate = raw["candidate"]
    if change == "unknown_field":
        candidate["approval"] = True
    elif change == "dangling_target":
        candidate["rules"][0]["targets"] = ["missing"]
    elif change == "bad_assumption":
        candidate["assumptions"][0]["input"] = "missing"
    elif change == "duplicate_state":
        candidate["states"].append("idle")
    elif change == "duplicate_observation":
        candidate["state_observations"][1]["state"] = "idle"
    elif change == "duplicate_rule":
        candidate["rules"].append(deepcopy(candidate["rules"][0]))
    elif change == "empty_initial":
        candidate["initial_states"] = []
    else:
        raw["schema_version"] = True
    with pytest.raises((ValueError, SchemaValidationError)):
        compare_transition_request(raw)


def test_public_cli_and_mcp_agree_on_models_without_creating_approval(make_project, tmp_path):
    project = make_project({"check.py": DRIVER},
                           [scenario_check("check", "check.py", scenarios=["ok"], inputs=["check.py"])],
                           accept=False)
    client = McpClient(project)
    try:
        for case, expected, exit_code in [("same", "equivalent", 0), ("changed", "different", 1),
                                          ("budget", "unknown", 3)]:
            raw = request()
            if case == "changed":
                raw["candidate"]["rules"][1]["targets"] = ["idle"]
            elif case == "budget":
                raw["max_contexts"] = 1
            path = tmp_path / f"{case}.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            code, cli = vkit(project, "model", "compare", "--request", str(path))
            mcp, error = client.call("model_compare", request=raw)
            assert (code, error, cli["kind"]) == (exit_code, False, expected)
            assert cli == mcp
            assert (cli["source_proof"], cli["approval_authority"]) == (False, False)
        assert Store.of(open_project(project)).approved() == {}
    finally:
        client.close()


def test_human_cli_shows_the_concrete_transition_witness(tmp_path, capsys):
    candidate = model()
    candidate["rules"][1]["targets"] = ["idle"]
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request(candidate=candidate)), encoding="utf-8")

    assert main(["model", "compare", "--request", str(path), "--project", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "idle --step(go)--> working\n    working --step(go)--> idle" in output
    assert "difference present in candidate: working, step, go, idle" in output

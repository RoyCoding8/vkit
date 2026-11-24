from __future__ import annotations

import pytest

from helpers import vkit


@pytest.mark.parametrize("kind,fields,category,establishes,limits", [
    ("scenario", {"command": ["unused-driver"], "required_scenarios": ["case"]}, "scenario",
     "this named sequence of calls produced this observed result", "anything about a sequence that was not run"),
    ("pytest", {"required_tests": ["test.py::case"], "runner": {"executable": "pytest", "base_argv": []},
                "report_format": "pytest_json_report", "expect_report_version": 1}, "scenario",
     "this named sequence of calls produced this observed result", "anything about a sequence that was not run"),
    ("node_test", {"required_tests": ["test.mjs::case"], "runner": {"executable": "node", "base_argv": []},
                   "report_format": "node_tap", "expect_report_version": 13}, "scenario",
     "this named sequence of calls produced this observed result", "anything about a sequence that was not run"),
    ("property", {"required_tests": ["test.py::case"], "runner": {"executable": "pytest", "base_argv": []},
                  "report_format": "pytest_json_report", "expect_report_version": 1,
                  "generator": {"max_examples": 10, "stateful_step_count": 0, "deadline": None,
                                "suppress_health_check": []}}, "property",
     "no disagreement was found between the code and a separate reference model over the generated sequences",
     "implementation equivalence; the models differ mechanically and the sequences are sampled, not exhausted"),
    ("static", {"command": ["unused-analyzer"]}, "static_analysis",
     "the analyzer reported no finding outside the baseline a human accepted",
     "the absence of defects the analyzer's rules cannot see"),
    ("lean", {"challenge": {"module": "Challenge", "path": "Challenge.lean"}, "theorems": ["identity"],
              "profile": "reviewed_proof_sources", "permitted_axioms": [], "toolchain": {"tool": "lean"}},
     "theorem_checking", "the stated theorem holds for the Lean model, with the axioms audited",
     "anything about the Python core"),
    ("tlc", {"model": {"module": "Model", "path": "Model.tla"}, "config": "Model.cfg",
             "properties": ["TypeOK"], "bounds": {"Workers": 2},
             "fingerprint": {"constants_from_config": True, "checksum_states": True, "workers": 1},
             "toolchain": {"tool": "tlc"}}, "finite_model_checking",
     "the checked properties hold in every reachable state of one finite model at one recorded configuration",
     "any other number of workers, revisions or resources, and any implementation in any language"),
], ids=["scenario", "pytest", "node_test", "property", "static", "lean", "tlc"])
def test_status_carries_the_registered_checks_category_and_limits(
        make_project, kind, fields, category, establishes, limits):
    check = {"id": "example", "kind": kind, "timeout_seconds": 10, "artifact": "result.json",
             "subject": {"paths": ["README.md"], "digest": None}, "claim_id": "example", **fields}
    project = make_project({"README.md": "category scope\n", "Challenge.lean": "", "Model.tla": "",
                            "Model.cfg": ""}, [check], accept=False)

    code, body = vkit(project, "status")

    assert code == 0, body
    assert body["manifest_error"] is None, body
    assert len(body["checks"]) == 1
    reported = body["checks"][0]
    assert (reported["category"], reported["establishes"], reported["does_not_establish"]) == (
        category, establishes, limits)
    assert reported["state"] == "not_approved"

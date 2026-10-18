from __future__ import annotations

import json

from helpers import DRIVER, scenario_check, vkit

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def feature_map(*features: dict) -> str:
    return json.dumps({"schema_version": 2, "features": list(features)})


def test_features_report_reach_steps_freshness_and_audit_problems(make_project):
    project = make_project({
        "app.py": "print('hi')\n", "ok.py": PASSES,
        "verification/features.json": feature_map(
            {"id": "greets", "behavior": "prints a greeting", "how_to_reach": ["run python app.py"],
             "entry_points": ["app.py"], "covered_by": ["ok"], "gaps": []},
            {"id": "exports", "behavior": "exports a report", "how_to_reach": ["run python app.py --export"],
             "entry_points": ["export.py"], "covered_by": ["gone"], "gaps": ["nobody checked the file format"]},
        ),
    }, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py", "app.py"])])

    code, body = vkit(project, "features")
    by_id = {f["id"]: f for f in body["features"]}
    assert code == 3
    assert (by_id["greets"]["verified"], by_id["greets"]["checks"]) == (False, [{"id": "ok", "state": "missing"}])
    assert by_id["exports"]["problems"] == ["entry point 'export.py' does not exist", "check 'gone' is not registered"]

    vkit(project, "check", "run", "--check", "ok")
    code, body = vkit(project, "status", "--path", "app.py")
    assert [(f["id"], f["verified"]) for f in body["features"]] == [("greets", True)]


def test_a_feature_map_with_an_unknown_field_is_refused(make_project):
    project = make_project({"ok.py": PASSES, "verification/features.json": feature_map(
        {"id": "x", "behavior": "y", "expected_outcome": "prints 5"})},
        [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    code, body = vkit(project, "features")
    assert code == 2 and "unknown field(s) expected_outcome" in body["error"]

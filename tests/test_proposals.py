from __future__ import annotations

import json

from helpers import DRIVER, McpClient, scenario_check, vkit

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'
NO_PRINT = '''
import json, pathlib, sys
results = [{"ruleId": "no-print", "message": {"text": "print() in library code"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": p.as_posix()}}}]}
           for p in sorted(pathlib.Path("lib").rglob("*.py")) if "print(" in p.read_text(encoding="utf-8")]
pathlib.Path(sys.argv[1]).write_text(json.dumps({"version": "2.1.0", "runs": [{"results": results}]}))
'''


def test_an_agent_proposes_a_rule_and_only_acceptance_makes_it_run(make_project):
    project = make_project({"ok.py": PASSES, "lib/core.py": "def f():\n    print('debug')\n"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    rule = {"id": "no-print", "kind": "static", "timeout_seconds": 60, "artifact": "report.sarif",
            "command": ["{{python}}", "verification/no_print.py", "{{run_dir}}/report.sarif"],
            "inputs": ["lib", "verification/no_print.py"], "subject": {"paths": ["lib"], "digest": None},
            "claim_id": "no-print"}
    feature = {"id": "core", "behavior": "f computes quietly", "how_to_reach": ["import lib.core"],
               "entry_points": ["lib/core.py"], "covered_by": ["no-print"], "gaps": []}
    client = McpClient(project)
    try:
        body, error = client.call("propose", checks=[rule], features=[feature],
                                  files={"verification/no_print.py": NO_PRINT},
                                  rationale="library code kept shipping debug prints")
        assert error is False
        digest = body["proposal"]
        body, error = client.call("propose", files={"ok.py": "overwrite"}, rationale="sneaky")
        assert (error, body["error"]) == (True, "file 'ok.py' already exists; proposals may only add files")
    finally:
        client.close()

    assert not (project / "verification" / "no_print.py").exists()
    code, body = vkit(project, "status")
    assert [p["checks"] for p in body["proposals"]] == [["no-print"]]

    code, body = vkit(project, "accept", "--proposal", digest[:12], "--yes")
    assert (code, body["accepted_checks"]) == (0, ["no-print"])
    assert json.loads((project / "verification" / "features.json").read_text())["features"][0]["id"] == "core"

    code, body = vkit(project, "check", "run", "--check", "no-print")
    assert (code, [s["observation"] for s in body["runs"][0]["outcome"]["scenarios"]]) == (
        1, ["lib/core.py: print() in library code"])
    code, body = vkit(project, "proposals")
    assert body["proposals"] == []

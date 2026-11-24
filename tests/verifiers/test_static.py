from __future__ import annotations

from helpers import vkit

ANALYZER = '''
import json, pathlib, sys
results = []
for path in sorted(pathlib.Path("src").rglob("*.py")):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "TODO" in line:
            results.append({"ruleId": "no-todo", "message": {"text": line.strip()},
                            "locations": [{"physicalLocation": {"artifactLocation": {"uri": path.as_posix()},
                                                                "region": {"startLine": number}}}]})
pathlib.Path(sys.argv[1]).write_text(json.dumps({"version": "2.1.0", "runs": [{"results": results}]}))
sys.exit(1 if results else 0)
'''


def lint_check() -> dict:
    return {"id": "lint", "kind": "static", "timeout_seconds": 60, "artifact": "report.sarif",
            "command": ["{{python}}", "analyze.py", "{{run_dir}}/report.sarif"], "inputs": ["src", "analyze.py"],
            "subject": {"paths": ["src"], "digest": None}, "claim_id": "lint"}


def test_only_findings_outside_the_pinned_baseline_fail(make_project):
    project = make_project({"analyze.py": ANALYZER, "src/app.py": "x = 1  # TODO old debt\n"}, [lint_check()])
    code, body = vkit(project, "check", "run", "--check", "lint")
    run = body["runs"][0]
    assert (code, run["category"], [s["observation"] for s in run["outcome"]["scenarios"]]) == (
        1, "static_analysis", ["src/app.py:1: x = 1  # TODO old debt"])

    code, body = vkit(project, "baseline", "--check", "lint")
    assert (code, body["findings"]) == (0, {"no-todo|src/app.py|x = 1  # TODO old debt": 1})

    code, body = vkit(project, "check", "run", "--needed")
    assert (code, body["runs"][0]["outcome"]["scenarios"][0]["observation"]) == (
        0, "1 finding(s), all in the accepted baseline")

    (project / "src" / "app.py").write_text("import os\nx = 1  # TODO old debt\ny = 2  # TODO new debt\n",
                                            encoding="utf-8")
    code, body = vkit(project, "check", "run", "--needed")
    assert (code, [s["observation"] for s in body["runs"][0]["outcome"]["scenarios"]]) == (
        1, ["src/app.py:3: y = 2  # TODO new debt"])

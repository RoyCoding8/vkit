from __future__ import annotations

from helpers import DRIVER, McpClient, scenario_check

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def test_an_agent_sees_staleness_runs_what_is_needed_and_reaches_ready(make_project):
    project = make_project({"ok.py": PASSES, "README.md": "hi"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    client = McpClient(project)
    try:
        tools = sorted(t["name"] for t in client.request("tools/list", {})["tools"])
        assert tools == ["check_history", "check_proto_compatibility", "check_rewrite", "check_run",
                         "compare_iteration_sets", "compare_matchsets", "features", "gate", "minimize_cover", "propose", "reduce_failure",
                         "run_cancel", "run_get", "simplify_function", "status"]

        body, error = client.call("gate")
        assert (error, body["verdict"], body["checks"][0]["state"]) == (False, "BLOCKED", "missing")

        body, error = client.call("check_run", needed=True, wait_seconds=60)
        assert (error, [r["outcome"]["result"] for r in body["runs"]]) == (False, ["PASS"])
        run_id = body["runs"][0]["run_id"]

        body, _ = client.call("gate")
        assert body["verdict"] == "READY"

        (project / "ok.py").write_text(PASSES + "\n", encoding="utf-8")
        body, _ = client.call("status", paths=["ok.py", "README.md"])
        assert ([c["state"] for c in body["checks"]], body["unmapped_paths"]) == (["stale"], ["README.md"])

        body, _ = client.call("run_get", run_id=run_id)
        assert (body["state"], body["outcome"]["scenarios"][0]["observation"]) == ("done", "printed ok")
    finally:
        client.close()


def test_a_request_with_a_command_or_unknown_field_is_refused(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    client = McpClient(project)
    try:
        body, error = client.call("check_run", command="rm -rf /")
        assert (error, body["error"]) == (True, "unknown argument(s): command")
        body, error = client.call("check_run", check_ids=["not-registered"])
        assert error and "unknown check 'not-registered'" in body["error"]
    finally:
        client.close()

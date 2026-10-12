from __future__ import annotations

from helpers import DRIVER, McpClient, scenario_check

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'


def test_an_agent_sees_staleness_runs_what_is_needed_and_reaches_ready(make_project):
    project = make_project({"ok.py": PASSES, "README.md": "hi"},
                           [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    client = McpClient(project)
    try:
        tools = sorted(t["name"] for t in client.request("tools/list", {})["tools"])
        assert tools == sorted(["analyze_affine_accesses", "analyze_dependencies", "analyze_loop_termination", "analyze_regex_cost", "analyze_roundoff", "check_api_sequences", "check_c_safety", "check_history",
                         "check_protocol_model", "check_rewrite", "check_run", "check_semantic_merge", "compare_authorization_policies", "compare_contracts", "compare_iteration_sets",
                         "compare_matchsets", "compare_migrations", "compare_network_paths", "compare_rational_expressions", "compare_sql_queries", "compare_wheels", "compare_workload_cost", "features",
                         "explain_trace_interval", "find_behavioral_difference", "find_cache_key_collision", "find_data_flows", "find_grammar_ambiguity",
                         "gate", "generate_configurations", "help", "infer_trace_relations", "minimize_cover", "mutation_survivors",
                         "preview_typescript_symbol_rename", "propose", "prove_c_contract", "reduce_failure", "replay_retained_cases", "resolve_dependencies", "run_cancel", "run_get",
                         "simplify_function", "status", "synthesize_function"])

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

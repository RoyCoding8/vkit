from __future__ import annotations

import subprocess

import pytest

from vkit.manifest import parse_manifest
from vkit.operations.workloads import _Unsupported, _git, compare_workload_cost
from vkit.paths import open_project
from vkit.runner import run_check
from vkit.store import Store


def _check(subject_paths: list[str] | None = None) -> dict:
    return {
        "id": "workload",
        "kind": "scenario",
        "timeout_seconds": 10,
        "artifact": "result.json",
        "command": ["{{python}}", "verification/driver.py", "{{run_dir}}/result.json"],
        "required_scenarios": ["workload-ran"],
        "budgets": [{"name": "latency", "max_regression_pct": 25}],
        "inputs": ["src/product.py", "verification/driver.py"],
        "subject": {"paths": subject_paths or ["src/product.py"], "digest": None},
        "claim_id": "workload",
    }


def _commit(project) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _record(project) -> str:
    opened = open_project(project)
    return run_check(opened, parse_manifest(opened), "workload")["run_id"]


def test_workload_comparison_uses_committed_subject_and_paired_scenarios(make_project):
    project = make_project({
        "src/product.py": "VALUE = 0\n",
        "verification/driver.py": (
            "import json, pathlib, sys\n"
            "VALUE = __import__('runpy').run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'src/product.py'))['VALUE']\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': str(pathlib.Path.cwd())}],"
            "'measurements': [{'name': 'latency', 'value': 1 if VALUE == 0 else 3, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_id = _record(project)

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "MEASURED"
    assert result["completed_pairs"] == 10
    assert [row["order"] for row in result["samples"][:2]] == [["before", "candidate"], ["candidate", "before"]]
    measurement = result["measurements"][0]
    assert measurement["name"] == "latency"
    assert measurement["median_regression_pct"] == 200
    assert measurement["confidence_interval_95_pct"] == [200, 200]
    assert measurement["status"] == "OVER_BUDGET"
    assert all(row["measurements"]["latency"]["before"] == 1 for row in result["samples"])
    assert all(row["measurements"]["latency"]["candidate"] == 3 for row in result["samples"])
    assert (project / "src/product.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_git_blob_capture_stops_at_the_shared_process_output_limit(make_project):
    project = make_project({"payload.bin": "x" * 8192}, [], accept=False)
    object_id = subprocess.check_output(
        ["git", "rev-parse", "HEAD:payload.bin"], cwd=project, text=True,
    ).strip()

    with pytest.raises(_Unsupported, match="output limits"):
        _git(open_project(project), "cat-file", "blob", object_id, max_output_bytes=1024)


def test_workload_comparison_rejects_changed_captured_driver(make_project):
    project = make_project({
        "src/product.py": "VALUE = 1\n",
        "verification/driver.py": (
            "import json, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': 'ran'}],"
            "'measurements': [{'name': 'latency', 'value': 1, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    before_ref = _commit(project)
    run_id = _record(project)
    (project / "verification/driver.py").write_text("raise RuntimeError('changed')\n", encoding="utf-8")

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "UNSUPPORTED"
    assert "input snapshot" in result["detail"]


def test_workload_comparison_keeps_failed_baseline_out_of_completed_pairs(make_project):
    project = make_project({
        "src/product.py": "VALUE = 0\n",
        "verification/driver.py": (
            "import json, pathlib, runpy, sys\n"
            "value = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'src/product.py'))['VALUE']\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'FAIL' if value == 0 else 'PASS', 'observation': 'subject outcome'}],"
            "'measurements': [{'name': 'latency', 'value': 1 if value == 0 else 3, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_id = _record(project)

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "INCONCLUSIVE"
    assert result["completed_pairs"] == 0
    assert result["samples"] == []
    assert "scenario failed" in result["detail"]


def test_workload_comparison_refuses_a_revoked_approval(make_project):
    project = make_project({
        "src/product.py": "VALUE = 1\n",
        "verification/driver.py": (
            "import json, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': 'ran'}],"
            "'measurements': [{'name': 'latency', 'value': 1, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    before_ref = _commit(project)
    run_id = _record(project)
    approval_file = Store.of(open_project(project)).root / "approved.json"
    approval_file.write_text("{}\n", encoding="utf-8")

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "UNSUPPORTED"
    assert "no longer approved" in result["detail"]


def test_workload_comparison_rejects_subject_that_is_the_scenario_driver(make_project):
    project = make_project({
        "src/product.py": "VALUE = 1\n",
        "verification/driver.py": (
            "import json, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': 'ran'}],"
            "'measurements': [{'name': 'latency', 'value': 1, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check(["src/product.py", "verification/driver.py"])])
    before_ref = _commit(project)
    run_id = _record(project)

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "UNSUPPORTED"
    assert "command input" in result["detail"]


def test_workload_comparison_rejects_symlink_mode_in_before_commit(make_project):
    project = make_project({
        "src/product.py": "VALUE = 1\n",
        "verification/driver.py": (
            "import json, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': 'ran'}],"
            "'measurements': [{'name': 'latency', 'value': 1, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    run_id = _record(project)
    target = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=project, input=b"target",
                            check=True, capture_output=True).stdout.decode("ascii").strip()
    subprocess.run(["git", "update-index", "--cacheinfo", f"120000,{target},src/product.py"],
                   cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@vkit", "-c", "user.name=test", "commit", "-qm", "symlink mode"],
                   cwd=project, check=True, capture_output=True)
    before_ref = _commit(project)

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "UNSUPPORTED"
    assert "symlink" in result["detail"]


def test_workload_comparison_rejects_measurement_unit_mismatch(make_project):
    project = make_project({
        "src/product.py": "VALUE = 0\n",
        "verification/driver.py": (
            "import json, pathlib, runpy, sys\n"
            "value = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'src/product.py'))['VALUE']\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': 'ran'}],"
            "'measurements': [{'name': 'latency', 'value': 1 + value, 'unit': 'ms' if value else 's', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_id = _record(project)

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "INCONCLUSIVE"
    assert result["completed_pairs"] == 0
    assert "units" in result["detail"]


def test_workload_comparison_rejects_zero_baseline_for_relative_regression(make_project):
    project = make_project({
        "src/product.py": "VALUE = 0\n",
        "verification/driver.py": (
            "import json, pathlib, runpy, sys\n"
            "value = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'src/product.py'))['VALUE']\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': 'ran'}],"
            "'measurements': [{'name': 'latency', 'value': 0 if value == 0 else 3, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_id = _record(project)

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "INCONCLUSIVE"
    assert result["completed_pairs"] == 0
    assert "nonpositive baseline" in result["detail"]


def test_workload_comparison_refuses_nonfinite_regression_ratio(make_project):
    project = make_project({
        "src/product.py": "VALUE = 0\n",
        "verification/driver.py": (
            "import json, pathlib, runpy, sys\n"
            "value = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'src/product.py'))['VALUE']\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'schema_version': 1, 'scenarios': [{'id': 'workload-ran', 'result': 'PASS', 'observation': 'ran'}],"
            "'measurements': [{'name': 'latency', 'value': 1e-300 if value == 0 else 1e300, 'unit': 'ms', 'better': 'lower'}]}))\n"
        ),
    }, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_id = _record(project)

    result = compare_workload_cost(project, run_id, before_ref, pairs=10, timeout_seconds=30)

    assert result["status"] == "INCONCLUSIVE"
    assert result["completed_pairs"] == 0
    assert "non-finite paired regression" in result["detail"]

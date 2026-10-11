from __future__ import annotations

import hashlib
import json
import subprocess

from vkit.manifest import parse_manifest
from vkit.operations.retained_cases import replay_retained_cases
from vkit.paths import open_project
from vkit.runner import run_check
from helpers import git, vkit


def _check(*, timeout_seconds: float = 10, subject_paths: list[str] | None = None) -> dict:
    return {
        "id": "retained",
        "kind": "scenario",
        "timeout_seconds": timeout_seconds,
        "artifact": "result.json",
        "command": ["{{python}}", "verification/driver.py", "{{run_dir}}/result.json"],
        "required_scenarios": ["product-invariant"],
        "inputs": ["src/product.py", "verification/driver.py"],
        "subject": {"paths": subject_paths or ["src/product.py"], "digest": None},
        "claim_id": "retained",
    }


def _commit(project) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _record(project) -> str:
    opened = open_project(project)
    return run_check(opened, parse_manifest(opened), "retained")["run_id"]


def _driver(*, assertion: bool) -> str:
    check = ("try:\n    assert value == 0\n    passed = True\nexcept AssertionError:\n    passed = False"
             if assertion else "passed = True")
    return (
        "import json, pathlib, runpy, sys\n"
        "value = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'src/product.py'))['VALUE']\n"
        f"{check}\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'schema_version': 1, 'scenarios': ["
        "{'id': 'product-invariant', 'result': 'PASS' if passed else 'FAIL', "
        "'observation': f'value={value}'}]}))\n"
    )


def _conditional_driver(*, candidate: str) -> str:
    """Keep an assertion-bearing retained driver while making current checks pass."""
    return (
        "import json, pathlib, runpy, sys\n"
        "value = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'src/product.py'))['VALUE']\n"
        "if value == 0:\n"
        "    pathlib.Path(sys.argv[1]).write_text(json.dumps({'schema_version': 1, 'scenarios': ["
        "{'id': 'product-invariant', 'result': 'PASS', 'observation': 'value=0'}]}))\n"
        "else:\n"
        f"    {candidate}\n"
    )


def test_replay_uses_retained_old_assertion_against_current_subject(make_project) -> None:
    retained_driver = _driver(assertion=True)
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": retained_driver}, [_check()])
    before_ref = _commit(project)
    retained_product_bytes = (project / "src/product.py").read_bytes()
    retained_driver_bytes = (project / "verification/driver.py").read_bytes()
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    # The candidate's current check has been weakened, but the replay takes its driver from before_ref.
    (project / "verification/driver.py").write_text(_driver(assertion=False), encoding="utf-8")
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "REGRESSION"
    assert result["baseline"]["status"] == "PASS"
    assert result["candidate"]["status"] == "FAIL"
    assert result["case_results"] == [{
        "id": "product-invariant",
        "baseline": {"id": "product-invariant", "result": "PASS", "observation": "value=0"},
        "candidate": {"id": "product-invariant", "result": "FAIL", "observation": "value=1"},
    }]
    assert result["retained_inputs"] == [
        {"path": "src/product.py", "sha256": hashlib.sha256(retained_product_bytes).hexdigest(), "mode": 0o644},
        {"path": "verification/driver.py", "sha256": hashlib.sha256(retained_driver_bytes).hexdigest(), "mode": 0o644},
    ]


def test_replay_does_not_classify_candidate_when_retained_baseline_fails(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 1\n",
                            "verification/driver.py": _driver(assertion=True)}, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 0\n", encoding="utf-8")
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "BASELINE_FAILED"
    assert result["baseline"]["status"] == "FAIL"
    assert result["candidate"] is None
    assert result["case_results"] == [{
        "id": "product-invariant",
        "baseline": {"id": "product-invariant", "result": "FAIL", "observation": "value=1"},
        "candidate": None,
    }]


def test_replay_refuses_candidate_bytes_changed_after_recorded_run(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": _driver(assertion=True)}, [_check()])
    before_ref = _commit(project)
    run_id = _record(project)
    (project / "src/product.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "UNSUPPORTED"
    assert "input snapshot" in result["detail"]


def test_replay_accepts_matching_retained_and_candidate_cases(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": _driver(assertion=True)}, [_check()])
    before_ref = _commit(project)
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "REPLAYED_PASS"
    assert result["baseline"]["cases"] == [
        {"id": "product-invariant", "result": "PASS", "observation": "value=0"}]
    assert result["candidate"]["cases"] == result["baseline"]["cases"]
    assert result["case_results"] == [{
        "id": "product-invariant",
        "baseline": {"id": "product-invariant", "result": "PASS", "observation": "value=0"},
        "candidate": {"id": "product-invariant", "result": "PASS", "observation": "value=0"},
    }]


def test_replay_can_expose_candidate_failure_even_if_current_check_failed(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": _driver(assertion=True)}, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "REGRESSION"
    assert result["baseline"]["status"] == "PASS"
    assert result["candidate"]["status"] == "FAIL"


def test_replay_rejects_old_check_digest_mismatch(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": _driver(assertion=True)}, [_check()])
    before_ref = _commit(project)
    manifest_path = project / "verification" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checks"][0]["claim_id"] = "changed-retained-claim"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    code, body = vkit(project, "accept", "--yes")
    assert code == 0, body
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "UNSUPPORTED"
    assert "check definition differs" in result["detail"]


def test_replay_rejects_subject_that_overlaps_approved_harness(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": _driver(assertion=True)},
                           [_check(subject_paths=["src/product.py", "verification/driver.py"])])
    before_ref = _commit(project)
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "UNSUPPORTED"
    assert "subject overlaps" in result["detail"]


def test_replay_uses_full_sha_without_git_replace_refs(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": _driver(assertion=True)}, [_check()])
    retained_ref = _commit(project)
    retained_product = (project / "src/product.py").read_bytes()
    (project / "src/product.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(project, "add", "src/product.py")
    git(project, "commit", "-qm", "alternate retained content")
    replacement_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_id = _record(project)

    short = replay_retained_cases(project, run_id, retained_ref[:12])
    git(project, "replace", retained_ref, replacement_ref)
    result = replay_retained_cases(project, run_id, retained_ref)

    assert short["status"] == "UNSUPPORTED"
    assert "full immutable" in short["detail"]
    assert result["status"] == "REGRESSION"
    assert result["baseline"]["cases"][0]["observation"] == "value=0"
    product_capture = next(item for item in result["retained_inputs"] if item["path"] == "src/product.py")
    assert product_capture["sha256"] == hashlib.sha256(retained_product).hexdigest()


def test_replay_rejects_retained_commit_missing_declared_harness(make_project) -> None:
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": _driver(assertion=True)}, [_check()])
    git(project, "rm", "verification/driver.py")
    git(project, "commit", "-qm", "remove approved harness")
    before_ref = _commit(project)
    (project / "verification" / "driver.py").write_text(_driver(assertion=True), encoding="utf-8")
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "UNSUPPORTED"
    assert "exact check input paths" in result["detail"]


def test_replay_incomplete_retained_report_is_inconclusive(make_project) -> None:
    retained_driver = _conditional_driver(
        candidate="pathlib.Path(sys.argv[1]).write_text('{\\\"schema_version\\\":1,\\\"scenarios\\\":[]}')")
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": retained_driver}, [_check()])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "verification/driver.py").write_text(_driver(assertion=False), encoding="utf-8")
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "INCONCLUSIVE"
    assert result["baseline"]["status"] == "PASS"
    assert result["candidate"]["status"] == "INCONCLUSIVE"
    assert result["candidate"]["detail"] == "the artifact listed no scenarios"


def test_replay_timeout_is_inconclusive_not_a_regression(make_project) -> None:
    retained_driver = _conditional_driver(
        candidate="import time; time.sleep(3)\n    pathlib.Path(sys.argv[1]).write_text('{}')")
    project = make_project({"src/product.py": "VALUE = 0\n",
                            "verification/driver.py": retained_driver}, [_check(timeout_seconds=1)])
    before_ref = _commit(project)
    (project / "src/product.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "verification/driver.py").write_text(_driver(assertion=False), encoding="utf-8")
    run_id = _record(project)

    result = replay_retained_cases(project, run_id, before_ref)

    assert result["status"] == "INCONCLUSIVE"
    assert result["baseline"]["status"] == "PASS"
    assert result["candidate"]["status"] == "INCONCLUSIVE"
    assert result["candidate"]["detail"] == "scenario command timed out"

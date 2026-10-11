from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from vkit.manifest import parse_manifest
from vkit.operations.mutation import mutation_survivors
from vkit.paths import open_project
from vkit.runner import run_check
from vkit.store import Store

_ENV = {
    "helper": "VKIT_MUTATION_HELPER",
    "helper_sha": "VKIT_MUTATION_SHA256",
    "lockfile": "VKIT_MUTATION_LOCKFILE",
    "lock_sha": "VKIT_MUTATION_LOCK_SHA256",
    "node": "VKIT_NODE_BIN",
}


def _node_check(required: list[str], inputs: list[str], subjects: list[str] | None = None) -> dict:
    return {
        "id": "node-tests", "kind": "node_test", "timeout_seconds": 20, "artifact": "node.tap",
        "required_tests": required, "runner": {"executable": "node", "base_argv": ["--test"]},
        "report_format": "node_tap", "expect_report_version": 13, "inputs": inputs,
        "subject": {"paths": subjects or ["src/calc.js"], "digest": None}, "claim_id": "node-tests",
    }


def _project(make_project, *, source: str = "export function positive(x) { return x > 0; }\n",
             expected: str = "true", required: list[str] | None = None) -> Path:
    source = source if source != "export function positive(x) { return x > 0; }\n" \
        else "/* Stryker disable all */\nconst label = '😀'; export function positive(x) { return x > 0; }\n"
    required = required or ["test/calc.test.js::positive values are accepted",
                            "test/calc.test.js::negative values are rejected"]
    return make_project({
        "package.json": '{"type":"module"}\n',
        "src/calc.js": source,
        "test/calc.test.js": (
            "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
            "import { positive } from '../src/calc.js';\n"
            f"test('positive values are accepted', () => assert.equal(positive(1), {expected}));\n"
            "test('negative values are rejected', () => assert.equal(positive(-1), false));\n"
        ),
    }, [_node_check(required, ["package.json", "src/calc.js", "test/calc.test.js"])])


@pytest.fixture
def configured_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {key: os.environ.get(name) for key, name in _ENV.items()}
    if not any(values.values()):
        pytest.skip("the pinned mutation runtime is not configured")
    if not all(values.values()):
        pytest.fail("the mutation runtime requires helper, lockfile, SHA-256 values, and Node paths")
    helper = Path(values["helper"]).resolve()
    lockfile = Path(values["lockfile"]).resolve()
    if not helper.is_file() or hashlib.sha256(helper.read_bytes()).hexdigest() != values["helper_sha"].lower():
        pytest.fail("the configured mutation helper does not match its SHA-256")
    if not lockfile.is_file() or hashlib.sha256(lockfile.read_bytes()).hexdigest() != values["lock_sha"].lower():
        pytest.fail("the configured mutation lockfile does not match its SHA-256")
    if not Path(values["node"]).is_file():
        pytest.fail("the configured Node executable does not exist")
    for key, value in values.items():
        monkeypatch.setenv(_ENV[key], str(helper if key == "helper" else lockfile if key == "lockfile" else value))


def _record(project: Path) -> tuple[Path, str]:
    opened = open_project(project)
    record = run_check(opened, parse_manifest(opened), "node-tests")
    return opened.root, record["run_id"]


def test_mutation_trials_classify_assertion_failures_and_replay_survivors(
    make_project, configured_mutation,
):
    project = _project(make_project)
    root, run_id = _record(project)
    before = (root / "src/calc.js").read_bytes()

    result = mutation_survivors(root, run_id, "src/calc.js", max_mutants=20, timeout_seconds=60)

    assert result["status"] == "ANALYZED"
    assert result["baseline"]["status"] == "PASS"
    assert result["counts"]["generated"] >= 2
    assert result["counts"]["tested"] >= 2
    assert result["counts"]["unexamined"] == 0
    assert any(item["status"] == "DETECTED" for item in result["mutants"])
    survivors = [item for item in result["mutants"] if item["status"] == "SURVIVED"]
    assert survivors
    assert all(item["replay"]["status"] == "PASS" for item in survivors)
    assert all(item["edit"]["before"] != item["edit"]["replacement"] for item in result["mutants"])
    assert any(
        [case["id"] for case in item["trial"]["cases"]] == [
            "test/calc.test.js::positive values are accepted",
            "test/calc.test.js::negative values are rejected",
        ] and [case["result"] for case in item["trial"]["cases"]] == ["FAIL", "PASS"]
        for item in result["mutants"] if item["status"] == "DETECTED"
    )
    assert (root / "src/calc.js").read_bytes() == before


def test_mutation_reports_unexamined_edits_when_limit_truncates(make_project, configured_mutation):
    project = _project(make_project)
    root, run_id = _record(project)

    result = mutation_survivors(root, run_id, "src/calc.js", max_mutants=1, timeout_seconds=60)

    assert result["status"] == "UNKNOWN"
    assert result["counts"]["generated"] > result["counts"]["tested"]
    assert result["counts"]["unexamined"] == result["counts"]["generated"] - 1


def test_mutation_refuses_a_recorded_baseline_failure(make_project, configured_mutation):
    project = _project(make_project, expected="false")
    root, run_id = _record(project)
    assert Store.of(open_project(root)).record(run_id)["outcome"]["result"] == "FAIL"

    result = mutation_survivors(root, run_id, "src/calc.js", timeout_seconds=30)

    assert result["status"] == "UNSUPPORTED"
    assert "PASS" in result["detail"]


def test_mutation_deadline_returns_unknown(make_project, configured_mutation):
    project = make_project({
        "package.json": '{"type":"module"}\n',
        "src/calc.js": "export function positive(x) { return x > 0; }\n",
        "test/calc.test.js": (
            "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
            "import { positive } from '../src/calc.js';\n"
            "test('positive values are accepted', async () => {\n"
            "  await new Promise(resolve => setTimeout(resolve, 250));\n"
            "  assert.equal(positive(1), true);\n});\n"
        ),
    }, [_node_check(["test/calc.test.js::positive values are accepted"],
                    ["package.json", "src/calc.js", "test/calc.test.js"])])
    root, run_id = _record(project)

    from vkit.operations import mutation

    request = mutation._prepare(root, run_id, "src/calc.js")
    with pytest.raises(TimeoutError, match="total time budget"):
        mutation._assert_fresh(request, time.monotonic() - 1)

    result = mutation_survivors(root, run_id, "src/calc.js", timeout_seconds=1)

    assert result["status"] == "UNKNOWN"
    assert result["detail"]
    assert result["counts"]["generated"] == len(result["mutants"]) + result["counts"]["unexamined"]


def test_mutation_refuses_changed_recorded_source_before_execution(make_project, configured_mutation):
    project = _project(make_project)
    root, run_id = _record(project)
    (root / "src/calc.js").write_text("export function positive(x) { return x >= 0; }\n", encoding="utf-8")

    result = mutation_survivors(root, run_id, "src/calc.js")

    assert result["status"] == "UNSUPPORTED"
    assert result["baseline"] is None
    assert "inputs changed" in result["detail"]


def test_mutation_refuses_a_recorded_report_without_cases(make_project, configured_mutation):
    project = _project(make_project)
    root, run_id = _record(project)
    check = parse_manifest(open_project(root)).require("node-tests")
    Store.of(open_project(root)).run_dir(run_id).joinpath(check.artifact_name).write_bytes(
        b"TAP version 13\n1..0\n"
    )

    result = mutation_survivors(root, run_id, "src/calc.js")

    assert result["status"] == "UNSUPPORTED"
    assert "required cases passed" in result["detail"]


def test_mutation_does_not_count_runtime_errors_as_detected(make_project, configured_mutation):
    project = make_project({
        "package.json": '{"type":"module"}\n',
        "src/calc.js": "export function positive(x) { return {value: x > 0}; }\n",
        "test/calc.test.js": (
            "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
            "import { positive } from '../src/calc.js';\n"
            "test('positive values are accepted', () => assert.equal(positive(1).value.toString(), 'true'));\n"
        ),
    }, [_node_check(["test/calc.test.js::positive values are accepted"],
                    ["package.json", "src/calc.js", "test/calc.test.js"])])
    root, run_id = _record(project)

    result = mutation_survivors(root, run_id, "src/calc.js")

    assert result["baseline"]["status"] == "PASS"
    runtime_errors = [item for item in result["mutants"]
                      if item["trial"] and "errored before asserting" in item["trial"].get("detail", "")]
    assert runtime_errors
    assert all(item["status"] == "UNKNOWN" for item in runtime_errors)


def test_mutation_refuses_missing_required_case_and_path_escape(make_project, configured_mutation):
    project = _project(make_project)
    root, run_id = _record(project)
    manifest = root / "verification/manifest.json"
    manifest_bytes = manifest.read_bytes()
    raw = json.loads(manifest_bytes)
    raw["checks"][0]["required_tests"] = ["test/other.test.js::one case"]
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    changed_check = mutation_survivors(root, run_id, "src/calc.js")
    manifest.write_bytes(manifest_bytes)
    escaped = mutation_survivors(root, run_id, "../outside.js")

    assert changed_check["status"] == "UNSUPPORTED"
    assert "definition changed" in changed_check["detail"]
    assert escaped["status"] == "UNSUPPORTED"

from __future__ import annotations

import json
import re
import shutil
import subprocess
from types import SimpleNamespace

from helpers import McpClient, vkit
from vkit.evidence import validate_record
from vkit.outcome import Blocked, BlockedReason, Passed
from vkit.query import open_context
from vkit.runner import _external_logical_trust


def _lean_check():
    lean = shutil.which("lean")
    assert lean, "the real Lean executable is required for this gate test"
    version_text = subprocess.run([lean, "--version"], check=True, capture_output=True, text=True).stdout
    version = re.search(r"\bversion\s+([0-9]+\.[0-9]+\.[0-9]+)\b", version_text)
    assert version, version_text
    return {
        "id": "lean",
        "kind": "lean",
        "timeout_seconds": 120,
        "artifact": "lean-report.json",
        "challenge": {"module": "Challenge", "path": "Challenge.lean"},
        "theorems": ["identity"],
        "profile": "reviewed_proof_sources",
        "permitted_axioms": [],
        "toolchain": {"tool": "lean", "version": version.group(1)},
        "inputs": ["Challenge.lean"],
        "subject": {"paths": ["Challenge.lean"], "digest": None},
        "claim_id": "lean",
    }


def _run_lean(make_project, source: str):
    project = make_project({"Challenge.lean": source}, [_lean_check()])
    client = McpClient(project)
    try:
        started, error = client.call("check_run", check_ids=["lean"], wait_seconds=60)
        assert not error, started
        run_id = started["runs"][0]["run_id"]
        view, error = client.call(
            "run_get", run_id=run_id,
            sections=["provenance", "obligations", "diagnostics", "artifacts"],
        )
        assert not error, view
        return project, run_id, view
    finally:
        client.close()


def test_real_lean_trust_is_sealed_and_exposed_through_mcp(make_project):
    project, run_id, view = _run_lean(
        make_project, "theorem identity (n : Nat) : n = n := by rfl\n",
    )

    assert view["summary"]["outcome"]["result"] == "PASS"
    assert view["summary"]["claim_outcome"] == "established"
    trust = view["provenance"]["logical_trust"]
    assert trust["engine"]["name"] == "lean"
    assert trust["axioms"] == {
        "status": "reported",
        "by_obligation": [{"obligation": "Challenge.identity", "status": "reported", "items": []}],
    }
    assert trust["dependencies"] == {"status": "not_reported", "items": []}
    assert trust["independent_check"]["status"] == "not_provided"
    assert view["obligations"]["satisfied"][0]["axioms"] == []

    context = open_context(project)
    record = context.store.record(run_id)
    run_dir = context.store.run_dir(run_id)
    assert validate_record(record, run_dir)
    receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["verification"] == record["verification"]

    record["verification"]["logical_trust"]["scope"]["items"][0]["theorems"] = ["forged"]
    assert not validate_record(record, run_dir)
    record["verification"]["logical_trust"]["scope"]["items"][0]["theorems"] = ["identity"]
    artifact = run_dir / "lean-report.json"
    artifact.write_bytes(artifact.read_bytes() + b" ")
    assert not validate_record(record, run_dir)


def test_real_lean_axiom_rejection_is_unknown_and_keeps_reported_trust(make_project):
    project = make_project({
        "Challenge.lean": "axiom untrusted : True\ntheorem identity : True := untrusted\n",
    }, [_lean_check()])
    code, result = vkit(project, "check", "run", "--check", "lean")

    assert code == 3
    run = result["runs"][0]
    assert run["outcome"]["result"] == "BLOCKED"
    assert run["outcome"]["reason"] == "formal_obligation"
    assert run["claim_outcome"] == "unknown"
    trust = run["verification"]["logical_trust"]
    assert trust["axioms"]["by_obligation"] == [
        {"obligation": "Challenge.identity", "status": "reported", "items": ["untrusted"]}
    ]


def test_real_lean_source_diagnostic_is_bound_to_captured_input(make_project):
    project, run_id, view = _run_lean(
        make_project, "theorem identity : True := by\n  exact definitely_missing\n",
    )

    assert view["summary"]["outcome"]["result"] == "BLOCKED"
    assert view["summary"]["claim_outcome"] == "unknown"
    rows = view["diagnostics"]["diagnostics"]
    diagnostic = next(item for item in rows if item["kind"] == "lean_message")
    assert diagnostic["location"]["path"] == "Challenge.lean"
    assert diagnostic["location"]["source_sha256"]
    assert diagnostic["location"]["start"]["line"] == 2
    context = open_context(project)
    record = context.store.record(run_id)
    assert record["obligations"] is None
    captured = next(item for item in record["inputs"]["files"] if item["path"] == "Challenge.lean")
    assert diagnostic["location"]["source_sha256"] == captured["sha256"]


def test_external_checker_is_reported_as_independent_only_after_acceptance():
    check = SimpleNamespace(
        model_scope="finite state model",
        assumptions=("declared constant bounds",),
        obligations=("safe",),
    )
    metadata = {
        "identity": {
            "backend": {"executable": {"path": "producer", "sha256": "a" * 64}},
            "checker": {"executable": {"path": "checker", "sha256": "b" * 64}},
        },
        "checker_receipt": {"document": {"verdict": "accepted"}},
    }

    accepted = _external_logical_trust(check, metadata, Passed(()))
    rejected = _external_logical_trust(
        check, metadata | {"checker_receipt": {"document": {"verdict": "rejected"}}},
        Blocked(BlockedReason.ARTIFACT_MALFORMED),
    )

    assert accepted["independent_check"]["status"] == "checked"
    assert accepted["independent_check"]["checker"]["digest"] == "b" * 64
    assert rejected["independent_check"]["status"] == "rejected"

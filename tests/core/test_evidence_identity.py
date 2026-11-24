from __future__ import annotations

from pathlib import Path

from helpers import scenario_check
from vkit.core.evidence import backend_identity
from vkit.core.job import job_id
from vkit.core.manifest import parse_check_definition
from vkit.runtime.paths import Project


def _check(tmp_path: Path, executable: str):
    entry = scenario_check("c1", "driver.py", scenarios=["s"], inputs=["a.py"])
    entry["prerequisites"] = [{"name": "tool", "executable": executable, "args": []}]
    return parse_check_definition(entry, Project(tmp_path, tmp_path / ".git"))


def _job_digest(backend: dict) -> str:
    return job_id({"backend": backend, "fixed": 1})


def test_tool_identity_ignores_path_spelling(tmp_path):
    tool = tmp_path / "bin" / "tool.exe"
    tool.parent.mkdir()
    tool.write_bytes(b"tool-bytes")
    spelled = str(tool.parent / ".." / tool.parent.name / tool.name)
    first, second = _check(tmp_path, str(tool)), _check(tmp_path, spelled)
    assert backend_identity(first) == backend_identity(second)
    assert _job_digest(backend_identity(first)) == _job_digest(backend_identity(second))


def test_tool_identity_still_separates_different_bytes(tmp_path):
    a, b = tmp_path / "a.exe", tmp_path / "b.exe"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    assert backend_identity(_check(tmp_path, str(a))) != backend_identity(_check(tmp_path, str(b)))


def test_validate_record_rejects_v1_change_certification_without_crashing(tmp_path):
    import json
    from vkit.core.evidence import sha256, validate_record

    receipt = {
        "schema_version": 1,
        "job_id": "a" * 64,
        "check_digest": "b" * 64,
        "backend": {},
        "inputs": {"files": []},
        "outcome": {"result": "PASS"},
        "obligations": None,
        "artifacts": {},
        "assurance": "empirical",
        "claim_outcome": "unknown",
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    record = {
        **receipt,
        "key": "a" * 64,
        "subject": {"kind": "change_certification", "id": "chg"},
        "receipt": {
            "path": "receipt.json",
            "sha256": sha256(receipt_path),
            "assurance": "empirical",
            "claim_outcome": "unknown",
        },
    }
    assert validate_record(record, tmp_path) is False


def test_write_json_cleans_up_temp_file_on_replace_failure(tmp_path, monkeypatch):
    import os
    import pytest
    from vkit.core.store import write_json

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_json(tmp_path / "out.json", {"ok": True})
    assert list(tmp_path.iterdir()) == []


def test_store_runs_ignores_non_run_id_entries_and_latest_rejects_traversal(tmp_path):
    from vkit.core.store import Store

    store = Store(tmp_path / "vkit")
    (store.root / "runs").mkdir(parents=True)
    (store.root / "runs" / ".DS_Store").write_text("x", encoding="utf-8")
    (store.root / "runs" / "bad.dir").mkdir()
    assert store.runs() == []
    assert store.latest("../escape") is None


def test_pinned_intact_returns_false_if_file_vanishes_after_stat(tmp_path, monkeypatch):
    from vkit.core import runner

    target = tmp_path / "eval.py"
    target.write_bytes(b"print(1)\n")
    info = target.stat()
    pin = {"path": str(target), "inode": [info.st_dev, info.st_ino], "sha256": "0" * 64}

    def vanish(_path):
        raise FileNotFoundError("vanished")

    monkeypatch.setattr(runner, "sha256", vanish)
    assert runner._pinned_intact(pin) is False


def test_execute_unknown_run_raises_value_error(tmp_path):
    import pytest
    from vkit.core.runner import execute
    from vkit.core.store import Store

    store = Store(tmp_path / "vkit")
    with pytest.raises(ValueError, match="unknown run"):
        execute(None, "missing-run", store=store)

from __future__ import annotations

from pathlib import Path

from helpers import scenario_check
from vkit.evidence import backend_identity
from vkit.job import job_id
from vkit.manifest import parse_check_definition
from vkit.paths import Project


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

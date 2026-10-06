from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from helpers import git, vkit


@pytest.fixture
def make_project(tmp_path: Path):
    def build(files: dict[str, str], checks: list[dict[str, Any]], *, accept: bool = True) -> Path:
        project = tmp_path / "project"
        for relative, text in files.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text), encoding="utf-8")
        manifest = {"schema_version": 2, "description": "test project", "checks": checks}
        (project / "verification").mkdir(parents=True, exist_ok=True)
        (project / "verification" / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        git(project, "init", "-q")
        git(project, "add", ".")
        git(project, "commit", "-qm", "init")
        if accept:
            code, body = vkit(project, "accept", "--yes")
            assert code == 0, body
        return project
    return build

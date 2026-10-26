"""Shared formal submission boundary for CLI and MCP."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from .. import jobs
from ..nowindow import hidden_window
from ..paths import Project
from ..query import Context
from .policy import VerificationRequest
from .verus import resolve_execution


def resolve_commit(project: Project, revision: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"],
        cwd=project.root, capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}, **hidden_window(),
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "candidate must resolve to a Git commit")
    return result.stdout.strip()


def verify(ctx: Context, raw: Any) -> dict[str, Any]:
    request = VerificationRequest.from_json(raw)
    component = ctx.require_manifest().require_component(request.component)
    identity = resolve_execution(component)
    record = jobs.submit_formal(ctx.project, request, component, identity, store=ctx.store)
    if request.wait_seconds:
        record = jobs.wait(ctx.store, record["run_id"], request.wait_seconds)
    return record

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
    approval = ctx.store.approved().get(component.digest, {})
    if approval.get("kind") != "formal_policy":
        raise ValueError("formal policy is not accepted; run `vkit accept --component " + component.id + "`")
    if request.reuse:
        from ..inputs import canonical_digest
        from .verus import resolve_execution

        current = resolve_execution(component)
        if not component.is_accepted(approval, canonical_digest(current["semantic"])):
            raise ValueError("formal policy, package or checker changed; accept them before reuse")
    record = jobs.submit_formal(ctx.project, request, component, approval["checker_sha256"], store=ctx.store)
    if request.wait_seconds:
        record = jobs.wait(ctx.store, record["run_id"], request.wait_seconds)
    return record

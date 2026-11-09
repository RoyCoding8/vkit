"""Claude Code hooks for vkit: report the gate at session start, and hold a stop while evidence is stale.

Usage: vkit_hook.py <SessionStart|Stop|SubagentStop> --project <root>, with the hook payload on stdin.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any

FIXABLE = {"stale", "missing", "fresh_fail"}
BLOCKING = FIXABLE | {"not_approved", "obligation_removed"}


def _required(status: dict[str, Any]) -> list[dict[str, Any]]:
    return status["checks"] + [item for item in status["formal"] if item["required"]]


def _summary(status: dict[str, Any]) -> str:
    gate = status["gate"]
    lines = [f"vkit gate: {gate['verdict']} - {gate['reason']}"]
    kinds = status.get("coverage_kinds") or {}
    parts = [f"{kind.replace('_', ' ')}: {', '.join(paths)}" for kind, paths in sorted(kinds.items()) if paths]
    if parts:
        lines.append("coverage: " + "; ".join(parts))
    lines += [f"  {c['state']}: {c['id']}" for c in _required(status) if c["state"] != "fresh_pass"]
    if status["needs_run"]:
        lines.append("Run the stale checks with the vkit check_run tool (needed=true), then read the gate.")
    for item in status["formal"]:
        if item["required"] and item["state"] in FIXABLE:
            lines.append(f"Run vkit verify {item['id']} --working-tree, or the MCP verify tool, then inspect the run and gate.")
    return "\n".join(lines)


def _git(project: str, *args: str) -> bytes:
    from vkit.paths import open_project

    done = subprocess.run(["git", "-C", str(open_project(project).root), *args], capture_output=True, timeout=30)
    if done.returncode != 0:
        raise OSError(done.stderr.decode("utf-8", "replace").strip())
    return done.stdout


def _telemetry(project: str, event: str) -> None:
    try:
        from vkit.paths import open_project

        path = open_project(project).git_common_dir / "vkit" / "telemetry.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"event": event, "project": project, "ts": time.time()}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _record_bypass(project: str) -> None:
    _telemetry(project, "stop_hook_bypass")


def _record_base(project: str) -> None:
    try:
        from vkit.paths import open_project

        top = _git(project, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
        head = _git(project, "rev-parse", "HEAD").decode("utf-8", "surrogateescape").strip()
        (open_project(project).git_common_dir / "vkit" / "session_base.txt").write_text(
            f"{top}\n{head}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _changed_paths(project: str) -> list[str]:
    from vkit.paths import open_project

    base_file = open_project(project).git_common_dir / "vkit" / "session_base.txt"
    if not base_file.is_file():
        _telemetry(project, "session_base_missing")
        return []
    try:
        top, base = base_file.read_text(encoding="utf-8").splitlines()[:2]
        prefix = _git(project, "rev-parse", "--show-prefix").decode("utf-8", "surrogateescape").strip()
        raw = _git(project, "diff", "--name-only", "-z", base) + _git(
            project, "ls-files", "--others", "--exclude-standard", "-z")
        names = {n.decode("utf-8", "surrogateescape") for n in raw.split(b"\0") if n}
        return sorted(n[len(prefix):] for n in names if n.startswith(prefix))
    except Exception:  # noqa: BLE001
        _telemetry(project, "session_base_error")
        return []


def _fail_closed(event: str, exc: Exception) -> dict[str, Any]:
    detail = f"vkit hook could not read the gate: {type(exc).__name__}: {exc}"
    return {"systemMessage": detail} if event == "SessionStart" else {"decision": "block", "reason": detail}


def handle(event: str, payload: dict[str, Any], project: str) -> dict[str, Any]:
    try:
        return _handle(event, payload, project)
    except Exception as exc:  # noqa: BLE001
        return _fail_closed(event, exc)


def _handle(event: str, payload: dict[str, Any], project: str) -> dict[str, Any]:
    from vkit import query

    if event == "SessionStart":
        _record_base(project)
        status = query.status(query.open_context(project))
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": _summary(status)}}
    paths = _changed_paths(project)
    status = query.status(query.open_context(project), paths, session=bool(paths))
    blocking = status["gate"]["verdict"] == "REJECTED" or any(c["state"] in BLOCKING for c in _required(status))
    if status["gate"]["verdict"] == "READY" or payload.get("stop_hook_active") or not blocking:
        if payload.get("stop_hook_active") and blocking and status["gate"]["verdict"] != "READY":
            _record_bypass(project)
        return {} if status["gate"]["verdict"] == "READY" else {"systemMessage": _summary(status)}
    return {"decision": "block", "reason": _summary(status)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event", choices=("SessionStart", "Stop", "SubagentStop"))
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        response = handle(args.event, payload, args.project)
    except Exception as exc:  # noqa: BLE001
        response = _fail_closed(args.event, exc)
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())

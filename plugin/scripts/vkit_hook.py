"""Claude Code hooks for vkit: report the gate at session start, and hold a stop while evidence is stale.

Usage: vkit_hook.py <SessionStart|Stop|SubagentStop> --project <root>, with the hook payload on stdin.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

FIXABLE = {"stale", "missing", "fresh_fail"}


def _required(status: dict[str, Any]) -> list[dict[str, Any]]:
    return status["checks"] + [item for item in status["formal"] if item["required"]]


def _summary(status: dict[str, Any]) -> str:
    gate = status["gate"]
    lines = [f"vkit gate: {gate['verdict']} - {gate['reason']}"]
    lines += [f"  {c['state']}: {c['id']}" for c in _required(status) if c["state"] != "fresh_pass"]
    if status["needs_run"]:
        lines.append("Run the stale checks with the vkit check_run tool (needed=true), then read the gate.")
    for item in status["formal"]:
        if item["required"] and item["state"] in FIXABLE:
            lines.append(f"Run vkit verify {item['id']} --working-tree, or the MCP verify tool, then inspect the run and gate.")
    return "\n".join(lines)


def handle(event: str, payload: dict[str, Any], project: str) -> dict[str, Any]:
    from vkit import query

    status = query.status(query.open_context(project))
    if event == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": _summary(status)}}
    fixable = any(c["state"] in FIXABLE for c in _required(status))
    if status["gate"]["verdict"] == "READY" or payload.get("stop_hook_active") or not fixable:
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
        response = {"systemMessage": f"vkit hook could not read the gate: {type(exc).__name__}: {exc}"}
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""vkit: run the checks a repository registered and report what the evidence establishes.

Exit codes: 0 ok/PASS/READY, 1 FAIL/REJECTED, 2 invalid request, 3 BLOCKED, 4 internal error, 5 unavailable.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import query
from .manifest import ManifestError
from .outcome import BlockedReason
from .paths import ProjectError
from .store import now

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INTERNAL = 4
EXIT_UNAVAILABLE = 5

_RESULT_EXIT = {"PASS": EXIT_OK, "FAIL": EXIT_FAILED, "BLOCKED": EXIT_BLOCKED}
_VERDICT_EXIT = {"READY": EXIT_OK, "REJECTED": EXIT_FAILED, "BLOCKED": EXIT_BLOCKED}


class Refused(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _emit(payload: dict[str, Any], as_json: bool, human: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else human)


def _context(args: argparse.Namespace) -> query.Context:
    try:
        return query.open_context(args.project)
    except ProjectError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc


def _outcome_line(record: dict[str, Any]) -> str:
    outcome = record.get("outcome") or {}
    line = f"{record['check_id']}: {outcome.get('result', record['state'].upper())}"
    if outcome.get("reason"):
        line += f" ({outcome['reason']}) {outcome.get('detail', '')}".rstrip()
    for scenario in outcome.get("scenarios", []):
        line += f"\n  [{scenario['result']}] {scenario['id']}: {scenario['observation']}"
    return line + f"\n  run {record['run_id']}"


def cmd_doctor(args: argparse.Namespace) -> int:
    report = query.doctor(_context(args))
    lines = [f"project : {report['project']}", f"state   : {report['state_root']}"]
    for f in report["findings"]:
        lines.append(f"  [{'ok  ' if f['ok'] else 'FAIL'}] {f.get('prerequisite', f['check'])}: {f['detail']}")
    lines.append("ready" if report["ok"] else "not ready")
    _emit(report, args.json, "\n".join(lines))
    return EXIT_OK if report["ok"] else EXIT_BLOCKED


def cmd_status(args: argparse.Namespace) -> int:
    report = query.status(_context(args), args.path or ())
    lines = [f"gate: {report['gate']['verdict']} - {report['gate']['reason']}"]
    for check in report["checks"]:
        lines.append(f"  {check['state']:<14} {check['id']}  [{check['category']}] {check['inputs']['scope']}")
    for path in report["unmapped_paths"]:
        lines.append(f"  no check reads {path}")
    if report["needs_run"]:
        lines.append("run: vkit check run --needed")
    _emit(report, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    report = query.status(_context(args))["gate"]
    lines = [f"{report['verdict']}: {report['reason']}"]
    lines += [f"  {c['state']:<14} {c['id']}" for c in report["checks"]]
    _emit(report, args.json, "\n".join(lines))
    return _VERDICT_EXIT[report["verdict"]]


def cmd_check_run(args: argparse.Namespace) -> int:
    from .runner import run_check

    ctx = _context(args)
    try:
        manifest = ctx.require_manifest()
        if args.needed:
            check_ids = query.status(ctx)["needs_run"]
        else:
            check_ids = args.check or []
            for check_id in check_ids:
                manifest.require(check_id)
    except ManifestError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    if not check_ids and not args.needed:
        raise Refused("name a check with --check, or pass --needed", EXIT_INVALID)
    records = [run_check(ctx.project, manifest, check_id, store=ctx.store) for check_id in check_ids]
    results = {r["outcome"]["result"] for r in records}
    _emit({"command": "check run", "runs": records}, args.json,
          "\n".join(_outcome_line(r) for r in records) or "every check is fresh; nothing to run")
    for result in ("FAIL", "BLOCKED"):
        if result in results:
            return _RESULT_EXIT[result]
    return EXIT_OK


def cmd_run_show(args: argparse.Namespace) -> int:
    view = query.run_view(_context(args), args.run, log=args.log, offset=args.offset, limit=args.limit)
    if view is None:
        raise Refused(f"no run {args.run!r}", EXIT_INVALID)
    _emit(view, args.json, _outcome_line(view) + "\n\n" + view["log"]["text"])
    if view["state"] == "done":
        return _RESULT_EXIT[view["outcome"]["result"]]
    return EXIT_BLOCKED if view["state"] == "interrupted" else EXIT_OK


def cmd_run_cancel(args: argparse.Namespace) -> int:
    ctx = _context(args)
    record = ctx.store.record(args.run)
    if record is None:
        raise Refused(f"no run {args.run!r}", EXIT_INVALID)
    if record["state"] != "running":
        _emit({"run_id": args.run, "cancelled": False, "state": record["state"]}, args.json,
              f"run {args.run} is {record['state']}; nothing to cancel")
        return EXIT_OK
    ctx.store.request_cancel(args.run)
    _emit({"run_id": args.run, "cancelled": True}, args.json, f"asked run {args.run} to stop")
    return EXIT_OK


def cmd_accept(args: argparse.Namespace) -> int:
    ctx = _context(args)
    try:
        manifest = ctx.require_manifest()
        wanted = args.check or sorted(manifest.checks)
        for check_id in wanted:
            manifest.require(check_id)
    except ManifestError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    approved = ctx.store.approved()
    pending = [c for c in wanted if manifest.digest(c) not in approved]
    lines = []
    for check_id in pending:
        lines.append(f"{check_id} ({manifest.digest(check_id)[:12]})")
        lines.append(json.dumps(manifest.entries[check_id], indent=2))
    if not pending:
        _emit({"accepted": [], "pending": []}, args.json, "every named check is already accepted")
        return EXIT_OK
    if not args.yes:
        if args.json or not sys.stdin.isatty():
            raise Refused("review the definitions and pass --yes to accept them:\n" + "\n".join(lines), EXIT_INVALID)
        print("\n".join(lines))
        if input(f"accept {len(pending)} check definition(s)? [y/N] ").strip().lower() != "y":
            print("nothing accepted")
            return EXIT_BLOCKED
    ctx.store.approve({manifest.digest(c): {"check_id": c, "accepted_at": now()} for c in pending})
    _emit({"accepted": pending}, args.json, f"accepted: {', '.join(pending)}")
    return EXIT_OK


def cmd_features(args: argparse.Namespace) -> int:
    report = query.status(_context(args))
    if report["feature_error"]:
        raise Refused(report["feature_error"], EXIT_INVALID)
    lines = []
    for feature in report["features"]:
        lines.append(f"[{'verified ' if feature['verified'] else 'UNVERIFIED'}] {feature['id']}: {feature['behavior']}")
        for gap in feature["coverage_gaps"]:
            lines.append(f"    gap: {gap}")
    _emit({"features": report["features"]}, args.json, "\n".join(lines) or "no feature map")
    return EXIT_OK if report["features"] and all(f["verified"] for f in report["features"]) else EXIT_BLOCKED


def cmd_project_inspect(args: argparse.Namespace) -> int:
    from .discover import inspect_repository

    inspection = inspect_repository(_context(args).project)
    found = bool(inspection.by_kind("test"))
    _emit(inspection.to_json(), args.json, inspection.render())
    return EXIT_OK if found else EXIT_BLOCKED


def cmd_project_enroll(args: argparse.Namespace) -> int:
    from .discover import inspect_repository
    from .enroll import EnrollmentError, enroll

    project = _context(args).project
    try:
        proposal, path = enroll(project, inspection=inspect_repository(project))
    except EnrollmentError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    _emit({**proposal.to_json(), "path": str(path)}, args.json, proposal.render())
    return EXIT_OK if proposal.entries else EXIT_BLOCKED


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from .mcp import MCPUnavailable, serve_stdio, tool_definitions

    project = _context(args).project
    if args.json:
        catalogue = tool_definitions()
        _emit({"project": str(project.root), "tools": catalogue}, True, "")
        return EXIT_OK
    try:
        return serve_stdio(project.root)
    except MCPUnavailable as exc:
        raise Refused(str(exc), EXIT_UNAVAILABLE) from exc


def cmd_console(args: argparse.Namespace) -> int:
    from .console import serve

    return serve(_context(args).project, port=args.port, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vkit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(container, name: str, help_text: str) -> argparse.ArgumentParser:
        target = container.add_parser(name, help=help_text)
        target.add_argument("--project", default=".", help="repository root, or a directory inside it")
        target.add_argument("--json", action="store_true", help="write one JSON object to stdout")
        return target

    command(sub, "doctor", "report whether this project's checks could run")
    status = command(sub, "status", "show every check's freshness and the gate")
    status.add_argument("--path", action="append", help="only checks that read this path (repeatable)")
    command(sub, "gate", "READY only if every check passed against the current inputs")
    command(sub, "features", "show the feature map and which features are verified now")
    accept = command(sub, "accept", "review and accept check definitions so they may run")
    accept.add_argument("--check", action="append", help="a check id (repeatable); default every check")
    accept.add_argument("--yes", action="store_true", help="accept without the interactive prompt")

    check = sub.add_parser("check", help="run registered checks").add_subparsers(dest="check_command", required=True)
    run_check = command(check, "run", "run checks in the foreground")
    run_check.add_argument("--check", action="append", help="a registered check id (repeatable)")
    run_check.add_argument("--needed", action="store_true", help="run every check that is stale or missing")

    run = sub.add_parser("run", help="read or stop a run").add_subparsers(dest="run_command", required=True)
    show = command(run, "show", "show a run, its outcome and a window of its log")
    show.add_argument("--run", required=True)
    show.add_argument("--log", choices=("stdout", "stderr"), default="stdout")
    show.add_argument("--offset", type=int, default=0)
    show.add_argument("--limit", type=int, default=query.DEFAULT_LOG_LIMIT)
    cancel = command(run, "cancel", "stop a running check and everything it started")
    cancel.add_argument("--run", required=True)

    project = sub.add_parser("project", help="onboard a repository").add_subparsers(
        dest="project_command", required=True)
    command(project, "inspect", "report what this repository declares about building and testing")
    command(project, "enroll", "propose a manifest; nothing runs until accepted")

    mcp = sub.add_parser("mcp", help="serve an agent over MCP").add_subparsers(dest="mcp_command", required=True)
    command(mcp, "serve", "serve stdio for one project root; --json prints the tool catalogue")

    console = command(sub, "console", "serve a read-only console on 127.0.0.1")
    console.add_argument("--port", type=int, default=8765, help="0 lets the OS pick a free port")
    console.add_argument("--no-browser", action="store_true")
    return parser


_DISPATCH = {
    ("doctor", None): cmd_doctor, ("status", None): cmd_status, ("gate", None): cmd_gate,
    ("features", None): cmd_features, ("accept", None): cmd_accept, ("check", "run"): cmd_check_run,
    ("run", "show"): cmd_run_show, ("run", "cancel"): cmd_run_cancel,
    ("project", "inspect"): cmd_project_inspect, ("project", "enroll"): cmd_project_enroll,
    ("mcp", "serve"): cmd_mcp_serve, ("console", None): cmd_console,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = _DISPATCH[(args.command, getattr(args, f"{args.command}_command", None))]
    try:
        return handler(args)
    except Refused as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "exit_code": exc.code}))
        else:
            print(str(exc), file=sys.stderr)
        return exc.code
    except Exception as exc:  # noqa: BLE001
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())

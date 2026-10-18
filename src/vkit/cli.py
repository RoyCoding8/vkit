"""vkit: run the checks a repository registered and report what the evidence establishes.

Exit codes: 0 ok/PASS/READY, 1 FAIL/REJECTED, 2 invalid request, 3 BLOCKED, 4 internal error, 5 unavailable.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from typing import Any, Sequence

from . import query
from .manifest import ManifestError
from .operations.catalog import OPERATIONS
from .operations.contract import (Operation, OperationContractError, OperationInputError, cli_arguments, cli_value,
                                  invoke)
from .paths import ProjectError
from .settings import load_launcher_settings, load_settings, save_settings
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


def _confirm(args: argparse.Namespace, description: str, question: str) -> bool:
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        raise Refused("review this and pass --yes to accept it:\n" + description, EXIT_INVALID)
    print(description)
    return input(f"{question} [y/N] ").strip().lower() == "y"


def cmd_proposals(args: argparse.Namespace) -> int:
    from .proposals import pending

    found = pending(_context(args).store)
    _emit({"proposals": [{"digest": p.digest, **p.body} for p in found]}, args.json,
          "\n\n".join(p.describe() for p in found) or "no pending proposals")
    return EXIT_OK


def cmd_reject(args: argparse.Namespace) -> int:
    from .proposals import ProposalError, find, reject

    ctx = _context(args)
    try:
        proposal = find(ctx.store, args.proposal)
    except ProposalError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    reject(ctx.store, proposal)
    _emit({"rejected": proposal.digest}, args.json, f"rejected proposal {proposal.digest[:12]}")
    return EXIT_OK


def _accept_proposal(args: argparse.Namespace, ctx: query.Context) -> int:
    from .proposals import ProposalError, apply, find

    try:
        proposal = find(ctx.store, args.proposal)
        if not _confirm(args, proposal.describe(), "apply this proposal to the working tree?"):
            print("nothing accepted")
            return EXIT_BLOCKED
        accepted = apply(ctx.project, ctx.store, proposal)
    except ProposalError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    _emit({"proposal": proposal.digest, "accepted_checks": accepted}, args.json,
          f"applied proposal {proposal.digest[:12]}; accepted checks: {', '.join(accepted) or 'none'}")
    return EXIT_OK


def cmd_accept(args: argparse.Namespace) -> int:
    ctx = _context(args)
    if args.proposal:
        return _accept_proposal(args, ctx)
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
    if not _confirm(args, "\n".join(lines), f"accept {len(pending)} check definition(s)?"):
        print("nothing accepted")
        return EXIT_BLOCKED
    ctx.store.approve({manifest.digest(c): {"check_id": c, "accepted_at": now()} for c in pending})
    _emit({"accepted": pending}, args.json, f"accepted: {', '.join(pending)}")
    return EXIT_OK


def cmd_baseline(args: argparse.Namespace) -> int:
    ctx = _context(args)
    try:
        ctx.require_manifest().require(args.check)
    except ManifestError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    run_id = args.run or (ctx.store.latest(args.check) or {}).get("run_id")
    record = ctx.store.record(run_id) if run_id else None
    if record is None or record["check_id"] != args.check or record["state"] != "done":
        raise Refused(f"no finished run of {args.check} to pin", EXIT_INVALID)
    findings: dict[str, int] = {}
    for finding in record.get("findings", []):
        findings[finding["fingerprint"]] = findings.get(finding["fingerprint"], 0) + 1
    baseline = {"run_id": record["run_id"], "pinned_at": now(),
                "measurements": {m["name"]: m["value"] for m in record.get("measurements", [])},
                "findings": findings}
    ctx.store.pin_baseline(args.check, baseline)
    _emit(baseline, args.json, f"baseline for {args.check} pinned from run {record['run_id']}: "
          f"{len(baseline['measurements'])} measurement(s), {sum(findings.values())} accepted finding(s)")
    return EXIT_OK


def cmd_features(args: argparse.Namespace) -> int:
    report = query.status(_context(args))
    if report["feature_error"]:
        raise Refused(report["feature_error"], EXIT_INVALID)
    lines = []
    for feature in report["features"]:
        lines.append(f"[{'verified ' if feature['verified'] else 'UNVERIFIED'}] {feature['id']}: {feature['behavior']}")
        lines += [f"    reach: {step}" for step in feature["how_to_reach"]]
        lines += [f"    gap: {gap}" for gap in feature["gaps"]]
        lines += [f"    problem: {problem}" for problem in feature["problems"]]
    _emit({"features": report["features"]}, args.json, "\n".join(lines) or "no feature map")
    return EXIT_OK if report["features"] and all(f["verified"] for f in report["features"]) else EXIT_BLOCKED


def cmd_project_inspect(args: argparse.Namespace) -> int:
    from .discover import inspect_repository

    inspection = inspect_repository(_context(args).project)
    found = bool(inspection.by_kind("test"))
    _emit(inspection.to_json(), args.json, inspection.render())
    return EXIT_OK if found else EXIT_BLOCKED


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from .mcp import MCPUnavailable, Server, serve_stdio

    project = _context(args).project
    if args.json:
        catalogue = Server.for_mcp(project.root, args.exposure, args.categories).definitions()
        _emit({"project": str(project.root), "tools": catalogue}, True, "")
        return EXIT_OK
    try:
        return serve_stdio(project.root, exposure=args.exposure, categories=args.categories)
    except MCPUnavailable as exc:
        raise Refused(str(exc), EXIT_UNAVAILABLE) from exc


def cmd_tools(args: argparse.Namespace) -> int:
    from .mcp import BY_NAME, Server

    project = _context(args).project
    from .categories import INFRASTRUCTURE_TOOLS

    name = args.enable or args.disable
    if name:
        if name not in BY_NAME or name in INFRASTRUCTURE_TOOLS:
            raise Refused("choose an installed tool other than help", EXIT_INVALID)
        settings = load_settings(project)
        disabled = set(settings.disabled_tools)
        if args.disable:
            disabled.add(name)
        else:
            disabled.discard(name)
        save_settings(project, replace(settings, disabled_tools=tuple(sorted(disabled))))
    category = args.enable_category or args.disable_category
    if category:
        settings = load_settings(project)
        disabled = set(settings.disabled_categories)
        if args.disable_category:
            disabled.add(category)
        else:
            disabled.discard(category)
        save_settings(project, replace(settings, disabled_categories=tuple(sorted(disabled))))
    selectors = {"query": args.query, "offset": args.offset, "limit": args.limit}
    if args.category:
        selectors["category"] = args.category
    body, is_error = Server(project.root).call("help", selectors)
    if is_error:
        raise Refused(body["error"], EXIT_INVALID)
    _emit(body, args.json, "\n\n".join(f"{tool['name']}\n{tool['description']}" for tool in body["tools"]))
    return EXIT_OK


def cmd_console(args: argparse.Namespace) -> int:
    from .console import serve

    return serve(_context(args).project, port=args.port, open_browser=args.open_browser)


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import launch

    return launch(_context(args).project, port=args.port)


def cmd_compute(args: argparse.Namespace) -> int:
    operation = args._vkit_operation
    project = _context(args).project
    if not load_settings(project).allows(operation.name, operation.category):
        raise Refused("unknown or unavailable tool", EXIT_INVALID)
    try:
        body = invoke(operation, project.root, cli_arguments(operation, vars(args)))
    except OperationInputError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    except OperationContractError as exc:
        raise Refused(str(exc), EXIT_INTERNAL) from exc
    _emit(body, args.json, json.dumps(body, indent=2))
    return operation.outcomes[body["status"]]


def _command_parser(container: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    target = container.add_parser(name, help=help_text)
    target.add_argument("--project", help="repository root; defaults to saved repository, then current directory")
    target.add_argument("--json", action="store_true", help="write one JSON object to stdout")
    return target


def _add_operation_parser(compute: Any, operation: Operation) -> argparse.ArgumentParser:
    parser = _command_parser(compute, operation.cli_name, operation.description)
    parser.set_defaults(handler=cmd_compute, _vkit_operation=operation)
    for name, schema in operation.properties.items():
        file_input = operation.cli_files.get(name)
        flag = file_input.flag if file_input is not None else name.replace("_", "-")
        argument_type = str if file_input is not None else (
            lambda text, schema=schema: _parse_cli_value(text, schema))
        options: dict[str, Any] = {"dest": name + "_file" if file_input is not None else name,
                                  "required": name in operation.required}
        options["default"] = argparse.SUPPRESS
        if file_input is not None:
            help_text = f"UTF-8 input file, at most {file_input.max_bytes} bytes"
        else:
            help_text = schema.get("description", "")
            if "enum" in schema:
                options["choices"] = schema["enum"]
        parser.add_argument(f"--{flag}", type=argument_type, help=help_text, **options)
    return parser


def _parse_cli_value(text: str, schema: dict[str, Any]) -> Any:
    try:
        return cli_value(text, schema)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vkit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    command = _command_parser
    command(sub, "doctor", "report whether this project's checks could run")
    status = command(sub, "status", "show every check's freshness and the gate")
    status.add_argument("--path", action="append", help="only checks that read this path (repeatable)")
    command(sub, "gate", "READY only if every check passed against the current inputs")
    command(sub, "features", "show the feature map and which features are verified now")
    tools = command(sub, "tools", "discover enabled tools or change their saved availability")
    tools.add_argument("--query", default="", help="filter enabled tools by words")
    from .categories import CATEGORIES

    tools.add_argument("--category", choices=list(CATEGORIES), help="filter enabled tools by category")
    tools.add_argument("--offset", type=int, default=0)
    tools.add_argument("--limit", type=int, default=10)
    availability = tools.add_mutually_exclusive_group()
    availability.add_argument("--enable", metavar="NAME", help="enable an installed tool by its MCP name")
    availability.add_argument("--disable", metavar="NAME", help="disable an installed tool by its MCP name")
    availability.add_argument("--enable-category", choices=list(CATEGORIES))
    availability.add_argument("--disable-category", choices=list(CATEGORIES))
    accept = command(sub, "accept", "review and accept check definitions so they may run")
    accept.add_argument("--check", action="append", help="a check id (repeatable); default every check")
    accept.add_argument("--proposal", help="apply a pending proposal by digest prefix instead")
    accept.add_argument("--yes", action="store_true", help="accept without the interactive prompt")
    command(sub, "proposals", "list changes agents proposed and nobody has accepted or rejected")
    reject = command(sub, "reject", "discard a pending proposal")
    reject.add_argument("--proposal", required=True)

    baseline = command(sub, "baseline", "pin a run's measurements as the baseline budgets compare against")
    baseline.add_argument("--check", required=True)
    baseline.add_argument("--run", help="default: the latest finished run of the check")

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

    mcp = sub.add_parser("mcp", help="serve an agent over MCP").add_subparsers(dest="mcp_command", required=True)
    serve = command(mcp, "serve", "serve stdio for one project root; --json prints advertised tools")
    serve.add_argument("--exposure", choices=["compact", "named"], default=None)
    serve.add_argument("--categories", nargs="*", choices=["all", *CATEGORIES], default=None,
                       help="category scope; all overrides a saved subset; empty exposes no domain tools")

    compute = sub.add_parser("compute", help="run a built-in source computation").add_subparsers(
        dest="compute_command", required=True)
    for operation in OPERATIONS:
        _add_operation_parser(compute, operation)

    console = command(sub, "console", "serve the interactive console on 127.0.0.1")
    console.add_argument("--port", type=int, help="override saved web port; 0 picks a free port")
    console.add_argument("--browser", dest="open_browser", action=argparse.BooleanOptionalAction,
                         help="override saved browser preference")
    tui = sub.add_parser("tui", help="open the terminal launcher")
    tui.set_defaults(json=False)
    tui.add_argument("--project", help="repository root; defaults to saved repository, then current directory")
    tui.add_argument("--port", type=int, help="override saved web port; 0 picks a free port")
    return parser


_DISPATCH = {
    ("doctor", None): cmd_doctor, ("status", None): cmd_status, ("gate", None): cmd_gate,
    ("features", None): cmd_features, ("accept", None): cmd_accept, ("baseline", None): cmd_baseline, ("check", "run"): cmd_check_run,
    ("run", "show"): cmd_run_show, ("run", "cancel"): cmd_run_cancel,
    ("project", "inspect"): cmd_project_inspect,
    ("proposals", None): cmd_proposals, ("reject", None): cmd_reject,
    ("mcp", "serve"): cmd_mcp_serve, ("console", None): cmd_console,
    ("tools", None): cmd_tools,
    ("tui", None): cmd_tui,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        handler = _DISPATCH[(args.command, getattr(args, f"{args.command}_command", None))]
    try:
        if args.project is None or args.command in {"console", "tui"}:
            defaults = load_launcher_settings()
            if args.project is None:
                args.project = defaults.project or "."
            if args.command in {"console", "tui"}:
                if args.port is None:
                    args.port = defaults.port
                if not 0 <= args.port <= 65535:
                    raise Refused("port must be between 0 and 65535", EXIT_INVALID)
                if args.command == "console" and args.open_browser is None:
                    args.open_browser = defaults.open_browser
        return handler(args)
    except Refused as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "exit_code": exc.code}))
        else:
            print(str(exc), file=sys.stderr)
        return exc.code
    except ValueError as exc:
        _emit({"error": str(exc), "exit_code": EXIT_INVALID}, args.json, str(exc))
        return EXIT_INVALID
    except Exception as exc:  # noqa: BLE001
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())

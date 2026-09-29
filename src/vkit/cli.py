"""The vkit command line.

Three commands in this milestone: doctor, check run, run show. Each exists to
expose the core, not to contain logic. `check run` in particular is a thin shell
around `execution.run_check`, because Plan 02's supervisor will call that
function directly and the two paths must not drift.

Exit codes come from one table, because CONTRACT.md makes them part of the
public interface and a scattered `return 3` is how they drift apart.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from .execution import ExecutionError, run_check
from .identity import compute_source_identity
from .manifest import ManifestError, parse_manifest
from .outcome import Blocked, BlockedReason, Failed, Outcome, Passed
from .paths import ProjectError, open_project
from .storage import Store, StoreError

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INTERNAL = 4


def exit_code_for(outcome: Outcome) -> int:
    """One place that knows what an outcome means to a shell.

    0 PASS, 1 a completed FAIL, 3 BLOCKED. Exit 2 is invalid invocation and 4 is
    an internal error, neither of which is an outcome, so neither appears here.
    """
    if isinstance(outcome, Passed):
        return EXIT_OK
    if isinstance(outcome, Failed):
        return EXIT_CHECK_FAILED
    return EXIT_BLOCKED


def _emit(payload: dict[str, Any], as_json: bool, human: str) -> None:
    """JSON mode writes one structured response to stdout; diagnostics go to stderr.

    Both modes describe the same result. Nothing is printed to stdout twice, so
    `vkit ... --json | jq` always works.
    """
    if as_json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(human)


def _fail(message: str, as_json: bool, code: int) -> int:
    if as_json:
        json.dump({"error": message, "exit_code": code}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report whether this project could run its checks. Launches nothing and
    installs nothing; a doctor that fixes your environment is not a doctor."""
    try:
        project = open_project(args.project)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    findings: list[dict[str, Any]] = []
    manifest: Manifest | None = None
    try:
        manifest = parse_manifest(project, project.runs_root / "doctor-probe")
    except ManifestError as exc:
        findings.append({"check": "<manifest>", "ok": False, "detail": str(exc)})

    if manifest is not None:
        for check in manifest.checks.values():
            for need in check.prerequisites:
                import shutil

                found = shutil.which(need.executable)
                findings.append({
                    "check": check.id,
                    "prerequisite": need.name,
                    "ok": found is not None,
                    "detail": found or f"{need.executable!r} is not on PATH",
                })

    state_writable = True
    detail = str(project.state_root)
    try:
        project.state_root.mkdir(parents=True, exist_ok=True)
        Store(project.db_path)
    except (StoreError, OSError) as exc:
        state_writable = False
        detail = str(exc)

    source = None
    try:
        source = compute_source_identity(project)
    except Exception as exc:  # noqa: BLE001 - doctor reports, it does not raise
        findings.append({"check": "<source>", "ok": False, "detail": str(exc)})

    ok = state_writable and all(f.get("ok", True) for f in findings)
    payload = {
        "command": "doctor",
        "project": str(project.root),
        "ok": ok,
        "state_root": str(project.state_root),
        "state_writable": state_writable,
        "state_detail": detail,
        "checks": sorted(manifest.checks) if manifest else [],
        "findings": findings,
        "source": None if source is None else source.to_json(),
    }
    lines = [
        f"project : {project.root}",
        f"state   : {project.state_root} ({'writable' if state_writable else 'NOT writable'})",
    ]
    if source is not None:
        lines.append(f"head    : {source.head[:12]}  dirty={source.dirty}")
    for finding in findings:
        mark = "ok  " if finding.get("ok", True) else "FAIL"
        label = finding.get("prerequisite", finding["check"])
        lines.append(f"  [{mark}] {label}: {finding['detail']}")
    lines.append("ready" if ok else "not ready")
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK if ok else EXIT_BLOCKED


def cmd_check_run(args: argparse.Namespace) -> int:
    try:
        project = open_project(args.project)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    run_dir = project.runs_root / "probe"
    try:
        manifest = parse_manifest(project, run_dir)
    except ManifestError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    try:
        manifest.require(args.check)
    except ManifestError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    try:
        source = compute_source_identity(project)
        store = Store(project.db_path)
    except (StoreError, OSError) as exc:
        return _fail(str(exc), args.json, EXIT_INTERNAL)

    try:
        result = run_check(manifest, args.check, store=store, source=source)
    except ExecutionError as exc:
        return _fail(str(exc), args.json, EXIT_INTERNAL)

    report = result.report
    payload = {
        "command": "check run",
        "run_id": report["run_id"],
        "check_id": report["check_id"],
        "outcome": report["outcome"],
        "report_path": str(store.run_dir(report["run_id"]) / "report.json"),
    }
    lines = [f"run {report['run_id']}  check {report['check_id']}"]
    body = report["outcome"]
    if body["result"] in ("PASS", "FAIL"):
        for scenario in body["scenarios"]:
            lines.append(f"  {scenario['result']:4} {scenario['id']}: {scenario['observation']}")
    else:
        lines.append(f"  BLOCKED {body['reason']}: {body.get('detail', '')}")
    lines.append(body["result"])
    _emit(payload, args.json, "\n".join(lines))
    return exit_code_for(result.outcome)


def cmd_run_show(args: argparse.Namespace) -> int:
    try:
        project = open_project(args.project)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    try:
        report = Store(project.db_path).load(args.run)
    except StoreError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    body = report["outcome"]
    if body["result"] in ("PASS", "FAIL"):
        human = "\n".join(
            f"  {s['result']:4} {s['id']}: {s['observation']}" for s in body["scenarios"]
        )
    else:
        human = f"  BLOCKED {body['reason']}: {body.get('detail', '')}"
    _emit(report, args.json, f"run {report['run_id']}  {body['result']}\n{human}")
    result = body["result"]
    outcome = (
        Passed(()) if result == "PASS"
        else Failed(()) if result == "FAIL"
        else Blocked(BlockedReason.INTERNAL_ERROR)
    )
    return exit_code_for(outcome)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vkit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project", required=True, help="repository root, or a directory inside it")
        p.add_argument("--json", action="store_true", help="write one JSON object to stdout")
        return p

    add("doctor", "report whether this project's checks could run")

    check = sub.add_parser("check", help="run registered checks")
    check_sub = check.add_subparsers(dest="check_command", required=True)
    run = check_sub.add_parser("run", help="run one registered check in the foreground")
    run.add_argument("--project", required=True)
    run.add_argument("--check", required=True, help="a registered check id, never a command")
    run.add_argument("--json", action="store_true")

    show = sub.add_parser("run", help="inspect a completed run")
    show.add_argument("--project", required=True)
    show.add_argument("--run", required=True, help="run id to display")
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "check":
            return cmd_check_run(args)
        if args.command == "run":
            return cmd_run_show(args)
    except Exception as exc:  # noqa: BLE001 - the shell must not traceback at a user
        print(f"internal error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    parser.print_help(file=sys.stderr)
    return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())

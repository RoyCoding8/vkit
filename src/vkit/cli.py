"""vkit: run the checks a repository registered and report what the evidence establishes.

Exit codes: 0 ok/PASS/READY, 1 FAIL/REJECTED, 2 invalid request, 3 BLOCKED, 4 internal error, 5 unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import query
from .manifest import Manifest, ManifestError
from .paths import Project, ProjectError
from .schemas import SchemaValidationError
from .settings import load_launcher_settings, load_settings, save_settings
from .store import Store, now
from .workspaces import remember

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


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _read_json_file(path: str, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_json_object,
                          parse_constant=_reject_json_constant)
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path!r}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _context(args: argparse.Namespace) -> query.Context:
    try:
        context = query.open_context(args.project)
    except ProjectError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    _remember_workspace(context.project, "cli")
    return context


def _remember_workspace(project: Project, source: str) -> None:
    try:
        remember(project, source)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"warning: could not remember VKit workspace: {exc}", file=sys.stderr, flush=True)


def _outcome_line(record: dict[str, Any]) -> str:
    outcome = record.get("outcome") or {}
    subject_id = record.get("subject", {}).get("id", record.get("check_id"))
    line = f"{subject_id}: {outcome.get('result', record['state'].upper())}"
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


def cmd_feedback_add(args: argparse.Namespace) -> int:
    from .feedback import submit

    request = {"task": args.task, "missing_capability": args.missing_capability,
               "benefit": args.benefit, "kind": args.kind, "run_id": args.run}
    entry = submit(_context(args).store, request)
    _emit(entry, args.json, f"Recorded {entry['kind']} verification feedback {entry['id']}")
    return EXIT_OK


def cmd_feedback_list(args: argparse.Namespace) -> int:
    from .feedback import collect

    report = collect(_context(args).store, kind=args.kind, offset=args.offset, limit=args.limit)
    _emit(report, args.json, "\n".join(
        f"[{entry['kind']}] {entry['path']}" for entry in report["items"]
    ) or "No verification feedback recorded")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    report = query.status(_context(args), args.path or ())
    lines = [f"gate: {report['gate']['verdict']} - {report['gate']['reason']}"]
    for check in report["checks"]:
        lines.append(f"  {check['state']:<14} {check['id']}  [{check['category']}] {check['inputs']['scope']}")
    for component in report["formal"]:
        lines.append(f"  {component['state']:<14} {component['id']}  [verus_source, required_api]")
        if component["detail"]:
            lines.append(f"    {component['detail']}")
    for path in report["unmapped_paths"]:
        lines.append(f"  no check reads {path}")
    if report["needs_run"]:
        lines.append("run: vkit check run --needed")
    _emit(report, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    if args.candidate is not None or args.target is not None or args.output is not None:
        from .ci import run_gate
        from .paths import open_project

        if not all((args.candidate, args.target, args.output)):
            raise Refused("the committed gate requires --candidate, --target and --output", EXIT_INVALID)
        project = open_project(args.project)
        _remember_workspace(project, "cli")
        report = run_gate(project, args.candidate, args.target, Path(args.output))
        _emit(report, args.json, f"{report['verdict']}: {report['reason']}")
        return EXIT_OK if report["verdict"] == "READY" else EXIT_FAILED if report["verdict"] == "FAILED" else EXIT_BLOCKED
    report = query.status(_context(args))["gate"]
    lines = [f"{report['verdict']}: {report['reason']}"]
    lines += [f"  {c['state']:<14} {c['id']}" for c in report["checks"]]
    lines += [f"  {c['state']:<14} {c['id']} [formal]" for c in report.get("formal", [])]
    _emit(report, args.json, "\n".join(lines))
    return _VERDICT_EXIT[report["verdict"]]


def cmd_check_run(args: argparse.Namespace) -> int:
    from . import jobs

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
    records = [jobs.submit(ctx.project, manifest, check_id, store=ctx.store) for check_id in check_ids]
    if not args.detach:
        records = [jobs.wait(ctx.store, r["run_id"]) for r in records]
        from .inputs import canonical_digest, capture_working_tree
        from .state import evaluator_digests
        tree = {item.path: item.content for item in capture_working_tree(ctx.project).files}
        for record in records:
            check_id = (record.get("subject") or {}).get("id")
            if (record.get("outcome") or {}).get("result") == "FAIL" and check_id in manifest.checks:
                ctx.store.retain_fail(check_id,
                                      canonical_digest(evaluator_digests(manifest.require(check_id))), tree)
    results = {(r.get("outcome") or {}).get("result", "BLOCKED" if r["state"] == "interrupted" else None)
               for r in records}
    _emit({"command": "check run", "runs": records}, args.json,
          "\n".join(_outcome_line(r) for r in records) or "every check is fresh; nothing to run")
    for result in ("FAIL", "BLOCKED"):
        if result in results:
            return _RESULT_EXIT[result]
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    from .formal.service import resolve_commit, verify
    from .formal.verus import ExecutionSetupError

    ctx = _context(args)
    query.normalize_run_sections(args.section)
    if args.request:
        if args.component is not None or args.working_tree or args.candidate is not None:
            raise Refused("--request cannot be combined with a component or candidate option", EXIT_INVALID)
        if args.wait is not None or args.reuse:
            raise Refused("--request cannot be combined with --wait or --reuse", EXIT_INVALID)
        request = _read_json_file(args.request, "verification request")
    else:
        if args.component is None or (not args.working_tree and args.candidate is None):
            raise Refused("verify requires COMPONENT and either --working-tree or --candidate, or --request FILE", EXIT_INVALID)
        component = ctx.require_manifest().require_component(args.component)
        candidate = ({"kind": "working_tree"} if args.working_tree else
                     {"kind": "git_commit", "oid": resolve_commit(ctx.project, args.candidate)})
        request = {"schema_version": 2, "claim": component.claim.to_json(),
                   "candidate": candidate, "wait_seconds": args.wait or 0, "reuse": args.reuse}
    try:
        record = verify(ctx, request)
    except ExecutionSetupError as exc:
        raise Refused(str(exc), EXIT_BLOCKED) from exc
    view = query.run_view(ctx, record["run_id"], sections=args.section)
    _emit(view, args.json, _run_view_line(view))
    return _RESULT_EXIT.get((view["summary"].get("outcome") or {}).get("result"), EXIT_OK)


def cmd_model_compare(args: argparse.Namespace) -> int:
    from .formal.transitions import compare_transition_request

    report = compare_transition_request(_read_json_file(args.request, "transition comparison request"))
    lines = [f"model comparison: {report['kind']} ({report['scope']})"]
    for delta in report["deltas"]:
        witness = delta["witness"]
        lines.append(f"  {delta['kind']}: {delta['removed']} removed, {delta['added']} added; "
                     f"{witness['side']} witness from {witness['initial_state']} "
                     f"in {len(witness['path'])} steps")
        for step in witness["path"]:
            lines.append(f"    {step['source']} --{step['action']}({step['input']})--> {step['target']}")
        if "probe" in witness:
            probe = witness["probe"]
            lines.append(f"    inspect {probe['state']} with {probe['action']}({probe['input']})")
        lines.append(f"    difference present in {witness['side']}: {', '.join(witness['difference'])}")
    if "reason" in report:
        lines.append(report["reason"])
    lines.append("Explicit finite models only; no source proof or code approval.")
    _emit(report, args.json, "\n".join(lines))
    return {"equivalent": EXIT_OK, "different": EXIT_FAILED}.get(report["kind"], EXIT_BLOCKED)


def cmd_change_certify(args: argparse.Namespace) -> int:
    from . import jobs
    from .change_methods import parse_kani_toolchain

    ctx = _context(args)
    wait_seconds = query.validate_wait_seconds(args.wait)
    request = _read_json_file(args.request, "change certification request")
    toolchain = (parse_kani_toolchain(_read_json_file(args.kani_toolchain, "Kani toolchain pin"))
                 if args.kani_toolchain else None)
    record = jobs.submit_change(ctx.project, request,
                                trusted_target_oid=args.trusted_target,
                                requested_method=args.method,
                                kani_toolchain=toolchain, store=ctx.store)
    record = jobs.wait(ctx.store, record["run_id"], wait_seconds)
    view = query.run_view(ctx, record["run_id"])
    if view is None:
        raise Refused(f"no run {record['run_id']!r}", EXIT_INVALID)
    _emit(view, args.json, _run_view_line(view))
    result = (view["summary"].get("outcome") or {}).get("result")
    if result is not None:
        return _RESULT_EXIT.get(result, EXIT_BLOCKED)
    return EXIT_BLOCKED if view["summary"]["state"] == "interrupted" else EXIT_OK


def cmd_run_show(args: argparse.Namespace) -> int:
    query.normalize_run_sections(args.section)
    view = query.run_view(_context(args), args.run, sections=args.section,
                          log=args.log, offset=args.offset, limit=args.limit,
                          artifact_offset=args.artifact_offset, artifact_limit=args.artifact_limit)
    if view is None:
        raise Refused(f"no run {args.run!r}", EXIT_INVALID)
    summary = view["summary"]
    _emit(view, args.json, _run_view_line(view))
    if summary["state"] == "done":
        return _RESULT_EXIT[(summary.get("outcome") or {}).get("result", "BLOCKED")]
    return EXIT_BLOCKED if summary["state"] == "interrupted" else EXIT_OK


def cmd_run_cancel(args: argparse.Namespace) -> int:
    ctx = _context(args)
    record = ctx.store.record(args.run)
    if record is None:
        raise Refused(f"no run {args.run!r}", EXIT_INVALID)
    if record["state"] not in ("queued", "running"):
        _emit({"run_id": args.run, "cancelled": False, "state": record["state"]}, args.json,
              f"run {args.run} is {record['state']}; nothing to cancel")
        return EXIT_OK
    ctx.store.request_cancel(args.run)
    _emit({"run_id": args.run, "cancelled": True}, args.json, f"asked run {args.run} to stop")
    return EXIT_OK


def cmd_run_wait(args: argparse.Namespace) -> int:
    from .jobs import wait

    ctx = _context(args)
    query.normalize_run_sections(args.section)
    record = wait(ctx.store, args.run, args.timeout)
    view = query.run_view(ctx, record["run_id"], sections=args.section)
    _emit(view, args.json, _run_view_line(view))
    return _RESULT_EXIT.get((view["summary"].get("outcome") or {}).get("result"), EXIT_BLOCKED)


def _run_view_line(view: dict[str, Any]) -> str:
    summary = view["summary"]
    lines = [_outcome_line(summary)]
    if "change_certification" in summary:
        shadow = summary["change_certification"]
        lines.append(f"shadow analysis: {shadow['status']} ({summary.get('assurance') or 'empirical'}, no approval authority)")
        if shadow["requirement"] is not None:
            lines.append(f"requirement: {shadow['requirement']['source']}")
        if "delegated_claim" in shadow:
            delegated = shadow["delegated_claim"]
            lines.append(f"delegated claim: {delegated['status']}")
            if "acceptance" in delegated:
                lines.append(f"acceptance evidence: {delegated['acceptance']['path']}")
            elif "reason" in delegated:
                lines.append(f"claim reason: {delegated['reason']}")
    for section in ("diagnostics", "obligations", "provenance", "artifacts"):
        if section in view:
            lines.append(f"{section}: {json.dumps(view[section], ensure_ascii=False)}")
    if "log" in view:
        lines.append(view["log"]["text"])
    return "\n\n".join(line for line in lines if line)


def cmd_run_retry(args: argparse.Namespace) -> int:
    from . import jobs

    ctx = _context(args)
    previous = ctx.store.record(args.run)
    if previous is None or previous["state"] in ("queued", "running"):
        raise Refused("retry requires a finished or interrupted attempt", EXIT_INVALID)
    record = jobs.retry(ctx.project, args.run, store=ctx.store)
    if not args.detach:
        record = jobs.wait(ctx.store, record["run_id"])
    _emit(record, args.json, _outcome_line(record))
    return _RESULT_EXIT.get((record.get("outcome") or {}).get("result"), EXIT_OK)


def cmd_evidence_export(args: argparse.Namespace) -> int:
    from .evidence import export

    result = export(_context(args).store, args.run, Path(args.output), include_assets=args.include_assets)
    _emit(result, args.json, f"exported evidence to {result['bundle']}")
    return EXIT_OK


def cmd_evidence_replay(args: argparse.Namespace) -> int:
    from . import jobs

    ctx = _context(args)
    record = jobs.replay(ctx.project, ctx.require_manifest(), Path(args.bundle), store=ctx.store,
                         restore_assets=args.restore_assets)
    if not args.detach:
        record = jobs.wait(ctx.store, record["run_id"])
    _emit(record, args.json, _outcome_line(record))
    return _RESULT_EXIT.get((record.get("outcome") or {}).get("result"), EXIT_OK)


AGENT_MARKERS = ("vkit_hook", "claude", "codex", "deepai", "aider", "mcp")


def _telemetry(project: Any, event: str) -> None:
    import time

    try:
        from .store import Store

        root = Store.of(project).root
        root.mkdir(parents=True, exist_ok=True)
        with (root / "telemetry.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, "project": str(getattr(project, "root", "")),
                                     "ts": time.time()}) + "\n")
    except OSError:
        pass


def _parent_commands(depth: int = 2) -> tuple[list[str], bool]:
    commands, pid, degraded = [], os.getppid(), False
    for _ in range(depth):
        try:
            commands.append(Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace"))
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            degraded = pid > 1
            break
        if pid <= 1:
            break
    return commands, degraded


def _confirm(args: argparse.Namespace, description: str, question: str,
             project: Project | None = None) -> str | None:
    if args.yes:
        if os.environ.get("VKIT_ALLOW_YES") != "1":
            raise Refused("this action needs operator confirmation; run it interactively or set "
                          "VKIT_ALLOW_YES in the operator environment:\n" + description, EXIT_INVALID)
        commands, degraded = _parent_commands()
        if degraded:
            _telemetry(project, "provenance_walk_degraded") if project else None
        if any(marker in command.lower() for command in commands for marker in AGENT_MARKERS):
            raise Refused("this acceptance was spawned by an agent hook chain, not an operator; "
                          "a human must accept it:\n" + description, EXIT_INVALID)
        return "operator_env" if sys.stdin.isatty() else "shell_env"
    if args.json or not sys.stdin.isatty():
        raise Refused("review this and confirm it interactively as the operator (or set "
                      "VKIT_ALLOW_YES):\n" + description, EXIT_INVALID)
    print(description)
    return "interactive" if input(f"{question} [y/N] ").strip().lower() == "y" else None


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
        via = _confirm(args, proposal.describe(), "apply this proposal to the working tree?", ctx.project)
        if via is None:
            print("nothing accepted")
            return EXIT_BLOCKED
        accepted = apply(ctx.project, ctx.store, proposal)
        if accepted:
            from .evidence import backend_identity
            from .manifest import parse_manifest
            from .state import evaluator_digests, sentinel as probe

            manifest = parse_manifest(ctx.project)
            _pinned_or_refuse(manifest, accepted)
            from .state import evaluator
            for c in accepted:
                ctx.store.retain_evaluator(c, evaluator(manifest.require(c)))
            checked = {c: probe(ctx.project, manifest.require(c)) for c in accepted}
            ctx.store.approve({manifest.digest(c): _definition_record(
                manifest, c, backend_identity(manifest.require(c)), checked[c], via,
                evaluator_digests(manifest.require(c))) for c in accepted})
            ctx.store.approve({"__inventory__": _inventory_record(ctx.store, manifest, accepted, via)})
    except ProposalError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    _emit({"proposal": proposal.digest, "accepted_checks": accepted}, args.json,
          f"applied proposal {proposal.digest[:12]}; accepted checks: {', '.join(accepted) or 'none'}")
    return EXIT_OK


_LIST_FIELDS = ("required_scenarios", "required_tests", "scenarios", "inputs")


def _def_change(old: Any, new: Any) -> tuple[str, list[str]]:
    if not isinstance(old, dict):
        return "incomparable", ["prior accepted definition not recorded"]
    changes = [(key, old.get(key), new.get(key)) for key in sorted(set(old) | set(new))
               if old.get(key) != new.get(key)]
    if not changes:
        return "unchanged", []
    stronger = weaker = False
    lines = []
    for key, was, current in changes:
        lines.append(f"{key}: {was!r} -> {current!r}")
        if key in _LIST_FIELDS:
            was, current = was or [], current or []
            if not set(was) <= set(current):
                weaker = True
            if not set(current) <= set(was):
                stronger = True
        elif key == "timeout_seconds":
            if was is None or current is None:
                stronger = weaker = True
            else:
                stronger = stronger or current < was
                weaker = weaker or current > was
        else:
            stronger = weaker = True
    label = ("strengthening" if stronger and not weaker else
             "weakening" if weaker and not stronger else "incomparable")
    return label, lines


def _demonstrated_label(label: str, demo: dict[str, int] | None) -> str:
    if label != "strengthening":
        return label
    if not demo or not demo["added"]:
        return "strengthening (unproven: no rejection corpus)"
    return f"strengthening (declared: {demo['added']} added obligation(s), not executed)"


def _corpus(prior_definition: Any, entry: Any) -> dict[str, int]:
    prior = prior_definition if isinstance(prior_definition, dict) else {}
    return {"added": sum(len(set(entry.get(key) or ()) - set(prior.get(key) or ())) for key in _LIST_FIELDS)}


def _prior_acceptances(approved: dict[str, Any], check_id: str) -> list[dict[str, Any]]:
    records = [r for r in approved.values()
               if r.get("kind") == "check_definition" and r.get("check_id") == check_id]
    return sorted(records, key=lambda r: r.get("accepted_at") or "")


def _oracle_diff(store: Store, prior: dict[str, Any], check) -> list[str]:
    import difflib

    from .state import evaluator
    from .verifiers.spec import ExternalCheck

    if isinstance(getattr(check, "variant", None) or check, ExternalCheck):
        return ["oracle: external check - evaluator content is not pinned"]
    was = store.evaluator_bytes(check.id, (prior.get("oracle") or {}).get("digest"))
    if was is None:
        return ["oracle: prior evaluator bytes are not retained (pre byte-store acceptance)"]
    current = evaluator(check)
    changed = [name for name in sorted(set(was) | set(current)) if was.get(name) != current.get(name)]
    lines = ["oracle byte diff vs last acceptance: " + ("; ".join(changed) or "unchanged")]
    for name in changed:
        diff = list(difflib.unified_diff(
            was.get(name, b"").decode("utf-8", "replace").splitlines(),
            current.get(name, b"").decode("utf-8", "replace").splitlines(),
            fromfile=f"{name} (accepted)", tofile=f"{name} (candidate)", lineterm=""))
        lines += [f"  {line}" for line in diff[:20]]
    return lines


def _sentinel_line(found: dict[str, Any]) -> str:
    if found["verdict"] == "rejects":
        return "null-subject sentinel: rejects the null subject (" + ", ".join(found["subject"]) + " emptied)"
    if found["verdict"] == "vacuous":
        return ("null-subject sentinel: VACUOUS - the evaluator passes with "
                + ", ".join(found["subject"]) + " emptied")
    if found["verdict"] == "timeout":
        return "null-subject sentinel: TIMEOUT - no artifact before the probe deadline; a slow evaluator proves nothing"
    if found["verdict"] == "error":
        return "null-subject sentinel: ERROR - the probe produced no judgeable artifact (crash or missing report)"
    return {"not_bound": "null-subject sentinel: NOT BOUND - no subject path distinct from the evaluator",
            "exempt": "null-subject sentinel: exempt for this check kind",
            "unavailable": "null-subject sentinel: unavailable - " + found.get("detail", "unknown")}[found["verdict"]]


def _accept_card(manifest, identities, pending, formal_records, approved, snapshot, store, sentinels=None) -> list[str]:
    from .state import sentinel

    sentinels = sentinels or {}
    lines = []
    for check_id in pending:
        entry = manifest.entries[check_id]
        check = manifest.require(check_id)
        priors = _prior_acceptances(approved, check_id)
        prior = priors[-1] if priors else None
        label, changes = _def_change((prior or {}).get("definition"), entry)
        demo = _corpus((prior or {}).get("definition"), entry)
        lines.append(f"{check_id} ({manifest.digest(check_id)[:12]}): "
                     f"{_demonstrated_label(label, demo)} vs last acceptance"
                     + (f" ({prior.get('accepted_at')}, {prior.get('confirmed_via') or 'unrecorded'})"
                        if prior else ""))
        lines += [f"  {change}" for change in changes]
        if prior is not None:
            first_label, _ = _def_change(priors[0].get("definition"), entry)
            if priors[0] is not prior:
                lines.append(f"  {_demonstrated_label(first_label, demo)} vs first acceptance"
                             f" ({priors[0].get('accepted_at')}, {priors[0].get('confirmed_via') or 'unrecorded'})")
            lines += ["  " + line for line in _oracle_diff(store, prior, check)]
        lines.append("  " + _sentinel_line(sentinels.get(check_id) or sentinel(snapshot.project, check)))
        last = store.latest(check_id)
        lines.append("  evidence: " + (f"last run {last['run_id']} {last['result']}" if last
                                        else "no finished run - accept-before-run relies on this card"))
        lines.append("  inputs: " + ", ".join(entry.get("inputs", ())))
        lines.append("  checker identity: " + json.dumps(identities[check_id], sort_keys=True))
    for component_id, (record, execution) in formal_records.items():
        component = manifest.require_component(component_id)
        lines += [f"formal component {component_id} ({component.digest[:12]})",
                  json.dumps(component.to_json(), indent=2),
                  "coverage: required_api; assurance: trusted_engine; method: verus_source",
                  "checker identity: " + json.dumps(execution, sort_keys=True)]
    sets = [snapshot.inputs(c.inputs) for c in manifest.checks.values()]
    uncovered = [f for f in snapshot.files if not any(s.covers(f) for s in sets)]
    lines.append("uncovered paths: " + (", ".join(uncovered[:20])
                + (f" (+{len(uncovered) - 20} more)" if len(uncovered) > 20 else "") if uncovered else "none"))
    return lines


def _inventory_diff(previous: dict[str, Any] | None, inventory: dict[str, Any],
                    required: dict[str, bool] | None = None) -> list[str]:
    if previous is None:
        return []
    lines = []
    for key, prefix in (("checks", "obligation inventory: "), ("formal_required", "formal obligations: ")):
        was, current = previous.get(key, {}), inventory[key]
        delta = {"removed": sorted(set(was) - set(current)), "added": sorted(set(current) - set(was)),
                 "rebound": sorted(name for name in current if was.get(name) != current[name])}
        if any(delta.values()):
            lines.append(prefix + json.dumps(delta))
    lines += [f"formal obligation no longer required: {name} (still bound until --drop {name})"
              for name in sorted(previous.get("formal_required", {}))
              if required is not None and name not in required]
    lines += [f"formal obligation demoted: {name} (still bound until --drop {name})"
              for name in sorted(previous.get("formal_required", {}))
              if required is not None and required.get(name) is False]
    lines += [f"drop requested: {name} (last evidence run {run})"
              for name, run in sorted(inventory["dropped"].items())
              if previous.get("dropped", {}).get(name) != run]
    lines += [f"drop marker cleared by this acceptance revision: {name} (slot re-accepted; its obligations bind again)"
              for name in sorted(previous.get("dropped", {})) if name not in inventory["dropped"]]
    return lines


def _definition_record(manifest, check_id: str, identity: dict[str, Any],
                       checked: dict[str, Any], via: str, files: dict[str, str]) -> dict[str, Any]:
    from .inputs import canonical_digest
    from .verifiers.spec import ExternalCheck

    external = isinstance(getattr(manifest.require(check_id), "variant", None) or manifest.require(check_id),
                          ExternalCheck)
    return {"kind": "check_definition", "check_id": check_id, "accepted_at": now(), "confirmed_via": via,
            "backend": identity, "definition": manifest.entries[check_id],
            "oracle": {"files": files, "digest": canonical_digest(files), "sentinel": checked,
                       "external": external}}


def _inventory_record(store: Store, manifest: Manifest, ids: list[str], via: str,
                      *, dropped: dict[str, str] | None = None,
                      formal: dict[str, str] | None = None,
                      drop_names: tuple[str, ...] = ()) -> dict[str, Any]:
    from .state import approved_inventory

    base = approved_inventory(store.approved()) or {}
    checks = {**base.get("checks", {}), **{c: manifest.digest(c) for c in ids}}
    for name in (*(dropped or {}), *drop_names):
        checks.pop(name, None)
    return {"kind": "obligation_inventory", "accepted_at": now(),
            "checks": checks,
            "formal_required": {**base.get("formal_required", {}), **(formal or {})},
            "dropped": {**{n: r for n, r in base.get("dropped", {}).items() if n not in ids},
                        **(dropped or {})},
            "confirmed_via": via}


def _pinned_or_refuse(manifest, check_ids: list[str]) -> None:
    from .state import evaluator_gaps

    for check_id in check_ids:
        gaps = evaluator_gaps(manifest.require(check_id))
        if gaps:
            raise Refused(f"{check_id}: evaluator content is not pinned ({'; '.join(gaps)}); "
                          "an empty oracle would match every future edit, so acceptance is refused", EXIT_INVALID)


def cmd_accept(args: argparse.Namespace) -> int:
    ctx = _context(args)
    if args.proposal:
        return _accept_proposal(args, ctx)
    try:
        manifest = ctx.require_manifest()
        explicit = bool(args.check or args.component)
        wanted = args.check or ([] if explicit else sorted(manifest.checks))
        components = args.component or ([] if explicit else sorted(manifest.components))
        for check_id in wanted:
            manifest.require(check_id)
        for component_id in components:
            manifest.require_component(component_id)
    except ManifestError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    approved = ctx.store.approved()
    from .evidence import backend_identity
    from .state import evaluator_digests, evaluator_gaps, oracle_matches, sentinel

    identities = {c: backend_identity(manifest.require(c)) for c in wanted}
    pending = [c for c in wanted if not oracle_matches(approved, manifest.require(c), manifest.digest(c))]
    for check_id in pending:
        gaps = evaluator_gaps(manifest.require(check_id))
        if gaps:
            raise Refused(f"{check_id}: evaluator content is not pinned ({'; '.join(gaps)}); "
                          "an empty oracle would match every future edit, so acceptance is refused", EXIT_INVALID)
    sentinels = {check_id: sentinel(ctx.project, manifest.require(check_id)) for check_id in pending}
    oracle = {check_id: evaluator_digests(manifest.require(check_id)) for check_id in pending}
    from .state import comparison, evaluator

    comparisons = {}
    for check_id in pending:
        priors = _prior_acceptances(approved, check_id)
        outcome = comparison(ctx.store, ctx.project, manifest.require(check_id), priors[-1] if priors else None)
        comparisons[check_id] = outcome
        if outcome["outcome"] == "weakened_check":
            raise Refused(f"{check_id}: weakened_check - the candidate evaluator passes a retained legitimate "
                          "FAIL; acceptance is refused: " + json.dumps(outcome["receipts"], indent=2),
                          EXIT_BLOCKED)
        if outcome["outcome"] == "incomparable":
            raise Refused(f"{check_id}: incomparable - the accepted evaluator cannot run against the current "
                          f"candidate; clearing: {outcome['clearing']}", EXIT_BLOCKED)
    from .formal.verus import ExecutionSetupError, resolve_execution
    from .inputs import canonical_digest

    formal_records = {}
    for component_id in components:
        component = manifest.require_component(component_id)
        try:
            execution = resolve_execution(component)
        except ExecutionSetupError as exc:
            raise Refused(str(exc), EXIT_BLOCKED) from exc
        record = {"kind": "formal_policy", "policy_sha256": component.digest,
                  "package_sha256": component.package.sha256,
                  "checker_sha256": canonical_digest(execution["semantic"])}
        if any(approved.get(component.digest, {}).get(key) != value for key, value in record.items()):
            formal_records[component_id] = (record, execution["semantic"])
    from .inputs import Snapshot
    from .state import approved_inventory

    previous = approved_inventory(approved)
    inventory = {"kind": "obligation_inventory", "accepted_at": now(),
                 "checks": dict((previous or {}).get("checks", {})),
                 "formal_required": dict((previous or {}).get("formal_required", {})),
                 "dropped": {name: run for name, run in (previous or {}).get("dropped", {}).items()
                             if name not in wanted}}
    inventory["checks"].update({c: manifest.digest(c) for c in wanted})
    inventory["formal_required"].update({c: manifest.require_component(c).digest for c in components
                                          if manifest.require_component(c).required})
    for name in args.drop or ():
        inventory["checks"].pop(name, None)
        inventory["formal_required"].pop(name, None)
        last = ctx.store.latest(name) or ctx.store.latest_for("formal_goal", name)
        if last is not None and last.get("result") == "FAIL":
            inventory["dropped"][name] = last["run_id"]
    lines = _accept_card(manifest, identities, pending, formal_records, approved, Snapshot(ctx.project),
                         ctx.store, sentinels)
    for check_id, outcome in comparisons.items():
        lines.append(f"{check_id} comparison: {outcome['outcome']}"
                     + (f" - clearing: {outcome['clearing']}" if outcome["clearing"] else ""))
    lines += _inventory_diff(previous, inventory, {c: comp.required for c, comp in manifest.components.items()})
    inventory_changed = previous != {k: inventory[k] for k in ("checks", "formal_required", "dropped")}
    if not pending and not formal_records and not inventory_changed:
        _emit({"accepted": [], "accepted_components": [], "pending": []}, args.json,
              "every named definition is already accepted")
        return EXIT_OK
    via = _confirm(args, "\n".join(lines), f"accept {len(pending) + len(formal_records)} definition(s)?", ctx.project)
    if via is None:
        print("nothing accepted")
        return EXIT_BLOCKED
    records = {manifest.digest(c): _definition_record(manifest, c, identities[c], sentinels[c], via, oracle[c])
               for c in pending}
    records.update({manifest.require_component(c).digest: {**entry, "accepted_at": now(), "confirmed_via": via}
                    for c, (entry, _) in formal_records.items()})
    records["__inventory__"] = _inventory_record(ctx.store, manifest, wanted, via,
                                                  dropped=inventory["dropped"],
                                                  formal=inventory["formal_required"],
                                                  drop_names=tuple(args.drop or ()))
    for check_id in pending:
        ctx.store.retain_evaluator(check_id, evaluator(manifest.require(check_id)))
    ctx.store.approve(records)
    _emit({"accepted": pending, "accepted_components": list(formal_records), "confirmed_via": via}, args.json,
          f"accepted ({via}): {', '.join(pending + list(formal_records))}")
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


def _command_parser(container: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    target = container.add_parser(name, help=help_text)
    target.add_argument("--project", help="repository root; defaults to saved repository, then current directory")
    target.add_argument("--json", action="store_true", help="write one JSON object to stdout")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vkit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    command = _command_parser
    command(sub, "doctor", "report whether this project's checks could run")
    status = command(sub, "status", "show every check's freshness and the gate")
    status.add_argument("--path", action="append", help="only checks that read this path (repeatable)")
    gate = command(sub, "gate", "check current inputs, or freshly check a committed candidate under target policy")
    gate.add_argument("--candidate", help="exact full combined-candidate commit supplied by the trusted host")
    gate.add_argument("--target", help="exact full protected target commit supplied by the trusted host")
    gate.add_argument("--output", help="new private evidence directory outside the candidate repository")
    command(sub, "features", "show the feature map and which features are verified now")
    feedback = sub.add_parser("feedback", help="append Markdown requests for missing verification capabilities").add_subparsers(
        dest="feedback_command", required=True)
    add_feedback = command(feedback, "add", "record a task-specific missing verification capability")
    add_feedback.add_argument("--task", required=True, help="task that needed the capability")
    add_feedback.add_argument("--missing-capability", required=True, help="what VKit could not verify")
    add_feedback.add_argument("--benefit", required=True, help="how it would materially improve verification")
    add_feedback.add_argument("--kind", choices=("formal", "reliable"), default="formal")
    add_feedback.add_argument("--run", help="related retained run id")
    list_feedback = command(feedback, "list", "read local feedback, formal verification requests first")
    list_feedback.add_argument("--kind", choices=("formal", "reliable"))
    list_feedback.add_argument("--offset", type=int, default=0)
    list_feedback.add_argument("--limit", type=int, default=20)
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
    accept.add_argument("--component", action="append", help="a formal component id (repeatable)")
    accept.add_argument("--proposal", help="apply a pending proposal by digest prefix instead")
    accept.add_argument("--drop", action="append", help="remove an accepted obligation slot (repeatable)")
    accept.add_argument("--yes", action="store_true", help="accept without the interactive prompt")
    command(sub, "proposals", "list changes agents proposed and nobody has accepted or rejected")
    reject = command(sub, "reject", "discard a pending proposal")
    reject.add_argument("--proposal", required=True)

    baseline = command(sub, "baseline", "pin a run's measurements as the baseline budgets compare against")
    baseline.add_argument("--check", required=True)
    baseline.add_argument("--run", help="default: the latest finished run of the check")

    check = sub.add_parser("check", help="run registered checks").add_subparsers(dest="check_command", required=True)
    run_check = command(check, "run", "submit verification jobs and wait for their results")
    run_check.add_argument("--check", action="append", help="a registered check id (repeatable)")
    run_check.add_argument("--needed", action="store_true", help="run every check that is stale or missing")
    run_check.add_argument("--detach", action="store_true", help="return after durable submission")

    verify = command(sub, "verify", "verify a pinned package claim against its accepted mathematical contract")
    verify.add_argument("component", nargs="?")
    candidate = verify.add_mutually_exclusive_group()
    candidate.add_argument("--working-tree", action="store_true", help="capture current files provisionally")
    candidate.add_argument("--candidate", help="capture a Git revision as raw immutable objects")
    candidate.add_argument("--request", help="read a strict package verification request JSON file")
    verify.add_argument("--wait", type=int, help="wait up to 60 seconds; zero returns the durable job")
    verify.add_argument("--reuse", action="store_true", help="reuse intact local proof evidence only for identical source, policy and checker")
    verify.add_argument("--section", choices=[*query.RUN_SECTIONS, "all"], action="append",
                        help="include selected result detail; repeat to select several sections")

    model = sub.add_parser("model", help="compare explicit finite transition models")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    compare = command(model_commands, "compare", "compare reachable transitions, guards and assumptions")
    compare.add_argument("--request", required=True, help="strict finite transition comparison JSON file")

    change = sub.add_parser("change", help="submit shadow change analysis")
    change_commands = change.add_subparsers(dest="change_command", required=True)
    certify = command(change_commands, "certify", "analyze a candidate against its trusted target and accepted intent")
    certify.add_argument("--request", required=True, help="strict change certification request JSON file")
    certify.add_argument("--trusted-target", required=True, help="exact target commit selected by the host")
    certify.add_argument("--method", choices=("auto", "finite_paired", "rust_kani"), default="auto")
    certify.add_argument("--kani-toolchain", help="host-selected closed Kani toolchain pin JSON file")
    certify.add_argument("--wait", type=float, default=0, help="wait from 0 to 60 seconds; default returns the durable job")

    run = sub.add_parser("run", help="read or stop a run").add_subparsers(dest="run_command", required=True)
    show = command(run, "show", "show a run, its outcome and a window of its log")
    show.add_argument("--run", required=True)
    show.add_argument("--log", choices=("stdout", "stderr"), default="stdout")
    show.add_argument("--offset", type=int, default=0)
    show.add_argument("--limit", type=int, default=query.DEFAULT_LOG_LIMIT)
    show.add_argument("--section", choices=[*query.RUN_SECTIONS, "all"], action="append",
                      help="include selected result detail; repeat to select several sections")
    show.add_argument("--artifact-offset", type=int, default=0)
    show.add_argument("--artifact-limit", type=int, default=query.MAX_ARTIFACTS)
    cancel = command(run, "cancel", "stop a running check and everything it started")
    cancel.add_argument("--run", required=True)
    waiting = command(run, "wait", "wait for a submitted attempt")
    waiting.add_argument("--run", required=True)
    waiting.add_argument("--timeout", type=float, help="maximum wait in seconds; leaves the job running")
    waiting.add_argument("--section", choices=[*query.RUN_SECTIONS, "all"], action="append",
                         help="include selected result detail; repeat to select several sections")
    retry = command(run, "retry", "retry the same captured job as a new execution attempt")
    retry.add_argument("--run", required=True)
    retry.add_argument("--detach", action="store_true")

    evidence = sub.add_parser("evidence", help="export and independently recheck evidence").add_subparsers(
        dest="evidence_command", required=True)
    export = command(evidence, "export", "export a complete evidence bundle")
    export.add_argument("--run", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--include-assets", action="store_true", help="preserve exact formal tool and container bytes")
    replay = command(evidence, "replay", "check a bundle with the current accepted verifier")
    replay.add_argument("--bundle", required=True)
    replay.add_argument("--detach", action="store_true")
    replay.add_argument("--restore-assets", action="store_true", help="restore digest-bound tools and image before a fresh formal replay")

    project = sub.add_parser("project", help="onboard a repository").add_subparsers(
        dest="project_command", required=True)
    command(project, "inspect", "report what this repository declares about building and testing")

    mcp = sub.add_parser("mcp", help="serve an agent over MCP").add_subparsers(dest="mcp_command", required=True)
    serve = command(mcp, "serve", "serve stdio for one project root; --json prints advertised tools")
    serve.add_argument("--exposure", choices=["compact", "named"], default=None)
    serve.add_argument("--categories", nargs="*", choices=["all", *CATEGORIES], default=None,
                       help="category scope; all overrides a saved subset; empty exposes no domain tools")

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
    ("feedback", "add"): cmd_feedback_add, ("feedback", "list"): cmd_feedback_list,
    ("doctor", None): cmd_doctor, ("status", None): cmd_status, ("gate", None): cmd_gate,
    ("features", None): cmd_features, ("accept", None): cmd_accept, ("baseline", None): cmd_baseline, ("check", "run"): cmd_check_run,
    ("run", "show"): cmd_run_show, ("run", "cancel"): cmd_run_cancel,
    ("run", "wait"): cmd_run_wait, ("run", "retry"): cmd_run_retry,
    ("evidence", "export"): cmd_evidence_export, ("evidence", "replay"): cmd_evidence_replay,
    ("project", "inspect"): cmd_project_inspect,
    ("verify", None): cmd_verify,
    ("change", "certify"): cmd_change_certify,
    ("model", "compare"): cmd_model_compare,
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
    except (ValueError, ManifestError, SchemaValidationError) as exc:
        _emit({"error": str(exc), "exit_code": EXIT_INVALID}, args.json, str(exc))
        return EXIT_INVALID
    except Exception as exc:  # noqa: BLE001
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())

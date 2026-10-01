"""The vkit command line.

`check run` is a thin shell around `execution.run_check`, and Plan 02's
supervisor calls that function directly, so the two paths must not drift. The
same holds for every command below: it parses arguments, calls one core
function, and maps what came back to an exit code. When a command needs a
decision that is not "what does the core say", that decision belongs in a core
module, not here.

Exit codes come from one table, because CONTRACT.md makes them part of the
public interface and a scattered `return 3` is how they drift apart.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import idempotency, recover, supervisor, tasks
from .execution import ExecutionError, run_check
from .identity import compute_source_identity
from .manifest import Manifest, ManifestError, parse_manifest
from .outcome import Blocked, BlockedReason, Failed, Outcome, Passed
from .paths import Project, ProjectError, open_project
from .procidentity import ProcessIdentity
from .storage import ConflictError, Store, StoreError, probe_state

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INTERNAL = 4
# A capability this build cannot provide, such as a transport whose optional
# dependency is absent. Distinct from EXIT_INTERNAL, which says the code is
# at fault, and from EXIT_INVALID, which says the request was.
EXIT_UNAVAILABLE = 5

# The operation names an idempotency key is filed under. They are part of the
# stored interface: a key recorded under one name never answers a request made
# under another, so these are constants and not strings built at the call site.
OP_TASK_BEGIN = "task.begin"
OP_CHECK_START = "check.start"
OP_RUN_CANCEL = "run.cancel"


class Refused(Exception):
    """A command that cannot proceed, carrying the exit code that means.

    One exception type so the mapping from a refusal to a shell exit sits in
    `main` next to the table, instead of being repeated as a try/except pair in
    every command.
    """

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


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


def exit_code_for_readiness(readiness: str) -> int:
    """What a finalization verdict means to a shell.

    READY 0, REJECTED 1, BLOCKED 3. REJECTED shares exit 1 with a completed
    failing check because to a script they are the same answer: this was
    decided, and the answer was no.
    """
    if readiness == "READY":
        return EXIT_OK
    if readiness == "REJECTED":
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


# ------------------------------------------------------------------ boundary


def _project(args: argparse.Namespace) -> Project:
    try:
        return open_project(args.project)
    except ProjectError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc


def _open_store(project: Project) -> Store:
    try:
        return Store(project.db_path)
    except (StoreError, OSError) as exc:
        raise Refused(str(exc), EXIT_INTERNAL) from exc


def _read_policy(project: Project) -> Manifest:
    """The registered policy, or the `ManifestError` that says why there is none.

    `tasks.acceptance_context` decides what a missing or malformed policy means,
    so the error has to reach it rather than be turned into an exit code here.
    A command that only *reports* on the policy wraps this in `Refused` at its
    own boundary, which is the one place a code has to be chosen.
    """
    return parse_manifest(project, project.runs_root)


def _task(store: Store, task_id: str) -> tasks.TaskRecord:
    try:
        return tasks.get_task(store, task_id)
    except tasks.TaskError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc


def _read_contract(path: str) -> dict[str, Any]:
    """Read the contract file named on the command line into a request.

    This is the boundary, so the file is read and shape-checked here and nowhere
    else. What the contract *means* is not decided here: `tasks.admit` derives
    the mandatory floor from the approved policy, so the `required_checks` this
    returns is the caller's selection — it can add to the floor and never
    subtract from it. Validating check ids against the manifest is `admit`'s
    refusal, because the manifest is part of the context admission reads.
    """
    optional = ("required_checks", "additional_checks", "description",
                "scope", "required_resources")

    file = Path(path).expanduser()
    if not file.is_absolute():
        file = Path.cwd() / file
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Refused(f"cannot read contract {file}: {exc}", EXIT_INVALID) from exc
    except json.JSONDecodeError as exc:
        raise Refused(f"{file} is not valid JSON: {exc}", EXIT_INVALID) from exc

    if not isinstance(raw, dict):
        raise Refused(f"{file} must contain a JSON object", EXIT_INVALID)
    unknown = sorted(set(raw) - set(optional))
    if unknown:
        raise Refused(
            f"{file} has unsupported key(s) {', '.join(unknown)}; a contract declares "
            f"{', '.join(optional)}",
            EXIT_INVALID,
        )
    selected = raw.get("required_checks", [])
    extra = raw.get("additional_checks", [])
    for key, value in (("required_checks", selected), ("additional_checks", extra)):
        if not isinstance(value, list) or any(not isinstance(i, str) or not i for i in value):
            raise Refused(f"{file}: {key} must be a list of check id strings", EXIT_INVALID)
    scope = raw.get("description", raw.get("scope", ""))
    if not isinstance(scope, str):
        raise Refused(f"{file}: description must be a string", EXIT_INVALID)
    return {
        "selected": list(dict.fromkeys(selected + extra)),
        "scope": scope,
        "resources": raw.get("required_resources"),
    }


def _subject(
    store: Store,
    args: argparse.Namespace,
    operation: str,
    payload: dict[str, Any],
    subject_id: str | None = None,
) -> str:
    """The id this request names, or the one already recorded for it.

    Same request id with the same payload returns the recorded subject, so a
    retry attaches to the task or run that already exists. A conflicting
    payload is a client error, not a store failure, so it is exit 2.

    `subject_id` is for an operation the client already named, such as a
    cancellation of a run it holds. Without it the subject is minted here, and a
    minted subject is a new task or a new run, which is the right default for
    beginning one and the wrong answer for stopping one.
    """
    try:
        return idempotency.begin(
            store, request_id=args.request_id, operation=operation,
            payload=payload, subject_id=subject_id,
        )
    except ConflictError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    except StoreError as exc:
        raise Refused(str(exc), EXIT_INTERNAL) from exc


def _outcome_lines(outcome: dict[str, Any]) -> list[str]:
    if outcome["result"] in ("PASS", "FAIL"):
        return [f"  {s['result']:4} {s['id']}: {s['observation']}" for s in outcome["scenarios"]]
    return [f"  BLOCKED {outcome['reason']}: {outcome.get('detail', '')}"]


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

    state_writable, detail = probe_state(project.state_root, project.db_path)

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
    # Both output modes must describe the same result, so name the checks here
    # too, not only in the JSON payload.
    lines.append(f"checks  : {', '.join(payload['checks']) or '<none>'}")
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


# ------------------------------------------------------------------ plan 02


def cmd_task_begin(args: argparse.Namespace) -> int:
    """Open a task through the core admission decision, at generation 1.

    The contract's own list is the caller's selection and nothing more. What the
    task must actually prove is derived inside `tasks.admit` from the policy in
    force, and a claim it cannot take refuses the admission rather than being
    reported beside an admitted task.
    """
    project = _project(args)
    contract = _read_contract(args.contract)
    store = _open_store(project)
    task_id = _subject(store, args, OP_TASK_BEGIN, {"contract": contract})
    try:
        # A retry of the same request id and the same contract returns the task
        # that already exists rather than opening a second one. A contract that
        # changed under the same id was already refused by the key, so the
        # contract pinned here is the one the returned task was opened with.
        task = tasks.get_task(store, task_id)
    except tasks.TaskError:
        context = tasks.acceptance_context(project, lambda: _read_policy(project))
        try:
            admitted = tasks.admit(
                store, task_id, context=context,
                required_checks=contract["selected"],
                scope=contract["scope"],
                resources=contract["resources"],
            )
        except ConflictError as exc:
            # The resource this task requires is held by someone else. Admission
            # is refused, so nothing exists to run under a task that believes it
            # owns a checkout it does not.
            raise Refused(str(exc), EXIT_BLOCKED) from exc
        except tasks.TaskError as exc:
            # `AdmissionRefused` is a `TaskError`, and every reason it gives is a
            # reason the caller's request is invalid rather than a conflict.
            raise Refused(str(exc), EXIT_INVALID) from exc
        task = admitted.task
        # The policy's floor reaches the caller beside its own selection, so the
        # difference between the two is visible instead of silently applied.
        selected = set(contract["selected"])
        required = list(admitted.contract.required_checks)
        extra = [c for c in required if c not in selected]
    else:
        required = list(task.pinned().required_checks)
        extra = []

    payload = {
        "command": "task begin",
        "task_id": task.task_id,
        "status": task.status,
        "generation": task.generation,
        "contract": task.contract,
        "required_checks": list(required),
        "policy_digest": task.policy_digest,
    }
    lines = [
        f"task {task.task_id}  generation {task.generation}  {task.status}",
        f"requires : {', '.join(required)}",
    ]
    if extra:
        lines.append(f"also required by the approved policy: {', '.join(extra)}")
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_check_start(args: argparse.Namespace) -> int:
    """Run one registered check under a task, returning the run it recorded.

    The run id is claimed before anything executes and handed to the supervisor
    as the run's own id, so a retry of the same request id returns the outcome
    that run already reached rather than starting a second one.
    """
    project = _project(args)
    store = _open_store(project)
    task = _task(store, args.task)
    if task.status == "closed":
        raise Refused(f"task {task.task_id} is closed; its result is final", EXIT_INVALID)
    try:
        manifest = _read_policy(project)
        manifest.require(args.check)
    except ManifestError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc

    # Rechecked here, not only at admission. A required resource can be released
    # by recovery while the attempt is still open, and a check launched under a
    # task that has lost its checkout is two workers writing one tree.
    try:
        tasks.verify_ownership(store, task.task_id, task.generation)
    except ConflictError as exc:
        raise Refused(str(exc), EXIT_BLOCKED) from exc
    except tasks.TaskError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc

    run_id = _subject(
        store, args, OP_CHECK_START, {"task_id": task.task_id, "check_id": args.check}
    )
    try:
        # `start_run` attaches to a run that already published a report, and
        # refuses one that was registered and then interrupted rather than
        # executing a second run under a claimed id. The request id is the only
        # thing that decides whether this is a retry, so the same id and payload
        # always land on the same run and a new id always begins a new one.
        started = supervisor.start_run(
            project, store, args.check,
            task_id=task.task_id, generation=task.generation,
            manifest=manifest, run_id=run_id,
        )
    except supervisor.SupervisorError as exc:
        # A run that is registered with no report is evidence that cannot be
        # decided, which is what BLOCKED means.
        raise Refused(str(exc), EXIT_BLOCKED) from exc
    except (StoreError, ExecutionError) as exc:
        raise Refused(str(exc), EXIT_INTERNAL) from exc

    report = started.report
    body = report["outcome"]
    payload = {
        "command": "check start",
        "run_id": report["run_id"],
        "task_id": task.task_id,
        "attempt": task.generation,
        "check_id": report["check_id"],
        "outcome": body,
        "report_path": str(store.run_dir(report["run_id"]) / "report.json"),
    }
    lines = [f"run {report['run_id']}  task {task.task_id}  check {report['check_id']}"]
    lines.extend(_outcome_lines(body))
    lines.append(body["result"])
    _emit(payload, args.json, "\n".join(lines))
    return exit_code_for(started.outcome)


def cmd_run_cancel(args: argparse.Namespace) -> int:
    """Ask a run to stop, by the process identity the run itself recorded.

    The identity is read from the run, not taken from the client, for the reason
    `procidentity` exists: a bare pid is recycled, and a cancellation aimed at a
    recycled pid kills a stranger. A run that already finished is not cancelled
    at all. It returns the outcome it actually reached.
    """
    store = _open_store(_project(args))
    recorded = store.run_process_identity(args.run)
    if recorded is None:
        raise Refused(f"no run is recorded under {args.run!r}", EXIT_INVALID)

    run_id = _subject(
        store, args, OP_RUN_CANCEL, {"run_id": args.run}, subject_id=args.run
    )
    if run_id != args.run:
        raise Refused(
            f"request id {args.request_id!r} was already used for run {run_id!r}, not "
            f"{args.run!r}; one request id names one run",
            EXIT_INVALID,
        )

    identity = _identity_of(recorded)
    try:
        outcome, report = supervisor.cancel_run(store, args.run, identity=identity)
    except supervisor.SupervisorError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    except StoreError as exc:
        raise Refused(str(exc), EXIT_INTERNAL) from exc

    body = report["outcome"]
    payload = {
        "command": "run cancel",
        "run_id": report["run_id"],
        "cancelled": body["result"] == "BLOCKED" and body.get("reason") == "cancelled",
        "outcome": body,
        "report_path": str(store.run_dir(report["run_id"]) / "report.json"),
    }
    lines = [f"run {report['run_id']}  {body['result']}"]
    lines.extend(_outcome_lines(body))
    _emit(payload, args.json, "\n".join(lines))
    return exit_code_for(outcome)


def _identity_of(recorded: dict[str, Any]) -> ProcessIdentity:
    """The identity a run recorded, or an impossibility if it never recorded one.

    `supervisor.cancel_run` refuses a bare pid by design, and refuses a
    mismatched one with `ownership_lost`, which BLOCKED. A run that launched
    nothing has no pid and is not recoverable by anyone, so it is reported as
    invalid here and `cancel_run` never sees an invented pid.

    The creation time is checked as carefully as the pid. A record carrying a
    pid but no creation time is not a partial identity that can be completed
    later, it is a record that cannot prove ownership of anything: a bare pid
    fails every comparison, so passing `None` through would reach
    `still_the_same_process` as a value that never matches and the user would be
    told the process is not theirs when it is. Refusing here names the actual
    defect instead of a downstream symptom of it.
    """
    pid = recorded.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise Refused(
            f"run {recorded.get('check_id', '<unknown>')!r} recorded no process identity, "
            "so there is no verified owner to cancel",
            EXIT_INVALID,
        )
    creation_time = recorded.get("creation_time")
    if not isinstance(creation_time, int) or isinstance(creation_time, bool) or creation_time <= 0:
        raise Refused(
            f"run {recorded.get('check_id', '<unknown>')!r} recorded pid {pid} without the "
            "creation time that identifies it, so ownership cannot be proven. Cancelling on "
            "a bare pid is refused because pids are recycled; reconcile this run instead",
            EXIT_INVALID,
        )
    return ProcessIdentity(pid=pid, creation_time=creation_time)


def cmd_task_finalize(args: argparse.Namespace) -> int:
    """Decide readiness through the core acceptance decision, and record it.

    There is no verdict argument. CONTRACT.md forbids a client-supplied verdict,
    so the answer is whatever `tasks.finalize` makes of this attempt's runs
    under the contract it was admitted with and the identities in force now, and
    the gaps it reports are what the caller has to read.
    """
    project = _project(args)
    store = _open_store(project)
    task = _task(store, args.task)
    if task.status == "closed":
        raise Refused(f"task {task.task_id} is closed; its result is final", EXIT_INVALID)

    try:
        decision = tasks.finalize(
            store, task.task_id,
            context=tasks.acceptance_context(project, lambda: _read_policy(project)),
        )
    except ConflictError as exc:
        # The attempt was superseded while readiness was being computed. This is
        # a legitimate answer, not an application failure.
        raise Refused(str(exc), EXIT_BLOCKED) from exc
    except tasks.AdmissionRefused as exc:
        # The recorded contract is not one this build can validate, so there is
        # no floor to decide against. That is a block, not a pass.
        raise Refused(str(exc), EXIT_BLOCKED) from exc
    except tasks.TaskError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc

    payload = {
        "command": "task finalize",
        "task_id": task.task_id,
        "status": task.status,
        "readiness": decision.readiness,
        "gaps": list(decision.gaps),
        "history": list(decision.history),
        "required_checks": decision.context.get("required_checks", []),
        "context": decision.context,
    }
    lines = [f"task {task.task_id}  {decision.readiness}"]
    for gap in decision.gaps:
        lines.append(f"  gap: {gap}")
    for entry in decision.history:
        lines.append(f"  earlier attempt: {entry}")
    if decision.readiness == "READY":
        lines.append("local requirements are satisfied; this is not merge permission")
    _emit(payload, args.json, "\n".join(lines))
    return exit_code_for_readiness(decision.readiness)


def cmd_recover(args: argparse.Namespace) -> int:
    """Inspect, and change nothing, unless an action is named with its evidence.

    `recover.apply_action` requires a target and non-empty evidence and refuses
    without them, so the pairing check is one condition here. Without any action
    this command only inspects; it opens no write transaction and moves no state.
    """
    store = _open_store(_project(args))
    if args.action is None:
        if args.target or args.evidence:
            raise Refused(
                "--target and --evidence describe an action; name one with --apply, or "
                "drop both and inspect",
                EXIT_INVALID,
            )
        report = recover.inspect(store)
        payload = {"command": "recover", "inspected": True, **report.to_json()}
        lines = [f"{len(report.findings)} finding(s); nothing was changed"]
        for finding in report.findings:
            lines.append(f"  [{finding.kind.value}] {finding.target}: {finding.detail}")
        _emit(payload, args.json, "\n".join(lines))
        return EXIT_OK

    if not args.target or not args.evidence.strip():
        missing = [name for name, value in (("--target", args.target), ("--evidence", args.evidence))
                   if not (value or "").strip()]
        raise Refused(
            f"refusing to apply {args.action}: {' and '.join(missing)} required; a recovery "
            "action must name what it affects and the evidence permitting it",
            EXIT_INVALID,
        )
    try:
        report = recover.apply_action(
            store, recover.Action(args.action), target=args.target, evidence=args.evidence
        )
    except recover.RecoveryRefused as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc
    except ValueError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc

    payload = {
        "command": "recover",
        "inspected": False,
        "applied": {"action": args.action, "target": args.target, "evidence": args.evidence},
        **report.to_json(),
    }
    lines = [f"applied {args.action} to {args.target}"]
    for finding in report.findings:
        lines.append(f"  [{finding.kind.value}] {finding.target}: {finding.detail}")
    if not report.findings:
        lines.append("no findings remain")
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Serve this project to an agent over MCP, bound to one root.

    The root is fixed at startup and no tool accepts another, so an agent cannot
    steer the server at a different repository.
    """
    from .mcp import MCPUnavailable, Server, serve_stdio, tool_definitions

    try:
        project = open_project(args.project)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    try:
        server = Server(project.root)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    if args.json:
        # A catalogue, so the wiring is inspectable without a transport.
        catalogue = tool_definitions()
        _emit(
            {"command": "mcp serve", "project": str(project.root), "tools": catalogue},
            True,
            "\n".join(t["name"] for t in catalogue),
        )
        return EXIT_OK

    try:
        return serve_stdio(project.root)
    except MCPUnavailable as exc:
        # The transport is an optional dependency. A host that starts this
        # command and gets silence cannot tell a missing SDK from a broken
        # server, so the absence is reported on stderr with its own exit code.
        return _fail(str(exc), False, EXIT_UNAVAILABLE)


def cmd_integration_verify(args: argparse.Namespace) -> int:
    """Decide whether a candidate commit satisfies the approved policy.

    Every refusal is a recorded decision rather than an exception, so the
    `--json` output is always the acceptance record. The exit code follows the
    decision: 0 accepted, 1 rejected, 3 blocked.
    """
    from .integration import verify as integration
    from .integration.checkout import CheckoutError as CheckoutRefused
    from .integration.gitidentity import GitError
    from .integration.policy import PolicyError
    from .integration.verify import IntegrationError, Request

    project = _project(args)
    store = _open_store(project)
    try:
        request = integration.Request(
            project=project,
            candidate_ref=args.candidate,
            target_ref=args.target,
            policy_request=args.policy,
            writers=args.writers,
            verifications=args.verifications,
            keep_checkout=args.keep_checkout,
        )
    except ValueError as exc:
        raise Refused(str(exc), EXIT_INVALID) from exc

    try:
        result = integration.verify(request, store)
    except (PolicyError, GitError) as exc:
        # A policy that cannot be read, or a ref that does not resolve, is an
        # invalid invocation. Nothing ran and nothing was decided.
        raise Refused(str(exc), EXIT_INVALID) from exc
    except IntegrationError as exc:
        raise Refused(str(exc), EXIT_BLOCKED) from exc
    except CheckoutRefused as exc:
        raise Refused(str(exc), EXIT_BLOCKED) from exc

    _emit(result.to_json(), args.json, integration.summarize(result))
    if result.decision == integration.ACCEPTED:
        return EXIT_OK
    if result.decision == integration.REJECTED:
        return EXIT_CHECK_FAILED
    return EXIT_BLOCKED


# ------------------------------------------------------------------ plan 06


def cmd_project_inspect(args: argparse.Namespace) -> int:
    """Report what a repository declares about itself. Launches nothing.

    The exit code is the interesting part. A repository that declares no test
    command is not a broken repository, and it is not a successful inspection
    either: there is nothing here to verify yet. That is exit 3, the same code a
    BLOCKED check uses, because it is the same answer to the same question.
    """
    from .discover import inspect_repository

    try:
        project = open_project(args.project)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    inspection = inspect_repository(project)
    has_actionable = bool(inspection.by_kind("test"))
    if has_actionable:
        lines = [inspection.render(), "", "run `vkit project enroll` to propose policy for these"]
    else:
        lines = [inspection.render(), "", "nothing here can be registered as a check yet"]
    _emit(inspection.to_json(), args.json, "\n".join(lines))
    return EXIT_OK if has_actionable else EXIT_BLOCKED


def cmd_project_enroll(args: argparse.Namespace) -> int:
    """Propose, accept, or decline. Never overwrites, never runs anything.

    The three modes are separate subcommands of one verb because they are
    separate decisions. Proposing is reading; accepting is a person agreeing to
    specific bytes, which is why it takes the digest that was read; declining is
    doing nothing and saying so.
    """
    from .discover import inspect_repository
    from .enroll import (
        EnrollmentError,
        State,
        accept,
        decline,
        diff_summary,
        enroll,
        read_enrollment,
    )

    try:
        project = open_project(args.project)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    if args.accept and args.decline:
        return _fail("--accept and --decline are opposite answers; pick one", args.json, EXIT_INVALID)

    try:
        if args.decline:
            record = decline(project)
            _emit(record.to_json(), args.json, record.render())
            return EXIT_OK

        if args.accept:
            record = accept(project, expected_digest=args.accept_digest or None)
            _emit(record.to_json(), args.json, record.render())
            return EXIT_OK

        proposal, path = enroll(project, inspection=inspect_repository(project))
    except EnrollmentError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    payload = proposal.to_json()
    lines = [proposal.render()]
    existing = project.manifest_path
    if existing.is_file():
        # A refusal to overwrite is not a refusal to help. The difference between
        # what is there and what enrolling would install is the decision the
        # person is being asked to make.
        difference = diff_summary(existing, path)
        if difference:
            lines.extend(["", "this repository already has a hand-maintained policy, and it "
                          "was not touched. The difference enrolling would make:"])
            lines.extend(difference)
        payload["existing_policy"] = str(existing)
        payload["difference"] = difference
    _emit(payload, args.json, "\n".join(lines))

    # A proposal that cannot be accepted yet is not a successful enroll, and the
    # reason is in the output. Exit 3 is the honest code for "nothing can run".
    return EXIT_BLOCKED if not proposal.entries else EXIT_OK


def cmd_features(args: argparse.Namespace) -> int:
    """Show the feature map, and name what is not covered.

    Exit 0 when every feature is verified, exit 3 when any is not. A feature map
    with gaps is not a failure of the application; it is an honest account of
    what nobody has checked, and the exit code is what stops a caller from
    reading "the map loaded" as "everything passes".
    """
    from .features import FeatureError, load_features

    try:
        project = open_project(args.project)
    except ProjectError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    try:
        feature_map = load_features(project.root)
    except FeatureError as exc:
        return _fail(str(exc), args.json, EXIT_INVALID)

    try:
        manifest = parse_manifest(project, project.runs_root / "probe")
        known = sorted(manifest.checks)
    except ManifestError:
        known = []

    uncovered = feature_map.uncovered(known) if known else []
    payload = feature_map.to_json(known)
    payload["command"] = "features"
    lines = [feature_map.render(known)]
    if not known:
        lines.extend([
            "",
            f"no usable manifest at {project.manifest_path}, so no feature can be "
            "verified against a registered check",
        ])
    elif uncovered:
        lines.extend(["", f"{len(uncovered)} feature(s) are not verified:"])
        lines.extend(f"  {row['feature']}: {row['reason']}" for row in uncovered)
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK if known and not uncovered else EXIT_BLOCKED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vkit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> argparse.ArgumentParser:
        target.add_argument("--project", required=True, help="repository root, or a directory inside it")
        target.add_argument("--json", action="store_true", help="write one JSON object to stdout")
        return target

    common(sub.add_parser("doctor", help="report whether this project's checks could run"))

    project = sub.add_parser("project", help="inspect and enroll a repository")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    inspect = project_sub.add_parser(
        "inspect", help="report what this repository says about how to build, test and launch it"
    )
    common(inspect)
    enroll_parser = project_sub.add_parser(
        "enroll", help="propose a manifest for this repository; nothing runs until it is accepted"
    )
    common(enroll_parser)
    enroll_parser.add_argument(
        "--accept", action="store_true",
        help="promote an existing proposal to policy, after showing what it would run",
    )
    enroll_parser.add_argument(
        "--decline", action="store_true",
        help="discard a proposal, leaving every existing file unchanged",
    )
    enroll_parser.add_argument(
        "--accept-digest", default="",
        help="the policy digest that was reviewed; acceptance is refused if the proposal changed since",
    )

    common(sub.add_parser("features", help="show the feature map and what is not covered"))

    check = sub.add_parser("check", help="run registered checks")
    check_sub = check.add_subparsers(dest="check_command", required=True)
    run_check_parser = check_sub.add_parser("run", help="run one registered check in the foreground")
    common(run_check_parser)
    run_check_parser.add_argument("--check", required=True, help="a registered check id, never a command")
    start = check_sub.add_parser("start", help="run one registered check under a task")
    common(start)
    start.add_argument("--task", required=True, help="task id returned by task begin")
    start.add_argument("--check", required=True, help="a registered check id, never a command")
    start.add_argument("--request-id", required=True, help="retry this exact request to get the same run")

    serve = sub.add_parser("mcp", help="serve the project to an agent over MCP")
    serve_sub = serve.add_subparsers(dest="mcp_command", required=True)
    serve_stdio = serve_sub.add_parser("serve", help="serve stdio, bound to one project root")
    serve_stdio.add_argument("--project", required=True,
                             help="the one project root this server answers for")
    serve_stdio.add_argument("--json", action="store_true",
                             help="print the tool catalogue and exit, without serving")

    run = sub.add_parser("run", help="inspect a run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    show = run_sub.add_parser("show", help="display a completed run by id")
    common(show)
    show.add_argument("--run", required=True, help="run id to display")
    cancel = run_sub.add_parser("cancel", help="stop a run by the identity it recorded")
    common(cancel)
    cancel.add_argument("--run", required=True, help="run id to stop")
    cancel.add_argument("--request-id", required=True, help="retry this exact request to get the same answer")

    task = sub.add_parser("task", help="own a check contract and its evidence")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    begin = task_sub.add_parser("begin", help="open a task against a contract file")
    common(begin)
    begin.add_argument("--contract", required=True, help="JSON file naming the required check ids")
    begin.add_argument("--request-id", required=True, help="retry this exact request to get the same task")
    finalize = task_sub.add_parser("finalize", help="compute readiness from recorded evidence")
    common(finalize)
    finalize.add_argument("--task", required=True, help="task id to finalize")

    recovery = sub.add_parser("recover", help="inspect runs, claims and processes; act only with evidence")
    common(recovery)
    recovery.add_argument(
        "--apply", dest="action", metavar="ACTION", default=None,
        choices=[a.value for a in recover.Action],
        help=f"apply one action instead of only inspecting; one of {', '.join(a.value for a in recover.Action)}",
    )
    recovery.add_argument("--target", default="", help="the run id or resource key the action affects")
    recovery.add_argument("--evidence", default="", help="why the change is permitted; may not be empty")

    integration = sub.add_parser(
        "integration",
        help="decide whether a candidate commit satisfies the approved policy",
    )
    integration_sub = integration.add_subparsers(dest="integration_command", required=True)
    verify_parser = integration_sub.add_parser(
        "verify",
        help="run the required checks against one exact candidate commit and record the decision",
    )
    common(verify_parser)
    verify_parser.add_argument("--candidate", required=True,
                               help="the candidate commit, or a ref that resolves to one")
    verify_parser.add_argument("--target", required=True,
                               help="the target commit the candidate must be built on")
    verify_parser.add_argument(
        "--policy", required=True,
        help="@<ref> for the approved policy in a commit, or a local path. A local "
             "path is local evidence and can never be the protected decision",
    )
    verify_parser.add_argument(
        "--verifications", type=int, default=None,
        help="capacity bound on concurrent verification runs for this repository",
    )
    verify_parser.add_argument(
        "--writers", type=int, default=None,
        help="capacity bound on active registered writers; independent of verifications",
    )
    verify_parser.add_argument(
        "--keep-checkout", action="store_true",
        help="leave the candidate checkout in place instead of retiring it",
    )
    return parser


# Which subcommand each parser's verb selected. A flat table, because the
# dispatch below is one comparison per verb and a chain of string tests is how a
# new verb gets wired to the wrong function.
_DISPATCH = {
    ("doctor", None): cmd_doctor,
    ("project", "inspect"): cmd_project_inspect,
    ("project", "enroll"): cmd_project_enroll,
    ("features", None): cmd_features,
    ("check", "run"): cmd_check_run,
    ("check", "start"): cmd_check_start,
    ("run", "show"): cmd_run_show,
    ("run", "cancel"): cmd_run_cancel,
    ("task", "begin"): cmd_task_begin,
    ("task", "finalize"): cmd_task_finalize,
    ("recover", None): cmd_recover,
    ("mcp", "serve"): cmd_mcp_serve,
    ("integration", "verify"): cmd_integration_verify,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH.get((args.command, getattr(args, f"{args.command}_command", None)))
    if handler is None:
        parser.print_help(file=sys.stderr)
        return EXIT_INVALID
    try:
        return handler(args)
    except Refused as exc:
        return _fail(str(exc), args.json, exc.code)
    except Exception as exc:  # noqa: BLE001 - the shell must not traceback at a user
        print(f"internal error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())

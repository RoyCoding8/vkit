"""The real logic: what the console shows, and the six things it may do.

Nothing in this module knows that HTTP exists. A test drives it directly and
never starts a server, which is the point of the dependency direction in
Plan 05: if the operations needed a request object to run, the two layers would
already be tangled.

The three kinds of answer are kept distinct because they mean different things to
an operator:

    a view            what the store already holds, as plain data
    Refused           the core was asked and declined; its reason, verbatim
    NotImplementedInBuild  the core has no such operation yet

There is no fourth. Nothing here invents a fact, and nothing here writes a fact
the store cannot already show. Where the core has no operation, this module says
so and performs nothing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution import ExecutionError
from ..identity import SourceIdentity, compute_source_identity
from ..manifest import Manifest, ManifestError, parse_manifest
from ..paths import Project, ProjectError, open_project
from ..procidentity import CannotConfirm, ProcessIdentity, read_identity
from ..recover import Report as RecoveryReport
from ..recover import inspect as inspect_recovery
from ..storage import Store, StoreError
from ..supervisor import StartedRun, SupervisorError, cancel_run, start_run
from .plan import (
    DEFAULT_RUN_LIMIT,
    LOOPBACK_HOST,
    MAX_LOG_BYTES,
    MAX_RUN_LIMIT,
    WRITABLE,
    Change,
    ChangeSet,
    NotImplementedInBuild,
    Refused,
    operation as named_operation,
)


class ConsoleError(Exception):
    """The console could not answer. Never a refusal, which is a separate type."""


@dataclass(frozen=True)
class Context:
    """Everything one console session is bound to, resolved once at open time.

    Resolved eagerly so that a request never has to decide where the repository
    is, and so a path that is not a project fails at startup with the core's own
    message rather than once per request.

    The manifest is parsed for the read-only views, and a project whose manifest
    is broken is still worth opening: `readiness` reports the parse failure as a
    finding, exactly as `vkit doctor` does, instead of the whole console
    refusing to start.
    """

    project: Project
    store: Store
    manifest: Manifest | None
    manifest_error: str | None


def open_context(path: str | os.PathLike[str]) -> Context:
    """Bind a console to one repository."""
    try:
        project = open_project(path)
    except ProjectError as exc:
        raise Refused(str(exc)) from exc

    try:
        store = Store(project.db_path)
    except (StoreError, OSError) as exc:
        raise ConsoleError(f"the state store is unusable: {exc}") from exc

    try:
        manifest: Manifest | None = parse_manifest(project, project.runs_root / "probe")
        manifest_error: str | None = None
    except ManifestError as exc:
        manifest, manifest_error = None, str(exc)
    return Context(project, store, manifest, manifest_error)


# --------------------------------------------------------------------- views


def project_view(context: Context) -> dict[str, Any]:
    """1. Project. Root, shared Git directory, HEAD, dirty.

    The source identity is the core's, read once here rather than recomputed per
    request, because a run that fingerprints the tree is fingerprinting the
    same bytes the run itself will fingerprint.
    """
    document: dict[str, Any] = {
        "root": str(context.project.root),
        "git_common_dir": str(context.project.git_common_dir),
        "state_root": str(context.project.state_root),
        "manifest_path": str(context.project.manifest_path),
        "manifest_error": context.manifest_error,
    }
    try:
        document["source"] = compute_source_identity(context.project).to_json()
    except ProjectError as exc:
        document["source"] = None
        document["source_error"] = str(exc)
    return document


def readiness_view(context: Context) -> dict[str, Any]:
    """2. Readiness. What `vkit doctor` reports, running nothing.

    Deliberately not a call into `cli.cmd_doctor`: that writes an argparse
    namespace and prints to stdout, and this returns a value. The rules it
    applies are the same and both read the same manifest, so there is one
    answer to show, not two that can drift.
    """
    findings: list[dict[str, Any]] = []
    if context.manifest is None:
        findings.append({"check": "<manifest>", "ok": False, "detail": context.manifest_error or "no manifest"})

    checks: list[str] = []
    if context.manifest is not None:
        import shutil

        checks = sorted(context.manifest.checks)
        for check in context.manifest.checks.values():
            for need in check.prerequisites:
                found = shutil.which(need.executable)
                findings.append({
                    "check": check.id,
                    "prerequisite": need.name,
                    "ok": found is not None,
                    "detail": found or f"{need.executable!r} is not on PATH",
                })

    ok = bool(context.manifest is not None) and all(f.get("ok", True) for f in findings)
    return {
        "ok": ok,
        "state_writable": True,
        "state_detail": str(context.project.state_root),
        "checks": checks,
        "findings": findings,
    }


def checks_view(context: Context) -> dict[str, Any]:
    """3 and 4. Installation and checks, as far as the manifest records them.

    "Installation" is reported honestly as what the manifest declares, because
    that is all the store holds in this build. There is no installed-plugin
    record to read, and the console does not get to invent one.
    """
    if context.manifest is None:
        return {"installed": None, "checks": [],
                "note": context.manifest_error or "the manifest could not be parsed"}
    return {
        "installed": None,
        "note": "the core records no installed plugin, skills or hooks yet",
        "checks": [
            {
                "id": check.id,
                "description": check.description,
                "command": list(check.argv),
                "cwd": str(check.cwd),
                "timeout_seconds": check.timeout_seconds,
                "required_scenarios": list(check.required_scenarios),
                "artifact": check.artifact_name,
                "prerequisites": [
                    {"name": need.name, "executable": need.executable, "args": list(need.args)}
                    for need in check.prerequisites
                ],
                "inputs": list(check.inputs),
            }
            for check in context.manifest.checks.values()
        ],
    }


def runs_view(context: Context, limit: int = DEFAULT_RUN_LIMIT) -> dict[str, Any]:
    """5. Runs. Outcome, reason, timings.

    The rows are the store's own, unmodified: the same dicts `list_runs`
    returns, so the console cannot show a different run list from the CLI's. A
    test asserts the equality rather than trusting it.
    """
    bounded = _bounded_limit(limit)
    return {"runs": context.store.list_runs(limit=bounded), "limit": bounded}


def run_detail_view(context: Context, run_id: str) -> dict[str, Any]:
    """6. One run in full. The stored report, as stored.

    The report is already a validated document, so it is passed through whole
    rather than re-described. Re-projecting it here would be a second schema
    that could disagree with the one the run was published against.
    """
    try:
        report = context.store.load(run_id)
    except StoreError as exc:
        raise Refused(str(exc)) from exc
    return {
        "run_id": run_id,
        "report": report,
        "logs": _log_index(context, run_id, report),
    }


def recovery_view(context: Context) -> dict[str, Any]:
    """Crash recovery, read-only. Findings the core found; this applies none.

    `recover.apply_action` mutates a claim or a run, and neither action is on the
    writable surface in this build, so the console shows the findings and refuses
    to offer the buttons.
    """
    report: RecoveryReport = inspect_recovery(context.store)
    return {
        "findings": [finding.to_json() for finding in report.findings],
        "actions_offered": [],
        "note": "applying a recovery action is not on the writable surface in this build",
    }


def log_tail(context: Context, run_id: str, stream: str, *, max_bytes: int = MAX_LOG_BYTES) -> dict[str, Any]:
    """The tail of one run log, bounded.

    A check can produce tens of megabytes, and reading it to render it is how a
    console hangs. This seeks to the end and reads at most `max_bytes`, so the
    cost of a read is bounded no matter what the log is.

    The byte ceiling is a hard read limit rather than a truncation-then-decode: a
    tail that starts mid-codepoint is replaced with U+FFFD for that one character,
    which is the only honest way to present a cut and is a single character, not
    a broken document.
    """
    if stream not in ("stdout", "stderr"):
        raise Refused(f"unknown log stream {stream!r}; the report names 'stdout' or 'stderr'")
    ceiling = _bounded_bytes(max_bytes)

    report = run_detail_view(context, run_id)["report"]
    name = (report.get("logs") or {}).get(stream)
    if not name:
        raise Refused(f"run {run_id} recorded no {stream} log")
    path = _log_path(context, run_id, name)

    if not path.is_file():
        return {"run_id": run_id, "stream": stream, "path": str(path),
                "exists": False, "text": "", "bytes": 0, "truncated": False}

    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > ceiling:
            handle.seek(size - ceiling)
        raw = handle.read(ceiling)
    return {
        "run_id": run_id,
        "stream": stream,
        "path": str(path),
        "exists": True,
        "text": raw.decode("utf-8", "replace"),
        "bytes": len(raw),
        "size": size,
        "truncated": size > len(raw),
    }


def _log_path(context: Context, run_id: str, name: str) -> Path:
    """Resolve a log name, refusing anything that leaves the run directory.

    `Store.resolve_artifact` already holds that containment rule for check-written
    names. The log name comes out of the stored report, which the console did not
    write, so the same containment is applied rather than assumed.
    """
    try:
        return context.store.resolve_artifact(run_id, name)
    except StoreError as exc:
        raise Refused(str(exc)) from exc


# --------------------------------------------------------------- the six ops


def plan_change_set(context: Context, name: str) -> ChangeSet:
    """The exact change set for an operation, before anything runs.

    Not a preview invented here. For a real operation the changes are the ones
    the core performs, named from the record it is about to write; for one the
    core does not have, the change set is empty and says so.
    """
    op = named_operation(name)
    if not op.implemented:
        return ChangeSet(op.name, (), implemented=False, note=op.note)

    if op.name == "run_check":
        if context.manifest is None:
            raise Refused(context.manifest_error or "no manifest")
        return ChangeSet(op.name, (
            Change("runs table", "insert one run row in lifecycle 'preparing'", True),
            Change("runs/<run_id>/", "create the run artifact directory", True),
        ))
    if op.name == "cancel_run":
        return ChangeSet(op.name, (
            Change("runs/<run_id>/report.json",
                   "publish a terminal report, or the outcome already recorded", False),
        ))
    raise NotImplementedInBuild(op.name)


def run_check(context: Context, check_id: str) -> dict[str, Any]:
    """Start a registered check.

    Delegates to the core's supervisor, so the console and the CLI register a
    run, fingerprint the source and publish a report through exactly one path.
    The core decides whether the check id exists; this does not pre-validate it,
    because a second validator is a second opinion that can disagree.
    """
    if not check_id:
        raise Refused("a check id is required; the manifest defines the permitted ones")

    try:
        started: StartedRun = start_run(
            context.project, context.store, check_id, manifest=context.manifest,
        )
    except ManifestError as exc:
        raise Refused(str(exc)) from exc
    except SupervisorError as exc:
        raise Refused(str(exc)) from exc
    except ExecutionError as exc:
        raise ConsoleError(str(exc)) from exc

    return {
        "operation": "run_check",
        "run_id": started.run_id,
        "check_id": check_id,
        "outcome": started.outcome.to_json(),
        "launched": started.launched,
    }


def cancel_check_run(context: Context, run_id: str) -> dict[str, Any]:
    """Cancel a run, by verified process identity.

    The identity is read here, from the live process, and handed to the core
    whole. A bare pid is refused by `cancel_run` because pids are recycled; that
    check stays in the core where the knowledge of what a run recorded lives.

    **The core's own refusal is what surfaces.** There are two ways a run cannot
    be cancelled and both are its answer, not this module's: a run that recorded
    no process, and a run whose process is gone or no longer provable. The second
    is reached by handing the core a pid that matches the recorded one with a
    creation time no live process has, so `still_the_same_process` is false and
    the core emits its own `ownership_lost` with its own explanation of why it
    refused to signal. Rewording that here would defeat the point of calling the
    core at all.
    """
    if not run_id:
        raise Refused("a run id is required to cancel a run")

    recorded = context.store.run_process_identity(run_id) or {}
    pid = recorded.get("pid")
    if not pid:
        return {
            "operation": "cancel_run",
            "run_id": run_id,
            "cancelled": False,
            "outcome": {
                "result": "BLOCKED",
                "reason": "ownership_lost",
                "detail": f"run {run_id} recorded no process, so there is nothing to cancel",
            },
        }

    try:
        live = read_identity(int(pid))
    except CannotConfirm:
        # The pid may be in use and unreadable. That is not the same as gone, and
        # either way ownership is unproven, so the unprovable identity below is
        # the honest thing to hand over rather than a guess.
        live = None

    identity = live if live is not None else ProcessIdentity(pid=int(pid), creation_time=-1)

    try:
        outcome, _report = cancel_run(context.store, run_id, identity=identity)
    except (StoreError, SupervisorError) as exc:
        raise Refused(str(exc)) from exc
    return {
        "operation": "cancel_run",
        "run_id": run_id,
        "cancelled": outcome.reason.value == "cancelled",
        "outcome": outcome.to_json(),
    }


def install(context: Context, **_ignored: Any) -> None:
    """Refuse. The core has no install operation."""
    raise NotImplementedInBuild("install")


def repair(context: Context, **_ignored: Any) -> None:
    """Refuse. The core has no repair operation."""
    raise NotImplementedInBuild("repair")


def remove(context: Context, **_ignored: Any) -> None:
    """Refuse. The core has no remove operation."""
    raise NotImplementedInBuild("remove")


def enroll(context: Context, **_ignored: Any) -> None:
    """Refuse. The core has no enroll operation."""
    raise NotImplementedInBuild("enroll")


#: The writable surface as callables, by name. A router that resolves an
#: operation through this mapping cannot reach a function that is not on the
#: list, which is the property that makes the list the whole surface.
OPERATIONS = {
    "enroll": enroll,
    "install": install,
    "repair": repair,
    "remove": remove,
    "run_check": run_check,
    "cancel_run": cancel_check_run,
}


def writable_surface() -> list[dict[str, Any]]:
    """The permitted operations, as the console shows them."""
    return [
        {
            "name": op.name,
            "effect": op.effect,
            "implemented": op.implemented,
            "writes": list(op.writes),
            "note": op.note,
        }
        for op in WRITABLE
    ]


# ----------------------------------------------------------------- internals


def _bounded_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise Refused(f"limit must be a positive integer, got {limit!r}")
    return min(limit, MAX_RUN_LIMIT)


def _bounded_bytes(max_bytes: int) -> int:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise Refused(f"max_bytes must be a positive integer, got {max_bytes!r}")
    return min(max_bytes, MAX_LOG_BYTES)


def host() -> str:
    """The one address this console will bind."""
    return LOOPBACK_HOST

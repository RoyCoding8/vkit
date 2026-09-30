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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution import ExecutionError
from ..execution import run_check as execute_check
from ..identity import compute_source_identity
# The console reads and writes the enrollment record through the core, which is
# the only module that owns its format. It did not before, and wrote a second
# one, so `enroll(accepted=True)` here and the same call from the CLI disagreed
# about whether a repository was enrolled.
from .. import enroll as core_enroll
from ..manifest import Manifest, ManifestError, parse_manifest
from ..paths import Project, ProjectError, open_project
from ..procidentity import CannotConfirm, ProcessIdentity, UnsupportedPlatform, read_identity
from ..recover import Report as RecoveryReport
from ..recover import inspect as inspect_recovery
from ..storage import Store, StoreError
from ..supervisor import SupervisorError, cancel_run
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
    """3 and 4. Installation and checks.

    The installation is read from the host's own record, because the host is
    what actually installs a plugin. There is no console-held ledger of what was
    installed: this reads `installed_plugins.json` and reports what it says, so
    a second browser and the host always agree.
    """
    record = _installed_plugin_record(PLUGIN_ID)
    try:
        source = str(_plugin_source_dir())
    except Refused:
        # A checkout with no package beside it is a fact to show, not a reason to
        # refuse a read view. install would refuse the same thing with the same
        # message when the operator asks it to act.
        source = None
    installation = {
        "plugin": PLUGIN_ID,
        "source": source,
        "marketplace_registered": _marketplace_registered(),
        "installed": record is not None,
        "install_path": record.get("installPath") if record else None,
        "version": record.get("version") if record else None,
        "scope": record.get("scope") if record else None,
    }
    if context.manifest is None:
        return {"installed": installation, "checks": [],
                "note": context.manifest_error or "the manifest could not be parsed"}
    return {
        "installed": installation,
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
    logs = (report.get("logs") or {})
    return {
        "run_id": run_id,
        "report": report,
        # Only the streams this run actually recorded. A cancel report names no
        # log at all, and listing one anyway would send the page looking for a
        # file the store does not hold.
        "logs": [stream for stream in ("stdout", "stderr") if logs.get(stream)],
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
    if op.name == "enroll":
        if context.manifest is None:
            raise Refused(context.manifest_error or "no manifest")
        return ChangeSet(op.name, (
            Change("verification-kit/enrollment.json",
                   "record acceptance of the manifest's executable policy", True),
        ), note="execution stays disabled until this policy is accepted")
    if op.name in ("install", "repair"):
        return ChangeSet(op.name, (
            Change("host plugin directory", f"{op.name} the plugin, skills and hooks via the host CLI", True),
        ))
    if op.name == "remove":
        return ChangeSet(op.name, (
            Change("host plugin directory", "uninstall the plugin via the host CLI", True),
        ))
    raise NotImplementedInBuild(op.name)


def run_check(context: Context, check_id: str) -> dict[str, Any]:
    """Start a registered check.

    Calls `execution.run_check` directly, which is the same call `vkit check run`
    makes, so the console and the CLI register a run, fingerprint the source and
    publish a report through exactly one path.

    It calls `run_check` rather than `supervisor.start_run`. The console wants
    the run to happen before the HTTP response returns, and `run_check` is the
    call that actually executes a check. `start_run` is the entry point for a
    caller that owns a run's lifecycle across a client disconnect, which the
    console is not.
    """
    if not check_id:
        raise Refused("a check id is required; the manifest defines the permitted ones")

    if context.manifest is None:
        raise Refused(context.manifest_error or "no manifest")
    try:
        context.manifest.require(check_id)
    except ManifestError as exc:
        raise Refused(str(exc)) from exc

    try:
        source = compute_source_identity(context.project)
        result = execute_check(context.manifest, check_id, store=context.store, source=source)
    except (StoreError, OSError) as exc:
        raise ConsoleError(f"the state store could not record the run: {exc}") from exc
    except ExecutionError as exc:
        raise ConsoleError(str(exc)) from exc

    return {
        "operation": "run_check",
        "run_id": result.report["run_id"],
        "check_id": check_id,
        "outcome": result.outcome.to_json(),
        "launched": result.report.get("process") is not None,
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
    except (CannotConfirm, UnsupportedPlatform):
        # The pid may be in use and unreadable. That is not the same as gone, and
        # either way ownership is unproven, so the unprovable identity below is
        # the honest thing to hand over rather than a guess.
        #
        # UnsupportedPlatform is the same kind of "cannot prove" on a host where
        # `procidentity` has no verified answer to give at all. Without it here
        # the exception escaped this boundary and an operator on a POSIX host
        # cancelling a finished run got a traceback instead of the run's outcome.
        live = None

    identity = live if live is not None else ProcessIdentity(pid=int(pid), creation_time=-1)

    try:
        outcome, _report = cancel_run(context.store, run_id, identity=identity)
    except (StoreError, SupervisorError) as exc:
        raise Refused(str(exc)) from exc

    # `cancelled` is a fact about the outcome, and the outcome is a sum type: a
    # run that already reached PASS or FAIL has no reason at all. Reading
    # `.reason` off it would raise instead of answering, so the verdict is
    # carried instead, which is the same value the core published.
    document = outcome.to_json()
    return {
        "operation": "cancel_run",
        "run_id": run_id,
        "cancelled": document["result"] == "BLOCKED"
        and document.get("reason") == "cancelled",
        "outcome": document,
    }


def _host_cli() -> str:
    """The Claude Code CLI, which owns plugin placement.

    Install, repair and remove all shell out to it rather than copying the
    package by hand. The host already owns where a plugin lives, how it is
    enabled, and how its configuration is stored; a second writer would create
    a second answer that drifts the moment the host changes. If the CLI is not
    on PATH there is no installation to make, and saying so beats guessing at
    the host's directory layout.
    """
    import shutil

    found = shutil.which("claude")
    if found is None:
        raise Refused(
            "the 'claude' CLI is not on PATH, so the host cannot install the plugin. "
            "Install Claude Code, or install the plugin with 'claude plugin install' yourself."
        )
    return found


def _run_host(args: list[str]) -> tuple[int, str, str]:
    """Run a host CLI command and return (returncode, stdout, stderr)."""
    import subprocess

    try:
        done = subprocess.run(
            [_host_cli(), *args], capture_output=True, encoding="utf-8",
            errors="replace", timeout=180, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConsoleError(f"'claude {' '.join(args)}' timed out after 180s") from exc
    except OSError as exc:
        raise ConsoleError(f"could not run the host CLI: {exc}") from exc
    return done.returncode, done.stdout, done.stderr


def _plugin_source_dir() -> Path:
    """The plugin package this build ships, wherever the package was checked out.

    Resolved relative to this module rather than the working directory, so a
    console opened anywhere still finds the package it belongs to. The plugin
    sits beside the Python package (`.../plugin`) in a source checkout and
    beside the installed package (`.../vkit/../../plugin`) in a wheel, so both
    are tried before anything is refused.
    """
    here = Path(__file__).resolve()
    candidates = [
        # Source checkout: <root>/src/vkit/console/operations.py -> <root>/plugin
        here.parents[3] / "plugin",
        # Installed wheel: <site-packages>/vkit/console/operations.py -> the bundled copy
        here.parent / "_plugin",
    ]
    for candidate in candidates:
        if (candidate / ".claude-plugin" / "plugin.json").is_file():
            return candidate
    raise Refused(
        f"the vkit plugin package is not present; looked in "
        f"{', '.join(str(c) for c in candidates)}. "
        f"Install operates on a packaged plugin, and this build has none to install."
    )


def _installed_plugin_record(plugin_id: str) -> dict | None:
    """What the host currently has installed for this plugin id, or None.

    Read from the host's own installed_plugins.json. This is a read of the
    host's state to decide idempotence and to report drift; the host remains
    the only writer of it.
    """
    import json
    from pathlib import Path as _Path

    record = _Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not record.is_file():
        return None
    try:
        document = json.loads(record.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    entries = (document.get("plugins") or {}).get(plugin_id) or []
    return entries[0] if entries else None


def _repo_root() -> Path:
    """The checkout this build was loaded from, which is also the marketplace root."""
    return _plugin_source_dir().parent


def _marketplace_registered() -> bool:
    """Whether the host already knows this repository's marketplace."""
    known = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
    if not known.is_file():
        return False
    import json

    try:
        document = json.loads(known.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return _MARKETPLACE in document


def _validate(source: Path) -> None:
    """The package must actually validate before anything is installed.

    Copying bytes and declaring success is how an install "succeeds" and then
    fails to load. `claude plugin validate --strict` is the host's own check, so
    running it here means the thing that decides is the same thing that loads.
    A non-zero exit is refused carrying the validator's own words, because that
    message is what tells an operator which field is wrong.
    """
    code, out, err = _run_host(["plugin", "validate", "--strict", str(source)])
    if code != 0:
        detail = (err or out).strip() or "the validator reported no detail"
        raise Refused(f"the vkit package at {source} did not validate: {detail}")


def install(context: Context, scope: str = "user", **_: Any) -> dict[str, Any]:
    """Install the host plugin, skills, and hooks at a chosen scope.

    Validation first, then the host CLI. The host owns where a plugin lives, how
    it is enabled, and how its configuration is stored; this never copies the
    package into the host's directories by hand, because a second writer is a
    second answer that drifts the moment the host changes. A package that would
    not load is refused before anything is placed.
    """
    if scope != "user":
        raise Refused(
            f"scope {scope!r} is not supported; this build installs at 'user' scope only"
        )
    source = _plugin_source_dir()
    _validate(_repo_root())

    # The host installs plugins from a marketplace, so the repository declares
    # one. Adding it twice is refused by the host rather than duplicated here.
    if not _marketplace_registered():
        code, out, err = _run_host(["plugin", "marketplace", "add", str(_repo_root())])
        if code != 0:
            detail = (err or out).strip() or "the host CLI reported no detail"
            raise Refused(f"the host refused to register the vkit marketplace: {detail}")

    code, out, err = _run_host(["plugin", "install", PLUGIN_ID, "--scope", scope])
    if code != 0:
        detail = (err or out).strip() or "the host CLI reported no detail"
        raise Refused(f"the host CLI refused to install {PLUGIN_ID}: {detail}")
    return {
        "operation": "install",
        "scope": scope,
        "plugin": PLUGIN_ID,
        "installed_from": str(source),
        "validated": True,
        "already_installed": "already installed" in out,
        "host_output": out.strip(),
    }


def repair(context: Context, scope: str = "user", **_: Any) -> dict[str, Any]:
    """Re-apply a drifted installation.

    Idempotent: it re-validates and re-installs through the same host call, and
    the host reports an already-correct installation as already installed rather
    than stacking a second copy. Running it twice leaves the same state as
    running it once, which is the property that makes it safe to retry.

    A drifted install is a version or enablement the host knows about but which
    no longer matches the package. There is no such installation here to repair,
    so this refuses rather than reporting a success for a no-op.
    """
    if scope != "user":
        raise Refused(
            f"scope {scope!r} is not supported; this build repairs at 'user' scope only"
        )
    _validate(_repo_root())
    if _installed_plugin_record(PLUGIN_ID) is None:
        raise Refused(
            f"{PLUGIN_ID} is not installed for this host, so there is no drifted "
            f"installation to repair. Use install first."
        )
    code, out, err = _run_host(["plugin", "update", PLUGIN_ID])
    if code != 0:
        detail = (err or out).strip() or "the host CLI reported no detail"
        raise Refused(f"the host CLI refused to update {PLUGIN_ID}: {detail}")
    return {
        "operation": "repair",
        "scope": scope,
        "plugin": PLUGIN_ID,
        "validated": True,
        "host_output": out.strip(),
    }


def remove(context: Context, **_: Any) -> dict[str, Any]:
    """Undo an installation, leaving no orphans.

    Uninstalling is only half of removing. The host keeps the marketplace
    registration and the cache directory after `plugin uninstall`, so both are
    taken out here and then verified: `remove` reports success only once the
    host lists no plugin under this id and no cache directory remains. That
    check is the difference between "the command succeeded" and "nothing is
    left behind".
    """
    record = _installed_plugin_record(PLUGIN_ID)
    if record is None:
        raise Refused(f"{PLUGIN_ID} is not installed for this host, so there is nothing to remove.")

    code, out, err = _run_host(["plugin", "uninstall", PLUGIN_ID])
    if code != 0:
        detail = (err or out).strip() or "the host CLI reported no detail"
        raise Refused(f"the host CLI refused to uninstall {PLUGIN_ID}: {detail}")

    # The host leaves the marketplace registered. Removing it here is what
    # "no orphans" means; otherwise a later install silently reuses a
    # registration pointing at a checkout that may be gone.
    if _marketplace_registered():
        _code, out2, err2 = _run_host(["plugin", "marketplace", "remove", _MARKETPLACE])
        if _code != 0:
            detail = (err2 or out2).strip() or "the host CLI reported no detail"
            raise Refused(
                f"the plugin was uninstalled but the {PLUGIN_ID!r} marketplace could not be "
                f"removed, so a stale registration is left behind: {detail}"
            )

    remaining = _installed_plugin_record(PLUGIN_ID)
    if remaining is not None:
        raise Refused(
            f"the host still lists the plugin at {remaining.get('installPath')!r} after "
            f"uninstall; the installation is not fully removed."
        )
    cache = Path.home() / ".claude" / "plugins" / "cache" / _MARKETPLACE / _PLUGIN_NAME
    leftover = _remove_cache(cache)
    if leftover is not None:
        # Reported as a refusal, not a warning. "No orphans" that quietly leaves
        # a directory behind is the claim this operation exists to make true, and
        # `shutil.rmtree(ignore_errors=True)` is exactly how it would: it returns
        # success while a locked file survives. The reason names the path.
        raise Refused(
            f"the plugin was uninstalled but its cache directory still holds files "
            f"under {leftover}. Close any process holding it and remove again; "
            f"nothing else is left installed."
        )
    return {
        "operation": "remove",
        "plugin": PLUGIN_ID,
        "uninstalled": True,
        # A Path here would reach the browser as a serialisation error, so every
        # path in a response is a string. The response is JSON and nothing else.
        "cache_removed": str(cache),
        "marketplace_removed": _MARKETPLACE,
        "host_output": out.strip(),
    }


def _remove_cache(cache: Path) -> str | None:
    """Delete the host's cache directory, or name what would not go.

    `shutil.rmtree(..., ignore_errors=True)` is the wrong call here: it reports
    success whatever it managed to delete, so a locked file leaves an orphan
    behind and the operation claims there is none. This deletes without the
    suppression, clearing the read-only flag Windows puts on plugin files, and
    returns a description of whatever survived so the caller can name it rather
    than guess.
    """
    if not cache.is_dir():
        return None
    import shutil
    import stat

    def clear_readonly(func, path, _exc) -> None:
        os.chmod(path, stat.S_IWRITE)
        func(path)

    # `onerror` was renamed `onexc` in 3.12 and removed later; the module must
    # work on both, because the read-only flag is a Windows fact, not a version
    # fact, and silently skipping it is what leaves the orphan.
    handler = (
        {"onexc": clear_readonly} if sys.version_info >= (3, 12) else {"onerror": clear_readonly}
    )
    try:
        shutil.rmtree(cache, **handler)
    except OSError as exc:
        return f"{exc.filename or cache} ({exc.strerror})"
    if cache.exists():
        # Returned without raising and the directory is still there, which is
        # what a partly-deleted tree looks like on Windows.
        return str(cache)
    return None


def enroll(context: Context, accepted: bool = False, **_: Any) -> dict[str, Any]:
    """Register a repository as verified, through the core enrollment record.

    This used to write its own `enrollment.json` with keys the rest of the system
    does not read — `accepted` and `configuration_digest`, where
    `enroll.read_enrollment` expects `state` and `policy_digest`. Two formats for
    one fact is how a repository came to read as enrolled here and not enrolled
    everywhere else. There is one record now, written by `vkit.enroll`, and the
    state reported here is read back from it rather than assumed from the call.

    Without `accepted` this reports the policy and changes nothing at all, so
    execution stays disabled until a person accepts. The proposal the core
    writes beside the manifest is deliberately not written from here: this
    package must leave the working tree byte-identical, and a proposal is a
    file in it.
    """
    if context.manifest is None:
        raise Refused(
            context.manifest_error
            or "the repository has no manifest to enroll against; create "
               "verification/manifest.json first"
        )
    if accepted:
        try:
            core_enroll.accept(context.project)
        except core_enroll.EnrollmentError as exc:
            raise Refused(str(exc)) from exc

    state = core_enroll.read_enrollment(context.project)
    return {
        "operation": "enroll",
        "enrolled": state.state is core_enroll.State.ACCEPTED,
        "accepted": state.state is core_enroll.State.ACCEPTED,
        "state": state.state.value,
        "policy": [
            {"id": check.id, "command": list(check.argv),
             "timeout_seconds": check.timeout_seconds}
            for check in context.manifest.checks.values()
        ],
        "policy_digest": state.policy_digest,
        "record": str(core_enroll.record_path(context.project)),
        "note": (
            "execution stays disabled until this policy is accepted; re-run with "
            "accepted=true" if state.state is not core_enroll.State.ACCEPTED else
            "this repository's policy is accepted; its registered checks may run"
        ),
    }


#: The host names this plugin by id. The marketplace is the repository's own
#: `.claude-plugin/marketplace.json`, so the id is vkit@vkit in every operation.
#: install, repair and remove all resolve it from here, so they cannot drift
#: onto two different names for one package.
_PLUGIN_NAME = "vkit"
_MARKETPLACE = "vkit"
PLUGIN_ID = f"{_PLUGIN_NAME}@{_MARKETPLACE}"



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

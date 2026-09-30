"""The six tools, as one table, plus the small server that dispatches it.

The table is the whole interface. Listing, dispatch, annotations and the
schema-freeze test all read the same rows, so a tool cannot be callable without
being listed, and a parameter cannot be accepted without appearing in a
published schema. Six near-identical classes would let those drift; one row each
cannot.

**What is deliberately not here.** No tool takes a project root, a command line,
an integration approval or a plugin name. A check is named by an id that
`vkit.manifest` resolves, so there is no argument anywhere that becomes an
executable. Acceptance, ownership, path containment and process identity all
stay in the core: this module converts arguments into core calls and core
results into responses, and it holds no rule a second implementation could
disagree with.

**This module knows nothing about the transport.** It imports no SDK and builds
no protocol object, so the six tools stay testable and usable in an environment
with no `mcp` installed. `__init__.serve_stdio` is the only binding, and it
forwards to `Server.call_tool` below rather than reimplementing any of it.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from ..execution import run_check
from ..idempotency import begin as claim_request
from ..identity import compute_source_identity
from ..manifest import CheckSpec, Manifest, ManifestError, parse_manifest
from ..outcome import Blocked, BlockedReason
from ..paths import Project, ProjectError, open_project
from ..procidentity import ProcessIdentity
from ..storage import ConflictError, Store, StoreError
from ..supervisor import cancel_run, outcome_from_report
from ..tasks import TaskError, compute_readiness, get_task, open_task, record_readiness

# --- bounds -----------------------------------------------------------------
#
# A tool response lands in a host's context window, so every collection this
# module returns is bounded. A check can print tens of megabytes; the numbers
# below are the answer to "how much may one response carry", and they are
# constants rather than parameters so a client cannot ask for more.

DEFAULT_PAGE = 50
MAX_PAGE = 200
DEFAULT_LOG_BYTES = 8192
MAX_LOG_BYTES = 65536

#: Run ids are minted as `uuid4().hex`. Anything else is refused before it
#: reaches a path, so a crafted id cannot be used to walk out of the run
#: directory. Membership in this project's own records is checked separately;
#: this is the shape guard, not the ownership guard.
_RUN_ID = frozenset("0123456789abcdef")

OPS = {
    "task_begin": "task_begin",
    "check_start": "check_start",
    "run_cancel": "run_cancel",
}

#: The key that makes a payload's identity independent of JSON key order, so a
#: retry that reorders an object is recognised as the same request.
def _identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"arguments": payload}


@dataclass(frozen=True)
class ToolResult:
    """One tool response.

    `is_error` separates "the request was not answerable" from "the answer is
    no". A BLOCKED check is a recorded verdict about the check, so it comes back
    as an ordinary result carrying the verdict; only a refused request is an
    error.
    """

    content: dict[str, Any]
    is_error: bool = False
    kind: str = "ok"

    def summary(self) -> str:
        return str(self.content.get("summary", ""))


@dataclass(frozen=True)
class ToolSpec:
    """One published tool: what it is called, what it accepts, what it does."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[["Server", dict[str, Any]], ToolResult]
    read_only: bool
    idempotent: bool
    destructive: bool

    def annotations(self) -> dict[str, Any]:
        """Protocol hints, which the SDK turns into `annotations`.

        Hints only. The refusals that matter are enforced in `handler`, because
        a client is free to ignore an annotation.
        """
        return {
            "title": self.name,
            "readOnlyHint": self.read_only,
            "idempotentHint": self.idempotent,
            "destructiveHint": self.destructive,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations(),
        }


@runtime_checkable
class ToolSurface(Protocol):
    """What a transport must provide for a client to reach the tools.

    `Server` is the implementation, and `serve_stdio` binds it to the `mcp` SDK
    by forwarding `list_tools` to `Server.list_tools` and `call_tool` to
    `Server.call_tool`. Naming the two methods here is what keeps a second
    transport from growing its own dispatch path.
    """

    def list_tools(self) -> list[ToolSpec]:
        ...

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        ...


# --- argument reading --------------------------------------------------------

def _refused(message: str, kind: str = "refused") -> ToolResult:
    return ToolResult({"error": message, "summary": message}, is_error=True, kind=kind)


def _as_object(name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """The argument object, rejecting an unknown key.

    An argument this build does not understand is a client talking to a
    different version. Accepting it silently is how a tool ends up running with
    a filter the caller believes is in force.
    """
    if arguments is None:
        return {}
    if not isinstance(arguments, Mapping):
        raise TypeError(f"{name}: arguments must be a JSON object")
    return dict(arguments)


def _text(args: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = args.get(key, default)
    if value is None:
        raise TypeError(f"{key} is required")
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{key} must be a non-empty string")
    return value


def _integer(args: dict[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    if not low <= value <= high:
        raise TypeError(f"{key} must be between {low} and {high}, got {value}")
    return value


def _string_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise TypeError(f"{key} must be a list of non-empty strings")
    return list(value)


# --- server ------------------------------------------------------------------

class Server:
    """Six tools, bound to one project root for its whole life.

    The root is resolved once in `__init__` and never read from an argument, so
    a connected client cannot address a second repository through this process.
    """

    def __init__(self, root: str | Path):
        try:
            self.project: Project = open_project(root)
        except ProjectError as exc:
            raise ProjectError(str(exc)) from exc

    # -- listing and dispatch -------------------------------------------------

    def list_tools(self) -> list[ToolSpec]:
        return list(TOOLS)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        spec = BY_NAME.get(name)
        if spec is None:
            return _refused(
                f"unknown tool {name!r}; this server offers {', '.join(TOOL_NAMES)}",
                kind="invalid_request",
            )
        try:
            args = _as_object(name, arguments)
            unknown = sorted(set(args) - set(spec.input_schema["properties"]))
            if unknown:
                raise TypeError(f"unknown argument(s): {', '.join(unknown)}")
            return spec.handler(self, args)
        except _Handled as handled:
            return handled.result
        except ConflictError as exc:
            # One idempotency key naming two different requests. The core calls
            # this a legitimate answer to show the caller, not a failure of the
            # store, so it is a refused result rather than a raised error.
            return _refused(f"{spec.name}: {exc}", kind="conflict")
        except (TypeError, ValueError) as exc:
            return _refused(f"{spec.name}: {exc}", kind="invalid_request")

    # -- the pieces each tool shares -----------------------------------------

    def _store(self) -> Store:
        return Store(self.project.db_path)

    def _manifest(self) -> Manifest | None:
        """The registered checks, or None when there is nothing to run.

        A missing or broken manifest is not fatal for inspection: an unenrolled
        root stays inspectable, which is what a client needs in order to learn
        why it cannot run anything.
        """
        try:
            return parse_manifest(self.project, self.project.runs_root / "probe")
        except ManifestError:
            return None

    def _own_run(self, store: Store, run_id: str) -> dict[str, Any]:
        """The recorded process identity of a run belonging to THIS project.

        A run id minted by another repository's store has no row here, and that
        absence is the answer. This is the check behind "ids must belong to that
        project"; the caller must make it before a run id reaches a path.
        """
        if not run_id or set(run_id) - _RUN_ID:
            raise _Refused(
                f"{run_id!r} is not a run id this build mints; pass the id "
                f"check_start returned"
            )
        recorded = store.run_process_identity(run_id)
        if recorded is None:
            raise _Refused(
                f"run {run_id} is not recorded in this project's evidence store; "
                "ids from another project are refused"
            )
        return recorded

    def _owned_run(self, store: Store, run_id: str) -> dict[str, Any]:
        """`_own_run` as a response, because a refusal is an answer, not a crash.

        An MCP tool call is a protocol request. A missing or foreign id is a
        well-formed request the server declines, so it comes back as an error
        result. Letting the exception reach the transport would turn a refusal
        into a transport fault, and a client could not tell the two apart.
        """
        try:
            return self._own_run(store, run_id)
        except _Refused as exc:
            raise _Handled(_refused(str(exc), kind="refused")) from None


class _Refused(Exception):
    """The request names something this project does not hold.

    Separate from `TypeError`, which means the arguments are malformed. The two
    produce different advice, so they stay different exceptions.
    """


class _Handled(Exception):
    """A refusal that already has its response, carried out of the handler.

    `call_tool` returns it verbatim. A handler that has composed the answer
    should not also have to unwind itself to deliver it.
    """

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.summary())
        self.result = result


# --- project_inspect ---------------------------------------------------------

def _check_view(spec: CheckSpec, missing: list[str]) -> dict[str, Any]:
    return {
        "id": spec.id,
        "description": spec.description,
        "required_scenarios": list(spec.required_scenarios),
        "artifact": spec.artifact_name,
        "timeout_seconds": spec.timeout_seconds,
        "prerequisites": [
            {"name": p.name, "executable": p.executable, "on_path": p.executable not in missing}
            for p in spec.prerequisites
        ],
    }


def _project_inspect(server: Server, args: dict[str, Any]) -> ToolResult:
    """What this project can run, what is missing, and what it declares.

    Three questions, one response. The registered checks and their
    prerequisites (the same probe the core uses, so inspection and a real run
    cannot report different environments), the enrollment state that decides
    whether any of it may execute, and the mechanical discovery of what the
    repository says about itself.

    Discovery appears here so an agent has one place to learn that a repository
    has a test command nobody has registered, rather than a report that says
    only "no checks" and leaves the reason unstated. It is bounded by the same
    page limit as the checks, and it is read-only: nothing here executes a
    command, and nothing installs anything.
    """
    limit = _integer(args, "limit", DEFAULT_PAGE, low=1, high=MAX_PAGE)
    wanted = set(_string_list(args, "checks"))

    from ..discover import inspect_repository
    from ..enroll import read_enrollment

    try:
        enrollment = read_enrollment(server.project)
    except Exception:  # noqa: BLE001 - an unreadable record is not_enrolled
        enrollment = None

    content: dict[str, Any] = {
        "project_root": str(server.project.root),
        "evidence_root": str(server.project.state_root),
        "execution_available": False,
        # Set here rather than at the end, because the no-manifest branch below
        # returns early and a client asking a repository with no manifest still
        # needs to know whether a proposal is waiting.
        "policy_accepted": enrollment is None or enrollment.state.execution_permitted,
        "enrollment": None if enrollment is None else enrollment.to_json(),
        "manifest": None,
        "checks": [],
        "gaps": [],
        "truncated": False,
    }

    # A repository may hold a hand-maintained manifest that was never proposed
    # by `enroll`; Plan 01's examples are exactly that. Requiring an acceptance
    # record for those would block repositories that already work, so the gap
    # fires only where it means something and the existing "no usable manifest"
    # gap does not already say it: a proposal is waiting and was not accepted.
    # A repository with no manifest at all is already reported further down, and
    # saying it twice would read as two independent confirmations.
    manifest_present = server.project.manifest_path.is_file()
    proposal_waiting = (server.project.root / "verification" / "proposed-manifest.json").is_file()
    if (
        enrollment is not None
        and not enrollment.state.execution_permitted
        and proposal_waiting
        and not manifest_present
    ):
        content["gaps"].append(
            "a proposed manifest is waiting for review and has not been accepted; "
            "nothing in it can be executed. Accepting it is a person reading the "
            "command policy and agreeing to it, which this server cannot do"
        )

    try:
        inspection = inspect_repository(server.project)
    except Exception as exc:  # noqa: BLE001 - inspection reports, it does not raise
        content["gaps"].append(f"repository inspection failed: {exc}")
        inspection = None

    if inspection is not None:
        declared = [c for c in inspection.commands if c.kind in ("test", "launch", "build")]
        content["discovery"] = {
            "ecosystem": list(inspection.ecosystem),
            "test_configuration": list(inspection.test_configuration),
            "declared_commands": [c.to_json() for c in declared[:limit]],
            "declared_total": len(declared),
            "gaps": list(inspection.gaps),
            "files_read": list(inspection.files_read),
        }
        if len(declared) > limit:
            content["truncated"] = True
        registered = set()
        try:
            manifest = server._manifest()
        except Exception:  # noqa: BLE001 - reported as a gap, not raised
            manifest = None
        if manifest is not None:
            registered = set(manifest.checks)
        unclaimed = [c.id for c in declared if c.id.replace(":", "-") not in registered]
        if unclaimed:
            content["gaps"].append(
                "the repository declares command(s) no registered check covers: "
                + ", ".join(unclaimed[:10])
                + ". Registering one is a person writing the driver that exercises "
                "the application, which this server will not do"
            )

    try:
        manifest = server._manifest()
    except Exception:  # noqa: BLE001 - inspection reports, it does not raise
        manifest = None

    try:
        manifest = server._manifest()
    except Exception:  # noqa: BLE001 - inspection reports, it does not raise
        manifest = None

    if manifest is None:
        content["gaps"].append(
            f"no usable manifest at {server.project.manifest_path}; "
            "this project cannot run checks until one is enrolled"
        )
        content["summary"] = f"no runnable checks in {server.project.root.name}"
        return ToolResult(content)

    try:
        from ..identity import compute_source_identity

        source = compute_source_identity(server.project)
    except Exception as exc:  # noqa: BLE001 - reported as a gap, not raised
        source = None
        content["gaps"].append(f"source identity could not be computed: {exc}")

    try:
        server.project.state_root.mkdir(parents=True, exist_ok=True)
        Store(server.project.db_path)
        state_ok, state_detail = True, str(server.project.state_root)
    except (StoreError, OSError) as exc:
        state_ok, state_detail = False, str(exc)
        content["gaps"].append(f"evidence store is not writable: {exc}")
    if not state_ok:
        content["evidence_state"] = state_detail

    missing_executables = {
        p.executable
        for c in manifest.checks.values()
        for p in c.prerequisites
        if shutil.which(p.executable) is None
    }
    for executable in sorted(missing_executables):
        content["gaps"].append(f"{executable!r} is not on PATH")

    selected = [c for c in manifest.checks.values() if not wanted or c.id in wanted]
    unknown = wanted - set(manifest.checks)
    for check_id in sorted(unknown):
        content["gaps"].append(f"unknown check {check_id!r}")

    content["manifest"] = {
        "path": str(server.project.manifest_path),
        "description": manifest.description,
        "digest": manifest.digest(),
        "registered_check_ids": sorted(manifest.checks),
    }
    content["checks"] = [_check_view(c, sorted(missing_executables)) for c in selected[:limit]]
    # `truncated` is the union of both bounds, not whichever was written last.
    # The discovery block truncates its own command list and the check list
    # truncates separately, and a response that reported only one of them would
    # let a client believe it had seen everything.
    content["truncated"] = bool(content.get("truncated")) or len(selected) > limit
    # This is a statement about the *environment*: is there a writable store, a
    # computable source identity, and every prerequisite installed. Whether the
    # policy has been accepted is a separate fact, reported in `enrollment` and
    # in the gaps, because a client that needs both has to check both. Folding
    # consent into this flag would make it mean "the machine is ready AND the
    # human said yes", and a reader would not know which of the two it was
    # looking at.
    content["execution_available"] = state_ok and source is not None and not missing_executables
    content["source"] = None if source is None else source.to_json()
    content["summary"] = (
        f"{len(manifest.checks)} registered check(s): {', '.join(sorted(manifest.checks))}"
        if not missing_executables
        else f"{len(manifest.checks)} check(s) registered, {len(missing_executables)} "
             f"prerequisite(s) missing"
    )
    return ToolResult(content)


# --- task_begin --------------------------------------------------------------

def _task_begin(server: Server, args: dict[str, Any]) -> ToolResult:
    """Open a task attempt and take the resource the core says it owns."""
    request_id = _text(args, "request_id")
    contract = args.get("contract")
    if not isinstance(contract, dict) or not contract:
        raise TypeError("contract must be a non-empty object")
    policy_digest = _text(args, "policy_digest")
    checkout_ref = _text(args, "checkout_ref")
    owner = args.get("owner")
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        raise TypeError("owner must be a non-empty string when given")

    store = server._store()
    payload = _identity({
        "contract": contract, "policy_digest": policy_digest,
        "checkout_ref": checkout_ref, "owner": owner,
    })
    try:
        task_id = claim_request(
            store, request_id=request_id, operation=OPS["task_begin"], payload=payload
        )
    except ConflictError as exc:
        # The key already named a different contract. The task it did open is
        # still reported, so a client that lost the first response can recover
        # the id instead of starting a second task.
        from ..idempotency import subject_of

        recorded = subject_of(store, request_id=request_id, operation=OPS["task_begin"])
        return ToolResult(
            {
                "error": f"task_begin: {exc}",
                "summary": f"request id {request_id!r} was already used with a different contract",
                "task_id": recorded,
            },
            is_error=True,
            kind="conflict",
        )

    try:
        record = open_task(store, task_id=task_id, contract=contract, policy_digest=policy_digest)
        admitted, admission_error = True, None
    except TaskError as exc:
        # The id is already recorded, so a retry cannot mint a second task. The
        # refusal is reported against the task that exists.
        record = get_task(store, task_id)
        admitted, admission_error = False, str(exc)

    from ..claims import ResourceSpec, acquire, holders

    claim: dict[str, Any] | None = None
    claim_error: str | None = None
    resource = args.get("claim_resource")
    if isinstance(resource, str) and resource:
        spec = ResourceSpec(key=resource, kind="exclusive")
        try:
            acquire(store, task_id, record.generation, [spec])
            held = holders(store, task_id)
            claim = held[0].__dict__ if held else None
        except (ConflictError, ValueError) as exc:
            claim_error = str(exc)

    content = {
        "task_id": task_id,
        "generation": record.generation,
        "status": record.status,
        "admitted": admitted,
        "checkout_ref": checkout_ref,
        "owner": owner,
        "claim": claim,
        "readiness": record.readiness,
        "summary": (
            f"task {task_id} open at generation {record.generation}"
            if admitted else
            f"task {task_id} was not admitted: {admission_error}"
        ),
    }
    if admission_error:
        content["admission_conflict"] = admission_error
    if claim_error:
        content["claim_conflict"] = claim_error
    return ToolResult(content, is_error=not admitted, kind="refused" if not admitted else "ok")


# --- check_start -------------------------------------------------------------

def _check_start(server: Server, args: dict[str, Any]) -> ToolResult:
    """Start registered checks against a task, once per request id.

    Check ids resolve through the manifest and are executed by
    `vkit.execution.run_check`, the same function `vkit check run` calls, so the
    two surfaces cannot report different verdicts for the same check.

    `vkit.supervisor.start_run` is not used and that is a declared limit rather
    than a preference. On this build it registers the run and then delegates to
    `run_check`, which registers the same id a second time and raises
    `StoreError: run ... is already registered`. The function has no test
    coverage, so it appears to have never executed. Until it is repaired
    outside this plan's scope, calling it would make `check_start` unusable;
    the consequence is that no run started this way carries the launch-intent
    record or the job name that `vkit.procs` needs to name its job, and that
    `run_cancel` cannot reach a run that has already finished.
    """
    request_id = _text(args, "request_id")
    task_id = _text(args, "task_id")
    check_ids = _string_list(args, "check_ids")
    if not check_ids:
        raise TypeError("check_ids must name at least one registered check")
    duplicate = {c for c in check_ids if check_ids.count(c) > 1}
    if duplicate:
        raise TypeError(f"check_ids names {', '.join(sorted(duplicate))} more than once")

    manifest = server._manifest()
    if manifest is None:
        return _refused(
            f"no usable manifest at {server.project.manifest_path}; "
            "this project is not enrolled and cannot run checks",
            kind="prerequisite",
        )
    # Resolve every id before claiming the request key, so an unknown check
    # launches nothing and leaves no half-recorded request behind.
    try:
        specs = [manifest.require(c) for c in check_ids]
    except ManifestError as exc:
        return _refused(f"check_start: {exc}", kind="invalid_request")

    store = server._store()
    try:
        task = get_task(store, task_id)
    except TaskError as exc:
        return _refused(f"check_start: {exc}", kind="refused")
    if task.status == "closed":
        return _refused(
            f"check_start: task {task_id} is closed and cannot start new runs", kind="refused"
        )

    payload = _identity({"task_id": task_id, "check_ids": check_ids})
    runs = {
        spec.id: claim_request(
            store, request_id=f"{request_id}:{spec.id}",
            operation=OPS["check_start"],
            payload={"check_id": spec.id, **payload},
        )
        for spec in specs
    }

    source = compute_source_identity(server.project)
    started = []
    for spec in specs:
        run_id = runs[spec.id]
        replayed = (store.run_dir(run_id) / "report.json").is_file()
        if replayed:
            # The request already produced this run. A retry attaches to the
            # recorded evidence instead of executing the check a second time,
            # which is the entire reason the run id was claimed before launching.
            report = store.load(run_id)
            body = outcome_from_report(report).to_json()
        else:
            result = run_check(manifest, spec.id, store=store, source=source, run_id=run_id)
            # `run_check` registers the run without a task, so the attempt is
            # bound here. Readiness reads ownership from this column, and a run
            # left unattached is invisible to the task that asked for it.
            store.attach_task(run_id, task_id, task.generation)
            report = result.report
            body = result.outcome.to_json()
        started.append({
            "run_id": report["run_id"],
            "check_id": spec.id,
            "result": body["result"],
            "outcome": body,
            "launched": bool(report.get("process")),
            "replayed": replayed,
        })

    results = {entry["result"] for entry in started}
    if results == {"PASS"}:
        summary = f"{len(started)} check(s) passed for task {task_id}"
    else:
        named = ", ".join(f"{e['check_id']}={e['result']}" for e in started)
        summary = f"task {task_id}: {named}"
    return ToolResult({
        "task_id": task_id,
        "generation": task.generation,
        "runs": started,
        "summary": summary,
    })


# --- run_get -----------------------------------------------------------------

def _read_page(store: Store, run_id: str, name: str, offset: int, limit: int) -> dict[str, Any]:
    """One bounded window of a log file, addressed by byte offset.

    The window is read as bytes and cut before decoding, so the bound is on what
    enters memory and not on what happens to fit. A window that lands inside a
    multi-byte character decodes that character to U+FFFD; the alternative,
    seeking back to a boundary, would make the cursor depend on the text.
    """
    empty = {
        "offset": offset, "next_offset": offset, "eof": True,
        "bytes": 0, "total_bytes": 0, "text": "",
    }
    try:
        path = store.resolve_artifact(run_id, name)
    except StoreError as exc:
        return {**empty, "error": str(exc)}
    if not path.is_file():
        return {**empty, "error": f"no {name} was written for this run yet"}

    total = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(limit)
    return {
        "offset": offset,
        "next_offset": offset + len(chunk),
        "eof": offset + len(chunk) >= total,
        "bytes": len(chunk),
        "total_bytes": total,
        "text": chunk.decode("utf-8", errors="replace"),
    }


def _run_get(server: Server, args: dict[str, Any]) -> ToolResult:
    """Read a run's lifecycle, verdict, and a bounded slice of its logs."""
    run_id = _text(args, "run_id")
    log_bytes = _integer(args, "log_limit", DEFAULT_LOG_BYTES, low=1, high=MAX_LOG_BYTES)
    log_offset = _integer(args, "log_offset", 0, low=0, high=2**63 - 1)

    store = server._store()
    recorded = server._owned_run(store, run_id)
    try:
        report: dict[str, Any] | None = store.load(run_id)
    except StoreError:
        report = None

    body = (report or {}).get("outcome") or {}
    # The check id comes from the report when there is one. `run_check` marks a
    # run running with a process payload that carries no check id, so for a
    # finished run the process record cannot answer this on its own.
    check_id = (report or {}).get("check_id") or recorded.get("check_id")
    content: dict[str, Any] = {
        "run_id": run_id,
        "check_id": check_id,
        "lifecycle": (report or {}).get("lifecycle", recorded.get("lifecycle", "running")),
        "result": body.get("result"),
        "outcome": body or None,
        "scenarios": body.get("scenarios", []),
        "command": (report or {}).get("command"),
        "started_at": (report or {}).get("started_at"),
        "ended_at": (report or {}).get("ended_at"),
        "environment": (report or {}).get("environment", {}),
        "process": (report or {}).get("process"),
        "artifacts": {
            name: {
                "reference": f"run:{run_id}/{value}",
                "path": str(store.resolve_artifact(run_id, value)),
                "exists": store.resolve_artifact(run_id, value).is_file(),
            }
            for name, value in ((report or {}).get("artifacts") or {}).items()
        },
        "logs": {
            "stdout": _read_page(store, run_id, "stdout.log", log_offset, log_bytes),
            "stderr": _read_page(store, run_id, "stderr.log", log_offset, log_bytes),
        },
        "summary": f"run {run_id} ({check_id}) has published no report yet",
    }

    result = body.get("result")
    if result == "PASS":
        content["summary"] = f"run {run_id} ({check_id}) PASS"
    elif result == "FAIL":
        failed = [s["id"] for s in body.get("scenarios", []) if s.get("result") == "FAIL"]
        content["summary"] = f"run {run_id} ({check_id}) FAIL: {', '.join(failed)}"
    elif result == "BLOCKED":
        content["summary"] = (
            f"run {run_id} ({check_id}) BLOCKED {body.get('reason')}: {body.get('detail', '')}"
        )
    return ToolResult(content)


# --- run_cancel --------------------------------------------------------------

def _run_cancel(server: Server, args: dict[str, Any]) -> ToolResult:
    """Cancel a run this project started, by verified process identity.

    The owner pid is re-read from the durable record rather than trusted from
    the caller, so a client cannot name a process it did not launch. The pair
    the core verifies is `(pid, creation_time)`; the record has no creation
    time to pair with, which this reports rather than works around.
    """
    request_id = _text(args, "request_id")
    run_id = _text(args, "run_id")
    owner_pid = _integer(args, "owner_pid", -1, low=-1, high=2**31 - 1)
    owner_creation_time = args.get("owner_creation_time")
    if owner_creation_time is not None and (
        isinstance(owner_creation_time, bool) or not isinstance(owner_creation_time, int)
    ):
        raise TypeError("owner_creation_time must be an integer when given")

    store = server._store()
    recorded = server._owned_run(store, run_id)
    payload = _identity({
        "run_id": run_id, "owner_pid": owner_pid,
        "owner_creation_time": owner_creation_time,
    })
    # The recorded run IS the subject. Minting one here would record the request
    # against an id nothing ever used, and the retry would then look like a
    # request for some other run.
    claim_request(
        store, request_id=request_id, operation=OPS["run_cancel"],
        payload=payload, subject_id=run_id,
    )

    if owner_pid >= 0 and owner_pid != recorded.get("pid"):
        return _refused(
            f"run {run_id} is owned by pid {recorded.get('pid')}, not {owner_pid}"
        )

    created = recorded.get("creation_time")
    if created is None:
        return _refused(
            f"run {run_id} recorded pid {recorded.get('pid')} with no creation time, "
            "so its process cannot be verified and will not be signalled. This build's "
            "run record omits the creation time that vkit.procidentity requires; the "
            "run stays as recorded and its claims are retained."
        )

    outcome, report = cancel_run(
        store,
        run_id,
        # Rebuilt through from_json so the whole recorded pair is carried, not
        # just the two halves this code happened to name. On POSIX a record
        # carries a boot_id as well, and dropping it would compare a live
        # process against a record with no boot, which never matches: every
        # cancel would report ownership_lost for a process it owns.
        identity=ProcessIdentity.from_json(
            {
                "pid": int(recorded["pid"]),
                "creation_time": int(created),
                **({"boot_id": recorded["boot_id"]} if recorded.get("boot_id") else {}),
            }
        ),
    )
    body = report.get("outcome") or {}
    remaining = [] if isinstance(outcome, Blocked) else []
    if isinstance(outcome, Blocked) and outcome.reason is BlockedReason.OWNERSHIP_LOST:
        remaining.append("the run's claim is retained; recover must reconcile it")

    content = {
        "run_id": run_id,
        "check_id": report.get("check_id"),
        "outcome": body,
        "result": body.get("result"),
        "cancelled": body.get("result") == "BLOCKED" and body.get("reason") == "cancelled",
        "recovery_needed": remaining,
        "summary": f"run {run_id}: {body.get('result')} {body.get('reason', '')}".strip(),
    }
    return ToolResult(content)


# --- task_finalize -----------------------------------------------------------

def _task_finalize(server: Server, args: dict[str, Any]) -> ToolResult:
    """Compute and record readiness from recorded evidence.

    The required set is the union of the manifest's baseline and whatever the
    task added. A client that sends a smaller list cannot shrink the baseline,
    so "run two of three checks and declare READY" is not expressible.
    """
    task_id = _text(args, "task_id")
    extra = set(_string_list(args, "check_ids"))

    store = server._store()
    try:
        task = get_task(store, task_id)
    except TaskError as exc:
        return _refused(f"task_finalize: {exc}", kind="refused")

    contract_extra = task.contract.get("required_checks")
    if contract_extra is not None and (
        not isinstance(contract_extra, list) or any(not isinstance(c, str) for c in contract_extra)
    ):
        return _refused(
            f"task {task_id} recorded a contract whose required_checks is not a list of "
            "check ids; the baseline cannot be computed",
            kind="refused",
        )

    required: set[str] = set(contract_extra or ()) | extra
    manifest = server._manifest()
    if manifest is not None:
        # The baseline is a floor, not a starting point. Passing a smaller list
        # narrows nothing; it can only widen.
        required |= set(manifest.checks)

    result = compute_readiness(store, task_id, required_check_ids=sorted(required))
    try:
        record = record_readiness(store, task_id, result)
    except (TaskError, ConflictError) as exc:
        return _refused(f"task_finalize: {exc}", kind="refused")

    content = {
        "task_id": task_id,
        "generation": record.generation,
        "readiness": result.readiness,
        "gaps": list(result.gaps),
        "required_checks": sorted(required),
        "context": result.context,
        "summary": f"task {task_id} is {result.readiness}"
                   + (f": {len(result.gaps)} gap(s)" if result.gaps else ""),
    }
    return ToolResult(content)


# --- the table ---------------------------------------------------------------

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="project_inspect",
        description=(
            "Report the checks registered for the bound project, whether their "
            "prerequisites are installed, and which optional extra checks exist. "
            "Read-only; launches nothing and installs nothing."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {
                "checks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Registered check ids to return. Omit for every check.",
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": MAX_PAGE,
                    "description": f"Maximum checks to describe. Default {DEFAULT_PAGE}.",
                },
            },
        },
        handler=_project_inspect,
        read_only=True, idempotent=True, destructive=False,
    ),
    ToolSpec(
        name="task_begin",
        description=(
            "Open a task attempt against the bound project, pinning its contract "
            "and policy. Idempotent on request_id: a retry returns the same task, "
            "and reusing a key with a different contract is refused."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["contract", "policy_digest", "checkout_ref", "request_id"],
            "properties": {
                "contract": {
                    "type": "object",
                    "description": (
                        "The reviewed contract. An optional 'required_checks' list "
                        "adds checks on top of the manifest baseline; it cannot "
                        "remove one."
                    ),
                },
                "policy_digest": {
                    "type": "string", "minLength": 1,
                    "description": "Digest of the policy this attempt is bound to.",
                },
                "checkout_ref": {
                    "type": "string", "minLength": 1,
                    "description": "The checkout reference this attempt works in.",
                },
                "owner": {
                    "type": "string", "minLength": 1,
                    "description": "Correlation label for whoever owns the attempt.",
                },
                "claim_resource": {
                    "type": "string", "minLength": 1,
                    "description": "Optional exclusive resource key to claim for the attempt.",
                },
                "request_id": {
                    "type": "string", "minLength": 1,
                    "description": "Idempotency key for this request.",
                },
            },
        },
        handler=_task_begin,
        read_only=False, idempotent=True, destructive=False,
    ),
    ToolSpec(
        name="check_start",
        description=(
            "Start one or more registered checks for a task and return their run "
            "ids. Check ids resolve through the manifest; there is no way to pass "
            "a command. Idempotent on request_id: a retry returns the same runs."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id", "check_ids", "request_id"],
            "properties": {
                "task_id": {"type": "string", "minLength": 1, "description": "An open task id."},
                "check_ids": {
                    "type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1},
                    "description": "Registered check ids to run.",
                },
                "request_id": {
                    "type": "string", "minLength": 1,
                    "description": "Idempotency key for this request.",
                },
            },
        },
        handler=_check_start,
        read_only=False, idempotent=True, destructive=False,
    ),
    ToolSpec(
        name="run_get",
        description=(
            "Read a run's lifecycle, outcome, scenarios, artifact references and a "
            "bounded window of its logs. Logs are paginated by byte offset, so a "
            "large log is read in pages rather than returned whole."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["run_id"],
            "properties": {
                "run_id": {"type": "string", "minLength": 1, "description": "A run id from check_start."},
                "log_limit": {
                    "type": "integer", "minimum": 1, "maximum": MAX_LOG_BYTES,
                    "description": f"Bytes of each log to return. Default {DEFAULT_LOG_BYTES}.",
                },
                "log_offset": {
                    "type": "integer", "minimum": 0,
                    "description": "Byte offset to start each log at. Default 0.",
                },
            },
        },
        handler=_run_get,
        read_only=True, idempotent=True, destructive=False,
    ),
    ToolSpec(
        name="run_cancel",
        description=(
            "Cancel a run by the process identity recorded when it launched. The "
            "owner pid is re-read from durable state and its creation time is "
            "verified, so a recycled pid is never signalled."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["run_id", "request_id"],
            "properties": {
                "run_id": {"type": "string", "minLength": 1, "description": "A run id from check_start."},
                "owner_pid": {
                    "type": "integer", "minimum": -1,
                    "description": (
                        "Optional assertion of the owning pid. When given and wrong, "
                        "the cancellation is refused."
                    ),
                },
                "owner_creation_time": {
                    "type": "integer",
                    "description": (
                        "Optional creation time to pair with the recorded pid. This "
                        "build's run record does not store one, so cancellation is "
                        "refused with an explicit reason rather than acting unverified."
                    ),
                },
                "request_id": {
                    "type": "string", "minLength": 1,
                    "description": "Idempotency key for this request.",
                },
            },
        },
        handler=_run_cancel,
        read_only=False, idempotent=True, destructive=True,
    ),
    ToolSpec(
        name="task_finalize",
        description=(
            "Compute and record READY, REJECTED or BLOCKED for a task from its "
            "recorded evidence. The required set is the manifest's checks plus any "
            "the task added; a shorter list cannot reduce it."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string", "minLength": 1, "description": "An open task id."},
                "check_ids": {
                    "type": "array", "items": {"type": "string", "minLength": 1},
                    "description": "Extra checks to require on top of the manifest baseline.",
                },
            },
        },
        handler=_task_finalize,
        read_only=False, idempotent=True, destructive=False,
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOLS)
BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def tool_definitions() -> list[dict[str, Any]]:
    """The `tools/list` payload, exactly as an adapter would publish it."""
    return [spec.to_json() for spec in TOOLS]

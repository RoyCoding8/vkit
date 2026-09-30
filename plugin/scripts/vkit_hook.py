"""The vkit plugin's Claude Code hook adapter.

A hook here reads records that already exist and returns the response its event
documents. It never launches a check, never calls a model, and never touches the
network, because CONTRACT.md requires hooks to be fast local reads over existing
records. Nothing in this module imports `vkit.execution`, `vkit.procs`, or
`vkit.supervisor`, and nothing here spawns a process; a hook that ran a test
suite would make the gate as slow as the thing it is gating.

Three rules shape the code below.

**A missing binding is not a failure.** A hook payload carries a native session
or agent id, and CONTRACT.md forbids guessing a task from timestamps, recent
files, or the last active task. A payload that matches no registered binding
therefore gets no gate, so read-only chats, unenrolled projects, and host
internal agents finish normally.

**A BLOCKED run is never an accepting response.** Readiness is computed from
recorded evidence by the core, and this module only translates that verdict into
the field the event documents. It cannot turn BLOCKED or REJECTED into a stop,
because that is the one translation the product exists to prevent.

**A hook error is not acceptance.** Every failure path returns a response saying
the gate did not run, rather than a traceback and rather than a clean stop that
would read as a pass.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Key in a task contract that binds a vkit task to a host session or agent. The
# core pins whatever contract `task_begin` recorded, so this is a read of an
# existing record and never a guess. A contract without it is unbound, which is
# the safe direction: the hook then has no task to gate.
HOST_BINDING_KEY = "host"
# Key naming the check ids the contract requires. A task contract that does not
# declare them has no declared evidence requirement, so the hook has nothing to
# enforce and says so rather than inventing a policy.
REQUIRED_CHECKS_KEY = "required_checks"

# The six MCP tools Plan 03 exposes. This is the whole supported operation set;
# a hook that parsed arbitrary shell text would be a second, divergent policy.
VKIT_TOOLS = frozenset({
    "project_inspect", "task_begin", "check_start",
    "run_get", "run_cancel", "task_finalize",
})

# The events this adapter answers. Anything else is a payload the host sent that
# this plugin does not declare a handler for.
EVENTS = (
    "SessionStart", "SubagentStart", "PreToolUse",
    "Stop", "SubagentStop", "TaskCompleted",
)

# Events that accept `hookSpecificOutput.additionalContext`. TaskCompleted
# decides by exit code or `continue: false` and is documented without the field,
# so the degraded response there carries only `systemMessage`.
CONTEXT_EVENTS = frozenset({
    "SessionStart", "SubagentStart", "PreToolUse", "Stop", "SubagentStop",
})

# Prefixed to every degraded message so a reader, and the plugin's own tests, can
# tell "the gate ran and found nothing" from "the gate never ran".
DEGRADED_PREFIX = "vkit: acceptance not established"

# `mcp__<server>__<tool>`, plus the plugin-scoped `mcp__plugin_<plugin>_<server>__<tool>`
# form. The matcher in hooks.json prefilters on this shape; the check below is
# the real one, because a prefilter that decides is a policy in a config file.
_MCP_TOOL = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>[^_].*)$")

#: This plugin's server key in `.mcp.json`. A live host session was recorded
#: naming the tool `mcp__plugin_vkit_vkit__project_inspect`, where the plugin
#: name and the server key are both `vkit`, so the segment between `mcp__` and
#: the tool is `vkit_vkit` and not `vkit`. Matching the server exactly therefore
#: rejected every tool a real host delivered, which made PreToolUse a silent
#: no-op: the response was `{}` for every call. A plugin-scoped name ends with
#: the server key, so that is what is compared.
VKIT_SERVER = "vkit"


def _is_vkit_server(server: str) -> bool:
    """Whether a tool name's server segment names this plugin's server.

    True for the bare `vkit` a `--mcp-config` server gets and for the
    `plugin_<plugin>_vkit` a plugin server gets. The plugin name is not parsed
    out, because a plugin name may itself contain `_` and the split would then
    be a guess; the server key is a suffix in both forms, so a suffix is what is
    checked.
    """
    return server == VKIT_SERVER or server.endswith(f"_{VKIT_SERVER}")


# The corrective action is bounded on purpose: it names the registered tool to
# call, not a command to run and not a procedure the model has to guess at.
_EVIDENCE_TOOL = "check_start"
_READ_TOOL = "run_get"
_FINALIZE_TOOL = "task_finalize"


class HookError(Exception):
    """The hook could not evaluate its gate.

    Never a verdict. A HookError means the answer is unknown, and the response
    built from it says so rather than defaulting to a stop.
    """


def _import_core() -> tuple[Any, Any, Any]:
    """Import the vkit core, adding a source checkout to the path if present.

    A source checkout puts `src/` beside the plugin directory so the adapter
    runs against the tree it ships with. An installed plugin has no such
    sibling and imports the installed package, which is the normal path.
    """
    checkout = PLUGIN_ROOT.parent / "src"
    if (checkout / "vkit").is_dir() and str(checkout) not in sys.path:
        sys.path.insert(0, str(checkout))
    try:
        from vkit import paths, storage, tasks
    except Exception as exc:  # noqa: BLE001 - a hook reports, it does not traceback
        raise HookError(f"the vkit core is not importable here: {exc}") from exc
    return paths, storage, tasks


def _open_store(project: str | None, payload: dict[str, Any]):
    """Resolve the one project root this hook reads, and open its store.

    The root comes from the caller, which is the manifest's configured project
    root, and falls back to the payload's own working directory. It is never
    taken from task prose or from a recent file, so two checkouts of one
    repository still share the one state directory git defines.
    """
    paths, storage, _ = _import_core()
    root = project or payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    if not isinstance(root, str) or not root:
        raise HookError("no project root was configured and the payload carried no cwd")
    try:
        resolved = paths.open_project(root)
    except Exception as exc:  # noqa: BLE001
        raise HookError(f"{root} is not a project vkit can read: {exc}") from exc
    return storage.Store(resolved.db_path), paths


def _bound_task_id(store, payload: dict[str, Any]) -> str | None:
    """The task this payload is registered against, or None.

    A payload with an `agent_id` only ever matches a binding that names that
    same agent, so one worker's completion gate can never be applied to a
    sibling subagent. A payload without one is a main-session event.
    """
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    agent_id = payload.get("agent_id")

    # Store has no public task listing yet; this is the same connection factory
    # `vkit.tasks` uses. Plan 03 owns a session-binding tool, and the listing
    # belongs there.
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT task_id, status, contract_json FROM tasks"
        ).fetchall()

    for task_id, status, contract_json in rows:
        if status == "closed":
            continue
        try:
            contract = json.loads(contract_json)
        except ValueError:
            continue
        if not isinstance(contract, dict):
            continue
        host = contract.get(HOST_BINDING_KEY)
        if not isinstance(host, dict) or host.get("session_id") != session_id:
            continue
        if agent_id is not None and host.get("agent_id") != agent_id:
            continue
        return task_id
    return None


def _required_checks(contract: dict[str, Any]) -> tuple[str, ...] | None:
    """The check ids the contract requires, or None when it declares none.

    None is not "no checks required", it is "no requirement was ever declared".
    The two produce different answers, and conflating them would let a contract
    that failed to record its checks read as a task with nothing to prove.
    """
    declared = contract.get(REQUIRED_CHECKS_KEY)
    if not isinstance(declared, list) or not declared:
        return None
    if not all(isinstance(item, str) and item for item in declared):
        return None
    return tuple(declared)


def _failing_runs(store, task_id: str, result: str) -> list[dict[str, Any]]:
    return [
        row for row in store.list_runs(task_id=task_id, limit=1000)
        if row["lifecycle"] == "terminal" and row["result"] == result
    ]


def _evidence_note(store, task_id: str, readiness: Any) -> str:
    """Why the task is not complete, in terms a reader can act on.

    `compute_readiness` reports a FAIL as REJECTED with no gap, because a
    failing check is an answer rather than an absence. Describing it from gaps
    alone would print "not complete" for a task whose evidence says the check
    ran and failed, which is the more useful fact.
    """
    lines: list[str] = []
    for gap in readiness.gaps:
        lines.append(f"- {gap}")

    failing = _failing_runs(store, task_id, "FAIL")
    for row in failing:
        lines.append(f"- check {row['check_id']!r} has a recorded FAIL, which is a decision, not a gap")

    blocked = _failing_runs(store, task_id, "BLOCKED")
    for row in blocked:
        lines.append(
            f"- check {row['check_id']!r} is BLOCKED ({row['reason'] or 'no reason recorded'}), "
            "so the evidence is insufficient to decide"
        )
    return "\n".join(lines)


def _mcp_tool(payload: dict[str, Any]) -> tuple[str, str] | None:
    """The (server, tool) an MCP tool payload names, if it is a vkit tool."""
    match = _MCP_TOOL.match(str(payload.get("tool_name") or ""))
    if match is None:
        return None
    server, tool = match.group("server"), match.group("tool")
    if not _is_vkit_server(server) or tool not in VKIT_TOOLS:
        return None
    return server, tool


def _with_context(event: str, response: dict[str, Any], text: str) -> dict[str, Any]:
    if event not in CONTEXT_EVENTS or not text:
        return response
    response["hookSpecificOutput"] = {
        "hookEventName": event, "additionalContext": text,
    }
    return response


# --- event handlers --------------------------------------------------------
#
# Each returns a response dict. A handler that has nothing to say returns {},
# which Claude Code parses as JSON with no field set and treats as a no-op.

def _on_session_start(store, payload: dict[str, Any]) -> dict[str, Any]:
    """Pointers only. SessionStart runs on every session, so it says very little."""
    pointers = (
        f"{PLUGIN_ROOT} holds this plugin. The vkit MCP server exposes "
        f"{', '.join(sorted(VKIT_TOOLS))}; use those for setup, runs, evidence, and "
        "finalization. A task becomes acceptable only when the app computes READY "
        "from recorded runs, and READY is local readiness, never merge permission. "
        "Skills: /vkit:work, /vkit:verify, /vkit:status."
    )
    task_id = _bound_task_id(store, payload)
    if task_id is not None:
        pointers += f" This session is registered against managed task {task_id}."
    return _with_context("SessionStart", {}, pointers)


def _on_subagent_start(store, payload: dict[str, Any]) -> dict[str, Any]:
    """Registered task references, or nothing at all.

    An unbound payload gets an empty response rather than a guessed task, so a
    fresh subagent is never handed a contract that belongs to another worker.
    """
    task_id = _bound_task_id(store, payload)
    if task_id is None:
        return {}
    return _with_context(
        "SubagentStart", {},
        f"You are registered against vkit managed task {task_id}. Its contract and "
        "required checks are records in the vkit store; read them with run_get and "
        "task_finalize rather than assuming a check has run.",
    )


def _on_pre_tool_use(store, payload: dict[str, Any]) -> dict[str, Any]:
    """State the registration for the specific operation about to run.

    It grants no permission. The MCP server already refuses an unregistered or
    unauthorized operation, and a hook that re-decided that would be a second
    acceptance algorithm with its own idea of the rules.
    """
    if _mcp_tool(payload) is None:
        return {}
    task_id = _bound_task_id(store, payload)
    if task_id is None:
        return _with_context(
            "PreToolUse", {},
            "This vkit call is not from a session registered against a managed task. "
            "task_begin is what registers one; the server binds every other operation "
            "to the task it created.",
        )
    return _with_context(
        "PreToolUse", {},
        f"Registered managed task: {task_id}. Evidence for it is whatever the store "
        f"records; use run_get rather than assuming this call's result is sufficient.",
    )


def _completion_response(
    event: str, store, payload: dict[str, Any], tasks_mod: Any, task_id: str
) -> dict[str, Any]:
    """The Stop/SubagentStop/TaskCompleted gate, translated to that event's fields.

    Readiness comes from the core and is not re-derived here. This function only
    decides whether to withhold the stop and, when it does, what to say.
    """
    task = tasks_mod.get_task(store, task_id)
    required = _required_checks(task.contract)
    if required is None:
        return _with_context(
            event, {},
            f"Managed task {task_id} declares no required checks, so there is no "
            "recorded-evidence gate to apply. Reopen it with an explicit contract "
            "rather than treating this as a pass.",
        )

    readiness = tasks_mod.compute_readiness(store, task_id, required_check_ids=required)
    if readiness.readiness == "READY":
        return {}

    note = (
        f"Managed task {task_id} is {readiness.readiness}. "
        f"{_evidence_note(store, task_id, readiness)}\n"
        f"Produce the missing evidence with the registered {_EVIDENCE_TOOL} tool, read "
        f"the run with {_READ_TOOL}, and finalize with {_FINALIZE_TOOL}. This gate reads "
        "records only: it never runs a check for you, and a clean finish here is not "
        "merge permission."
    )

    # The host sets stop_hook_active once it has already fed this gate back into
    # the turn. Blocking again would be a continuation loop that can never end,
    # so the blocker is recorded as context and the turn is allowed to close.
    if payload.get("stop_hook_active"):
        return _with_context(event, {}, note)

    return {"decision": "block", "reason": note}


def _on_completion(event: str, store, payload: dict[str, Any], tasks_mod: Any) -> dict[str, Any]:
    task_id = _bound_task_id(store, payload)
    if task_id is None:
        return {}
    return _completion_response(event, store, payload, tasks_mod, task_id)


# --- dispatch --------------------------------------------------------------

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "SessionStart": lambda store, payload, _tasks: _on_session_start(store, payload),
    "SubagentStart": lambda store, payload, _tasks: _on_subagent_start(store, payload),
    "PreToolUse": lambda store, payload, _tasks: _on_pre_tool_use(store, payload),
}


def handle(event: str, payload: dict[str, Any], project: str | None = None) -> dict[str, Any]:
    """Return the response for one host event.

    Callers that want a degraded answer instead of an exception use `respond`.
    """
    if event not in EVENTS:
        raise HookError(f"{event} is not an event this plugin declares a handler for")
    if not isinstance(payload, dict):
        raise HookError("the hook payload was not a JSON object")

    store, _paths = _open_store(project, payload)
    _, _storage, tasks_mod = _import_core()

    if event in ("Stop", "SubagentStop", "TaskCompleted"):
        return _on_completion(event, store, payload, tasks_mod)
    return _HANDLERS[event](store, payload, tasks_mod)


def degraded(event: str, detail: str) -> dict[str, Any]:
    """The response for a hook that could not evaluate its gate.

    It never blocks. A broken integration must not wedge a session, and the host
    is right to treat a hook failure as non-blocking. What it does do is state
    plainly that acceptance was not established, so the failure cannot be read as
    a clean pass.
    """
    message = f"{DEGRADED_PREFIX} - {detail}"
    response: dict[str, Any] = {"systemMessage": message}
    return _with_context(event, response, message)


def respond(event: str, payload: Any, project: str | None = None) -> tuple[dict[str, Any], int]:
    """Never raise. Return a response and the process exit code.

    Exit is always 0. Claude Code treats a non-zero exit from a standard
    decision event as a hook error, and on this event set that would report a
    failure we already described precisely in the response.
    """
    try:
        return handle(event, payload, project), 0
    except HookError as exc:
        return degraded(event, str(exc)), 0
    except Exception as exc:  # noqa: BLE001 - a hook must not traceback at a user
        return degraded(event, f"the gate raised {type(exc).__name__}: {exc}"), 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    event = args[0] if args else ""
    project = None
    if "--project" in args:
        index = args.index("--project")
        if index + 1 >= len(args):
            print(json.dumps(degraded(event, "--project was given without a value")))
            return 0
        project = args[index + 1]

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        response = degraded(event, f"the hook payload was not valid JSON: {exc}")
    else:
        response, _code = respond(event, payload, project)

    print(json.dumps(response))
    # Always 0. Claude Code reads a non-zero exit from a standard decision event
    # as a hook error, and the response above already says exactly what happened.
    return 0


if __name__ == "__main__":
    sys.exit(main())

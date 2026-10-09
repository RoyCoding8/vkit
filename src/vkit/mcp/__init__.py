"""MCP tools for one project root. Agents ask what is known, run registered checks, and read the gate."""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jsonschema import ValidationError, validate

from .. import __version__, query
from ..manifest import ManifestError
from ..store import new_run_id

MAX_WAIT_SECONDS = 60
INSTRUCTIONS = (
    "Verification evidence for one Git repository. Call status (optionally with the paths you changed) to see "
    "which registered checks are fresh, stale or missing; check_run with needed=true runs exactly the stale ones; "
    "run_get reads a run's outcome and log; gate says READY only when every check passed against the current "
    "inputs. features describes what the application does and how a user reaches each feature. Check ids come "
    "from the manifest; no tool accepts a command, and only a human can accept a check definition. "
    "check_rewrite and simplify_function compute over a restricted Python integer/boolean model; "
    "reduce_failure shrinks a recorded failure using its approved check; compare_matchsets compares complete "
    "regex languages over a stated finite alphabet. Computation results do not change the gate."
)


class MCPUnavailable(Exception):
    """The `mcp` SDK is not installed."""


class Refusal(Exception):
    """A request this server will not honour; returned to the client as an error result."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    handler: Callable[["Server", dict[str, Any]], Any]
    read_only: bool = True
    required: tuple[str, ...] = ()

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.required),
                "additionalProperties": False}


def _check(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise Refusal("arguments must be an object")
    unknown = sorted(set(arguments) - set(schema["properties"]))
    if unknown:
        raise Refusal(f"unknown argument(s): {', '.join(unknown)}")
    try:
        validate(arguments, schema)
    except ValidationError as exc:
        raise Refusal(exc.message) from exc


@dataclass
class Server:
    root: Path
    threads: dict[str, threading.Thread] = field(default_factory=dict)

    def context(self) -> query.Context:
        return query.open_context(self.root)

    def call(self, name: str, arguments: dict[str, Any] | None) -> tuple[Any, bool]:
        tool = BY_NAME.get(name)
        if tool is None:
            return {"error": f"unknown tool {name!r}"}, True
        arguments = {} if arguments is None else arguments
        try:
            _check(arguments, tool.input_schema)
            return tool.handler(self, arguments), False
        except (Refusal, ManifestError, ValueError) as exc:
            return {"error": str(exc)}, True

    def start(self, ctx: query.Context, check_id: str) -> str:
        from ..runner import run_check

        run_id = new_run_id()
        thread = threading.Thread(target=run_check, args=(ctx.project, ctx.require_manifest(), check_id),
                                  kwargs={"store": ctx.store, "run_id": run_id}, daemon=True)
        thread.start()
        self.threads[run_id] = thread
        return run_id


def _status(server: Server, args: dict[str, Any]) -> Any:
    return query.status(server.context(), args.get("paths", ()))


def _gate(server: Server, args: dict[str, Any]) -> Any:
    return query.status(server.context())["gate"]


def _features(server: Server, args: dict[str, Any]) -> Any:
    report = query.status(server.context())
    if report["feature_error"]:
        raise Refusal(report["feature_error"])
    words = str(args.get("query", "")).lower().split()
    return {"features": [f for f in report["features"]
                         if all(w in json.dumps(f).lower() for w in words)]}


def _summary(ctx: query.Context, run_id: str) -> dict[str, Any]:
    record = ctx.store.record(run_id)
    if record is None:
        return {"run_id": run_id, "state": "starting"}
    return {"run_id": run_id, "check_id": record["check_id"], "state": record["state"],
            "outcome": record["outcome"]}


def _check_run(server: Server, args: dict[str, Any]) -> Any:
    ctx = server.context()
    manifest = ctx.require_manifest()
    report = query.status(ctx)
    running = {c["id"]: c["running"][0] for c in report["checks"] if c["running"]}
    if args.get("needed"):
        wanted = report["needs_run"]
    else:
        wanted = args.get("check_ids") or []
        if not wanted:
            raise Refusal("pass check_ids, or needed=true to run every stale or missing check")
        for check_id in wanted:
            manifest.require(check_id)
    run_ids = [running.get(c) or server.start(ctx, c) for c in wanted]
    deadline = time.monotonic() + min(max(int(args.get("wait_seconds", 20)), 0), MAX_WAIT_SECONDS)
    while time.monotonic() < deadline and any(
            server.threads.get(r) is not None and server.threads[r].is_alive() for r in run_ids):
        time.sleep(0.2)
    return {"runs": [_summary(ctx, r) for r in run_ids],
            "note": "poll run_get for runs still running" if any(
                _summary(ctx, r)["state"] in ("running", "starting") for r in run_ids) else ""}


def _run_get(server: Server, args: dict[str, Any]) -> Any:
    if "run_id" not in args:
        raise Refusal("run_id is required")
    view = query.run_view(server.context(), args["run_id"], log=args.get("log", "stdout"),
                          offset=args.get("log_offset", 0), limit=min(args.get("log_limit", 16_000), 64_000))
    if view is None:
        raise Refusal(f"no run {args['run_id']!r}")
    return view


def _propose(server: Server, args: dict[str, Any]) -> Any:
    from ..proposals import ProposalError, submit

    ctx = server.context()
    try:
        proposal = submit(ctx.project, ctx.store, args)
    except ProposalError as exc:
        raise Refusal(str(exc)) from exc
    return {"proposal": proposal.digest, "next": f"a human reviews it with `vkit proposals` and applies it with "
                                                  f"`vkit accept --proposal {proposal.digest[:12]}`"}


def _run_cancel(server: Server, args: dict[str, Any]) -> Any:
    if "run_id" not in args:
        raise Refusal("run_id is required")
    ctx = server.context()
    record = ctx.store.record(args["run_id"])
    if record is None:
        raise Refusal(f"no run {args['run_id']!r}")
    if record["state"] != "running":
        return {"run_id": args["run_id"], "cancelled": False, "state": record["state"]}
    ctx.store.request_cancel(args["run_id"])
    return {"run_id": args["run_id"], "cancelled": True}


def _check_rewrite(server: Server, args: dict[str, Any]) -> Any:
    from ..operations import check_rewrite

    return check_rewrite(server.root, **args)


def _simplify_function(server: Server, args: dict[str, Any]) -> Any:
    from ..operations import simplify_function

    return simplify_function(server.root, **args)


def _reduce_failure(server: Server, args: dict[str, Any]) -> Any:
    from ..operations import reduce_failure

    return reduce_failure(server.root, **args)


def _compare_matchsets(server: Server, args: dict[str, Any]) -> Any:
    from ..operations.matchsets import compare_matchsets

    return compare_matchsets(**args).to_json()


_STR = {"type": "string"}
_PATHS = {"type": "array", "items": _STR, "description": "repository-relative paths"}
_EXPRESSION = {"path": {"type": "string", "description": "repository-relative Python source file"},
               "function": {"type": "string", "minLength": 1},
               "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60_000}}
TOOLS = (
    Tool("compare_matchsets", "Compare full-match acceptance sets in the fixed greenery regex dialect over the "
         "supplied alphabet. Returns EQUIVALENT or COUNTEREXAMPLE with shortest directional witnesses; "
         "UNKNOWN on timeout, UNSUPPORTED for unsupported inputs, or UNAVAILABLE without the backend.",
         {"old_pattern": {"type": "string", "maxLength": 2048},
          "new_pattern": {"type": "string", "maxLength": 2048},
          "alphabet": {"type": "string", "maxLength": 64},
          "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60_000}},
         _compare_matchsets, required=("old_pattern", "new_pattern", "alphabet")),
    Tool("check_rewrite", "Prove equal return values for a supported pure Python int/bool function and a "
         "replacement function with the same signature. Returns PROVED, COUNTEREXAMPLE, UNKNOWN, UNSUPPORTED "
         "or UNAVAILABLE, with the modeled domain and source digest. Does not execute or modify the source.",
         {**_EXPRESSION, "replacement": {"type": "string", "maxLength": 65_536}}, _check_rewrite,
         required=("path", "function", "replacement")),
    Tool("simplify_function", "Derive a smaller equivalent pure Python int/bool function using fixed egglog "
         "rules and independently check it with cvc5. Returns a suggested replacement and the proof status; "
         "does not modify files or establish a global minimum.", _EXPRESSION, _simplify_function,
         required=("path", "function")),
    Tool("reduce_failure", "Reduce one input file of a recorded, still-current failed run using Perses. "
         "Reuses the approved check and preserves the recorded failure. Returns a validated reproducer "
         "without modifying the checkout. Requires the configured POSIX Java/Perses runtime.",
         {"run_id": _STR, "path": _STR,
          "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300}}, _reduce_failure,
         read_only=False, required=("run_id", "path")),
    Tool("status", "Freshness of every registered check (fresh_pass, fresh_fail, fresh_blocked, stale, missing, "
         "not_approved), what each kind of evidence establishes and does not, the gate, and the features. Pass "
         "paths to see only the checks and features that read them, and which paths no check reads.",
         {"paths": _PATHS}, _status),
    Tool("features", "The feature map: what users can do, how they reach it, which checks cover it, and known "
         "gaps. Filter with a free-text query.", {"query": _STR}, _features),
    Tool("check_run", "Run registered checks by id, or needed=true for every stale or missing one. Waits up to "
         "wait_seconds (max 60) and returns each run's state and outcome.",
         {"check_ids": {"type": "array", "items": _STR}, "needed": {"type": "boolean"},
          "wait_seconds": {"type": "integer"}}, _check_run, read_only=False),
    Tool("run_get", "One run: state, outcome, obligations, inputs, and a window of its stdout or stderr.",
         {"run_id": _STR, "log": {"type": "string", "enum": ["stdout", "stderr"]},
          "log_offset": {"type": "integer", "description": "negative counts from the end"}, "log_limit": {"type": "integer"}}, _run_get),
    Tool("run_cancel", "Stop a running check and everything it started.", {"run_id": _STR}, _run_cancel,
         read_only=False),
    Tool("propose", "Suggest new or changed checks (manifest v2 entries), feature map entries, and new files they "
         "need, with a rationale. Nothing runs or changes until a human accepts it. Use it to move a lesson up "
         "the trust ladder: a recurring mistake becomes a static rule or a scenario check.",
         {"checks": {"type": "array", "items": {"type": "object"}}, "features": {"type": "array",
          "items": {"type": "object"}}, "files": {"type": "object", "description": "new path -> text"},
          "rationale": _STR}, _propose, read_only=False),
    Tool("gate", "READY only when every registered check passed against the current inputs; REJECTED when one "
         "failed; otherwise BLOCKED with the checks that lack fresh evidence.", {}, _gate),
)
BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_definitions() -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema,
             "annotations": {"readOnlyHint": t.read_only}} for t in TOOLS]


def serve_stdio(root: str | Path) -> int:
    try:
        import anyio
        import mcp.types as types
        from mcp.server.lowlevel import Server as SdkServer
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise MCPUnavailable(f"the MCP SDK is not installed ({exc}); install vkit[mcp]") from exc
    server = Server(Path(root))

    async def list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"],
                       annotations=types.ToolAnnotations(readOnlyHint=t["annotations"]["readOnlyHint"]))
            for t in tool_definitions()])

    async def call_tool(_ctx: Any, params: Any) -> Any:
        body, is_error = await anyio.to_thread.run_sync(server.call, params.name, params.arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body, indent=2))],
                                    isError=is_error)

    sdk = SdkServer(name=Path(root).name, version=__version__, instructions=INSTRUCTIONS,
                    on_list_tools=list_tools, on_call_tool=call_tool)

    async def main() -> None:
        print(f"vkit mcp serving {root}", file=sys.stderr, flush=True)
        async with stdio_server() as (read_stream, write_stream):
            await sdk.run(read_stream, write_stream, sdk.create_initialization_options())

    anyio.run(main)
    return 0
